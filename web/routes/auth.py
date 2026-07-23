"""
Маршруты аутентификации веб-приложения.

Эндпоинты:
    GET  /auth/login    — отображение формы входа
    POST /auth/login    — обработка входа (email + password)
    GET  /auth/register — отображение формы регистрации
    POST /auth/register — создание аккаунта
    POST /auth/logout   — выход (удаление сессии)

Формы отправляются как application/x-www-form-urlencoded (стандартные HTML-формы).
После успешного входа/регистрации — редирект на /dashboard (или на next из query).

Паттерн: Controller — принимает HTTP-запрос, делегирует бизнес-логику
сервисам (PasswordHasher, WebSessionManager), возвращает HTTP-ответ.
"""

from typing import TYPE_CHECKING

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from config.config import Config
from web.auth.password import PasswordHasher
from web.auth.session import WebSessionManager
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

if TYPE_CHECKING:
    pass

logger = setup_logger("web.routes.auth")

# Максимальная длина полей формы (защита от abuse)
_MAX_EMAIL_LENGTH: int = 255
_MAX_DISPLAY_NAME_LENGTH: int = 100


async def login_page(request: Request) -> Response:
    """
    GET /auth/login — отображение формы входа.

    Если пользователь уже авторизован — редирект на dashboard.
    Параметр ?next= сохраняется для редиректа после логина.
    Параметр ?error= отображает сообщение об ошибке.

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница с формой входа
    """
    # Если уже авторизован — редирект
    if request.get("user"):
        raise web.HTTPFound("/dashboard")

    error = request.query.get("error", "")
    next_url = request.query.get("next", "/dashboard")
    csrf_token = get_csrf_token(request)

    context = {
        "error": error,
        "next_url": next_url,
        "csrf_token": csrf_token,
        "registration_open": Config.WEB_REGISTRATION_OPEN,
    }

    return aiohttp_jinja2.render_template("auth/login.html", request, context)


async def login_handler(request: Request) -> Response:
    """
    POST /auth/login — обработка формы входа.

    Порядок:
        1. Извлечение email и password из формы
        2. Поиск пользователя в БД по email
        3. Проверка пароля через bcrypt
        4. Создание сессии
        5. Установка cookie и редирект

    При ошибке — редирект на /auth/login?error=...

    Args:
        request: HTTP-запрос с данными формы

    Returns:
        Редирект на dashboard или обратно на login с ошибкой
    """
    from bot import storage

    try:
        data = await request.post()
    except Exception:
        raise web.HTTPFound("/auth/login?error=Некорректные данные формы")

    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    next_url = str(data.get("next", "/dashboard")).strip()

    # Валидация входных данных
    if not email or not password:
        raise web.HTTPFound("/auth/login?error=Заполните все поля")

    if len(email) > _MAX_EMAIL_LENGTH:
        raise web.HTTPFound("/auth/login?error=Некорректный email")

    # Безопасность: next_url только относительный путь
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/dashboard"

    # Поиск пользователя
    user = await storage.db.get_web_user_by_email(email)
    if not user:
        logger.warning("Неудачная попытка входа: email=%s (не найден)", email)
        raise web.HTTPFound("/auth/login?error=Неверный email или пароль")

    # Проверка активности
    if not user.get("is_active", True):
        logger.warning(
            "Попытка входа заблокированного пользователя: email=%s", email,
        )
        raise web.HTTPFound("/auth/login?error=Аккаунт заблокирован")

    # Проверка пароля
    password_valid = await PasswordHasher.verify_password(
        password, user["password_hash"],
    )
    if not password_valid:
        logger.warning("Неудачная попытка входа: email=%s (неверный пароль)", email)
        raise web.HTTPFound("/auth/login?error=Неверный email или пароль")

    # Создание сессии
    session_data = await WebSessionManager.create_session(
        web_user_id=user["id"],
        request=request,
    )

    # Обновление времени последнего входа
    await storage.db.update_web_user_last_login(user["id"])

    logger.info(
        "Успешный вход: email=%s, web_user_id=%d", email, user["id"],
    )

    # Редирект с установкой cookie
    response = web.HTTPFound(next_url)
    WebSessionManager.set_session_cookie(
        response,
        session_data["session_id"],
        session_data["expires_at"],
    )
    raise response


async def register_page(request: Request) -> Response:
    """
    GET /auth/register — отображение формы регистрации.

    Доступна только если WEB_REGISTRATION_OPEN=true в .env.
    Иначе — 404 (регистрация закрыта, аккаунты создаёт admin).

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница с формой регистрации
    """
    if not Config.WEB_REGISTRATION_OPEN:
        raise web.HTTPNotFound(reason="Регистрация закрыта")

    # Если уже авторизован — редирект
    if request.get("user"):
        raise web.HTTPFound("/dashboard")

    error = request.query.get("error", "")
    csrf_token = get_csrf_token(request)

    context = {
        "error": error,
        "csrf_token": csrf_token,
    }

    return aiohttp_jinja2.render_template("auth/register.html", request, context)


async def register_handler(request: Request) -> Response:
    """
    POST /auth/register — создание нового аккаунта.

    Доступно только если WEB_REGISTRATION_OPEN=true.
    Новый пользователь получает роль 'user'.

    Порядок:
        1. Валидация формы (email, password, display_name)
        2. Проверка уникальности email
        3. Хеширование пароля
        4. Создание записи в web_users
        5. Создание сессии и редирект

    Args:
        request: HTTP-запрос с данными формы

    Returns:
        Редирект на dashboard или обратно на register с ошибкой
    """
    from bot import storage

    if not Config.WEB_REGISTRATION_OPEN:
        raise web.HTTPNotFound(reason="Регистрация закрыта")

    try:
        data = await request.post()
    except Exception:
        raise web.HTTPFound("/auth/register?error=Некорректные данные формы")

    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    password_confirm = str(data.get("password_confirm", ""))
    display_name = str(data.get("display_name", "")).strip()

    # Валидация email
    if not email:
        raise web.HTTPFound("/auth/register?error=Введите email")

    if len(email) > _MAX_EMAIL_LENGTH:
        raise web.HTTPFound("/auth/register?error=Email слишком длинный")

    if "@" not in email or "." not in email:
        raise web.HTTPFound("/auth/register?error=Некорректный формат email")

    # Валидация пароля
    is_valid, error_msg = PasswordHasher.validate_password_strength(password)
    if not is_valid:
        raise web.HTTPFound(f"/auth/register?error={error_msg}")

    # Подтверждение пароля
    if password != password_confirm:
        raise web.HTTPFound("/auth/register?error=Пароли не совпадают")

    # Валидация display_name
    if display_name and len(display_name) > _MAX_DISPLAY_NAME_LENGTH:
        raise web.HTTPFound("/auth/register?error=Имя слишком длинное")

    # Хеширование пароля
    password_hash = await PasswordHasher.hash_password(password)

    # Создание пользователя
    user_id = await storage.db.create_web_user(
        email=email,
        password_hash=password_hash,
        display_name=display_name or None,
        role="user",
    )

    if user_id is None:
        raise web.HTTPFound(
            "/auth/register?error=Пользователь с таким email уже существует"
        )

    logger.info("Новый пользователь зарегистрирован: email=%s, id=%d", email, user_id)

    # Автоматический вход после регистрации
    session_data = await WebSessionManager.create_session(
        web_user_id=user_id,
        request=request,
    )

    response = web.HTTPFound("/dashboard")
    WebSessionManager.set_session_cookie(
        response,
        session_data["session_id"],
        session_data["expires_at"],
    )
    raise response


async def logout_handler(request: Request) -> Response:
    """
    POST /auth/logout — выход из системы.

    Удаляет сессию из БД и cookie из браузера.
    Работает даже если сессия уже истекла (идемпотентно).

    Args:
        request: HTTP-запрос

    Returns:
        Редирект на страницу входа
    """
    session_id = WebSessionManager.get_session_id_from_request(request)

    if session_id:
        await WebSessionManager.delete_session(session_id)
        logger.debug("Пользователь вышел (сессия: %s)", session_id[:8])

    response = web.HTTPFound("/auth/login")
    WebSessionManager.delete_session_cookie(response)
    raise response


def setup_auth_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты аутентификации.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/auth/login", login_page)
    app.router.add_post("/auth/login", login_handler)
    app.router.add_get("/auth/register", register_page)
    app.router.add_post("/auth/register", register_handler)
    app.router.add_post("/auth/logout", logout_handler)
