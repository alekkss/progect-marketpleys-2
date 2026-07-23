"""
Регистрация маршрутов веб-приложения.

Единая точка входа — функция setup_routes(app), которая
подключает все группы маршрутов к aiohttp Application.

Маршруты разделены по файлам-модулям (по доменным областям):
    - auth.py       — вход, регистрация, выход (Фаза 2) ✓
    - dashboard.py  — главная панель (Фаза 3) ✓
    - schemas.py    — CRUD схем сопоставлений (Фаза 3) ✓
    - upload.py     — загрузка файлов (Фаза 3) ✓
    - tasks.py      — статусы задач, скачивание результатов (Фаза 3) ✓
    - admin.py      — управление пользователями (Фаза 3) ✓
    - categories.py — AJAX поиск категорий XML (Фаза 4)
    - websocket.py  — WebSocket прогресса (Фаза 1) ✓
    - api.py        — JSON API для AJAX (Фаза 4)

Паттерн: каждый модуль маршрутов экспортирует функцию
setup_*_routes(app), которая регистрирует свою группу.
Это обеспечивает Open/Closed — добавление нового модуля
маршрутов не требует изменения существующих.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

from aiohttp.web import Request, Response

from web.routes.auth import setup_auth_routes
from web.routes.dashboard import setup_dashboard_routes
from web.routes.schemas import setup_schemas_routes
from web.routes.upload import setup_upload_routes
from web.routes.tasks import setup_tasks_routes
from web.routes.admin import setup_admin_routes
from utils.logger_config import setup_logger

logger = setup_logger("web.routes")


async def health_check(request: Request) -> Response:
    """
    Эндпоинт проверки здоровья приложения.

    Используется для:
        - Мониторинга (systemd, Nginx upstream checks)
        - Проверки что aiohttp запущен и отвечает
        - Smoke-тест после деплоя

    Returns:
        JSON с состоянием сервиса
    """
    from aiohttp import web

    return web.json_response({
        "status": "ok",
        "service": "marketplace-sync-web",
    })


def setup_routes(app: "web.Application") -> None:
    """
    Регистрирует все маршруты в приложении.

    Вызывается из web/app.py после создания Application.
    Каждая группа маршрутов регистрируется через свою функцию setup_*_routes().

    Порядок регистрации:
        1. Служебные (health, root redirect)
        2. Аутентификация (/auth/*)
        3. Бизнес-маршруты (/dashboard, /schemas, /upload, /tasks)
        4. Администрирование (/admin/*)

    Args:
        app: Экземпляр aiohttp Application
    """
    # Служебные маршруты — всегда доступны, без авторизации
    app.router.add_get("/health", health_check)
    app.router.add_get("/", _root_redirect)

    # Аутентификация: /auth/login, /auth/register, /auth/logout
    setup_auth_routes(app)

    # Бизнес-маршруты (требуют авторизации через декораторы)
    setup_dashboard_routes(app)
    setup_schemas_routes(app)
    setup_upload_routes(app)
    setup_tasks_routes(app)

    # Администрирование (требует роли admin+)
    setup_admin_routes(app)

    logger.info("Маршруты веб-приложения зарегистрированы")


async def _root_redirect(request: Request) -> Response:
    """
    Редирект с корневого URL.

    Проверяет наличие авторизованного пользователя в request["user"]
    (заполняется auth middleware):
        - Авторизован → /dashboard
        - Не авторизован → /auth/login

    Args:
        request: Входящий HTTP-запрос

    Returns:
        HTTP 302 редирект
    """
    from aiohttp import web

    user = request.get("user")
    if user:
        raise web.HTTPFound("/dashboard")
    raise web.HTTPFound("/auth/login")


__all__ = ["setup_routes"]
