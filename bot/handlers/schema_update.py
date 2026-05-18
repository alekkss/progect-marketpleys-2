"""
Обработчики обновления схем.
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
from bot.storage import user_schemas
from bot import storage
from bot.utils import download_file
from bot.handlers.common import schema_management
from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from utils.xml_reader import XmlReader
from services.ai_comparator import AIComparator
from bot.security import AccessManager
logger = logging.getLogger('schema_update')

# =====================================================================
# ОБЩИЙ ВХОД: ВЫБОР СХЕМЫ
# =====================================================================
async def update_schema_start(message: types.Message, state: FSMContext):
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

    text = "🔄 Выбери схему для обновления:\n\n"
    for i, schema in enumerate(schemas, 1):
        s_type = schema.get('schema_type', 'standard')
        icon = "📦" if s_type == 'mvm' else "📊"
        owner = f" (владелец: {schema.get('owner_name', 'неизвестен')})" if can_see_all else ""
        text += f"{i}. {icon} {schema['name']}{owner}\n"

    await state.set_state(SchemaStates.selecting_schema_to_update)
    await message.answer(text, reply_markup=keyboard)

async def schema_selected_for_update(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await schema_management(message, state)
        return
    user_id = message.from_user.id
    schema_name = message.text
    can_see_all = await AccessManager.can_see_all_schemas(user_id)

    schema = await storage.db.get_schema_by_name_global(schema_name) if can_see_all else await storage.db.get_schema(user_id, schema_name)
    if not schema:
        await message.answer("❌ Схема не найдена")
        return

    schema_id = schema['id']
    schema_type = await storage.db.get_schema_type(schema_id)
    owner_warning = f"\n\n⚠️ Вы обновляете схему другого пользователя (ID: {schema['owner_id']})" if can_see_all and schema.get('owner_id') != user_id else ""

    if schema_type == 'mvm':
        await state.update_data(update_schema_id=schema_id, update_schema_name=schema['name'], update_schema_type='mvm', mvm_mp_files_processed=False)
        user_schemas[user_id] = {}
        await state.set_state(SchemaUpdateMvmStates.waiting_mp_files)
        await message.answer(f"📦 МВМ-схема '{schema['name']}' выбрана{owner_warning}\n\n📤 Отправь 3 файла Excel (wb, ozon, yandex)", reply_markup=ReplyKeyboardRemove())
    else:
        await state.update_data(update_schema_id=schema_id, update_schema_name=schema['name'], update_schema_type='standard')
        user_schemas[user_id] = {}
        await state.set_state(SchemaStates.waiting_update_files)
        await message.answer(f"✅ Схема '{schema['name']}' выбрана{owner_warning}\n\nОтправь 3 файла Excel", reply_markup=ReplyKeyboardRemove())

# =====================================================================
# СТАНДАРТНЫЙ ФЛОУ (3 МП)
# =====================================================================
async def handle_update_file(message: types.Message, state: FSMContext, bot):
    user_id = message.from_user.id
    if user_id not in user_schemas: user_schemas[user_id] = {}
    data = await state.get_data()
    if data.get('files_processed'): return

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
        await state.update_data(files_processed=True)
        await message.answer("✅ Все файлы загружены!", reply_markup=get_update_schema_keyboard())

async def finalize_schema_update(message: types.Message, state: FSMContext):
    if await state.get_state() != SchemaStates.waiting_update_files:
        await message.answer("❌ Сначала выбери схему для обновления")
        return
    user_id = message.from_user.id
    if user_id not in user_schemas or len(user_schemas[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла!")
        return

    data = await state.get_data()
    schema_id, schema_name = data.get('update_schema_id'), data.get('update_schema_name')
    if not schema_id or not schema_name:
        await message.answer("❌ Данные схемы потеряны.")
        return

    await message.answer(f"⏳ Анализирую столбцы для схемы '{schema_name}'...")
    try:
        file_paths = user_schemas[user_id]
        reader = ExcelReader()
        all_columns = {}
        for mp, fp in file_paths.items():
            cfg = FILE_CONFIGS[mp]
            all_columns[mp] = reader.get_column_names(fp, cfg['sheet_name'], cfg['header_row'])

        existing_matches = await storage.db.get_schema_matches(schema_id)
        matched_wb, matched_ozon, matched_yandex = _collect_matched_columns_standard(existing_matches)

        remaining = {
            'wildberries': [c for c in all_columns['wildberries'] if c not in matched_wb],
            'ozon': [c for c in all_columns['ozon'] if c not in matched_ozon],
            'yandex': [c for c in all_columns['yandex'] if c not in matched_yandex],
        }
        total_remaining = sum(len(v) for v in remaining.values())
        if total_remaining == 0:
            user_schemas[user_id] = {}
            await state.clear()
            await message.answer(f"ℹ️ Все столбцы уже сопоставлены!\n\nСхема '{schema_name}' не требует обновления", reply_markup=get_main_menu_keyboard())
            return

        await message.answer(f"🔍 Несопоставленных: WB:{len(remaining['wildberries'])} Ozon:{len(remaining['ozon'])} Я:{len(remaining['yandex'])}\n\n🤖 AI ищет новые совпадения...")
        new_result = await AIComparator().compare_columns(remaining['wildberries'], remaining['ozon'], remaining['yandex'])
        new_count, skipped = _merge_new_standard_matches(existing_matches, new_result)

        if new_count > 0:
            await storage.db.save_schema_matches(schema_id, existing_matches)

        user_schemas[user_id] = {}
        await state.clear()
        await message.answer(_build_update_result_text(schema_name, new_count, skipped, len(existing_matches.get('matches_all_three', [])), total_remaining), reply_markup=get_main_menu_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка обновления стандартной схемы: {e}", exc_info=True)

# =====================================================================
# МВМ-ФЛОУ (3 МП + XML)
# =====================================================================
async def handle_mvm_update_mp_file(message: types.Message, state: FSMContext, bot):
    user_id = message.from_user.id
    if user_id not in user_schemas: user_schemas[user_id] = {}
    if (await state.get_data()).get('mvm_mp_files_processed'): return

    fp, fn, mp = await download_file(bot, message, user_id)
    if not mp: await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)"); return
    if mp in user_schemas[user_id]: await message.answer(f"⚠️ {mp.upper()} уже загружен"); return

    user_schemas[user_id][mp] = fp
    loaded = len(user_schemas[user_id])
    await message.answer(f"✅ {mp.upper()} ({loaded}/3)")
    if loaded == 3:
        await state.update_data(mvm_mp_files_processed=True)
        await state.set_state(SchemaUpdateMvmStates.waiting_xml_file)
        await message.answer("✅ Все 3 шаблона МП загружены!\n\n📎 Теперь отправь XML файл каталога:", reply_markup=get_mvm_waiting_xml_keyboard())

async def handle_mvm_update_mp_text(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas: user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
    else:
        await message.answer("📎 Отправь файл Excel как документ или нажми ❌ Отмена")

async def handle_mvm_update_xml_file(message: types.Message, state: FSMContext, bot):
    if not message.document:
        if message.text == "❌ Отмена":
            u = message.from_user.id
            if u in user_schemas: user_schemas[u] = {}
            await state.clear()
            await schema_management(message, state)
        else:
            await message.answer("📎 Отправь XML файл как документ или нажми ❌ Отмена")
        return

    fn = message.document.file_name or ""
    if not fn.lower().endswith('.xml'):
        await message.answer("❌ Нужен файл с расширением .xml"); return

    file = await bot.get_file(message.document.file_id)
    dl_dir = Path("downloads") / str(message.from_user.id)
    dl_dir.mkdir(parents=True, exist_ok=True)
    xml_path = str(dl_dir / fn)
    await bot.download_file(file.file_path, xml_path)

    try:
        xr = XmlReader()
        cnt = xr.get_offer_count(xml_path)
        if cnt == 0: await message.answer("❌ XML не содержит офферов."); return
        await state.update_data(mvm_xml_file_path=xml_path)
        await message.answer(f"✅ XML загружен! Офферов: {cnt}, полей: {len(xr.get_field_names(xml_path))}\n\nНажми кнопку:", reply_markup=get_update_schema_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения XML: {e}")
        logger.error(f"Ошибка XML при обновлении МВМ: {e}", exc_info=True)

async def handle_mvm_update_xml_text(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        u = message.from_user.id
        if u in user_schemas: user_schemas[u] = {}
        await state.clear()
        await schema_management(message, state)
    elif message.text == "✅ Обновить схему":
        await finalize_mvm_schema_update(message, state)
    else:
        await message.answer("📎 Отправь XML файл или нажми ❌ Отмена")

async def finalize_mvm_schema_update(message: types.Message, state: FSMContext):
    u = message.from_user.id
    if u not in user_schemas or len(user_schemas[u]) != 3:
        await message.answer("❌ Не хватает файлов МП."); return

    data = await state.get_data()
    schema_id, schema_name, xml_path = data.get('update_schema_id'), data.get('update_schema_name'), data.get('mvm_xml_file_path')
    if not schema_id or not schema_name or not xml_path:
        await message.answer("❌ Данные схемы потеряны."); return

    await message.answer(f"⏳ Анализирую столбцы для МВМ-схемы '{schema_name}'...", reply_markup=ReplyKeyboardRemove())
    try:
        reader = ExcelReader()
        all_cols = {}
        for mp, fp in user_schemas[u].items():
            cfg = FILE_CONFIGS[mp]
            all_cols[mp] = reader.get_column_names(fp, cfg['sheet_name'], cfg['header_row'])

        xr = XmlReader()
        xml_fields = xr.get_field_names(xml_path)
        existing = await storage.db.get_schema_matches(schema_id)
        matched_sets = _collect_matched_columns_mvm(existing)

        rem = {
            'wildberries': [c for c in all_cols['wildberries'] if c not in matched_sets['column_1']],
            'ozon': [c for c in all_cols['ozon'] if c not in matched_sets['column_2']],
            'yandex': [c for c in all_cols['yandex'] if c not in matched_sets['column_3']],
            'xml': [c for c in xml_fields if c not in matched_sets['column_4']],
        }
        total_rem = sum(len(v) for v in rem.values())
        if total_rem == 0:
            user_schemas[u] = {}
            await state.clear()
            await message.answer(f"ℹ️ Все столбцы уже сопоставлены!\n\nМВМ-схема '{schema_name}' не требует обновления", reply_markup=get_main_menu_keyboard())
            return

        await message.answer(f"🔍 Несопоставленных: WB:{len(rem['wildberries'])} Ozon:{len(rem['ozon'])} Я:{len(rem['yandex'])} XML:{len(rem['xml'])}\n\n🤖 AI ищет новые совпадения (4 источника)...")
        new_res = await AIComparator().compare_columns_mvm(rem['wildberries'], rem['ozon'], rem['yandex'], rem['xml'])
        new_cnt, skip = _merge_new_mvm_matches(existing, new_res)

        if new_cnt > 0:
            await storage.db.save_schema_matches(schema_id, existing)

        user_schemas[u] = {}
        await state.clear()
        await message.answer(_build_mvm_update_result_text(schema_name, new_cnt, skip, existing, total_rem), reply_markup=get_main_menu_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка обновления МВМ-схемы: {e}", exc_info=True)

# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================
def _collect_matched_columns_standard(m: dict) -> tuple:
    wb, oz, ya = set(), set(), set()
    for gk in ['matches_all_three', 'matches_1_2', 'matches_1_3', 'matches_2_3']:
        for match in m.get(gk, []):
            if match.get('column_1'): wb.add(match['column_1'])
            if match.get('column_2'): oz.add(match['column_2'])
            if match.get('column_3'): ya.add(match['column_3'])
    return wb, oz, ya

def _collect_matched_columns_mvm(m: dict) -> dict:
    matched = {'column_1': set(), 'column_2': set(), 'column_3': set(), 'column_4': set()}
    for gk in ['matches_all_four', 'matches_triple_1_2_3', 'matches_triple_1_2_4', 'matches_triple_1_3_4', 'matches_triple_2_3_4', 'matches_pair_1_2', 'matches_pair_1_3', 'matches_pair_1_4', 'matches_pair_2_3', 'matches_pair_2_4', 'matches_pair_3_4']:
        for match in m.get(gk, []):
            for ck in matched:
                if match.get(ck): matched[ck].add(match[ck])
    return matched

def _merge_new_standard_matches(existing: dict, new: dict) -> tuple:
    nc, sc = 0, 0
    for gk in ['matches_all_three', 'matches_1_2', 'matches_1_3', 'matches_2_3']:
        eks = {tuple(m.get(f'column_{i}', '') for i in range(1,4)) for m in existing.get(gk, [])}
        for match in new.get(gk, []):
            if match.get('confidence', 0) < 0.85: sc += 1; continue
            k = tuple(match.get(f'column_{i}', '') for i in range(1,4))
            if k not in eks:
                existing.get(gk, []).append(match)
                eks.add(k); nc += 1
    return nc, sc

def _merge_new_mvm_matches(existing: dict, new: dict) -> tuple:
    nc, sc = 0, 0
    mvm_groups = ['matches_all_four', 'matches_triple_1_2_3', 'matches_triple_1_2_4', 'matches_triple_1_3_4', 'matches_triple_2_3_4', 'matches_pair_1_2', 'matches_pair_1_3', 'matches_pair_1_4', 'matches_pair_2_3', 'matches_pair_2_4', 'matches_pair_3_4']
    for gk in mvm_groups:
        eks = {tuple(m.get(f'column_{i}', '') for i in range(1,5)) for m in existing.get(gk, [])}
        for match in new.get(gk, []):
            if match.get('confidence', 0) < 0.85: sc += 1; continue
            k = tuple(match.get(f'column_{i}', '') for i in range(1,5))
            if k not in eks:
                existing.get(gk, []).append(match)
                eks.add(k); nc += 1
    return nc, sc

def _build_update_result_text(sn, nc, sc, tm, tr):
    if nc > 0:
        t = f"✅ Схема '{sn}' обновлена!\n\n➕ Добавлено: {nc}\n📊 Всего: {tm}"
        return f"{t}\n⚠️ Пропущено (<85%): {sc}" if sc > 0 else t
    t = f"ℹ️ Новых совпадений не найдено\n\nAI не нашел подходящих пар (>=85%) среди {tr} столбцов"
    return f"{t}\n⚠️ Пропущено (<85%): {sc}" if sc > 0 else t

def _build_mvm_update_result_text(sn, nc, sc, existing, tr):
    if nc > 0:
        t = f"✅ МВМ-схема '{sn}' обновлена!\n\n➕ Добавлено: {nc}\n\n"
        labels = {'matches_all_four': '🎯 Четверные', 'matches_triple_1_2_3': '🔷 Тройные (WB+Ozon+Яндекс)', 'matches_triple_1_2_4': '🔷 Тройные (WB+Ozon+XML)', 'matches_triple_1_3_4': '🔷 Тройные (WB+Яндекс+XML)', 'matches_triple_2_3_4': '🔷 Тройные (Ozon+Яндекс+XML)', 'matches_pair_1_2': '🔗 Парные (WB+Ozon)', 'matches_pair_1_3': '🔗 Парные (WB+Яндекс)', 'matches_pair_1_4': '🔗 Парные (WB+XML)', 'matches_pair_2_3': '🔗 Парные (Ozon+Яндекс)', 'matches_pair_2_4': '🔗 Парные (Ozon+XML)', 'matches_pair_3_4': '🔗 Парные (Яндекс+XML)'}
        for k, l in labels.items():
            c = len(existing.get(k, []))
            if c > 0: t += f"{l}: {c}\n"
        t += f"\n📊 Всего: {sum(len(existing.get(k, [])) for k in labels)}"
        return f"{t}\n⚠️ Пропущено (<85%): {sc}" if sc > 0 else t
    t = f"ℹ️ Новых совпадений не найдено\n\nAI не нашел подходящих пар (>=85%) среди {tr} столбцов"
    return f"{t}\n⚠️ Пропущено (<85%): {sc}" if sc > 0 else t

def register_schema_update_handlers(dp, bot):
    from functools import partial
    dp.message.register(update_schema_start, F.text == "🔄 Обновить схему")
    dp.message.register(schema_selected_for_update, SchemaStates.selecting_schema_to_update)
    dp.message.register(partial(handle_update_file, bot=bot), SchemaStates.waiting_update_files, F.document)
    dp.message.register(finalize_schema_update, F.text == "✅ Обновить схему")
    dp.message.register(partial(handle_mvm_update_mp_file, bot=bot), SchemaUpdateMvmStates.waiting_mp_files, F.document)
    dp.message.register(handle_mvm_update_mp_text, SchemaUpdateMvmStates.waiting_mp_files, F.text)
    dp.message.register(partial(handle_mvm_update_xml_file, bot=bot), SchemaUpdateMvmStates.waiting_xml_file, F.document)
    dp.message.register(handle_mvm_update_xml_text, SchemaUpdateMvmStates.waiting_xml_file, F.text)