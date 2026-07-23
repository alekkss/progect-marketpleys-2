"""
Маршруты администрирования веб-пользователей.

Эндпоинты:
    GET  /admin/users       — список веб-пользователей
    POST /admin/users/add   — создание нового аккаунта
    POST /admin/users/toggle — блокировка/разблокировка пользователя
    POST /admin/users/role  — изменение роли пользователя

Доступ: @admin_required (только owner и admin).

Регистрация по умолчанию закрыта (WEB_REGISTRATION_OPEN=false).
Создание аккаунтов — через эту панель. Admin может создать
пользователя с ролью user или editor. Роли owner и admin
назначаются только owner.

Паттерн: Controller — HTTP → валидация → БД → ответ.
"""

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from web.auth.decorators import admin_required
from web.auth.password import PasswordHasher
from web.auth.permissions import WebAccessManager
from web.auth.session import WebSessionManager
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.admin")


@admin_required
async def admin_users_page(request: Request) -> Response:
    """
    GET /admin/users — список веб-пользователей.

    Отображает все аккаунты с их ролями, статусом активности,
    датами регистрации и последнего входа.

    Читает flash-сообщения из query params:
        ?success=Текст — зелёное уведомление
        ?error=Текст — красное уведомление

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница управления пользователями
    """
    user_data = request["user"]
    users = await storage.db.get_web_users_list(limit=100)
    csrf_token = get_csrf_token(request)
    is_owner = WebAccessManager.is_owner(user_data)

    # Flash-сообщения из query params (после редиректов)
    success_message = request.query.get("success", "")
    error_message = request.query.get("error", "")

    context = {
        "users": users,
        "is_owner": is_owner,
        "csrf_token": csrf_token,
        "user": user_data,
        "success_message": success_message,
        "error_message": error_message,
    }

    return aiohttp_jinja2.render_template("admin/users.html", request, context)


@admin_required
async def admin_add_user(request: Request) -> Response:
    """
    POST /admin/users/add — создание нового веб-аккаунта.

    Форма:
        - email: Email нового пользователя
        - display_name: Отображаемое имя (опционально)
        - password: Начальный пароль
        - role: Роль (user, editor; admin — только для owner)

    Args:
        request: HTTP-запрос с данными формы

    Returns:
        Редирект на /admin/users с результатом
    """
    user_data = request["user"]
    is_owner = WebAccessManager.is_owner(user_data)

    try:
        data = await request.post()
    except Exception:
        raise web.HTTPFound("/admin/users?error=Некорректные данные формы")

    email = str(data.get("email", "")).strip().lower()
    display_name = str(data.get("display_name", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "user")).strip()

    # Валидация email
    if not email or "@" not in email or "." not in email:
        raise web.HTTPFound("/admin/users?error=Некорректный email")

    if len(email) > 255:
        raise web.HTTPFound("/admin/users?error=Email слишком длинный")

    # Валидация пароля
    is_valid, error_msg = PasswordHasher.validate_password_strength(password)
    if not is_valid:
        raise web.HTTPFound(f"/admin/users?error={error_msg}")

    # Валидация роли
    allowed_roles = {"user", "editor"}
    if is_owner:
        allowed_roles.add("admin")

    if role not in allowed_roles:
        raise web.HTTPFound("/admin/users?error=Недопустимая роль")

    # Хеширование пароля
    password_hash = await PasswordHasher.hash_password(password)

    # Создание пользователя
    user_id = await storage.db.create_web_user(
        email=email,
        password_hash=password_hash,
        display_name=display_name or None,
        role=role,
    )

    if user_id is None:
        raise web.HTTPFound(
            "/admin/users?error=Пользователь с таким email уже существует"
        )

    logger.info(
        "Admin создал пользователя: email=%s, role=%s, created_by=web_user_id:%d",
        email, role, user_data.get("web_user_id", 0),
    )

    raise web.HTTPFound("/admin/users?success=Пользователь создан")


@admin_required
async def admin_toggle_user(request: Request) -> Response:
    """
    POST /admin/users/toggle — блокировка/разблокировка пользователя.

    Блокировка:
        - Деактивирует аккаунт (is_active=False)
        - Удаляет все активные сессии (принудительный logout)

    Ограничения:
        - Нельзя заблокировать owner
        - Admin не может заблокировать другого admin

    Args:
        request: HTTP-запрос с данными формы

    Returns:
        Редирект на /admin/users
    """
    user_data = request["user"]
    is_owner = WebAccessManager.is_owner(user_data)

    try:
        data = await request.post()
    except Exception:
        raise web.HTTPFound("/admin/users?error=Некорректные данные")

    target_user_id = int(data.get("user_id", 0))
    if not target_user_id:
        raise web.HTTPFound("/admin/users?error=Не указан ID пользователя")

    # Загружаем целевого пользователя
    target_user = await storage.db.get_web_user_by_id(target_user_id)
    if not target_user:
        raise web.HTTPFound("/admin/users?error=Пользователь не найден")

    # Защита: нельзя блокировать owner
    if target_user["role"] == "owner":
        raise web.HTTPFound("/admin/users?error=Нельзя заблокировать владельца")

    # Защита: admin не может блокировать admin
    if target_user["role"] == "admin" and not is_owner:
        raise web.HTTPFound("/admin/users?error=Только владелец может блокировать администратора")

    # Переключаем статус
    new_status = not target_user["is_active"]
    await storage.db.set_web_user_active(target_user_id, new_status)

    # При блокировке — удаляем все сессии
    if not new_status:
        await WebSessionManager.delete_all_user_sessions(target_user_id)

    action = "разблокирован" if new_status else "заблокирован"
    logger.info(
        "Admin %s пользователя: web_user_id=%d, email=%s",
        action, target_user_id, target_user["email"],
    )

    raise web.HTTPFound(f"/admin/users?success=Пользователь {action}")


@admin_required
async def admin_change_role(request: Request) -> Response:
    """
    POST /admin/users/role — изменение роли пользователя.

    Ограничения:
        - Admin может назначать: user, editor
        - Owner может назначать: user, editor, admin
        - Нельзя изменить роль owner
        - Нельзя изменить свою роль

    При смене роли сессии НЕ удаляются — новая роль
    подхватится при следующем запросе (auth middleware
    читает роль из web_users при проверке сессии).

    Args:
        request: HTTP-запрос с данными формы

    Returns:
        Редирект на /admin/users
    """
    user_data = request["user"]
    is_owner = WebAccessManager.is_owner(user_data)
    current_web_user_id = user_data.get("web_user_id", 0)

    try:
        data = await request.post()
    except Exception:
        raise web.HTTPFound("/admin/users?error=Некорректные данные")

    target_user_id = int(data.get("user_id", 0))
    new_role = str(data.get("role", "")).strip()

    if not target_user_id or not new_role:
        raise web.HTTPFound("/admin/users?error=Не указаны ID или роль")

    # Нельзя менять свою роль
    if target_user_id == current_web_user_id:
        raise web.HTTPFound("/admin/users?error=Нельзя изменить свою роль")

    # Загружаем целевого пользователя
    target_user = await storage.db.get_web_user_by_id(target_user_id)
    if not target_user:
        raise web.HTTPFound("/admin/users?error=Пользователь не найден")

    # Нельзя менять роль owner
    if target_user["role"] == "owner":
        raise web.HTTPFound("/admin/users?error=Нельзя изменить роль владельца")

    # Валидация новой роли
    allowed_roles = {"user", "editor"}
    if is_owner:
        allowed_roles.add("admin")

    if new_role not in allowed_roles:
        raise web.HTTPFound("/admin/users?error=Недопустимая роль")

    # Обновляем роль
    async with storage.db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE web_users SET role = $1 WHERE id = $2",
            new_role, target_user_id,
        )

    logger.info(
        "Admin изменил роль: web_user_id=%d, %s → %s, changed_by=%d",
        target_user_id, target_user["role"], new_role, current_web_user_id,
    )

    raise web.HTTPFound("/admin/users?success=Роль изменена")


def setup_admin_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты администрирования.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/admin/users", admin_users_page)
    app.router.add_post("/admin/users/add", admin_add_user)
    app.router.add_post("/admin/users/toggle", admin_toggle_user)
    app.router.add_post("/admin/users/role", admin_change_role)
