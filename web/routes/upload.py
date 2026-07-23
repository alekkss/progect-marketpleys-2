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

import os
import uuid
from pathlib import Path

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

    html = _render_upload_page(
        schemas=schemas,
        csrf_token=csrf_token,
    )
    return Response(text=html, content_type="text/html")


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


# ===================================================================
# Временный HTML-шаблон (будет заменён на Jinja2 в Фазе 4)
# ===================================================================


def _render_upload_page(schemas: list, csrf_token: str) -> str:
    """Генерирует HTML страницы загрузки."""

    # Опции выбора схемы
    schema_options = ""
    for schema in schemas:
        type_icon = "📦" if schema.get("schema_type") == "mvm" else "📋"
        schema_options += (
            f'<option value="{schema["id"]}" '
            f'data-type="{schema.get("schema_type", "standard")}">'
            f'{type_icon} {schema["name"]}</option>\n'
        )

    if not schema_options:
        schema_options = '<option value="" disabled>Нет доступных схем</option>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Загрузка файлов — Marketplace Sync</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f1f5f9; color: #1e293b; line-height: 1.6;
        }}
        .navbar {{
            background: white; border-bottom: 1px solid #e2e8f0;
            padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center;
        }}
        .navbar-brand {{ font-weight: 700; font-size: 1.25rem; color: #3b82f6; text-decoration: none; }}
        .navbar-nav {{ display: flex; gap: 1.5rem; }}
        .navbar-nav a {{ color: #64748b; text-decoration: none; font-size: 0.875rem; font-weight: 500; }}
        .navbar-nav a:hover {{ color: #3b82f6; }}
        .container {{ max-width: 800px; margin: 0 auto; padding: 2rem; }}
        .page-title {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 1.5rem; }}
        .card {{
            background: white; border-radius: 0.75rem; padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1.5rem;
        }}
        .card-title {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 1rem; }}
        .drop-zone {{
            border: 2px dashed #cbd5e1; border-radius: 0.75rem; padding: 3rem;
            text-align: center; cursor: pointer; transition: all 0.2s;
        }}
        .drop-zone:hover, .drop-zone.drag-over {{
            border-color: #3b82f6; background: #eff6ff;
        }}
        .drop-zone-text {{ color: #64748b; font-size: 1rem; }}
        .drop-zone-hint {{ color: #94a3b8; font-size: 0.875rem; margin-top: 0.5rem; }}
        .file-list {{ margin-top: 1rem; }}
        .file-item {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 0.75rem; background: #f8fafc; border-radius: 0.5rem; margin-bottom: 0.5rem;
        }}
        .file-item-name {{ font-weight: 500; font-size: 0.875rem; }}
        .file-item-mp {{
            font-size: 0.75rem; padding: 0.125rem 0.5rem;
            background: #dbeafe; color: #1e40af; border-radius: 0.25rem;
        }}
        .form-group {{ margin-bottom: 1.25rem; }}
        .form-label {{ display: block; font-size: 0.875rem; font-weight: 500; margin-bottom: 0.375rem; }}
        .form-select {{
            width: 100%; padding: 0.75rem 1rem; border: 1px solid #d1d5db;
            border-radius: 0.5rem; font-size: 1rem; outline: none;
        }}
        .form-select:focus {{ border-color: #3b82f6; }}
        .btn-process {{
            width: 100%; padding: 0.875rem; background: #3b82f6; color: white;
            border: none; border-radius: 0.5rem; font-size: 1rem; font-weight: 600;
            cursor: pointer; transition: background 0.2s;
        }}
        .btn-process:hover {{ background: #2563eb; }}
        .btn-process:disabled {{ background: #94a3b8; cursor: not-allowed; }}
        .status {{ margin-top: 1rem; padding: 1rem; border-radius: 0.5rem; display: none; }}
        .status-success {{ background: #dcfce7; color: #166534; }}
        .status-error {{ background: #fef2f2; color: #dc2626; }}
        .status-info {{ background: #dbeafe; color: #1e40af; }}
        .errors-list {{ margin-top: 0.5rem; font-size: 0.875rem; }}
    </style>
</head>
<body>
    <nav class="navbar">
        <a href="/dashboard" class="navbar-brand">Marketplace Sync</a>
        <div class="navbar-nav">
            <a href="/dashboard">Dashboard</a>
            <a href="/schemas">Схемы</a>
            <a href="/upload">Загрузка</a>
            <a href="/tasks">Задачи</a>
        </div>
    </nav>

    <div class="container">
        <h1 class="page-title">Загрузка файлов</h1>

        <div class="card">
            <h2 class="card-title">1. Загрузите файлы маркетплейсов</h2>
            <div class="drop-zone" id="dropZone">
                <div class="drop-zone-text">Перетащите файлы сюда или нажмите для выбора</div>
                <div class="drop-zone-hint">.xlsx (WB, Ozon, Яндекс) и .xml (каталог МВМ)</div>
                <input type="file" id="fileInput" multiple accept=".xlsx,.xml" style="display:none">
            </div>
            <div class="file-list" id="fileList"></div>
            <div class="status" id="uploadStatus"></div>
        </div>

        <div class="card">
            <h2 class="card-title">2. Выберите схему и запустите обработку</h2>
            <div class="form-group">
                <label class="form-label" for="schemaSelect">Схема сопоставлений</label>
                <select class="form-select" id="schemaSelect">
                    <option value="">— Выберите схему —</option>
                    {schema_options}
                </select>
            </div>
            <button class="btn-process" id="processBtn" disabled>🚀 Обработать</button>
            <div class="status" id="processStatus"></div>
        </div>
    </div>

    <script>
    const csrfToken = '{csrf_token}';
    let uploadedFiles = {{}};
    let xmlFilePath = null;

    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const uploadStatus = document.getElementById('uploadStatus');
    const processBtn = document.getElementById('processBtn');
    const processStatus = document.getElementById('processStatus');
    const schemaSelect = document.getElementById('schemaSelect');

    // Drag & drop
    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => {{ e.preventDefault(); dropZone.classList.add('drag-over'); }});
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', (e) => {{
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        handleFiles(e.dataTransfer.files);
    }});
    fileInput.addEventListener('change', (e) => handleFiles(e.target.files));

    async function handleFiles(files) {{
        const formData = new FormData();
        for (const file of files) {{
            formData.append('files[]', file);
        }}

        uploadStatus.style.display = 'block';
        uploadStatus.className = 'status status-info';
        uploadStatus.textContent = 'Загрузка файлов...';

        try {{
            const resp = await fetch('/upload/files', {{
                method: 'POST',
                headers: {{ 'X-CSRF-Token': csrfToken }},
                body: formData,
            }});
            const data = await resp.json();

            if (data.error) {{
                uploadStatus.className = 'status status-error';
                uploadStatus.textContent = data.error;
                return;
            }}

            uploadedFiles = data.files || {{}};
            xmlFilePath = data.xml || null;

            // Отображаем загруженные файлы
            let html = '';
            for (const [mp, path] of Object.entries(uploadedFiles)) {{
                const mpLabel = {{wildberries: 'WB', ozon: 'Ozon', yandex: 'Яндекс'}}[mp] || mp;
                html += `<div class="file-item"><span class="file-item-name">${{path.split('/').pop()}}</span><span class="file-item-mp">${{mpLabel}}</span></div>`;
            }}
            if (xmlFilePath) {{
                html += `<div class="file-item"><span class="file-item-name">${{xmlFilePath.split('/').pop()}}</span><span class="file-item-mp">XML</span></div>`;
            }}
            fileList.innerHTML = html;

            // Ошибки
            if (data.errors && data.errors.length > 0) {{
                uploadStatus.className = 'status status-error';
                uploadStatus.innerHTML = 'Предупреждения:<div class="errors-list">' + data.errors.map(e => '• ' + e).join('<br>') + '</div>';
            }} else {{
                uploadStatus.className = 'status status-success';
                uploadStatus.textContent = `Загружено: ${{Object.keys(uploadedFiles).length}} МП-файлов` + (xmlFilePath ? ' + XML' : '');
            }}

            updateProcessBtn();
        }} catch (e) {{
            uploadStatus.className = 'status status-error';
            uploadStatus.textContent = 'Ошибка сети: ' + e.message;
        }}
    }}

    schemaSelect.addEventListener('change', updateProcessBtn);

    function updateProcessBtn() {{
        const hasFiles = Object.keys(uploadedFiles).length >= 2;
        const hasSchema = schemaSelect.value !== '';
        processBtn.disabled = !(hasFiles && hasSchema);
    }}

    processBtn.addEventListener('click', async () => {{
        const schemaId = parseInt(schemaSelect.value);
        if (!schemaId) return;

        processBtn.disabled = true;
        processBtn.textContent = '⏳ Запуск...';
        processStatus.style.display = 'block';
        processStatus.className = 'status status-info';
        processStatus.textContent = 'Создание задачи...';

        try {{
            const resp = await fetch('/upload/process', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken,
                }},
                body: JSON.stringify({{
                    schema_id: schemaId,
                    files: uploadedFiles,
                    xml: xmlFilePath,
                }}),
            }});
            const data = await resp.json();

            if (data.error) {{
                processStatus.className = 'status status-error';
                processStatus.textContent = data.error;
                processBtn.disabled = false;
                processBtn.textContent = '🚀 Обработать';
                return;
            }}

            processStatus.className = 'status status-success';
            processStatus.innerHTML = `✅ Задача создана! <a href="/tasks">Перейти к задачам →</a>`;

            // Подключаем WebSocket для прогресса
            const taskId = data.task_id;
            const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const ws = new WebSocket(`${{wsProtocol}}//${{location.host}}/ws/tasks/${{taskId}}`);

            ws.onmessage = (event) => {{
                const msg = JSON.parse(event.data);
                if (msg.type === 'progress') {{
                    processStatus.className = 'status status-info';
                    processStatus.textContent = msg.message;
                }} else if (msg.type === 'completed') {{
                    processStatus.className = 'status status-success';
                    processStatus.innerHTML = msg.message + ` <a href="/tasks">Скачать результаты →</a>`;
                }} else if (msg.type === 'error') {{
                    processStatus.className = 'status status-error';
                    processStatus.textContent = '❌ ' + msg.message;
                }}
            }};

        }} catch (e) {{
            processStatus.className = 'status status-error';
            processStatus.textContent = 'Ошибка: ' + e.message;
            processBtn.disabled = false;
            processBtn.textContent = '🚀 Обработать';
        }}
    }});
    </script>
</body>
</html>"""
