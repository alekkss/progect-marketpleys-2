"""
Модуль для работы с базой данных SQLite
"""
import sqlite3
from datetime import datetime
from typing import Optional, Dict, List
import json
import os
import sys
from pathlib import Path
from utils.logger_config import setup_logger
sys.path.insert(0, str(Path(__file__).parent.parent))

class Database:

    def migrate_add_role_column(self):
        """
        Миграция: добавляет колонку role в whitelist_users
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # Проверяем, есть ли уже колонка role
            cursor.execute("PRAGMA table_info(whitelist_users)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'role' not in columns:
                print("[MIGRATION] Добавление колонки 'role' в таблицу whitelist_users...")
                
                # Добавляем колонку role
                cursor.execute("""
                    ALTER TABLE whitelist_users 
                    ADD COLUMN role TEXT NOT NULL DEFAULT 'user'
                """)
                
                # Добавляем CHECK constraint (SQLite не поддерживает ADD CONSTRAINT, 
                # поэтому пересоздаём таблицу)
                cursor.execute("""
                    CREATE TABLE whitelist_users_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER UNIQUE NOT NULL,
                        role TEXT NOT NULL DEFAULT 'user',
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        added_by INTEGER,
                        notes TEXT,
                        CHECK(role IN ('editor', 'user'))
                    )
                """)
                
                # Копируем данные из старой таблицы
                cursor.execute("""
                    INSERT INTO whitelist_users_new (id, user_id, role, added_at, added_by, notes)
                    SELECT id, user_id, 'user', added_at, added_by, notes
                    FROM whitelist_users
                """)
                
                # Удаляем старую таблицу
                cursor.execute("DROP TABLE whitelist_users")
                
                # Переименовываем новую
                cursor.execute("ALTER TABLE whitelist_users_new RENAME TO whitelist_users")
                
                conn.commit()
                print("[MIGRATION] ✅ Миграция завершена успешно!")
            else:
                print("[MIGRATION] Колонка 'role' уже существует, миграция не требуется.")
        
        except sqlite3.OperationalError as e:
            print(f"[MIGRATION] ❌ Ошибка миграции: {e}")
            conn.rollback()
        finally:
            conn.close()
            
    def __init__(self, db_path: str = "marketplace_sync.db"):
        self.db_path = db_path
        self.init_db()
        self.migrate_add_role_column()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        """Инициализация базы данных"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_processings INTEGER DEFAULT 0
            )
        """)
        
        # Таблица истории обработок
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processing_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                wb_products_count INTEGER DEFAULT 0,
                ozon_products_count INTEGER DEFAULT 0,
                yandex_products_count INTEGER DEFAULT 0,
                synced_cells_count INTEGER DEFAULT 0,
                status TEXT,
                error_message TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)
        
        # Таблица файлов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                processing_id INTEGER,
                marketplace TEXT,
                original_filename TEXT,
                file_path TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (processing_id) REFERENCES processing_history (id)
            )
        """)
        
        # НОВАЯ таблица для схем
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schemas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                schema_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                full_comparison_json TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                UNIQUE(user_id, schema_name)
            )
        """)
        # Таблица настроек системы (для хранения admin_id и других настроек)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by INTEGER
            )
        """)
        
        # Таблица белого списка пользователей (максимум 3 слота)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',  -- 'editor' или 'user'
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                added_by INTEGER,
                notes TEXT,
                CHECK(role IN ('editor', 'user'))
            )
        """)
        
        # НОВАЯ таблица для сопоставлений столбцов в схеме
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_id INTEGER,
                wb_column TEXT,
                ozon_column TEXT,
                yandex_column TEXT,
                confidence REAL,
                is_mandatory BOOLEAN DEFAULT 0,
                FOREIGN KEY (schema_id) REFERENCES schemas (id) ON DELETE CASCADE
            )
        """)
        
        conn.commit()
        conn.close()
    
    def add_user(self, user_id: int, username: str = None, 
                 first_name: str = None, last_name: str = None):
        """Добавляет пользователя или обновляет его данные"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, registered_at, total_processings)
            VALUES (?, ?, ?, ?, COALESCE((SELECT registered_at FROM users WHERE user_id = ?), CURRENT_TIMESTAMP),
                    COALESCE((SELECT total_processings FROM users WHERE user_id = ?), 0))
        """, (user_id, username, first_name, last_name, user_id, user_id))
        
        conn.commit()
        conn.close()
    
    def start_processing(self, user_id: int) -> int:
        """Начинает новую обработку, возвращает processing_id"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO processing_history (user_id, started_at, status)
            VALUES (?, ?, 'processing')
        """, (user_id, datetime.now()))
        
        processing_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return processing_id
    
    def complete_processing(self, processing_id: int, 
                          wb_count: int, ozon_count: int, yandex_count: int,
                          synced_cells: int):
        """Завершает обработку успешно"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE processing_history
            SET completed_at = ?,
                wb_products_count = ?,
                ozon_products_count = ?,
                yandex_products_count = ?,
                synced_cells_count = ?,
                status = 'completed'
            WHERE id = ?
        """, (datetime.now(), wb_count, ozon_count, yandex_count, synced_cells, processing_id))
        
        # Увеличиваем счетчик обработок у пользователя
        cursor.execute("""
            UPDATE users
            SET total_processings = total_processings + 1
            WHERE user_id = (SELECT user_id FROM processing_history WHERE id = ?)
        """, (processing_id,))
        
        conn.commit()
        conn.close()
    
    def fail_processing(self, processing_id: int, error_message: str):
        """Завершает обработку с ошибкой"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE processing_history
            SET completed_at = ?,
                status = 'failed',
                error_message = ?
            WHERE id = ?
        """, (datetime.now(), error_message, processing_id))
        
        conn.commit()
        conn.close()
    
    def add_file(self, user_id: int, processing_id: int, 
                 marketplace: str, original_filename: str, file_path: str):
        """Добавляет информацию о загруженном файле"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO files (user_id, processing_id, marketplace, original_filename, file_path)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, processing_id, marketplace, original_filename, file_path))
        
        conn.commit()
        conn.close()
    
    def get_user_stats(self, user_id: int) -> Optional[Dict]:
        """Получает статистику пользователя"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                u.total_processings,
                u.registered_at,
                COUNT(CASE WHEN ph.status = 'completed' THEN 1 END) as successful,
                COUNT(CASE WHEN ph.status = 'failed' THEN 1 END) as failed,
                SUM(CASE WHEN ph.status = 'completed' THEN ph.synced_cells_count ELSE 0 END) as total_synced
            FROM users u
            LEFT JOIN processing_history ph ON u.user_id = ph.user_id
            WHERE u.user_id = ?
            GROUP BY u.user_id
        """, (user_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'total_processings': row[0],
                'registered_at': row[1],
                'successful': row[2],
                'failed': row[3],
                'total_synced_cells': row[4] or 0
            }
        return None
    
    def get_user_history(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Получает историю обработок пользователя"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                id,
                started_at,
                completed_at,
                wb_products_count,
                ozon_products_count,
                yandex_products_count,
                synced_cells_count,
                status,
                error_message
            FROM processing_history
            WHERE user_id = ?
            ORDER BY started_at DESC
            LIMIT ?
        """, (user_id, limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        history = []
        for row in rows:
            history.append({
                'id': row[0],
                'started_at': row[1],
                'completed_at': row[2],
                'wb_count': row[3],
                'ozon_count': row[4],
                'yandex_count': row[5],
                'synced_cells': row[6],
                'status': row[7],
                'error': row[8]
            })
        
        return history
    
    def create_schema(self, user_id: int, schema_name: str) -> int:
        """Создает новую схему"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO schemas (user_id, schema_name)
                VALUES (?, ?)
            """, (user_id, schema_name))
            
            schema_id = cursor.lastrowid
            conn.commit()
            return schema_id
        except sqlite3.IntegrityError:
            return None  # Схема с таким именем уже существует
        finally:
            conn.close()

    def get_user_schemas(self, user_id: int, all_schemas: bool = False) -> List[Dict]:
        """
        Получает список схем пользователя
        
        Args:
            user_id: ID пользователя (для фильтрации СВОИХ схем)
            all_schemas: Если True - попытка получить ВСЕ схемы 
                        (но проверка прав происходит в коде выше!)
        
        Returns:
            List[Dict]: Список схем
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        if all_schemas:
            # ВСЕ схемы (для владельца/админа/редактора)
            # Проверка прав должна быть сделана ДО вызова этого метода!
            cursor.execute("""
                SELECT 
                    s.id, s.schema_name, s.created_at, s.updated_at,
                    s.user_id, u.username, u.first_name,
                    (SELECT COUNT(*) FROM schema_matches WHERE schema_id = s.id) as matches_count
                FROM schemas s
                LEFT JOIN users u ON s.user_id = u.user_id
                ORDER BY s.updated_at DESC
            """)
            rows = cursor.fetchall()
            conn.close()
            
            schemas = []
            for row in rows:
                owner_display = row[6] if row[6] else f"ID: {row[4]}"
                schemas.append({
                    'id': row[0],
                    'name': row[1],
                    'created_at': row[2],
                    'updated_at': row[3],
                    'owner_id': row[4],
                    'owner_name': owner_display,
                    'matches_count': row[7]
                })
            return schemas
        else:
            # Только свои схемы (для всех пользователей, включая обычных)
            cursor.execute("""
                SELECT id, schema_name, created_at, updated_at,
                    (SELECT COUNT(*) FROM schema_matches WHERE schema_id = schemas.id) as matches_count
                FROM schemas
                WHERE user_id = ?
                ORDER BY updated_at DESC
            """, (user_id,))
            rows = cursor.fetchall()
            conn.close()
            
            schemas = []
            for row in rows:
                schemas.append({
                    'id': row[0],
                    'name': row[1],
                    'created_at': row[2],
                    'updated_at': row[3],
                    'matches_count': row[4]
                })
            return schemas
    
    def get_schema_by_name_global(self, schema_name: str) -> Optional[Dict]:
        """
        Получает схему по имени (глобальный поиск, для админов)
        
        Args:
            schema_name: Имя схемы
        
        Returns:
            Optional[Dict]: Информация о схеме или None
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, schema_name, user_id, created_at, updated_at
            FROM schemas
            WHERE schema_name = ?
        """, (schema_name,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'id': row[0],
                'name': row[1],
                'owner_id': row[2],
                'created_at': row[3],
                'updated_at': row[4]
            }
        
        return None

    def get_schema(self, user_id: int, schema_name: str) -> Optional[Dict]:
        """Получает схему по имени"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, schema_name, created_at, updated_at
            FROM schemas
            WHERE user_id = ? AND schema_name = ?
        """, (user_id, schema_name))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'id': row[0],
                'name': row[1],
                'created_at': row[2],
                'updated_at': row[3]
            }
        return None

    def delete_schema(self, user_id: int, schema_name: str) -> bool:
        """Удаляет схему"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM schemas
            WHERE user_id = ? AND schema_name = ?
        """, (user_id, schema_name))
        
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        
        return deleted

    def save_schema_matches(self, schema_id: int, comparison_result: Dict):
        """Сохраняет все совпадения (>= 85%)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Удаляем старые записи
        cursor.execute("DELETE FROM schema_matches WHERE schema_id = ?", (schema_id,))
        
        saved_count = 0
        skipped_count = 0
        
        # Сохраняем только matches_all_three в старую таблицу
        for match in comparison_result.get('matches_all_three', []):
            confidence = match.get('confidence', 0)
            if confidence >= 0.85:
                cursor.execute(
                    "INSERT INTO schema_matches (schema_id, wb_column, ozon_column, yandex_column, confidence, is_mandatory) VALUES (?, ?, ?, ?, ?, ?)",
                    (schema_id, match.get('column_1'), match.get('column_2'), match.get('column_3'), confidence, match.get('mandatory', False))
                )
                saved_count += 1
            else:
                skipped_count += 1
        
        # ✅ НОВОЕ: Сохраняем ВЕСЬ comparison_result как JSON
        full_json = json.dumps(comparison_result, ensure_ascii=False)
        cursor.execute(
            "UPDATE schemas SET full_comparison_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (full_json, schema_id)
        )
        
        cursor.execute("UPDATE schemas SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (schema_id,))
        conn.commit()
        conn.close()
        
        print(f"[DB] Сохранено совпадений: {saved_count}, пропущено (confidence < 85%): {skipped_count}")

    def get_schema_matches(self, schema_id: int) -> Dict:
        """Получает совпадения для схемы"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # ✅ НОВОЕ: Пробуем загрузить полный JSON
        cursor.execute("SELECT full_comparison_json FROM schemas WHERE id = ?", (schema_id,))
        row = cursor.fetchone()
        
        if row and row[0]:
            # Есть полный JSON - возвращаем его
            conn.close()
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                print(f"[DB] Ошибка парсинга JSON для схемы {schema_id}")
        
        # Старая логика (для схем созданных ДО обновления)
        cursor.execute(
            "SELECT wb_column, ozon_column, yandex_column, confidence, is_mandatory FROM schema_matches WHERE schema_id = ?",
            (schema_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        matches = []
        for row in rows:
            matches.append({
                'column_1': row[0],
                'column_2': row[1],
                'column_3': row[2],
                'confidence': row[3],
                'mandatory': row[4]
            })
        
        # Возвращаем пустые массивы для парных (старые схемы)
        return {
            'matches_all_three': matches,
            'matches_1_2': [],
            'matches_1_3': [],
            'matches_2_3': [],
            'only_in_first': [],
            'only_in_second': [],
            'only_in_third': []
        }

    def update_schema_matches(self, schema_id: int, new_comparison_result: Dict):
        """Обновляет схему, добавляя новые совпадения"""
        # Получаем существующие совпадения
        existing_matches = self.get_schema_matches(schema_id)
        
        # Создаем set существующих комбинаций столбцов
        existing_set = set()
        for match in existing_matches['matches_all_three']:
            key = (match['column_1'], match['column_2'], match['column_3'])
            existing_set.add(key)
        
        # Добавляем новые совпадения
        new_count = 0
        for match in new_comparison_result.get('matches_all_three', []):
            key = (match.get('column_1'), match.get('column_2'), match.get('column_3'))
            if key not in existing_set:
                existing_matches['matches_all_three'].append(match)
                new_count += 1
        
        # Сохраняем обновленную схему
        if new_count > 0:
            self.save_schema_matches(schema_id, existing_matches)
        
        return new_count
    
    def get_admin_user_id(self) -> Optional[int]:
        """Получает ID администратора из БД"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT setting_value 
            FROM system_settings 
            WHERE setting_key = 'admin_user_id'
        """)
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None
        return None


    def set_admin_user_id(self, admin_id: int, updated_by: int) -> bool:
        """
        Устанавливает ID администратора
        
        Args:
            admin_id: Telegram user_id нового администратора
            updated_by: Telegram user_id владельца, который делает изменение
        
        Returns:
            bool: True если успешно
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO system_settings (setting_key, setting_value, updated_at, updated_by)
            VALUES ('admin_user_id', ?, CURRENT_TIMESTAMP, ?)
        """, (str(admin_id), updated_by))
        
        conn.commit()
        conn.close()
        return True


    def remove_admin_user_id(self, updated_by: int) -> bool:
        """
        Удаляет администратора (сброс)
        
        Args:
            updated_by: Telegram user_id владельца
        
        Returns:
            bool: True если успешно
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM system_settings 
            WHERE setting_key = 'admin_user_id'
        """)
        
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def get_whitelist_users(self) -> List[int]:
        """Получает список ID пользователей из белого списка"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id 
            FROM whitelist_users 
            ORDER BY added_at ASC
        """)
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]


    def get_whitelist_details(self) -> list:
        """
        Получает детальную информацию о пользователях в whitelist
        
        Returns:
            list: Список словарей с информацией о каждом пользователе
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT user_id, role, added_at, added_by, notes
            FROM whitelist_users
            ORDER BY 
                CASE role 
                    WHEN 'editor' THEN 1 
                    WHEN 'user' THEN 2 
                END,
                added_at ASC
        """)
        
        rows = cursor.fetchall()
        conn.close()
        
        result = []
        for row in rows:
            result.append({
                'user_id': row[0],
                'role': row[1],
                'added_at': row[2],
                'added_by': row[3],
                'notes': row[4]
            })
        
        return result


    def add_whitelist_user(self, user_id: int, added_by: int, role: str = 'user', notes: str = None) -> bool:
        """
        Добавляет пользователя в whitelist с указанием роли
        
        Args:
            user_id: Telegram ID пользователя
            added_by: Telegram ID того, кто добавляет
            role: Роль ('editor' или 'user')
            notes: Опциональная заметка
        
        Returns:
            bool: True если успешно, False если лимит достигнут или ошибка
        """
        # Валидация роли
        if role not in ('editor', 'user'):
            return False
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # Добавляем пользователя (без проверки лимитов)
            cursor.execute("""
                INSERT INTO whitelist_users (user_id, role, added_by, notes)
                VALUES (?, ?, ?, ?)
            """, (user_id, role, added_by, notes))
            
            conn.commit()
            conn.close()
            return True
        
        except sqlite3.IntegrityError:
            conn.close()
            return False  # Дубликат user_id


    def remove_whitelist_user(self, user_id: int) -> bool:
        """
        Удаляет пользователя из белого списка
        
        Args:
            user_id: Telegram user_id удаляемого пользователя
        
        Returns:
            bool: True если успешно удалён
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM whitelist_users 
            WHERE user_id = ?
        """, (user_id,))
        
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted


    def get_whitelist_count(self) -> int:
        """Возвращает текущее количество пользователей в белом списке"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM whitelist_users")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    def get_user_role(self, user_id: int) -> str | None:
        """
        Получает роль пользователя из whitelist
        
        Args:
            user_id: Telegram ID пользователя
        
        Returns:
            str | None: 'editor', 'user' или None если не в whitelist
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT role FROM whitelist_users WHERE user_id = ?
        """, (user_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        return row[0] if row else None
    
    def get_whitelist_slots_info(self) -> dict:
        """
        Возвращает информацию о количестве пользователей по ролям
        Returns:
            dict: {
                'editor': {'used': int},
                'user': {'used': int},
                'total_used': int
            }
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Считаем редакторов
        cursor.execute("SELECT COUNT(*) FROM whitelist_users WHERE role = 'editor'")
        editor_count = cursor.fetchone()[0]
        
        # Считаем обычных пользователей
        cursor.execute("SELECT COUNT(*) FROM whitelist_users WHERE role = 'user'")
        user_count = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'editor': {
                'used': editor_count
            },
            'user': {
                'used': user_count
            },
            'total_used': editor_count + user_count
        }



