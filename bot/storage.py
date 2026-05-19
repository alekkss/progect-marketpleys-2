"""
Глобальное хранилище данных бота.

Модуль предоставляет единую точку доступа к экземпляру Database
и сессионному хранилищу SessionStorage для всех хендлеров бота.

Жизненный цикл:
    1. При импорте: db = None, session_storage = SessionStorage() (не подключён)
    2. При старте бота: await init_storage() → db.connect() + session_storage.connect()
    3. При работе: хендлеры используют db и session_storage напрямую
    4. При остановке: await shutdown_storage() → закрытие всех соединений

Важно:
    До вызова init_storage() переменные db и session_storage не готовы
    к полноценному использованию (db = None, Redis не подключён).
    Порядок гарантируется тем, что init_storage() вызывается в start_bot()
    ДО регистрации хендлеров и начала polling.
"""

from typing import Optional

from database.database import Database
from database.migrations import run_migrations
from config.config import Config
from utils.logger_config import setup_logger
from bot.session_storage import SessionStorage

logger = setup_logger('storage')

# ===================================================================
# Экземпляр базы данных (инициализируется через init_storage)
# ===================================================================
# ВАЖНО: до вызова init_storage() значение равно None.
# Все хендлеры импортируют db и используют его ПОСЛЕ инициализации.
# Порядок гарантируется тем, что init_storage() вызывается
# в start_bot() ДО регистрации хендлеров и начала polling.
# ===================================================================
db: Optional[Database] = None

# ===================================================================
# Сессионное хранилище (Redis с fallback на in-memory)
# ===================================================================
# Используется для временных данных FSM-сессий:
#   - пути к загруженным файлам при обработке (upload.py)
#   - пути к файлам при создании схем (schema_create.py, schema_create_mvm.py)
#
# Преимущества перед глобальным dict:
#   - TTL 30 минут — автоочистка "забытых" сессий
#   - Persistence (при настроенном Redis) — данные переживают перезапуск
#   - Shared state — доступно из нескольких процессов бота
#   - Graceful fallback — если Redis недоступен, работает на dict с предупреждением
# ===================================================================
session_storage: SessionStorage = SessionStorage()


async def init_storage() -> Database:
    """
    Асинхронная инициализация всех хранилищ приложения.

    Выполняет действия в строгом порядке:
        1. Создаёт экземпляр Database с параметрами из конфигурации
        2. Создаёт connection pool PostgreSQL (await db.connect())
        3. Запускает миграции (создание таблиц, обновление структуры)
        4. Подключается к Redis (session_storage.connect())

    Функция должна вызываться ОДИН РАЗ при старте приложения,
    до регистрации хендлеров и начала polling.

    Returns:
        Инициализированный экземпляр Database

    Raises:
        ValueError: если DATABASE_URL не задан в конфигурации
        asyncpg.PostgresError: если не удалось подключиться к БД
    """
    global db

    if db is not None:
        logger.warning("База данных уже инициализирована, повторный вызов пропущен.")
        return db

    logger.info("Инициализация подключения к PostgreSQL...")

    db = Database(
        database_url=Config.DATABASE_URL,
        pool_min_size=Config.DATABASE_POOL_MIN_SIZE,
        pool_max_size=Config.DATABASE_POOL_MAX_SIZE,
    )

    # Шаг 1: Создаём connection pool PostgreSQL
    await db.connect()

    # Шаг 2: Запускаем миграции (создание таблиц + обновление структуры)
    await run_migrations(db.pool)

    logger.info("База данных инициализирована и готова к работе.")

    # Шаг 3: Подключаем сессионное хранилище (Redis или fallback)
    try:
        await session_storage.connect()
    except Exception as e:
        logger.warning(
            "Сессионное хранилище не удалось инициализировать: %s. "
            "Бот продолжит работу с in-memory fallback.",
            e,
        )

    return db


async def shutdown_storage() -> None:
    """
    Корректное завершение работы всех хранилищ.

    Закрывает connection pool PostgreSQL и соединение с Redis,
    ожидая завершения всех активных запросов.
    Вызывается при остановке бота (graceful shutdown).
    """
    global db

    # Шаг 1: Закрываем Redis-соединение
    try:
        await session_storage.close()
        logger.info("Сессионное хранилище закрыто.")
    except Exception as e:
        logger.warning("Ошибка при закрытии сессионного хранилища: %s", e)

    # Шаг 2: Закрываем PostgreSQL
    if db is None:
        logger.warning("База данных не была инициализирована, закрытие пропущено.")
        return

    logger.info("Закрытие подключения к PostgreSQL...")
    await db.close()
    db = None
    logger.info("Подключение к PostgreSQL закрыто.")