"""
Обработчики создания стандартных схем (WB + Ozon + Яндекс).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
import logging
from aiogram import types, F
from aiogram.types import ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from bot.states import SchemaStates, SchemaMvmStates
from bot.keyboards import (
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_create_schema_keyboard,
    get_schema_type_keyboard,
)
from bot.storage import user_schemas
from bot import storage  # ✅ Импортируем модуль, а не переменную
from bot.utils import download_file
from bot.handlers.common import schema_management
from bot.security import AccessManager
from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from services.ai_comparator import AIComparator

logger = logging.getLogger('schema_create')

async def create_schema_start(message: types.Message, state: FSMContext) -> None:
    """Начало создания схемы — выбор типа."""
    await state.set_state(SchemaMvmStates.choosing_schema_type)
    await message.answer(
        "Выбери тип схемы:\n\n"
        "📊  Загрузить 3 МП  — стандартная схема (WB + Ozon + Яндекс)\n"
        "📦  Создать схему МВМ  — расширенная (3 МП + XML каталог)",
        reply_markup=get_schema_type_keyboard(),
        parse_mode="Markdown",
    )

async def schema_type_chosen(message: types.Message, state: FSMContext) -> None:
    """Обработка выбора типа схемы."""
    if message.text == "❌ Отмена":
        await schema_management(message, state)
        return
    if message.text == "📊 Загрузить 3 МП":
        await state.set_state(SchemaStates.waiting_schema_name)
        await message.answer("Введи название схемы:", reply_markup=get_cancel_keyboard())
        return
    if message.text == "📦 Создать схему МВМ":
        await state.set_state(SchemaMvmStates.waiting_schema_name)
        await message.answer(
            "📦 Создание схемы МВМ (3 МП + XML)\n\nВведи название схемы:",
            reply_markup=get_cancel_keyboard(),
        )
        return
    await message.answer("Выбери один из вариантов:", reply_markup=get_schema_type_keyboard())

async def schema_name_entered(message: types.Message, state: FSMContext) -> None:
    """Имя схемы введено — проверяем уникальность."""
    if message.text == "❌ Отмена":
        await schema_management(message, state)
        return
    
    schema_name = message.text.strip()
    user_id = message.from_user.id
    can_see_all = await AccessManager.can_see_all_schemas(user_id)

    if can_see_all:
        existing_schema = await storage.db.get_schema_by_name_global(schema_name)
        if existing_schema:
            owner_info = (
                f" (владелец: ID {existing_schema['owner_id']})"
                if existing_schema['owner_id'] != user_id else ""
            )
            await message.answer(
                f"❌ Схема с названием '{schema_name}' уже существует{owner_info}.\n\n"
                "Введи другое название:"
            )
            return
    else:
        existing = await storage.db.get_schema(user_id, schema_name)
        if existing:
            await message.answer("❌ Схема с таким названием уже существует. Введи другое название:")
            return

    await state.update_data(schema_name=schema_name)
    user_schemas[user_id] = {}
    await state.set_state(SchemaStates.waiting_schema_files)

    await message.answer(
        f"✅ Название схемы: '{schema_name}'\n\n"
        "Теперь отправь 3 файла Excel для определения столбцов",
        reply_markup=ReplyKeyboardRemove(),
    )

async def handle_schema_file(message: types.Message, state: FSMContext, bot) -> None:
    """Обработка загруженного файла для схемы."""
    user_id = message.from_user.id
    if user_id not in user_schemas:
        user_schemas[user_id] = {}

    data = await state.get_data()
    if data.get('files_processed'):
        return

    file_path, file_name, marketplace = await download_file(bot, message, user_id)
    if not marketplace:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)")
        return
    if marketplace in user_schemas[user_id]:
        await message.answer(f"⚠️ {marketplace.upper()} уже загружен")
        return

    user_schemas[user_id][marketplace] = file_path
    await message.answer(f"✅ {marketplace.upper()} ({len(user_schemas[user_id])}/3)")

    if len(user_schemas[user_id]) == 3:
        data = await state.get_data()
        if data.get('files_processed'):
            return
        await state.update_data(files_processed=True)
        await message.answer("✅ Все файлы загружены!", reply_markup=get_create_schema_keyboard())

async def finalize_schema_creation(message: types.Message, state: FSMContext) -> None:
    """Финализация создания схемы — AI-сопоставление и сохранение в БД."""
    current_state = await state.get_state()
    if current_state != SchemaStates.waiting_schema_files:
        await message.answer("❌ Сначала начни создание схемы через '➕ Создать схему'")
        return
    
    user_id = message.from_user.id
    if user_id not in user_schemas or len(user_schemas[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла!")
        return

    data = await state.get_data()
    schema_name = data.get('schema_name')
    if not schema_name:
        await message.answer("❌ Название схемы потеряно. Начни заново.")
        return

    await message.answer("⏳ Анализирую столбцы...")

    try:
        file_paths = user_schemas[user_id]
        reader = ExcelReader()
        columns = {}

        for marketplace, file_path in file_paths.items():
            config = FILE_CONFIGS[marketplace]
            columns[marketplace] = reader.get_column_names(
                file_path, config['sheet_name'], config['header_row']
            )

        await message.answer("🤖 AI сравнивает столбцы...")
        comparator = AIComparator()
        comparison_result = await comparator.compare_columns(
            columns['wildberries'], columns['ozon'], columns['yandex']
        )

        # Фильтруем совпадения по уверенности >= 85%
        all_matches = comparison_result.get('matches_all_three', [])
        filtered = [m for m in all_matches if m.get('confidence', 0) >= 0.85]
        skipped_count = len(all_matches) - len(filtered)
        comparison_result['matches_all_three'] = filtered
        matches_count = len(filtered)

        # Создаём схему в БД
        schema_id = await storage.db.create_schema(user_id, schema_name)
        if not schema_id:
            await message.answer("❌ Схема с таким названием уже существует!")
            return

        # Сохраняем сопоставления
        await storage.db.save_schema_matches(schema_id, comparison_result)

        user_schemas[user_id] = {}
        await state.clear()

        text = f"✅ Схема '{schema_name}' создана!\n\n"
        text += f"📊 Сохранено совпадений: {matches_count}"
        if skipped_count > 0:
            text += f"\n⚠️ Пропущено (< 85%): {skipped_count}"

        await message.answer(text, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error("Ошибка создания схемы: %s", e, exc_info=True)

def register_schema_create_handlers(dp, bot) -> None:
    """Регистрация обработчиков создания схем."""
    from functools import partial
    dp.message.register(create_schema_start, F.text == "➕ Создать схему")
    dp.message.register(schema_type_chosen, SchemaMvmStates.choosing_schema_type)
    dp.message.register(schema_name_entered, SchemaStates.waiting_schema_name)
    dp.message.register(partial(handle_schema_file, bot=bot), SchemaStates.waiting_schema_files, F.document)
    dp.message.register(finalize_schema_creation, F.text == "✅ Создать схему")