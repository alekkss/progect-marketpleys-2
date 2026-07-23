"""
Маршруты для просмотра задач и скачивания результатов.

Эндпоинты:
    GET /tasks                          — список задач пользователя (HTML)
    GET /api/tasks/{id}/status          — JSON-статус задачи (polling fallback)
    GET /tasks/{id}/download/{filename} — скачивание файла результата

Поток:
    1. Пользователь запускает обработку → задача появляется в списке
    2. WebSocket показывает прогресс в real-time
    3. Если WebSocket недоступен — фронтенд polling через /api/tasks/{id}/status
    4. По завершении — кнопки скачивания для каждого файла

Безопасность:
    - Пользователь может скачивать только свои результаты
    - Путь к файлу проверяется на path traversal (../)
    - Файлы отдаются через aiohttp FileResponse (streaming)

Паттерн: Controller — HTTP → БД → ответ (JSON или файл).
"""

from pathlib import Path

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from web.auth.decorators import login_required
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.tasks")


@login_required
async def tasks_list(request: Request) -> Response:
    """
    GET /tasks — список задач пользователя.

    Отображает все задачи (от новых к старым) с их статусами,
    датами и кнопками скачивания для завершённых.

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница со списком задач
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]

    tasks = await storage.db.get_user_task_results(web_user_id, limit=50)
    csrf_token = get_csrf_token(request)

    context = {
        "tasks": tasks,
        "csrf_token": csrf_token,
        "user": user_data,
    }

    return aiohttp_jinja2.render_template("tasks/list.html", request, context)


@login_required
async def task_status_api(request: Request) -> Response:
    """
    GET /api/tasks/{id}/status — JSON-статус задачи.

    Polling fallback: если WebSocket недоступен, фронтенд
    опрашивает этот эндпоинт каждые 3 секунды.

    Возвращает:
        - status: pending | processing | completed | failed
        - output_files: {filename: path} (при completed)
        - error_message: str (при failed)
        - stats: {} (при completed)

    Args:
        request: HTTP-запрос

    Returns:
        JSON с текущим статусом задачи
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]
    task_id = request.match_info["id"]

    task_result = await storage.db.get_task_result(task_id)

    if not task_result:
        return web.json_response(
            {"error": "Задача не найдена"}, status=404,
        )

    # Проверяем принадлежность задачи пользователю
    if task_result["web_user_id"] != web_user_id:
        return web.json_response(
            {"error": "Нет доступа к этой задаче"}, status=403,
        )

    # Формируем ответ
    response_data = {
        "task_id": task_result["task_id"],
        "status": task_result["status"],
        "created_at": task_result["created_at"],
        "completed_at": task_result["completed_at"],
    }

    if task_result["status"] == "completed":
        # Добавляем список файлов для скачивания
        output_files = task_result.get("output_files") or {}
        response_data["files"] = [
            {
                "filename": filename,
                "download_url": f"/tasks/{task_id}/download/{filename}",
            }
            for filename in output_files.keys()
        ]
        response_data["stats"] = task_result.get("stats")

    elif task_result["status"] == "failed":
        response_data["error_message"] = task_result.get("error_message", "")

    return web.json_response(response_data)


@login_required
async def task_download(request: Request) -> Response:
    """
    GET /tasks/{id}/download/{filename} — скачивание файла результата.

    Безопасность:
        - Проверяет принадлежность задачи пользователю
        - Проверяет, что filename есть в output_files задачи
        - Защита от path traversal (../ в filename)
        - Файл отдаётся через FileResponse (streaming, не в память)

    Args:
        request: HTTP-запрос

    Returns:
        FileResponse с файлом или 404/403
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]
    task_id = request.match_info["id"]
    filename = request.match_info["filename"]

    # Защита от path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise web.HTTPBadRequest(reason="Некорректное имя файла")

    # Получаем результат задачи
    task_result = await storage.db.get_task_result(task_id)

    if not task_result:
        raise web.HTTPNotFound(reason="Задача не найдена")

    # Проверяем принадлежность
    if task_result["web_user_id"] != web_user_id:
        raise web.HTTPForbidden(reason="Нет доступа к этой задаче")

    # Проверяем статус
    if task_result["status"] != "completed":
        raise web.HTTPNotFound(reason="Результаты ещё не готовы")

    # Проверяем наличие файла в output_files
    output_files = task_result.get("output_files") or {}
    file_path = output_files.get(filename)

    if not file_path:
        raise web.HTTPNotFound(reason="Файл не найден в результатах задачи")

    # Проверяем существование файла на диске
    file_path_obj = Path(file_path)
    if not file_path_obj.exists() or not file_path_obj.is_file():
        logger.error(
            "Файл результата не найден на диске: %s (task_id=%s)",
            file_path, task_id,
        )
        raise web.HTTPNotFound(reason="Файл не найден на диске (возможно, удалён уборщиком)")

    logger.info(
        "Скачивание файла: %s (task_id=%s, web_user_id=%d)",
        filename, task_id, web_user_id,
    )

    return web.FileResponse(
        path=file_path_obj,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def shorten_filename(filename: str) -> str:
    """
    Сокращает имя файла для отображения в кнопке скачивания.

    Регистрируется как Jinja2 global-функция в web/app.py:
        env.globals["_shorten_filename"] = shorten_filename

    Использование в шаблонах:
        {{ _shorten_filename(filename) }}

    Args:
        filename: Полное имя файла

    Returns:
        Короткое отображаемое имя
    """
    lower = filename.lower()
    if "wb" in lower or "wildberries" in lower:
        return "📥 WB"
    if "ozon" in lower:
        return "📥 Ozon"
    if "яндекс" in lower or "yandex" in lower:
        return "📥 Яндекс"
    if "отчёт" in lower or "report" in lower:
        return "📥 Отчёт"
    return f"📥 {filename[:20]}"


def setup_tasks_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты задач.

    Функция _shorten_filename регистрируется в Jinja2 globals
    централизованно в web/app.py → _setup_jinja2().

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/tasks", tasks_list)
    app.router.add_get("/api/tasks/{id}/status", task_status_api)
    app.router.add_get("/tasks/{id}/download/{filename:.+}", task_download)
