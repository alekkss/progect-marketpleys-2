"""
Middleware аутентификации веб-приложения.

Выполняется на каждый HTTP-запрос ПЕРЕД обработчиком маршрута.
Извлекает session_id из cookie и проверяет его валидность.

Результат работы:
    - request["user"] = Dict с данными пользователя (если сессия валидна)
    - request["user"] = None (если сессия отсутствует или невалидна)

Middleware НЕ блокирует запрос — это ответственность декораторов
(@login_required, @admin_required). Это позволяет иметь публичные
маршруты (health-check, login, register), которые работают без авторизации.

Пропускаемые пути (не требуют проверки сессии):
    - /health — мониторинг
    - /static/ — статические файлы
    - /auth/login — страница входа
    - /auth/register — страница регистрации

Паттерн: Middleware (Chain of Responsibility) — каждый запрос
проходит через цепочку обработки, middleware добавляет контекст.
"""

from typing import Callable, Awaitable, Set

from aiohttp.web import Request, StreamResponse, middleware

from web.auth.session import WebSessionManager
from utils.logger_config import setup_logger

logger = setup_logger("web.middleware.auth")

# Тип handler в aiohttp middleware
_Handler = Callable[[Request], Awaitable[StreamResponse]]

# Пути, для которых НЕ проверяется сессия.
# Эти маршруты должны работать без авторизации.
_PUBLIC_PATHS: Set[str] = {
    "/health",
    "/auth/login",
    "/auth/register",
}

# Префиксы путей, для которых НЕ проверяется сессия
_PUBLIC_PREFIXES: tuple = (
    "/static/",
)


def _is_public_path(path: str) -> bool:
    """
    Проверяет, является ли путь публичным (не требует авторизации).

    Args:
        path: URL-путь запроса

    Returns:
        True если путь публичный
    """
    if path in _PUBLIC_PATHS:
        return True

    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True

    return False


@middleware
async def auth_middleware(
    request: Request,
    handler: _Handler,
) -> StreamResponse:
    """
    Middleware проверки cookie-сессии.

    Порядок выполнения:
        1. Проверяет, является ли путь публичным → пропускает
        2. Извлекает session_id из cookie
        3. Проверяет сессию в БД через WebSessionManager
        4. Загружает данные пользователя в request["user"]
        5. Передаёт управление следующему handler

    Если сессия невалидна — request["user"] остаётся None.
    Решение о блокировке запроса принимает декоратор маршрута
    или сам обработчик.

    Args:
        request: Входящий HTTP-запрос
        handler: Следующий обработчик в цепочке

    Returns:
        HTTP-ответ от обработчика
    """
    # По умолчанию — неавторизованный запрос
    request["user"] = None

    # Публичные пути — не тратим время на проверку сессии
    if _is_public_path(request.path):
        return await handler(request)

    # Извлекаем session_id из cookie
    session_id = WebSessionManager.get_session_id_from_request(request)

    if session_id:
        try:
            session_data = await WebSessionManager.get_session(session_id)

            if session_data:
                # Сессия валидна — загружаем данные пользователя
                request["user"] = session_data
                logger.debug(
                    "Авторизован: web_user_id=%d, role=%s, path=%s",
                    session_data.get("web_user_id", 0),
                    session_data.get("role", "unknown"),
                    request.path,
                )
            else:
                # Сессия истекла или пользователь деактивирован
                logger.debug(
                    "Невалидная сессия: %s, path=%s",
                    session_id[:8],
                    request.path,
                )

        except Exception as e:
            # Ошибка БД — не блокируем запрос, но логируем
            logger.error(
                "Ошибка проверки сессии: %s (path=%s)",
                e, request.path,
            )

    return await handler(request)
