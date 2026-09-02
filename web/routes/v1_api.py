"""
REST API AI-агента маппинга PIM+FDM (маршруты /v1/*).

Реализует транспортную часть протокола обмена
(разделы 2-4 спецификации):

    POST /v1/mapping-tasks         — создание задания, 202 Accepted
    GET  /v1/mapping-tasks/{jobId} — статус и результат (поллинг FDM)

DELETE /v1/mapping-tasks/{jobId} отложен по согласованию и НЕ
регистрируется: незарегистрированный метод aiohttp автоматически
возвращает 405 Method Not Allowed (в JSON-формате — см. шаг 15).

Разделение ответственности (Controller layer — тонкие контроллеры):
    - аутентификация   — api_auth_middleware (Bearer, шаг 13)
    - валидация тела   — services/mapping/validators.py
    - хранение/очередь — database.Database (mapping_jobs)
    - обработка        — services/mapping/job_worker.py

Ошибки возвращаются НАПРЯМУЮ через web.json_response с точным
текстом (п. 1.5 доработок): errors-middleware содержит только
обобщённые тексты, поднимать HTTPException здесь было бы потерей
диагностических сообщений валидации для FDM. Непредвиденные
исключения (например, сбой БД) перехватит errors-middleware → 500.
"""

import secrets
from typing import Dict, List, Optional

from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from services.mapping.models import (
    AttributeMappingTask,
    ReferenceValueMappingTask,
)
from services.mapping.validators import MappingValidationError, parse_mapping_task
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.v1_api")

# Длина идентификатора задания: 16 байт → 32 hex-символа
_JOB_ID_BYTES: int = 16


def _error_response(status: int, message: str) -> Response:
    """
    Формирует JSON-ответ об ошибке API в едином формате.

    Формат совпадает с api_auth_middleware и errors-middleware:
    {"error": true, "status": N, "message": "..."}.

    Args:
        status: HTTP-статус код
        message: Точная диагностическая причина

    Returns:
        aiohttp Response с JSON-телом
    """
    return web.json_response(
        {"error": True, "status": status, "message": message},
        status=status,
    )


async def create_mapping_task(request: Request) -> Response:
    """
    POST /v1/mapping-tasks — принимает задание на маппинг от FDM.

    Порядок:
        1. Разбор JSON-тела (невалидный JSON → 400).
        2. Валидация по спецификации протокола (ошибки → 422).
        3. Генерация jobId, сохранение задания в статусе pending.
        4. Ответ 202 Accepted: {"jobId": ..., "status": "pending"}.

    Обработка задания выполняется асинхронно MappingJobWorker'ом —
    FDM получает статус и результат через GET-поллинг.

    Args:
        request: HTTP-запрос с JSON-телом задания

    Returns:
        202 с jobId | 400 | 422 | 500
    """
    # --- 1. Разбор JSON-тела ---
    try:
        payload = await request.json()
    except ValueError:
        # json.JSONDecodeError и UnicodeDecodeError — потомки ValueError:
        # покрывает невалидный синтаксис и не-UTF8 тело одним except
        logger.warning(
            "API агента: невалидное JSON-тело (IP=%s, метод=POST)",
            request.remote,
        )
        return _error_response(
            400, "Тело запроса не является корректным JSON (UTF-8)"
        )

    # --- 2. Валидация по спецификации ---
    try:
        task = parse_mapping_task(payload)
    except MappingValidationError as e:
        logger.warning(
            "API агента: ошибка валидации (статус=%d): %s", e.status, e.message
        )
        return _error_response(e.status, e.message)

    # --- 3. Данные для дашборда + сохранение задания ---
    if isinstance(task, AttributeMappingTask):
        category_name: Optional[str] = task.category.name
        attribute_name: Optional[str] = None
    elif isinstance(task, ReferenceValueMappingTask):
        category_name = None
        attribute_name = task.attribute.name
    else:
        # Ветка недостижима (валидатор пропускает только два типа),
        # но защищает от регрессии при добавлении новых типов
        return _error_response(422, "Неизвестный тип задания")

    channels: List[Dict] = [
        {
            "platform": channel.platform,
            "name": channel.name,
            "schemaChannelId": channel.schema_channel_id,
        }
        for channel in task.channels
    ]

    job_id = secrets.token_hex(_JOB_ID_BYTES)

    try:
        await storage.db.create_mapping_job(
            job_id=job_id,
            task_type=task.task_type,
            schema_id=task.schema_id,
            payload=payload,
            channels=channels,
            category_name=category_name,
            attribute_name=attribute_name,
        )
    except Exception as e:
        logger.error(
            "API агента: не удалось сохранить задание: %s", e, exc_info=True
        )
        return _error_response(500, "Внутренняя ошибка при создании задания")

    logger.info(
        "API агента: задание принято job_id=%s, тип=%s, схема=%s (IP=%s)",
        job_id, task.task_type, task.schema_id, request.remote,
    )

    # --- 4. 202 Accepted по протоколу ---
    return web.json_response(
        {"jobId": job_id, "status": "pending"},
        status=202,
    )


async def get_mapping_task_status(request: Request) -> Response:
    """
    GET /v1/mapping-tasks/{jobId} — статус и результат задания.

    Формат ответа строго по протоколу (разделы 2.4, 3.2, 4.2):
        - pending / processing → {"jobId", "status"}
        - completed → {"jobId", "status", ...result}
          (result содержит results+unresolved для attribute_mapping
          или channels+matches для reference_value_mapping)
        - failed → {"jobId", "status", "error"}

    Результат читается из БД одним SELECT — источник истины о
    статусе тот же, куда пишет MappingJobWorker.

    Args:
        request: HTTP-запрос; jobId в параметрах маршрута

    Returns:
        200 с телом статуса | 404 если задание не найдено
    """
    job_id = request.match_info["job_id"]

    job = await storage.db.get_mapping_job(job_id)

    if job is None:
        logger.debug("API агента: задание не найдено job_id=%s", job_id)
        return _error_response(404, f"Задание {job_id} не найдено")

    status = job["status"]

    # --- Завершено: разворачиваем сохранённый результат ---
    if status == "completed":
        response: Dict = {"jobId": job_id, "status": "completed"}
        result = job.get("result") or {}
        response.update(result)
        return web.json_response(response)

    # --- Ошибка: текст причины в поле error ---
    if status == "failed":
        return web.json_response({
            "jobId": job_id,
            "status": "failed",
            "error": job.get("error_message") or "Ошибка обработки задания",
        })

    # --- Отменено: зарезервированный статус (DELETE отложен) ---
    if status == "cancelled":
        return web.json_response({
            "jobId": job_id,
            "status": "cancelled",
            "error": "Задание отменено",
        })

    # --- Промежуточный статус: только jobId и status ---
    return web.json_response({"jobId": job_id, "status": status})


def setup_v1_api_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты внешнего API агента маппинга.

    Вызывается из web/routes/__init__.py → setup_routes().
    Порядок относительно других маршрутов не важен: префикс /v1/
    не пересекается с внутренними маршрутами приложения.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_post("/v1/mapping-tasks", create_mapping_task)
    app.router.add_get("/v1/mapping-tasks/{job_id}", get_mapping_task_status)
    logger.info("Маршруты API агента (/v1/*) зарегистрированы")
