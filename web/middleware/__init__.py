"""
Middleware веб-приложения.

Порядок регистрации middleware критичен — они выполняются
в порядке добавления (первый зарегистрированный = внешний слой):

    1. errors — перехват исключений, отображение 404/500 страниц
    2. auth — проверка cookie-сессии, загрузка пользователя в request["user"]
    3. csrf — валидация CSRF-токенов в POST/PUT/DELETE формах
    4. api_auth — Bearer-аутентификация внешнего REST API (/v1/*,
       AI-агент маппинга PIM+FDM)

Функция setup_middlewares() — единая точка регистрации.
Вызывается из web/app.py при создании Application.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

from web.middleware.errors import error_middleware
from web.middleware.auth import auth_middleware
from web.middleware.csrf import csrf_middleware
from web.middleware.api_auth import api_auth_middleware


def setup_middlewares(app: "web.Application") -> None:
    """
    Регистрирует все middleware в приложении.

    Порядок важен:
        1. error_middleware — внешний слой, перехватывает исключения
           (включая HTTPForbidden из csrf и непредвиденные ошибки
           api_auth) и форматирует ответы (JSON для /v1/* и /api/*).
        2. auth_middleware — извлекает сессию из cookie, загружает
           данные пользователя в request["user"]. Пропускает /v1/*
           без проверки сессии — внешний контур API не связан
           с cookie-сессиями сайта.
        3. csrf_middleware — проверяет CSRF-токен для мутирующих
           запросов (POST/PUT/DELETE/PATCH). Пропускает /v1/* —
           Bearer-заголовок сам доказывает происхождение запроса.
        4. api_auth_middleware — Bearer-аутентификация запросов
           /v1/* (AI-агент маппинга PIM+FDM): 401 при невалидном
           токене, 503 при выключенном агенте. Все прочие пути
           пропускает без проверок.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.middlewares.append(error_middleware)
    app.middlewares.append(auth_middleware)
    app.middlewares.append(csrf_middleware)
    app.middlewares.append(api_auth_middleware)


__all__ = ["setup_middlewares"]
