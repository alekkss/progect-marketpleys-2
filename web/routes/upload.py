"""
Маршруты загрузки файлов и запуска обработки.

Эндпоинты:
    GET  /upload         — страница загрузки (выбор схемы + drag&drop)
    POST /upload/files   — приём файлов (multipart/form-data), сохранение на диск
    POST /upload/process — запуск обработки (создание Task → TaskQueue)

Поток загрузки:
    1. GET /upload — пользователь видит список схем и зону drag&drop
    2. POST /upload/files — JS отправляет файлы, сервер определяет МП по имени
    3. Сервер возвращает JSON: {"files": {"wildberries": "/path/...", ...}}
    4. POST /upload/process — пользователь выбирает схему, нажимает "Обработать"
    5. Сервер создаёт Task(delivery_channel="web") → task_queue.enqueue()
    6. Ответ: {"task_id": "uuid"} — фронтенд подключает WebSocket

Паттерн: Controller — принимает HTTP, делегирует сохранение файлов
и создание задач соответствующим сервисам.
"""

import uuid
from pathlib import Path

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from config.config import Config
from services.task_queue import Task
from web.auth.decorators import login_required
from web.auth.permissions import WebAccessManager
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.upload")

# Допустимые расширения файлов
_ALLOWED_EXTENSIONS_EXCEL: set = {".xlsx"}
_ALLOWED_EXTENSIONS_XML: set = {".xml"}

# Максимальный размер файла (250 МБ, совпадает с Nginx client_max_body_size)
_MAX_FILE_SIZE: int = 250 * 1024 * 1024

# Ключевые слова для определения маркетплейса по имени файла
_MP_KEYWORDS: dict = {
    "wildberries": ["wb", "wildberries", "вайлдберриз", "вб"],
    "ozon": ["ozon", "озон"],
    "yandex": ["yandex", "яндекс", "ymarket", "маркет"],
}


@login_required
async def upload_page(request: Request) -> Response:
    """
    GET /upload — страница загрузки файлов.

    Отображает:
        - Зону drag&drop для файлов
        - Список доступных схем для выбора
        - Кнопку запуска обработки

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница загрузки
    """
    user_data = request["user"]
    can_see_all = WebAccessManager.can_see_all_schemas(user_data)
    telegram_user_id = user_data.get("telegram_user_id")

    # Получаем доступные схемы
    if can_see_all:
        schemas = await storage.db.get_user_schemas(0, all_schemas=True)
    elif telegram_user_id:
        schemas = await storage.db.get_user_schemas(telegram_user_id, all_schemas=False)
    else:
        schemas = []

    csrf_token = get_csrf_token(request)

    context = {
        "schemas": schemas,
        "csrf_token": csrf_token,
        "user": user_data,
    }

    return aiohttp_jinja2.render_template("upload/index.html", request, context)


@login_required
async def upload_files_handler(request: Request) -> Response:
    """
    POST /upload/files — приём файлов через multipart/form-data.

    Определяет маркетплейс по имени файла. Сохраняет в UPLOAD_DIR
    с уникальным префиксом (UUID). Возвращает JSON с путями.

    Ожидает поля формы:
        - files[] — один или несколько файлов (.xlsx или .xml)

    Args:
        request: HTTP-запрос с multipart данными

    Returns:
        JSON: {"files": {"wildberries": "/path/...", ...}, "xml": "/path/..." | null}
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]

    # Уникальная директория для этой загрузки
    upload_id = str(uuid.uuid4())[:8]
    upload_dir = Path(Config.UPLOAD_DIR) / f"web_{web_user_id}_{upload_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_files: dict = {}
    xml_file_path: str | None = None
    errors: list = []

    try:
        reader = await request.multipart()

        async for part in reader:
            if part.name != "files[]" and part.name != "files":
                # Пропускаем CSRF и другие не-файловые поля
                await part.read()
                continue

            filename = part.filename
            if not filename:
                continue

            # Проверка расширения
            ext = Path(filename).suffix.lower()
            if ext not in _ALLOWED_EXTENSIONS_EXCEL and ext not in _ALLOWED_EXTENSIONS_XML:
                errors.append(f"Недопустимый формат: {filename} (только .xlsx и .xml)")
                await part.read()
                continue

            # Чтение файла с проверкой размера
            file_data = bytearray()
            while True:
                chunk = await part.read_chunk(8192)
                if not chunk:
                    break
                file_data.extend(chunk)
                if len(file_data) > _MAX_FILE_SIZE:
                    errors.append(f"Файл слишком большой: {filename} (макс. 250 МБ)")
                    break

            if len(file_data) > _MAX_FILE_SIZE:
                continue

            # Сохранение файла
            safe_filename = f"{upload_id}_{filename}"
            file_path = upload_dir / safe_filename

            with open(file_path, "wb") as f:
                f.write(file_data)

            # Определяем тип файла
            if ext in _ALLOWED_EXTENSIONS_XML:
                xml_file_path = str(file_path)
                logger.info(
                    "XML файл загружен: %s (%.1f МБ)",
                    filename, len(file_data) / 1024 / 1024,
                )
            else:
                # Определяем маркетплейс по имени файла
                marketplace = _detect_marketplace(filename)
                if marketplace:
                    saved_files[marketplace] = str(file_path)
                    logger.info(
                        "Файл %s определён как %s (%.1f МБ)",
                        filename, marketplace, len(file_data) / 1024 / 1024,
                    )
                else:
                    errors.append(
                        f"Не удалось определить маркетплейс: {filename}. "
                        f"Имя файла должно содержать wb/ozon/yandex."
                    )

    except Exception as e:
        logger.error("Ошибка загрузки файлов: %s", e, exc_info=True)
        return web.json_response(
            {"error": f"Ошибка загрузки: {e}"}, status=500,
        )

    # Результат
    response_data = {
        "files": saved_files,
        "xml": xml_file_path,
        "errors": errors,
        "upload_dir": str(upload_dir),
    }

    return web.json_response(response_data)


@login_required
async def upload_process_handler(request: Request) -> Response:
    """
    POST /upload/process — запуск обработки файлов.

    Создаёт Task с delivery_channel="web" и ставит в очередь.
    TaskWorker обработает задачу и уведомит через WebSocket.

    Ожидает JSON-тело:
        {
            "schema_id": int,
            "files": {"wildberries": "/path/...", ...},
            "xml": "/path/..." | null
        }

    Args:
        request: HTTP-запрос с JSON

    Returns:
        JSON: {"task_id": "uuid"} или {"error": "..."}
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]
    telegram_user_id = user_data.get("telegram_user_id") or 0

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {"error": "Некорректный JSON"}, status=400,
        )

    schema_id = data.get("schema_id")
    file_paths = data.get("files", {})
    xml_file_path = data.get("xml")

    # Валидация
    if not schema_id:
        return web.json_response(
            {"error": "Не выбрана схема сопоставлений"}, status=400,
        )

    if not file_paths:
        return web.json_response(
            {"error": "Не загружены файлы маркетплейсов"}, status=400,
        )

    # Проверяем наличие хотя бы 2 МП-файлов
    mp_count = sum(1 for k in file_paths if k in ("wildberries", "ozon", "yandex"))
    if mp_count < 2:
        return web.json_response(
            {"error": "Необходимо минимум 2 файла маркетплейсов"}, status=400,
        )

    # Проверяем существование файлов
    for mp, path in file_paths.items():
        if not Path(path).exists():
            return web.json_response(
                {"error": f"Файл не найден: {mp}"}, status=400,
            )

    # Определяем тип задачи по типу схемы
    schema_type = await storage.db.get_schema_type(schema_id)
    task_type = "mvm" if schema_type == "mvm" else "standard"

    # Проверяем XML для МВМ
    if task_type == "mvm" and not xml_file_path:
        return web.json_response(
            {"error": "Для МВМ-схемы требуется XML файл каталога"}, status=400,
        )

    # Создаём директорию для результатов
    task_id = str(uuid.uuid4())
    output_dir = Path(Config.OUTPUT_DIR) / f"web_{web_user_id}_{task_id[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = str(output_dir / "Отчёт_синхронизации.xlsx")

    # Создаём задачу
    task = Task(
        user_id=telegram_user_id,
        chat_id=0,  # Не используется для веб-задач
        task_type=task_type,
        schema_id=schema_id,
        file_paths=file_paths,
        output_dir=str(output_dir),
        report_path=report_path,
        xml_file_path=xml_file_path,
        delivery_channel="web",
        web_user_id=web_user_id,
        id=task_id,
    )

    # Создаём запись в task_results (для отслеживания и скачивания)
    await storage.db.create_task_result(task_id, web_user_id)

    # Ставим задачу в очередь
    task_queue = request.app["task_queue"]
    await task_queue.enqueue(task)

    queue_length = await task_queue.get_queue_length()

    logger.info(
        "Веб-задача %s создана: schema_id=%d, type=%s, web_user_id=%d (очередь: %d)",
        task_id, schema_id, task_type, web_user_id, queue_length,
    )

    return web.json_response({
        "task_id": task_id,
        "queue_position": queue_length,
    })


def _detect_marketplace(filename: str) -> str | None:
    """
    Определяет маркетплейс по имени файла.

    Ищет ключевые слова в имени файла (без учёта регистра).

    Args:
        filename: Имя загруженного файла

    Returns:
        Название маркетплейса или None если не определён
    """
    name_lower = filename.lower()

    for marketplace, keywords in _MP_KEYWORDS.items():
        for keyword in keywords:
            if keyword in name_lower:
                return marketplace

    return None


def setup_upload_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты загрузки.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/upload", upload_page)
    app.router.add_post("/upload/files", upload_files_handler)
    app.router.add_post("/upload/process", upload_process_handler)
