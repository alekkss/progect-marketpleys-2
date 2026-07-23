"""
Middleware веб-приложения.

Порядок регистрации middleware критичен — они выполняются
в порядке добавления (первый зарегистрированный = внешний слой):

    1. errors — перехват исключений, отображение 404/500 страниц
    2. auth — проверка cookie-сессии, загрузка пользователя в request["user"]
    3. csrf — валидация CSRF-токенов в POST/PUT/DELETE формах

Функция setup_middlewares() — единая точка регистрации.
Вызывается из web/app.py при создании Application.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

from web.middleware.errors import error_middleware
from web.middleware.auth import auth_middleware
from web.middleware.csrf import csrf_middleware


def setup_middlewares(app: "web.Application") -> None:
    """
    Регистрирует все middleware в приложении.

    Порядок важен:
        1. error_middleware — внешний слой, перехватывает исключения
           (включая HTTPForbidden из csrf) и форматирует ответы.
        2. auth_middleware — извлекает сессию из cookie, загружает
           данные пользователя в request["user"].
        3. csrf_middleware — проверяет CSRF-токен для мутирующих
           запросов (POST/PUT/DELETE/PATCH). Требует, чтобы auth
           уже отработал (некоторые пути пропускаются по роли).

    Args:
        app: Экземпляр aiohttp Application
    """
    app.middlewares.append(error_middleware)
    app.middlewares.append(auth_middleware)
    app.middlewares.append(csrf_middleware)


__all__ = ["setup_middlewares"]
