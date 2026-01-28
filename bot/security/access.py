"""
Модуль управления правами доступа пользователей
Следует принципу Single Responsibility - только проверка прав
"""
import sys
from pathlib import Path
from typing import Set, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.config import Config
from bot.storage import db


class AccessManager:
    """Управление правами доступа пользователей"""
    
    @staticmethod
    def get_admin_user_id() -> Optional[int]:
        """
        Получает ID администратора (приоритет: БД -> env)
        
        Returns:
            Optional[int]: ID администратора или None
        """
        admin_from_db = db.get_admin_user_id()
        if admin_from_db and admin_from_db > 0:
            return admin_from_db
        
        if Config.ACCESS_ADMIN_ID > 0:
            return Config.ACCESS_ADMIN_ID
        
        return None
    
    @staticmethod
    def get_privileged_users() -> Set[int]:
        """
        Возвращает множество ID пользователей с привилегированным доступом
        (владелец + администратор)
        """
        privileged = set()
        
        if Config.ACCESS_OWNER_ID > 0:
            privileged.add(Config.ACCESS_OWNER_ID)
        
        admin_id = AccessManager.get_admin_user_id()
        if admin_id:
            privileged.add(admin_id)
        
        return privileged
    
    @staticmethod
    def get_all_authorized_users() -> Set[int]:
        """
        Возвращает ВСЕ авторизованные пользователи:
        владелец + администратор + whitelist (максимум 3)
        """
        authorized = AccessManager.get_privileged_users()
        whitelist = db.get_whitelist_users()
        authorized.update(whitelist)
        return authorized
    
    @staticmethod
    def has_access(user_id: int) -> bool:
        """Проверяет, имеет ли пользователь доступ к боту"""
        return user_id in AccessManager.get_all_authorized_users()
    
    @staticmethod
    def has_access_management_rights(user_id: int) -> bool:
        """
        Проверяет права на управление доступами
        (только владелец и администратор, НЕ whitelist пользователи)
        """
        return user_id in AccessManager.get_privileged_users()
    
    @staticmethod
    def is_owner(user_id: int) -> bool:
        """Проверяет, является ли пользователь владельцем"""
        return user_id == Config.ACCESS_OWNER_ID and Config.ACCESS_OWNER_ID > 0
    
    @staticmethod
    def is_admin(user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором"""
        admin_id = AccessManager.get_admin_user_id()
        return admin_id is not None and user_id == admin_id
    
    @staticmethod
    def is_in_whitelist(user_id: int) -> bool:
        """Проверяет, находится ли пользователь в белом списке"""
        return user_id in db.get_whitelist_users()
    
    @staticmethod
    def is_editor(user_id: int) -> bool:
        """Проверяет, является ли пользователь редактором"""
        role = db.get_user_role(user_id)
        return role == 'editor'
    
    @staticmethod
    def can_manage_schemas(user_id: int) -> bool:
        """
        Может ли управлять СВОИМИ схемами (создавать, редактировать, удалять СВОИ)
        
        ✅ ВСЕ авторизованные пользователи могут управлять СВОИМИ схемами!
        """
        return AccessManager.has_access(user_id)
    
    @staticmethod
    def can_see_all_schemas(user_id: int) -> bool:
        """
        Может ли видеть ВСЕ схемы (включая чужие)
        
        ✅ Только владелец, админ и редактор
        ❌ Обычные пользователи (user) - НЕТ
        """
        if AccessManager.has_access_management_rights(user_id):  # Владелец + Админ
            return True
        return AccessManager.is_editor(user_id)  # Редактор
        
    @staticmethod
    def set_admin(admin_id: int, updated_by: int) -> bool:
        """Устанавливает нового администратора (только владелец может!)"""
        if not AccessManager.is_owner(updated_by):
            return False
        return db.set_admin_user_id(admin_id, updated_by)
    
    @staticmethod
    def remove_admin(updated_by: int) -> bool:
        """Удаляет администратора (только владелец может!)"""
        if not AccessManager.is_owner(updated_by):
            return False
        return db.remove_admin_user_id(updated_by)
    
    @staticmethod
    def add_whitelist_user(user_id: int, added_by: int, role: str = 'user', notes: str = None) -> tuple[bool, str]:
        """
        Добавляет пользователя в whitelist с указанием роли
        
        Args:
            user_id: Telegram user_id добавляемого пользователя
            added_by: Telegram user_id того, кто добавляет
            role: Роль ('editor' или 'user')
            notes: Опциональная заметка
        
        Returns:
            tuple[bool, str]: (успех, сообщение об ошибке если неудача)
        """
        if not AccessManager.has_access_management_rights(added_by):
            return False, "Нет прав на управление доступами"
        
        if role not in ('editor', 'user'):
            return False, "Неверная роль. Допустимо: 'editor' или 'user'"
        
        if AccessManager.is_owner(user_id):
            return False, "Владелец уже имеет полный доступ"
        
        if AccessManager.is_admin(user_id):
            return False, "Администратор уже имеет полный доступ"
        
        if AccessManager.is_in_whitelist(user_id):
            return False, "Пользователь уже в белом списке"
        
        # Лимиты на количество пользователей отсутствуют
        # Можно добавлять неограниченное количество редакторов и пользователей
        
        success = db.add_whitelist_user(user_id, added_by, role, notes)
        if not success:
            return False, "Ошибка при добавлении в базу данных"
        
        return True, ""
    
    @staticmethod
    def remove_whitelist_user(user_id: int, removed_by: int) -> tuple[bool, str]:
        """Удаляет пользователя из whitelist (владелец или админ могут)"""
        if not AccessManager.has_access_management_rights(removed_by):
            return False, "Нет прав на управление доступами"
        
        success = db.remove_whitelist_user(user_id)
        
        if not success:
            return False, "Пользователь не найден в белом списке"
        
        return True, ""
    
    @staticmethod
    def get_whitelist_slots_info() -> dict:
        """
        Возвращает информацию о занятых/свободных слотах whitelist
        с разбивкой по ролям
        """
        return db.get_whitelist_slots_info()
