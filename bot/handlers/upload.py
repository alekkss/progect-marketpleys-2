"""
Обработчики загрузки и обработки файлов
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

from bot.states import UploadStates
from bot.keyboards import (
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_process_keyboard,
    get_schema_list_keyboard
)
from bot.storage import user_files, db
from bot.utils import download_file
from bot.handlers.common import cmd_start

from config.config import FILE_CONFIGS
from utils.excel_writer import ExcelWriter
from services.data_synchronizer import DataSynchronizer
from services.ai_comparator import AIComparator
from utils.logger_config import setup_logger

logger = setup_logger('upload')


async def select_schema_for_upload(message: types.Message, state: FSMContext):
    """Выбор схемы для загрузки файлов"""
    user_id = message.from_user.id
    schemas = db.get_user_schemas(user_id)
    
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
    
    await state.set_state(UploadStates.selecting_schema)
    await message.answer("Выбери схему для синхронизации:", reply_markup=keyboard)


async def schema_selected(message: types.Message, state: FSMContext, bot):
    """Схема выбрана, начинаем загрузку файлов"""
    if message.text == "❌ Отмена":
        await cmd_start(message, state)
        return
    
    user_id = message.from_user.id
    schema = db.get_schema(user_id, message.text)
    
    if not schema:
        await message.answer("❌ Схема не найдена. Выбери из списка.")
        return
    
    # Сохраняем выбранную схему
    await state.update_data(selected_schema_id=schema['id'])
    user_files[user_id] = {}
    
    await state.set_state(UploadStates.waiting_for_files)
    await message.answer(
        f"✅ Схема '{message.text}' выбрана\n\n"
        "Отправь 3 файла Excel",
        reply_markup=ReplyKeyboardRemove()
    )


async def handle_file(message: types.Message, state: FSMContext, bot):
    """Обработка загруженного файла"""
    user_id = message.from_user.id
    
    if user_id not in user_files:
        user_files[user_id] = {}
    
    # НОВОЕ: Проверяем, не обработали ли мы уже все файлы
    data = await state.get_data()
    if data.get('files_processed'):
        return  # Уже обработали, игнорируем дубликаты
    
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
        # КРИТИЧНО: Двойная проверка флага
        data = await state.get_data()
        if data.get('files_processed'):
            return
        
        await state.update_data(files_processed=True)
        
        await message.answer(
            "✅ Все файлы загружены!",
            reply_markup=get_process_keyboard()
        )


async def process_files(message: types.Message, state: FSMContext, bot):
    """Обработка файлов по схеме"""
    user_id = message.from_user.id
    
    if user_id not in user_files or len(user_files[user_id]) != 3:
        await message.answer("❌ Загрузи 3 файла!")
        return
    
    # Получаем выбранную схему
    data = await state.get_data()
    schema_id = data.get('selected_schema_id')
    
    if not schema_id:
        await message.answer("❌ Схема не выбрана!")
        return
    
    processing_id = db.start_processing(user_id)
    await message.answer("⏳ Обработка по схеме...")
    
    try:
        file_paths = user_files[user_id]
        
        # Добавляем файлы в БД
        for marketplace, file_path in file_paths.items():
            db.add_file(user_id, processing_id, marketplace, os.path.basename(file_path), file_path)
        
        await message.answer("📖 Читаю файлы...")
        
        # Получаем сопоставления из схемы
        comparison_result = db.get_schema_matches(schema_id)
        
        await message.answer(
            f"🔄 Синхронизирую по схеме ({len(comparison_result['matches_all_three'])} столбцов)..."
        )
        
        # Создаем AI comparator для validation проверок
        comparator = AIComparator()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = f"output/{user_id}_{timestamp}"
        os.makedirs(output_dir, exist_ok=True)
        
        output_sync_paths = {
            'wildberries': f"{output_dir}/WB_синхронизировано.xlsx",
            'ozon': f"{output_dir}/Ozon_синхронизировано.xlsx",
            'yandex': f"{output_dir}/Яндекс_синхронизировано.xlsx"
        }
        
        report_path = f"{output_dir}/результат_{timestamp}.xlsx"
        
        # Синхронизация
        synchronizer = DataSynchronizer(comparison_result, ai_comparator=comparator)
        synced_dfs, changes_log = synchronizer.synchronize_data(
            file_paths,
            output_sync_paths,
            report_path=report_path
        )
        
        await message.answer("📊 Создаю отчет...")
        
        writer = ExcelWriter()
        writer.create_report_with_changes(comparison_result, changes_log, report_path)
        
        # Добавляем AI-логи если есть
        if hasattr(synchronizer, 'ai_validation_log') and synchronizer.ai_validation_log:
            logger.info(f"📋 Создаю лист с AI-логами ({len(synchronizer.ai_validation_log)} записей)...")
            synchronizer._create_ai_log_sheet_in_report(report_path)
        
        # Статистика
        wb_count = len(synced_dfs['wildberries'])
        ozon_count = len(synced_dfs['ozon'])
        yandex_count = len(synced_dfs['yandex'])
        total_synced = sum(len(changes_log[mp]) for mp in changes_log)
        
        db.complete_processing(processing_id, wb_count, ozon_count, yandex_count, total_synced)
        
        await message.answer("📤 Отправляю результаты...")
        
        # Отправляем файлы
        for marketplace, path in output_sync_paths.items():
            doc = FSInputFile(path)
            await message.answer_document(doc)
        
        report_doc = FSInputFile(report_path)
        await message.answer_document(report_doc, caption="📊 Отчет")
        
        # Очистка
        user_files[user_id] = {}
        await state.clear()
        
        await message.answer(
            f"✅ Готово!\n\n"
            f"📦 Обработано товаров:\n"
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


def register_upload_handlers(dp, bot):
    """Регистрация обработчиков загрузки"""
    from functools import partial
    
    dp.message.register(select_schema_for_upload, F.text == "📤 Загрузить файлы")
    dp.message.register(partial(schema_selected, bot=bot), UploadStates.selecting_schema)
    dp.message.register(partial(handle_file, bot=bot), UploadStates.waiting_for_files, F.document)
    dp.message.register(partial(process_files, bot=bot), F.text == "🚀 Обработать")

