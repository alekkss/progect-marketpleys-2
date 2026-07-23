"""
Хеширование и проверка паролей (bcrypt).

bcrypt — адаптивная хеш-функция, специально разработанная для паролей.
Каждый вызов hash_password() занимает ~100-300 мс (зависит от rounds),
поэтому все операции выполняются в thread pool через asyncio.to_thread().

Паттерн: Service — изолирует логику работы с паролями.
Принцип Single Responsibility — только хеширование и проверка.

Использование:
    from web.auth.password import PasswordHasher

    password_hash = await PasswordHasher.hash_password("my_secret")
    is_valid = await PasswordHasher.verify_password("my_secret", password_hash)
"""

import asyncio

from utils.logger_config import setup_logger

logger = setup_logger("web.auth.password")

# Количество раундов bcrypt (2^12 = 4096 итераций)
# 12 — баланс между безопасностью и скоростью (~250 мс на хеш)
# Увеличение на 1 удваивает время хеширования
_BCRYPT_ROUNDS: int = 12


class PasswordHasher:
    """
    Сервис хеширования паролей.

    Все методы статические и асинхронные — класс используется
    как namespace, не требует создания экземпляра.

    Внутри использует bcrypt с автоматической генерацией соли.
    Результат содержит соль и параметры алгоритма — для проверки
    нужен только хеш и исходный пароль.
    """

    @staticmethod
    async def hash_password(password: str) -> str:
        """
        Хеширует пароль с автоматической генерацией соли.

        Выполняется в thread pool — не блокирует event loop.

        Args:
            password: Исходный пароль (plain text)

        Returns:
            Строка bcrypt-хеша (60 символов, содержит соль и параметры)

        Raises:
            ValueError: если пароль пустой
            RuntimeError: если bcrypt не установлен
        """
        if not password:
            raise ValueError("Пароль не может быть пустым")

        return await asyncio.to_thread(_hash_sync, password)

    @staticmethod
    async def verify_password(password: str, password_hash: str) -> bool:
        """
        Проверяет пароль против сохранённого хеша.

        Выполняется в thread pool — не блокирует event loop.
        Использует constant-time сравнение (защита от timing-атак).

        Args:
            password: Пароль для проверки (plain text)
            password_hash: Сохранённый bcrypt-хеш из БД

        Returns:
            True если пароль совпадает, False иначе
        """
        if not password or not password_hash:
            return False

        return await asyncio.to_thread(_verify_sync, password, password_hash)

    @staticmethod
    def validate_password_strength(password: str) -> tuple[bool, str]:
        """
        Проверяет минимальные требования к паролю.

        Синхронный метод — не требует IO, только проверка строки.

        Требования:
            - Минимум 8 символов
            - Не состоит только из пробелов

        Args:
            password: Пароль для проверки

        Returns:
            Кортеж (is_valid, error_message).
            Если is_valid=True, error_message пустая строка.
        """
        if not password or not password.strip():
            return False, "Пароль не может быть пустым"

        if len(password) < 8:
            return False, "Пароль должен содержать минимум 8 символов"

        if len(password) > 72:
            # bcrypt обрезает пароли длиннее 72 байт
            return False, "Пароль не должен превышать 72 символа"

        return True, ""


def _hash_sync(password: str) -> str:
    """
    Синхронное хеширование (выполняется в thread pool).

    Args:
        password: Исходный пароль

    Returns:
        Строка bcrypt-хеша

    Raises:
        RuntimeError: если bcrypt не установлен
    """
    try:
        import bcrypt
    except ImportError:
        raise RuntimeError(
            "Библиотека bcrypt не установлена. "
            "Установите: pip install bcrypt==4.2.0"
        )

    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def _verify_sync(password: str, password_hash: str) -> bool:
    """
    Синхронная проверка пароля (выполняется в thread pool).

    Args:
        password: Пароль для проверки
        password_hash: Сохранённый хеш

    Returns:
        True если совпадает
    """
    try:
        import bcrypt
    except ImportError:
        logger.error(
            "Библиотека bcrypt не установлена — проверка пароля невозможна"
        )
        return False

    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError) as e:
        # Невалидный формат хеша (повреждённая запись в БД)
        logger.error("Ошибка проверки пароля: %s", e)
        return False
