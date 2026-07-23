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

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница управления пользователями
    """
    user_data = request["user"]
    users = await storage.db.get_web_users_list(limit=100)
    csrf_token = get_csrf_token(request)
    is_owner = WebAccessManager.is_owner(user_data)

    html = _render_admin_users(
        users=users,
        is_owner=is_owner,
        csrf_token=csrf_token,
    )
    return Response(text=html, content_type="text/html")


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


# ===================================================================
# Временный HTML-шаблон (будет заменён на Jinja2 в Фазе 4)
# ===================================================================


def _render_admin_users(
    users: list,
    is_owner: bool,
    csrf_token: str,
) -> str:
    """Генерирует HTML admin-панели."""

    # Таблица пользователей
    rows_html = ""
    for user in users:
        role_badge = _get_role_badge(user["role"])
        status_badge = (
            '<span class="badge badge-success">Активен</span>'
            if user["is_active"]
            else '<span class="badge badge-error">Заблокирован</span>'
        )
        last_login = user.get("last_login_at", "")[:16] if user.get("last_login_at") else "—"
        telegram = user.get("telegram_user_id") or "—"

        # Кнопка блокировки (не для owner)
        toggle_html = ""
        if user["role"] != "owner":
            btn_text = "Разблокировать" if not user["is_active"] else "Заблокировать"
            btn_class = "btn-unblock" if not user["is_active"] else "btn-block"
            toggle_html = f"""
            <form method="POST" action="/admin/users/toggle" style="display:inline">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <input type="hidden" name="user_id" value="{user['id']}">
                <button type="submit" class="btn-small {btn_class}">{btn_text}</button>
            </form>
            """

        # Селект роли (не для owner)
        role_select_html = ""
        if user["role"] != "owner":
            options = '<option value="user"' + (' selected' if user["role"] == "user" else '') + '>user</option>'
            options += '<option value="editor"' + (' selected' if user["role"] == "editor" else '') + '>editor</option>'
            if is_owner:
                options += '<option value="admin"' + (' selected' if user["role"] == "admin" else '') + '>admin</option>'

            role_select_html = f"""
            <form method="POST" action="/admin/users/role" style="display:inline">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <input type="hidden" name="user_id" value="{user['id']}">
                <select name="role" class="role-select" onchange="this.form.submit()">
                    {options}
                </select>
            </form>
            """
        else:
            role_select_html = '<span class="role-fixed">owner</span>'

        rows_html += f"""
        <tr>
            <td>{user['email']}</td>
            <td>{user.get('display_name') or '—'}</td>
            <td>{role_select_html}</td>
            <td>{status_badge}</td>
            <td>{telegram}</td>
            <td>{last_login}</td>
            <td>{toggle_html}</td>
        </tr>
        """

    # Форма добавления пользователя
    role_options = """
        <option value="user">user</option>
        <option value="editor">editor</option>
    """
    if is_owner:
        role_options += '<option value="admin">admin</option>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Управление пользователями — Marketplace Sync</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f1f5f9; color: #1e293b; line-height: 1.6;
        }}
        .navbar {{
            background: white; border-bottom: 1px solid #e2e8f0;
            padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center;
        }}
        .navbar-brand {{ font-weight: 700; font-size: 1.25rem; color: #3b82f6; text-decoration: none; }}
        .navbar-nav {{ display: flex; gap: 1.5rem; }}
        .navbar-nav a {{ color: #64748b; text-decoration: none; font-size: 0.875rem; font-weight: 500; }}
        .navbar-nav a:hover {{ color: #3b82f6; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 2rem; }}
        .page-title {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 1.5rem; }}
        .card {{
            background: white; border-radius: 0.75rem; padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1.5rem;
        }}
        .card-title {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 1rem; }}
        .form-row {{ display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; }}
        .form-group {{ flex: 1; min-width: 150px; }}
        .form-label {{ display: block; font-size: 0.75rem; font-weight: 500; color: #64748b; margin-bottom: 0.25rem; }}
        .form-input, .form-select {{
            width: 100%; padding: 0.5rem 0.75rem; border: 1px solid #d1d5db;
            border-radius: 0.375rem; font-size: 0.875rem; outline: none;
        }}
        .form-input:focus, .form-select:focus {{ border-color: #3b82f6; }}
        .btn-add {{
            padding: 0.5rem 1rem; background: #3b82f6; color: white;
            border: none; border-radius: 0.375rem; font-size: 0.875rem;
            font-weight: 600; cursor: pointer; white-space: nowrap;
        }}
        .btn-add:hover {{ background: #2563eb; }}
        .table-container {{ overflow-x: auto; }}
        .table {{ width: 100%; border-collapse: collapse; }}
        .table th, .table td {{
            text-align: left; padding: 0.75rem 0.5rem;
            border-bottom: 1px solid #e2e8f0; font-size: 0.8125rem;
        }}
        .table th {{ font-weight: 600; color: #64748b; background: #f8fafc; }}
        .badge {{
            display: inline-block; padding: 0.125rem 0.5rem; border-radius: 0.25rem;
            font-size: 0.6875rem; font-weight: 600;
        }}
        .badge-success {{ background: #dcfce7; color: #166534; }}
        .badge-error {{ background: #fef2f2; color: #dc2626; }}
        .badge-owner {{ background: #fef3c7; color: #92400e; }}
        .badge-admin {{ background: #dbeafe; color: #1e40af; }}
        .badge-editor {{ background: #f3e8ff; color: #6b21a8; }}
        .badge-user {{ background: #f1f5f9; color: #475569; }}
        .btn-small {{
            padding: 0.25rem 0.5rem; border: 1px solid #d1d5db;
            border-radius: 0.25rem; font-size: 0.75rem; cursor: pointer; background: white;
        }}
        .btn-block {{ color: #dc2626; }}
        .btn-block:hover {{ background: #fef2f2; border-color: #dc2626; }}
        .btn-unblock {{ color: #166534; }}
        .btn-unblock:hover {{ background: #dcfce7; border-color: #166534; }}
        .role-select {{
            padding: 0.25rem 0.375rem; border: 1px solid #d1d5db;
            border-radius: 0.25rem; font-size: 0.75rem;
        }}
        .role-fixed {{ font-size: 0.75rem; color: #92400e; font-weight: 600; }}
        .alert {{
            padding: 0.75rem 1rem; border-radius: 0.5rem; margin-bottom: 1rem;
            font-size: 0.875rem;
        }}
        .alert-success {{ background: #dcfce7; color: #166534; }}
        .alert-error {{ background: #fef2f2; color: #dc2626; }}
    </style>
</head>
<body>
    <nav class="navbar">
        <a href="/dashboard" class="navbar-brand">Marketplace Sync</a>
        <div class="navbar-nav">
            <a href="/dashboard">Dashboard</a>
            <a href="/schemas">Схемы</a>
            <a href="/upload">Загрузка</a>
            <a href="/tasks">Задачи</a>
        </div>
    </nav>

    <div class="container">
        <h1 class="page-title">👥 Управление пользователями</h1>

        <script>
        // Показ уведомлений из query params
        const params = new URLSearchParams(window.location.search);
        const container = document.currentScript.parentElement;
        if (params.get('success')) {{
            container.insertAdjacentHTML('beforeend',
                `<div class="alert alert-success">${{params.get('success')}}</div>`);
        }}
        if (params.get('error')) {{
            container.insertAdjacentHTML('beforeend',
                `<div class="alert alert-error">${{params.get('error')}}</div>`);
        }}
        </script>

        <div class="card">
            <h2 class="card-title">Добавить пользователя</h2>
            <form method="POST" action="/admin/users/add">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">Email</label>
                        <input class="form-input" type="email" name="email" required placeholder="user@example.com">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Имя</label>
                        <input class="form-input" type="text" name="display_name" placeholder="Необязательно">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Пароль</label>
                        <input class="form-input" type="password" name="password" required placeholder="Мин. 8 символов">
                    </div>
                    <div class="form-group" style="max-width:120px">
                        <label class="form-label">Роль</label>
                        <select class="form-select" name="role">{role_options}</select>
                    </div>
                    <div>
                        <button type="submit" class="btn-add">+ Добавить</button>
                    </div>
                </div>
            </form>
        </div>

        <div class="card">
            <h2 class="card-title">Пользователи ({len(users)})</h2>
            <div class="table-container">
                <table class="table">
                    <thead>
                        <tr>
                            <th>Email</th>
                            <th>Имя</th>
                            <th>Роль</th>
                            <th>Статус</th>
                            <th>Telegram</th>
                            <th>Последний вход</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>"""


def _get_role_badge(role: str) -> str:
    """Возвращает HTML-badge для роли."""
    badges = {
        "owner": '<span class="badge badge-owner">👑 owner</span>',
        "admin": '<span class="badge badge-admin">👨‍💼 admin</span>',
        "editor": '<span class="badge badge-editor">✏️ editor</span>',
        "user": '<span class="badge badge-user">👤 user</span>',
    }
    return badges.get(role, f'<span class="badge">{role}</span>')
