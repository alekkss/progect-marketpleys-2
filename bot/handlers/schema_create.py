"""
Обработчики создания схем
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import os
import logging
from aiogram import types, F
from aiogram.types import ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

from bot.states import SchemaStates
from bot.keyboards import (
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_create_schema_keyboard
)
from bot.storage import user_schemas, db
from bot.utils import download_file
from bot.handlers.common import schema_management

from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from services.ai_comparator import AIComparator
from bot.security import AccessManager


async def create_schema_start(message: types.Message, state: FSMContext):
    """Начало создания схемы"""
    await state.set_state(SchemaStates.waiting_schema_name)
    await message.answer(
        "Введи название схемы:",
        reply_markup=get_cancel_keyboard()
    )


async def schema_name_entered(message: types.Message, state: FSMContext):
    """Имя схемы введено"""
    if message.text == "❌ Отмена":
        await schema_management(message, state)
        return
    
    schema_name = message.text.strip()
    user_id = message.from_user.id
    
    # Проверяем права доступа
    can_see_all = AccessManager.can_see_all_schemas(user_id)
    
    # Проверяем, не существует ли уже (глобально для админов)
    if can_see_all:
        # Админы/редакторы проверяют глобально
        existing_schema = db.get_schema_by_name_global(schema_name)
        if existing_schema:
            owner_info = f" (владелец: ID {existing_schema['owner_id']})" if existing_schema['owner_id'] != user_id else ""
            await message.answer(
                f"❌ Схема с названием '{schema_name}' уже существует{owner_info}.\n\n"
                "Введи другое название:"
            )
            return
    else:
        # Обычные пользователи проверяют только свои
        if db.get_schema(user_id, schema_name):
            await message.answer("❌ Схема с таким названием уже существует. Введи другое название:")
            return
    
    # Сохраняем название
    await state.update_data(schema_name=schema_name)
    user_schemas[user_id] = {}
    await state.set_state(SchemaStates.waiting_schema_files)
    
    await message.answer(
        f"✅ Название схемы: '{schema_name}'\n\n"
        "Теперь отправь 3 файла Excel для определения столбцов",
        reply_markup=ReplyKeyboardRemove()
    )


async def handle_schema_file(message: types.Message, state: FSMContext, bot):
    """Обработка файла для схемы"""
    user_id = message.from_user.id
    
    if user_id not in user_schemas:
        user_schemas[user_id] = {}
    
    # НОВОЕ: Проверяем, не обработали ли мы уже все файлы
    data = await state.get_data()
    if data.get('files_processed'):
        return  # Уже обработали, игнорируем дубликаты
    
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
        # КРИТИЧНО: Двойная проверка флага
        data = await state.get_data()
        if data.get('files_processed'):
            return
        
        await state.update_data(files_processed=True)
        
        await message.answer(
            "✅ Все файлы загружены!",
            reply_markup=get_create_schema_keyboard()
        )


async def finalize_schema_creation(message: types.Message, state: FSMContext):
    """Финализация создания схемы"""
    # Проверяем состояние
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
        
        # Читаем столбцы
        reader = ExcelReader()
        columns = {}
        
        for marketplace, file_path in file_paths.items():
            config = FILE_CONFIGS[marketplace]
            columns[marketplace] = reader.get_column_names(
                file_path,
                config['sheet_name'],
                config['header_row']
            )
        
        await message.answer("🤖 AI сравнивает столбцы...")
        
        # Сравниваем с помощью AI
        comparator = AIComparator()
        comparison_result = comparator.compare_columns(
            columns['wildberries'],
            columns['ozon'],
            columns['yandex']
        )
        
        # Фильтруем совпадения по уверенности >= 85%
        total_matches = len(comparison_result.get('matches_all_three', []))
        filtered_matches = [
            match for match in comparison_result.get('matches_all_three', [])
            if match.get('confidence', 0) >= 0.85
        ]
        
        # Заменяем на отфильтрованные
        comparison_result['matches_all_three'] = filtered_matches
        matches_count = len(filtered_matches)
        skipped_count = total_matches - matches_count
        
        # Создаем схему в БД
        schema_id = db.create_schema(user_id, schema_name)
        
        if not schema_id:
            await message.answer("❌ Схема с таким названием уже существует!")
            return
        
        # Сохраняем сопоставления
        db.save_schema_matches(schema_id, comparison_result)
        
        user_schemas[user_id] = {}
        await state.clear()
        
        message_text = f"✅ Схема '{schema_name}' создана!\n\n"
        message_text += f"📊 Сохранено совпадений: {matches_count}"
        
        if skipped_count > 0:
            message_text += f"\n⚠️ Пропущено (< 85%): {skipped_count}"
        
        await message.answer(message_text, reply_markup=get_main_menu_keyboard())
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logging.error(f"Error creating schema: {e}", exc_info=True)


def register_schema_create_handlers(dp, bot):
    """Регистрация обработчиков создания схем"""
    from functools import partial
    
    dp.message.register(create_schema_start, F.text == "➕ Создать схему")
    dp.message.register(schema_name_entered, SchemaStates.waiting_schema_name)
    dp.message.register(partial(handle_schema_file, bot=bot), SchemaStates.waiting_schema_files, F.document)
    dp.message.register(finalize_schema_creation, F.text == "✅ Создать схему")
