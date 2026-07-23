"""
Маршруты для управления схемами сопоставлений.

Эндпоинты:
    GET    /schemas         — список схем (свои или все, зависит от роли)
    GET    /schemas/{id}    — детали схемы (группы сопоставлений)
    DELETE /api/schemas/{id} — удаление схемы (AJAX, JSON-ответ)

Создание и редактирование схем (AI wizard) будет реализовано в Фазе 4
через отдельные шаблоны с пошаговым интерфейсом.

Доступ:
    - Список: @login_required (user видит свои, editor+ — все)
    - Детали: @login_required (user — свои, editor+ — все)
    - Удаление: @login_required (user — свои, admin+ — любые)

Паттерн: Controller — принимает HTTP, делегирует БД, возвращает ответ.
"""

import json

from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from web.auth.decorators import login_required
from web.auth.permissions import WebAccessManager
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.schemas")


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

    html = _render_schemas_list(
        schemas=schemas,
        can_see_all=can_see_all,
        user_data=user_data,
        csrf_token=csrf_token,
    )
    return Response(text=html, content_type="text/html")


@login_required
async def schema_detail(request: Request) -> Response:
    """
    GET /schemas/{id} — детали схемы (группы сопоставлений).

    Отображает все группы сопоставлений схемы:
        - Тройные / четверные
        - Парные (все комбинации)
        - Уникальные столбцы

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

    csrf_token = get_csrf_token(request)

    html = _render_schema_detail(
        schema_meta=schema_meta,
        matches=matches,
        user_data=user_data,
        csrf_token=csrf_token,
    )
    return Response(text=html, content_type="text/html")


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
    # Нужно получить schema_name для метода delete_schema
    schema_name = schema_meta.get("name", "")
    owner_id = schema_meta.get("owner_id", 0)

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


# ===================================================================
# Временные HTML-шаблоны (будут заменены на Jinja2 в Фазе 4)
# ===================================================================


def _render_schemas_list(
    schemas: list,
    can_see_all: bool,
    user_data: dict,
    csrf_token: str,
) -> str:
    """Генерирует HTML списка схем."""

    rows_html = ""
    if schemas:
        for schema in schemas:
            schema_type_icon = "📦" if schema.get("schema_type") == "mvm" else "📋"
            schema_type_label = "МВМ" if schema.get("schema_type") == "mvm" else "Стандартная"
            owner_info = ""
            if can_see_all and schema.get("owner_name"):
                owner_info = f'<span class="owner-tag">{schema["owner_name"]}</span>'

            updated = schema.get("updated_at", "")[:16] if schema.get("updated_at") else "—"

            rows_html += f"""
            <tr>
                <td>
                    <a href="/schemas/{schema['id']}" class="schema-link">
                        {schema_type_icon} {schema['name']}
                    </a>
                    {owner_info}
                </td>
                <td>{schema_type_label}</td>
                <td>{updated}</td>
                <td>
                    <button class="btn-delete" onclick="deleteSchema({schema['id']}, '{csrf_token}')">
                        🗑️
                    </button>
                </td>
            </tr>
            """
    else:
        rows_html = """
        <tr>
            <td colspan="4" class="text-center text-muted">
                Схемы не найдены. Создайте первую схему через Telegram-бота.
            </td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Схемы — Marketplace Sync</title>
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
        .container {{ max-width: 1000px; margin: 0 auto; padding: 2rem; }}
        .page-header {{
            display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;
        }}
        .page-header h1 {{ font-size: 1.5rem; font-weight: 700; }}
        .table-container {{
            background: white; border-radius: 0.75rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden;
        }}
        .table {{ width: 100%; border-collapse: collapse; }}
        .table th, .table td {{
            text-align: left; padding: 0.875rem 1rem;
            border-bottom: 1px solid #e2e8f0; font-size: 0.875rem;
        }}
        .table th {{ font-weight: 600; color: #64748b; background: #f8fafc; }}
        .table tr:hover {{ background: #f8fafc; }}
        .schema-link {{ color: #3b82f6; text-decoration: none; font-weight: 500; }}
        .schema-link:hover {{ text-decoration: underline; }}
        .owner-tag {{
            display: inline-block; margin-left: 0.5rem; padding: 0.125rem 0.5rem;
            background: #f1f5f9; border-radius: 0.25rem;
            font-size: 0.75rem; color: #64748b;
        }}
        .btn-delete {{
            background: none; border: none; cursor: pointer;
            font-size: 1rem; padding: 0.25rem 0.5rem; border-radius: 0.25rem;
            transition: background 0.2s;
        }}
        .btn-delete:hover {{ background: #fef2f2; }}
        .text-center {{ text-align: center; }}
        .text-muted {{ color: #64748b; padding: 2rem !important; }}
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
        <div class="page-header">
            <h1>Схемы сопоставлений</h1>
        </div>

        <div class="table-container">
            <table class="table">
                <thead>
                    <tr>
                        <th>Название</th>
                        <th>Тип</th>
                        <th>Обновлена</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <script>
    async function deleteSchema(schemaId, csrfToken) {{
        if (!confirm('Удалить схему? Это действие нельзя отменить.')) return;

        try {{
            const response = await fetch('/api/schemas/' + schemaId, {{
                method: 'DELETE',
                headers: {{
                    'X-CSRF-Token': csrfToken,
                    'Accept': 'application/json',
                }},
            }});
            const data = await response.json();

            if (data.success) {{
                location.reload();
            }} else {{
                alert('Ошибка: ' + (data.error || 'Неизвестная ошибка'));
            }}
        }} catch (e) {{
            alert('Ошибка сети: ' + e.message);
        }}
    }}
    </script>
</body>
</html>"""


def _render_schema_detail(
    schema_meta: dict,
    matches: dict,
    user_data: dict,
    csrf_token: str,
) -> str:
    """Генерирует HTML деталей схемы."""

    schema_type = schema_meta.get("schema_type", "standard")
    type_icon = "📦" if schema_type == "mvm" else "📋"
    type_label = "МВМ (3 МП + XML)" if schema_type == "mvm" else "Стандартная (3 МП)"

    # Подсчёт сопоставлений по группам
    groups_html = ""

    # Определяем группы в зависимости от типа
    if schema_type == "mvm":
        group_defs = [
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
    else:
        group_defs = [
            ("matches_all_three", "Тройные (WB + Ozon + Яндекс)"),
            ("matches_1_2", "Парные (WB + Ozon)"),
            ("matches_1_3", "Парные (WB + Яндекс)"),
            ("matches_2_3", "Парные (Ozon + Яндекс)"),
        ]

    for group_key, group_label in group_defs:
        items = matches.get(group_key, [])
        if not items:
            continue

        items_html = ""
        for item in items:
            cols = []
            for key in ("column_1", "column_2", "column_3", "column_4"):
                val = item.get(key)
                if val:
                    cols.append(val)
            cols_text = " ↔ ".join(cols)
            confidence = item.get("confidence", 0)
            conf_pct = f"{confidence * 100:.0f}%" if isinstance(confidence, float) else str(confidence)
            items_html += f"<li>{cols_text} <span class='conf'>({conf_pct})</span></li>"

        groups_html += f"""
        <div class="group-section">
            <h3 class="group-title">{group_label} <span class="group-count">({len(items)})</span></h3>
            <ul class="matches-list">{items_html}</ul>
        </div>
        """

    if not groups_html:
        groups_html = '<p class="text-muted">Нет сопоставлений</p>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{schema_meta['name']} — Marketplace Sync</title>
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
        .container {{ max-width: 900px; margin: 0 auto; padding: 2rem; }}
        .breadcrumb {{ font-size: 0.875rem; color: #64748b; margin-bottom: 1rem; }}
        .breadcrumb a {{ color: #3b82f6; text-decoration: none; }}
        .schema-header {{
            background: white; border-radius: 0.75rem; padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1.5rem;
        }}
        .schema-header h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 0.5rem; }}
        .schema-meta {{ font-size: 0.875rem; color: #64748b; display: flex; gap: 1.5rem; flex-wrap: wrap; }}
        .group-section {{
            background: white; border-radius: 0.75rem; padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1rem;
        }}
        .group-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 0.75rem; }}
        .group-count {{ font-weight: 400; color: #64748b; }}
        .matches-list {{ list-style: none; padding: 0; }}
        .matches-list li {{
            padding: 0.5rem 0; border-bottom: 1px solid #f1f5f9;
            font-size: 0.875rem;
        }}
        .matches-list li:last-child {{ border-bottom: none; }}
        .conf {{ color: #64748b; font-size: 0.75rem; }}
        .text-muted {{ color: #64748b; }}
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
        <div class="breadcrumb">
            <a href="/schemas">Схемы</a> / {schema_meta['name']}
        </div>

        <div class="schema-header">
            <h1>{type_icon} {schema_meta['name']}</h1>
            <div class="schema-meta">
                <span>Тип: {type_label}</span>
                <span>Владелец: {schema_meta['owner_name']}</span>
                <span>Обновлена: {schema_meta['updated_at']}</span>
            </div>
        </div>

        {groups_html}
    </div>
</body>
</html>"""
