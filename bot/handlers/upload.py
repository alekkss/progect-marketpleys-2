"""
Обработчики загрузки и обработки файлов.
Поддерживает два типа схем:
- standard (3 МП): загрузка 3 Excel → задача в очередь
- mvm (3 МП + XML): загрузка 3 Excel + XML → задача в очередь

Принцип Open/Closed: МВМ-логика добавлена через отдельные StatesGroup
и хендлеры, стандартный флоу не модифицирован.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
from datetime import datetime
from aiogram import types, F
from aiogram.types import ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from bot.states import UploadStates, UploadMvmStates
from bot.keyboards import (
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_process_keyboard,
    get_schema_list_keyboard,
    get_mvm_waiting_xml_keyboard,
)
from bot import storage
from bot.utils import download_file, download_xml_from_telegram, download_file_by_url
from bot.handlers.common import cmd_start
from bot.security import AccessManager
from services.task_queue import Task, TaskQueue
from utils.xml_reader import XmlReader
from utils.logger_config import setup_logger

logger = setup_logger('upload')

# Ключ для хранения путей к файлам в сессионном хранилище
_SESSION_KEY_UPLOAD: str = 'upload'


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
        await storage.session_storage.set(user_id, _SESSION_KEY_UPLOAD, {})
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
        await storage.session_storage.set(user_id, _SESSION_KEY_UPLOAD, {})
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

    data = await state.get_data()
    if data.get('files_processed'):
        return

    file_path, file_name, marketplace = await download_file(bot, message, user_id)
    if not marketplace:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)")
        return

    user_files = await storage.session_storage.get_files_dict(user_id, _SESSION_KEY_UPLOAD)
    if marketplace in user_files:
        await message.answer(f"⚠️ {marketplace.upper()} уже загружен")
        return

    user_files[marketplace] = file_path
    await storage.session_storage.set_files_dict(user_id, _SESSION_KEY_UPLOAD, user_files)
    await message.answer(f"✅ {marketplace.upper()} ({len(user_files)}/3)")

    if len(user_files) == 3:
        data = await state.get_data()
        if data.get('files_processed'):
            return
        await state.update_data(files_processed=True)
        await message.answer("✅ Все файлы загружены!", reply_markup=get_process_keyboard())

async def process_files(message: types.Message, state: FSMContext, bot, task_queue: TaskQueue) -> None:
    """
    Постановка стандартной задачи в очередь на обработку.

    Вместо inline-синхронизации создаёт Task и передаёт её в TaskQueue.
    Результат придёт пользователю автоматически от TaskWorker.
    """
    user_id = message.from_user.id
    user_files = await storage.session_storage.get_files_dict(user_id, _SESSION_KEY_UPLOAD)

    if len(user_files) != 3:
        await message.answer("❌ Загрузи 3 файла!")
        return

    data = await state.get_data()
    schema_id = data.get('selected_schema_id')
    if not schema_id:
        await message.answer("❌ Схема не выбрана!")
        return

    # Формируем пути для выходных файлов
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = f"output/{user_id}_{timestamp}"
    os.makedirs(output_dir, exist_ok=True)

    report_path = f"{output_dir}/результат_{timestamp}.xlsx"

    # Создаём задачу и ставим в очередь
    task = Task(
        user_id=user_id,
        chat_id=message.chat.id,
        task_type="standard",
        schema_id=schema_id,
        file_paths=user_files,
        output_dir=output_dir,
        report_path=report_path,
    )
    await task_queue.enqueue(task)

    # Очищаем сессию и состояние
    await state.clear()
    await storage.session_storage.clear(user_id)

    # Сообщаем пользователю о постановке в очередь
    queue_length = await task_queue.get_queue_length()
    position_text = ""
    if queue_length > 1:
        position_text = f"\n\n📊 Задач в очереди: {queue_length}"

    await message.answer(
        f"✅ Задача принята в обработку!{position_text}\n\n"
        f"⏳ Результат придёт автоматически по завершении.",
        reply_markup=get_main_menu_keyboard()
    )

# =====================================================================
# МВМ-ФЛОУ (3 МП + XML)
# =====================================================================
async def handle_mvm_upload_mp_file(message: types.Message, state: FSMContext, bot) -> None:
    """Загрузка файла МП при обработке по МВМ-схеме."""
    user_id = message.from_user.id

    data = await state.get_data()
    if data.get('mvm_mp_files_processed'):
        return

    file_path, file_name, marketplace = await download_file(bot, message, user_id)
    if not marketplace:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)")
        return

    user_files = await storage.session_storage.get_files_dict(user_id, _SESSION_KEY_UPLOAD)
    if marketplace in user_files:
        await message.answer(f"⚠️ {marketplace.upper()} уже загружен")
        return

    user_files[marketplace] = file_path
    await storage.session_storage.set_files_dict(user_id, _SESSION_KEY_UPLOAD, user_files)
    loaded = len(user_files)
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
        await storage.session_storage.clear(u)
        await state.clear()
        await cmd_start(message, state)
        return
    await message.answer("📎 Отправь файл Excel как документ или нажми ❌ Отмена")

async def handle_mvm_upload_xml_file(message: types.Message, state: FSMContext, bot) -> None:
    if not message.document:
        if message.text == "❌ Отмена":
            u = message.from_user.id
            await storage.session_storage.clear(u)
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
        await storage.session_storage.clear(u)
        await state.clear()
        await cmd_start(message, state)
        return
    if message.text == "🚀 Обработать":
        await process_files_mvm(message, state, None, None)
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

async def process_files_mvm(message: types.Message, state: FSMContext, bot, task_queue: TaskQueue) -> None:
    """
    Постановка МВМ-задачи в очередь на обработку.

    Вместо inline-синхронизации создаёт Task и передаёт её в TaskQueue.
    Результат придёт пользователю автоматически от TaskWorker.
    """
    user_id = message.from_user.id
    user_files = await storage.session_storage.get_files_dict(user_id, _SESSION_KEY_UPLOAD)

    if len(user_files) != 3:
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

    # Формируем пути для выходных файлов
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = f"output/{user_id}_{timestamp}"
    os.makedirs(output_dir, exist_ok=True)

    report_path = f"{output_dir}/результат_{timestamp}.xlsx"

    # Создаём задачу и ставим в очередь
    task = Task(
        user_id=user_id,
        chat_id=message.chat.id,
        task_type="mvm",
        schema_id=schema_id,
        file_paths=user_files,
        output_dir=output_dir,
        report_path=report_path,
        xml_file_path=xml_file_path,
    )
    await task_queue.enqueue(task)

    # Очищаем сессию и состояние
    await state.clear()
    await storage.session_storage.clear(user_id)

    # Сообщаем пользователю о постановке в очередь
    queue_length = await task_queue.get_queue_length()
    position_text = ""
    if queue_length > 1:
        position_text = f"\n\n📊 Задач в очереди: {queue_length}"

    await message.answer(
        f"✅ МВМ-задача принята в обработку!{position_text}\n\n"
        f"⏳ Результат придёт автоматически по завершении.",
        reply_markup=get_main_menu_keyboard()
    )

def register_upload_handlers(dp, bot, task_queue: TaskQueue) -> None:
    from functools import partial
    dp.message.register(select_schema_for_upload, F.text == "📤 Загрузить файлы")
    dp.message.register(partial(schema_selected, bot=bot), UploadStates.selecting_schema)
    dp.message.register(partial(handle_file, bot=bot), UploadStates.waiting_for_files, F.document)
    dp.message.register(partial(process_files, bot=bot, task_queue=task_queue), UploadStates.waiting_for_files, F.text == "🚀 Обработать")
    dp.message.register(partial(handle_mvm_upload_mp_file, bot=bot), UploadMvmStates.waiting_for_mp_files, F.document)
    dp.message.register(handle_mvm_upload_mp_text, UploadMvmStates.waiting_for_mp_files, F.text)
    dp.message.register(partial(handle_mvm_upload_xml_file, bot=bot), UploadMvmStates.waiting_for_xml_file, F.document)
    dp.message.register(partial(process_files_mvm, bot=bot, task_queue=task_queue), UploadMvmStates.waiting_for_xml_file, F.text == "🚀 Обработать")
    dp.message.register(handle_mvm_upload_xml_text, UploadMvmStates.waiting_for_xml_file, F.text)