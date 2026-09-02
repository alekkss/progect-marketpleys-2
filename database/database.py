"""
Модуль для работы с базой данных PostgreSQL (asyncpg).

Предоставляет асинхронный класс Database с connection pool
для всех CRUD-операций. Миграции вынесены в отдельный модуль
database/migrations.py.
"""

import json
from datetime import datetime, timezone
from typing import Optional, Dict, List

import asyncpg

from utils.logger_config import setup_logger

logger = setup_logger('database')


class Database:
    """
    Асинхронный класс для работы с PostgreSQL.

    Использует asyncpg connection pool для эффективного управления
    соединениями. Каждый публичный метод получает соединение из пула,
    выполняет запрос и автоматически возвращает соединение обратно.

    Жизненный цикл:
        1. Создание экземпляра: db = Database(database_url)
        2. Инициализация пула: await db.connect()
        3. Использование: await db.add_user(...)
        4. Завершение: await db.close()
    """

    def __init__(
        self,
        database_url: str,
        pool_min_size: int = 2,
        pool_max_size: int = 10,
    ) -> None:
        """
        Инициализация параметров подключения.

        Пул соединений НЕ создаётся в конструкторе — для этого
        нужно вызвать await connect(). Это связано с тем, что
        __init__ не может быть асинхронным.

        Args:
            database_url: Строка подключения PostgreSQL
            pool_min_size: Минимальное количество соединений в пуле
            pool_max_size: Максимальное количество соединений в пуле
        """
        self._database_url: str = database_url
        self._pool_min_size: int = pool_min_size
        self._pool_max_size: int = pool_max_size
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """
        Создаёт connection pool к PostgreSQL.

        Вызывается один раз при старте приложения.
        Если пул уже создан — повторный вызов безопасен.
        """
        if self._pool is not None:
            logger.warning("Пул соединений уже создан, повторная инициализация пропущена.")
            return

        self._pool = await asyncpg.create_pool(
            dsn=self._database_url,
            min_size=self._pool_min_size,
            max_size=self._pool_max_size,
        )
        logger.info(
            "Пул соединений PostgreSQL создан (min=%d, max=%d).",
            self._pool_min_size,
            self._pool_max_size,
        )

    async def close(self) -> None:
        """
        Закрывает connection pool.

        Вызывается при завершении работы приложения.
        Ожидает завершения всех активных запросов.
        """
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Пул соединений PostgreSQL закрыт.")

    @property
    def pool(self) -> asyncpg.Pool:
        """
        Возвращает пул соединений.

        Raises:
            RuntimeError: если пул не инициализирован
        """
        if self._pool is None:
            raise RuntimeError(
                "Пул соединений не инициализирован. "
                "Вызовите await db.connect() перед использованием."
            )
        return self._pool

    # =================================================================
    # Пользователи
    # =================================================================

    async def add_user(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> None:
        """
        Добавляет пользователя или обновляет его данные.

        Использует INSERT ... ON CONFLICT DO UPDATE для атомарного
        upsert. Поля registered_at и total_processings сохраняются
        при обновлении.

        Args:
            user_id: Telegram user_id
            username: Имя пользователя в Telegram
            first_name: Имя
            last_name: Фамилия
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name
                """,
                user_id, username, first_name, last_name,
            )

    async def get_user_stats(self, user_id: int) -> Optional[Dict]:
        """
        Получает статистику пользователя.

        Args:
            user_id: Telegram user_id

        Returns:
            Словарь со статистикой или None если пользователь не найден
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    u.total_processings,
                    u.registered_at,
                    COUNT(CASE WHEN ph.status = 'completed' THEN 1 END) AS successful,
                    COUNT(CASE WHEN ph.status = 'failed' THEN 1 END) AS failed,
                    COALESCE(
                        SUM(CASE WHEN ph.status = 'completed'
                            THEN ph.synced_cells_count ELSE 0 END),
                        0
                    ) AS total_synced
                FROM users u
                LEFT JOIN processing_history ph ON u.user_id = ph.user_id
                WHERE u.user_id = $1
                GROUP BY u.user_id
                """,
                user_id,
            )

        if row is None:
            return None

        return {
            'total_processings': row['total_processings'],
            'registered_at': str(row['registered_at']) if row['registered_at'] else None,
            'successful': row['successful'],
            'failed': row['failed'],
            'total_synced_cells': row['total_synced'],
        }

    async def get_user_history(
        self,
        user_id: int,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Получает историю обработок пользователя.

        Args:
            user_id: Telegram user_id
            limit: Максимальное количество записей

        Returns:
            Список словарей с историей обработок
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, started_at, completed_at,
                    wb_products_count, ozon_products_count,
                    yandex_products_count, synced_cells_count,
                    status, error_message
                FROM processing_history
                WHERE user_id = $1
                ORDER BY started_at DESC
                LIMIT $2
                """,
                user_id, limit,
            )

        history: List[Dict] = []
        for row in rows:
            history.append({
                'id': row['id'],
                'started_at': str(row['started_at']) if row['started_at'] else None,
                'completed_at': str(row['completed_at']) if row['completed_at'] else None,
                'wb_count': row['wb_products_count'],
                'ozon_count': row['ozon_products_count'],
                'yandex_count': row['yandex_products_count'],
                'synced_cells': row['synced_cells_count'],
                'status': row['status'],
                'error': row['error_message'],
            })

        return history

    # =================================================================
    # Обработки (processing)
    # =================================================================

    async def start_processing(self, user_id: int) -> int:
        """
        Начинает новую обработку.

        Args:
            user_id: Telegram user_id

        Returns:
            ID созданной записи обработки
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO processing_history (user_id, started_at, status)
                VALUES ($1, $2, 'processing')
                RETURNING id
                """,
                user_id, datetime.now(timezone.utc),
            )
            return row['id']

    async def complete_processing(
        self,
        processing_id: int,
        wb_count: int,
        ozon_count: int,
        yandex_count: int,
        synced_cells: int,
    ) -> None:
        """
        Завершает обработку успешно.

        Обновляет запись обработки и увеличивает счётчик
        total_processings у пользователя в одной транзакции.

        Args:
            processing_id: ID обработки
            wb_count: Количество товаров WB
            ozon_count: Количество товаров Ozon
            yandex_count: Количество товаров Яндекс
            synced_cells: Количество синхронизированных ячеек
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE processing_history
                    SET completed_at = $1,
                        wb_products_count = $2,
                        ozon_products_count = $3,
                        yandex_products_count = $4,
                        synced_cells_count = $5,
                        status = 'completed'
                    WHERE id = $6
                    """,
                    datetime.now(timezone.utc),
                    wb_count, ozon_count, yandex_count,
                    synced_cells, processing_id,
                )

                await conn.execute(
                    """
                    UPDATE users
                    SET total_processings = total_processings + 1
                    WHERE user_id = (
                        SELECT user_id FROM processing_history WHERE id = $1
                    )
                    """,
                    processing_id,
                )

    async def fail_processing(
        self,
        processing_id: int,
        error_message: str,
    ) -> None:
        """
        Завершает обработку с ошибкой.

        Args:
            processing_id: ID обработки
            error_message: Текст ошибки
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE processing_history
                SET completed_at = $1,
                    status = 'failed',
                    error_message = $2
                WHERE id = $3
                """,
                datetime.now(timezone.utc),
                error_message, processing_id,
            )

    # =================================================================
    # Файлы
    # =================================================================

    async def add_file(
        self,
        user_id: int,
        processing_id: int,
        marketplace: str,
        original_filename: str,
        file_path: str,
    ) -> None:
        """
        Добавляет информацию о загруженном файле.

        Args:
            user_id: Telegram user_id
            processing_id: ID обработки
            marketplace: Название маркетплейса
            original_filename: Оригинальное имя файла
            file_path: Путь к сохранённому файлу
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO files
                    (user_id, processing_id, marketplace,
                     original_filename, file_path)
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_id, processing_id, marketplace,
                original_filename, file_path,
            )

    # =================================================================
    # Схемы сопоставлений
    # =================================================================

    async def create_schema(
        self,
        user_id: int,
        schema_name: str,
        schema_type: str = 'standard',
    ) -> Optional[int]:
        """
        Создаёт новую схему для Telegram-пользователя.

        Args:
            user_id: Telegram user_id
            schema_name: Название схемы
            schema_type: Тип схемы ('standard' или 'mvm')

        Returns:
            ID созданной схемы или None если имя уже занято
        """
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO schemas (user_id, schema_name, schema_type)
                    VALUES ($1, $2, $3)
                    RETURNING id
                    """,
                    user_id, schema_name, schema_type,
                )
                return row['id']
            except asyncpg.UniqueViolationError:
                return None

    async def create_schema_for_web_user(
        self,
        web_user_id: int,
        schema_name: str,
        schema_type: str = 'standard',
        telegram_user_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Создаёт новую схему для веб-пользователя.

        Если у веб-пользователя есть привязанный telegram_user_id —
        записывает оба ID. Если нет — только web_user_id, user_id = NULL.

        Args:
            web_user_id: ID в таблице web_users
            schema_name: Название схемы
            schema_type: Тип схемы ('standard' или 'mvm')
            telegram_user_id: Telegram user_id (если привязан)

        Returns:
            ID созданной схемы или None если имя уже занято
        """
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO schemas (user_id, web_user_id, schema_name, schema_type)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    telegram_user_id, web_user_id, schema_name, schema_type,
                )
                return row['id']
            except asyncpg.UniqueViolationError:
                return None

    async def get_web_user_schemas(
        self,
        web_user_id: int,
    ) -> List[Dict]:
        """
        Получает список схем веб-пользователя.

        Находит схемы по web_user_id ИЛИ по telegram_user_id
        (если у веб-пользователя есть привязка к Telegram).

        Args:
            web_user_id: ID в таблице web_users

        Returns:
            Список словарей с информацией о схемах
        """
        async with self.pool.acquire() as conn:
            # Получаем telegram_user_id привязанный к этому веб-пользователю
            web_user_row = await conn.fetchrow(
                "SELECT telegram_user_id FROM web_users WHERE id = $1",
                web_user_id,
            )

            telegram_user_id = (
                web_user_row['telegram_user_id']
                if web_user_row and web_user_row['telegram_user_id']
                else None
            )

            # Ищем схемы по web_user_id ИЛИ по telegram_user_id
            if telegram_user_id:
                rows = await conn.fetch(
                    """
                    SELECT
                        s.id, s.schema_name, s.created_at, s.updated_at,
                        s.schema_type,
                        (SELECT COUNT(*) FROM schema_matches
                         WHERE schema_id = s.id) AS matches_count
                    FROM schemas s
                    WHERE s.web_user_id = $1 OR s.user_id = $2
                    ORDER BY s.updated_at DESC
                    """,
                    web_user_id, telegram_user_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT
                        s.id, s.schema_name, s.created_at, s.updated_at,
                        s.schema_type,
                        (SELECT COUNT(*) FROM schema_matches
                         WHERE schema_id = s.id) AS matches_count
                    FROM schemas s
                    WHERE s.web_user_id = $1
                    ORDER BY s.updated_at DESC
                    """,
                    web_user_id,
                )

        schemas: List[Dict] = []
        for row in rows:
            schemas.append({
                'id': row['id'],
                'name': row['schema_name'],
                'created_at': str(row['created_at']) if row['created_at'] else None,
                'updated_at': str(row['updated_at']) if row['updated_at'] else None,
                'matches_count': row['matches_count'],
                'schema_type': row['schema_type'] or 'standard',
            })
        return schemas

    async def get_schema_by_name_for_web_user(
        self,
        web_user_id: int,
        schema_name: str,
    ) -> Optional[Dict]:
        """
        Проверяет наличие схемы с данным именем у веб-пользователя.

        Учитывает и привязанный telegram_user_id.

        Args:
            web_user_id: ID в таблице web_users
            schema_name: Название схемы

        Returns:
            Словарь с информацией о схеме или None
        """
        async with self.pool.acquire() as conn:
            # Получаем telegram_user_id
            web_user_row = await conn.fetchrow(
                "SELECT telegram_user_id FROM web_users WHERE id = $1",
                web_user_id,
            )

            telegram_user_id = (
                web_user_row['telegram_user_id']
                if web_user_row and web_user_row['telegram_user_id']
                else None
            )

            if telegram_user_id:
                row = await conn.fetchrow(
                    """
                    SELECT id, schema_name, schema_type, created_at, updated_at
                    FROM schemas
                    WHERE schema_name = $1
                      AND (web_user_id = $2 OR user_id = $3)
                    LIMIT 1
                    """,
                    schema_name, web_user_id, telegram_user_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT id, schema_name, schema_type, created_at, updated_at
                    FROM schemas
                    WHERE schema_name = $1 AND web_user_id = $2
                    LIMIT 1
                    """,
                    schema_name, web_user_id,
                )

        if row is None:
            return None

        return {
            'id': row['id'],
            'name': row['schema_name'],
            'schema_type': row['schema_type'] or 'standard',
            'created_at': str(row['created_at']) if row['created_at'] else None,
            'updated_at': str(row['updated_at']) if row['updated_at'] else None,
        }

    async def get_user_schemas(
        self,
        user_id: int,
        all_schemas: bool = False,
    ) -> List[Dict]:
        """
        Получает список схем пользователя.

        Args:
            user_id: ID пользователя (для фильтрации СВОИХ схем)
            all_schemas: Если True — все схемы (для владельца/админа/редактора).
                         Проверка прав должна быть сделана ДО вызова!

        Returns:
            Список словарей с информацией о схемах
        """
        async with self.pool.acquire() as conn:
            if all_schemas:
                rows = await conn.fetch(
                    """
                    SELECT
                        s.id, s.schema_name, s.created_at, s.updated_at,
                        s.user_id, s.web_user_id,
                        u.username, u.first_name,
                        wu.display_name AS web_display_name,
                        wu.email AS web_email,
                        (SELECT COUNT(*) FROM schema_matches
                         WHERE schema_id = s.id) AS matches_count,
                        s.schema_type
                    FROM schemas s
                    LEFT JOIN users u ON s.user_id = u.user_id
                    LEFT JOIN web_users wu ON s.web_user_id = wu.id
                    ORDER BY s.updated_at DESC
                    """,
                )

                schemas: List[Dict] = []
                for row in rows:
                    # Определяем отображаемое имя владельца
                    if row['first_name']:
                        owner_display = row['first_name']
                    elif row['web_display_name']:
                        owner_display = row['web_display_name']
                    elif row['web_email']:
                        owner_display = row['web_email']
                    elif row['user_id']:
                        owner_display = f"TG ID: {row['user_id']}"
                    elif row['web_user_id']:
                        owner_display = f"Web ID: {row['web_user_id']}"
                    else:
                        owner_display = "Неизвестен"

                    schemas.append({
                        'id': row['id'],
                        'name': row['schema_name'],
                        'created_at': str(row['created_at']) if row['created_at'] else None,
                        'updated_at': str(row['updated_at']) if row['updated_at'] else None,
                        'owner_id': row['user_id'],
                        'web_user_id': row['web_user_id'],
                        'owner_name': owner_display,
                        'matches_count': row['matches_count'],
                        'schema_type': row['schema_type'] or 'standard',
                    })
                return schemas

            else:
                rows = await conn.fetch(
                    """
                    SELECT
                        id, schema_name, created_at, updated_at,
                        (SELECT COUNT(*) FROM schema_matches
                         WHERE schema_id = schemas.id) AS matches_count,
                        schema_type
                    FROM schemas
                    WHERE user_id = $1
                    ORDER BY updated_at DESC
                    """,
                    user_id,
                )

                schemas = []
                for row in rows:
                    schemas.append({
                        'id': row['id'],
                        'name': row['schema_name'],
                        'created_at': str(row['created_at']) if row['created_at'] else None,
                        'updated_at': str(row['updated_at']) if row['updated_at'] else None,
                        'matches_count': row['matches_count'],
                        'schema_type': row['schema_type'] or 'standard',
                    })
                return schemas

    async def get_schema(
        self,
        user_id: int,
        schema_name: str,
    ) -> Optional[Dict]:
        """
        Получает схему по имени для конкретного пользователя.

        Args:
            user_id: Telegram user_id
            schema_name: Название схемы

        Returns:
            Словарь с информацией о схеме или None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, schema_name, created_at, updated_at, schema_type
                FROM schemas
                WHERE user_id = $1 AND schema_name = $2
                """,
                user_id, schema_name,
            )

        if row is None:
            return None

        return {
            'id': row['id'],
            'name': row['schema_name'],
            'created_at': str(row['created_at']) if row['created_at'] else None,
            'updated_at': str(row['updated_at']) if row['updated_at'] else None,
            'schema_type': row['schema_type'] or 'standard',
        }

    async def get_schema_by_name_global(
        self,
        schema_name: str,
    ) -> Optional[Dict]:
        """
        Получает схему по имени среди всех пользователей (глобальный поиск).

        Используется владельцем, администратором и редактором для работы
        с чужими схемами. Проверка прав доступа должна быть выполнена
        ДО вызова этого метода.

        Args:
            schema_name: Название схемы для поиска

        Returns:
            Словарь с информацией о схеме или None если не найдена
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, web_user_id, schema_name, schema_type,
                       created_at, updated_at
                FROM schemas
                WHERE schema_name = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                schema_name,
            )

        if row is None:
            return None

        return {
            'id': row['id'],
            'owner_id': row['user_id'],
            'web_user_id': row['web_user_id'],
            'name': row['schema_name'],
            'schema_type': row['schema_type'] or 'standard',
            'created_at': str(row['created_at']) if row['created_at'] else None,
            'updated_at': str(row['updated_at']) if row['updated_at'] else None,
        }

    async def delete_schema(
        self,
        user_id: int,
        schema_name: str,
    ) -> bool:
        """
        Удаляет схему пользователя.

        Args:
            user_id: Telegram user_id
            schema_name: Название схемы

        Returns:
            True если схема была удалена
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM schemas
                WHERE user_id = $1 AND schema_name = $2
                """,
                user_id, schema_name,
            )
            return result == 'DELETE 1'

    async def get_schema_type(self, schema_id: int) -> str:
        """
        Получает тип схемы по её ID.

        Args:
            schema_id: ID схемы

        Returns:
            'standard' или 'mvm'
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT schema_type FROM schemas WHERE id = $1",
                schema_id,
            )

        if row and row['schema_type']:
            return row['schema_type']
        return 'standard'

    # =================================================================
    # Сопоставления схем (matches)
    # =================================================================

    async def save_schema_matches(
        self,
        schema_id: int,
        comparison_result: Dict,
    ) -> None:
        """
        Сохраняет все совпадения (>= 85%) и полный JSON результата.

        Выполняется в одной транзакции: удаление старых записей,
        вставка новых, обновление JSON в таблице schemas.

        Args:
            schema_id: ID схемы
            comparison_result: Полный результат AI-сопоставления
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Удаляем старые записи
                await conn.execute(
                    "DELETE FROM schema_matches WHERE schema_id = $1",
                    schema_id,
                )

                saved_count = 0
                skipped_count = 0

                # Сохраняем matches_all_three в legacy-таблицу
                for match in comparison_result.get('matches_all_three', []):
                    confidence = match.get('confidence', 0)
                    if confidence >= 0.85:
                        await conn.execute(
                            """
                            INSERT INTO schema_matches
                                (schema_id, wb_column, ozon_column,
                                 yandex_column, confidence, is_mandatory)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            """,
                            schema_id,
                            match.get('column_1'),
                            match.get('column_2'),
                            match.get('column_3'),
                            confidence,
                            match.get('mandatory', False),
                        )
                        saved_count += 1
                    else:
                        skipped_count += 1

                # Сохраняем полный JSON в schemas
                full_json = json.dumps(
                    comparison_result, ensure_ascii=False,
                )
                await conn.execute(
                    """
                    UPDATE schemas
                    SET full_comparison_json = $1,
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    full_json, schema_id,
                )

        logger.info(
            "Схема %d: сохранено совпадений: %d, пропущено (confidence < 85%%): %d",
            schema_id, saved_count, skipped_count,
        )

    async def get_schema_matches(self, schema_id: int) -> Dict:
        """
        Получает совпадения для схемы.

        Сначала пробует загрузить полный JSON (новый формат),
        при неудаче — fallback на legacy-таблицу schema_matches.

        Args:
            schema_id: ID схемы

        Returns:
            Словарь с группами сопоставлений
        """
        async with self.pool.acquire() as conn:
            # Пробуем загрузить полный JSON
            row = await conn.fetchrow(
                "SELECT full_comparison_json FROM schemas WHERE id = $1",
                schema_id,
            )

            if row and row['full_comparison_json']:
                try:
                    return json.loads(row['full_comparison_json'])
                except json.JSONDecodeError:
                    logger.error(
                        "Ошибка парсинга JSON для схемы %d",
                        schema_id,
                    )

            # Fallback: legacy-таблица schema_matches
            rows = await conn.fetch(
                """
                SELECT wb_column, ozon_column, yandex_column,
                       confidence, is_mandatory
                FROM schema_matches
                WHERE schema_id = $1
                """,
                schema_id,
            )

        matches: List[Dict] = []
        for r in rows:
            matches.append({
                'column_1': r['wb_column'],
                'column_2': r['ozon_column'],
                'column_3': r['yandex_column'],
                'confidence': r['confidence'],
                'mandatory': r['is_mandatory'],
            })

        return {
            'matches_all_three': matches,
            'matches_1_2': [],
            'matches_1_3': [],
            'matches_2_3': [],
            'only_in_first': [],
            'only_in_second': [],
            'only_in_third': [],
        }

    async def get_schema_category_ids(self, schema_id: int) -> list:
        """
        Извлекает список выбранных category_id из JSON схемы.

        Args:
            schema_id: ID схемы

        Returns:
            Список строковых ID категорий, например ['16530', '16531']
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT full_comparison_json FROM schemas WHERE id = $1",
                schema_id,
            )

        if not row or not row['full_comparison_json']:
            return []

        try:
            data = json.loads(row['full_comparison_json'])
            category_ids = data.get('selected_category_ids', [])
            if isinstance(category_ids, list):
                return category_ids
            return []
        except (json.JSONDecodeError, TypeError):
            return []

    async def update_schema_matches(
        self,
        schema_id: int,
        new_comparison_result: Dict,
    ) -> int:
        """
        Обновляет схему, добавляя новые совпадения.

        Args:
            schema_id: ID схемы
            new_comparison_result: Новый результат AI-сопоставления

        Returns:
            Количество добавленных совпадений
        """
        existing_matches = await self.get_schema_matches(schema_id)

        existing_set: set = set()
        for match in existing_matches.get('matches_all_three', []):
            key = (match['column_1'], match['column_2'], match['column_3'])
            existing_set.add(key)

        new_count = 0
        for match in new_comparison_result.get('matches_all_three', []):
            key = (
                match.get('column_1'),
                match.get('column_2'),
                match.get('column_3'),
            )
            if key not in existing_set:
                existing_matches['matches_all_three'].append(match)
                new_count += 1

        if new_count > 0:
            await self.save_schema_matches(schema_id, existing_matches)

        return new_count

    # =================================================================
    # Системные настройки (admin)
    # =================================================================

    async def get_admin_user_id(self) -> Optional[int]:
        """
        Получает ID администратора из БД.

        Returns:
            Telegram user_id администратора или None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT setting_value
                FROM system_settings
                WHERE setting_key = 'admin_user_id'
                """,
            )

        if row and row['setting_value']:
            try:
                return int(row['setting_value'])
            except (TypeError, ValueError):
                return None
        return None

    async def set_admin_user_id(
        self,
        admin_id: int,
        updated_by: int,
    ) -> bool:
        """
        Устанавливает ID администратора.

        Args:
            admin_id: Telegram user_id нового администратора
            updated_by: Telegram user_id владельца

        Returns:
            True если успешно
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO system_settings
                    (setting_key, setting_value, updated_at, updated_by)
                VALUES ('admin_user_id', $1, NOW(), $2)
                ON CONFLICT (setting_key) DO UPDATE SET
                    setting_value = EXCLUDED.setting_value,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
                """,
                str(admin_id), updated_by,
            )
        return True

    async def remove_admin_user_id(self, updated_by: int) -> bool:
        """
        Удаляет администратора (сброс).

        Args:
            updated_by: Telegram user_id владельца

        Returns:
            True если запись была удалена
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM system_settings
                WHERE setting_key = 'admin_user_id'
                """,
            )
            return result == 'DELETE 1'

    # =================================================================
    # Белый список (whitelist)
    # =================================================================

    async def get_whitelist_users(self) -> List[int]:
        """
        Получает список ID пользователей из белого списка.

        Returns:
            Список Telegram user_id
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id
                FROM whitelist_users
                ORDER BY added_at ASC
                """,
            )
        return [row['user_id'] for row in rows]

    async def get_whitelist_details(self) -> List[Dict]:
        """
        Получает детальную информацию о пользователях в whitelist.

        Returns:
            Список словарей с информацией о каждом пользователе
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, role, added_at, added_by, notes
                FROM whitelist_users
                ORDER BY
                    CASE role
                        WHEN 'editor' THEN 1
                        WHEN 'user' THEN 2
                    END,
                    added_at ASC
                """,
            )

        result: List[Dict] = []
        for row in rows:
            result.append({
                'user_id': row['user_id'],
                'role': row['role'],
                'added_at': str(row['added_at']) if row['added_at'] else None,
                'added_by': row['added_by'],
                'notes': row['notes'],
            })
        return result

    async def add_whitelist_user(
        self,
        user_id: int,
        added_by: int,
        role: str = 'user',
        notes: Optional[str] = None,
    ) -> bool:
        """
        Добавляет пользователя в whitelist с указанием роли.

        Args:
            user_id: Telegram ID пользователя
            added_by: Telegram ID того, кто добавляет
            role: Роль ('editor' или 'user')
            notes: Опциональная заметка

        Returns:
            True если успешно, False если дубликат или невалидная роль
        """
        if role not in ('editor', 'user'):
            return False

        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO whitelist_users
                        (user_id, role, added_by, notes)
                    VALUES ($1, $2, $3, $4)
                    """,
                    user_id, role, added_by, notes,
                )
                return True
            except asyncpg.UniqueViolationError:
                return False

    async def remove_whitelist_user(self, user_id: int) -> bool:
        """
        Удаляет пользователя из белого списка.

        Args:
            user_id: Telegram user_id удаляемого пользователя

        Returns:
            True если успешно удалён
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM whitelist_users WHERE user_id = $1",
                user_id,
            )
            return result == 'DELETE 1'

    async def get_whitelist_count(self) -> int:
        """
        Возвращает текущее количество пользователей в белом списке.

        Returns:
            Количество пользователей
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM whitelist_users",
            )
        return row['cnt']

    async def get_user_role(self, user_id: int) -> Optional[str]:
        """
        Получает роль пользователя из whitelist.

        Args:
            user_id: Telegram ID пользователя

        Returns:
            'editor', 'user' или None если не в whitelist
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM whitelist_users WHERE user_id = $1",
                user_id,
            )
        return row['role'] if row else None

    async def get_whitelist_slots_info(self) -> Dict:
        """
        Возвращает информацию о количестве пользователей по ролям.

        Returns:
            Словарь с количеством пользователей по ролям
        """
        async with self.pool.acquire() as conn:
            editor_row = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM whitelist_users WHERE role = 'editor'",
            )
            user_row = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM whitelist_users WHERE role = 'user'",
            )

        editor_count = editor_row['cnt']
        user_count = user_row['cnt']

        return {
            'editor': {'used': editor_count},
            'user': {'used': user_count},
            'total_used': editor_count + user_count,
        }

    # =================================================================
    # Веб-пользователи (web_users)
    # =================================================================

    async def create_web_user(
        self,
        email: str,
        password_hash: str,
        display_name: Optional[str] = None,
        role: str = 'user',
    ) -> Optional[int]:
        """
        Создаёт нового веб-пользователя.

        Args:
            email: Email (уникальный, используется для входа)
            password_hash: Хеш пароля (bcrypt)
            display_name: Отображаемое имя
            role: Роль ('owner', 'admin', 'editor', 'user')

        Returns:
            ID созданного пользователя или None при дубликате email
        """
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO web_users (email, password_hash, display_name, role)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    email, password_hash, display_name, role,
                )
                return row['id']
            except asyncpg.UniqueViolationError:
                return None

    async def get_web_user_by_email(self, email: str) -> Optional[Dict]:
        """
        Получает веб-пользователя по email.

        Args:
            email: Email для поиска

        Returns:
            Словарь с данными пользователя или None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, email, password_hash, display_name,
                       telegram_user_id, role, is_active,
                       created_at, last_login_at
                FROM web_users
                WHERE email = $1
                """,
                email,
            )

        if row is None:
            return None

        return {
            'id': row['id'],
            'email': row['email'],
            'password_hash': row['password_hash'],
            'display_name': row['display_name'],
            'telegram_user_id': row['telegram_user_id'],
            'role': row['role'],
            'is_active': row['is_active'],
            'created_at': str(row['created_at']) if row['created_at'] else None,
            'last_login_at': str(row['last_login_at']) if row['last_login_at'] else None,
        }

    async def get_web_user_by_id(self, user_id: int) -> Optional[Dict]:
        """
        Получает веб-пользователя по ID.

        Args:
            user_id: ID пользователя в таблице web_users

        Returns:
            Словарь с данными пользователя или None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, email, password_hash, display_name,
                       telegram_user_id, role, is_active,
                       created_at, last_login_at
                FROM web_users
                WHERE id = $1
                """,
                user_id,
            )

        if row is None:
            return None

        return {
            'id': row['id'],
            'email': row['email'],
            'password_hash': row['password_hash'],
            'display_name': row['display_name'],
            'telegram_user_id': row['telegram_user_id'],
            'role': row['role'],
            'is_active': row['is_active'],
            'created_at': str(row['created_at']) if row['created_at'] else None,
            'last_login_at': str(row['last_login_at']) if row['last_login_at'] else None,
        }

    async def update_web_user_last_login(self, user_id: int) -> None:
        """
        Обновляет время последнего входа.

        Args:
            user_id: ID веб-пользователя
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE web_users
                SET last_login_at = $1
                WHERE id = $2
                """,
                datetime.now(timezone.utc), user_id,
            )

    async def link_telegram_to_web_user(
        self,
        web_user_id: int,
        telegram_user_id: int,
    ) -> bool:
        """
        Привязывает Telegram-аккаунт к веб-пользователю.

        Args:
            web_user_id: ID в таблице web_users
            telegram_user_id: Telegram user_id

        Returns:
            True если привязка успешна, False если telegram_user_id уже занят
        """
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    UPDATE web_users
                    SET telegram_user_id = $1
                    WHERE id = $2
                    """,
                    telegram_user_id, web_user_id,
                )
                return True
            except asyncpg.UniqueViolationError:
                return False

    async def get_web_users_list(self, limit: int = 50) -> List[Dict]:
        """
        Получает список всех веб-пользователей.

        Args:
            limit: Максимальное количество записей

        Returns:
            Список словарей с информацией о пользователях
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, email, display_name, telegram_user_id,
                       role, is_active, created_at, last_login_at
                FROM web_users
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )

        result: List[Dict] = []
        for row in rows:
            result.append({
                'id': row['id'],
                'email': row['email'],
                'display_name': row['display_name'],
                'telegram_user_id': row['telegram_user_id'],
                'role': row['role'],
                'is_active': row['is_active'],
                'created_at': str(row['created_at']) if row['created_at'] else None,
                'last_login_at': str(row['last_login_at']) if row['last_login_at'] else None,
            })
        return result

    async def set_web_user_active(self, user_id: int, is_active: bool) -> None:
        """
        Активирует или деактивирует веб-пользователя.

        Args:
            user_id: ID веб-пользователя
            is_active: True для активации, False для блокировки
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE web_users SET is_active = $1 WHERE id = $2",
                is_active, user_id,
            )

    # =================================================================
    # Веб-сессии (web_sessions)
    # =================================================================

    async def create_web_session(
        self,
        session_id: str,
        web_user_id: int,
        expires_at: datetime,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """
        Создаёт новую веб-сессию.

        Args:
            session_id: UUID сессии (устанавливается в cookie)
            web_user_id: ID веб-пользователя
            expires_at: Время истечения сессии
            ip_address: IP-адрес клиента
            user_agent: User-Agent браузера
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO web_sessions
                    (id, web_user_id, expires_at, ip_address, user_agent)
                VALUES ($1, $2, $3, $4, $5)
                """,
                session_id, web_user_id, expires_at,
                ip_address, user_agent,
            )

    async def get_web_session(self, session_id: str) -> Optional[Dict]:
        """
        Получает данные сессии по ID.

        Возвращает None если сессия не найдена или истекла.

        Args:
            session_id: UUID сессии из cookie

        Returns:
            Словарь с данными сессии или None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ws.id, ws.web_user_id, ws.expires_at,
                       wu.email, wu.display_name, wu.role,
                       wu.is_active, wu.telegram_user_id
                FROM web_sessions ws
                JOIN web_users wu ON ws.web_user_id = wu.id
                WHERE ws.id = $1 AND ws.expires_at > NOW()
                """,
                session_id,
            )

        if row is None:
            return None

        return {
            'session_id': row['id'],
            'web_user_id': row['web_user_id'],
            'expires_at': str(row['expires_at']),
            'email': row['email'],
            'display_name': row['display_name'],
            'role': row['role'],
            'is_active': row['is_active'],
            'telegram_user_id': row['telegram_user_id'],
        }

    async def delete_web_session(self, session_id: str) -> None:
        """
        Удаляет сессию (logout).

        Args:
            session_id: UUID сессии
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM web_sessions WHERE id = $1",
                session_id,
            )

    async def delete_user_sessions(self, web_user_id: int) -> None:
        """
        Удаляет все сессии пользователя (принудительный logout).

        Args:
            web_user_id: ID веб-пользователя
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM web_sessions WHERE web_user_id = $1",
                web_user_id,
            )

    async def cleanup_expired_web_sessions(self) -> int:
        """
        Удаляет все истёкшие сессии.

        Вызывается периодически (например, раз в час) для очистки БД.

        Returns:
            Количество удалённых сессий
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM web_sessions WHERE expires_at <= NOW()",
            )
            # result = 'DELETE N'
            try:
                return int(result.split()[-1])
            except (IndexError, ValueError):
                return 0

    # =================================================================
    # Результаты задач (task_results) — для веб-скачивания
    # =================================================================

    async def create_task_result(
        self,
        task_id: str,
        web_user_id: int,
    ) -> None:
        """
        Создаёт запись результата задачи (статус pending).

        Вызывается при постановке веб-задачи в очередь.

        Args:
            task_id: UUID задачи
            web_user_id: ID веб-пользователя (владелец результатов)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO task_results (task_id, web_user_id, status)
                VALUES ($1, $2, 'pending')
                ON CONFLICT (task_id) DO NOTHING
                """,
                task_id, web_user_id,
            )

    async def update_task_result(
        self,
        task_id: str,
        status: Optional[str] = None,
        output_files: Optional[Dict] = None,
        report_path: Optional[str] = None,
        stats: Optional[Dict] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Обновляет результат задачи.

        Вызывается из WebDelivery при завершении обработки.
        Обновляет только переданные поля (не-None).

        Args:
            task_id: UUID задачи
            status: Новый статус ('processing', 'completed', 'failed')
            output_files: Словарь {filename: path} с результатами
            report_path: Путь к файлу отчёта
            stats: Статистика обработки (JSONB)
            error_message: Сообщение об ошибке
        """
        fields: List[str] = []
        values: List = []
        param_idx = 1

        if status is not None:
            fields.append(f"status = ${param_idx}")
            values.append(status)
            param_idx += 1

        if output_files is not None:
            fields.append(f"output_files = ${param_idx}")
            values.append(json.dumps(output_files, ensure_ascii=False))
            param_idx += 1

        if report_path is not None:
            fields.append(f"report_path = ${param_idx}")
            values.append(report_path)
            param_idx += 1

        if stats is not None:
            fields.append(f"stats = ${param_idx}")
            values.append(json.dumps(stats, ensure_ascii=False))
            param_idx += 1

        if error_message is not None:
            fields.append(f"error_message = ${param_idx}")
            values.append(error_message)
            param_idx += 1

        if status == 'completed' or status == 'failed':
            fields.append(f"completed_at = ${param_idx}")
            values.append(datetime.now(timezone.utc))
            param_idx += 1

        if not fields:
            return

        values.append(task_id)
        query = f"""
            UPDATE task_results
            SET {', '.join(fields)}
            WHERE task_id = ${param_idx}
        """

        async with self.pool.acquire() as conn:
            await conn.execute(query, *values)

    async def get_task_result(self, task_id: str) -> Optional[Dict]:
        """
        Получает результат задачи по ID.

        Args:
            task_id: UUID задачи

        Returns:
            Словарь с данными результата или None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, task_id, web_user_id, status,
                       output_files, report_path, stats,
                       created_at, completed_at, error_message
                FROM task_results
                WHERE task_id = $1
                """,
                task_id,
            )

        if row is None:
            return None

        output_files = None
        if row['output_files']:
            try:
                output_files = json.loads(row['output_files']) if isinstance(
                    row['output_files'], str
                ) else row['output_files']
            except (json.JSONDecodeError, TypeError):
                output_files = None

        stats = None
        if row['stats']:
            try:
                stats = json.loads(row['stats']) if isinstance(
                    row['stats'], str
                ) else row['stats']
            except (json.JSONDecodeError, TypeError):
                stats = None

        return {
            'id': row['id'],
            'task_id': row['task_id'],
            'web_user_id': row['web_user_id'],
            'status': row['status'],
            'output_files': output_files,
            'report_path': row['report_path'],
            'stats': stats,
            'created_at': str(row['created_at']) if row['created_at'] else None,
            'completed_at': str(row['completed_at']) if row['completed_at'] else None,
            'error_message': row['error_message'],
        }

    async def get_user_task_results(
        self,
        web_user_id: int,
        limit: int = 20,
    ) -> List[Dict]:
        """
        Получает список результатов задач пользователя.

        Args:
            web_user_id: ID веб-пользователя
            limit: Максимальное количество записей

        Returns:
            Список словарей с результатами (от новых к старым)
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task_id, status, output_files, stats,
                       created_at, completed_at, error_message
                FROM task_results
                WHERE web_user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                web_user_id, limit,
            )

        results: List[Dict] = []
        for row in rows:
            output_files = None
            if row['output_files']:
                try:
                    output_files = json.loads(row['output_files']) if isinstance(
                        row['output_files'], str
                    ) else row['output_files']
                except (json.JSONDecodeError, TypeError):
                    output_files = None

            stats = None
            if row['stats']:
                try:
                    stats = json.loads(row['stats']) if isinstance(
                        row['stats'], str
                    ) else row['stats']
                except (json.JSONDecodeError, TypeError):
                    stats = None

            results.append({
                'task_id': row['task_id'],
                'status': row['status'],
                'output_files': output_files,
                'stats': stats,
                'created_at': str(row['created_at']) if row['created_at'] else None,
                'completed_at': str(row['completed_at']) if row['completed_at'] else None,
                'error_message': row['error_message'],
            })

        return results

    # =================================================================
    # Задания AI-агента маппинга PIM+FDM (mapping_jobs)
    # =================================================================
    # Таблица совмещает очередь заданий (claim_pending_mapping_job)
    # и историю/статистику для дашборда оператора.
    #
    # Жизненный цикл (п. 4.2 доработок):
    #     pending → processing → completed | failed
    #
    # Длительность (duration_sec) вычисляется на стороне SQL из
    # started_at и completed_at — единый источник истины, воркер
    # не измеряет время сам и не может разойтись с БД.
    # =================================================================

    @staticmethod
    def _parse_jsonb_field(value: object) -> Optional[object]:
        """
        Разбирает значение JSONB-столбца в Python-объект.

        asyncpg без кастомных кодеков возвращает JSONB строкой;
        при установленных кодеках — уже разобранным объектом.
        Метод безопасно обрабатывает оба случая.

        Args:
            value: Сырое значение столбца

        Returns:
            Разобранный объект (dict/list) или None
        """
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return None
        return value

    async def create_mapping_job(
        self,
        job_id: str,
        task_type: str,
        schema_id: int,
        payload: Dict,
        channels: List[Dict],
        category_name: Optional[str] = None,
        attribute_name: Optional[str] = None,
    ) -> None:
        """
        Создаёт задание в статусе pending.

        Вызывается маршрутом POST /v1/mapping-tasks после успешной
        валидации. Каналы и человекочитаемые названия сохраняются
        отдельными столбцами — дашборд читает их без парсинга payload.

        Args:
            job_id: Идентификатор задания (генерирует API-слой)
            task_type: 'attribute_mapping' или 'reference_value_mapping'
            schema_id: Идентификатор схемы FDM
            payload: Полный исходный JSON запроса
            channels: Список [{platform, name, schemaChannelId}]
            category_name: Название категории (для дашборда)
            attribute_name: Название атрибута (для дашборда)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mapping_jobs
                    (id, task_type, schema_id, payload, channels,
                     category_name, attribute_name, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
                """,
                job_id, task_type, schema_id,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(channels, ensure_ascii=False),
                category_name, attribute_name,
            )
        logger.info(
            "Задание агента создано: id=%s, тип=%s, схема=%s",
            job_id, task_type, schema_id,
        )

    async def claim_pending_mapping_job(self) -> Optional[Dict]:
        """
        Атомарно забирает старейшее pending-задание в обработку.

        Переводит статус pending → processing и фиксирует started_at
        одним UPDATE с подзапросом FOR UPDATE SKIP LOCKED:
        строка блокируется только для текущей транзакции, конкурентные
        выборки не ждут друг друга и не забирают одну строку дважды.

        Вызывается MappingJobWorker.

        Returns:
            Словарь {job_id, task_type, schema_id, payload}
            (payload — разобранный JSON) или None, если очередь пуста
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE mapping_jobs
                SET status = 'processing',
                    started_at = NOW()
                WHERE id = (
                    SELECT id
                    FROM mapping_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, task_type, schema_id, payload
                """
            )

        if row is None:
            return None

        payload = self._parse_jsonb_field(row['payload'])
        return {
            'job_id': row['id'],
            'task_type': row['task_type'],
            'schema_id': row['schema_id'],
            'payload': payload if isinstance(payload, dict) else {},
        }

    async def mark_mapping_job_completed(
        self,
        job_id: str,
        result: Dict,
        matched_count: Optional[int] = None,
        unresolved_count: Optional[int] = None,
    ) -> None:
        """
        Завершает задание успешно.

        Длительность вычисляется в SQL: completed_at − started_at.

        Args:
            job_id: Идентификатор задания
            result: Итоговый JSON по протоколу (to_dict() результата)
            matched_count: Число сопоставленных связок/значений
            unresolved_count: Число несопоставленных (unresolved/null)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE mapping_jobs
                SET status = 'completed',
                    result = $2,
                    matched_count = $3,
                    unresolved_count = $4,
                    completed_at = NOW(),
                    duration_sec = EXTRACT(EPOCH FROM (NOW() - started_at))
                WHERE id = $1
                """,
                job_id,
                json.dumps(result, ensure_ascii=False),
                matched_count,
                unresolved_count,
            )
        logger.info("Задание агента %s завершено успешно", job_id)

    async def mark_mapping_job_failed(
        self,
        job_id: str,
        error_message: str,
    ) -> None:
        """
        Завершает задание с ошибкой.

        Args:
            job_id: Идентификатор задания
            error_message: Текст ошибки (возвращается FDM в поле error)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE mapping_jobs
                SET status = 'failed',
                    error_message = $2,
                    completed_at = NOW(),
                    duration_sec = EXTRACT(EPOCH FROM (NOW() - started_at))
                WHERE id = $1
                """,
                job_id, error_message,
            )
        logger.error("Задание агента %s завершено с ошибкой: %s", job_id, error_message)

    async def get_mapping_job(self, job_id: str) -> Optional[Dict]:
        """
        Получает задание по ID для GET-поллинга FDM.

        Args:
            job_id: Идентификатор задания

        Returns:
            Словарь с данными задания или None если не найдено.
            result — разобранный JSON (None для pending/processing)
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, task_type, status, result, error_message
                FROM mapping_jobs
                WHERE id = $1
                """,
                job_id,
            )

        if row is None:
            return None

        result = self._parse_jsonb_field(row['result'])
        return {
            'job_id': row['id'],
            'task_type': row['task_type'],
            'status': row['status'],
            'result': result if isinstance(result, dict) else None,
            'error_message': row['error_message'],
        }

    async def get_mapping_jobs_list(
        self,
        search: str = '',
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Получает список заданий для дашборда оператора (п. 6.1-6.2).

        Сортировка — новые сверху. Поиск — по schemaId (как текст),
        названию категории и названию атрибута.

        Args:
            search: Строка поиска (пустая — без фильтра)
            limit: Количество записей на странице
            offset: Смещение страницы (пагинация)

        Returns:
            Список словарей с данными для таблицы дашборда
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, task_type, schema_id, status,
                    category_name, attribute_name,
                    jsonb_array_length(channels) AS channels_count,
                    matched_count, unresolved_count, duration_sec,
                    error_message, created_at, started_at, completed_at
                FROM mapping_jobs
                WHERE ($1 = '' OR
                       schema_id::text LIKE '%' || $1 || '%' OR
                       category_name ILIKE '%' || $1 || '%' OR
                       attribute_name ILIKE '%' || $1 || '%')
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                search, limit, offset,
            )

        jobs: List[Dict] = []
        for row in rows:
            jobs.append({
                'job_id': row['id'],
                'task_type': row['task_type'],
                'schema_id': row['schema_id'],
                'status': row['status'],
                'category_name': row['category_name'],
                'attribute_name': row['attribute_name'],
                'channels_count': row['channels_count'],
                'matched_count': row['matched_count'],
                'unresolved_count': row['unresolved_count'],
                'duration_sec': row['duration_sec'],
                'error_message': row['error_message'],
                'created_at': str(row['created_at']) if row['created_at'] else None,
                'started_at': str(row['started_at']) if row['started_at'] else None,
                'completed_at': str(row['completed_at']) if row['completed_at'] else None,
            })
        return jobs

    async def get_mapping_jobs_count(self, search: str = '') -> int:
        """
        Возвращает количество заданий с учётом поиска — для пагинации.

        Args:
            search: Строка поиска (пустая — без фильтра)

        Returns:
            Общее количество записей по фильтру
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS cnt
                FROM mapping_jobs
                WHERE ($1 = '' OR
                       schema_id::text LIKE '%' || $1 || '%' OR
                       category_name ILIKE '%' || $1 || '%' OR
                       attribute_name ILIKE '%' || $1 || '%')
                """,
                search,
            )
        return row['cnt']

    async def get_mapping_job_detail(self, job_id: str) -> Optional[Dict]:
        """
        Получает полную запись задания для страницы детализации (п. 6.3).

        Включает payload и result целиком — страница показывает
        произведённые связки, уверенность и нераспознанное.

        Args:
            job_id: Идентификатор задания

        Returns:
            Словарь со всеми полями задания или None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id, task_type, schema_id, status, payload, result,
                    channels, category_name, attribute_name,
                    matched_count, unresolved_count, duration_sec,
                    error_message, created_at, started_at, completed_at
                FROM mapping_jobs
                WHERE id = $1
                """,
                job_id,
            )

        if row is None:
            return None

        payload = self._parse_jsonb_field(row['payload'])
        result = self._parse_jsonb_field(row['result'])
        channels = self._parse_jsonb_field(row['channels'])

        return {
            'job_id': row['id'],
            'task_type': row['task_type'],
            'schema_id': row['schema_id'],
            'status': row['status'],
            'payload': payload if isinstance(payload, dict) else {},
            'result': result if isinstance(result, dict) else None,
            'channels': channels if isinstance(channels, list) else [],
            'category_name': row['category_name'],
            'attribute_name': row['attribute_name'],
            'matched_count': row['matched_count'],
            'unresolved_count': row['unresolved_count'],
            'duration_sec': row['duration_sec'],
            'error_message': row['error_message'],
            'created_at': str(row['created_at']) if row['created_at'] else None,
            'started_at': str(row['started_at']) if row['started_at'] else None,
            'completed_at': str(row['completed_at']) if row['completed_at'] else None,
        }

    async def recover_stale_mapping_jobs(self, stale_seconds: int = 0) -> int:
        """
        Помечает failed задания, застрявшие в processing.

        Вызывается MappingJobWorker при старте: единственный воркер
        процесса не может иметь активных processing-заданий до запуска,
        поэтому все такие записи — следы падения предыдущего процесса
        (graceful degradation: FDM получит failed вместо вечного поллинга).

        Args:
            stale_seconds: Порог зависания в секундах
                (0 — считать зависшими все processing-задания)

        Returns:
            Количество переведённых в failed заданий
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE mapping_jobs
                SET status = 'failed',
                    error_message = 'Воркер агента перезапущен во время '
                                    'обработки — задание прервано',
                    completed_at = NOW()
                WHERE status = 'processing'
                  AND started_at < NOW() - make_interval(secs => $1)
                """,
                stale_seconds,
            )
            try:
                count = int(result.split()[-1])
            except (IndexError, ValueError):
                count = 0

        if count > 0:
            logger.warning(
                "Восстановление заданий агента: %d зависших переведены в failed",
                count,
            )
        return count

    async def cleanup_old_mapping_jobs(self, retention_days: int) -> int:
        """
        Удаляет завершённые задания старше указанного срока.

        Вызывается циклом обслуживания воркера агента.
        Активные задания (pending/processing) не удаляются никогда.

        Args:
            retention_days: Срок хранения заданий в днях

        Returns:
            Количество удалённых записей
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM mapping_jobs
                WHERE status IN ('completed', 'failed')
                  AND created_at < NOW() - make_interval(days => $1)
                """,
                retention_days,
            )
            try:
                count = int(result.split()[-1])
            except (IndexError, ValueError):
                count = 0

        if count > 0:
            logger.info(
                "Очистка заданий агента: удалено %d записей старше %d дн.",
                count, retention_days,
            )
        return count
