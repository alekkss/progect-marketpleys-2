"""
Модуль сессионного хранилища для временных данных FSM.

Предоставляет единый интерфейс для хранения промежуточных данных
(пути к файлам, состояния загрузки) с TTL и fallback на in-memory.

Архитектура:
    - Redis: основной бэкенд (TTL 30 мин, автоочистка)
    - Dict: fallback если Redis недоступен (предупреждение в логах)
"""

import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from config.config import Config

logger = logging.getLogger('session_storage')

# ===================================================================
# Константы
# ===================================================================
SESSION_TTL_SECONDS: int = 1800  # 30 минут автоочистки
REDIS_KEY_PREFIX: str = "bot:session:"


class SessionStorage:
    """
    Универсальное хранилище сессий с поддержкой Redis и in-memory fallback.

    Интерфейс:
        get(user_id, key) -> dict | None
        set(user_id, key, value) -> None
        delete(user_id, key) -> None
        clear(user_id) -> None
        exists(user_id, key) -> bool

    Args:
        redis_url: URL подключения к Redis (опционально, из Config)
    """

    def __init__(self, redis_url: Optional[str] = None) -> None:
        self._redis_url: Optional[str] = redis_url or getattr(Config, 'REDIS_URL', None)
        self._redis: Optional[Any] = None
        self._fallback: Dict[int, Dict[str, Any]] = {}
        self._using_fallback: bool = False

    async def connect(self) -> None:
        """
        Инициализация подключения к Redis.

        Если Redis недоступен — переключается на in-memory fallback
        с предупреждением в логах (приложение НЕ падает).
        """
        if not self._redis_url:
            logger.warning("REDIS_URL не задан — используем in-memory fallback.")
            self._using_fallback = True
            return

        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
            )
            # Проверяем соединение
            await self._redis.ping()
            logger.info("Подключение к Redis установлено (TTL=%d сек).", SESSION_TTL_SECONDS)
        except Exception as e:
            logger.warning(
                "Не удалось подключиться к Redis (%s). "
                "Переключение на in-memory fallback. "
                "Данные сессий будут потеряны при перезапуске бота!",
                e,
            )
            self._redis = None
            self._using_fallback = True

    async def close(self) -> None:
        """Корректное закрытие соединения с Redis."""
        if self._redis is not None:
            await self._redis.close()
            self._redis = None
            logger.info("Соединение с Redis закрыто.")

    def _make_key(self, user_id: int, key: str) -> str:
        """Формирует ключ Redis с префиксом."""
        return f"{REDIS_KEY_PREFIX}{user_id}:{key}"

    # -----------------------------------------------------------------
    # Публичный интерфейс
    # -----------------------------------------------------------------

    async def get(self, user_id: int, key: str) -> Optional[Dict[str, Any]]:
        """
        Получает данные сессии пользователя.

        Args:
            user_id: Telegram user_id
            key: Идентификатор данных (например, 'upload_files', 'schema_files')

        Returns:
            Словарь с данными или None если не найдено / истек TTL
        """
        if self._using_fallback or self._redis is None:
            session = self._fallback.get(user_id, {})
            return session.get(key)

        try:
            raw = await self._redis.get(self._make_key(user_id, key))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as e:
            logger.error("Ошибка чтения из Redis: %s", e)
            return None

    async def set(self, user_id: int, key: str, value: Dict[str, Any]) -> None:
        """
        Сохраняет данные сессии с TTL.

        Args:
            user_id: Telegram user_id
            key: Идентификатор данных
            value: Словарь с данными (должен быть JSON-serializable)
        """
        if self._using_fallback or self._redis is None:
            if user_id not in self._fallback:
                self._fallback[user_id] = {}
            self._fallback[user_id][key] = value
            return

        try:
            raw = json.dumps(value, ensure_ascii=False, default=str)
            await self._redis.setex(
                self._make_key(user_id, key),
                SESSION_TTL_SECONDS,
                raw,
            )
        except Exception as e:
            logger.error("Ошибка записи в Redis: %s", e)
            # Fallback на in-memory для этого конкретного вызова
            if user_id not in self._fallback:
                self._fallback[user_id] = {}
            self._fallback[user_id][key] = value

    async def delete(self, user_id: int, key: str) -> None:
        """
        Удаляет конкретный ключ сессии.

        Args:
            user_id: Telegram user_id
            key: Идентификатор данных
        """
        if self._using_fallback or self._redis is None:
            session = self._fallback.get(user_id, {})
            session.pop(key, None)
            if not session:
                self._fallback.pop(user_id, None)
            return

        try:
            await self._redis.delete(self._make_key(user_id, key))
        except Exception as e:
            logger.error("Ошибка удаления из Redis: %s", e)

    async def clear(self, user_id: int) -> None:
        """
        Полностью очищает все данные сессии пользователя.

        Args:
            user_id: Telegram user_id
        """
        if self._using_fallback or self._redis is None:
            self._fallback.pop(user_id, None)
            return

        try:
            # Удаляем все ключи с префиксом пользователя
            pattern = f"{REDIS_KEY_PREFIX}{user_id}:*"
            keys = await self._redis.keys(pattern)
            if keys:
                await self._redis.delete(*keys)
        except Exception as e:
            logger.error("Ошибка очистки сессии в Redis: %s", e)

    async def exists(self, user_id: int, key: str) -> bool:
        """
        Проверяет существование ключа сессии.

        Args:
            user_id: Telegram user_id
            key: Идентификатор данных

        Returns:
            True если ключ существует и не истёк
        """
        if self._using_fallback or self._redis is None:
            session = self._fallback.get(user_id, {})
            return key in session

        try:
            return await self._redis.exists(self._make_key(user_id, key)) > 0
        except Exception as e:
            logger.error("Ошибка проверки в Redis: %s", e)
            return False

    # -----------------------------------------------------------------
    # Контекстный менеджер для graceful cleanup
    # -----------------------------------------------------------------

    async def get_files_dict(self, user_id: int, key: str) -> Dict[str, Any]:
        """
        Получает словарь файлов или возвращает пустой dict.

        Удобно для паттерна:
            files = await session_storage.get_files_dict(user_id, 'upload')
            files['wb'] = path
            await session_storage.set(user_id, 'upload', files)

        Args:
            user_id: Telegram user_id
            key: Идентификатор (например, 'upload', 'schema')

        Returns:
            Словарь файлов (может быть пустым)
        """
        data = await self.get(user_id, key)
        if data is None:
            return {}
        if not isinstance(data, dict):
            logger.warning("Некорректный формат сессии user_id=%d key=%s", user_id, key)
            return {}
        return data

    async def set_files_dict(self, user_id: int, key: str, files: Dict[str, Any]) -> None:
        """
        Сохраняет словарь файлов в сессию.

        Args:
            user_id: Telegram user_id
            key: Идентификатор сессии
            files: Словарь {marketplace: path, ...}
        """
        await self.set(user_id, key, files)


# ===================================================================
# Глобальный экземпляр (инициализируется в bot.py)
# ===================================================================
session_storage: SessionStorage = SessionStorage()