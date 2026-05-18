"""
Обработчики загрузки и обработки файлов.
Поддерживает два типа схем:
- standard (3 МП): загрузка 3 Excel → синхронизация между МП
- mvm (3 МП + XML): загрузка 3 Excel + XML → синхронизация + заполнение из XML
Принцип Open/Closed: МВМ-логика добавлена через отдельные StatesGroup
и хендлеры, стандартный флоу не модифицирован.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
from datetime import datetime
from aiogram import types, F
from aiogram.types import FSInputFile, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from bot.states import UploadStates, UploadMvmStates
from bot.keyboards import (
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_process_keyboard,
    get_schema_list_keyboard,
    get_mvm_waiting_xml_keyboard,
)
from bot.storage import user_files
from bot import storage
from bot.utils import download_file, download_xml_from_telegram, download_file_by_url
from bot.handlers.common import cmd_start
from bot.security import AccessManager
from config.config import FILE_CONFIGS
from utils.excel_writer import ExcelWriter
from utils.xml_reader import XmlReader
from services.data_synchronizer import DataSynchronizer
from services.ai_comparator import AIComparator
from utils.logger_config import setup_logger

logger = setup_logger('upload')

# =====================================================================
# ОБЩИЙ ВХОД: ВЫБОР СХЕМЫ
# =====================================================================
async def select_schema_for_upload(message: types.Message, state: FSMContext) -> None:
    """Выбор схемы для загрузки файлов — определяет тип и направляет в нужный флоу."""
    user_id = message.from_user.id
    can_see_all = await AccessManager.can_see_all_schemas(user_id)
    schemas = await storage.db.get_user_schemas(user_id, all_schemas=can_see_all)

    if not schemas:
        await message.answer("❌ У тебя нет схем!\n\nСначала создай схему через 📋 Управление схемами")
        return

    keyboard = get_schema_list_keyboard(schemas)
    if not keyboard:
        await message.answer("❌ У тебя нет валидных схем!\n\nСоздай новую схему через 📋 Управление схемами")
        return

    text = "Выбери схему для синхронизации:\n\n"
    for schema in schemas:
        s_type = schema.get('schema_type', 'standard')
        type_icon = "📦" if s_type == 'mvm' else "📊"
        text += f"{type_icon} {schema['name']}\n"

    await state.set_state(UploadStates.selecting_schema)
    await message.answer(text, reply_markup=keyboard)

async def schema_selected(message: types.Message, state: FSMContext, bot) -> None:
    """Схема выбрана — определяем тип и начинаем загрузку файлов."""
    if message.text == "❌ Отмена":
        await cmd_start(message, state)
        return

    user_id = message.from_user.id
    can_see_all = await AccessManager.can_see_all_schemas(user_id)

    if can_see_all:
        schema = await storage.db.get_schema_by_name_global(message.text)
    else:
        schema = await storage.db.get_schema(user_id, message.text)

    if not schema:
        await message.answer("❌ Схема не найдена. Выбери из списка.")
        return

    schema_id = schema['id']
    schema_type = await storage.db.get_schema_type(schema_id)

    if schema_type == 'mvm':
        await state.update_data(
            selected_schema_id=schema_id,
            selected_schema_type='mvm',
            mvm_mp_files_processed=False
        )
        user_files[user_id] = {}
        await state.set_state(UploadMvmStates.waiting_for_mp_files)
        await message.answer(
            f"📦 МВМ-схема '{message.text}' выбрана\n\n"
            f"Отправь 3 файла Excel (wb, ozon, yandex)\nЗатем — XML файл каталога",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await state.update_data(
            selected_schema_id=schema_id,
            selected_schema_type='standard'
        )
        user_files[user_id] = {}
        await state.set_state(UploadStates.waiting_for_files)
        await message.answer(
            f"✅ Схема '{message.text}' выбрана\n\nОтправь 3 файла Excel",
            reply_markup=ReplyKeyboardRemove()
        )

# =====================================================================
# СТАНДАРТНЫЙ ФЛОУ (3 МП)
# =====================================================================
async def handle_file(message: types.Message, state: FSMContext, bot) -> None:
    """Обработка загруженного файла (стандартная схема)."""
    user_id = message.from_user.id
    if user_id not in user_files:
        user_files[user_id] = {}

    data = await state.get_data()
    if data.get('files_processed'):
        return

    file_path, file_name, marketplace = await download_file(bot, message, user_id)
    if not marketplace:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)")
        return
    if marketplace in user_files[user_id]:
        await message.answer(f"⚠️ {marketplace.upper()} уже загружен")
        return

    user_files[user_id][marketplace] = file_path
    await message.answer(f"✅ {marketplace.upper()} ({len(user_files[user_id])}/3)")

    if len(user_files[user_id]) == 3:
        data = await state.get_data()
        if data.get('files_processed'):
            return
        await state.update_data(files_processed=True)
        await message.answer("✅ Все файлы загружены!", reply_markup=get_process_keyboard())

async def process_files(message: types.Message, state: FSMContext, bot) -> None:
    """Обработка файлов по стандартной схеме."""
    user_id = message.from_user.id
    if user_id not in user_files or len(user_files[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла!")
        return

    data = await state.get_data()
    schema_id = data.get('selected_schema_id')
    if not schema_id:
        await message.answer("❌ Схема не выбрана!")
        return

    processing_id = await storage.db.start_processing(user_id)
    await message.answer("⏳ Обработка по схеме...")

    try:
        file_paths = user_files[user_id]
        for marketplace, file_path in file_paths.items():
            await storage.db.add_file(
                user_id, processing_id, marketplace,
                os.path.basename(file_path), file_path
            )

        await message.answer("📖 Читаю файлы...")
        comparison_result = await storage.db.get_schema_matches(schema_id)
        await message.answer(
            f"🔄 Синхронизирую по схеме "
            f"({len(comparison_result.get('matches_all_three', []))} столбцов)..."
        )

        comparator = AIComparator()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = f"output/{user_id}_{timestamp}"
        os.makedirs(output_dir, exist_ok=True)

        output_sync_paths = {
            'wildberries': f"{output_dir}/WB_синхронизировано.xlsx",
            'ozon':        f"{output_dir}/Ozon_синхронизировано.xlsx",
            'yandex':      f"{output_dir}/Яндекс_синхронизировано.xlsx",
        }
        report_path = f"{output_dir}/результат_{timestamp}.xlsx"

        synchronizer = DataSynchronizer(comparison_result, ai_comparator=comparator)
        synced_dfs, changes_log = await synchronizer.synchronize_data(
            file_paths, output_sync_paths
        )

        # Шаг 1: создаём файл отчёта через ExcelWriter
        await message.answer("📊 Создаю отчет...")
        writer = ExcelWriter()
        writer.create_report_with_changes(comparison_result, changes_log, report_path)

        # Шаг 2: добавляем AI-лог в уже готовый файл отчёта
        synchronizer.create_ai_log_in_report(report_path)

        wb_count     = len(synced_dfs['wildberries'])
        ozon_count   = len(synced_dfs['ozon'])
        yandex_count = len(synced_dfs['yandex'])
        total_synced = sum(len(changes_log[mp]) for mp in changes_log)

        await storage.db.complete_processing(
            processing_id, wb_count, ozon_count, yandex_count, total_synced
        )

        await message.answer("📤 Отправляю результаты...")
        for path in output_sync_paths.values():
            await message.answer_document(FSInputFile(path))
        await message.answer_document(FSInputFile(report_path), caption="📊 Отчет")

        user_files[user_id] = {}
        await state.clear()
        await message.answer(
            f"✅ Готово!\n\n📦 Обработка товаров:\n"
            f"• WB: {wb_count}\n• Ozon: {ozon_count}\n• Яндекс: {yandex_count}\n\n"
            f"🔄 Синхронизировано ячеек: {total_synced}",
            reply_markup=get_main_menu_keyboard()
        )

    except Exception as e:
        await storage.db.fail_processing(processing_id, str(e))
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error("Ошибка обработки: %s", e, exc_info=True)

# =====================================================================
# МВМ-ФЛОУ (3 МП + XML)
# =====================================================================
async def handle_mvm_upload_mp_file(message: types.Message, state: FSMContext, bot) -> None:
    """Загрузка файла МП при обработке по МВМ-схеме."""
    user_id = message.from_user.id
    if user_id not in user_files:
        user_files[user_id] = {}
    if (await state.get_data()).get('mvm_mp_files_processed'):
        return

    file_path, file_name, marketplace = await download_file(bot, message, user_id)
    if not marketplace:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)")
        return
    if marketplace in user_files[user_id]:
        await message.answer(f"⚠️ {marketplace.upper()} уже загружен")
        return

    user_files[user_id][marketplace] = file_path
    loaded = len(user_files[user_id])
    await message.answer(f"✅ {marketplace.upper()} ({loaded}/3)")

    if loaded == 3:
        data = await state.get_data()
        if data.get('mvm_mp_files_processed'):
            return
        await state.update_data(mvm_mp_files_processed=True)
        await state.set_state(UploadMvmStates.waiting_for_xml_file)
        await message.answer(
            "✅ Все 3 файла МП загружены!\n\n📎 Теперь отправь XML файл каталога:",
            reply_markup=get_mvm_waiting_xml_keyboard()
        )

async def handle_mvm_upload_mp_text(message: types.Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        u = message.from_user.id
        if u in user_files:
            user_files[u] = {}
        await state.clear()
        await cmd_start(message, state)
        return
    await message.answer("📎 Отправь файл Excel как документ или нажми ❌ Отмена")

async def handle_mvm_upload_xml_file(message: types.Message, state: FSMContext, bot) -> None:
    if not message.document:
        if message.text == "❌ Отмена":
            u = message.from_user.id
            if u in user_files:
                user_files[u] = {}
            await state.clear()
            await cmd_start(message, state)
            return
        await message.answer("📎 Отправь XML файл как документ или нажми ❌ Отмена")
        return

    user_id = message.from_user.id
    file_name = message.document.file_name or ""
    if not file_name.lower().endswith('.xml'):
        await message.answer("❌ Нужен файл с расширением .xml")
        return

    xml_path, error = await download_xml_from_telegram(bot, message, user_id)
    if error == "file_too_big":
        file_size_mb = round(message.document.file_size / (1024 * 1024), 1)
        await message.answer(
            f"⚠️ XML файл слишком большой ({file_size_mb} МБ).\n"
            f"Telegram не позволяет скачивать файлы > 20 МБ.\n\n"
            f"📎 Отправь прямую ссылку на XML файл\n"
            f"(загрузи на Google Drive, Dropbox, Яндекс.Диск и скопируй прямую ссылку):\n\n"
            f"Или нажми ❌ Отмена",
            reply_markup=get_mvm_waiting_xml_keyboard()
        )
        return
    if error:
        await message.answer(f"❌ {error}\n\nОтправь файл заново: ")
        return

    await _validate_and_save_upload_xml(message, state, xml_path)

async def handle_mvm_upload_xml_text(message: types.Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        u = message.from_user.id
        if u in user_files:
            user_files[u] = {}
        await state.clear()
        await cmd_start(message, state)
        return
    if message.text == "🚀 Обработать":
        await process_files_mvm(message, state, None)
        return

    text = message.text.strip()
    if text.startswith('http://') or text.startswith('https://'):
        user_id = message.from_user.id
        await message.answer("⏳ Скачиваю XML файл по ссылке...")
        from urllib.parse import urlparse, unquote
        parsed = urlparse(text)
        url_filename = (
            unquote(Path(parsed.path).name) if parsed.path else "catalog.xml"
        )
        if not url_filename.lower().endswith('.xml'):
            url_filename = "catalog.xml"

        xml_path, error = await download_file_by_url(text, user_id, url_filename)
        if error:
            await message.answer(
                f"❌ {error}\n\nПроверь ссылку и отправь заново, "
                f"или отправь XML файл как документ: ",
                reply_markup=get_mvm_waiting_xml_keyboard()
            )
            return
        await _validate_and_save_upload_xml(message, state, xml_path)
        return

    await message.answer(
        "📎 Отправь XML файл как документ,\n"
        "прямую ссылку на файл (http://...),\n"
        "или нажми ❌ Отмена"
    )

async def _validate_and_save_upload_xml(
    message: types.Message, state: FSMContext, xml_path: str
) -> None:
    try:
        xml_reader = XmlReader()
        offer_count = xml_reader.get_offer_count(xml_path)
        if offer_count == 0:
            await message.answer("❌ XML файл не содержит офферов. Отправь другой: ")
            return
    except ValueError as e:
        await message.answer(f"❌ Ошибка чтения XML: {e}")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error("Ошибка XML при загрузке МВМ: %s", e, exc_info=True)
        return

    await state.update_data(mvm_xml_file_path=xml_path)
    await message.answer(
        f"✅ XML файл загружен! Офферов: {offer_count}\n\n"
        f"Нажми 🚀 Обработать для запуска синхронизации: ",
        reply_markup=get_process_keyboard()
    )

async def process_files_mvm(message: types.Message, state: FSMContext, bot) -> None:
    user_id = message.from_user.id
    if user_id not in user_files or len(user_files[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла МП!")
        return

    data = await state.get_data()
    schema_id     = data.get('selected_schema_id')
    xml_file_path = data.get('mvm_xml_file_path')
    if not schema_id:
        await message.answer("❌ Схема не выбрана!")
        return
    if not xml_file_path:
        await message.answer("❌ XML файл не загружен!")
        return

    processing_id = await storage.db.start_processing(user_id)
    await message.answer("⏳ Обработка по МВМ-схеме...", reply_markup=ReplyKeyboardRemove())

    try:
        file_paths = user_files[user_id]
        for marketplace, file_path in file_paths.items():
            await storage.db.add_file(
                user_id, processing_id, marketplace,
                os.path.basename(file_path), file_path
            )

        await message.answer("📖 Читаю файлы МП и XML каталог...")
        comparison_result = await storage.db.get_schema_matches(schema_id)

        xml_reader    = XmlReader()
        xml_offer_data = xml_reader.get_offer_data(xml_file_path)
        xml_categories = xml_reader.get_categories(xml_file_path)
        logger.info("XML: %d офферов, %d категорий", len(xml_offer_data), len(xml_categories))

        total_matches = sum(
            len(v) for k, v in comparison_result.items()
            if isinstance(v, list) and k.startswith('match')
        )
        await message.answer(
            f"🔄 Синхронизирую по МВМ-схеме\n"
            f"📊 Сопоставлений: {total_matches}\n"
            f"📦 XML офферов: {len(xml_offer_data)}"
        )

        comparator = AIComparator()
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = f"output/{user_id}_{timestamp}"
        os.makedirs(output_dir, exist_ok=True)

        output_sync_paths = {
            'wildberries': f"{output_dir}/WB_синхронизировано.xlsx",
            'ozon':        f"{output_dir}/Ozon_синхронизировано.xlsx",
            'yandex':      f"{output_dir}/Яндекс_синхронизировано.xlsx",
        }
        report_path = f"{output_dir}/результат_{timestamp}.xlsx"
        selected_category_ids = await storage.db.get_schema_category_ids(schema_id)

        synchronizer = DataSynchronizer(
            comparison_result,
            ai_comparator=comparator,
            xml_offer_data=xml_offer_data,
            xml_categories=xml_categories,
            selected_category_ids=selected_category_ids,
        )
        synced_dfs, changes_log = await synchronizer.synchronize_data(
            file_paths, output_sync_paths
        )

        # Шаг 1: создаём файл отчёта через ExcelWriter
        await message.answer("📊 Создаю отчет...")
        writer = ExcelWriter()
        writer.create_report_with_changes(comparison_result, changes_log, report_path)

        # Шаг 2: добавляем AI-лог в уже готовый файл отчёта
        synchronizer.create_ai_log_in_report(report_path)

        wb_count     = len(synced_dfs['wildberries'])
        ozon_count   = len(synced_dfs['ozon'])
        yandex_count = len(synced_dfs['yandex'])
        total_synced = sum(len(changes_log[mp]) for mp in changes_log)
        xml_filled   = sum(
            1 for mp in changes_log
            for change in changes_log[mp]
            if change.get('source_marketplace') == 'xml'
        )

        await storage.db.complete_processing(
            processing_id, wb_count, ozon_count, yandex_count, total_synced
        )
        await message.answer("📤 Отправляю результаты...")
        for path in output_sync_paths.values():
            await message.answer_document(FSInputFile(path))
        await message.answer_document(FSInputFile(report_path), caption="📊 Отчет МВМ")

        user_files[user_id] = {}
        await state.clear()

        result_text = (
            f"✅ Готово!\n\n📦 Обработка товаров:\n"
            f"• WB: {wb_count}\n• Ozon: {ozon_count}\n• Яндекс: {yandex_count}\n\n"
            f"🔄 Синхронизировано ячеек: {total_synced}"
        )
        if xml_filled > 0:
            result_text += f"\n📦 Из XML каталога: {xml_filled}"
        await message.answer(result_text, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        await storage.db.fail_processing(processing_id, str(e))
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error("Ошибка обработки МВМ: %s", e, exc_info=True)

def register_upload_handlers(dp, bot) -> None:
    from functools import partial
    dp.message.register(select_schema_for_upload, F.text == "📤 Загрузить файлы")
    dp.message.register(partial(schema_selected, bot=bot), UploadStates.selecting_schema)
    dp.message.register(partial(handle_file, bot=bot), UploadStates.waiting_for_files, F.document)
    dp.message.register(partial(process_files, bot=bot), UploadStates.waiting_for_files, F.text == "🚀 Обработать")
    dp.message.register(partial(handle_mvm_upload_mp_file, bot=bot), UploadMvmStates.waiting_for_mp_files, F.document)
    dp.message.register(handle_mvm_upload_mp_text, UploadMvmStates.waiting_for_mp_files, F.text)
    dp.message.register(partial(handle_mvm_upload_xml_file, bot=bot), UploadMvmStates.waiting_for_xml_file, F.document)
    dp.message.register(partial(process_files_mvm, bot=bot), UploadMvmStates.waiting_for_xml_file, F.text == "🚀 Обработать")
    dp.message.register(handle_mvm_upload_xml_text, UploadMvmStates.waiting_for_xml_file, F.text)