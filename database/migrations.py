"""
Модуль миграций базы данных PostgreSQL.

Отвечает за создание структуры таблиц и выполнение
последовательных миграций при обновлении версий приложения.
Каждая миграция идемпотентна — безопасно запускать повторно.
"""

import asyncpg
from utils.logger_config import setup_logger

logger = setup_logger('migrations')

# ===================================================================
# SQL-скрипт создания всех таблиц (идемпотентный)
# ===================================================================
# Порядок важен: сначала таблицы без зависимостей, затем с FOREIGN KEY.
# IF NOT EXISTS гарантирует безопасный повторный запуск.
# ===================================================================

CREATE_TABLES_SQL: str = """

-- Таблица пользователей
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    total_processings INTEGER DEFAULT 0
);

-- Таблица истории обработок
CREATE TABLE IF NOT EXISTS processing_history (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users (user_id),
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    wb_products_count INTEGER DEFAULT 0,
    ozon_products_count INTEGER DEFAULT 0,
    yandex_products_count INTEGER DEFAULT 0,
    synced_cells_count INTEGER DEFAULT 0,
    status TEXT,
    error_message TEXT
);

-- Таблица файлов
CREATE TABLE IF NOT EXISTS files (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users (user_id),
    processing_id INTEGER REFERENCES processing_history (id),
    marketplace TEXT,
    original_filename TEXT,
    file_path TEXT,
    uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Таблица схем сопоставлений
CREATE TABLE IF NOT EXISTS schemas (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users (user_id),
    schema_name TEXT,
    schema_type TEXT NOT NULL DEFAULT 'standard',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    full_comparison_json TEXT,
    UNIQUE(user_id, schema_name)
);

-- Таблица сопоставлений столбцов в схеме (legacy, для совместимости)
CREATE TABLE IF NOT EXISTS schema_matches (
    id SERIAL PRIMARY KEY,
    schema_id INTEGER REFERENCES schemas (id) ON DELETE CASCADE,
    wb_column TEXT,
    ozon_column TEXT,
    yandex_column TEXT,
    confidence REAL,
    is_mandatory BOOLEAN DEFAULT FALSE
);

-- Таблица системных настроек
CREATE TABLE IF NOT EXISTS system_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_by BIGINT
);

-- Таблица белого списка пользователей
CREATE TABLE IF NOT EXISTS whitelist_users (
    id SERIAL PRIMARY KEY,
    user_id BIGINT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    added_by BIGINT,
    notes TEXT,
    CONSTRAINT whitelist_role_check CHECK (role IN ('editor', 'user'))
);

-- Индексы для ускорения частых запросов
CREATE INDEX IF NOT EXISTS idx_processing_history_user_id
    ON processing_history (user_id);

CREATE INDEX IF NOT EXISTS idx_files_user_id
    ON files (user_id);

CREATE INDEX IF NOT EXISTS idx_files_processing_id
    ON files (processing_id);

CREATE INDEX IF NOT EXISTS idx_schemas_user_id
    ON schemas (user_id);

CREATE INDEX IF NOT EXISTS idx_schema_matches_schema_id
    ON schema_matches (schema_id);

CREATE INDEX IF NOT EXISTS idx_whitelist_users_role
    ON whitelist_users (role);

"""


# ===================================================================
# Последовательные миграции
# ===================================================================
# Каждая миграция — кортеж (название, SQL-запрос).
# Все миграции идемпотентны (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
# Новые миграции ВСЕГДА добавляются В КОНЕЦ списка.
# ВАЖНО: нумерация должна быть уникальной и последовательной —
# 001_, 002_, 003_ и т.д.
# ===================================================================

MIGRATIONS: list[tuple[str, str]] = [
    (
        "001_add_role_column",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'whitelist_users' AND column_name = 'role'
            ) THEN
                ALTER TABLE whitelist_users
                    ADD COLUMN role TEXT NOT NULL DEFAULT 'user';
                ALTER TABLE whitelist_users
                    ADD CONSTRAINT whitelist_role_check
                    CHECK (role IN ('editor', 'user'));
            END IF;
        END $$;
        """
    ),
    (
        "002_add_schema_type_column",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'schemas' AND column_name = 'schema_type'
            ) THEN
                ALTER TABLE schemas
                    ADD COLUMN schema_type TEXT NOT NULL DEFAULT 'standard';
            END IF;
        END $$;
        """
    ),
    (
        "003_create_web_users_table",
        """
        CREATE TABLE IF NOT EXISTS web_users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            telegram_user_id BIGINT UNIQUE,
            role TEXT NOT NULL DEFAULT 'user',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_login_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT web_users_role_check CHECK (role IN ('owner', 'admin', 'editor', 'user'))
        );
        CREATE INDEX IF NOT EXISTS idx_web_users_email ON web_users(email);
        CREATE INDEX IF NOT EXISTS idx_web_users_telegram_id ON web_users(telegram_user_id);
        """
    ),
    (
        "004_create_web_sessions_table",
        """
        CREATE TABLE IF NOT EXISTS web_sessions (
            id TEXT PRIMARY KEY,
            web_user_id INTEGER REFERENCES web_users(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            ip_address TEXT,
            user_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_web_sessions_user_id ON web_sessions(web_user_id);
        CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at);
        """
    ),
    (
        "005_create_task_results_table",
        """
        CREATE TABLE IF NOT EXISTS task_results (
            id SERIAL PRIMARY KEY,
            task_id TEXT UNIQUE NOT NULL,
            web_user_id INTEGER REFERENCES web_users(id),
            status TEXT NOT NULL DEFAULT 'pending',
            output_files JSONB,
            report_path TEXT,
            stats JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            completed_at TIMESTAMP WITH TIME ZONE,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_task_results_user_id ON task_results(web_user_id);
        CREATE INDEX IF NOT EXISTS idx_task_results_task_id ON task_results(task_id);
        """
    ),
    (
        "006_add_web_user_id_to_schemas",
        """
        DO $$
        BEGIN
            -- Шаг 1: Добавляем столбец web_user_id если не существует
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'schemas' AND column_name = 'web_user_id'
            ) THEN
                ALTER TABLE schemas
                    ADD COLUMN web_user_id INTEGER REFERENCES web_users(id);
            END IF;

            -- Шаг 2: Делаем user_id nullable (убираем NOT NULL если был)
            -- ALTER COLUMN ... DROP NOT NULL идемпотентен — не ошибается если уже nullable
            ALTER TABLE schemas ALTER COLUMN user_id DROP NOT NULL;

            -- Шаг 3: Удаляем старый constraint уникальности (user_id, schema_name)
            -- и создаём новый, который учитывает nullable user_id
            IF EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_name = 'schemas'
                  AND constraint_type = 'UNIQUE'
                  AND constraint_name = 'schemas_user_id_schema_name_key'
            ) THEN
                ALTER TABLE schemas
                    DROP CONSTRAINT schemas_user_id_schema_name_key;
            END IF;

            -- Шаг 4: Создаём новые уникальные индексы
            -- Для Telegram-пользователей: уникальность по (user_id, schema_name) где user_id NOT NULL
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_schemas_unique_user_name'
            ) THEN
                CREATE UNIQUE INDEX idx_schemas_unique_user_name
                    ON schemas (user_id, schema_name)
                    WHERE user_id IS NOT NULL;
            END IF;

            -- Для веб-пользователей: уникальность по (web_user_id, schema_name) где web_user_id NOT NULL
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_schemas_unique_web_user_name'
            ) THEN
                CREATE UNIQUE INDEX idx_schemas_unique_web_user_name
                    ON schemas (web_user_id, schema_name)
                    WHERE web_user_id IS NOT NULL;
            END IF;

            -- Шаг 5: Индекс по web_user_id для быстрого поиска
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'idx_schemas_web_user_id'
            ) THEN
                CREATE INDEX idx_schemas_web_user_id
                    ON schemas (web_user_id);
            END IF;
        END $$;
        """
    ),
]


async def run_migrations(pool: asyncpg.Pool) -> None:
    """
    Создаёт структуру таблиц и выполняет все миграции.

    Порядок выполнения:
        1. Создание таблиц (CREATE TABLE IF NOT EXISTS)
        2. Последовательное применение миграций

    Каждая операция идемпотентна — безопасно вызывать при каждом
    запуске приложения. Миграции выполняются в одной транзакции.

    Args:
        pool: Пул соединений asyncpg
    """
    async with pool.acquire() as conn:
        # Шаг 1: Создание таблиц
        logger.info("Создание структуры таблиц PostgreSQL...")
        await conn.execute(CREATE_TABLES_SQL)
        logger.info("Структура таблиц создана успешно.")

        # Шаг 2: Применение миграций
        for migration_name, migration_sql in MIGRATIONS:
            try:
                await conn.execute(migration_sql)
                logger.info(
                    "Миграция '%s' выполнена успешно.",
                    migration_name,
                )
            except asyncpg.PostgresError as e:
                logger.error(
                    "Ошибка миграции '%s': %s",
                    migration_name,
                    e,
                )
                raise

    logger.info(
        "Все миграции применены. Всего миграций: %d.",
        len(MIGRATIONS),
    )
