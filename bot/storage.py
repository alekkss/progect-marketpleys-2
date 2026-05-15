"""
Глобальное хранилище данных бота.

Модуль предоставляет единую точку доступа к экземпляру Database
и временным in-memory хранилищам для всех хендлеров бота.

Жизненный цикл Database:
    1. При импорте: db = None (заглушка до инициализации)
    2. При старте бота: await init_database() → db = Database(...)
    3. При работе: хендлеры используют db напрямую
    4. При остановке: await shutdown_database() → pool.close()

Временные хранилища (user_files, user_schemas):
    - Живут только в оперативной памяти
    - Сбрасываются при перезапуске бота
    - Ключ: user_id (int), значение: dict с путями к файлам
    - Очищаются хендлером после завершения FSM-сессии
"""

from typing import Optional, Dict

from database.database import Database
from database.migrations import run_migrations
from config.config import Config
from utils.logger_config import setup_logger

logger = setup_logger('storage')

# ===================================================================
# Экземпляр базы данных (инициализируется через init_database)
# ===================================================================
# ВАЖНО: до вызова init_database() значение равно None.
# Все хендлеры импортируют db и используют его ПОСЛЕ инициализации.
# Порядок гарантируется тем, что init_database() вызывается
# в start_bot() ДО регистрации хендлеров и начала polling.
# ===================================================================
db: Optional[Database] = None

# ===================================================================
# Временные in-memory хранилища для FSM-сессий
# ===================================================================
# user_files: временное хранение путей к загруженным Excel/XML файлам
#   при обработке (upload.py). Формат: {user_id: {'wildberries': path, ...}}
#
# user_schemas: временное хранение путей к файлам при создании
#   и редактировании схем (schema_create.py, schema_edit.py и др.).
#   Формат: {user_id: {'wildberries': path, 'ozon': path, 'yandex': path}}
#
# Оба словаря очищаются хендлером после завершения FSM-сессии
# (state.clear() + user_files[user_id] = {}).
# ===================================================================
user_files: Dict[int, Dict] = {}
user_schemas: Dict[int, Dict] = {}


async def init_database() -> Database:
    """
    Асинхронная инициализация базы данных.

    Выполняет три действия в строгом порядке:
        1. Создаёт экземпляр Database с параметрами из конфигурации
        2. Создаёт connection pool (await db.connect())
        3. Запускает миграции (создание таблиц, обновление структуры)

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

    # Создаём connection pool
    await db.connect()

    # Запускаем миграции (создание таблиц + обновление структуры)
    await run_migrations(db.pool)

    logger.info("База данных инициализирована и готова к работе.")
    return db


async def shutdown_database() -> None:
    """
    Корректное завершение работы с базой данных.

    Закрывает connection pool, ожидая завершения всех
    активных запросов. Вызывается при остановке бота
    (graceful shutdown).
    """
    global db

    if db is None:
        logger.warning("База данных не была инициализирована, закрытие пропущено.")
        return

    logger.info("Закрытие подключения к PostgreSQL...")
    await db.close()
    db = None
    logger.info("Подключение к PostgreSQL закрыто.")
