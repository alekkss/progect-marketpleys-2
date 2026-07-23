"""
CSRF-защита для веб-форм.

Механизм Double Submit Cookie:
    1. При первом GET-запросе генерируется случайный токен
    2. Токен сохраняется в cookie (httponly=False — доступен JS для AJAX)
    3. В HTML-формах токен дублируется в hidden field <input name="csrf_token">
    4. При POST middleware проверяет совпадение cookie-токена и form/header-токена

Почему Double Submit Cookie, а не Synchronizer Token:
    - Не требует серверного хранилища (Redis/DB) для токенов
    - Stateless — работает с любым количеством инстансов
    - Достаточно безопасен при HTTPS (cookie не перехватывается)

Паттерн: Middleware — проверка выполняется автоматически для всех
мутирующих запросов, без изменения обработчиков маршрутов.

Пропускаемые пути:
    - /health — мониторинг
    - /api/* — JSON API (защищается через заголовок X-CSRF-Token)
    - /ws/* — WebSocket

Отключение: WEB_CSRF_ENABLED=false в .env (для отладки).
"""

import secrets
from typing import Callable, Awaitable, Set

from aiohttp.web import Request, StreamResponse, middleware, HTTPForbidden

from config.config import Config
from utils.logger_config import setup_logger

logger = setup_logger("web.middleware.csrf")

# Тип handler в aiohttp middleware
_Handler = Callable[[Request], Awaitable[StreamResponse]]

# Имя cookie для CSRF-токена
CSRF_COOKIE_NAME: str = "CSRF_TOKEN"

# Имя hidden field в HTML-формах
CSRF_FIELD_NAME: str = "csrf_token"

# Имя заголовка для AJAX-запросов
CSRF_HEADER_NAME: str = "X-CSRF-Token"

# Длина токена (32 байта = 64 hex-символа)
_TOKEN_LENGTH: int = 32

# HTTP-методы, требующие проверки CSRF
_UNSAFE_METHODS: Set[str] = {"POST", "PUT", "DELETE", "PATCH"}

# Пути, исключённые из CSRF-проверки
_EXEMPT_PATHS: Set[str] = {
    "/health",
}

# Префиксы путей, исключённых из CSRF-проверки
_EXEMPT_PREFIXES: tuple = (
    "/ws/",
)


def _is_exempt(request: Request) -> bool:
    """
    Проверяет, исключён ли путь из CSRF-проверки.

    Args:
        request: HTTP-запрос

    Returns:
        True если путь исключён
    """
    path = request.path

    if path in _EXEMPT_PATHS:
        return True

    for prefix in _EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return True

    return False


def generate_csrf_token() -> str:
    """
    Генерирует криптографически стойкий CSRF-токен.

    Returns:
        Hex-строка длиной 64 символа
    """
    return secrets.token_hex(_TOKEN_LENGTH)


@middleware
async def csrf_middleware(
    request: Request,
    handler: _Handler,
) -> StreamResponse:
    """
    Middleware CSRF-защиты.

    Логика:
        1. Если CSRF отключён в конфигурации — пропускает
        2. Для GET/HEAD/OPTIONS — генерирует токен (если нет в cookie)
        3. Для POST/PUT/DELETE/PATCH — проверяет совпадение токенов

    Проверка токена:
        - Из cookie: request.cookies[CSRF_COOKIE_NAME]
        - Из формы: post_data[csrf_token] или headers[X-CSRF-Token]
        - Если не совпадают — 403 Forbidden

    Args:
        request: Входящий HTTP-запрос
        handler: Следующий обработчик в цепочке

    Returns:
        HTTP-ответ (возможно с установленной CSRF-cookie)
    """
    # Отключение CSRF через конфигурацию (для отладки)
    if not Config.WEB_CSRF_ENABLED:
        return await handler(request)

    # Исключённые пути
    if _is_exempt(request):
        return await handler(request)

    # Безопасные методы — только установка cookie с токеном
    if request.method not in _UNSAFE_METHODS:
        response = await handler(request)
        _ensure_csrf_cookie(request, response)
        return response

    # Небезопасные методы — проверка токена
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")

    if not cookie_token:
        logger.warning(
            "CSRF: отсутствует cookie-токен (path=%s, method=%s)",
            request.path, request.method,
        )
        raise HTTPForbidden(reason="CSRF-токен отсутствует. Обновите страницу.")

    # Получаем токен из формы или заголовка
    submitted_token = await _get_submitted_token(request)

    if not submitted_token:
        logger.warning(
            "CSRF: токен не передан в форме/заголовке (path=%s)",
            request.path,
        )
        raise HTTPForbidden(reason="CSRF-токен не передан. Обновите страницу.")

    # Сравнение (constant-time через secrets.compare_digest)
    if not secrets.compare_digest(cookie_token, submitted_token):
        logger.warning(
            "CSRF: токены не совпадают (path=%s, method=%s)",
            request.path, request.method,
        )
        raise HTTPForbidden(reason="Невалидный CSRF-токен. Обновите страницу.")

    # Токен валиден — пропускаем запрос
    response = await handler(request)
    return response


async def _get_submitted_token(request: Request) -> str:
    """
    Извлекает CSRF-токен из тела запроса или заголовка.

    Порядок проверки:
        1. Заголовок X-CSRF-Token (для AJAX)
        2. Поле формы csrf_token (для HTML-форм)

    Args:
        request: HTTP-запрос

    Returns:
        Строка токена или пустая строка
    """
    # Проверка заголовка (AJAX)
    header_token = request.headers.get(CSRF_HEADER_NAME, "")
    if header_token:
        return header_token

    # Проверка тела формы (HTML)
    try:
        content_type = request.content_type or ""
        if "multipart/form-data" in content_type:
            # Для multipart (загрузка файлов) — читаем только поле csrf_token
            reader = await request.multipart()
            async for part in reader:
                if part.name == CSRF_FIELD_NAME:
                    token = await part.text()
                    return token.strip()
                # Пропускаем остальные части (файлы)
                await part.read()
            return ""
        else:
            post_data = await request.post()
            return str(post_data.get(CSRF_FIELD_NAME, "")).strip()
    except Exception:
        return ""


def _ensure_csrf_cookie(request: Request, response: StreamResponse) -> None:
    """
    Устанавливает CSRF-cookie если её нет.

    Вызывается для GET-запросов — при первом посещении
    генерируется новый токен.

    Cookie-атрибуты:
        - httponly=False: JS должен иметь доступ для AJAX-заголовков
        - secure=True: только через HTTPS
        - samesite=Lax: защита от CSRF в cross-site запросах

    Args:
        request: HTTP-запрос (для проверки наличия cookie)
        response: HTTP-ответ (для установки cookie)
    """
    existing_token = request.cookies.get(CSRF_COOKIE_NAME)

    if not existing_token:
        token = generate_csrf_token()
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            max_age=Config.WEB_SESSION_MAX_AGE,
            httponly=False,  # JS нужен доступ для X-CSRF-Token
            secure=True,
            samesite="Lax",
            path="/",
        )


def get_csrf_token(request: Request) -> str:
    """
    Получает текущий CSRF-токен из cookie запроса.

    Используется в шаблонах для вставки в hidden field:
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

    Если токена нет (первый запрос) — генерирует новый.
    Новый токен будет установлен в cookie через middleware
    при следующем GET-запросе.

    Args:
        request: HTTP-запрос

    Returns:
        CSRF-токен (строка)
    """
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = generate_csrf_token()
    return token
