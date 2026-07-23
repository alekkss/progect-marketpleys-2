"""
Маршрут главной панели (dashboard).

GET /dashboard — отображает:
    - Приветствие с именем пользователя
    - Статистику: количество схем, обработок, последние задачи
    - Быстрые действия: загрузить файлы, создать схему, управление

Доступ: @login_required (любой авторизованный пользователь).

Паттерн: Controller — собирает данные из БД и передаёт в шаблон.
"""

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from web.auth.decorators import login_required
from web.auth.permissions import WebAccessManager
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.dashboard")


@login_required
async def dashboard_page(request: Request) -> Response:
    """
    GET /dashboard — главная панель пользователя.

    Собирает:
        - Данные пользователя из request["user"]
        - Количество схем (свои или все, зависит от роли)
        - Последние задачи (из task_results)
        - Статистику обработок (из processing_history через telegram_user_id)

    Args:
        request: HTTP-запрос (request["user"] заполнен auth middleware)

    Returns:
        HTML-страница dashboard
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]
    role = user_data.get("role", "user")
    display_name = user_data.get("display_name") or user_data.get("email", "")
    telegram_user_id = user_data.get("telegram_user_id")

    # Получаем количество схем
    schemas_count = await _get_schemas_count(user_data)

    # Получаем последние задачи
    recent_tasks = await storage.db.get_user_task_results(web_user_id, limit=5)

    # Статистика обработок (если привязан Telegram)
    processing_stats = None
    if telegram_user_id:
        processing_stats = await storage.db.get_user_stats(telegram_user_id)

    # Считаем задачи по статусам
    completed_tasks = sum(1 for t in recent_tasks if t["status"] == "completed")
    failed_tasks = sum(1 for t in recent_tasks if t["status"] == "failed")
    pending_tasks = sum(1 for t in recent_tasks if t["status"] in ("pending", "processing"))

    csrf_token = get_csrf_token(request)
    can_manage = WebAccessManager.can_manage_users(user_data)

    context = {
        "display_name": display_name,
        "role": role,
        "schemas_count": schemas_count,
        "completed_tasks": completed_tasks,
        "failed_tasks": failed_tasks,
        "pending_tasks": pending_tasks,
        "recent_tasks": recent_tasks,
        "processing_stats": processing_stats,
        "can_manage": can_manage,
        "csrf_token": csrf_token,
        "user": user_data,
    }

    return aiohttp_jinja2.render_template("dashboard.html", request, context)


async def _get_schemas_count(user_data: dict) -> int:
    """
    Получает количество схем, доступных пользователю.

    Owner/admin/editor видят все схемы, user — только свои.

    Args:
        user_data: Данные пользователя из request["user"]

    Returns:
        Количество доступных схем
    """
    can_see_all = WebAccessManager.can_see_all_schemas(user_data)
    telegram_user_id = user_data.get("telegram_user_id")

    if can_see_all:
        schemas = await storage.db.get_user_schemas(0, all_schemas=True)
    elif telegram_user_id:
        schemas = await storage.db.get_user_schemas(telegram_user_id, all_schemas=False)
    else:
        schemas = []

    return len(schemas)


def setup_dashboard_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты dashboard.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/dashboard", dashboard_page)
