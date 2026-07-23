"""
Фабрика создания aiohttp веб-приложения.

Собирает все компоненты в единое Application:
    - Jinja2 шаблонизатор (серверный рендеринг HTML)
    - Middleware (обработка ошибок, auth, csrf)
    - Маршруты (health-check, WebSocket, бизнес-маршруты)
    - Shared-ресурсы через app context (DI для обработчиков маршрутов)

Паттерн: Factory — скрывает сложность создания и настройки Application.
Паттерн: Dependency Injection — shared-ресурсы передаются через аргументы
и сохраняются в app["key"] для доступа из обработчиков маршрутов.

Использование:
    from web import create_web_app
    web_app = await create_web_app(task_queue, ai_comparator, ws_manager)
"""

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger_config import setup_logger
from web.middleware import setup_middlewares
from web.routes import setup_routes
from web.routes.websocket import WebSocketManager, setup_websocket_routes

if TYPE_CHECKING:
    from services.ai_comparator import AIComparator
    from services.task_queue import TaskQueue

logger = setup_logger("web.app")

# Путь к директории шаблонов
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Путь к директории статических файлов
_STATIC_DIR = Path(__file__).parent / "static"


def _setup_jinja2(app: web.Application) -> None:
    """
    Настраивает Jinja2 шаблонизатор для aiohttp.

    Шаблоны загружаются из web/templates/. Если библиотека
    aiohttp-jinja2 не установлена — шаблонизатор не подключается
    (приложение продолжит работать, но HTML-страницы будут недоступны).

    Глобальные переменные и функции доступные во всех шаблонах:
        - app_name: Название приложения
        - web_domain: Публичный домен
        - _shorten_filename(filename): Сокращение имени файла для кнопок скачивания

    Args:
        app: Экземпляр aiohttp Application
    """
    try:
        import aiohttp_jinja2
        import jinja2
    except ImportError:
        logger.warning(
            "aiohttp-jinja2 или jinja2 не установлены. "
            "HTML-шаблоны будут недоступны. "
            "Установите: pip install aiohttp-jinja2 jinja2"
        )
        return

    from config.config import Config
    from web.routes.tasks import shorten_filename

    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        context_processors=[aiohttp_jinja2.request_processor],
    )

    # Глобальные переменные и функции для всех шаблонов
    env = aiohttp_jinja2.get_env(app)
    env.globals.update({
        "app_name": "Marketplace Sync",
        "web_domain": Config.WEB_DOMAIN,
        "_shorten_filename": shorten_filename,
    })

    logger.info("Jinja2 шаблонизатор настроен (шаблоны: %s)", _TEMPLATES_DIR)


def _setup_static(app: web.Application) -> None:
    """
    Настраивает раздачу статических файлов.

    В production статику раздаёт Nginx напрямую (location /static/).
    Этот маршрут нужен только для разработки без Nginx.

    Args:
        app: Экземпляр aiohttp Application
    """
    if _STATIC_DIR.exists():
        app.router.add_static("/static/", path=str(_STATIC_DIR), name="static")
        logger.debug(
            "Статические файлы: %s (в production используйте Nginx)",
            _STATIC_DIR,
        )
    else:
        logger.warning(
            "Директория статических файлов не найдена: %s",
            _STATIC_DIR,
        )


async def _on_startup(app: web.Application) -> None:
    """
    Callback при запуске приложения.

    Вызывается aiohttp после создания Application, но до
    начала обработки запросов. Используется для логирования
    и проверки доступности ресурсов.

    Args:
        app: Экземпляр aiohttp Application
    """
    logger.info(
        "Веб-приложение инициализировано "
        "(маршруты: %d, middleware: %d)",
        len(app.router.routes()),
        len(app.middlewares),
    )


async def _on_shutdown(app: web.Application) -> None:
    """
    Callback при остановке приложения.

    Вызывается при graceful shutdown. Закрывает все
    активные WebSocket-соединения.

    Args:
        app: Экземпляр aiohttp Application
    """
    ws_manager: WebSocketManager = app["ws_manager"]
    await ws_manager.close_all()
    logger.info("Веб-приложение остановлено")


async def create_web_app(
    task_queue: "TaskQueue",
    ai_comparator: "AIComparator",
    ws_manager: WebSocketManager,
) -> web.Application:
    """
    Создаёт полностью настроенный aiohttp Application.

    Порядок настройки:
        1. Создание Application
        2. Сохранение shared-ресурсов в app context (DI)
        3. Настройка Jinja2 шаблонизатора
        4. Регистрация middleware
        5. Регистрация маршрутов (основные + WebSocket)
        6. Настройка статических файлов (dev mode)
        7. Регистрация lifecycle callbacks

    Shared-ресурсы доступны из обработчиков через request.app["key"]:
        request.app["task_queue"]     — очередь задач
        request.app["ai_comparator"]  — AI-компаратор
        request.app["ws_manager"]     — менеджер WebSocket-соединений
        request.app["db"]             — экземпляр Database (из bot/storage.py)

    Args:
        task_queue: Общая очередь задач (та же, что у бота)
        ai_comparator: Общий AI-компаратор (один на весь процесс)
        ws_manager: Менеджер WebSocket-соединений

    Returns:
        Настроенный aiohttp Application, готовый к запуску
    """
    from bot import storage

    app = web.Application(
        client_max_size=250 * 1024 * 1024,  # 250 МБ для загрузки файлов
    )

    # ===================================================================
    # Dependency Injection через app context
    # ===================================================================
    # Все shared-ресурсы сохраняются в app["key"] и доступны
    # из любого обработчика через request.app["key"].
    # Это те же экземпляры, что использует Telegram-бот —
    # единый DB pool, единая очередь, единый AI-компаратор.
    # ===================================================================
    app["task_queue"] = task_queue
    app["ai_comparator"] = ai_comparator
    app["ws_manager"] = ws_manager
    app["db"] = storage.db

    # Настройка компонентов
    _setup_jinja2(app)
    setup_middlewares(app)
    setup_routes(app)
    setup_websocket_routes(app)
    _setup_static(app)

    # Lifecycle callbacks
    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)

    logger.info("Веб-приложение создано")
    return app
