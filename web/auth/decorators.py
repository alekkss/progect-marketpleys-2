"""
Декораторы защиты маршрутов веб-приложения.

Используются для декларативного указания требований к доступу
прямо на уровне обработчика маршрута:

    @login_required
    async def dashboard(request):
        user = request["user"]  # гарантированно заполнен
        ...

    @admin_required
    async def manage_users(request):
        ...  # только owner и admin

Декораторы проверяют request["user"], который заполняется
auth middleware ДО вызова обработчика. Если middleware
не нашёл валидную сессию — request["user"] = None.

Паттерн: Decorator — добавляет поведение (проверку прав)
к обработчику без изменения его кода.
Принцип Open/Closed — новый уровень доступа = новый декоратор.

Логика ответа при отказе:
    - JSON-запрос (AJAX) → 401 Unauthorized или 403 Forbidden
    - HTML-запрос (браузер) → 302 редирект на /auth/login
"""

import functools
from typing import Callable, Awaitable

from aiohttp.web import Request, StreamResponse, HTTPFound, HTTPUnauthorized, HTTPForbidden

from web.auth.permissions import WebAccessManager
from utils.logger_config import setup_logger

logger = setup_logger("web.auth.decorators")

# Тип обработчика маршрута aiohttp
_Handler = Callable[[Request], Awaitable[StreamResponse]]


def _is_json_request(request: Request) -> bool:
    """
    Определяет, ожидает ли клиент JSON-ответ.

    Args:
        request: HTTP-запрос

    Returns:
        True если клиент ожидает JSON
    """
    accept = request.headers.get("Accept", "")
    if "application/json" in accept:
        return True

    if request.path.startswith("/api/"):
        return True

    if request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest":
        return True

    return False


def login_required(handler: _Handler) -> _Handler:
    """
    Требует авторизованного пользователя.

    Проверяет, что request["user"] содержит данные сессии.
    Если нет — редирект на страницу входа (или 401 для AJAX).

    Использование:
        @login_required
        async def my_handler(request):
            user = request["user"]  # гарантированно не None
            ...

    Args:
        handler: Оригинальный обработчик маршрута

    Returns:
        Обёрнутый обработчик с проверкой авторизации
    """

    @functools.wraps(handler)
    async def wrapper(request: Request) -> StreamResponse:
        user_data = request.get("user")

        if not user_data:
            if _is_json_request(request):
                raise HTTPUnauthorized(
                    reason="Требуется авторизация",
                )
            # Сохраняем URL для редиректа после логина
            login_url = f"/auth/login?next={request.path}"
            raise HTTPFound(login_url)

        return await handler(request)

    return wrapper


def admin_required(handler: _Handler) -> _Handler:
    """
    Требует роль owner или admin.

    Сначала проверяет авторизацию (как login_required),
    затем проверяет роль. При недостаточных правах — 403.

    Использование:
        @admin_required
        async def manage_users(request):
            ...  # только owner и admin

    Args:
        handler: Оригинальный обработчик маршрута

    Returns:
        Обёрнутый обработчик с проверкой роли
    """

    @functools.wraps(handler)
    async def wrapper(request: Request) -> StreamResponse:
        user_data = request.get("user")

        # Проверка авторизации
        if not user_data:
            if _is_json_request(request):
                raise HTTPUnauthorized(
                    reason="Требуется авторизация",
                )
            login_url = f"/auth/login?next={request.path}"
            raise HTTPFound(login_url)

        # Проверка роли
        if not WebAccessManager.is_admin_or_owner(user_data):
            logger.warning(
                "Попытка доступа к admin-маршруту: web_user_id=%d, role=%s, path=%s",
                user_data.get("web_user_id", 0),
                user_data.get("role", "unknown"),
                request.path,
            )
            if _is_json_request(request):
                raise HTTPForbidden(
                    reason="Недостаточно прав. Требуется роль администратора.",
                )
            raise HTTPForbidden(
                reason="Недостаточно прав. Требуется роль администратора.",
            )

        return await handler(request)

    return wrapper


def editor_required(handler: _Handler) -> _Handler:
    """
    Требует роль owner, admin или editor.

    Используется для маршрутов, доступных редакторам и выше
    (например, просмотр всех схем).

    Использование:
        @editor_required
        async def all_schemas(request):
            ...  # owner, admin, editor

    Args:
        handler: Оригинальный обработчик маршрута

    Returns:
        Обёрнутый обработчик с проверкой роли
    """

    @functools.wraps(handler)
    async def wrapper(request: Request) -> StreamResponse:
        user_data = request.get("user")

        # Проверка авторизации
        if not user_data:
            if _is_json_request(request):
                raise HTTPUnauthorized(
                    reason="Требуется авторизация",
                )
            login_url = f"/auth/login?next={request.path}"
            raise HTTPFound(login_url)

        # Проверка роли (минимум editor)
        if not WebAccessManager.has_minimum_role(user_data, "editor"):
            logger.warning(
                "Попытка доступа к editor-маршруту: web_user_id=%d, role=%s, path=%s",
                user_data.get("web_user_id", 0),
                user_data.get("role", "unknown"),
                request.path,
            )
            if _is_json_request(request):
                raise HTTPForbidden(
                    reason="Недостаточно прав. Требуется роль редактора.",
                )
            raise HTTPForbidden(
                reason="Недостаточно прав. Требуется роль редактора.",
            )

        return await handler(request)

    return wrapper
