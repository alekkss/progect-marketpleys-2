"""
Обработчики обновления схем.

Поддерживает два типа:
    - standard (3 МП): загрузка 3 Excel → AI-пересопоставление оставшихся
    - mvm (3 МП + XML): загрузка 3 Excel + XML → AI-пересопоставление 4 источников

Принцип Open/Closed: МВМ-логика добавлена через отдельные StatesGroup
и хендлеры, стандартный флоу не изменён.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import logging
from aiogram import types, F
from aiogram.types import ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

from bot.states import SchemaStates, SchemaUpdateMvmStates
from bot.keyboards import (
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_update_schema_keyboard,
    get_schema_list_keyboard,
    get_mvm_waiting_xml_keyboard,
)
from bot.storage import user_schemas, db
from bot.utils import download_file
from bot.handlers.common import schema_management

from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from utils.xml_reader import XmlReader
from services.ai_comparator import AIComparator
from bot.security import AccessManager

logger = logging.getLogger('schema_update')


# =====================================================================
#  ОБЩИЙ ВХОД: ВЫБОР СХЕМЫ
# =====================================================================

async def update_schema_start(message: types.Message, state: FSMContext):
    """Начало обновления схемы — выбор из списка"""
    user_id = message.from_user.id

    can_see_all = AccessManager.can_see_all_schemas(user_id)
    schemas = db.get_user_schemas(user_id, all_schemas=can_see_all)

    if not schemas:
        await message.answer("❌ У тебя нет схем!")
        return

    keyboard = get_schema_list_keyboard(schemas)
    if not keyboard:
        await message.answer("❌ У тебя нет валидных схем!")
        return

    # Формируем текст с типами и владельцами
    text = "🔄 Выбери схему для обновления:\n\n"

    if can_see_all:
        for i, schema in enumerate(schemas, 1):
            owner_info = f" (владелец: {schema.get('owner_name', 'неизвестен')})"
            s_type = schema.get('schema_type', 'standard')
            type_icon = "📦" if s_type == 'mvm' else "📊"
            text += f"{i}. {type_icon} {schema['name']}{owner_info}\n"
    else:
        for i, schema in enumerate(schemas, 1):
            s_type = schema.get('schema_type', 'standard')
            type_icon = "📦" if s_type == 'mvm' else "📊"
            text += f"{i}. {type_icon} {schema['name']}\n"

    await state.set_state(SchemaStates.selecting_schema_to_update)
    await message.answer(text, reply_markup=keyboard)


async def schema_selected_for_update(message: types.Message, state: FSMContext):
    """Схема выбрана — определяем тип и направляем в нужный флоу"""
    if message.text == "❌ Отмена":
        await schema_management(message, state)
        return

    user_id = message.from_user.id
    schema_name = message.text

    can_see_all = AccessManager.can_see_all_schemas(user_id)

    if can_see_all:
        schema = db.get_schema_by_name_global(schema_name)
    else:
        schema = db.get_schema(user_id, schema_name)

    if not schema:
        await message.answer("❌ Схема не найдена")
        return

    schema_id = schema['id']
    schema_type = db.get_schema_type(schema_id)

    owner_warning = ""
    if can_see_all and schema.get('owner_id') != user_id:
        owner_warning = f"\n\n⚠️ Вы обновляете схему другого пользователя (ID: {schema['owner_id']})"

    if schema_type == 'mvm':
        # МВМ-флоу
        await state.update_data(
            update_schema_id=schema_id,
            update_schema_name=schema['name'],
            update_schema_type='mvm',
            mvm_mp_files_processed=False,
        )
        user_schemas[user_id] = {}
        await state.set_state(SchemaUpdateMvmStates.waiting_mp_files)

        await message.answer(
            f"📦 МВМ-схема '{schema['name']}' выбрана{owner_warning}\n\n"
            "📤 Отправь 3 файла Excel для повторного анализа\n"
            "(wb, ozon, yandex)",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        # Стандартный флоу
        await state.update_data(
            update_schema_id=schema_id,
            update_schema_name=schema['name'],
            update_schema_type='standard',
        )
        user_schemas[user_id] = {}
        await state.set_state(SchemaStates.waiting_update_files)

        await message.answer(
            f"✅ Схема '{schema['name']}' выбрана{owner_warning}\n\n"
            "Отправь 3 файла Excel для повторного анализа",
            reply_markup=ReplyKeyboardRemove()
        )


# =====================================================================
#  СТАНДАРТНЫЙ ФЛОУ (3 МП) — без изменений
# =====================================================================

async def handle_update_file(message: types.Message, state: FSMContext, bot):
    """Обработка файла при обновлении стандартной схемы"""
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

        await message.answer(
            "✅ Все файлы загружены!",
            reply_markup=get_update_schema_keyboard()
        )


async def finalize_schema_update(message: types.Message, state: FSMContext):
    """Финализация обновления стандартной схемы"""
    current_state = await state.get_state()
    if current_state != SchemaStates.waiting_update_files:
        await message.answer("❌ Сначала выбери схему для обновления")
        return

    user_id = message.from_user.id

    if user_id not in user_schemas or len(user_schemas[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла!")
        return

    data = await state.get_data()
    schema_id = data.get('update_schema_id')
    schema_name = data.get('update_schema_name')

    if not schema_id or not schema_name:
        await message.answer("❌ Данные схемы потеряны. Начни заново.")
        return

    await message.answer(f"⏳ Анализирую столбцы для схемы '{schema_name}'...")

    try:
        file_paths = user_schemas[user_id]

        reader = ExcelReader()
        all_columns = {}

        for marketplace, file_path in file_paths.items():
            config = FILE_CONFIGS[marketplace]
            all_columns[marketplace] = reader.get_column_names(
                file_path, config['sheet_name'], config['header_row']
            )

        existing_matches = db.get_schema_matches(schema_id)

        # Собираем уже сопоставленные столбцы
        matched_wb, matched_ozon, matched_yandex = _collect_matched_columns_standard(existing_matches)

        remaining_columns = {
            'wildberries': [c for c in all_columns['wildberries'] if c not in matched_wb],
            'ozon': [c for c in all_columns['ozon'] if c not in matched_ozon],
            'yandex': [c for c in all_columns['yandex'] if c not in matched_yandex],
        }

        total_remaining = sum(len(v) for v in remaining_columns.values())

        if total_remaining == 0:
            user_schemas[user_id] = {}
            await state.clear()
            await message.answer(
                f"ℹ️ Все столбцы уже сопоставлены!\n\n"
                f"Схема '{schema_name}' не требует обновления",
                reply_markup=get_main_menu_keyboard()
            )
            return

        await message.answer(
            f"🔍 Несопоставленных столбцов:\n"
            f"• WB: {len(remaining_columns['wildberries'])}\n"
            f"• Ozon: {len(remaining_columns['ozon'])}\n"
            f"• Яндекс: {len(remaining_columns['yandex'])}\n\n"
            f"🤖 AI ищет новые совпадения..."
        )

        comparator = AIComparator()
        new_comparison_result = comparator.compare_columns(
            remaining_columns['wildberries'],
            remaining_columns['ozon'],
            remaining_columns['yandex'],
        )

        new_count, skipped_count = _merge_new_standard_matches(existing_matches, new_comparison_result)

        if new_count > 0:
            db.save_schema_matches(schema_id, existing_matches)

        user_schemas[user_id] = {}
        await state.clear()

        _text = _build_update_result_text(schema_name, new_count, skipped_count,
                                          len(existing_matches.get('matches_all_three', [])),
                                          total_remaining)
        await message.answer(_text, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка обновления стандартной схемы: {e}", exc_info=True)


# =====================================================================
#  МВМ-ФЛОУ (3 МП + XML)
# =====================================================================

async def handle_mvm_update_mp_file(message: types.Message, state: FSMContext, bot):
    """Загрузка файла МП при обновлении МВМ-схемы"""
    user_id = message.from_user.id

    if user_id not in user_schemas:
        user_schemas[user_id] = {}

    data = await state.get_data()
    if data.get('mvm_mp_files_processed'):
        return

    file_path, file_name, marketplace = await download_file(bot, message, user_id)

    if not marketplace:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)")
        return

    if marketplace in user_schemas[user_id]:
        await message.answer(f"⚠️ {marketplace.upper()} уже загружен")
        return

    user_schemas[user_id][marketplace] = file_path
    loaded = len(user_schemas[user_id])
    await message.answer(f"✅ {marketplace.upper()} ({loaded}/3)")

    if loaded == 3:
        data = await state.get_data()
        if data.get('mvm_mp_files_processed'):
            return
        await state.update_data(mvm_mp_files_processed=True)

        await state.set_state(SchemaUpdateMvmStates.waiting_xml_file)
        await message.answer(
            "✅ Все 3 шаблона МП загружены!\n\n"
            "📎 Теперь отправь XML файл каталога:",
            reply_markup=get_mvm_waiting_xml_keyboard()
        )


async def handle_mvm_update_mp_text(message: types.Message, state: FSMContext):
    """Текст в состоянии ожидания МП при обновлении МВМ"""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
    else:
        await message.answer("📎 Отправь файл Excel как документ или нажми ❌ Отмена")


async def handle_mvm_update_xml_file(message: types.Message, state: FSMContext, bot):
    """Загрузка XML файла при обновлении МВМ-схемы"""
    if not message.document:
        if message.text == "❌ Отмена":
            user_id = message.from_user.id
            if user_id in user_schemas:
                user_schemas[user_id] = {}
            await state.clear()
            await schema_management(message, state)
            return
        await message.answer("📎 Отправь XML файл как документ или нажми ❌ Отмена")
        return

    user_id = message.from_user.id
    file_name = message.document.file_name or ""

    if not file_name.lower().endswith('.xml'):
        await message.answer("❌ Нужен файл с расширением .xml")
        return

    file = await bot.get_file(message.document.file_id)
    downloads_dir = Path("downloads") / str(user_id)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    xml_path = str(downloads_dir / file_name)
    await bot.download_file(file.file_path, xml_path)

    try:
        xml_reader = XmlReader()
        offer_count = xml_reader.get_offer_count(xml_path)
        if offer_count == 0:
            await message.answer("❌ XML файл не содержит офферов. Отправь другой файл:")
            return

        xml_fields = xml_reader.get_field_names(xml_path)
    except ValueError as e:
        await message.answer(f"❌ Ошибка чтения XML: {e}")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error(f"Ошибка XML при обновлении МВМ: {e}", exc_info=True)
        return

    await state.update_data(mvm_xml_file_path=xml_path)

    await message.answer(
        f"✅ XML файл загружен!\n"
        f"📦 Офферов: {offer_count}, полей: {len(xml_fields)}\n\n"
        "Нажми кнопку для запуска обновления:",
        reply_markup=get_update_schema_keyboard()
    )


async def handle_mvm_update_xml_text(message: types.Message, state: FSMContext):
    """Текст в состоянии ожидания XML при обновлении МВМ"""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
    elif message.text == "✅ Обновить схему":
        await finalize_mvm_schema_update(message, state)
    else:
        await message.answer("📎 Отправь XML файл или нажми ❌ Отмена")


async def finalize_mvm_schema_update(message: types.Message, state: FSMContext):
    """Финализация обновления МВМ-схемы: AI-пересопоставление 4 источников"""
    user_id = message.from_user.id

    if user_id not in user_schemas or len(user_schemas[user_id]) != 3:
        await message.answer("❌ Не хватает файлов МП. Начни заново.")
        return

    data = await state.get_data()
    schema_id = data.get('update_schema_id')
    schema_name = data.get('update_schema_name')
    xml_file_path = data.get('mvm_xml_file_path')

    if not schema_id or not schema_name:
        await message.answer("❌ Данные схемы потеряны. Начни заново.")
        return

    if not xml_file_path:
        await message.answer("❌ XML файл не загружен. Начни заново.")
        return

    await message.answer(
        f"⏳ Анализирую столбцы для МВМ-схемы '{schema_name}'...",
        reply_markup=ReplyKeyboardRemove()
    )

    try:
        file_paths = user_schemas[user_id]

        # Читаем столбцы из 3 МП
        reader = ExcelReader()
        all_columns = {}
        for marketplace, file_path in file_paths.items():
            config = FILE_CONFIGS[marketplace]
            all_columns[marketplace] = reader.get_column_names(
                file_path, config['sheet_name'], config['header_row']
            )

        # Читаем поля XML
        xml_reader = XmlReader()
        xml_fields = xml_reader.get_field_names(xml_file_path)

        # Получаем существующие сопоставления
        existing_matches = db.get_schema_matches(schema_id)

        # Собираем уже сопоставленные столбцы из всех 11 групп
        matched_sets = _collect_matched_columns_mvm(existing_matches)

        remaining_columns = {
            'wildberries': [c for c in all_columns['wildberries'] if c not in matched_sets['column_1']],
            'ozon': [c for c in all_columns['ozon'] if c not in matched_sets['column_2']],
            'yandex': [c for c in all_columns['yandex'] if c not in matched_sets['column_3']],
            'xml': [c for c in xml_fields if c not in matched_sets['column_4']],
        }

        total_remaining = sum(len(v) for v in remaining_columns.values())

        if total_remaining == 0:
            user_schemas[user_id] = {}
            await state.clear()
            await message.answer(
                f"ℹ️ Все столбцы уже сопоставлены!\n\n"
                f"МВМ-схема '{schema_name}' не требует обновления",
                reply_markup=get_main_menu_keyboard()
            )
            return

        await message.answer(
            f"🔍 Несопоставленных столбцов:\n"
            f"• WB: {len(remaining_columns['wildberries'])}\n"
            f"• Ozon: {len(remaining_columns['ozon'])}\n"
            f"• Яндекс: {len(remaining_columns['yandex'])}\n"
            f"• XML: {len(remaining_columns['xml'])}\n\n"
            f"🤖 AI ищет новые совпадения (4 источника)..."
        )

        comparator = AIComparator()
        new_comparison_result = comparator.compare_columns_mvm(
            remaining_columns['wildberries'],
            remaining_columns['ozon'],
            remaining_columns['yandex'],
            remaining_columns['xml'],
        )

        new_count, skipped_count = _merge_new_mvm_matches(existing_matches, new_comparison_result)

        if new_count > 0:
            db.save_schema_matches(schema_id, existing_matches)

        user_schemas[user_id] = {}
        await state.clear()

        _text = _build_mvm_update_result_text(
            schema_name, new_count, skipped_count, existing_matches, total_remaining
        )
        await message.answer(_text, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка обновления МВМ-схемы: {e}", exc_info=True)


# =====================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

def _collect_matched_columns_standard(existing_matches: dict) -> tuple:
    """
    Собирает множества уже сопоставленных столбцов для стандартной схемы.

    Returns:
        Кортеж (matched_wb, matched_ozon, matched_yandex)
    """
    matched_wb = set()
    matched_ozon = set()
    matched_yandex = set()

    # Все группы стандартной схемы
    all_groups_keys = ['matches_all_three', 'matches_1_2', 'matches_1_3', 'matches_2_3']

    for group_key in all_groups_keys:
        for match in existing_matches.get(group_key, []):
            if match.get('column_1'):
                matched_wb.add(match['column_1'])
            if match.get('column_2'):
                matched_ozon.add(match['column_2'])
            if match.get('column_3'):
                matched_yandex.add(match['column_3'])

    return matched_wb, matched_ozon, matched_yandex


def _collect_matched_columns_mvm(existing_matches: dict) -> dict:
    """
    Собирает множества уже сопоставленных столбцов для МВМ-схемы (все 11 групп).

    Returns:
        Словарь {'column_1': set, 'column_2': set, 'column_3': set, 'column_4': set}
    """
    matched = {
        'column_1': set(),
        'column_2': set(),
        'column_3': set(),
        'column_4': set(),
    }

    mvm_groups = [
        'matches_all_four',
        'matches_triple_1_2_3', 'matches_triple_1_2_4',
        'matches_triple_1_3_4', 'matches_triple_2_3_4',
        'matches_pair_1_2', 'matches_pair_1_3', 'matches_pair_1_4',
        'matches_pair_2_3', 'matches_pair_2_4', 'matches_pair_3_4',
    ]

    for group_key in mvm_groups:
        for match in existing_matches.get(group_key, []):
            for col_key in matched:
                val = match.get(col_key)
                if val:
                    matched[col_key].add(val)

    return matched


def _merge_new_standard_matches(existing_matches: dict, new_result: dict) -> tuple:
    """
    Добавляет новые совпадения из AI-результата в существующую стандартную схему.

    Returns:
        Кортеж (new_count, skipped_count)
    """
    new_count = 0
    skipped_count = 0

    standard_groups = ['matches_all_three', 'matches_1_2', 'matches_1_3', 'matches_2_3']

    for group_key in standard_groups:
        new_matches = new_result.get(group_key, [])
        existing_group = existing_matches.get(group_key, [])

        # Создаём множество существующих ключей для проверки дубликатов
        existing_keys = set()
        for m in existing_group:
            key = tuple(m.get(f'column_{i}', '') for i in range(1, 4))
            existing_keys.add(key)

        for match in new_matches:
            confidence = match.get('confidence', 0)
            if confidence < 0.85:
                skipped_count += 1
                continue

            key = tuple(match.get(f'column_{i}', '') for i in range(1, 4))
            if key not in existing_keys:
                existing_group.append(match)
                existing_keys.add(key)
                new_count += 1

        existing_matches[group_key] = existing_group

    return new_count, skipped_count


def _merge_new_mvm_matches(existing_matches: dict, new_result: dict) -> tuple:
    """
    Добавляет новые совпадения из AI-результата МВМ в существующую схему.

    Returns:
        Кортеж (new_count, skipped_count)
    """
    new_count = 0
    skipped_count = 0

    mvm_groups = [
        'matches_all_four',
        'matches_triple_1_2_3', 'matches_triple_1_2_4',
        'matches_triple_1_3_4', 'matches_triple_2_3_4',
        'matches_pair_1_2', 'matches_pair_1_3', 'matches_pair_1_4',
        'matches_pair_2_3', 'matches_pair_2_4', 'matches_pair_3_4',
    ]

    for group_key in mvm_groups:
        new_matches = new_result.get(group_key, [])
        existing_group = existing_matches.get(group_key, [])

        existing_keys = set()
        for m in existing_group:
            key = tuple(m.get(f'column_{i}', '') for i in range(1, 5))
            existing_keys.add(key)

        for match in new_matches:
            confidence = match.get('confidence', 0)
            if confidence < 0.85:
                skipped_count += 1
                continue

            key = tuple(match.get(f'column_{i}', '') for i in range(1, 5))
            if key not in existing_keys:
                existing_group.append(match)
                existing_keys.add(key)
                new_count += 1

        existing_matches[group_key] = existing_group

    return new_count, skipped_count


def _build_update_result_text(
    schema_name: str,
    new_count: int,
    skipped_count: int,
    total_matches: int,
    total_remaining: int,
) -> str:
    """Формирует итоговый текст для стандартного обновления."""
    if new_count > 0:
        text = f"✅ Схема '{schema_name}' обновлена!\n\n"
        text += f"➕ Добавлено новых совпадений: {new_count}\n"
        text += f"📊 Всего столбцов в схеме: {total_matches}"
        if skipped_count > 0:
            text += f"\n⚠️ Пропущено (< 85%): {skipped_count}"
    else:
        text = f"ℹ️ Новых совпадений не найдено\n\n"
        text += f"AI не нашел подходящих пар (>= 85%) среди {total_remaining} столбцов"
        if skipped_count > 0:
            text += f"\n⚠️ Пропущено (< 85%): {skipped_count}"

    return text


def _build_mvm_update_result_text(
    schema_name: str,
    new_count: int,
    skipped_count: int,
    existing_matches: dict,
    total_remaining: int,
) -> str:
    """Формирует итоговый текст для МВМ-обновления."""
    if new_count > 0:
        text = f"✅ МВМ-схема '{schema_name}' обновлена!\n\n"
        text += f"➕ Добавлено новых совпадений: {new_count}\n\n"

        # Статистика по группам
        labels = {
            'matches_all_four': '🎯 Четверные',
            'matches_triple_1_2_3': '🔷 Тройные (WB+Ozon+Яндекс)',
            'matches_triple_1_2_4': '🔷 Тройные (WB+Ozon+XML)',
            'matches_triple_1_3_4': '🔷 Тройные (WB+Яндекс+XML)',
            'matches_triple_2_3_4': '🔷 Тройные (Ozon+Яндекс+XML)',
            'matches_pair_1_2': '🔗 Парные (WB+Ozon)',
            'matches_pair_1_3': '🔗 Парные (WB+Яндекс)',
            'matches_pair_1_4': '🔗 Парные (WB+XML)',
            'matches_pair_2_3': '🔗 Парные (Ozon+Яндекс)',
            'matches_pair_2_4': '🔗 Парные (Ozon+XML)',
            'matches_pair_3_4': '🔗 Парные (Яндекс+XML)',
        }

        total = 0
        for key, label in labels.items():
            count = len(existing_matches.get(key, []))
            if count > 0:
                text += f"{label}: {count}\n"
                total += count

        text += f"\n📊 Всего сопоставлений: {total}"

        if skipped_count > 0:
            text += f"\n⚠️ Пропущено (< 85%): {skipped_count}"
    else:
        text = f"ℹ️ Новых совпадений не найдено\n\n"
        text += f"AI не нашел подходящих пар (>= 85%) среди {total_remaining} столбцов"
        if skipped_count > 0:
            text += f"\n⚠️ Пропущено (< 85%): {skipped_count}"

    return text


# =====================================================================
#  РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# =====================================================================

def register_schema_update_handlers(dp, bot):
    """Регистрация обработчиков обновления схем"""
    from functools import partial

    # Общий вход
    dp.message.register(update_schema_start, F.text == "🔄 Обновить схему")
    dp.message.register(schema_selected_for_update, SchemaStates.selecting_schema_to_update)

    # Стандартный флоу (3 МП)
    dp.message.register(
        partial(handle_update_file, bot=bot),
        SchemaStates.waiting_update_files, F.document
    )
    dp.message.register(finalize_schema_update, F.text == "✅ Обновить схему")

    # МВМ-флоу: загрузка 3 МП
    dp.message.register(
        partial(handle_mvm_update_mp_file, bot=bot),
        SchemaUpdateMvmStates.waiting_mp_files, F.document
    )
    dp.message.register(
        handle_mvm_update_mp_text,
        SchemaUpdateMvmStates.waiting_mp_files, F.text
    )

    # МВМ-флоу: загрузка XML
    dp.message.register(
        partial(handle_mvm_update_xml_file, bot=bot),
        SchemaUpdateMvmStates.waiting_xml_file, F.document
    )
    dp.message.register(
        handle_mvm_update_xml_text,
        SchemaUpdateMvmStates.waiting_xml_file, F.text
    )
