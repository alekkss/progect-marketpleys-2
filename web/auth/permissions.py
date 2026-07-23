"""
Проверка прав доступа веб-пользователей.

WebAccessManager — адаптер, который преобразует роль из таблицы web_users
в набор разрешений. Роли идентичны Telegram-боту:
    - owner  — полный контроль (управление всеми пользователями и схемами)
    - admin  — управление whitelist, доступ ко всем схемам
    - editor — все схемы (чтение/запись), свои CRUD
    - user   — только свои схемы, загрузка, статистика

Паттерн: Adapter — преобразует данные из БД (строка role) в булевы проверки.
Принцип Single Responsibility — только проверка прав, без аутентификации.
Принцип Open/Closed — добавление новой роли = новый метод, существующие не меняются.

Использование:
    from web.auth.permissions import WebAccessManager

    can_manage = await WebAccessManager.can_manage_users(user_data)
    can_see = WebAccessManager.can_see_all_schemas(user_data)
"""

from typing import Dict, Optional

from utils.logger_config import setup_logger

logger = setup_logger("web.auth.permissions")

# Иерархия ролей (больший вес = больше прав)
_ROLE_HIERARCHY: Dict[str, int] = {
    "owner": 100,
    "admin": 80,
    "editor": 60,
    "user": 40,
}


class WebAccessManager:
    """
    Менеджер проверки прав веб-пользователей.

    Все методы статические — класс используется как namespace.
    Принимают user_data (словарь из request["user"]) — данные
    сессии, загруженные auth middleware.

    Формат user_data (из WebSessionManager.get_session):
        {
            "web_user_id": int,
            "email": str,
            "display_name": str | None,
            "role": str,  — "owner" | "admin" | "editor" | "user"
            "is_active": bool,
        }
    """

    @staticmethod
    def get_role(user_data: Optional[Dict]) -> str:
        """
        Извлекает роль пользователя из данных сессии.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            Строка роли или 'user' по умолчанию
        """
        if not user_data:
            return "user"
        return user_data.get("role", "user")

    @staticmethod
    def is_owner(user_data: Optional[Dict]) -> bool:
        """
        Проверяет, является ли пользователь владельцем.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если роль = 'owner'
        """
        if not user_data:
            return False
        return user_data.get("role") == "owner"

    @staticmethod
    def is_admin(user_data: Optional[Dict]) -> bool:
        """
        Проверяет, является ли пользователь администратором.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если роль = 'admin'
        """
        if not user_data:
            return False
        return user_data.get("role") == "admin"

    @staticmethod
    def is_admin_or_owner(user_data: Optional[Dict]) -> bool:
        """
        Проверяет, является ли пользователь владельцем или администратором.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если роль = 'owner' или 'admin'
        """
        if not user_data:
            return False
        return user_data.get("role") in ("owner", "admin")

    @staticmethod
    def is_editor(user_data: Optional[Dict]) -> bool:
        """
        Проверяет, является ли пользователь редактором.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если роль = 'editor'
        """
        if not user_data:
            return False
        return user_data.get("role") == "editor"

    @staticmethod
    def can_manage_users(user_data: Optional[Dict]) -> bool:
        """
        Может ли пользователь управлять другими пользователями.

        Доступно: owner, admin.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если имеет право управления
        """
        if not user_data:
            return False
        return user_data.get("role") in ("owner", "admin")

    @staticmethod
    def can_see_all_schemas(user_data: Optional[Dict]) -> bool:
        """
        Может ли пользователь видеть все схемы (не только свои).

        Доступно: owner, admin, editor.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если видит все схемы
        """
        if not user_data:
            return False
        return user_data.get("role") in ("owner", "admin", "editor")

    @staticmethod
    def can_create_schemas(user_data: Optional[Dict]) -> bool:
        """
        Может ли пользователь создавать схемы.

        Доступно: все авторизованные пользователи.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если может создавать
        """
        if not user_data:
            return False
        return user_data.get("role") in ("owner", "admin", "editor", "user")

    @staticmethod
    def can_upload_files(user_data: Optional[Dict]) -> bool:
        """
        Может ли пользователь загружать файлы для обработки.

        Доступно: все авторизованные пользователи.

        Args:
            user_data: Данные пользователя из request["user"]

        Returns:
            True если может загружать
        """
        if not user_data:
            return False
        return user_data.get("role") in ("owner", "admin", "editor", "user")

    @staticmethod
    def can_delete_schema(user_data: Optional[Dict], schema_owner_id: int) -> bool:
        """
        Может ли пользователь удалить конкретную схему.

        Правила:
            - Owner и admin могут удалять любые схемы
            - Editor и user — только свои

        Args:
            user_data: Данные пользователя из request["user"]
            schema_owner_id: user_id владельца схемы

        Returns:
            True если имеет право удалить
        """
        if not user_data:
            return False

        role = user_data.get("role", "user")
        if role in ("owner", "admin"):
            return True

        # Editor и user — только свои схемы
        # Связь web_user → telegram_user для проверки владения
        # осуществляется через telegram_user_id в web_users
        web_user_id = user_data.get("web_user_id")
        telegram_user_id = user_data.get("telegram_user_id")

        if telegram_user_id and telegram_user_id == schema_owner_id:
            return True

        return False

    @staticmethod
    def has_minimum_role(user_data: Optional[Dict], minimum_role: str) -> bool:
        """
        Проверяет, что роль пользователя не ниже указанной.

        Иерархия: owner > admin > editor > user.

        Args:
            user_data: Данные пользователя из request["user"]
            minimum_role: Минимальная требуемая роль

        Returns:
            True если роль пользователя >= minimum_role
        """
        if not user_data:
            return False

        user_role = user_data.get("role", "user")
        user_weight = _ROLE_HIERARCHY.get(user_role, 0)
        required_weight = _ROLE_HIERARCHY.get(minimum_role, 0)

        return user_weight >= required_weight
