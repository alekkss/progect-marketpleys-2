"""
Telegram бот для синхронизации маркетплейсов.
Главный файл инициализации.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from typing import Optional
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from config.config import Config, TELEGRAM_BOT_TOKEN
from bot.storage import init_storage, shutdown_storage
from services.task_queue import create_task_queue, TaskQueue
from services.task_worker import TaskWorker
from services.ai_comparator import AIComparator
from utils.logger_config import setup_logger

# Импорт регистраторов обработчиков
from bot.handlers.common import register_common_handlers
from bot.handlers.upload import register_upload_handlers
from bot.handlers.schema_create import register_schema_create_handlers
from bot.handlers.schema_create_mvm import register_schema_create_mvm_handlers
from bot.handlers.schema_edit import register_schema_edit_handlers
from bot.handlers.schema_update import register_schema_update_handlers
from bot.handlers.schema_delete import register_schema_delete_handlers
from bot.handlers.stats import register_stats_handlers
from bot.handlers.access_management import register_access_management_handlers
from bot.middlewares.access_control import AccessControlMiddleware

logger = setup_logger('bot')
logging.basicConfig(level=logging.INFO)


def create_bot(
    task_queue: Optional[TaskQueue] = None,
    ai_comparator: Optional[AIComparator] = None,
) -> tuple[Bot, Dispatcher]:
    """
    Создание и настройка бота.

    Создаёт экземпляры Bot и Dispatcher, регистрирует middleware
    и все обработчики команд. Порядок регистрации важен:
        1. Middleware (проверка доступа) — ПЕРЕД handlers
        2. Handlers — в порядке приоритета (common → специфичные)
        3. schema_create_mvm — ПОСЛЕ schema_create

    FSM Storage:
        - RedisStorage (при доступном Redis) — состояния переживают перезапуск
        - MemoryStorage (fallback) — состояния теряются при перезапуске

    Args:
        task_queue: Очередь задач для фоновой обработки. Передаётся
            в хендлеры загрузки файлов для постановки задач в очередь.
        ai_comparator: Общий экземпляр AIComparator. Передаётся
            в dispatcher workflow data для инъекции в хендлеры через
            aiogram DI (параметр ai_comparator в функциях-хендлерах).

    Returns:
        Кортеж (bot, dispatcher)
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # ===================================================================
    # Выбор FSM-хранилища: Redis (предпочтительно) или Memory (fallback)
    # ===================================================================
    # RedisStorage позволяет:
    #   - Сохранять FSM-состояния при перезапуске бота
    #   - Работать с несколькими инстансами бота (horizontal scaling)
    #   - Иметь единый источник состояний для всех процессов
    #
    # Fallback на MemoryStorage если Redis недоступен — бот продолжит работу.
    # ===================================================================
    dp: Dispatcher
    try:
        fsm_storage = RedisStorage.from_url(Config.REDIS_URL)
        logger.info(
            "FSM-хранилище: Redis (%s). Состояния переживают перезапуск.",
            Config.REDIS_URL,
        )
        dp = Dispatcher(storage=fsm_storage)
    except Exception as e:
        logger.warning(
            "Не удалось подключиться к Redis для FSM: %s. "
            "Используем MemoryStorage (состояния будут потеряны при перезапуске).",
            e,
        )
        dp = Dispatcher(storage=MemoryStorage())

    # =================================================================
    # Dependency Injection через aiogram workflow data
    # =================================================================
    # Значения, установленные в dp["key"], автоматически инжектируются
    # в хендлеры как именованные параметры с тем же именем.
    # Например: dp["ai_comparator"] → async def handler(..., ai_comparator)
    # =================================================================
    if ai_comparator is not None:
        dp["ai_comparator"] = ai_comparator

    # Регистрация middleware (ПЕРЕД handlers!)
    dp.message.middleware(AccessControlMiddleware())
    dp.callback_query.middleware(AccessControlMiddleware())

    # Регистрация всех обработчиков (БЕЗ дубликатов)
    register_common_handlers(dp)
    register_access_management_handlers(dp)
    register_upload_handlers(dp, bot, task_queue)
    register_schema_create_handlers(dp, bot)
    register_schema_create_mvm_handlers(dp, bot)
    register_schema_edit_handlers(dp, bot)
    register_schema_update_handlers(dp, bot)
    register_schema_delete_handlers(dp)
    register_stats_handlers(dp)

    return bot, dp


async def start_bot() -> None:
    """
    Запуск бота с полным жизненным циклом.

    Порядок выполнения:
        1. Валидация конфигурации (fail fast)
        2. Инициализация хранилищ (PostgreSQL + Redis session storage)
        3. Подключение очереди задач (Redis или in-memory)
        4. Создание AIComparator (один экземпляр на весь жизненный цикл)
        5. Создание бота и диспетчера (с DI через workflow data)
        6. [Условно] Создание и запуск веб-сервера (если WEB_HOST задан)
        7. Запуск фонового воркера обработки задач
        8. Запуск polling
        9. При остановке — graceful shutdown (веб → воркер → очередь → ресурсы)

    Raises:
        ValueError: если обязательные параметры конфигурации не заданы
        asyncpg.PostgresError: если не удалось подключиться к БД
    """
    # Шаг 1: Валидация конфигурации
    # Если не задан DATABASE_URL, TELEGRAM_BOT_TOKEN и т.д. —
    # приложение остановится с понятным сообщением об ошибке
    try:
        Config.validate()
        logger.info("Конфигурация прошла валидацию.")
    except ValueError as e:
        logger.error("Ошибка конфигурации: %s", e)
        raise

    # Шаг 2: Инициализация хранилищ
    # Создаёт connection pool PostgreSQL, запускает миграции,
    # подключает Redis для сессий (или fallback на in-memory).
    # После этого шага глобальные переменные db и session_storage
    # в bot/storage.py содержат готовые к работе экземпляры.
    await init_storage()

    # Шаг 3: Инициализация очереди задач
    # Очередь использует тот же Redis, что и FSM (если доступен),
    # или in-memory fallback. TaskQueue — абстракция, детали скрыты.
    task_queue = create_task_queue(Config.REDIS_URL, Config.TASK_QUEUE_KEY)
    try:
        await task_queue.connect()
        logger.info("Очередь задач инициализирована.")
    except Exception as e:
        logger.warning(
            "Не удалось подключить Redis-очередь (%s). "
            "Используем in-memory очередь (задачи будут потеряны при перезапуске).",
            e,
        )
        # Фабрика create_task_queue уже вернёт InMemoryTaskQueue при ошибке,
        # но если connect() упал после создания — пересоздаём.
        task_queue = create_task_queue(None, Config.TASK_QUEUE_KEY)
        await task_queue.connect()

    # Шаг 4: Создание AIComparator (единственный экземпляр)
    # Промпты читаются с диска один раз. Семафор внутри ограничивает
    # суммарное число AI-запросов по всем задачам и хендлерам.
    # Этот экземпляр используется и в TaskWorker, и в хендлерах
    # создания/обновления схем через aiogram DI.
    ai_comparator = AIComparator()

    # Шаг 5: Создание бота и диспетчера
    # ai_comparator передаётся в dp["ai_comparator"] для автоматической
    # инъекции в хендлеры, которые объявляют параметр ai_comparator.
    bot, dp = create_bot(task_queue, ai_comparator)

    # ===================================================================
    # Шаг 6: Условный запуск веб-сервера
    # ===================================================================
    # Веб-сервер запускается ТОЛЬКО если WEB_HOST задан в .env.
    # Если не задан — бот работает как раньше, без веб-интерфейса.
    #
    # Бот и веб работают в одном event loop через asyncio:
    #   - aiohttp слушает на WEB_HOST:WEB_PORT (за Nginx)
    #   - aiogram polling работает параллельно
    #   - Общие ресурсы: db pool, Redis, TaskQueue, AIComparator
    #
    # WebSocketManager передаётся в TaskWorker для WebDelivery.
    # ===================================================================
    web_runner = None
    ws_manager = None

    if Config.WEB_HOST:
        try:
            from aiohttp import web
            from web.app import create_web_app

            ws_manager_module = __import__(
                'web.routes.websocket', fromlist=['WebSocketManager']
            )
            WebSocketManager = ws_manager_module.WebSocketManager
            ws_manager = WebSocketManager()

            web_app = await create_web_app(
                task_queue=task_queue,
                ai_comparator=ai_comparator,
                ws_manager=ws_manager,
            )

            web_runner = web.AppRunner(web_app)
            await web_runner.setup()
            site = web.TCPSite(web_runner, Config.WEB_HOST, Config.WEB_PORT)
            await site.start()

            logger.info(
                "Веб-сервер запущен: http://%s:%s (домен: %s)",
                Config.WEB_HOST,
                Config.WEB_PORT,
                Config.WEB_DOMAIN,
            )
            print(f"🌐 Веб-сервер: http://{Config.WEB_HOST}:{Config.WEB_PORT}")

        except ImportError as e:
            logger.warning(
                "Не удалось импортировать веб-модули (%s). "
                "Веб-сервер не запущен. Установите: "
                "pip install aiohttp-jinja2 aiohttp-session bcrypt jinja2",
                e,
            )
            web_runner = None
            ws_manager = None
        except Exception as e:
            logger.error(
                "Ошибка запуска веб-сервера: %s. Бот продолжит работу без веба.",
                e,
                exc_info=True,
            )
            web_runner = None
            ws_manager = None

    # Шаг 7: Запуск фонового воркера
    # Воркер получает тот же ai_comparator — НЕ создаёт свой.
    # ws_manager передаётся для WebDelivery (None если веб выключен).
    # Семафор общий: максимум 5 AI-запросов суммарно.
    task_worker = TaskWorker(
        task_queue,
        Config.MAX_CONCURRENT_TASKS,
        ai_comparator,
        ws_manager,
    )
    await task_worker.start(bot)

    # Шаг 8: Запуск polling с graceful shutdown
    logger.info("Telegram бот запущен!")
    print("🚀 Telegram бот запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        # Шаг 9: Корректное завершение
        # Порядок важен:
        #   1. Останавливаем веб-сервер (прекращаем приём HTTP)
        #   2. Останавливаем воркер (ждём активных задач)
        #   3. Отключаем очередь (Redis)
        #   4. Закрываем основные ресурсы (PostgreSQL pool)
        logger.info("Остановка приложения, закрытие ресурсов...")

        if web_runner:
            await web_runner.cleanup()
            logger.info("Веб-сервер остановлен.")

        await task_worker.stop()
        await task_queue.disconnect()
        await shutdown_storage()
        logger.info("Все ресурсы освобождены.")
