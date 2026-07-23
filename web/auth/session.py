"""
Управление cookie-сессиями веб-приложения.

Сессия — запись в таблице web_sessions, привязанная к web_user.
Session_id (UUID4) хранится в подписанном cookie на стороне клиента.
При каждом запросе middleware извлекает session_id из cookie и
проверяет его валидность через WebSessionManager.

Жизненный цикл:
    1. Пользователь вводит email + пароль → POST /auth/login
    2. WebSessionManager.create_session() → запись в БД + cookie
    3. Middleware проверяет cookie → WebSessionManager.get_session()
    4. Logout → WebSessionManager.delete_session() → удаление из БД + cookie

Паттерн: Repository — изолирует логику хранения сессий (PostgreSQL).
Паттерн: Service — предоставляет бизнес-операции над сессиями.

Использование:
    from web.auth.session import WebSessionManager

    session_mgr = WebSessionManager()
    session_data = await session_mgr.create_session(web_user_id, request)
    user_data = await session_mgr.get_session(session_id)
    await session_mgr.delete_session(session_id)
"""

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

from aiohttp.web import Request, Response

from config.config import Config
from utils.logger_config import setup_logger

logger = setup_logger("web.auth.session")

# Имя cookie для хранения session_id
COOKIE_NAME: str = "MARKETPLACE_SESSION"

# Атрибуты cookie для безопасности
_COOKIE_HTTPONLY: bool = True      # Недоступна из JavaScript (защита от XSS)
_COOKIE_SECURE: bool = True       # Только через HTTPS
_COOKIE_SAMESITE: str = "Lax"     # Защита от CSRF (Lax — баланс безопасности и UX)
_COOKIE_PATH: str = "/"           # Действует на весь сайт


class WebSessionManager:
    """
    Менеджер веб-сессий.

    Все операции выполняются через таблицу web_sessions в PostgreSQL.
    Session_id генерируется как UUID4 — криптографически стойкий,
    невозможно угадать или предсказать.

    Не хранит состояние в памяти — каждый запрос проверяет БД.
    Это позволяет корректно работать при нескольких инстансах
    и при перезапуске сервера.
    """

    @staticmethod
    async def create_session(
        web_user_id: int,
        request: Request,
    ) -> Dict:
        """
        Создаёт новую сессию в БД.

        Перед созданием удаляет все предыдущие сессии пользователя
        (одна активная сессия на пользователя — ограничение проекта).

        Args:
            web_user_id: ID пользователя в таблице web_users
            request: HTTP-запрос (для извлечения IP и User-Agent)

        Returns:
            Словарь с session_id и expires_at для установки cookie
        """
        from bot import storage

        session_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=Config.WEB_SESSION_MAX_AGE,
        )

        # Извлекаем метаданные клиента
        ip_address = _get_client_ip(request)
        user_agent = request.headers.get("User-Agent", "")[:512]

        # Удаляем старые сессии пользователя (одна активная сессия)
        await storage.db.delete_user_sessions(web_user_id)

        # Создаём новую сессию
        await storage.db.create_web_session(
            session_id=session_id,
            web_user_id=web_user_id,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        logger.info(
            "Создана сессия для web_user_id=%d (IP: %s)",
            web_user_id, ip_address,
        )

        return {
            "session_id": session_id,
            "expires_at": expires_at,
        }

    @staticmethod
    async def get_session(session_id: str) -> Optional[Dict]:
        """
        Проверяет и возвращает данные сессии.

        Возвращает None если:
            - Сессия не найдена в БД
            - Сессия истекла (expires_at <= NOW())
            - Пользователь деактивирован (is_active=False)

        Args:
            session_id: UUID сессии из cookie

        Returns:
            Словарь с данными пользователя или None
        """
        if not session_id:
            return None

        from bot import storage

        session_data = await storage.db.get_web_session(session_id)

        if session_data is None:
            return None

        # Проверяем активность пользователя
        if not session_data.get("is_active", True):
            logger.warning(
                "Попытка использовать сессию деактивированного пользователя "
                "(web_user_id=%d)",
                session_data.get("web_user_id"),
            )
            return None

        return session_data

    @staticmethod
    async def delete_session(session_id: str) -> None:
        """
        Удаляет сессию из БД (logout).

        Args:
            session_id: UUID сессии для удаления
        """
        if not session_id:
            return

        from bot import storage

        await storage.db.delete_web_session(session_id)
        logger.debug("Сессия удалена: %s", session_id[:8])

    @staticmethod
    async def delete_all_user_sessions(web_user_id: int) -> None:
        """
        Удаляет все сессии пользователя (принудительный logout).

        Используется при:
            - Блокировке пользователя администратором
            - Смене пароля
            - Смене роли

        Args:
            web_user_id: ID веб-пользователя
        """
        from bot import storage

        await storage.db.delete_user_sessions(web_user_id)
        logger.info(
            "Все сессии удалены для web_user_id=%d", web_user_id,
        )

    @staticmethod
    async def cleanup_expired() -> int:
        """
        Удаляет все истёкшие сессии из БД.

        Вызывается периодически (раз в час) для очистки.

        Returns:
            Количество удалённых сессий
        """
        from bot import storage

        deleted = await storage.db.cleanup_expired_web_sessions()
        if deleted > 0:
            logger.info("Очищено %d истёкших сессий", deleted)
        return deleted

    @staticmethod
    def set_session_cookie(
        response: Response,
        session_id: str,
        expires_at: datetime,
    ) -> None:
        """
        Устанавливает cookie с session_id в HTTP-ответ.

        Атрибуты cookie:
            - HttpOnly: недоступна из JS (защита от XSS)
            - Secure: только через HTTPS
            - SameSite=Lax: защита от CSRF
            - Max-Age: время жизни в секундах

        Args:
            response: HTTP-ответ, в который добавляется cookie
            session_id: UUID сессии
            expires_at: Время истечения (для расчёта Max-Age)
        """
        max_age = int(
            (expires_at - datetime.now(timezone.utc)).total_seconds()
        )

        response.set_cookie(
            COOKIE_NAME,
            session_id,
            max_age=max_age,
            httponly=_COOKIE_HTTPONLY,
            secure=_COOKIE_SECURE,
            samesite=_COOKIE_SAMESITE,
            path=_COOKIE_PATH,
        )

    @staticmethod
    def delete_session_cookie(response: Response) -> None:
        """
        Удаляет cookie сессии из HTTP-ответа (logout).

        Устанавливает cookie с пустым значением и max_age=0,
        что заставляет браузер немедленно удалить её.

        Args:
            response: HTTP-ответ
        """
        response.del_cookie(
            COOKIE_NAME,
            path=_COOKIE_PATH,
        )

    @staticmethod
    def get_session_id_from_request(request: Request) -> Optional[str]:
        """
        Извлекает session_id из cookie запроса.

        Args:
            request: HTTP-запрос

        Returns:
            Session_id или None если cookie отсутствует/пустая
        """
        session_id = request.cookies.get(COOKIE_NAME)
        if not session_id or not session_id.strip():
            return None
        return session_id.strip()


def _get_client_ip(request: Request) -> str:
    """
    Извлекает реальный IP клиента с учётом Nginx proxy.

    Порядок проверки:
        1. X-Real-IP (устанавливается Nginx)
        2. X-Forwarded-For (первый IP в цепочке)
        3. request.remote (прямое подключение)

    Args:
        request: HTTP-запрос

    Returns:
        Строка с IP-адресом клиента
    """
    # Nginx устанавливает X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fallback: X-Forwarded-For (может содержать цепочку прокси)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Первый IP — реальный клиент
        return forwarded_for.split(",")[0].strip()

    # Прямое подключение (без прокси)
    return request.remote or "unknown"
