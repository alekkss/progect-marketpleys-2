"""
Обработчики редактирования схем (просмотр и изменение сопоставлений).

Поддерживает два типа схем:
    - standard (3 МП): WB + Ozon + Яндекс
    - mvm (3 МП + XML): WB + Ozon + Яндекс + XML каталог

Принцип Open/Closed: МВМ-логика добавлена через проверку schema_type,
существующая логика стандартных схем не модифицирована.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import logging
from aiogram import types, F
from aiogram.types import ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

from bot.states import SchemaStates
from bot.keyboards import (
    get_schema_edit_keyboard,
    get_cancel_keyboard,
    get_edit_column_keyboard,
    get_edit_column_keyboard_mvm,
    get_back_to_edit_keyboard,
    get_schema_list_keyboard,
    get_edit_match_menu_keyboard,
    get_filter_matches_keyboard,
    get_filter_matches_mvm_keyboard,
    get_mvm_waiting_xml_keyboard,
)
from bot.storage import user_schemas
from bot import storage
from bot.utils import download_file
from bot.handlers.common import cmd_start
from bot.security import AccessManager

from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from utils.xml_reader import XmlReader

logger = logging.getLogger('schema_edit')


# =====================================================================
#  КОНСТАНТЫ: группы сопоставлений
# =====================================================================

STANDARD_MATCH_GROUPS = [
    ('matches_all_three', 'triple', '🎯 Тройные сопоставления',
     ['column_1', 'column_2', 'column_3']),
    ('matches_1_2', 'pair_1_2', '🔗 Парные (WB + Ozon)',
     ['column_1', 'column_2']),
    ('matches_1_3', 'pair_1_3', '🔗 Парные (WB + Яндекс)',
     ['column_1', 'column_3']),
    ('matches_2_3', 'pair_2_3', '🔗 Парные (Ozon + Яндекс)',
     ['column_2', 'column_3']),
]

MVM_MATCH_GROUPS = [
    ('matches_all_four', 'quad', '🎯 Четверные (WB+Ozon+Яндекс+XML)',
     ['column_1', 'column_2', 'column_3', 'column_4']),
    ('matches_triple_1_2_3', 'triple_1_2_3', '🔷 Тройные (WB+Ozon+Яндекс)',
     ['column_1', 'column_2', 'column_3']),
    ('matches_triple_1_2_4', 'triple_1_2_4', '🔷 Тройные (WB+Ozon+XML)',
     ['column_1', 'column_2', 'column_4']),
    ('matches_triple_1_3_4', 'triple_1_3_4', '🔷 Тройные (WB+Яндекс+XML)',
     ['column_1', 'column_3', 'column_4']),
    ('matches_triple_2_3_4', 'triple_2_3_4', '🔷 Тройные (Ozon+Яндекс+XML)',
     ['column_2', 'column_3', 'column_4']),
    ('matches_pair_1_2', 'pair_1_2', '🔗 Парные (WB+Ozon)',
     ['column_1', 'column_2']),
    ('matches_pair_1_3', 'pair_1_3', '🔗 Парные (WB+Яндекс)',
     ['column_1', 'column_3']),
    ('matches_pair_1_4', 'pair_1_4', '🔗 Парные (WB+XML)',
     ['column_1', 'column_4']),
    ('matches_pair_2_3', 'pair_2_3', '🔗 Парные (Ozon+Яндекс)',
     ['column_2', 'column_3']),
    ('matches_pair_2_4', 'pair_2_4', '🔗 Парные (Ozon+XML)',
     ['column_2', 'column_4']),
    ('matches_pair_3_4', 'pair_3_4', '🔗 Парные (Яндекс+XML)',
     ['column_3', 'column_4']),
]

COLUMN_DISPLAY_NAMES = {
    'column_1': 'WB',
    'column_2': 'Ozon',
    'column_3': 'Яндекс',
    'column_4': 'XML',
}

ALL_COLUMN_KEYS = ['column_1', 'column_2', 'column_3', 'column_4']


# =====================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (синхронные — не обращаются к БД)
# =====================================================================

def _get_match_groups(schema_type: str) -> list:
    """Возвращает список групп сопоставлений для данного типа схемы."""
    if schema_type == 'mvm':
        return MVM_MATCH_GROUPS
    return STANDARD_MATCH_GROUPS


def _build_all_matches_list(matches_data: dict, schema_type: str) -> list:
    """Собирает единый список сопоставлений из всех групп."""
    groups = _get_match_groups(schema_type)
    all_matches = []
    for group_key, match_type, _, _ in groups:
        for match in matches_data.get(group_key, []):
            all_matches.append({'type': match_type, 'data': match})
    return all_matches


def _count_by_type(all_matches: list, match_type: str) -> int:
    """Считает количество сопоставлений заданного типа."""
    return sum(1 for m in all_matches if m['type'] == match_type)


def _get_group_key(match_type: str, schema_type: str) -> str:
    """Получает ключ БД для внутреннего типа сопоставления."""
    groups = _get_match_groups(schema_type)
    for group_key, mt, _, _ in groups:
        if mt == match_type:
            return group_key
    fallback = {
        'triple': 'matches_all_three',
        'pair_1_2': 'matches_1_2',
        'pair_1_3': 'matches_1_3',
        'pair_2_3': 'matches_2_3',
    }
    return fallback.get(match_type, 'matches_all_three')


def _format_type(match_type: str, schema_type: str) -> str:
    """Форматирует внутренний тип для отображения пользователю."""
    groups = _get_match_groups(schema_type)
    for _, mt, display_name, _ in groups:
        if mt == match_type:
            return display_name
    return 'Неизвестно'


def _get_column_keys_for_type(match_type: str, schema_type: str) -> list:
    """Возвращает список column_key, участвующих в данном типе."""
    groups = _get_match_groups(schema_type)
    for _, mt, _, col_keys in groups:
        if mt == match_type:
            return col_keys
    return ['column_1', 'column_2', 'column_3']


def _determine_new_match_type(match_data: dict, schema_type: str) -> str | None:
    """Определяет новый тип сопоставления по заполненным столбцам."""
    filled = set()
    for ck in ALL_COLUMN_KEYS:
        if match_data.get(ck):
            filled.add(ck)
    if schema_type == 'mvm':
        return _determine_mvm_type(filled)
    return _determine_standard_type(filled)


def _determine_standard_type(filled: set) -> str | None:
    """Определяет тип для стандартной схемы (3 МП)."""
    c1 = 'column_1' in filled
    c2 = 'column_2' in filled
    c3 = 'column_3' in filled
    count = sum([c1, c2, c3])
    if count < 2:
        return None
    if count == 3:
        return 'triple'
    if c1 and c2:
        return 'pair_1_2'
    if c1 and c3:
        return 'pair_1_3'
    if c2 and c3:
        return 'pair_2_3'
    return None


def _determine_mvm_type(filled: set) -> str | None:
    """Определяет тип для МВМ-схемы (3 МП + XML)."""
    c1 = 'column_1' in filled
    c2 = 'column_2' in filled
    c3 = 'column_3' in filled
    c4 = 'column_4' in filled
    count = sum([c1, c2, c3, c4])
    if count < 2:
        return None
    if count == 4:
        return 'quad'
    if count == 3:
        if c1 and c2 and c3:
            return 'triple_1_2_3'
        if c1 and c2 and c4:
            return 'triple_1_2_4'
        if c1 and c3 and c4:
            return 'triple_1_3_4'
        if c2 and c3 and c4:
            return 'triple_2_3_4'
    if c1 and c2:
        return 'pair_1_2'
    if c1 and c3:
        return 'pair_1_3'
    if c1 and c4:
        return 'pair_1_4'
    if c2 and c3:
        return 'pair_2_3'
    if c2 and c4:
        return 'pair_2_4'
    if c3 and c4:
        return 'pair_3_4'
    return None


def _format_match_line(match_data: dict, schema_type: str) -> str:
    """Форматирует одну строку сопоставления для отображения."""
    parts = []
    keys = (
        ALL_COLUMN_KEYS
        if schema_type == 'mvm'
        else ['column_1', 'column_2', 'column_3']
    )
    for ck in keys:
        val = match_data.get(ck, '') or 'N/A'
        parts.append(val)
    return ' | '.join(parts)


def _format_match_detail(match_data: dict, match_type: str, schema_type: str) -> str:
    """Форматирует детальное отображение сопоставления."""
    labels = {
        'column_1': '🔹 WB',
        'column_2': '🔸 Ozon',
        'column_3': '🔹 Яндекс',
        'column_4': '📦 XML',
    }
    keys = (
        ALL_COLUMN_KEYS
        if schema_type == 'mvm'
        else ['column_1', 'column_2', 'column_3']
    )
    active_keys = set(_get_column_keys_for_type(match_type, schema_type))
    lines = []
    for ck in keys:
        label = labels.get(ck, ck)
        val = match_data.get(ck, '')
        if not val and ck not in active_keys:
            val = 'N/A'
            label = f"❌ {COLUMN_DISPLAY_NAMES.get(ck, ck)}"
        elif not val:
            val = 'N/A'
        lines.append(f"{label}: {val}")
    confidence = match_data.get('confidence', 0)
    lines.append(f"📈 Уверенность: {confidence:.0%}")
    description = match_data.get('description', '')
    if description:
        lines.append(f"💬 {description}")
    return '\n'.join(lines)


def _get_stats_text(all_matches: list, schema_type: str) -> str:
    """Формирует текст статистики по типам сопоставлений."""
    groups = _get_match_groups(schema_type)
    lines = []
    for _, match_type, display_name, _ in groups:
        count = _count_by_type(all_matches, match_type)
        if count > 0:
            lines.append(f"{display_name}: {count}")
    return '\n'.join(lines)


def _get_edit_keyboard(schema_type: str):
    """Возвращает клавиатуру выбора столбца для редактирования."""
    if schema_type == 'mvm':
        return get_edit_column_keyboard_mvm()
    return get_edit_column_keyboard()


def _get_filter_keyboard(schema_type: str):
    """Возвращает клавиатуру фильтрации."""
    if schema_type == 'mvm':
        return get_filter_matches_mvm_keyboard()
    return get_filter_matches_keyboard()


def _build_columns_text(display_name: str, columns_list: list) -> str:
    """Формирует полный нумерованный список столбцов в одну строку."""
    text = f"📋 Доступные столбцы {display_name} ({len(columns_list)}):\n\n"
    for i, col in enumerate(columns_list, 1):
        text += f"{i}. {col}\n"
    return text


async def _send_long_text(
    message: types.Message,
    text: str,
    max_length: int = 3500,
) -> None:
    """Отправляет длинный текст, разбивая на части по границам строк."""
    if len(text) <= max_length:
        await message.answer(text)
        return
    current_pos = 0
    while current_pos < len(text):
        end_pos = current_pos + max_length
        if end_pos < len(text):
            last_newline = text.rfind('\n', current_pos, end_pos)
            if last_newline > current_pos:
                end_pos = last_newline + 1
        chunk = text[current_pos:end_pos]
        await message.answer(chunk)
        current_pos = end_pos


def _find_index_in_group(
    all_matches: list,
    match_index: int,
    match_type: str,
) -> int:
    """Находит позиционный индекс сопоставления внутри его группы."""
    index_in_group = 0
    for i in range(match_index):
        if all_matches[i]['type'] == match_type:
            index_in_group += 1
    return index_in_group


def _resolve_column_input(user_input: str, columns_list: list) -> str | None:
    """Ищет столбец по номеру или названию (с нечётким регистром)."""
    try:
        col_number = int(user_input)
        if 1 <= col_number <= len(columns_list):
            return columns_list[col_number - 1]
    except ValueError:
        pass
    if user_input in columns_list:
        return user_input
    user_lower = user_input.lower()
    for col in columns_list:
        if col.lower() == user_lower:
            return col
    return None


# =====================================================================
#  ПРОСМОТР СОПОСТАВЛЕНИЙ
# =====================================================================

async def edit_schema_start(message: types.Message, state: FSMContext) -> None:
    """Меню редактирования схемы."""
    await message.answer(
        "Редактирование схемы:\n\nВыбери действие:",
        reply_markup=get_schema_edit_keyboard(),
    )


async def view_matches_start(message: types.Message, state: FSMContext) -> None:
    """Выбор схемы для просмотра."""
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

    await state.set_state(SchemaStates.selecting_schema_to_view)
    await message.answer("Выбери схему для просмотра:", reply_markup=keyboard)


async def show_schema_matches(message: types.Message, state: FSMContext) -> None:
    """Отображение сопоставлений выбранной схемы (стандартная + МВМ)."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    user_id = message.from_user.id
    schema_name = message.text

    can_see_all = await AccessManager.can_see_all_schemas(user_id)
    if can_see_all:
        schema = await storage.db.get_schema_by_name_global(schema_name)
    else:
        schema = await storage.db.get_schema(user_id, schema_name)

    if not schema:
        await message.answer("❌ Схема не найдена")
        return

    schema_id = schema['id']
    schema_name = schema['name']
    schema_type = await storage.db.get_schema_type(schema_id)
    matches_data = await storage.db.get_schema_matches(schema_id)
    all_matches = _build_all_matches_list(matches_data, schema_type)
    total_matches = len(all_matches)

    if total_matches == 0:
        await state.clear()
        await message.answer(
            f"📋 Схема '{schema_name}'\n\n⚠️ Нет сопоставлений",
            reply_markup=get_back_to_edit_keyboard(),
        )
        return

    text_parts = []
    type_label = "МВМ " if schema_type == 'mvm' else ""
    text_parts.append(f"📋 {type_label}Схема: {schema_name}\n")
    text_parts.append(f"📊 Всего сопоставлений: {total_matches}\n\n")
    text_parts.append("─" * 40 + "\n\n")

    groups = _get_match_groups(schema_type)
    for group_key, match_type, display_name, col_keys in groups:
        group_matches = [m for m in all_matches if m['type'] == match_type]
        if not group_matches:
            continue
        text_parts.append(f"{display_name}: {len(group_matches)}\n\n")
        for match_obj in group_matches:
            global_idx = all_matches.index(match_obj) + 1
            match = match_obj['data']
            text_parts.append(f"#{global_idx}\n")
            text_parts.append(_format_match_detail(match, match_type, schema_type))
            text_parts.append("\n\n")
        text_parts.append("─" * 40 + "\n\n")

    await _send_long_text(message, ''.join(text_parts))
    await state.clear()
    await message.answer(
        "✅ Просмотр завершен",
        reply_markup=get_back_to_edit_keyboard(),
    )


# =====================================================================
#  ВЫБОР СХЕМЫ ДЛЯ РЕДАКТИРОВАНИЯ + ЗАГРУЗКА ФАЙЛОВ
# =====================================================================

async def edit_match_start(message: types.Message, state: FSMContext) -> None:
    """Выбор схемы для редактирования."""
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

    await state.set_state(SchemaStates.selecting_schema_to_edit)
    await message.answer("Выбери схему для редактирования:", reply_markup=keyboard)


async def schema_selected_for_edit(message: types.Message, state: FSMContext) -> None:
    """Схема выбрана — запрашиваем файлы для валидации."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    user_id = message.from_user.id
    schema_name = message.text

    can_see_all = await AccessManager.can_see_all_schemas(user_id)
    if can_see_all:
        schema = await storage.db.get_schema_by_name_global(schema_name)
    else:
        schema = await storage.db.get_schema(user_id, schema_name)

    if not schema:
        await message.answer("❌ Схема не найдена")
        return

    schema_id = schema['id']
    schema_name = schema['name']
    schema_type = await storage.db.get_schema_type(schema_id)
    matches_data = await storage.db.get_schema_matches(schema_id)
    all_matches = _build_all_matches_list(matches_data, schema_type)
    total_matches = len(all_matches)

    if total_matches == 0:
        await state.clear()
        await message.answer(
            f"📋 Схема '{schema_name}'\n\n⚠️ Нет сопоставлений для редактирования"
        )
        await edit_schema_start(message, state)
        return

    await state.update_data(
        edit_schema_id=schema_id,
        edit_schema_name=schema_name,
        edit_schema_type=schema_type,
        edit_all_matches=all_matches,
        edit_matches_data=matches_data,
    )

    user_schemas[user_id] = {}
    await state.update_data(files_processed=False)

    stats_text = _get_stats_text(all_matches, schema_type)
    type_label = "МВМ " if schema_type == 'mvm' else ""
    files_hint = (
        "📤 Загрузи 3 файла Excel (wb, ozon, yandex)"
        if schema_type == 'standard'
        else "📤 Загрузи 3 файла Excel (wb, ozon, yandex)\nЗатем — XML файл каталога"
    )

    await message.answer(
        f"📋 {type_label}Схема '{schema_name}' выбрана\n\n"
        f"📊 Всего сопоставлений: {total_matches}\n"
        f"{stats_text}\n\n"
        f"{files_hint}",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(SchemaStates.waiting_edit_files)


async def handle_edit_validation_file(
    message: types.Message,
    state: FSMContext,
    bot,
) -> None:
    """Загрузка файлов МП для валидации при редактировании."""
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

        try:
            reader = ExcelReader()
            available_columns = {}

            for mp, fp in user_schemas[user_id].items():
                config = FILE_CONFIGS[mp]
                available_columns[mp] = reader.get_column_names(
                    fp, config['sheet_name'], config['header_row']
                )

            await state.update_data(available_columns=available_columns)

            schema_type = data.get('edit_schema_type', 'standard')

            if schema_type == 'mvm':
                await state.set_state(SchemaStates.waiting_edit_xml_file)
                await message.answer(
                    "✅ 3 файла МП загружены!\n\n"
                    "📎 Теперь отправь XML файл каталога:",
                    reply_markup=get_mvm_waiting_xml_keyboard(),
                )
            else:
                await _show_edit_menu(message, state)

        except Exception as e:
            await message.answer(f"❌ Ошибка чтения файлов: {str(e)}")
            await edit_schema_start(message, state)


async def handle_edit_xml_file(
    message: types.Message,
    state: FSMContext,
    bot,
) -> None:
    """Загрузка XML файла при редактировании МВМ-схемы."""
    if not message.document:
        if message.text == "❌ Отмена":
            user_id = message.from_user.id
            if user_id in user_schemas:
                user_schemas[user_id] = {}
            await state.clear()
            await edit_schema_start(message, state)
            return
        await message.answer(
            "📎 Отправь XML файл как документ или нажми ❌ Отмена"
        )
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
            await message.answer(
                "❌ XML файл не содержит офферов (<offer>).\n"
                "Проверь файл и отправь заново:"
            )
            return
        xml_fields = xml_reader.get_field_names(xml_path)
    except ValueError as e:
        await message.answer(f"❌ Ошибка чтения XML: {e}")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error("Ошибка чтения XML при редактировании: %s", e, exc_info=True)
        return

    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    available_columns['xml'] = xml_fields
    await state.update_data(
        available_columns=available_columns,
        edit_xml_file_path=xml_path,
    )

    await message.answer(
        f"✅ XML файл загружен!\n"
        f"📦 Офферов: {offer_count}, полей: {len(xml_fields)}"
    )

    await _show_edit_menu(message, state)


async def handle_edit_xml_text(message: types.Message, state: FSMContext) -> None:
    """Обработка текста в состоянии ожидания XML при редактировании."""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await edit_schema_start(message, state)
    else:
        await message.answer(
            "📎 Отправь XML файл как документ или нажми ❌ Отмена"
        )


async def _show_edit_menu(message: types.Message, state: FSMContext) -> None:
    """Показывает меню фильтрации/редактирования после загрузки всех файлов."""
    data = await state.get_data()
    all_matches = data.get('edit_all_matches', [])
    schema_name = data.get('edit_schema_name')
    schema_type = data.get('edit_schema_type', 'standard')

    stats_text = _get_stats_text(all_matches, schema_type)

    text = (
        f"✅ Файлы загружены!\n\n"
        f"📋 Схема: {schema_name}\n"
        f"📊 Всего сопоставлений: {len(all_matches)}\n\n"
        f"{stats_text}\n\n"
        "Выбери тип для просмотра или начни редактирование:"
    )

    await state.set_state(SchemaStates.choosing_edit_action)
    await message.answer(text, reply_markup=_get_filter_keyboard(schema_type))


# =====================================================================
#  ФИЛЬТРАЦИЯ И ВЫБОР ДЕЙСТВИЯ
# =====================================================================

async def edit_action_selected(message: types.Message, state: FSMContext) -> None:
    """Обработка действия после загрузки файлов (фильтры / редактирование / добавление)."""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await edit_schema_start(message, state)
        return

    data = await state.get_data()
    all_matches = data.get('edit_all_matches', [])
    schema_name = data.get('edit_schema_name')
    schema_type = data.get('edit_schema_type', 'standard')

    if message.text == "➕ Добавить сопоставление":
        available_columns = data.get('available_columns')
        if not available_columns:
            await message.answer(
                "⚠️ Сначала загрузи файлы для валидации.",
                reply_markup=_get_filter_keyboard(schema_type),
            )
            return
        await add_new_match_start(message, state)
        return

    if message.text == "✏️ Редактировать сопоставление":
        if not all_matches:
            await message.answer(
                "⚠️ Нет сопоставлений для редактирования.",
                reply_markup=_get_filter_keyboard(schema_type),
            )
            return
        await state.set_state(SchemaStates.entering_match_number)
        await message.answer(
            f"Введи номер сопоставления для редактирования (1-{len(all_matches)}):",
            reply_markup=get_cancel_keyboard(),
        )
        return

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
        await message.answer(
            "Выбери действие из меню.",
            reply_markup=_get_filter_keyboard(schema_type),
        )
        return

    text = f"📋 Схема: {schema_name}\n{filter_name}\n"
    shown_count = (
        _count_by_type(all_matches, filter_type)
        if filter_type
        else len(all_matches)
    )
    text += f"📊 Показано: {shown_count}\n\n"

    if schema_type == 'mvm':
        text += "WB | Ozon | Яндекс | XML\n"
    else:
        text += "WB | Ozon | Яндекс\n"
    text += "─" * 40 + "\n"

    for i, match_obj in enumerate(all_matches):
        if filter_type and match_obj['type'] != filter_type:
            continue
        line = _format_match_line(match_obj['data'], schema_type)
        text += f"#{i + 1}: {line}\n"

    await _send_long_text(message, text)
    await message.answer(
        "Выбери другой тип или начни редактирование:",
        reply_markup=_get_filter_keyboard(schema_type),
    )


# =====================================================================
#  РЕДАКТИРОВАНИЕ СУЩЕСТВУЮЩЕГО СОПОСТАВЛЕНИЯ
# =====================================================================

async def match_number_entered(message: types.Message, state: FSMContext) -> None:
    """Номер введён — показываем детали сопоставления."""
    if message.text == "❌ Отмена":
        data = await state.get_data()
        schema_type = data.get('edit_schema_type', 'standard')
        await state.set_state(SchemaStates.choosing_edit_action)
        await message.answer(
            "Выбери действие:",
            reply_markup=_get_filter_keyboard(schema_type),
        )
        return

    try:
        match_number = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число!")
        return

    data = await state.get_data()
    all_matches = data.get('edit_all_matches', [])
    schema_type = data.get('edit_schema_type', 'standard')

    if match_number < 1 or match_number > len(all_matches):
        await message.answer(f"❌ Номер должен быть от 1 до {len(all_matches)}")
        return

    selected_match_obj = all_matches[match_number - 1]
    match_type = selected_match_obj['type']
    match_data = selected_match_obj['data']

    await state.update_data(
        edit_match_index=match_number - 1,
        edit_match_type=match_type,
        edit_match_data=match_data,
    )

    type_display = _format_type(match_type, schema_type)
    detail = _format_match_detail(match_data, match_type, schema_type)

    text = f"📋 Сопоставление #{match_number}\n{type_display}\n\n{detail}"
    await message.answer(text)
    await state.set_state(SchemaStates.selecting_column_to_edit)
    await message.answer(
        "Что хочешь изменить?",
        reply_markup=_get_edit_keyboard(schema_type),
    )


async def column_selected_for_edit(message: types.Message, state: FSMContext) -> None:
    """Выбран столбец для редактирования."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    if message.text == "🗑 Удалить сопоставление":
        await delete_match_confirm(message, state)
        return

    data = await state.get_data()
    schema_type = data.get('edit_schema_type', 'standard')

    column_map = {
        "📝 Изменить WB столбец":    ('wildberries', 'column_1', 'WB'),
        "📝 Изменить Ozon столбец":  ('ozon', 'column_2', 'Ozon'),
        "📝 Изменить Яндекс столбец": ('yandex', 'column_3', 'Яндекс'),
        "📝 Изменить XML поле":      ('xml', 'column_4', 'XML'),
    }

    if message.text not in column_map:
        await message.answer("❌ Неизвестная команда")
        return

    marketplace, column_key, display_name = column_map[message.text]

    if column_key == 'column_4' and schema_type != 'mvm':
        await message.answer("❌ XML доступен только для МВМ-схем")
        return

    available_columns = data.get('available_columns', {})
    columns_list = available_columns.get(marketplace, [])

    if not columns_list:
        await message.answer(
            f"❌ Не удалось загрузить список столбцов {display_name}"
        )
        return

    await state.update_data(
        edit_marketplace=marketplace,
        edit_column_key=column_key,
        edit_display_name=display_name,
    )

    columns_text = _build_columns_text(display_name, columns_list)
    await _send_long_text(message, columns_text)

    await message.answer(
        f"Введи название столбца из списка или номер (1-{len(columns_list)}):\n"
        "💡 Введи NA чтобы удалить столбец из сопоставления",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(SchemaStates.selecting_new_column_value)


async def new_column_value_entered(message: types.Message, state: FSMContext) -> None:
    """Новое значение введено — валидация, обновление типа, сохранение."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    user_input = message.text.strip()
    if not user_input:
        await message.answer("❌ Название столбца не может быть пустым!")
        return

    data = await state.get_data()
    marketplace = data.get('edit_marketplace')
    column_key = data.get('edit_column_key')
    display_name = data.get('edit_display_name')
    schema_type = data.get('edit_schema_type', 'standard')
    available_columns = data.get('available_columns', {})
    columns_list = available_columns.get(marketplace, [])

    if user_input.upper() == 'NA':
        new_value = None
    else:
        new_value = _resolve_column_input(user_input, columns_list)
        if new_value is None:
            await message.answer(
                f"❌ Столбец '{user_input}' не найден в шаблоне {display_name}!\n\n"
                "Введи точное название или номер из списка.\n"
                "💡 Чтобы удалить столбец, введи: NA"
            )
            return

    schema_id = data.get('edit_schema_id')
    schema_name = data.get('edit_schema_name')
    all_matches = data.get('edit_all_matches', [])
    match_index = data.get('edit_match_index')
    match_type = data.get('edit_match_type')
    matches_data = data.get('edit_matches_data', {})

    current_match_obj = all_matches[match_index]
    current_match = current_match_obj['data']

    old_value = current_match.get(column_key, '') or 'N/A'
    display_new_value = new_value if new_value else 'N/A'

    if new_value is None:
        current_match[column_key] = ''
    else:
        current_match[column_key] = new_value

    new_type = _determine_new_match_type(current_match, schema_type)

    if new_type is None:
        if new_value is None:
            current_match[column_key] = old_value if old_value != 'N/A' else ''
        await message.answer(
            "❌ Сопоставление должно содержать минимум 2 источника!"
        )
        return

    type_change_text = ""
    if new_type != match_type:
        old_group_key = _get_group_key(match_type, schema_type)
        new_group_key = _get_group_key(new_type, schema_type)

        old_group = matches_data.get(old_group_key, [])
        idx_in_group = _find_index_in_group(all_matches, match_index, match_type)
        if idx_in_group < len(old_group):
            old_group.pop(idx_in_group)
        matches_data[old_group_key] = old_group

        if new_group_key not in matches_data:
            matches_data[new_group_key] = []
        matches_data[new_group_key].append(current_match)

        current_match_obj['type'] = new_type

        old_display = _format_type(match_type, schema_type)
        new_display = _format_type(new_type, schema_type)
        type_change_text = f"\n🔄 Тип изменен: {old_display} → {new_display}"
    else:
        group_key = _get_group_key(match_type, schema_type)
        group = matches_data.get(group_key, [])
        idx_in_group = _find_index_in_group(all_matches, match_index, match_type)
        if idx_in_group < len(group):
            group[idx_in_group] = current_match
        matches_data[group_key] = group

    # Сохраняем в БД (await!)
    await storage.db.save_schema_matches(schema_id, matches_data)

    user_id = message.from_user.id
    if user_id in user_schemas:
        user_schemas[user_id] = {}

    await state.clear()

    text = (
        f"✅ Сопоставление обновлено!\n\n"
        f"📋 Схема: {schema_name}\n"
        f"📝 Столбец {display_name}:\n"
        f"  Было: {old_value}\n"
        f"  Стало: {display_new_value}"
        f"{type_change_text}"
    )
    await message.answer(text)
    await edit_schema_start(message, state)


# =====================================================================
#  УДАЛЕНИЕ СОПОСТАВЛЕНИЯ
# =====================================================================

async def delete_match_confirm(message: types.Message, state: FSMContext) -> None:
    """Удаление сопоставления."""
    data = await state.get_data()

    schema_id = data.get('edit_schema_id')
    schema_name = data.get('edit_schema_name')
    match_index = data.get('edit_match_index')
    match_type = data.get('edit_match_type')
    schema_type = data.get('edit_schema_type', 'standard')

    if schema_id is None or match_index is None or not match_type:
        await message.answer(
            "❌ Не удалось определить сопоставление. Попробуй заново."
        )
        await edit_schema_start(message, state)
        return

    matches_data = data.get('edit_matches_data') or {}
    all_matches = data.get('edit_all_matches', [])

    if match_index < 0 or match_index >= len(all_matches):
        await message.answer("❌ Некорректный индекс сопоставления.")
        await edit_schema_start(message, state)
        return

    match_obj = all_matches[match_index]
    deleted_match = match_obj.get('data', {})

    all_matches.pop(match_index)

    group_key = _get_group_key(match_type, schema_type)
    group_list = matches_data.get(group_key, [])
    idx_in_group = _find_index_in_group(all_matches, match_index, match_type)
    if 0 <= idx_in_group < len(group_list):
        group_list.pop(idx_in_group)
    matches_data[group_key] = group_list

    # Сохраняем в БД (await!)
    await storage.db.save_schema_matches(schema_id, matches_data)

    await state.update_data(
        edit_matches_data=matches_data,
        edit_all_matches=all_matches,
    )
    await state.clear()

    text = f"✅ Сопоставление удалено!\n\n📋 Схема: {schema_name}\n🗑 Удалено:\n"
    labels = {
        'column_1': 'WB', 'column_2': 'Ozon',
        'column_3': 'Яндекс', 'column_4': 'XML',
    }
    keys = (
        ALL_COLUMN_KEYS
        if schema_type == 'mvm'
        else ['column_1', 'column_2', 'column_3']
    )
    for ck in keys:
        val = deleted_match.get(ck, '—')
        text += f"  {labels[ck]}: {val}\n"

    await message.answer(text)
    await edit_schema_start(message, state)


# =====================================================================
#  ДОБАВЛЕНИЕ НОВОГО СОПОСТАВЛЕНИЯ
# =====================================================================

async def add_new_match_start(message: types.Message, state: FSMContext) -> None:
    """Начало добавления — шаг 1: WB."""
    data = await state.get_data()
    available_columns = data.get('available_columns', {})

    wb_columns = available_columns.get('wildberries', [])
    if not wb_columns:
        await message.answer("❌ Не удалось загрузить столбцы WB")
        return

    columns_text = _build_columns_text('WB', wb_columns)
    await _send_long_text(message, columns_text)

    await state.set_state(SchemaStates.selecting_wb_column)
    await message.answer(
        f"Шаг 1: Выбери столбец WB\n\n"
        f"Введи название или номер (1-{len(wb_columns)})\n"
        "💡 Введи NA чтобы пропустить",
        reply_markup=get_cancel_keyboard(),
    )


async def wb_column_selected(message: types.Message, state: FSMContext) -> None:
    """Столбец WB выбран — шаг 2: Ozon."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    wb_columns = available_columns.get('wildberries', [])

    user_input = message.text.strip()

    if user_input.upper() == 'NA':
        await state.update_data(new_match_wb=None)
        display_wb = "⏭ WB: пропущен (NA)"
    else:
        wb_value = _resolve_column_input(user_input, wb_columns)
        if not wb_value:
            await message.answer(
                f"❌ Столбец '{user_input}' не найден!\n"
                "💡 Или введи NA чтобы пропустить WB"
            )
            return
        await state.update_data(new_match_wb=wb_value)
        display_wb = f"✅ WB: {wb_value}"

    ozon_columns = available_columns.get('ozon', [])
    text = f"{display_wb}\n\n"
    text += _build_columns_text('Ozon', ozon_columns)
    await _send_long_text(message, text)

    await state.set_state(SchemaStates.selecting_ozon_column)
    await message.answer(
        f"Шаг 2: Выбери столбец Ozon\n\n"
        f"Введи название или номер (1-{len(ozon_columns)})\n"
        "💡 Введи NA чтобы пропустить",
        reply_markup=get_cancel_keyboard(),
    )


async def ozon_column_selected(message: types.Message, state: FSMContext) -> None:
    """Столбец Ozon выбран — шаг 3: Яндекс."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    ozon_columns = available_columns.get('ozon', [])
    wb_value = data.get('new_match_wb')

    user_input = message.text.strip()

    if user_input.upper() == 'NA':
        await state.update_data(new_match_ozon=None)
        display_ozon = "⏭ Ozon: пропущен (NA)"
        ozon_val_for_hint = None
    else:
        ozon_value = _resolve_column_input(user_input, ozon_columns)
        if not ozon_value:
            await message.answer(
                f"❌ Столбец '{user_input}' не найден!\n"
                "💡 Или введи NA чтобы пропустить Ozon"
            )
            return
        await state.update_data(new_match_ozon=ozon_value)
        display_ozon = f"✅ Ozon: {ozon_value}"
        ozon_val_for_hint = ozon_value

    yandex_columns = available_columns.get('yandex', [])
    display_wb = f"✅ WB: {wb_value}" if wb_value else "⏭ WB: пропущен (NA)"

    text = f"{display_wb}\n{display_ozon}\n\n"
    text += _build_columns_text('Яндекс', yandex_columns)
    await _send_long_text(message, text)

    skipped_count = (0 if wb_value else 1) + (0 if ozon_val_for_hint else 1)
    hint = (
        "⚠️ Яндекс обязателен (уже пропущено 2)"
        if skipped_count >= 2
        else "💡 Введи NA чтобы пропустить"
    )

    await state.set_state(SchemaStates.selecting_yandex_column)
    await message.answer(
        f"Шаг 3: Выбери столбец Яндекс\n\n"
        f"Введи название или номер (1-{len(yandex_columns)})\n"
        f"{hint}",
        reply_markup=get_cancel_keyboard(),
    )


async def yandex_column_selected(message: types.Message, state: FSMContext) -> None:
    """Столбец Яндекс выбран — для МВМ переходим к XML, для стандарта — сохраняем."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    yandex_columns = available_columns.get('yandex', [])
    schema_type = data.get('edit_schema_type', 'standard')

    wb_value = data.get('new_match_wb')
    ozon_value = data.get('new_match_ozon')

    user_input = message.text.strip()

    if user_input.upper() == 'NA':
        yandex_value = None
    else:
        yandex_value = _resolve_column_input(user_input, yandex_columns)
        if not yandex_value:
            filled_count = (1 if wb_value else 0) + (1 if ozon_value else 0)
            hint = (
                "\n💡 Или введи NA"
                if filled_count >= 2
                else "\n⚠️ Яндекс обязателен (нужно минимум 2)"
            )
            await message.answer(f"❌ Столбец '{user_input}' не найден!{hint}")
            return

    await state.update_data(new_match_yandex=yandex_value)

    if schema_type == 'mvm':
        xml_columns = available_columns.get('xml', [])
        if xml_columns:
            display_wb = f"✅ WB: {wb_value}" if wb_value else "⏭ WB: N/A"
            display_ozon = f"✅ Ozon: {ozon_value}" if ozon_value else "⏭ Ozon: N/A"
            display_yandex = (
                f"✅ Яндекс: {yandex_value}" if yandex_value else "⏭ Яндекс: N/A"
            )

            text = f"{display_wb}\n{display_ozon}\n{display_yandex}\n\n"
            text += _build_columns_text('XML', xml_columns)
            await _send_long_text(message, text)

            filled_count = (
                (1 if wb_value else 0)
                + (1 if ozon_value else 0)
                + (1 if yandex_value else 0)
            )
            hint = (
                "💡 Введи NA чтобы пропустить"
                if filled_count >= 2
                else "⚠️ XML обязателен (нужно минимум 2)"
            )

            await state.set_state(SchemaStates.selecting_xml_column)
            await message.answer(
                f"Шаг 4: Выбери XML поле\n\n"
                f"Введи название или номер (1-{len(xml_columns)})\n"
                f"{hint}",
                reply_markup=get_cancel_keyboard(),
            )
            return

    await _finalize_new_match(message, state, xml_value=None)


async def xml_column_selected(message: types.Message, state: FSMContext) -> None:
    """XML-поле выбрано (шаг 4 для МВМ) — сохраняем."""
    if message.text == "❌ Отмена":
        await edit_schema_start(message, state)
        return

    data = await state.get_data()
    available_columns = data.get('available_columns', {})
    xml_columns = available_columns.get('xml', [])

    user_input = message.text.strip()

    if user_input.upper() == 'NA':
        xml_value = None
    else:
        xml_value = _resolve_column_input(user_input, xml_columns)
        if not xml_value:
            wb_value = data.get('new_match_wb')
            ozon_value = data.get('new_match_ozon')
            yandex_value = data.get('new_match_yandex')
            filled = (
                (1 if wb_value else 0)
                + (1 if ozon_value else 0)
                + (1 if yandex_value else 0)
            )
            hint = "\n💡 Или введи NA" if filled >= 2 else "\n⚠️ XML обязателен"
            await message.answer(f"❌ Поле '{user_input}' не найдено!{hint}")
            return

    await _finalize_new_match(message, state, xml_value=xml_value)


async def _finalize_new_match(
    message: types.Message,
    state: FSMContext,
    xml_value: str | None,
) -> None:
    """
    Финализация добавления нового сопоставления.

    Общая для стандартных и МВМ-схем. Определяет тип, проверяет дубли, сохраняет.
    """
    data = await state.get_data()

    wb_value = data.get('new_match_wb')
    ozon_value = data.get('new_match_ozon')
    yandex_value = data.get('new_match_yandex')
    schema_id = data.get('edit_schema_id')
    schema_name = data.get('edit_schema_name')
    schema_type = data.get('edit_schema_type', 'standard')
    matches_data = data.get('edit_matches_data') or {}
    all_matches = data.get('edit_all_matches', [])

    if not schema_id:
        await message.answer("❌ Не удалось определить схему. Попробуй заново.")
        await edit_schema_start(message, state)
        return

    new_match: dict = {'confidence': 1.0, 'description': 'Добавлено вручную'}
    if wb_value:
        new_match['column_1'] = wb_value
    if ozon_value:
        new_match['column_2'] = ozon_value
    if yandex_value:
        new_match['column_3'] = yandex_value
    if xml_value:
        new_match['column_4'] = xml_value

    new_type = _determine_new_match_type(new_match, schema_type)
    if new_type is None:
        await message.answer(
            "❌ Сопоставление должно содержать минимум 2 источника!",
            reply_markup=_get_filter_keyboard(schema_type),
        )
        await state.set_state(SchemaStates.choosing_edit_action)
        return

    group_key = _get_group_key(new_type, schema_type)
    type_display = _format_type(new_type, schema_type)

    col_keys = _get_column_keys_for_type(new_type, schema_type)
    existing_group = matches_data.get(group_key, [])

    is_duplicate = any(
        all(m.get(ck) == new_match.get(ck) for ck in col_keys)
        for m in existing_group
    )

    if is_duplicate:
        labels = {
            'column_1': 'WB', 'column_2': 'Ozon',
            'column_3': 'Яндекс', 'column_4': 'XML',
        }
        keys = (
            ALL_COLUMN_KEYS
            if schema_type == 'mvm'
            else ['column_1', 'column_2', 'column_3']
        )
        lines = [f"{labels[ck]}: {new_match.get(ck, 'N/A')}" for ck in keys]
        await message.answer(
            "⚠️ Такое сопоставление уже существует!\n\n" + "\n".join(lines),
            reply_markup=_get_filter_keyboard(schema_type),
        )
        await state.set_state(SchemaStates.choosing_edit_action)
        return

    if group_key not in matches_data:
        matches_data[group_key] = []
    matches_data[group_key].append(new_match)

    # Сохраняем в БД (await!)
    await storage.db.save_schema_matches(schema_id, matches_data)

    all_matches.append({'type': new_type, 'data': new_match})
    await state.update_data(
        edit_matches_data=matches_data,
        edit_all_matches=all_matches,
    )

    user_id = message.from_user.id
    if user_id in user_schemas:
        user_schemas[user_id] = {}

    await state.clear()

    total_count = sum(
        len(matches_data.get(gk, []))
        for gk, _, _, _ in _get_match_groups(schema_type)
    )

    labels = {
        'column_1': 'WB', 'column_2': 'Ozon',
        'column_3': 'Яндекс', 'column_4': 'XML',
    }
    keys = (
        ALL_COLUMN_KEYS
        if schema_type == 'mvm'
        else ['column_1', 'column_2', 'column_3']
    )

    match_lines = []
    for ck in keys:
        val = new_match.get(ck)
        icon = "✅" if val else "⏭"
        display = val if val else "N/A"
        match_lines.append(f"{icon} {labels[ck]}: {display}")

    text = (
        "✅ Новое сопоставление добавлено!\n\n"
        f"📋 Схема: {schema_name}\n"
        f"📊 Всего сопоставлений: {total_count}\n"
        f"🏷 Тип: {type_display}\n\n"
        "Добавлено:\n"
        + "\n".join(match_lines)
    )

    await message.answer(text)
    await edit_schema_start(message, state)


# =====================================================================
#  РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# =====================================================================

def register_schema_edit_handlers(dp, bot) -> None:
    """Регистрация обработчиков редактирования схем."""
    from functools import partial

    dp.message.register(edit_schema_start, F.text == "✏️ Редактировать схему")

    dp.message.register(view_matches_start, F.text == "👁 Просмотреть текущие сопоставления")
    dp.message.register(show_schema_matches, SchemaStates.selecting_schema_to_view)

    dp.message.register(edit_match_start, F.text == "✏️ Изменить сопоставление")
    dp.message.register(schema_selected_for_edit, SchemaStates.selecting_schema_to_edit)

    dp.message.register(
        partial(handle_edit_validation_file, bot=bot),
        SchemaStates.waiting_edit_files, F.document,
    )
    dp.message.register(
        partial(handle_edit_xml_file, bot=bot),
        SchemaStates.waiting_edit_xml_file, F.document,
    )
    dp.message.register(
        handle_edit_xml_text,
        SchemaStates.waiting_edit_xml_file, F.text,
    )

    dp.message.register(edit_action_selected, SchemaStates.choosing_edit_action)
    dp.message.register(match_number_entered, SchemaStates.entering_match_number)
    dp.message.register(column_selected_for_edit, SchemaStates.selecting_column_to_edit)
    dp.message.register(new_column_value_entered, SchemaStates.selecting_new_column_value)

    dp.message.register(wb_column_selected, SchemaStates.selecting_wb_column)
    dp.message.register(ozon_column_selected, SchemaStates.selecting_ozon_column)
    dp.message.register(yandex_column_selected, SchemaStates.selecting_yandex_column)
    dp.message.register(xml_column_selected, SchemaStates.selecting_xml_column)