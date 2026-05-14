"""
Telegram бот для синхронизации маркетплейсов.
Главный файл инициализации.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config.config import Config, TELEGRAM_BOT_TOKEN
from bot.storage import init_database, shutdown_database
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


def create_bot() -> tuple[Bot, Dispatcher]:
    """
    Создание и настройка бота.

    Создаёт экземпляры Bot и Dispatcher, регистрирует middleware
    и все обработчики команд. Порядок регистрации важен:
        1. Middleware (проверка доступа) — ПЕРЕД handlers
        2. Handlers — в порядке приоритета (common → специфичные)
        3. schema_create_mvm — ПОСЛЕ schema_create

    Returns:
        Кортеж (bot, dispatcher)
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация middleware (ПЕРЕД handlers!)
    dp.message.middleware(AccessControlMiddleware())
    dp.callback_query.middleware(AccessControlMiddleware())

    # Регистрация всех обработчиков (БЕЗ дубликатов)
    register_common_handlers(dp)
    register_access_management_handlers(dp)
    register_upload_handlers(dp, bot)
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
        2. Инициализация PostgreSQL (pool + миграции)
        3. Создание бота и диспетчера
        4. Запуск polling
        5. При остановке — graceful shutdown (закрытие pool)

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

    # Шаг 2: Инициализация базы данных
    # Создаёт connection pool и запускает миграции.
    # После этого шага глобальная переменная db в bot/storage.py
    # содержит готовый к работе экземпляр Database.
    await init_database()

    # Шаг 3: Создание бота и диспетчера
    bot, dp = create_bot()

    # Шаг 4: Запуск polling с graceful shutdown
    logger.info("Telegram бот запущен!")
    print("🚀 Telegram бот запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        # Шаг 5: Корректное завершение
        # Закрываем connection pool PostgreSQL,
        # ожидая завершения всех активных запросов
        logger.info("Остановка бота, закрытие ресурсов...")
        await shutdown_database()
        logger.info("Все ресурсы освобождены.")
