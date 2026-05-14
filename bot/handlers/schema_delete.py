"""
Обработчики удаления схем.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiogram import types, F
from aiogram.fsm.context import FSMContext

from bot.states import SchemaStates
from bot.keyboards import get_main_menu_keyboard, get_schema_list_keyboard
from bot import storage
from bot.security import AccessManager
from bot.handlers.common import schema_management


async def delete_schema_start(message: types.Message, state: FSMContext) -> None:
    """Начало удаления схемы — показываем список схем пользователя."""
    user_id = message.from_user.id

    can_see_all = await AccessManager.can_see_all_schemas(user_id)
    schemas = await storage.db.get_user_schemas(user_id, all_schemas=can_see_all)

    if not schemas:
        await message.answer("❌ У тебя нет схем!")
        return

    keyboard = get_schema_list_keyboard(schemas)

    if not keyboard:
        await message.answer("❌ У тебя нет валидных схем!")
        return

    await state.set_state(SchemaStates.selecting_schema_to_delete)
    await message.answer("Выбери схему для удаления:", reply_markup=keyboard)


async def schema_selected_for_deletion(
    message: types.Message,
    state: FSMContext,
) -> None:
    """Подтверждение удаления выбранной схемы."""
    if message.text == "❌ Отмена":
        await schema_management(message, state)
        return

    user_id = message.from_user.id
    schema_name = message.text

    deleted = await storage.db.delete_schema(user_id, schema_name)

    await state.clear()

    if deleted:
        await message.answer(
            f"✅ Схема '{schema_name}' удалена",
            reply_markup=get_main_menu_keyboard(),
        )
    else:
        await message.answer(
            "❌ Не удалось удалить схему",
            reply_markup=get_main_menu_keyboard(),
        )


def register_schema_delete_handlers(dp) -> None:
    """Регистрация обработчиков удаления схем."""
    dp.message.register(delete_schema_start, F.text == "🗑 Удалить схему")
    dp.message.register(
        schema_selected_for_deletion,
        SchemaStates.selecting_schema_to_delete,
    )