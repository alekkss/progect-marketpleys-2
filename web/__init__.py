"""
Веб-приложение на aiohttp — браузерный интерфейс для синхронизации маркетплейсов.

Предоставляет параллельный канал доступа к тому же функционалу, что и Telegram-бот:
загрузка файлов, создание схем, обработка с AI-сопоставлением, скачивание результатов.

Точка входа — фабричная функция create_web_app(), которая создаёт
полностью настроенный aiohttp.web.Application с middleware, маршрутами
и shared-ресурсами (DB, Redis, AIComparator, TaskQueue).

Использование в bot/bot.py:
    from web import create_web_app
    web_app = await create_web_app(task_queue, ai_comparator, ws_manager)
"""

from web.app import create_web_app

__all__ = ["create_web_app"]
