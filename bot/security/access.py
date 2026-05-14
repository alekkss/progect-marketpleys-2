"""
Модуль управления правами доступа пользователей.
Следует принципу Single Responsibility — только проверка прав.
Все методы, обращающиеся к БД, асинхронны (async/await).
Единственное исключение: is_owner() — читает только конфиг (Config).
"""
import sys
from pathlib import Path
from typing import Optional, Set
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.config import Config
from bot import storage  # ✅ Импортируем модуль, а не переменную

class AccessManager:
    """Управление правами доступа пользователей."""

    @staticmethod
    async def get_admin_user_id() -> Optional[int]:
        """Получает ID администратора (приоритет: БД → env)."""
        admin_from_db = await storage.db.get_admin_user_id()  # ✅
        if admin_from_db and admin_from_db > 0:
            return admin_from_db
        if Config.ACCESS_ADMIN_ID > 0:
            return Config.ACCESS_ADMIN_ID
        return None

    @staticmethod
    async def get_privileged_users() -> Set[int]:
        """Возвращает множество ID пользователей с привилегированным доступом."""
        privileged: Set[int] = set()
        if Config.ACCESS_OWNER_ID > 0:
            privileged.add(Config.ACCESS_OWNER_ID)
        admin_id = await AccessManager.get_admin_user_id()
        if admin_id:
            privileged.add(admin_id)
        return privileged

    @staticmethod
    async def get_all_authorized_users() -> Set[int]:
        """Возвращает ВСЕ авторизованные пользователи."""
        authorized = await AccessManager.get_privileged_users()
        whitelist = await storage.db.get_whitelist_users()  # ✅
        authorized.update(whitelist)
        return authorized

    @staticmethod
    async def has_access(user_id: int) -> bool:
        """Проверяет, имеет ли пользователь доступ к боту."""
        authorized = await AccessManager.get_all_authorized_users()
        return user_id in authorized

    @staticmethod
    async def has_access_management_rights(user_id: int) -> bool:
        """Проверяет права на управление доступами."""
        privileged = await AccessManager.get_privileged_users()
        return user_id in privileged

    @staticmethod
    def is_owner(user_id: int) -> bool:
        """Проверяет, является ли пользователь владельцем."""
        return user_id == Config.ACCESS_OWNER_ID and Config.ACCESS_OWNER_ID > 0

    @staticmethod
    async def is_admin(user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором."""
        admin_id = await AccessManager.get_admin_user_id()
        return admin_id is not None and user_id == admin_id

    @staticmethod
    async def is_in_whitelist(user_id: int) -> bool:
        """Проверяет, находится ли пользователь в белом списке."""
        whitelist = await storage.db.get_whitelist_users()  # ✅
        return user_id in whitelist

    @staticmethod
    async def is_editor(user_id: int) -> bool:
        """Проверяет, является ли пользователь редактором."""
        role = await storage.db.get_user_role(user_id)  # ✅
        return role == 'editor'

    @staticmethod
    async def can_manage_schemas(user_id: int) -> bool:
        """Может ли пользователь управлять СВОИМИ схемами."""
        return await AccessManager.has_access(user_id)

    @staticmethod
    async def can_see_all_schemas(user_id: int) -> bool:
        """Может ли видеть ВСЕ схемы."""
        if await AccessManager.has_access_management_rights(user_id):
            return True
        return await AccessManager.is_editor(user_id)

    @staticmethod
    async def set_admin(admin_id: int, updated_by: int) -> bool:
        """Устанавливает нового администратора."""
        if not AccessManager.is_owner(updated_by):
            return False
        return await storage.db.set_admin_user_id(admin_id, updated_by)  # ✅

    @staticmethod
    async def remove_admin(updated_by: int) -> bool:
        """Удаляет администратора."""
        if not AccessManager.is_owner(updated_by):
            return False
        return await storage.db.remove_admin_user_id(updated_by)  # ✅

    @staticmethod
    async def add_whitelist_user(
        user_id: int,
        added_by: int,
        role: str = 'user',
        notes: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Добавляет пользователя в whitelist."""
        if not await AccessManager.has_access_management_rights(added_by):
            return False, "Нет прав на управление доступами"
        if role not in ('editor', 'user'):
            return False, "Неверная роль. Допустимо: 'editor' или 'user'"
        if AccessManager.is_owner(user_id):
            return False, "Владелец уже имеет полный доступ"
        if await AccessManager.is_admin(user_id):
            return False, "Администратор уже имеет полный доступ"
        if await AccessManager.is_in_whitelist(user_id):
            return False, "Пользователь уже в белом списке"

        success = await storage.db.add_whitelist_user(user_id, added_by, role, notes)  # ✅
        if not success:
            return False, "Ошибка при добавлении в базу данных"
        return True, ""

    @staticmethod
    async def remove_whitelist_user(user_id: int, removed_by: int) -> tuple[bool, str]:
        """Удаляет пользователя из whitelist."""
        if not await AccessManager.has_access_management_rights(removed_by):
            return False, "Нет прав на управление доступами"
        success = await storage.db.remove_whitelist_user(user_id)  # ✅
        if not success:
            return False, "Пользователь не найден в белом списке"
        return True, ""

    @staticmethod
    async def get_whitelist_slots_info() -> dict:
        """Возвращает информацию о занятых слотах whitelist."""
        return await storage.db.get_whitelist_slots_info()  # ✅