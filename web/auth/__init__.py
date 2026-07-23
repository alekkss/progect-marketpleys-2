"""
Пакет аутентификации веб-приложения.

Компоненты:
    - password.py    — хеширование и проверка паролей (bcrypt)
    - session.py     — управление cookie-сессиями (создание, валидация, удаление)
    - permissions.py — проверка ролей веб-пользователей (адаптер)
    - decorators.py  — декораторы защиты маршрутов (@login_required, @admin_required)

Использование в маршрутах:
    from web.auth import PasswordHasher, WebSessionManager, login_required

    @login_required
    async def dashboard(request):
        user = request["user"]
        ...
"""

from web.auth.password import PasswordHasher
from web.auth.session import WebSessionManager
from web.auth.decorators import login_required, admin_required, editor_required
from web.auth.permissions import WebAccessManager

__all__ = [
    "PasswordHasher",
    "WebSessionManager",
    "WebAccessManager",
    "login_required",
    "admin_required",
    "editor_required",
]
