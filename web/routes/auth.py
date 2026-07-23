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

from aiohttp import web
from aiohttp.web import Request, Response

from config.config import Config
from web.auth.password import PasswordHasher
from web.auth.session import WebSessionManager
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

    html = _render_login_page(error=error, next_url=next_url)
    return Response(text=html, content_type="text/html")


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
    html = _render_register_page(error=error)
    return Response(text=html, content_type="text/html")


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


# ===================================================================
# Временные HTML-шаблоны (будут заменены на Jinja2 в Фазе 4)
# ===================================================================


def _render_login_page(error: str = "", next_url: str = "/dashboard") -> str:
    """Генерирует HTML страницы входа."""
    error_html = ""
    if error:
        error_html = f"""
        <div class="error-message">{error}</div>
        """

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход — Marketplace Sync</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f1f5f9;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 1rem;
        }}
        .auth-card {{
            background: white;
            border-radius: 1rem;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);
            padding: 2.5rem;
            width: 100%;
            max-width: 400px;
        }}
        .auth-title {{
            font-size: 1.5rem;
            font-weight: 700;
            color: #1e293b;
            text-align: center;
            margin-bottom: 0.5rem;
        }}
        .auth-subtitle {{
            font-size: 0.875rem;
            color: #64748b;
            text-align: center;
            margin-bottom: 2rem;
        }}
        .form-group {{
            margin-bottom: 1.25rem;
        }}
        .form-label {{
            display: block;
            font-size: 0.875rem;
            font-weight: 500;
            color: #374151;
            margin-bottom: 0.375rem;
        }}
        .form-input {{
            width: 100%;
            padding: 0.75rem 1rem;
            border: 1px solid #d1d5db;
            border-radius: 0.5rem;
            font-size: 1rem;
            transition: border-color 0.2s;
            outline: none;
        }}
        .form-input:focus {{
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59,130,246,0.1);
        }}
        .btn-primary {{
            width: 100%;
            padding: 0.75rem 1.5rem;
            background-color: #3b82f6;
            color: white;
            border: none;
            border-radius: 0.5rem;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.2s;
        }}
        .btn-primary:hover {{
            background-color: #2563eb;
        }}
        .error-message {{
            background-color: #fef2f2;
            border: 1px solid #fecaca;
            color: #dc2626;
            padding: 0.75rem 1rem;
            border-radius: 0.5rem;
            font-size: 0.875rem;
            margin-bottom: 1.25rem;
        }}
        .auth-footer {{
            text-align: center;
            margin-top: 1.5rem;
            font-size: 0.875rem;
            color: #64748b;
        }}
        .auth-footer a {{
            color: #3b82f6;
            text-decoration: none;
            font-weight: 500;
        }}
        .auth-footer a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="auth-card">
        <h1 class="auth-title">Marketplace Sync</h1>
        <p class="auth-subtitle">Войдите в свой аккаунт</p>
        {error_html}
        <form method="POST" action="/auth/login">
            <input type="hidden" name="next" value="{next_url}">
            <div class="form-group">
                <label class="form-label" for="email">Email</label>
                <input class="form-input" type="email" id="email" name="email"
                       required autocomplete="email" placeholder="user@example.com">
            </div>
            <div class="form-group">
                <label class="form-label" for="password">Пароль</label>
                <input class="form-input" type="password" id="password" name="password"
                       required autocomplete="current-password" placeholder="Минимум 8 символов">
            </div>
            <button type="submit" class="btn-primary">Войти</button>
        </form>
        <div class="auth-footer">
            <a href="/auth/register">Создать аккаунт</a>
        </div>
    </div>
</body>
</html>"""


def _render_register_page(error: str = "") -> str:
    """Генерирует HTML страницы регистрации."""
    error_html = ""
    if error:
        error_html = f"""
        <div class="error-message">{error}</div>
        """

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Регистрация — Marketplace Sync</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f1f5f9;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 1rem;
        }}
        .auth-card {{
            background: white;
            border-radius: 1rem;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);
            padding: 2.5rem;
            width: 100%;
            max-width: 400px;
        }}
        .auth-title {{
            font-size: 1.5rem;
            font-weight: 700;
            color: #1e293b;
            text-align: center;
            margin-bottom: 0.5rem;
        }}
        .auth-subtitle {{
            font-size: 0.875rem;
            color: #64748b;
            text-align: center;
            margin-bottom: 2rem;
        }}
        .form-group {{
            margin-bottom: 1.25rem;
        }}
        .form-label {{
            display: block;
            font-size: 0.875rem;
            font-weight: 500;
            color: #374151;
            margin-bottom: 0.375rem;
        }}
        .form-input {{
            width: 100%;
            padding: 0.75rem 1rem;
            border: 1px solid #d1d5db;
            border-radius: 0.5rem;
            font-size: 1rem;
            transition: border-color 0.2s;
            outline: none;
        }}
        .form-input:focus {{
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59,130,246,0.1);
        }}
        .btn-primary {{
            width: 100%;
            padding: 0.75rem 1.5rem;
            background-color: #3b82f6;
            color: white;
            border: none;
            border-radius: 0.5rem;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.2s;
        }}
        .btn-primary:hover {{
            background-color: #2563eb;
        }}
        .error-message {{
            background-color: #fef2f2;
            border: 1px solid #fecaca;
            color: #dc2626;
            padding: 0.75rem 1rem;
            border-radius: 0.5rem;
            font-size: 0.875rem;
            margin-bottom: 1.25rem;
        }}
        .auth-footer {{
            text-align: center;
            margin-top: 1.5rem;
            font-size: 0.875rem;
            color: #64748b;
        }}
        .auth-footer a {{
            color: #3b82f6;
            text-decoration: none;
            font-weight: 500;
        }}
        .auth-footer a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="auth-card">
        <h1 class="auth-title">Marketplace Sync</h1>
        <p class="auth-subtitle">Создайте новый аккаунт</p>
        {error_html}
        <form method="POST" action="/auth/register">
            <div class="form-group">
                <label class="form-label" for="email">Email</label>
                <input class="form-input" type="email" id="email" name="email"
                       required autocomplete="email" placeholder="user@example.com">
            </div>
            <div class="form-group">
                <label class="form-label" for="display_name">Имя (необязательно)</label>
                <input class="form-input" type="text" id="display_name" name="display_name"
                       autocomplete="name" placeholder="Как вас называть">
            </div>
            <div class="form-group">
                <label class="form-label" for="password">Пароль</label>
                <input class="form-input" type="password" id="password" name="password"
                       required autocomplete="new-password" placeholder="Минимум 8 символов">
            </div>
            <div class="form-group">
                <label class="form-label" for="password_confirm">Подтверждение пароля</label>
                <input class="form-input" type="password" id="password_confirm"
                       name="password_confirm" required autocomplete="new-password"
                       placeholder="Повторите пароль">
            </div>
            <button type="submit" class="btn-primary">Зарегистрироваться</button>
        </form>
        <div class="auth-footer">
            Уже есть аккаунт? <a href="/auth/login">Войти</a>
        </div>
    </div>
</body>
</html>"""
