"""
Обработчики статистики пользователя и списка схем.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiogram import types, F

from bot import storage


async def list_schemas(message: types.Message) -> None:
    """Список схем пользователя."""
    user_id = message.from_user.id
    schemas = await storage.db.get_user_schemas(user_id)

    if not schemas:
        await message.answer("У тебя пока нет схем")
        return

    text = "📋 Твои схемы:\n\n"
    for i, schema in enumerate(schemas, 1):
        if schema.get('name'):
            text += f"{i}. {schema['name']}\n"
            text += f"   📊 Столбцов: {schema.get('matches_count', 0)}\n"
            text += f"   📅 Создана: {schema.get('created_at', '')[:10]}\n\n"

    await message.answer(text)


async def show_stats(message: types.Message) -> None:
    """Статистика пользователя."""
    user_id = message.from_user.id
    stats = await storage.db.get_user_stats(user_id)

    if not stats:
        await message.answer("Статистика не найдена")
        return

    registered = stats.get('registered_at', '')
    registered_short = registered[:10] if registered else '—'

    text = (
        f"📊 Твоя статистика:\n\n"
        f"✅ Всего обработок: {stats['total_processings']}\n"
        f"🎯 Успешных: {stats['successful']}\n"
        f"❌ С ошибками: {stats['failed']}\n"
        f"🔄 Синхронизировано ячеек: {stats['total_synced_cells']}\n"
        f"📅 Зарегистрирован: {registered_short}"
    )
    await message.answer(text)


def register_stats_handlers(dp) -> None:
    """Регистрация обработчиков статистики."""
    dp.message.register(list_schemas, F.text == "📋 Мои схемы")
    dp.message.register(show_stats, F.text == "📊 Моя статистика")