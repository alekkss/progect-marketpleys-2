"""
Маршрут главной панели (dashboard).

GET /dashboard — отображает:
    - Приветствие с именем пользователя
    - Статистику: количество схем, обработок, последние задачи
    - Быстрые действия: загрузить файлы, создать схему, управление

Доступ: @login_required (любой авторизованный пользователь).

Паттерн: Controller — собирает данные из БД и передаёт в шаблон.
"""

from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from web.auth.decorators import login_required
from web.auth.permissions import WebAccessManager
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.dashboard")


@login_required
async def dashboard_page(request: Request) -> Response:
    """
    GET /dashboard — главная панель пользователя.

    Собирает:
        - Данные пользователя из request["user"]
        - Количество схем (свои или все, зависит от роли)
        - Последние задачи (из task_results)
        - Статистику обработок (из processing_history через telegram_user_id)

    Args:
        request: HTTP-запрос (request["user"] заполнен auth middleware)

    Returns:
        HTML-страница dashboard
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]
    role = user_data.get("role", "user")
    display_name = user_data.get("display_name") or user_data.get("email", "")
    telegram_user_id = user_data.get("telegram_user_id")

    # Получаем количество схем
    schemas_count = await _get_schemas_count(user_data)

    # Получаем последние задачи
    recent_tasks = await storage.db.get_user_task_results(web_user_id, limit=5)

    # Статистика обработок (если привязан Telegram)
    processing_stats = None
    if telegram_user_id:
        processing_stats = await storage.db.get_user_stats(telegram_user_id)

    # Считаем задачи по статусам
    completed_tasks = sum(1 for t in recent_tasks if t["status"] == "completed")
    failed_tasks = sum(1 for t in recent_tasks if t["status"] == "failed")
    pending_tasks = sum(1 for t in recent_tasks if t["status"] in ("pending", "processing"))

    csrf_token = get_csrf_token(request)
    can_manage = WebAccessManager.can_manage_users(user_data)

    html = _render_dashboard(
        display_name=display_name,
        role=role,
        schemas_count=schemas_count,
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
        pending_tasks=pending_tasks,
        recent_tasks=recent_tasks,
        processing_stats=processing_stats,
        can_manage=can_manage,
        csrf_token=csrf_token,
    )
    return Response(text=html, content_type="text/html")


async def _get_schemas_count(user_data: dict) -> int:
    """
    Получает количество схем, доступных пользователю.

    Owner/admin/editor видят все схемы, user — только свои.

    Args:
        user_data: Данные пользователя из request["user"]

    Returns:
        Количество доступных схем
    """
    can_see_all = WebAccessManager.can_see_all_schemas(user_data)
    telegram_user_id = user_data.get("telegram_user_id")

    if can_see_all:
        schemas = await storage.db.get_user_schemas(0, all_schemas=True)
    elif telegram_user_id:
        schemas = await storage.db.get_user_schemas(telegram_user_id, all_schemas=False)
    else:
        schemas = []

    return len(schemas)


def setup_dashboard_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты dashboard.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/dashboard", dashboard_page)


# ===================================================================
# Временный HTML-шаблон (будет заменён на Jinja2 в Фазе 4)
# ===================================================================


def _render_dashboard(
    display_name: str,
    role: str,
    schemas_count: int,
    completed_tasks: int,
    failed_tasks: int,
    pending_tasks: int,
    recent_tasks: list,
    processing_stats: dict | None,
    can_manage: bool,
    csrf_token: str,
) -> str:
    """Генерирует HTML страницы dashboard."""

    # Статистика обработок
    stats_html = ""
    if processing_stats:
        stats_html = f"""
        <div class="stat-card">
            <div class="stat-value">{processing_stats.get('total_processings', 0)}</div>
            <div class="stat-label">Всего обработок</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{processing_stats.get('total_synced_cells', 0)}</div>
            <div class="stat-label">Синхронизировано ячеек</div>
        </div>
        """

    # Последние задачи
    tasks_html = ""
    if recent_tasks:
        tasks_rows = ""
        for task in recent_tasks[:5]:
            status = task["status"]
            status_badge = _get_status_badge(status)
            created = task.get("created_at", "")[:16] if task.get("created_at") else "—"
            tasks_rows += f"""
            <tr>
                <td>{created}</td>
                <td>{status_badge}</td>
                <td>{task.get('error_message', '') if status == 'failed' else '—'}</td>
            </tr>
            """
        tasks_html = f"""
        <div class="section">
            <h2 class="section-title">Последние задачи</h2>
            <table class="table">
                <thead>
                    <tr><th>Дата</th><th>Статус</th><th>Детали</th></tr>
                </thead>
                <tbody>{tasks_rows}</tbody>
            </table>
            <a href="/tasks" class="link">Все задачи →</a>
        </div>
        """
    else:
        tasks_html = """
        <div class="section">
            <h2 class="section-title">Последние задачи</h2>
            <p class="text-muted">Пока нет обработанных задач</p>
        </div>
        """

    # Кнопка администрирования
    admin_btn = ""
    if can_manage:
        admin_btn = '<a href="/admin/users" class="action-card action-admin">👥 Управление пользователями</a>'

    role_display = {"owner": "👑 Владелец", "admin": "👨‍💼 Администратор", "editor": "✏️ Редактор", "user": "👤 Пользователь"}.get(role, role)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard — Marketplace Sync</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f1f5f9;
            color: #1e293b;
            line-height: 1.6;
        }}
        .navbar {{
            background: white;
            border-bottom: 1px solid #e2e8f0;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .navbar-brand {{ font-weight: 700; font-size: 1.25rem; color: #3b82f6; }}
        .navbar-user {{ display: flex; align-items: center; gap: 1rem; font-size: 0.875rem; color: #64748b; }}
        .navbar-user form {{ display: inline; }}
        .btn-logout {{
            background: none; border: 1px solid #e2e8f0; padding: 0.375rem 0.75rem;
            border-radius: 0.375rem; cursor: pointer; font-size: 0.875rem; color: #64748b;
        }}
        .btn-logout:hover {{ background: #f8fafc; color: #ef4444; border-color: #ef4444; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 2rem; }}
        .welcome {{ margin-bottom: 2rem; }}
        .welcome h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 0.25rem; }}
        .welcome .role {{ font-size: 0.875rem; color: #64748b; }}
        .stats-grid {{
            display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem; margin-bottom: 2rem;
        }}
        .stat-card {{
            background: white; border-radius: 0.75rem; padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .stat-value {{ font-size: 2rem; font-weight: 700; color: #3b82f6; }}
        .stat-label {{ font-size: 0.875rem; color: #64748b; margin-top: 0.25rem; }}
        .actions-grid {{
            display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1rem; margin-bottom: 2rem;
        }}
        .action-card {{
            display: block; background: white; border-radius: 0.75rem; padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-decoration: none;
            color: #1e293b; font-weight: 600; font-size: 1.1rem;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .action-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
        .action-admin {{ border-left: 4px solid #f59e0b; }}
        .section {{ background: white; border-radius: 0.75rem; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1.5rem; }}
        .section-title {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 1rem; }}
        .table {{ width: 100%; border-collapse: collapse; }}
        .table th, .table td {{ text-align: left; padding: 0.75rem; border-bottom: 1px solid #e2e8f0; font-size: 0.875rem; }}
        .table th {{ font-weight: 600; color: #64748b; }}
        .badge {{
            display: inline-block; padding: 0.25rem 0.5rem; border-radius: 0.25rem;
            font-size: 0.75rem; font-weight: 600;
        }}
        .badge-success {{ background: #dcfce7; color: #166534; }}
        .badge-error {{ background: #fef2f2; color: #dc2626; }}
        .badge-pending {{ background: #fef3c7; color: #92400e; }}
        .badge-processing {{ background: #dbeafe; color: #1e40af; }}
        .link {{ color: #3b82f6; text-decoration: none; font-size: 0.875rem; font-weight: 500; }}
        .link:hover {{ text-decoration: underline; }}
        .text-muted {{ color: #64748b; font-size: 0.875rem; }}
    </style>
</head>
<body>
    <nav class="navbar">
        <div class="navbar-brand">Marketplace Sync</div>
        <div class="navbar-user">
            <span>{display_name}</span>
            <form method="POST" action="/auth/logout">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <button type="submit" class="btn-logout">Выйти</button>
            </form>
        </div>
    </nav>

    <div class="container">
        <div class="welcome">
            <h1>Добро пожаловать, {display_name}!</h1>
            <span class="role">{role_display}</span>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">{schemas_count}</div>
                <div class="stat-label">Схем сопоставлений</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{completed_tasks}</div>
                <div class="stat-label">Выполнено задач</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{pending_tasks}</div>
                <div class="stat-label">В обработке</div>
            </div>
            {stats_html}
        </div>

        <div class="actions-grid">
            <a href="/upload" class="action-card">📤 Загрузить файлы</a>
            <a href="/schemas" class="action-card">📋 Схемы сопоставлений</a>
            <a href="/tasks" class="action-card">📊 Мои задачи</a>
            {admin_btn}
        </div>

        {tasks_html}
    </div>
</body>
</html>"""


def _get_status_badge(status: str) -> str:
    """Возвращает HTML-badge для статуса задачи."""
    badges = {
        "completed": '<span class="badge badge-success">✅ Выполнено</span>',
        "failed": '<span class="badge badge-error">❌ Ошибка</span>',
        "pending": '<span class="badge badge-pending">⏳ В очереди</span>',
        "processing": '<span class="badge badge-processing">🔄 Обработка</span>',
    }
    return badges.get(status, f'<span class="badge">{status}</span>')
