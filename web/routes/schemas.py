"""
Маршруты для управления схемами сопоставлений.

Эндпоинты:
    GET    /schemas            — список схем (свои или все, зависит от роли)
    GET    /schemas/create     — страница создания стандартной схемы
    POST   /schemas/create     — AI-сопоставление + сохранение схемы
    GET    /schemas/{id}       — детали схемы (группы сопоставлений)
    DELETE /api/schemas/{id}   — удаление схемы (AJAX, JSON-ответ)

Доступ:
    - Список: @login_required (user видит свои, editor+ — все)
    - Создание: @login_required (любой авторизованный)
    - Детали: @login_required (user — свои, editor+ — все)
    - Удаление: @login_required (user — свои, admin+ — любые)

Паттерн: Controller — принимает HTTP, делегирует БД, возвращает ответ.
"""

import uuid
from pathlib import Path

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from config.config import Config, FILE_CONFIGS
from utils.excel_reader import ExcelReader
from web.auth.decorators import login_required
from web.auth.permissions import WebAccessManager
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.schemas")

# Определение групп сопоставлений для каждого типа схемы
_STANDARD_GROUPS = [
    ("matches_all_three", "Тройные (WB + Ozon + Яндекс)"),
    ("matches_1_2", "Парные (WB + Ozon)"),
    ("matches_1_3", "Парные (WB + Яндекс)"),
    ("matches_2_3", "Парные (Ozon + Яндекс)"),
]

_MVM_GROUPS = [
    ("matches_all_four", "Четверные (WB + Ozon + Яндекс + XML)"),
    ("matches_triple_1_2_3", "Тройные (WB + Ozon + Яндекс)"),
    ("matches_triple_1_2_4", "Тройные (WB + Ozon + XML)"),
    ("matches_triple_1_3_4", "Тройные (WB + Яндекс + XML)"),
    ("matches_triple_2_3_4", "Тройные (Ozon + Яндекс + XML)"),
    ("matches_pair_1_2", "Парные (WB + Ozon)"),
    ("matches_pair_1_3", "Парные (WB + Яндекс)"),
    ("matches_pair_1_4", "Парные (WB + XML)"),
    ("matches_pair_2_3", "Парные (Ozon + Яндекс)"),
    ("matches_pair_2_4", "Парные (Ozon + XML)"),
    ("matches_pair_3_4", "Парные (Яндекс + XML)"),
]

# Маппинг column_key → маркетплейс (для цветовой маркировки в шаблоне)
_COLUMN_MP_MAP = {
    "column_1": "wb",
    "column_2": "ozon",
    "column_3": "yandex",
    "column_4": "xml",
}

# Допустимые расширения файлов
_ALLOWED_EXTENSIONS: set = {".xlsx"}

# Максимальный размер файла (250 МБ)
_MAX_FILE_SIZE: int = 250 * 1024 * 1024

# Ключевые слова для определения маркетплейса по имени файла
_MP_KEYWORDS: dict = {
    "wildberries": ["wb", "wildberries", "вайлдберриз", "вб"],
    "ozon": ["ozon", "озон"],
    "yandex": ["yandex", "яндекс", "ymarket", "маркет"],
}


@login_required
async def schemas_list(request: Request) -> Response:
    """
    GET /schemas — список схем сопоставлений.

    Owner/admin/editor видят все схемы (с указанием владельца).
    User видит только свои.

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница со списком схем
    """
    user_data = request["user"]
    can_see_all = WebAccessManager.can_see_all_schemas(user_data)
    telegram_user_id = user_data.get("telegram_user_id")

    if can_see_all:
        schemas = await storage.db.get_user_schemas(0, all_schemas=True)
    elif telegram_user_id:
        schemas = await storage.db.get_user_schemas(telegram_user_id, all_schemas=False)
    else:
        schemas = []

    csrf_token = get_csrf_token(request)

    context = {
        "schemas": schemas,
        "can_see_all": can_see_all,
        "csrf_token": csrf_token,
        "user": user_data,
    }

    return aiohttp_jinja2.render_template("schemas/list.html", request, context)


@login_required
async def schema_create_page(request: Request) -> Response:
    """
    GET /schemas/create — страница создания стандартной схемы.

    Отображает форму с:
        - Полем ввода названия схемы
        - Зоной drag&drop для загрузки 3 файлов МП
        - Кнопкой запуска AI-сопоставления

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница создания схемы
    """
    user_data = request["user"]
    csrf_token = get_csrf_token(request)

    context = {
        "csrf_token": csrf_token,
        "user": user_data,
        "error_message": None,
    }

    return aiohttp_jinja2.render_template("schemas/create.html", request, context)


@login_required
async def schema_create_handler(request: Request) -> Response:
    """
    POST /schemas/create — создание стандартной схемы.

    Поток:
        1. Читает multipart-данные (название + 3 файла)
        2. Валидирует файлы (расширение, маркетплейс, количество)
        3. Извлекает столбцы из каждого файла через ExcelReader
        4. Вызывает AI-сопоставление (AIComparator.compare_columns)
        5. Фильтрует по confidence >= 85%
        6. Сохраняет схему и сопоставления в БД
        7. Редирект на /schemas/{id}

    Args:
        request: HTTP-запрос с multipart-данными

    Returns:
        Редирект на страницу схемы или HTML с ошибкой
    """
    user_data = request["user"]
    telegram_user_id = user_data.get("telegram_user_id")

    # Для создания схемы нужен telegram_user_id (схемы привязаны к Telegram ID)
    if not telegram_user_id:
        return _render_create_error(
            request, user_data,
            "Для создания схемы необходимо привязать Telegram-аккаунт в профиле.",
        )

    # Парсим multipart-данные
    schema_name = ""
    saved_files: dict = {}
    upload_dir: Path | None = None

    try:
        reader = await request.multipart()

        async for part in reader:
            # Текстовое поле — название схемы или CSRF
            if part.filename is None:
                field_name = part.name
                value = (await part.read()).decode("utf-8", errors="ignore").strip()

                if field_name == "schema_name":
                    schema_name = value
                # csrf_token проверяется middleware, пропускаем
                continue

            # Файловое поле
            if part.name != "files[]" and part.name != "files":
                await part.read()
                continue

            filename = part.filename
            if not filename:
                continue

            # Проверка расширения
            ext = Path(filename).suffix.lower()
            if ext not in _ALLOWED_EXTENSIONS:
                return _render_create_error(
                    request, user_data,
                    f"Недопустимый формат файла: {filename}. Разрешены только .xlsx",
                )

            # Чтение файла
            file_data = bytearray()
            while True:
                chunk = await part.read_chunk(8192)
                if not chunk:
                    break
                file_data.extend(chunk)
                if len(file_data) > _MAX_FILE_SIZE:
                    return _render_create_error(
                        request, user_data,
                        f"Файл слишком большой: {filename} (макс. 250 МБ)",
                    )

            # Определяем маркетплейс
            marketplace = _detect_marketplace(filename)
            if not marketplace:
                return _render_create_error(
                    request, user_data,
                    f"Не удалось определить маркетплейс для файла: {filename}. "
                    f"Имя должно содержать wb/ozon/yandex.",
                )

            if marketplace in saved_files:
                return _render_create_error(
                    request, user_data,
                    f"Дублирование: два файла определены как {marketplace}.",
                )

            # Сохраняем файл на диск
            if upload_dir is None:
                web_user_id = user_data.get("web_user_id", 0)
                upload_id = str(uuid.uuid4())[:8]
                upload_dir = Path(Config.UPLOAD_DIR) / f"web_schema_{web_user_id}_{upload_id}"
                upload_dir.mkdir(parents=True, exist_ok=True)

            safe_filename = f"{marketplace}_{filename}"
            file_path = upload_dir / safe_filename

            with open(file_path, "wb") as f:
                f.write(file_data)

            saved_files[marketplace] = str(file_path)

    except Exception as e:
        logger.error("Ошибка чтения multipart при создании схемы: %s", e, exc_info=True)
        return _render_create_error(
            request, user_data,
            f"Ошибка загрузки файлов: {e}",
        )

    # === Валидация ===

    if not schema_name:
        return _render_create_error(
            request, user_data,
            "Не указано название схемы.",
        )

    if len(schema_name) > 100:
        return _render_create_error(
            request, user_data,
            "Название схемы слишком длинное (максимум 100 символов).",
        )

    if len(saved_files) < 2:
        return _render_create_error(
            request, user_data,
            f"Загружено файлов: {len(saved_files)}. Необходимо минимум 2 файла маркетплейсов.",
        )

    # Проверка уникальности названия
    can_see_all = WebAccessManager.can_see_all_schemas(user_data)

    if can_see_all:
        existing = await storage.db.get_schema_by_name_global(schema_name)
        if existing:
            return _render_create_error(
                request, user_data,
                f"Схема с названием '{schema_name}' уже существует.",
            )
    else:
        existing = await storage.db.get_schema(telegram_user_id, schema_name)
        if existing:
            return _render_create_error(
                request, user_data,
                f"Схема с названием '{schema_name}' уже существует.",
            )

    # === Чтение столбцов из файлов ===

    try:
        excel_reader = ExcelReader()
        columns: dict = {}

        for marketplace, file_path in saved_files.items():
            config = FILE_CONFIGS[marketplace]
            columns[marketplace] = excel_reader.get_column_names(
                file_path, config["sheet_name"], config["header_row"],
            )

        logger.info(
            "Столбцы прочитаны для создания схемы '%s': WB=%d, Ozon=%d, Яндекс=%d",
            schema_name,
            len(columns.get("wildberries", [])),
            len(columns.get("ozon", [])),
            len(columns.get("yandex", [])),
        )

    except Exception as e:
        logger.error("Ошибка чтения столбцов: %s", e, exc_info=True)
        return _render_create_error(
            request, user_data,
            f"Ошибка чтения файлов Excel: {e}",
        )

    # === AI-сопоставление ===

    try:
        ai_comparator = request.app["ai_comparator"]

        comparison_result = await ai_comparator.compare_columns(
            columns.get("wildberries", []),
            columns.get("ozon", []),
            columns.get("yandex", []),
        )

        # Фильтруем по confidence >= 85%
        all_matches = comparison_result.get("matches_all_three", [])
        filtered = [m for m in all_matches if m.get("confidence", 0) >= 0.85]
        comparison_result["matches_all_three"] = filtered

        total_matches = len(filtered)
        for pair_key in ("matches_1_2", "matches_1_3", "matches_2_3"):
            total_matches += len(comparison_result.get(pair_key, []))

        logger.info(
            "AI-сопоставление для схемы '%s' завершено: %d совпадений",
            schema_name, total_matches,
        )

    except Exception as e:
        logger.error("Ошибка AI-сопоставления: %s", e, exc_info=True)
        return _render_create_error(
            request, user_data,
            f"Ошибка AI-сопоставления: {e}",
        )

    # === Сохранение в БД ===

    try:
        schema_id = await storage.db.create_schema(
            telegram_user_id, schema_name, schema_type="standard",
        )

        if not schema_id:
            return _render_create_error(
                request, user_data,
                f"Схема с названием '{schema_name}' уже существует.",
            )

        await storage.db.save_schema_matches(schema_id, comparison_result)

        logger.info(
            "Схема '%s' (id=%d) создана через веб, пользователь web_user_id=%d",
            schema_name, schema_id, user_data.get("web_user_id", 0),
        )

    except Exception as e:
        logger.error("Ошибка сохранения схемы: %s", e, exc_info=True)
        return _render_create_error(
            request, user_data,
            f"Ошибка сохранения: {e}",
        )

    # Успех — редирект на страницу созданной схемы
    raise web.HTTPFound(f"/schemas/{schema_id}")


@login_required
async def schema_detail(request: Request) -> Response:
    """
    GET /schemas/{id} — детали схемы (группы сопоставлений).

    Отображает все группы сопоставлений схемы:
        - Тройные / четверные
        - Парные (все комбинации)

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница с деталями схемы
    """
    user_data = request["user"]
    schema_id = int(request.match_info["id"])

    # Загружаем данные схемы
    matches = await storage.db.get_schema_matches(schema_id)
    if not matches:
        raise web.HTTPNotFound(reason="Схема не найдена")

    # Получаем метаданные схемы
    schema_meta = await _get_schema_meta(schema_id)
    if not schema_meta:
        raise web.HTTPNotFound(reason="Схема не найдена")

    # Проверка доступа: user может видеть только свои
    can_see_all = WebAccessManager.can_see_all_schemas(user_data)
    telegram_user_id = user_data.get("telegram_user_id")

    if not can_see_all:
        if not telegram_user_id or schema_meta.get("owner_id") != telegram_user_id:
            raise web.HTTPForbidden(reason="Нет доступа к этой схеме")

    # Подготовка групп для шаблона
    groups = _prepare_groups_for_template(matches, schema_meta.get("schema_type", "standard"))

    csrf_token = get_csrf_token(request)

    context = {
        "schema_meta": schema_meta,
        "groups": groups,
        "csrf_token": csrf_token,
        "user": user_data,
    }

    return aiohttp_jinja2.render_template("schemas/detail.html", request, context)


@login_required
async def schema_delete_api(request: Request) -> Response:
    """
    DELETE /api/schemas/{id} — удаление схемы (AJAX).

    Проверяет права:
        - Owner/admin могут удалять любые
        - Editor/user — только свои (по telegram_user_id)

    Args:
        request: HTTP-запрос

    Returns:
        JSON: {"success": true} или {"error": "..."}
    """
    user_data = request["user"]
    schema_id = int(request.match_info["id"])

    # Загружаем метаданные схемы
    schema_meta = await _get_schema_meta(schema_id)
    if not schema_meta:
        return web.json_response(
            {"error": "Схема не найдена"}, status=404,
        )

    # Проверка прав на удаление
    schema_owner_id = schema_meta.get("owner_id", 0)
    can_delete = WebAccessManager.can_delete_schema(user_data, schema_owner_id)

    if not can_delete:
        return web.json_response(
            {"error": "Недостаточно прав для удаления этой схемы"}, status=403,
        )

    # Удаляем схему (CASCADE удалит schema_matches)
    schema_name = schema_meta.get("name", "")

    async with storage.db.pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM schemas WHERE id = $1",
            schema_id,
        )

    if result == "DELETE 1":
        logger.info(
            "Схема id=%d (%s) удалена пользователем web_user_id=%d",
            schema_id, schema_name, user_data.get("web_user_id", 0),
        )
        return web.json_response({"success": True})
    else:
        return web.json_response(
            {"error": "Не удалось удалить схему"}, status=500,
        )


# =================================================================
# Вспомогательные функции
# =================================================================

def _render_create_error(
    request: Request,
    user_data: dict,
    error_message: str,
) -> Response:
    """
    Рендерит страницу создания схемы с сообщением об ошибке.

    Args:
        request: HTTP-запрос
        user_data: Данные пользователя из сессии
        error_message: Текст ошибки для отображения

    Returns:
        HTML-страница с ошибкой
    """
    csrf_token = get_csrf_token(request)
    context = {
        "csrf_token": csrf_token,
        "user": user_data,
        "error_message": error_message,
    }
    return aiohttp_jinja2.render_template(
        "schemas/create.html", request, context, status=400,
    )


def _detect_marketplace(filename: str) -> str | None:
    """
    Определяет маркетплейс по имени файла.

    Args:
        filename: Имя загруженного файла

    Returns:
        Название маркетплейса или None
    """
    name_lower = filename.lower()

    for marketplace, keywords in _MP_KEYWORDS.items():
        for keyword in keywords:
            if keyword in name_lower:
                return marketplace

    return None


def _prepare_groups_for_template(matches: dict, schema_type: str) -> list:
    """
    Подготавливает группы сопоставлений для отображения в шаблоне.

    ВАЖНО: Ключ для списка сопоставлений — "matches", НЕ "items".
    В Jinja2 обращение dict.items интерпретируется как вызов
    встроенного метода dict.items(), что вызывает TypeError.

    Args:
        matches: Сырые данные сопоставлений из БД
        schema_type: Тип схемы ('standard' или 'mvm')

    Returns:
        Список групп для рендеринга в шаблоне
    """
    group_defs = _MVM_GROUPS if schema_type == "mvm" else _STANDARD_GROUPS
    groups = []

    for group_key, group_label in group_defs:
        items_raw = matches.get(group_key, [])
        if not items_raw:
            continue

        match_items = []
        for item in items_raw:
            columns = []
            for col_key in ("column_1", "column_2", "column_3", "column_4"):
                col_value = item.get(col_key)
                if col_value:
                    mp = _COLUMN_MP_MAP.get(col_key, "unknown")
                    columns.append({"mp": mp, "name": col_value})

            confidence = item.get("confidence")
            if isinstance(confidence, (int, float)):
                confidence = float(confidence)
            else:
                confidence = None

            match_items.append({
                "columns": columns,
                "confidence": confidence,
            })

        groups.append({
            "key": group_key,
            "label": group_label,
            "matches": match_items,
        })

    return groups


async def _get_schema_meta(schema_id: int) -> dict | None:
    """
    Загружает метаданные схемы по ID.

    Args:
        schema_id: ID схемы

    Returns:
        Словарь с метаданными или None
    """
    async with storage.db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.id, s.user_id, s.schema_name, s.schema_type,
                   s.created_at, s.updated_at,
                   u.username, u.first_name
            FROM schemas s
            LEFT JOIN users u ON s.user_id = u.user_id
            WHERE s.id = $1
            """,
            schema_id,
        )

    if not row:
        return None

    owner_display = row["first_name"] if row["first_name"] else f"ID: {row['user_id']}"

    return {
        "id": row["id"],
        "owner_id": row["user_id"],
        "owner_name": owner_display,
        "name": row["schema_name"],
        "schema_type": row["schema_type"] or "standard",
        "created_at": str(row["created_at"])[:16] if row["created_at"] else "—",
        "updated_at": str(row["updated_at"])[:16] if row["updated_at"] else "—",
    }


def setup_schemas_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты схем.

    ВАЖНО: /schemas/create регистрируется ПЕРЕД /schemas/{id},
    иначе aiohttp интерпретирует "create" как {id} и вернёт ошибку.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/schemas", schemas_list)
    app.router.add_get("/schemas/create", schema_create_page)
    app.router.add_post("/schemas/create", schema_create_handler)
    app.router.add_get("/schemas/{id:\\d+}", schema_detail)
    app.router.add_delete("/api/schemas/{id:\\d+}", schema_delete_api)
