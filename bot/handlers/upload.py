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
import logging
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
from bot.storage import user_files, db
from bot.utils import download_file, download_xml_from_telegram, download_file_by_url
from bot.handlers.common import cmd_start

from config.config import FILE_CONFIGS
from utils.excel_writer import ExcelWriter
from utils.xml_reader import XmlReader
from services.data_synchronizer import DataSynchronizer
from services.ai_comparator import AIComparator
from utils.logger_config import setup_logger
from bot.security import AccessManager

logger = setup_logger('upload')


# =====================================================================
#  ОБЩИЙ ВХОД: ВЫБОР СХЕМЫ
# =====================================================================

async def select_schema_for_upload(message: types.Message, state: FSMContext):
    """Выбор схемы для загрузки файлов — определяет тип и направляет в нужный флоу"""
    user_id = message.from_user.id

    can_see_all = AccessManager.can_see_all_schemas(user_id)
    schemas = db.get_user_schemas(user_id, all_schemas=can_see_all)

    if not schemas:
        await message.answer(
            "❌ У тебя нет схем!\n\n"
            "Сначала создай схему через 📋 Управление схемами"
        )
        return

    keyboard = get_schema_list_keyboard(schemas)

    if not keyboard:
        await message.answer(
            "❌ У тебя нет валидных схем!\n\n"
            "Создай новую схему через 📋 Управление схемами"
        )
        return

    # Формируем текст с типами схем
    text = "Выбери схему для синхронизации:\n\n"
    for i, schema in enumerate(schemas, 1):
        s_type = schema.get('schema_type', 'standard')
        type_icon = "📦" if s_type == 'mvm' else "📊"
        text += f"{type_icon} {schema['name']}\n"

    await state.set_state(UploadStates.selecting_schema)
    await message.answer(text, reply_markup=keyboard)


async def schema_selected(message: types.Message, state: FSMContext, bot):
    """Схема выбрана — определяем тип и начинаем загрузку файлов"""
    if message.text == "❌ Отмена":
        await cmd_start(message, state)
        return

    user_id = message.from_user.id

    can_see_all = AccessManager.can_see_all_schemas(user_id)

    if can_see_all:
        schema = db.get_schema_by_name_global(message.text)
    else:
        schema = db.get_schema(user_id, message.text)

    if not schema:
        await message.answer("❌ Схема не найдена. Выбери из списка.")
        return

    schema_id = schema['id']
    schema_type = db.get_schema_type(schema_id)

    if schema_type == 'mvm':
        # МВМ-флоу
        await state.update_data(
            selected_schema_id=schema_id,
            selected_schema_type='mvm',
            mvm_mp_files_processed=False,
        )
        user_files[user_id] = {}
        await state.set_state(UploadMvmStates.waiting_for_mp_files)

        await message.answer(
            f"📦 МВМ-схема '{message.text}' выбрана\n\n"
            "Отправь 3 файла Excel (wb, ozon, yandex)\n"
            "Затем — XML файл каталога",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        # Стандартный флоу
        await state.update_data(
            selected_schema_id=schema_id,
            selected_schema_type='standard',
        )
        user_files[user_id] = {}
        await state.set_state(UploadStates.waiting_for_files)

        await message.answer(
            f"✅ Схема '{message.text}' выбрана\n\n"
            "Отправь 3 файла Excel",
            reply_markup=ReplyKeyboardRemove()
        )


# =====================================================================
#  СТАНДАРТНЫЙ ФЛОУ (3 МП) — без изменений
# =====================================================================

async def handle_file(message: types.Message, state: FSMContext, bot):
    """Обработка загруженного файла (стандартная схема)"""
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

        await message.answer(
            "✅ Все файлы загружены!",
            reply_markup=get_process_keyboard()
        )


async def process_files(message: types.Message, state: FSMContext, bot):
    """Обработка файлов по стандартной схеме"""
    user_id = message.from_user.id

    if user_id not in user_files or len(user_files[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла!")
        return

    data = await state.get_data()
    schema_id = data.get('selected_schema_id')

    if not schema_id:
        await message.answer("❌ Схема не выбрана!")
        return

    processing_id = db.start_processing(user_id)
    await message.answer("⏳ Обработка по схеме...")

    try:
        file_paths = user_files[user_id]

        for marketplace, file_path in file_paths.items():
            db.add_file(user_id, processing_id, marketplace, os.path.basename(file_path), file_path)

        await message.answer("📖 Читаю файлы...")

        comparison_result = db.get_schema_matches(schema_id)

        await message.answer(
            f"🔄 Синхронизирую по схеме ({len(comparison_result.get('matches_all_three', []))} столбцов)..."
        )

        comparator = AIComparator()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = f"output/{user_id}_{timestamp}"
        os.makedirs(output_dir, exist_ok=True)

        output_sync_paths = {
            'wildberries': f"{output_dir}/WB_синхронизировано.xlsx",
            'ozon': f"{output_dir}/Ozon_синхронизировано.xlsx",
            'yandex': f"{output_dir}/Яндекс_синхронизировано.xlsx",
        }

        report_path = f"{output_dir}/результат_{timestamp}.xlsx"

        synchronizer = DataSynchronizer(comparison_result, ai_comparator=comparator)
        synced_dfs, changes_log = synchronizer.synchronize_data(
            file_paths, output_sync_paths, report_path=report_path
        )

        await message.answer("📊 Создаю отчет...")

        writer = ExcelWriter()
        writer.create_report_with_changes(comparison_result, changes_log, report_path)

        if hasattr(synchronizer, 'ai_validation_log') and synchronizer.ai_validation_log:
            logger.info(f"📋 AI-логи: {len(synchronizer.ai_validation_log)} записей")
            synchronizer._create_ai_log_sheet_in_report(report_path)

        wb_count = len(synced_dfs['wildberries'])
        ozon_count = len(synced_dfs['ozon'])
        yandex_count = len(synced_dfs['yandex'])
        total_synced = sum(len(changes_log[mp]) for mp in changes_log)

        db.complete_processing(processing_id, wb_count, ozon_count, yandex_count, total_synced)

        await message.answer("📤 Отправляю результаты...")

        for marketplace, path in output_sync_paths.items():
            doc = FSInputFile(path)
            await message.answer_document(doc)

        report_doc = FSInputFile(report_path)
        await message.answer_document(report_doc, caption="📊 Отчет")

        user_files[user_id] = {}
        await state.clear()

        await message.answer(
            f"✅ Готово!\n\n"
            f"📦 Обработка товаров:\n"
            f"• WB: {wb_count}\n"
            f"• Ozon: {ozon_count}\n"
            f"• Яндекс: {yandex_count}\n\n"
            f"🔄 Синхронизировано ячеек: {total_synced}",
            reply_markup=get_main_menu_keyboard()
        )

    except Exception as e:
        db.fail_processing(processing_id, str(e))
        await message.answer(f"❌ Ошибка: {str(e)}")
        logging.error(f"Error: {e}", exc_info=True)


# =====================================================================
#  МВМ-ФЛОУ (3 МП + XML)
# =====================================================================

async def handle_mvm_upload_mp_file(message: types.Message, state: FSMContext, bot):
    """Загрузка файла МП при обработке по МВМ-схеме"""
    user_id = message.from_user.id

    if user_id not in user_files:
        user_files[user_id] = {}

    data = await state.get_data()
    if data.get('mvm_mp_files_processed'):
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
            "✅ Все 3 файла МП загружены!\n\n"
            "📎 Теперь отправь XML файл каталога:",
            reply_markup=get_mvm_waiting_xml_keyboard()
        )


async def handle_mvm_upload_mp_text(message: types.Message, state: FSMContext):
    """Текст в состоянии ожидания МП при загрузке МВМ"""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_files:
            user_files[user_id] = {}
        await state.clear()
        await cmd_start(message, state)
    else:
        await message.answer("📎 Отправь файл Excel как документ или нажми ❌ Отмена")


async def handle_mvm_upload_xml_file(message: types.Message, state: FSMContext, bot):
    """Загрузка XML файла при обработке по МВМ-схеме"""
    if not message.document:
        if message.text == "❌ Отмена":
            user_id = message.from_user.id
            if user_id in user_files:
                user_files[user_id] = {}
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

    # Скачиваем через обёртку с обработкой ошибки большого файла
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
        await message.answer(f"❌ {error}\n\nОтправь файл заново:")
        return

    # Валидируем и сохраняем
    await _validate_and_save_upload_xml(message, state, xml_path)


async def _validate_and_save_upload_xml(
    message: types.Message,
    state: FSMContext,
    xml_path: str,
):
    """
    Общая логика валидации XML и перехода к обработке при загрузке МВМ.

    Вынесена в отдельную функцию, чтобы не дублировать код
    между загрузкой через Telegram и загрузкой по URL.
    """
    try:
        xml_reader = XmlReader()
        offer_count = xml_reader.get_offer_count(xml_path)
        if offer_count == 0:
            await message.answer("❌ XML файл не содержит офферов. Отправь другой:")
            return
    except ValueError as e:
        await message.answer(f"❌ Ошибка чтения XML: {e}")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error(f"Ошибка XML при загрузке МВМ: {e}", exc_info=True)
        return

    await state.update_data(mvm_xml_file_path=xml_path)

    await message.answer(
        f"✅ XML файл загружен! Офферов: {offer_count}\n\n"
        "Нажми 🚀 Обработать для запуска синхронизации:",
        reply_markup=get_process_keyboard()
    )


async def handle_mvm_upload_xml_text(message: types.Message, state: FSMContext):
    """Текст в состоянии ожидания XML при загрузке МВМ (отмена, обработка или URL)"""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_files:
            user_files[user_id] = {}
        await state.clear()
        await cmd_start(message, state)
    elif message.text == "🚀 Обработать":
        await process_files_mvm(message, state, None)
    else:
        # Пробуем обработать как URL
        text = message.text.strip()
        if text.startswith('http://') or text.startswith('https://'):
            user_id = message.from_user.id
            await message.answer("⏳ Скачиваю XML файл по ссылке...")

            from urllib.parse import urlparse, unquote
            parsed = urlparse(text)
            url_filename = unquote(Path(parsed.path).name) if parsed.path else "catalog.xml"
            if not url_filename.lower().endswith('.xml'):
                url_filename = "catalog.xml"

            xml_path, error = await download_file_by_url(text, user_id, url_filename)

            if error:
                await message.answer(
                    f"❌ {error}\n\n"
                    "Проверь ссылку и отправь заново, "
                    "или отправь XML файл как документ:",
                    reply_markup=get_mvm_waiting_xml_keyboard()
                )
                return

            await _validate_and_save_upload_xml(message, state, xml_path)
        else:
            await message.answer(
                "📎 Отправь XML файл как документ,\n"
                "прямую ссылку на файл (http://...),\n"
                "или нажми ❌ Отмена"
            )


async def process_files_mvm(message: types.Message, state: FSMContext, bot):
    """Обработка файлов по МВМ-схеме (3 МП + XML как источник данных)"""
    user_id = message.from_user.id

    if user_id not in user_files or len(user_files[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла МП!")
        return

    data = await state.get_data()
    schema_id = data.get('selected_schema_id')
    xml_file_path = data.get('mvm_xml_file_path')

    if not schema_id:
        await message.answer("❌ Схема не выбрана!")
        return

    if not xml_file_path:
        await message.answer("❌ XML файл не загружен!")
        return

    processing_id = db.start_processing(user_id)
    await message.answer("⏳ Обработка по МВМ-схеме...", reply_markup=ReplyKeyboardRemove())

    try:
        file_paths = user_files[user_id]

        for marketplace, file_path in file_paths.items():
            db.add_file(user_id, processing_id, marketplace, os.path.basename(file_path), file_path)

        await message.answer("📖 Читаю файлы МП и XML каталог...")

        # Получаем схему сопоставлений
        comparison_result = db.get_schema_matches(schema_id)

        # Извлекаем данные офферов из XML
        xml_reader = XmlReader()
        xml_offer_data = xml_reader.get_offer_data(xml_file_path)
        xml_categories = xml_reader.get_categories(xml_file_path)

        logger.info(f"XML: {len(xml_offer_data)} офферов, {len(xml_categories)} категорий")

        # Подсчитываем общее количество сопоставлений
        total_matches = 0
        for key in comparison_result:
            if isinstance(comparison_result[key], list) and key.startswith('match'):
                total_matches += len(comparison_result[key])

        await message.answer(
            f"🔄 Синхронизирую по МВМ-схеме\n"
            f"📊 Сопоставлений: {total_matches}\n"
            f"📦 XML офферов: {len(xml_offer_data)}"
        )

        comparator = AIComparator()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = f"output/{user_id}_{timestamp}"
        os.makedirs(output_dir, exist_ok=True)

        output_sync_paths = {
            'wildberries': f"{output_dir}/WB_синхронизировано.xlsx",
            'ozon': f"{output_dir}/Ozon_синхронизировано.xlsx",
            'yandex': f"{output_dir}/Яндекс_синхронизировано.xlsx",
        }

        report_path = f"{output_dir}/результат_{timestamp}.xlsx"

        # Извлекаем выбранные категории из схемы
        selected_category_ids = db.get_schema_category_ids(schema_id)

        if selected_category_ids:
            logger.info(
                f"📂 Фильтр по категориям: {len(selected_category_ids)} категорий"
            )

        # Создаём DataSynchronizer с XML-данными и фильтром категорий
        synchronizer = DataSynchronizer(
            comparison_result,
            ai_comparator=comparator,
            xml_offer_data=xml_offer_data,
            xml_categories=xml_categories,
            selected_category_ids=selected_category_ids,
        )

        synced_dfs, changes_log = synchronizer.synchronize_data(
            file_paths, output_sync_paths, report_path=report_path
        )

        await message.answer("📊 Создаю отчет...")

        writer = ExcelWriter()
        writer.create_report_with_changes(comparison_result, changes_log, report_path)

        if hasattr(synchronizer, 'ai_validation_log') and synchronizer.ai_validation_log:
            logger.info(f"📋 AI-логи: {len(synchronizer.ai_validation_log)} записей")
            synchronizer._create_ai_log_sheet_in_report(report_path)

        wb_count = len(synced_dfs['wildberries'])
        ozon_count = len(synced_dfs['ozon'])
        yandex_count = len(synced_dfs['yandex'])
        total_synced = sum(len(changes_log[mp]) for mp in changes_log)

        # Считаем заполнения из XML отдельно
        xml_filled = sum(
            1 for mp in changes_log
            for change in changes_log[mp]
            if change.get('source_marketplace') == 'xml'
        )

        db.complete_processing(processing_id, wb_count, ozon_count, yandex_count, total_synced)

        await message.answer("📤 Отправляю результаты...")

        for marketplace, path in output_sync_paths.items():
            doc = FSInputFile(path)
            await message.answer_document(doc)

        report_doc = FSInputFile(report_path)
        await message.answer_document(report_doc, caption="📊 Отчет МВМ")

        user_files[user_id] = {}
        await state.clear()

        result_text = (
            f"✅ Готово!\n\n"
            f"📦 Обработка товаров:\n"
            f"• WB: {wb_count}\n"
            f"• Ozon: {ozon_count}\n"
            f"• Яндекс: {yandex_count}\n\n"
            f"🔄 Синхронизировано ячеек: {total_synced}"
        )

        if xml_filled > 0:
            result_text += f"\n📦 Из XML каталога: {xml_filled}"

        await message.answer(result_text, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        db.fail_processing(processing_id, str(e))
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка обработки МВМ: {e}", exc_info=True)


# =====================================================================
#  РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# =====================================================================

def register_upload_handlers(dp, bot):
    """Регистрация обработчиков загрузки"""
    from functools import partial

    # Общий вход
    dp.message.register(select_schema_for_upload, F.text == "📤 Загрузить файлы")
    dp.message.register(partial(schema_selected, bot=bot), UploadStates.selecting_schema)

    # Стандартный флоу (3 МП)
    dp.message.register(
        partial(handle_file, bot=bot),
        UploadStates.waiting_for_files, F.document
    )
    dp.message.register(
        partial(process_files, bot=bot),
        UploadStates.waiting_for_files, F.text == "🚀 Обработать"
    )

    # МВМ-флоу: загрузка 3 МП
    dp.message.register(
        partial(handle_mvm_upload_mp_file, bot=bot),
        UploadMvmStates.waiting_for_mp_files, F.document
    )
    dp.message.register(
        handle_mvm_upload_mp_text,
        UploadMvmStates.waiting_for_mp_files, F.text
    )

    # МВМ-флоу: загрузка XML
    dp.message.register(
        partial(handle_mvm_upload_xml_file, bot=bot),
        UploadMvmStates.waiting_for_xml_file, F.document
    )
    dp.message.register(
        partial(process_files_mvm, bot=bot),
        UploadMvmStates.waiting_for_xml_file, F.text == "🚀 Обработать"
    )
    dp.message.register(
        handle_mvm_upload_xml_text,
        UploadMvmStates.waiting_for_xml_file, F.text
    )
