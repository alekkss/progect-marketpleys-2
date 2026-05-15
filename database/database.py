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
        Создаёт новую схему.

        Args:
            user_id: ID пользователя
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
                        s.user_id, u.username, u.first_name,
                        (SELECT COUNT(*) FROM schema_matches
                         WHERE schema_id = s.id) AS matches_count,
                        s.schema_type
                    FROM schemas s
                    LEFT JOIN users u ON s.user_id = u.user_id
                    ORDER BY s.updated_at DESC
                    """,
                )

                schemas: List[Dict] = []
                for row in rows:
                    owner_display = (
                        row['first_name']
                        if row['first_name']
                        else f"ID: {row['user_id']}"
                    )
                    schemas.append({
                        'id': row['id'],
                        'name': row['schema_name'],
                        'created_at': str(row['created_at']) if row['created_at'] else None,
                        'updated_at': str(row['updated_at']) if row['updated_at'] else None,
                        'owner_id': row['user_id'],
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
                SELECT id, user_id, schema_name, schema_type,
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
