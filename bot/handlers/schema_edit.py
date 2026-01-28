"""
Обработчики редактирования схем (просмотр и изменение сопоставлений)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiogram import types, F
from aiogram.types import ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

from bot.states import SchemaStates
from bot.keyboards import (
    get_schema_edit_keyboard,
    get_cancel_keyboard,
    get_edit_column_keyboard,
    get_back_to_edit_keyboard,
    get_schema_list_keyboard,
    get_edit_match_menu_keyboard,
    get_filter_matches_keyboard
)

from bot.storage import user_schemas, db
from bot.utils import download_file
from bot.handlers.common import cmd_start
from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from bot.security import AccessManager


async def edit_schema_start(message: types.Message, state: FSMContext):
    """Меню редактирования схемы"""
    await message.answer(
        "Редактирование схемы:\n\n"
        "Выбери действие:",
        reply_markup=get_schema_edit_keyboard()
    )


# ===== ПРОСМОТР СОПОСТАВЛЕНИЙ =====

async def view_matches_start(message: types.Message, state: FSMContext):
    """Выбор схемы для просмотра"""
    user_id = message.from_user.id
    
    # Проверяем права - админы/редакторы видят все схемы
    can_see_all = AccessManager.can_see_all_schemas(user_id)
    schemas = db.get_user_schemas(user_id, all_schemas=can_see_all)
    
    if not schemas:
        await message.answer("❌ У тебя нет схем!")
        return
    
    keyboard = get_schema_list_keyboard(schemas)
    if not keyboard:
        await message.answer("❌ У тебя нет валидных схем!")
        return
    
    await state.set_state(SchemaStates.selecting_schema_to_view)
    await message.answer("Выбери схему для просмотра:", reply_markup=keyboard)


async def show_schema_matches(message: types.Message, state: FSMContext):
    """Отображение сопоставлений выбранной схемы"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return
    
    user_id = message.from_user.id
    schema_name = message.text
    
    # Проверяем права
    can_see_all = AccessManager.can_see_all_schemas(user_id)
    if can_see_all:
        # Админы/редакторы - глобальный поиск
        schema = db.get_schema_by_name_global(schema_name)
    else:
        # Обычные пользователи - только свои
        schema = db.get_schema(user_id, schema_name)
    
    if not schema:
        await message.answer("❌ Схема не найдена")
        return
    
    schema_id = schema['id']
    schema_name = schema['name']
    
    # Получаем сопоставления
    matches_data = db.get_schema_matches(schema_id)
    
    # Извлекаем все типы сопоставлений
    matches_all_three = matches_data.get('matches_all_three', [])
    matches_1_2 = matches_data.get('matches_1_2', [])  # WB + Ozon
    matches_1_3 = matches_data.get('matches_1_3', [])  # WB + Яндекс
    matches_2_3 = matches_data.get('matches_2_3', [])  # Ozon + Яндекс
    
    # Проверяем, есть ли хоть какие-то сопоставления
    total_matches = len(matches_all_three) + len(matches_1_2) + len(matches_1_3) + len(matches_2_3)
    
    if total_matches == 0:
        await state.clear()
        await message.answer(
            f"📋 Схема '{schema_name}'\n\n"
            "⚠️ Нет сопоставлений",
            reply_markup=get_back_to_edit_keyboard()
        )
        return
    
    # Формируем красивый вывод
    text_parts = []
    
    # Заголовок
    text_parts.append(f"📋 Схема: {schema_name}\n")
    text_parts.append(f"📊 Всего сопоставлений: {total_matches}\n\n")
    text_parts.append("─" * 40 + "\n\n")
    
    # ===== ТРОЙНЫЕ СОПОСТАВЛЕНИЯ =====
    if matches_all_three:
        text_parts.append(f"🎯 Тройные сопоставления: {len(matches_all_three)}\n\n")
        
        for i, match in enumerate(matches_all_three, 1):
            wb_col = match.get('column_1', '—')
            ozon_col = match.get('column_2', '—')
            yandex_col = match.get('column_3', '—')
            confidence = match.get('confidence', 0)
            description = match.get('description', '')
            
            text_parts.append(f"#{i}\n")
            text_parts.append(f"🔹 WB: {wb_col}\n")
            text_parts.append(f"🔸 Ozon: {ozon_col}\n")
            text_parts.append(f"🔹 Яндекс: {yandex_col}\n")
            text_parts.append(f"📈 Уверенность: {confidence:.0%}\n")
            if description:
                text_parts.append(f"💬 {description}\n")
            text_parts.append("\n")
    
    # ===== ПАРНЫЕ СОПОСТАВЛЕНИЯ: WB + OZON =====
    if matches_1_2:
        text_parts.append("─" * 40 + "\n\n")
        text_parts.append(f"🔗 Парные сопоставления (WB + Ozon): {len(matches_1_2)}\n\n")
        
        for i, match in enumerate(matches_1_2, 1):
            wb_col = match.get('column_1', '—')
            ozon_col = match.get('column_2', '—')
            confidence = match.get('confidence', 0)
            description = match.get('description', '')
            
            text_parts.append(f"#{i}\n")
            text_parts.append(f"🔹 WB: {wb_col}\n")
            text_parts.append(f"🔸 Ozon: {ozon_col}\n")
            text_parts.append(f"❌ Яндекс: N/A\n")
            text_parts.append(f"📈 Уверенность: {confidence:.0%}\n")
            if description:
                text_parts.append(f"💬 {description}\n")
            text_parts.append("\n")
    
    # ===== ПАРНЫЕ СОПОСТАВЛЕНИЯ: WB + ЯНДЕКС =====
    if matches_1_3:
        text_parts.append("─" * 40 + "\n\n")
        text_parts.append(f"🔗 Парные сопоставления (WB + Яндекс): {len(matches_1_3)}\n\n")
        
        for i, match in enumerate(matches_1_3, 1):
            wb_col = match.get('column_1', '—')
            yandex_col = match.get('column_3', '—')
            confidence = match.get('confidence', 0)
            description = match.get('description', '')
            
            text_parts.append(f"#{i}\n")
            text_parts.append(f"🔹 WB: {wb_col}\n")
            text_parts.append(f"❌ Ozon: N/A\n")
            text_parts.append(f"🔹 Яндекс: {yandex_col}\n")
            text_parts.append(f"📈 Уверенность: {confidence:.0%}\n")
            if description:
                text_parts.append(f"💬 {description}\n")
            text_parts.append("\n")
    
    # ===== ПАРНЫЕ СОПОСТАВЛЕНИЯ: OZON + ЯНДЕКС =====
    if matches_2_3:
        text_parts.append("─" * 40 + "\n\n")
        text_parts.append(f"🔗 Парные сопоставления (Ozon + Яндекс): {len(matches_2_3)}\n\n")
        
        for i, match in enumerate(matches_2_3, 1):
            ozon_col = match.get('column_2', '—')
            yandex_col = match.get('column_3', '—')
            confidence = match.get('confidence', 0)
            description = match.get('description', '')
            
            text_parts.append(f"#{i}\n")
            text_parts.append(f"❌ WB: N/A\n")
            text_parts.append(f"🔸 Ozon: {ozon_col}\n")
            text_parts.append(f"🔹 Яндекс: {yandex_col}\n")
            text_parts.append(f"📈 Уверенность: {confidence:.0%}\n")
            if description:
                text_parts.append(f"💬 {description}\n")
            text_parts.append("\n")
    
    # Объединяем весь текст
    full_text = ''.join(text_parts)
    
    # Разбиваем на части если слишком длинное (лимит Telegram 4096 символов)
    max_length = 3500
    if len(full_text) <= max_length:
        await message.answer(full_text)
    else:
        # Разбиваем на части
        current_pos = 0
        while current_pos < len(full_text):
            # Ищем последний перенос строки перед лимитом
            end_pos = current_pos + max_length
            if end_pos < len(full_text):
                # Ищем последний \n перед лимитом
                last_newline = full_text.rfind('\n', current_pos, end_pos)
                if last_newline > current_pos:
                    end_pos = last_newline + 1
            
            chunk = full_text[current_pos:end_pos]
            await message.answer(chunk)
            current_pos = end_pos
    
    await state.clear()
    await message.answer("✅ Просмотр завершен", reply_markup=get_back_to_edit_keyboard())


# ===== ИЗМЕНЕНИЕ СОПОСТАВЛЕНИЙ =====

async def edit_match_start(message: types.Message, state: FSMContext):
    """Выбор схемы для редактирования"""
    user_id = message.from_user.id
    
    # Проверяем права - админы/редакторы видят все схемы
    can_see_all = AccessManager.can_see_all_schemas(user_id)
    schemas = db.get_user_schemas(user_id, all_schemas=can_see_all)
    
    if not schemas:
        await message.answer("❌ У тебя нет схем!")
        return
    
    keyboard = get_schema_list_keyboard(schemas)
    if not keyboard:
        await message.answer("❌ У тебя нет валидных схем!")
        return
    
    await state.set_state(SchemaStates.selecting_schema_to_edit)
    await message.answer("Выбери схему для редактирования:", reply_markup=keyboard)


async def schema_selected_for_edit(message: types.Message, state: FSMContext):
    """Схема выбрана, запрашиваем файлы для валидации"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return
    
    user_id = message.from_user.id
    schema_name = message.text
    
    # Проверяем права
    can_see_all = AccessManager.can_see_all_schemas(user_id)
    if can_see_all:
        # Админы/редакторы - глобальный поиск
        schema = db.get_schema_by_name_global(schema_name)
    else:
        # Обычные пользователи - только свои
        schema = db.get_schema(user_id, schema_name)
    
    if not schema:
        await message.answer("❌ Схема не найдена")
        return
    
    schema_id = schema['id']
    schema_name = schema['name']
    
    # Получаем ВСЕ сопоставления (тройные + парные)
    matches_data = db.get_schema_matches(schema_id)
    
    # Извлекаем все типы
    matches_all_three = matches_data.get('matches_all_three', [])
    matches_1_2 = matches_data.get('matches_1_2', [])  # WB + Ozon
    matches_1_3 = matches_data.get('matches_1_3', [])  # WB + Яндекс
    matches_2_3 = matches_data.get('matches_2_3', [])  # Ozon + Яндекс
    
    # Подсчитываем общее количество
    total_matches = len(matches_all_three) + len(matches_1_2) + len(matches_1_3) + len(matches_2_3)
    
    if total_matches == 0:
        await state.clear()
        await message.answer(
            f"📋 Схема '{schema_name}'\n\n"
            "⚠️ Нет сопоставлений для редактирования"
        )
        await edit_schema_start(message, state)
        return
    
    # Создаем единый список с метками типа для удобной работы
    # Формат: {'type': 'triple'/'pair_1_2'/'pair_1_3'/'pair_2_3', 'data': {...}}
    all_matches = []
    
    for match in matches_all_three:
        all_matches.append({'type': 'triple', 'data': match})
    
    for match in matches_1_2:
        all_matches.append({'type': 'pair_1_2', 'data': match})
    
    for match in matches_1_3:
        all_matches.append({'type': 'pair_1_3', 'data': match})
    
    for match in matches_2_3:
        all_matches.append({'type': 'pair_2_3', 'data': match})
    
    # Сохраняем в state
    await state.update_data(
        edit_schema_id=schema_id,
        edit_schema_name=schema_name,
        edit_all_matches=all_matches,  # Единый список с типами
        edit_matches_data=matches_data  # Оригинальные данные для сохранения
    )
    
    # Запрашиваем загрузку файлов для валидации
    user_schemas[user_id] = {}
    await state.update_data(files_processed=False)
    
    await message.answer(
        f"📋 Схема '{schema_name}' выбрана\n\n"
        f"📊 Всего сопоставлений: {total_matches}\n"
        f"  • Тройные: {len(matches_all_three)}\n"
        f"  • Парные (WB+Ozon): {len(matches_1_2)}\n"
        f"  • Парные (WB+Яндекс): {len(matches_1_3)}\n"
        f"  • Парные (Ozon+Яндекс): {len(matches_2_3)}\n\n"
        "📤 Для валидации столбцов загрузи 3 актуальных файла Excel\n"
        "(wb, ozon, yandex)",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(SchemaStates.waiting_edit_files)


async def handle_edit_validation_file(message: types.Message, state: FSMContext, bot):
    """Загрузка файлов для валидации при редактировании"""
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
    
    # ВАЖНО: Проверяем ровно == 3 и ЕЩЁ РАЗ проверяем флаг
    if len(user_schemas[user_id]) == 3:
        # КРИТИЧНО: Сразу устанавливаем флаг ДО любой обработки
        data = await state.get_data()
        if data.get('files_processed'):
            return  # Другой обработчик уже начал обработку
        
        await state.update_data(files_processed=True)
        
        # Читаем столбцы из файлов
        try:
            reader = ExcelReader()
            available_columns = {}
            
            for marketplace, file_path in user_schemas[user_id].items():
                config = FILE_CONFIGS[marketplace]
                available_columns[marketplace] = reader.get_column_names(
                    file_path,
                    config['sheet_name'],
                    config['header_row']
                )
            
            # Сохраняем доступные столбцы
            await state.update_data(available_columns=available_columns)
            
            # Показываем список сопоставлений
            data = await state.get_data()  # Перечитываем data
            all_matches = data.get('edit_all_matches', [])
            schema_name = data.get('edit_schema_name')
            
            # Показываем статистику и меню фильтра
            triple_count = len([m for m in all_matches if m['type'] == 'triple'])
            pair_1_2_count = len([m for m in all_matches if m['type'] == 'pair_1_2'])
            pair_1_3_count = len([m for m in all_matches if m['type'] == 'pair_1_3'])
            pair_2_3_count = len([m for m in all_matches if m['type'] == 'pair_2_3'])
            
            text = f"✅ Файлы загружены!\n\n"
            text += f"📋 Схема: {schema_name}\n"
            text += f"📊 Всего сопоставлений: {len(all_matches)}\n\n"
            text += f"🎯 Тройные: {triple_count}\n"
            text += f"🔗 Парные (WB+Ozon): {pair_1_2_count}\n"
            text += f"🔗 Парные (WB+Яндекс): {pair_1_3_count}\n"
            text += f"🔗 Парные (Ozon+Яндекс): {pair_2_3_count}\n\n"
            text += "Выбери тип для просмотра или начни редактирование:"
            
            await state.set_state(SchemaStates.choosing_edit_action)
            await message.answer(text, reply_markup=get_filter_matches_keyboard())
            
        except Exception as e:
            await message.answer(f"❌ Ошибка чтения файлов: {str(e)}")
            await edit_schema_start(message, state)


async def edit_action_selected(message: types.Message, state: FSMContext):
    """Обработка действия после загрузки файлов (фильтры / редактирование / добавление)"""
    if message.text == "❌ Отмена":
        # Очистка временных загруженных файлов для пользователя
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await edit_schema_start(message, state)
        return

    data = await state.get_data()
    all_matches = data.get('edit_all_matches', [])
    schema_name = data.get('edit_schema_name')

    # --- НОВОЕ: добавление сопоставления ---
    if message.text == "➕ Добавить сопоставление":
        available_columns = data.get('available_columns')
        if not available_columns:
            # Это защита от некорректного состояния (если кнопку нажали без загрузки файлов)
            await message.answer(
                "⚠️ Чтобы добавить сопоставление, сначала загрузи 3 файла для валидации (wb/ozon/yandex).",
                reply_markup=get_filter_matches_keyboard()
            )
            return

        await add_new_match_start(message, state)
        return

    # --- Редактирование существующего ---
    if message.text == "✏️ Редактировать сопоставление":
        if not all_matches:
            await message.answer("⚠️ Нет сопоставлений для редактирования.", reply_markup=get_filter_matches_keyboard())
            return

        await state.set_state(SchemaStates.entering_match_number)
        await message.answer(
            f"Введи номер сопоставления для редактирования (1-{len(all_matches)}):",
            reply_markup=get_cancel_keyboard()
        )
        return

    # --- Фильтры отображения ---
    filter_type = None
    filter_name = None

    if message.text == "🎯 Показать тройные":
        filter_type = 'triple'
        filter_name = "🎯 Тройные сопоставления"
    elif message.text == "🔗 Показать парные (WB+Ozon)":
        filter_type = 'pair_1_2'
        filter_name = "🔗 Парные (WB + Ozon)"
    elif message.text == "🔗 Показать парные (WB+Яндекс)":
        filter_type = 'pair_1_3'
        filter_name = "🔗 Парные (WB + Яндекс)"
    elif message.text == "🔗 Показать парные (Ozon+Яндекс)":
        filter_type = 'pair_2_3'
        filter_name = "🔗 Парные (Ozon + Яндекс)"
    elif message.text == "📋 Показать всё":
        filter_type = None
        filter_name = "📋 Все сопоставления"
    else:
        await message.answer("Выбери действие из меню.", reply_markup=get_filter_matches_keyboard())
        return

    # --- Фильтруем сопоставления ---
    if filter_type:
        filtered_matches = [m for m in all_matches if m.get('type') == filter_type]
    else:
        filtered_matches = all_matches

    if not filtered_matches:
        await message.answer(f"⚠️ {filter_name}: нет сопоставлений", reply_markup=get_filter_matches_keyboard())
        return

    # --- Формируем список ---
    text = f"📋 Схема: {schema_name}\n"
    text += f"{filter_name}\n"
    text += f"📊 Показано: {len(filtered_matches)}\n\n"

    for i, match_obj in enumerate(all_matches):
        # Важно: нумерация должна оставаться по исходному all_matches (для редактирования по номеру)
        if filter_type and match_obj.get('type') != filter_type:
            continue

        match = match_obj.get('data', {})
        match_type = match_obj.get('type')

        wb = match.get('column_1', '—')
        ozon = match.get('column_2', '—')
        yandex = match.get('column_3', '—')

        # Для парных показываем N/A
        if match_type == 'pair_1_2':
            yandex = 'N/A'
        elif match_type == 'pair_1_3':
            ozon = 'N/A'
        elif match_type == 'pair_2_3':
            wb = 'N/A'

        text += f"#{i + 1}: {wb} | {ozon} | {yandex}\n"

    # --- Разбиваем на части (лимит Telegram) ---
    max_length = 3500
    if len(text) <= max_length:
        await message.answer(text)
    else:
        parts = []
        current_part = ""
        for line in text.split('\n'):
            if len(current_part) + len(line) + 1 <= max_length:
                current_part += line + '\n'
            else:
                parts.append(current_part)
                current_part = line + '\n'
        if current_part:
            parts.append(current_part)

        for part in parts:
            await message.answer(part)

    # --- Возвращаемся к меню фильтра ---
    await message.answer(
        "Выбери другой тип или начни редактирование:",
        reply_markup=get_filter_matches_keyboard()
    )


async def match_number_entered(message: types.Message, state: FSMContext):
    """Номер введен, показываем детали"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return
    
    # Проверяем что это число
    try:
        match_number = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число!")
        return
    
    data = await state.get_data()
    all_matches = data.get('edit_all_matches', [])
    
    if match_number < 1 or match_number > len(all_matches):
        await message.answer(f"❌ Номер должен быть от 1 до {len(all_matches)}")
        return
    
    # Получаем выбранное сопоставление (индекс с 0)
    selected_match_obj = all_matches[match_number - 1]
    match_type = selected_match_obj['type']
    match_data = selected_match_obj['data']
    
    # Сохраняем номер и тип
    await state.update_data(
        edit_match_index=match_number - 1,
        edit_match_type=match_type,
        edit_match_data=match_data
    )
    
    # Показываем текущее сопоставление в зависимости от типа
    wb_col = match_data.get('column_1', '—')
    ozon_col = match_data.get('column_2', '—')
    yandex_col = match_data.get('column_3', '—')
    confidence = match_data.get('confidence', 0)
    description = match_data.get('description', '')
    
    # Определяем какие столбцы отсутствуют (N/A)
    if match_type == 'pair_1_2':  # WB + Ozon, нет Яндекса
        yandex_col = 'N/A'
    elif match_type == 'pair_1_3':  # WB + Яндекс, нет Ozon
        ozon_col = 'N/A'
    elif match_type == 'pair_2_3':  # Ozon + Яндекс, нет WB
        wb_col = 'N/A'
    # Для 'triple' все столбцы уже есть
    
    # Формируем красивый вывод
    text = f"📋 Сопоставление #{match_number}\n"
    
    # Указываем тип
    if match_type == 'triple':
        text += "🎯 Тип: Тройное\n\n"
    elif match_type == 'pair_1_2':
        text += "🔗 Тип: Парное (WB + Ozon)\n\n"
    elif match_type == 'pair_1_3':
        text += "🔗 Тип: Парное (WB + Яндекс)\n\n"
    elif match_type == 'pair_2_3':
        text += "🔗 Тип: Парное (Ozon + Яндекс)\n\n"
    
    text += f"🔹 WB: {wb_col}\n"
    text += f"🔸 Ozon: {ozon_col}\n"
    text += f"🔹 Яндекс: {yandex_col}\n"
    text += f"📈 Уверенность: {confidence:.0%}\n"
    if description:
        text += f"💬 {description}\n"
    
    await message.answer(text)
    await state.set_state(SchemaStates.selecting_column_to_edit)
    await message.answer("Что хочешь изменить?", reply_markup=get_edit_column_keyboard())


async def column_selected_for_edit(message: types.Message, state: FSMContext):
    """Выбран столбец для редактирования"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return
    
    if message.text == "🗑 Удалить сопоставление":
        await delete_match_confirm(message, state)
        return
    
    # Определяем какой столбец редактируем
    if message.text == "📝 Изменить WB столбец":
        marketplace = 'wildberries'
        column_key = 'column_1'
        display_name = 'WB'
    elif message.text == "📝 Изменить Ozon столбец":
        marketplace = 'ozon'
        column_key = 'column_2'
        display_name = 'Ozon'
    elif message.text == "📝 Изменить Яндекс столбец":
        marketplace = 'yandex'
        column_key = 'column_3'
        display_name = 'Яндекс'
    else:
        await message.answer("❌ Неизвестная команда")
        return
    
    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    columns_list = available_columns.get(marketplace, [])
    
    if not columns_list:
        await message.answer("❌ Не удалось загрузить список столбцов")
        return
    
    await state.update_data(
        edit_marketplace=marketplace,
        edit_column_key=column_key,
        edit_display_name=display_name
    )
    
    # Показываем список доступных столбцов
    text = f"📋 Доступные столбцы {display_name} ({len(columns_list)}):\n\n"
    for i, col in enumerate(columns_list, 1):
        text += f"{i}. {col}\n"
        # Разбиваем на части
        if i % 30 == 0:
            await message.answer(text)
            text = ""
    
    if text:
        await message.answer(text)
    
    await message.answer(
        f"Введи название столбца из списка выше или номер (1-{len(columns_list)}):",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(SchemaStates.selecting_new_column_value)


async def new_column_value_entered(message: types.Message, state: FSMContext):
    """Новое значение введено, валидируем и сохраняем"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return
    
    user_input = message.text.strip()
    
    if not user_input:
        await message.answer("❌ Название столбца не может быть пустым!")
        return
    
    data = await state.get_data()
    marketplace = data.get('edit_marketplace')
    available_columns = data.get('available_columns', {})
    columns_list = available_columns.get(marketplace, [])
    
    # НОВОЕ: Проверяем ввод "NA" для удаления столбца
    if user_input.upper() == 'NA':
        # Пользователь хочет удалить столбец (тройное → парное)
        new_value = None  # Устанавливаем пустое значение
    else:
        # Валидация обычного значения
        new_value = None
        
        # Проверяем, может это номер
        try:
            col_number = int(user_input)
            if 1 <= col_number <= len(columns_list):
                new_value = columns_list[col_number - 1]
        except ValueError:
            # Не номер, ищем по точному совпадению
            if user_input in columns_list:
                new_value = user_input
            else:
                # Ищем похожее (case-insensitive)
                user_lower = user_input.lower()
                for col in columns_list:
                    if col.lower() == user_lower:
                        new_value = col
                        break
        
        if not new_value:
            await message.answer(
                f"❌ Столбец '{user_input}' не найден в шаблоне {data.get('edit_display_name')}!\n\n"
                f"Введи точное название или номер из списка.\n"
                f"💡 Чтобы удалить столбец (тройное → парное), введи: NA"
            )
            return
    
    # Получаем данные для обновления
    schema_id = data.get('edit_schema_id')
    schema_name = data.get('edit_schema_name')
    all_matches = data.get('edit_all_matches', [])
    match_index = data.get('edit_match_index')
    match_type = data.get('edit_match_type')
    column_key = data.get('edit_column_key')
    display_name = data.get('edit_display_name')
    matches_data = data.get('edit_matches_data', {})
    
    # Получаем текущее сопоставление из all_matches
    current_match_obj = all_matches[match_index]
    current_match = current_match_obj['data']
    
    # Сохраняем старое значение для отчета
    old_value = current_match.get(column_key, 'N/A')
    if not old_value or old_value == '':
        old_value = 'N/A'
    
    # Обновляем значение (может быть None для удаления)
    if new_value is None:
        current_match[column_key] = ''  # Очищаем столбец
        display_new_value = 'N/A'  # Для отображения
    else:
        current_match[column_key] = new_value
        display_new_value = new_value
    
    # КРИТИЧНО: Определяем новый тип сопоставления
    wb_exists = bool(current_match.get('column_1'))
    ozon_exists = bool(current_match.get('column_2'))
    yandex_exists = bool(current_match.get('column_3'))
    
    # Определяем новый тип
    new_type = None
    if wb_exists and ozon_exists and yandex_exists:
        new_type = 'triple'
    elif wb_exists and ozon_exists and not yandex_exists:
        new_type = 'pair_1_2'
    elif wb_exists and not ozon_exists and yandex_exists:
        new_type = 'pair_1_3'
    elif not wb_exists and ozon_exists and yandex_exists:
        new_type = 'pair_2_3'
    else:
        # Некорректное состояние (меньше 2 столбцов)
        await message.answer("❌ Сопоставление должно содержать минимум 2 маркетплейса!")
        return
    
    # Если тип изменился - нужно переместить между группами
    if new_type != match_type:
        # Удаляем из старой группы
        old_group_key = _get_group_key(match_type)
        old_group = matches_data.get(old_group_key, [])
        
        # Находим индекс в старой группе
        index_in_old_group = 0
        for i in range(match_index):
            if all_matches[i]['type'] == match_type:
                index_in_old_group += 1
        
        # Удаляем из старой группы
        if old_group_key in matches_data and index_in_old_group < len(matches_data[old_group_key]):
            matches_data[old_group_key].pop(index_in_old_group)
        
        # Добавляем в новую группу
        new_group_key = _get_group_key(new_type)
        if new_group_key not in matches_data:
            matches_data[new_group_key] = []
        matches_data[new_group_key].append(current_match)
        
        # Обновляем тип в all_matches
        current_match_obj['type'] = new_type
        
        type_changed = True
        type_change_text = f"\n🔄 Тип изменен: {_format_type(match_type)} → {_format_type(new_type)}"
    else:
        # Тип не изменился, просто обновляем в той же группе
        group_key = _get_group_key(match_type)
        group = matches_data.get(group_key, [])
        
        # Находим индекс в группе
        index_in_group = 0
        for i in range(match_index):
            if all_matches[i]['type'] == match_type:
                index_in_group += 1
        
        # Обновляем в группе
        if group_key in matches_data and index_in_group < len(matches_data[group_key]):
            matches_data[group_key][index_in_group] = current_match
        
        type_changed = False
        type_change_text = ""
    
    # Сохраняем в БД
    db.save_schema_matches(schema_id, matches_data)
    
    # Очищаем временные файлы
    user_id = message.from_user.id
    if user_id in user_schemas:
        user_schemas[user_id] = {}
    
    await state.clear()
    
    text = f"✅ Сопоставление обновлено!\n\n"
    text += f"📋 Схема: {schema_name}\n"
    text += f"📝 Столбец {display_name}:\n"
    text += f"  Было: {old_value}\n"
    text += f"  Стало: {display_new_value}"
    text += type_change_text
    
    await message.answer(text)
    
    # Возвращаемся к меню редактирования
    await edit_schema_start(message, state)


# Вспомогательные функции для работы с типами
def _get_group_key(match_type: str) -> str:
    """Получить ключ группы для типа сопоставления"""
    type_to_key = {
        'triple': 'matches_all_three',
        'pair_1_2': 'matches_1_2',
        'pair_1_3': 'matches_1_3',
        'pair_2_3': 'matches_2_3'
    }
    return type_to_key.get(match_type, 'matches_all_three')


def _format_type(match_type: str) -> str:
    """Форматировать тип для отображения"""
    type_names = {
        'triple': 'Тройное',
        'pair_1_2': 'Парное (WB+Ozon)',
        'pair_1_3': 'Парное (WB+Яндекс)',
        'pair_2_3': 'Парное (Ozon+Яндекс)'
    }
    return type_names.get(match_type, 'Неизвестно')


async def delete_match_confirm(message: types.Message, state: FSMContext):
    """Удаление сопоставления (без затирания других групп)"""
    data = await state.get_data()

    schema_id = data.get('edit_schema_id')
    schema_name = data.get('edit_schema_name')
    match_index = data.get('edit_match_index')
    match_type = data.get('edit_match_type')

    if schema_id is None or match_index is None or not match_type:
        await message.answer(
            "❌ Не удалось определить, какое сопоставление удалять. Попробуй выбрать сопоставление заново."
        )
        await edit_schema_start(message, state)
        return

    matches_data = data.get('edit_matches_data') or {}
    all_matches = data.get('edit_all_matches', [])

    if match_index < 0 or match_index >= len(all_matches):
        await message.answer("❌ Некорректный индекс сопоставления.")
        await edit_schema_start(message, state)
        return

    # Забираем удаляемый объект для красивого отчёта
    match_obj = all_matches[match_index]
    deleted_match = match_obj.get('data', {})

    # Удаляем из all_matches (единый список)
    all_matches.pop(match_index)

    # Удаляем из соответствующей группы в matches_data
    group_key = _get_group_key(match_type)
    group_list = matches_data.get(group_key, [])

    # Найдём индекс в группе: считаем, сколько таких же типов было ДО match_index
    index_in_group = 0
    for i in range(match_index):
        if all_matches[i].get('type') == match_type:
            index_in_group += 1

    if 0 <= index_in_group < len(group_list):
        group_list.pop(index_in_group)

    matches_data[group_key] = group_list

    # Важно: не потерять остальные группы
    for key in ('matches_all_three', 'matches_1_2', 'matches_1_3', 'matches_2_3'):
        matches_data.setdefault(key, [])

    db.save_schema_matches(schema_id, matches_data)

    await state.update_data(
        edit_matches_data=matches_data,
        edit_all_matches=all_matches
    )

    await state.clear()

    text = "✅ Сопоставление удалено!\n\n"
    text += f"📋 Схема: {schema_name}\n"
    text += "🗑 Удалено:\n"
    text += f"  WB: {deleted_match.get('column_1', '—')}\n"
    text += f"  Ozon: {deleted_match.get('column_2', '—')}\n"
    text += f"  Яндекс: {deleted_match.get('column_3', '—')}"

    await message.answer(text)
    await edit_schema_start(message, state)


# ===== ДОБАВЛЕНИЕ НОВОГО СОПОСТАВЛЕНИЯ =====

async def add_new_match_start(message: types.Message, state: FSMContext):
    """Начало добавления нового сопоставления"""
    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    
    wb_columns = available_columns.get('wildberries', [])
    if not wb_columns:
        await message.answer("❌ Не удалось загрузить столбцы WB")
        return
    
    # Показываем список доступных столбцов WB
    text = f"📋 Доступные столбцы WB ({len(wb_columns)}):\n\n"
    for i, col in enumerate(wb_columns, 1):
        text += f"{i}. {col}\n"
        if i % 30 == 0:
            await message.answer(text)
            text = ""
    
    if text:
        await message.answer(text)
    
    await state.set_state(SchemaStates.selecting_wb_column)
    await message.answer(
        f"Шаг 1/3: Выбери столбец WB\n\n"
        f"Введи название или номер (1-{len(wb_columns)}):",
        reply_markup=get_cancel_keyboard()
    )


async def wb_column_selected(message: types.Message, state: FSMContext):
    """Столбец WB выбран"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return
    
    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    wb_columns = available_columns.get('wildberries', [])
    
    # Валидация выбора
    wb_value = None
    user_input = message.text.strip()
    
    try:
        col_number = int(user_input)
        if 1 <= col_number <= len(wb_columns):
            wb_value = wb_columns[col_number - 1]
    except ValueError:
        if user_input in wb_columns:
            wb_value = user_input
        else:
            user_lower = user_input.lower()
            for col in wb_columns:
                if col.lower() == user_lower:
                    wb_value = col
                    break
    
    if not wb_value:
        await message.answer(
            f"❌ Столбец '{user_input}' не найден!\n\n"
            f"Введи точное название или номер из списка."
        )
        return
    
    # Сохраняем выбор
    await state.update_data(new_match_wb=wb_value)
    
    # Переходим к Ozon
    ozon_columns = available_columns.get('ozon', [])
    
    text = f"✅ WB: {wb_value}\n\n"
    text += f"📋 Доступные столбцы Ozon ({len(ozon_columns)}):\n\n"
    for i, col in enumerate(ozon_columns, 1):
        text += f"{i}. {col}\n"
        if i % 30 == 0:
            await message.answer(text)
            text = ""
    
    if text:
        await message.answer(text)
    
    await state.set_state(SchemaStates.selecting_ozon_column)
    await message.answer(
        f"Шаг 2/3: Выбери столбец Ozon\n\n"
        f"Введи название или номер (1-{len(ozon_columns)}):",
        reply_markup=get_cancel_keyboard()
    )


async def ozon_column_selected(message: types.Message, state: FSMContext):
    """Столбец Ozon выбран"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return
    
    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    ozon_columns = available_columns.get('ozon', [])
    
    # Валидация выбора
    ozon_value = None
    user_input = message.text.strip()
    
    try:
        col_number = int(user_input)
        if 1 <= col_number <= len(ozon_columns):
            ozon_value = ozon_columns[col_number - 1]
    except ValueError:
        if user_input in ozon_columns:
            ozon_value = user_input
        else:
            user_lower = user_input.lower()
            for col in ozon_columns:
                if col.lower() == user_lower:
                    ozon_value = col
                    break
    
    if not ozon_value:
        await message.answer(
            f"❌ Столбец '{user_input}' не найден!\n\n"
            f"Введи точное название или номер из списка."
        )
        return
    
    # Сохраняем выбор
    await state.update_data(new_match_ozon=ozon_value)
    
    # Переходим к Яндекс
    yandex_columns = available_columns.get('yandex', [])
    
    text = f"✅ WB: {data.get('new_match_wb')}\n"
    text += f"✅ Ozon: {ozon_value}\n\n"
    text += f"📋 Доступные столбцы Яндекс ({len(yandex_columns)}):\n\n"
    for i, col in enumerate(yandex_columns, 1):
        text += f"{i}. {col}\n"
        if i % 30 == 0:
            await message.answer(text)
            text = ""
    
    if text:
        await message.answer(text)
    
    await state.set_state(SchemaStates.selecting_yandex_column)
    await message.answer(
        f"Шаг 3/3: Выбери столбец Яндекс\n\n"
        f"Введи название или номер (1-{len(yandex_columns)}):",
        reply_markup=get_cancel_keyboard()
    )


async def yandex_column_selected(message: types.Message, state: FSMContext):
    """Столбец Яндекс выбран, сохраняем сопоставление (без затирания других групп)"""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    yandex_columns = available_columns.get('yandex', [])

    # Валидация выбора
    yandex_value = None
    user_input = message.text.strip()

    try:
        col_number = int(user_input)
        if 1 <= col_number <= len(yandex_columns):
            yandex_value = yandex_columns[col_number - 1]
    except ValueError:
        if user_input in yandex_columns:
            yandex_value = user_input
        else:
            user_lower = user_input.lower()
            for col in yandex_columns:
                if col.lower() == user_lower:
                    yandex_value = col
                    break

    if not yandex_value:
        await message.answer(
            f"❌ Столбец '{user_input}' не найден!\n\n"
            f"Введи точное название или номер из списка."
        )
        return

    # Собираем новое сопоставление
    wb_value = data.get('new_match_wb')
    ozon_value = data.get('new_match_ozon')

    if not wb_value or not ozon_value:
        await message.answer(
            "❌ Не удалось восстановить выбранные столбцы WB/Ozon.\n"
            "Попробуй начать добавление заново.",
            reply_markup=get_filter_matches_keyboard()
        )
        await state.set_state(SchemaStates.choosing_edit_action)
        return

    new_match = {
        'column_1': wb_value,
        'column_2': ozon_value,
        'column_3': yandex_value,
        'confidence': 1.0,  # Ручное сопоставление = 100%
        'description': 'Добавлено вручную'
    }

    schema_id = data.get('edit_schema_id')
    schema_name = data.get('edit_schema_name')

    if not schema_id:
        await message.answer(
            "❌ Не удалось определить схему (schema_id). Попробуй выбрать схему заново."
        )
        await edit_schema_start(message, state)
        return

    # Берём полную структуру сопоставлений (чтобы ничего не затереть)
    matches_data = data.get('edit_matches_data') or {}
    matches_all_three = matches_data.get('matches_all_three', [])
    matches_1_2 = matches_data.get('matches_1_2', [])
    matches_1_3 = matches_data.get('matches_1_3', [])
    matches_2_3 = matches_data.get('matches_2_3', [])

    # Проверка на дубликат (по тройным)
    is_duplicate = any(
        m.get('column_1') == wb_value and
        m.get('column_2') == ozon_value and
        m.get('column_3') == yandex_value
        for m in matches_all_three
    )

    if is_duplicate:
        await message.answer(
            "⚠️ Такое сопоставление уже существует!\n\n"
            f"WB: {wb_value}\n"
            f"Ozon: {ozon_value}\n"
            f"Яндекс: {yandex_value}",
            reply_markup=get_filter_matches_keyboard()
        )
        await state.set_state(SchemaStates.choosing_edit_action)
        return

    # Добавляем в ТРОЙНЫЕ (и сохраняем ПОЛНУЮ структуру)
    matches_all_three.append(new_match)
    matches_data['matches_all_three'] = matches_all_three
    matches_data['matches_1_2'] = matches_1_2
    matches_data['matches_1_3'] = matches_1_3
    matches_data['matches_2_3'] = matches_2_3

    db.save_schema_matches(schema_id, matches_data)

    # Также обновим edit_all_matches в state, чтобы UI/нумерация не рассинхронизировались
    all_matches = data.get('edit_all_matches', [])
    all_matches.append({'type': 'triple', 'data': new_match})

    await state.update_data(
        edit_matches_data=matches_data,
        edit_all_matches=all_matches
    )

    # Очищаем временные загруженные файлы пользователя (как у тебя было)
    user_id = message.from_user.id
    if user_id in user_schemas:
        user_schemas[user_id] = {}

    await state.clear()

    total_count = (
        len(matches_data.get('matches_all_three', [])) +
        len(matches_data.get('matches_1_2', [])) +
        len(matches_data.get('matches_1_3', [])) +
        len(matches_data.get('matches_2_3', []))
    )

    text = "✅ Новое сопоставление добавлено!\n\n"
    text += f"📋 Схема: {schema_name}\n"
    text += f"📊 Всего сопоставлений: {total_count}\n\n"
    text += "Добавлено:\n"
    text += f"🔹 WB: {wb_value}\n"
    text += f"🔸 Ozon: {ozon_value}\n"
    text += f"🔹 Яндекс: {yandex_value}"

    await message.answer(text)
    await edit_schema_start(message, state)


def register_schema_edit_handlers(dp, bot):
    """Регистрация обработчиков редактирования схем"""
    from functools import partial
    
    dp.message.register(edit_schema_start, F.text == "✏️ Редактировать схему")
    
    # Просмотр
    dp.message.register(view_matches_start, F.text == "👁 Просмотреть текущие сопоставления")
    dp.message.register(show_schema_matches, SchemaStates.selecting_schema_to_view)
    
    # Редактирование - выбор схемы
    dp.message.register(edit_match_start, F.text == "✏️ Изменить сопоставление")
    dp.message.register(schema_selected_for_edit, SchemaStates.selecting_schema_to_edit)
    
    # Загрузка файлов для валидации
    dp.message.register(partial(handle_edit_validation_file, bot=bot), SchemaStates.waiting_edit_files, F.document)
    
    # ВАЖНО: Выбор действия ПОСЛЕ загрузки файлов (в специальном состоянии)
    dp.message.register(edit_action_selected, SchemaStates.choosing_edit_action)
    
    # Изменение существующего сопоставления
    dp.message.register(match_number_entered, SchemaStates.entering_match_number)
    dp.message.register(column_selected_for_edit, SchemaStates.selecting_column_to_edit)
    dp.message.register(new_column_value_entered, SchemaStates.selecting_new_column_value)
    
    # Добавление нового сопоставления
    dp.message.register(wb_column_selected, SchemaStates.selecting_wb_column)
    dp.message.register(ozon_column_selected, SchemaStates.selecting_ozon_column)
    dp.message.register(yandex_column_selected, SchemaStates.selecting_yandex_column)
