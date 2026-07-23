"""
Маршруты для просмотра задач и скачивания результатов.

Эндпоинты:
    GET /tasks                          — список задач пользователя (HTML)
    GET /api/tasks/{id}/status          — JSON-статус задачи (polling fallback)
    GET /tasks/{id}/download/{filename} — скачивание файла результата

Поток:
    1. Пользователь запускает обработку → задача появляется в списке
    2. WebSocket показывает прогресс в real-time
    3. Если WebSocket недоступен — фронтенд polling через /api/tasks/{id}/status
    4. По завершении — кнопки скачивания для каждого файла

Безопасность:
    - Пользователь может скачивать только свои результаты
    - Путь к файлу проверяется на path traversal (../)
    - Файлы отдаются через aiohttp FileResponse (streaming)

Паттерн: Controller — HTTP → БД → ответ (JSON или файл).
"""

import os
from pathlib import Path

from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from web.auth.decorators import login_required
from web.middleware.csrf import get_csrf_token
from utils.logger_config import setup_logger

logger = setup_logger("web.routes.tasks")


@login_required
async def tasks_list(request: Request) -> Response:
    """
    GET /tasks — список задач пользователя.

    Отображает все задачи (от новых к старым) с их статусами,
    датами и кнопками скачивания для завершённых.

    Args:
        request: HTTP-запрос

    Returns:
        HTML-страница со списком задач
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]

    tasks = await storage.db.get_user_task_results(web_user_id, limit=50)
    csrf_token = get_csrf_token(request)

    html = _render_tasks_list(tasks=tasks, csrf_token=csrf_token)
    return Response(text=html, content_type="text/html")


@login_required
async def task_status_api(request: Request) -> Response:
    """
    GET /api/tasks/{id}/status — JSON-статус задачи.

    Polling fallback: если WebSocket недоступен, фронтенд
    опрашивает этот эндпоинт каждые 3 секунды.

    Возвращает:
        - status: pending | processing | completed | failed
        - output_files: {filename: path} (при completed)
        - error_message: str (при failed)
        - stats: {} (при completed)

    Args:
        request: HTTP-запрос

    Returns:
        JSON с текущим статусом задачи
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]
    task_id = request.match_info["id"]

    task_result = await storage.db.get_task_result(task_id)

    if not task_result:
        return web.json_response(
            {"error": "Задача не найдена"}, status=404,
        )

    # Проверяем принадлежность задачи пользователю
    if task_result["web_user_id"] != web_user_id:
        return web.json_response(
            {"error": "Нет доступа к этой задаче"}, status=403,
        )

    # Формируем ответ
    response_data = {
        "task_id": task_result["task_id"],
        "status": task_result["status"],
        "created_at": task_result["created_at"],
        "completed_at": task_result["completed_at"],
    }

    if task_result["status"] == "completed":
        # Добавляем список файлов для скачивания
        output_files = task_result.get("output_files") or {}
        response_data["files"] = [
            {
                "filename": filename,
                "download_url": f"/tasks/{task_id}/download/{filename}",
            }
            for filename in output_files.keys()
        ]
        response_data["stats"] = task_result.get("stats")

    elif task_result["status"] == "failed":
        response_data["error_message"] = task_result.get("error_message", "")

    return web.json_response(response_data)


@login_required
async def task_download(request: Request) -> Response:
    """
    GET /tasks/{id}/download/{filename} — скачивание файла результата.

    Безопасность:
        - Проверяет принадлежность задачи пользователю
        - Проверяет, что filename есть в output_files задачи
        - Защита от path traversal (../ в filename)
        - Файл отдаётся через FileResponse (streaming, не в память)

    Args:
        request: HTTP-запрос

    Returns:
        FileResponse с файлом или 404/403
    """
    user_data = request["user"]
    web_user_id = user_data["web_user_id"]
    task_id = request.match_info["id"]
    filename = request.match_info["filename"]

    # Защита от path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise web.HTTPBadRequest(reason="Некорректное имя файла")

    # Получаем результат задачи
    task_result = await storage.db.get_task_result(task_id)

    if not task_result:
        raise web.HTTPNotFound(reason="Задача не найдена")

    # Проверяем принадлежность
    if task_result["web_user_id"] != web_user_id:
        raise web.HTTPForbidden(reason="Нет доступа к этой задаче")

    # Проверяем статус
    if task_result["status"] != "completed":
        raise web.HTTPNotFound(reason="Результаты ещё не готовы")

    # Проверяем наличие файла в output_files
    output_files = task_result.get("output_files") or {}
    file_path = output_files.get(filename)

    if not file_path:
        raise web.HTTPNotFound(reason="Файл не найден в результатах задачи")

    # Проверяем существование файла на диске
    file_path_obj = Path(file_path)
    if not file_path_obj.exists() or not file_path_obj.is_file():
        logger.error(
            "Файл результата не найден на диске: %s (task_id=%s)",
            file_path, task_id,
        )
        raise web.HTTPNotFound(reason="Файл не найден на диске (возможно, удалён уборщиком)")

    logger.info(
        "Скачивание файла: %s (task_id=%s, web_user_id=%d)",
        filename, task_id, web_user_id,
    )

    return web.FileResponse(
        path=file_path_obj,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def setup_tasks_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты задач.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/tasks", tasks_list)
    app.router.add_get("/api/tasks/{id}/status", task_status_api)
    app.router.add_get("/tasks/{id}/download/{filename:.+}", task_download)


# ===================================================================
# Временный HTML-шаблон (будет заменён на Jinja2 в Фазе 4)
# ===================================================================


def _render_tasks_list(tasks: list, csrf_token: str) -> str:
    """Генерирует HTML списка задач."""

    rows_html = ""
    if tasks:
        for task in tasks:
            status = task["status"]
            status_badge = _get_status_badge(status)
            created = task.get("created_at", "")[:16] if task.get("created_at") else "—"
            completed = task.get("completed_at", "")[:16] if task.get("completed_at") else "—"

            # Кнопки скачивания для завершённых задач
            actions_html = ""
            if status == "completed":
                output_files = task.get("output_files") or {}
                for filename in output_files.keys():
                    short_name = _shorten_filename(filename)
                    actions_html += (
                        f'<a href="/tasks/{task["task_id"]}/download/{filename}" '
                        f'class="btn-download">{short_name}</a> '
                    )
            elif status == "failed":
                error = task.get("error_message", "")
                if error:
                    actions_html = f'<span class="error-text" title="{error}">{error[:50]}</span>'
            elif status in ("pending", "processing"):
                actions_html = (
                    f'<span class="progress-text" id="progress-{task["task_id"]}">'
                    f'Ожидание...</span>'
                )

            rows_html += f"""
            <tr id="task-row-{task['task_id']}">
                <td>{created}</td>
                <td>{status_badge}</td>
                <td>{completed}</td>
                <td class="actions-cell">{actions_html}</td>
            </tr>
            """
    else:
        rows_html = """
        <tr>
            <td colspan="4" class="text-center text-muted">
                Нет задач. <a href="/upload">Загрузите файлы</a> для начала обработки.
            </td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Задачи — Marketplace Sync</title>
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
        .container {{ max-width: 1100px; margin: 0 auto; padding: 2rem; }}
        .page-title {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 1.5rem; }}
        .table-container {{
            background: white; border-radius: 0.75rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden;
        }}
        .table {{ width: 100%; border-collapse: collapse; }}
        .table th, .table td {{
            text-align: left; padding: 0.875rem 1rem;
            border-bottom: 1px solid #e2e8f0; font-size: 0.875rem;
        }}
        .table th {{ font-weight: 600; color: #64748b; background: #f8fafc; }}
        .table tr:hover {{ background: #f8fafc; }}
        .badge {{
            display: inline-block; padding: 0.25rem 0.5rem; border-radius: 0.25rem;
            font-size: 0.75rem; font-weight: 600;
        }}
        .badge-success {{ background: #dcfce7; color: #166534; }}
        .badge-error {{ background: #fef2f2; color: #dc2626; }}
        .badge-pending {{ background: #fef3c7; color: #92400e; }}
        .badge-processing {{ background: #dbeafe; color: #1e40af; }}
        .btn-download {{
            display: inline-block; padding: 0.25rem 0.5rem;
            background: #eff6ff; color: #3b82f6; border-radius: 0.25rem;
            text-decoration: none; font-size: 0.75rem; font-weight: 500;
            margin-right: 0.25rem; margin-bottom: 0.25rem;
        }}
        .btn-download:hover {{ background: #dbeafe; }}
        .error-text {{ color: #dc2626; font-size: 0.75rem; }}
        .progress-text {{ color: #1e40af; font-size: 0.75rem; font-style: italic; }}
        .actions-cell {{ max-width: 300px; }}
        .text-center {{ text-align: center; }}
        .text-muted {{ color: #64748b; padding: 2rem !important; }}
        .text-muted a {{ color: #3b82f6; text-decoration: none; }}
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
        <h1 class="page-title">Мои задачи</h1>

        <div class="table-container">
            <table class="table">
                <thead>
                    <tr>
                        <th>Создана</th>
                        <th>Статус</th>
                        <th>Завершена</th>
                        <th>Файлы / Детали</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <script>
    // Polling для задач в обработке (fallback если WebSocket недоступен)
    const pendingTasks = document.querySelectorAll('[id^="progress-"]');
    if (pendingTasks.length > 0) {{
        setInterval(async () => {{
            for (const el of pendingTasks) {{
                const taskId = el.id.replace('progress-', '');
                try {{
                    const resp = await fetch(`/api/tasks/${{taskId}}/status`, {{
                        headers: {{ 'Accept': 'application/json' }},
                    }});
                    const data = await resp.json();
                    if (data.status === 'completed' || data.status === 'failed') {{
                        location.reload();
                    }}
                }} catch (e) {{ /* ignore */ }}
            }}
        }}, 5000);
    }}
    </script>
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


def _shorten_filename(filename: str) -> str:
    """Сокращает имя файла для отображения в кнопке."""
    if "WB" in filename or "wildberries" in filename.lower():
        return "📥 WB"
    if "Ozon" in filename or "ozon" in filename.lower():
        return "📥 Ozon"
    if "Яндекс" in filename or "yandex" in filename.lower():
        return "📥 Яндекс"
    if "Отчёт" in filename or "report" in filename.lower():
        return "📥 Отчёт"
    return f"📥 {filename[:20]}"
