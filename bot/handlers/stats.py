"""
Обработчики статистики
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiogram import types, F
from bot.storage import db


async def list_schemas(message: types.Message):
    """Список схем пользователя"""
    user_id = message.from_user.id
    schemas = db.get_user_schemas(user_id)
    
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


async def show_stats(message: types.Message):
    """Статистика пользователя"""
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    
    if stats:
        text = f"""
📊 Твоя статистика:

✅ Всего обработок: {stats['total_processings']}
🎯 Успешных: {stats['successful']}
❌ С ошибками: {stats['failed']}
🔄 Синхронизировано ячеек: {stats['total_synced_cells']}
📅 Зарегистрирован: {stats['registered_at'][:10]}
"""
        await message.answer(text)
    else:
        await message.answer("Статистика не найдена")


def register_stats_handlers(dp):
    """Регистрация обработчиков статистики"""
    dp.message.register(list_schemas, F.text == "📋 Мои схемы")
    dp.message.register(show_stats, F.text == "📊 Моя статистика")
