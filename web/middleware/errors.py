"""
Middleware обработки ошибок HTTP.

Перехватывает все исключения, возникающие в обработчиках маршрутов
и внутренних middleware. Возвращает:
    - JSON-ответ для AJAX-запросов (Accept: application/json или /api/*)
    - HTML-страницу для обычных браузерных запросов

Паттерн: Middleware (Chain of Responsibility) — ошибка перехватывается
на внешнем слое, внутренние обработчики не занимаются форматированием ошибок.
"""

from typing import Callable, Awaitable

from aiohttp import web
from aiohttp.web import Request, StreamResponse

from utils.logger_config import setup_logger

logger = setup_logger("web.middleware.errors")

# Тип handler в aiohttp middleware
_Handler = Callable[[Request], Awaitable[StreamResponse]]


def _is_json_request(request: Request) -> bool:
    """
    Определяет, ожидает ли клиент JSON-ответ.

    Критерии:
        - Заголовок Accept содержит 'application/json'
        - URL начинается с /api/
        - Заголовок X-Requested-With == 'XMLHttpRequest' (AJAX)

    Args:
        request: Входящий HTTP-запрос

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


def _json_error_response(status: int, message: str) -> web.Response:
    """
    Формирует JSON-ответ об ошибке.

    Args:
        status: HTTP-статус код
        message: Описание ошибки

    Returns:
        aiohttp Response с JSON-телом
    """
    return web.json_response(
        {"error": True, "status": status, "message": message},
        status=status,
    )


def _html_error_response(status: int, title: str, message: str) -> web.Response:
    """
    Формирует минимальную HTML-страницу ошибки.

    В Фазе 4 будет заменена на рендеринг через Jinja2-шаблон.
    Сейчас — самодостаточный HTML без внешних зависимостей.

    Args:
        status: HTTP-статус код
        title: Заголовок страницы
        message: Текст ошибки для пользователя

    Returns:
        aiohttp Response с HTML-телом
    """
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            background-color: #f8fafc;
            color: #334155;
        }}
        .error-container {{
            text-align: center;
            padding: 2rem;
        }}
        .error-code {{
            font-size: 4rem;
            font-weight: 700;
            color: #ef4444;
            margin-bottom: 0.5rem;
        }}
        .error-title {{
            font-size: 1.5rem;
            font-weight: 600;
            margin-bottom: 1rem;
        }}
        .error-message {{
            font-size: 1rem;
            color: #64748b;
            margin-bottom: 2rem;
        }}
        .back-link {{
            display: inline-block;
            padding: 0.75rem 1.5rem;
            background-color: #3b82f6;
            color: white;
            text-decoration: none;
            border-radius: 0.5rem;
            font-weight: 500;
            transition: background-color 0.2s;
        }}
        .back-link:hover {{
            background-color: #2563eb;
        }}
    </style>
</head>
<body>
    <div class="error-container">
        <div class="error-code">{status}</div>
        <div class="error-title">{title}</div>
        <div class="error-message">{message}</div>
        <a href="/" class="back-link">На главную</a>
    </div>
</body>
</html>"""
    return web.Response(
        text=html,
        status=status,
        content_type="text/html",
    )


# Маппинг HTTP-статусов на понятные описания
_ERROR_MESSAGES = {
    400: ("Некорректный запрос", "Сервер не смог обработать ваш запрос. Проверьте данные и попробуйте снова."),
    403: ("Доступ запрещён", "У вас нет прав для выполнения этого действия."),
    404: ("Страница не найдена", "Запрашиваемая страница не существует или была удалена."),
    405: ("Метод не разрешён", "Этот HTTP-метод не поддерживается для данного URL."),
    413: ("Файл слишком большой", "Размер загружаемого файла превышает допустимый лимит."),
    429: ("Слишком много запросов", "Превышен лимит запросов. Подождите немного и попробуйте снова."),
    500: ("Внутренняя ошибка сервера", "Произошла непредвиденная ошибка. Мы уже работаем над исправлением."),
}


@web.middleware
async def error_middleware(
    request: Request,
    handler: _Handler,
) -> StreamResponse:
    """
    Middleware перехвата HTTP-ошибок.

    Оборачивает вызов handler в try/except и возвращает
    корректный ответ в зависимости от типа клиента (JSON/HTML).

    Логирует все 5xx ошибки с полным traceback для отладки.
    4xx ошибки логируются на уровне WARNING без traceback.

    Args:
        request: Входящий HTTP-запрос
        handler: Следующий обработчик в цепочке middleware

    Returns:
        HTTP-ответ (нормальный или страница ошибки)
    """
    try:
        return await handler(request)

    except web.HTTPException as exc:
        status = exc.status
        is_json = _is_json_request(request)

        # Редиректы (3xx) пропускаем без обработки
        if 300 <= status < 400:
            raise

        # Получаем описание ошибки
        title, message = _ERROR_MESSAGES.get(
            status,
            ("Ошибка", exc.reason or "Произошла ошибка при обработке запроса."),
        )

        # Логирование
        if status >= 500:
            logger.error(
                "HTTP %d: %s %s — %s",
                status, request.method, request.path, exc.reason,
                exc_info=True,
            )
        else:
            logger.warning(
                "HTTP %d: %s %s — %s",
                status, request.method, request.path, exc.reason,
            )

        # Формирование ответа
        if is_json:
            return _json_error_response(status, message)
        return _html_error_response(status, title, message)

    except asyncio.CancelledError:
        # Не перехватываем отмену — это штатное завершение
        raise

    except Exception as exc:
        # Непредвиденная ошибка — всегда 500
        logger.error(
            "Необработанное исключение: %s %s",
            request.method, request.path,
            exc_info=True,
        )

        is_json = _is_json_request(request)
        title, message = _ERROR_MESSAGES[500]

        if is_json:
            return _json_error_response(500, message)
        return _html_error_response(500, title, message)


# Импорт asyncio нужен для CancelledError
import asyncio
