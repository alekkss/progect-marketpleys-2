"""
Bearer-аутентификация внешнего REST API AI-агента (маршруты /v1/*).

Изолированный контур безопасности (п. 7.2 доработок):
    - Cookie-сессии сайта (/auth/*, /dashboard, ...) — внутренний контур
    - Bearer-токен FDM (/v1/*) — внешний контур

Контур НЕ пересекаются: проверка cookie здесь не выполняется,
Bearer-токен не имеет отношения к web_users/web_sessions.

Логика:
    1. Действует ТОЛЬКО на пути с префиксом /v1/ — остальные
       запросы проходят без изменений.
    2. FDM_API_TOKEN пуст → 503 Service Unavailable: агент
       выключен, деплой без токена безопасен по умолчанию.
    3. Заголовок Authorization отсутствует/невалиден → 401.
    4. Токен не совпал → 401 (constant-time сравнение).

Ответы об ошибках — JSON (error, status, message): единый
формат ошибок API (п. 1.5 доработок).

Паттерн: Middleware — проверка выполняется до обработчиков
маршрутов /v1/*, сами обработчики не занимаются аутентификацией.
"""

import secrets
from typing import Callable, Awaitable

from aiohttp import web
from aiohttp.web import Request, StreamResponse

from config.config import Config
from utils.logger_config import setup_logger

logger = setup_logger("web.middleware.api_auth")

# Тип handler в aiohttp middleware
_Handler = Callable[[Request], Awaitable[StreamResponse]]

# Префикс путей внешнего API агента
_API_PREFIX: str = "/v1/"

# Схема заголовка Authorization (регистронезависимая по RFC 7235,
# нормализуем к нижнему регистру перед сравнением)
_AUTH_SCHEME: str = "bearer"


def _unauthorized(message: str) -> web.Response:
    """
    Формирует JSON-ответ 401 Unauthorized.

    Заголовок WWW-Authenticate обязателен по RFC 6750 при ошибке
    Bearer-аутентификации — корректные API-клиенты используют его
    для диагностики.

    Args:
        message: Описание ошибки для клиента FDM

    Returns:
        aiohttp Response 401 с JSON-телом
    """
    return web.json_response(
        {"error": True, "status": 401, "message": message},
        status=401,
        headers={"WWW-Authenticate": 'Bearer realm="mapping-api"'},
    )


@web.middleware
async def api_auth_middleware(
    request: Request,
    handler: _Handler,
) -> StreamResponse:
    """
    Проверяет Bearer-токен для запросов к /v1/*.

    Порядок:
        1. Не /v1/* — пропуск без каких-либо проверок.
        2. Токен агента не настроен — 503 (агент выключен).
        3. Разбор Authorization: Bearer <token>.
        4. Constant-time сравнение с FDM_API_TOKEN.

    Безопасность:
        - secrets.compare_digest исключает timing-атаки;
        - подробности отказа не раскрываются (нет/невалиден/
          не совпал — единый ответ "невалидный токен"), чтобы
          не давать внешнему сканеру информацию о состоянии;
        - все отказы логируются с IP для мониторинга подборов.

    Args:
        request: Входящий HTTP-запрос
        handler: Следующий обработчик в цепочке

    Returns:
        HTTP-ответ (401/503 при отказе, иначе ответ обработчика)
    """
    # Шаг 1: действует только на /v1/*
    if not request.path.startswith(_API_PREFIX):
        return await handler(request)

    # Шаг 2: агент выключен — единый ответ для всех /v1/*-запросов
    expected_token = Config.FDM_API_TOKEN
    if not expected_token:
        logger.warning(
            "Запрос к выключенному API агента: %s %s (IP=%s)",
            request.method, request.path, request.remote,
        )
        return web.json_response(
            {
                "error": True,
                "status": 503,
                "message": "AI-агент маппинга отключён на сервере",
            },
            status=503,
        )

    # Шаг 3: разбор заголовка Authorization
    auth_header = request.headers.get("Authorization", "")
    scheme, _, token = auth_header.partition(" ")

    if not token or scheme.lower() != _AUTH_SCHEME:
        logger.warning(
            "API агента: отсутствует или невалиден Authorization "
            "(%s %s, IP=%s)",
            request.method, request.path, request.remote,
        )
        return _unauthorized(
            "Требуется заголовок Authorization: Bearer <токен>"
        )

    # Шаг 4: constant-time сравнение
    if not secrets.compare_digest(token.strip(), expected_token):
        logger.warning(
            "API агента: невалидный Bearer-токен (%s %s, IP=%s)",
            request.method, request.path, request.remote,
        )
        return _unauthorized("Невалидный токен доступа")

    # Аутентификация пройдена
    return await handler(request)
