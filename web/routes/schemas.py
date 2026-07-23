"""
Маршруты для управления схемами сопоставлений.

Эндпоинты:
    GET    /schemas         — список схем (свои или все, зависит от роли)
    GET    /schemas/{id}    — детали схемы (группы сопоставлений)
    DELETE /api/schemas/{id} — удаление схемы (AJAX, JSON-ответ)

Доступ:
    - Список: @login_required (user видит свои, editor+ — все)
    - Детали: @login_required (user — свои, editor+ — все)
    - Удаление: @login_required (user — свои, admin+ — любые)

Паттерн: Controller — принимает HTTP, делегирует БД, возвращает ответ.
"""

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
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


def _prepare_groups_for_template(matches: dict, schema_type: str) -> list:
    """
    Подготавливает группы сопоставлений для отображения в шаблоне.

    Преобразует сырые данные из БД в структуру:
        [
            {
                "key": "matches_all_three",
                "label": "Тройные (WB + Ozon + Яндекс)",
                "items": [
                    {
                        "columns": [{"mp": "wb", "name": "Артикул продавца"}, ...],
                        "confidence": 0.95,
                    },
                    ...
                ]
            },
            ...
        ]

    Пустые группы (без сопоставлений) не включаются.

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

        items = []
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

            items.append({
                "columns": columns,
                "confidence": confidence,
            })

        groups.append({
            "key": group_key,
            "label": group_label,
            "items": items,
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

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/schemas", schemas_list)
    app.router.add_get("/schemas/{id:\\d+}", schema_detail)
    app.router.add_delete("/api/schemas/{id:\\d+}", schema_delete_api)
