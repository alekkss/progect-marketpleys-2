"""
Общие обработчики: старт, главное меню, навигация
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiogram import types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from bot.keyboards import get_main_menu_keyboard, get_schema_management_keyboard
from bot.storage import user_files, db
from bot.security import AccessManager  # ← ДОБАВИТЬ

async def cmd_start(message: types.Message, state: FSMContext):
    """Команда /start - главное меню"""
    await state.clear()
    user_files[message.from_user.id] = {}
    
    # Регистрируем пользователя
    db.add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )
    
    # Проверяем права доступа для показа кнопки управления
    user_id = message.from_user.id
    has_management_rights = AccessManager.has_access_management_rights(user_id)  # ← ДОБАВИТЬ
    
    await message.answer(
        "🤖 Бот синхронизации маркетплейсов\n\n"
        "📤 Загрузить файлы - синхронизация по схеме\n"
        "📋 Управление схемами - создать/обновить/удалить",
        reply_markup=get_main_menu_keyboard(show_access_management=has_management_rights)  # ← ИЗМЕНИТЬ
    )

async def schema_management(message: types.Message, state: FSMContext):
    """Меню управления схемами"""
    await message.answer(
        "Управление схемами:",
        reply_markup=get_schema_management_keyboard()
    )

async def go_back(message: types.Message, state: FSMContext):
    """Возврат в главное меню"""
    await cmd_start(message, state)

def register_common_handlers(dp):
    """Регистрация общих обработчиков"""
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(schema_management, F.text == "📋 Управление схемами")
    dp.message.register(go_back, F.text == "◀️ Назад")
    # Регистрация "🔐 Управление доступами" теперь в access_management.py
