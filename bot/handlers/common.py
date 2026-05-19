"""
Общие обработчики: старт, главное меню, навигация.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiogram import types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from bot.keyboards import get_main_menu_keyboard, get_schema_management_keyboard
from bot import storage
from bot.security import AccessManager


async def cmd_start(message: types.Message, state: FSMContext) -> None:
    """Команда /start — очистка состояния и главное меню."""
    await state.clear()

    # Очищаем все сессионные данные пользователя из Redis/in-memory.
    # Это гарантирует, что прерванные загрузки/создания схем
    # не оставят "мусор" в хранилище.
    user_id = message.from_user.id
    await storage.session_storage.clear(user_id)

    # Регистрируем или обновляем данные пользователя в БД
    await storage.db.add_user(
        user_id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    has_management_rights = await AccessManager.has_access_management_rights(user_id)

    await message.answer(
        "🤖 Бот синхронизации маркетплейсов\n\n"
        "📤 Загрузить файлы - синхронизация по схеме\n"
        "📋 Управление схемами - создать/обновить/удалить",
        reply_markup=get_main_menu_keyboard(
            show_access_management=has_management_rights
        ),
    )


async def schema_management(message: types.Message, state: FSMContext) -> None:
    """Меню управления схемами."""
    await message.answer(
        "Управление схемами:",
        reply_markup=get_schema_management_keyboard(),
    )


async def go_back(message: types.Message, state: FSMContext) -> None:
    """Возврат в главное меню."""
    await cmd_start(message, state)


def register_common_handlers(dp) -> None:
    """Регистрация общих обработчиков."""
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(schema_management, F.text == "📋 Управление схемами")
    dp.message.register(go_back, F.text == "◀️ Назад")