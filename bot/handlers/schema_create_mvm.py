"""
Обработчики создания схемы МВМ (3 маркетплейса + XML каталог).

Сценарий:
    1. Ввод названия схемы
    2. Загрузка 3 файлов МП (WB, Ozon, Яндекс)
    3. Загрузка XML файла каталога
    4. Поиск и выбор категорий товаров из XML
    5. AI-сопоставление 4 источников
    6. Сохранение схемы с типом 'mvm'

Принцип Single Responsibility: только FSM-логика МВМ-потока.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import logging
from aiogram import types, F
from aiogram.types import ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

from bot.states import SchemaMvmStates
from bot.keyboards import (
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_mvm_create_schema_keyboard,
    get_mvm_waiting_xml_keyboard,
    get_category_search_keyboard,
    get_category_selection_inline_keyboard,
)
from bot.storage import user_schemas
from bot import storage
from bot.utils import download_file, download_xml_from_telegram, download_file_by_url
from bot.handlers.common import schema_management
from bot.security import AccessManager

from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from utils.xml_reader import XmlReader
from services.ai_comparator import AIComparator

logger = logging.getLogger('schema_create_mvm')


# ===== ШАГ 1: ВВОД НАЗВАНИЯ =====

async def mvm_schema_name_entered(message: types.Message, state: FSMContext) -> None:
    """Имя МВМ-схемы введено — валидация уникальности и переход к загрузке файлов."""
    if message.text == "❌ Отмена":
        await state.clear()
        await schema_management(message, state)
        return

    schema_name = message.text.strip()
    user_id = message.from_user.id

    can_see_all = await AccessManager.can_see_all_schemas(user_id)

    if can_see_all:
        existing = await storage.db.get_schema_by_name_global(schema_name)
        if existing:
            owner_info = (
                f" (владелец: ID {existing['owner_id']})"
                if existing['owner_id'] != user_id
                else ""
            )
            await message.answer(
                f"❌ Схема с названием '{schema_name}' уже существует{owner_info}.\n\n"
                "Введи другое название:"
            )
            return
    else:
        existing = await storage.db.get_schema(user_id, schema_name)
        if existing:
            await message.answer(
                "❌ Схема с таким названием уже существует.\n"
                "Введи другое название:"
            )
            return

    await state.update_data(schema_name=schema_name, mp_files_processed=False)
    user_schemas[user_id] = {}
    await state.set_state(SchemaMvmStates.waiting_mp_files)

    await message.answer(
        f"✅ Название МВМ-схемы: '{schema_name}'\n\n"
        "📎 Отправь 3 файла Excel шаблонов маркетплейсов:\n"
        "• WB (файл с 'wb' в названии)\n"
        "• Ozon (файл с 'ozon' в названии)\n"
        "• Яндекс (файл с 'yandex' в названии)",
        reply_markup=get_cancel_keyboard(),
    )


# ===== ШАГ 2: ЗАГРУЗКА 3 МП ФАЙЛОВ =====

async def handle_mvm_mp_file(
    message: types.Message,
    state: FSMContext,
    bot,
) -> None:
    """Обработка файла маркетплейса для МВМ-схемы."""
    user_id = message.from_user.id

    if user_id not in user_schemas:
        user_schemas[user_id] = {}

    data = await state.get_data()
    if data.get('mp_files_processed'):
        return

    file_path, file_name, marketplace = await download_file(bot, message, user_id)

    if not marketplace:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex в название)")
        return

    if marketplace in user_schemas[user_id]:
        await message.answer(f"⚠️ {marketplace.upper()} уже загружен")
        return

    user_schemas[user_id][marketplace] = file_path
    loaded = len(user_schemas[user_id])
    await message.answer(f"✅ {marketplace.upper()} ({loaded}/3)")

    if loaded == 3:
        data = await state.get_data()
        if data.get('mp_files_processed'):
            return

        await state.update_data(mp_files_processed=True)
        await state.set_state(SchemaMvmStates.waiting_xml_file)

        await message.answer(
            "✅ Все 3 шаблона МП загружены!\n\n"
            "📎 Теперь отправь XML файл каталога (МВидео/xway):",
            reply_markup=get_mvm_waiting_xml_keyboard(),
        )


async def handle_mvm_mp_text(message: types.Message, state: FSMContext) -> None:
    """Обработка текста в состоянии загрузки МП файлов (отмена)."""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
    else:
        await message.answer(
            "📎 Отправь файл Excel как документ или нажми ❌ Отмена"
        )


# ===== ШАГ 3: ЗАГРУЗКА XML ФАЙЛА =====

async def handle_mvm_xml_file(
    message: types.Message,
    state: FSMContext,
    bot,
) -> None:
    """Обработка XML файла каталога — валидация и переход к выбору категорий."""
    user_id = message.from_user.id

    if not message.document:
        await message.answer("📎 Отправь XML файл как документ")
        return

    file_name = message.document.file_name or ""

    if not file_name.lower().endswith('.xml'):
        await message.answer(
            "❌ Нужен файл с расширением .xml\n\n"
            "Отправь XML файл каталога:"
        )
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
            reply_markup=get_mvm_waiting_xml_keyboard(),
        )
        return

    if error:
        await message.answer(f"❌ {error}\n\nОтправь файл заново:")
        return

    await _validate_and_proceed_xml(message, state, xml_path)


async def handle_mvm_xml_url(message: types.Message, state: FSMContext) -> bool:
    """Обработка URL-ссылки на XML файл (для файлов > 20 МБ)."""
    text = message.text.strip()

    if not (text.startswith('http://') or text.startswith('https://')):
        return False

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
            f"❌ {error}\n\n"
            "Проверь ссылку и отправь заново, "
            "или отправь XML файл как документ:",
            reply_markup=get_mvm_waiting_xml_keyboard(),
        )
        return True

    await _validate_and_proceed_xml(message, state, xml_path)
    return True


async def _validate_and_proceed_xml(
    message: types.Message,
    state: FSMContext,
    xml_path: str,
) -> None:
    """
    Общая логика валидации XML и перехода к выбору категорий.

    Вынесена чтобы не дублировать код между загрузкой через Telegram и URL.
    """
    try:
        xml_reader = XmlReader()
        offer_count = xml_reader.get_offer_count(xml_path)

        if offer_count == 0:
            await message.answer(
                "❌ XML файл не содержит офферов (<offer>).\n\n"
                "Проверь файл и отправь заново:"
            )
            return

        xml_fields = xml_reader.get_field_names(xml_path)
        xml_categories = xml_reader.get_categories(xml_path)

    except ValueError as e:
        await message.answer(
            f"❌ Ошибка чтения XML: {e}\n\n"
            "Отправь корректный файл:"
        )
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error("Ошибка чтения XML: %s", e, exc_info=True)
        return

    await state.update_data(
        xml_file_path=xml_path,
        selected_category_ids=[],
    )

    await state.set_state(SchemaMvmStates.waiting_category_search)

    await message.answer(
        f"✅ XML файл загружен!\n\n"
        f"📦 Офферов: {offer_count}\n"
        f"📋 Полей XML: {len(xml_fields)}\n"
        f"📂 Категорий: {len(xml_categories)}\n\n"
        "🔍 Введи название категории товаров из шаблонов МП\n"
        "(например: Холодильник, Телевизор, Кроссовки):",
        reply_markup=get_category_search_keyboard(),
    )


async def handle_mvm_xml_text(message: types.Message, state: FSMContext) -> None:
    """Обработка текста в состоянии ожидания XML (отмена или URL)."""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
        return

    text = message.text.strip()
    if text.startswith('http://') or text.startswith('https://'):
        await handle_mvm_xml_url(message, state)
        return

    await message.answer(
        "📎 Отправь XML файл как документ,\n"
        "прямую ссылку на файл (http://...),\n"
        "или нажми ❌ Отмена"
    )


# ===== ШАГ 4: ПОИСК И ВЫБОР КАТЕГОРИЙ =====

async def handle_category_search(message: types.Message, state: FSMContext) -> None:
    """Обработка поискового запроса по категориям XML."""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
        return

    query = message.text.strip()
    data = await state.get_data()
    xml_file_path = data.get('xml_file_path')

    if not xml_file_path:
        await message.answer("❌ XML файл потерян. Начни заново.")
        await state.clear()
        return

    xml_reader = XmlReader()
    found_categories = xml_reader.search_categories(xml_file_path, query)

    if not found_categories:
        await message.answer(
            f"❌ По запросу '{query}' категорий не найдено.\n\n"
            "Попробуй другой запрос\n"
            "(например: часть слова, синоним, другой падеж):",
            reply_markup=get_category_search_keyboard(),
        )
        return

    selected_ids = set(data.get('selected_category_ids', []))

    await state.update_data(found_categories=found_categories)
    await state.set_state(SchemaMvmStates.waiting_category_selection)

    text = (
        f"🔍 По запросу '{query}' найдено категорий: {len(found_categories)}\n\n"
        "Нажми на категорию чтобы выбрать/снять выбор.\n"
        "Можно выбрать несколько категорий:"
    )

    await message.answer(
        text,
        reply_markup=get_category_selection_inline_keyboard(
            found_categories, selected_ids
        ),
    )


async def handle_category_toggle(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    """Обработка нажатия на inline-кнопку категории (toggle выбора)."""
    cat_id = callback.data.split(":", 1)[1]

    data = await state.get_data()
    selected_ids = set(data.get('selected_category_ids', []))
    found_categories = data.get('found_categories', [])

    if cat_id in selected_ids:
        selected_ids.discard(cat_id)
    else:
        selected_ids.add(cat_id)

    await state.update_data(selected_category_ids=list(selected_ids))

    await callback.message.edit_reply_markup(
        reply_markup=get_category_selection_inline_keyboard(
            found_categories, selected_ids
        )
    )
    await callback.answer()


async def handle_category_search_again(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    """Возврат к поисковому вводу для добавления категорий."""
    await state.set_state(SchemaMvmStates.waiting_category_search)

    data = await state.get_data()
    selected_ids = set(data.get('selected_category_ids', []))

    if selected_ids:
        text = (
            f"📂 Уже выбрано категорий: {len(selected_ids)}\n\n"
            "🔍 Введи ещё один запрос для поиска дополнительных категорий:"
        )
    else:
        text = "🔍 Введи название категории для поиска:"

    await callback.message.answer(text, reply_markup=get_category_search_keyboard())
    await callback.answer()


async def handle_category_confirm(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    """Подтверждение выбора категорий — переход к финализации."""
    data = await state.get_data()
    selected_ids = set(data.get('selected_category_ids', []))
    xml_file_path = data.get('xml_file_path')

    if not selected_ids:
        await callback.answer("⚠️ Выбери хотя бы одну категорию!", show_alert=True)
        return

    xml_reader = XmlReader()
    all_categories = xml_reader.get_categories(xml_file_path)

    selected_names = [
        f"• {all_categories.get(cat_id, f'ID:{cat_id}')} (ID: {cat_id})"
        for cat_id in selected_ids
    ]

    filtered_offers = xml_reader.get_offer_data_by_categories(
        xml_file_path, selected_ids
    )

    await state.set_state(SchemaMvmStates.finalizing)

    selected_text = "\n".join(selected_names)
    await callback.message.answer(
        f"✅ Выбрано категорий: {len(selected_ids)}\n\n"
        f"{selected_text}\n\n"
        f"📦 Офферов в выбранных категориях: {len(filtered_offers)}\n\n"
        "Нажми кнопку для запуска AI-сопоставления:",
        reply_markup=get_mvm_create_schema_keyboard(),
    )
    await callback.answer()


async def handle_category_cancel(
    callback: types.CallbackQuery,
    state: FSMContext,
) -> None:
    """Отмена выбора категорий."""
    user_id = callback.from_user.id
    if user_id in user_schemas:
        user_schemas[user_id] = {}
    await state.clear()

    await callback.message.answer(
        "❌ Создание МВМ-схемы отменено.",
        reply_markup=get_main_menu_keyboard(),
    )
    await callback.answer()


async def handle_category_selection_text(
    message: types.Message,
    state: FSMContext,
) -> None:
    """Обработка текста в состоянии выбора категорий."""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
    else:
        await message.answer(
            "⬆️ Используй inline-кнопки выше для выбора категорий.\n"
            "Или нажми ❌ Отмена."
        )


# ===== ШАГ 5: ФИНАЛИЗАЦИЯ (AI + СОХРАНЕНИЕ) =====

async def finalize_mvm_schema(message: types.Message, state: FSMContext) -> None:
    """Финализация: AI-сопоставление 4 источников и сохранение схемы в БД."""
    user_id = message.from_user.id

    if message.text == "❌ Отмена":
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
        return

    if message.text != "✅ Создать схему МВМ":
        await message.answer(
            "Нажми кнопку '✅ Создать схему МВМ' для запуска или '❌ Отмена'",
            reply_markup=get_mvm_create_schema_keyboard(),
        )
        return

    if user_id not in user_schemas or len(user_schemas[user_id]) != 3:
        await message.answer("❌ Не хватает файлов МП. Начни заново.")
        return

    data = await state.get_data()
    schema_name = data.get('schema_name')
    xml_file_path = data.get('xml_file_path')
    selected_category_ids = data.get('selected_category_ids', [])

    if not schema_name:
        await message.answer("❌ Название схемы потеряно. Начни заново.")
        return

    if not xml_file_path:
        await message.answer("❌ XML файл не загружен. Начни заново.")
        return

    if not selected_category_ids:
        await message.answer("❌ Категории не выбраны. Начни заново.")
        return

    await message.answer(
        "⏳ Анализирую столбцы из 4 источников...",
        reply_markup=ReplyKeyboardRemove(),
    )

    try:
        file_paths = user_schemas[user_id]

        reader = ExcelReader()
        columns = {}

        for marketplace, file_path in file_paths.items():
            config = FILE_CONFIGS[marketplace]
            columns[marketplace] = reader.get_column_names(
                file_path,
                config['sheet_name'],
                config['header_row'],
            )

        xml_reader = XmlReader()
        filtered_offers = xml_reader.get_offer_data_by_categories(
            xml_file_path, set(selected_category_ids)
        )

        xml_fields = _extract_fields_from_offers(filtered_offers)

        await message.answer(
            f"📊 Столбцы прочитаны:\n"
            f"• WB: {len(columns['wildberries'])}\n"
            f"• Ozon: {len(columns['ozon'])}\n"
            f"• Яндекс: {len(columns['yandex'])}\n"
            f"• XML (по выбранным категориям): {len(xml_fields)}\n\n"
            "🤖 AI сопоставляет 4 источника..."
        )

        comparator = AIComparator()
        comparison_result = comparator.compare_columns_mvm(
            columns['wildberries'],
            columns['ozon'],
            columns['yandex'],
            xml_fields,
        )

        comparison_result['selected_category_ids'] = selected_category_ids

        stats = _count_match_stats(comparison_result)

        # Создаём схему в БД с типом 'mvm'
        schema_id = await storage.db.create_schema(user_id, schema_name, schema_type='mvm')

        if not schema_id:
            await message.answer("❌ Схема с таким названием уже существует!")
            return

        # Сохраняем сопоставления (полный JSON включая selected_category_ids)
        await storage.db.save_schema_matches(schema_id, comparison_result)

        user_schemas[user_id] = {}
        await state.clear()

        text = _build_result_message(
            schema_name, stats, selected_category_ids, xml_reader, xml_file_path
        )
        await message.answer(text, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error("Ошибка создания МВМ-схемы: %s", e, exc_info=True)


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

def _extract_fields_from_offers(offers: list) -> list:
    """
    Извлекает уникальные имена полей из списка офферов.

    Сохраняет порядок первого появления. Пропускает служебное поле offer_id.
    """
    from collections import OrderedDict
    seen: OrderedDict = OrderedDict()
    for offer in offers:
        for key in offer:
            if key == 'offer_id':
                continue
            if key not in seen:
                seen[key] = True
    return list(seen.keys())


def _count_match_stats(comparison_result: dict) -> dict:
    """Подсчитывает статистику по типам сопоставлений."""
    match_keys = [
        'matches_all_four',
        'matches_triple_1_2_3', 'matches_triple_1_2_4',
        'matches_triple_1_3_4', 'matches_triple_2_3_4',
        'matches_pair_1_2', 'matches_pair_1_3', 'matches_pair_2_3',
        'matches_pair_1_4', 'matches_pair_2_4', 'matches_pair_3_4',
    ]
    stats: dict = {}
    total = 0
    for key in match_keys:
        count = len(comparison_result.get(key, []))
        stats[key] = count
        total += count
    stats['total'] = total
    return stats


def _build_result_message(
    schema_name: str,
    stats: dict,
    selected_category_ids: list,
    xml_reader: XmlReader,
    xml_file_path: str,
) -> str:
    """Формирует итоговое сообщение о созданной МВМ-схеме."""
    text = f"✅ МВМ-схема '{schema_name}' создана!\n\n"

    all_categories = xml_reader.get_categories(xml_file_path)
    cat_names = [
        all_categories.get(cat_id, f"ID:{cat_id}")
        for cat_id in selected_category_ids
    ]

    text += f"📂 Категории ({len(cat_names)}):\n"
    for name in cat_names:
        text += f"  • {name}\n"
    text += "\n"
    text += f"📊 Всего сопоставлений: {stats['total']}\n\n"

    labels = {
        'matches_all_four':         "🎯 Четверные (WB+Ozon+Яндекс+XML)",
        'matches_triple_1_2_3':     "🔷 Тройные (WB+Ozon+Яндекс)",
        'matches_triple_1_2_4':     "🔷 Тройные (WB+Ozon+XML)",
        'matches_triple_1_3_4':     "🔷 Тройные (WB+Яндекс+XML)",
        'matches_triple_2_3_4':     "🔷 Тройные (Ozon+Яндекс+XML)",
        'matches_pair_1_2':         "🔗 Парные (WB+Ozon)",
        'matches_pair_1_3':         "🔗 Парные (WB+Яндекс)",
        'matches_pair_2_3':         "🔗 Парные (Ozon+Яндекс)",
        'matches_pair_1_4':         "🔗 Парные (WB+XML)",
        'matches_pair_2_4':         "🔗 Парные (Ozon+XML)",
        'matches_pair_3_4':         "🔗 Парные (Яндекс+XML)",
    }

    for key, label in labels.items():
        count = stats.get(key, 0)
        if count > 0:
            text += f"{label}: {count}\n"

    return text


# ===== РЕГИСТРАЦИЯ ХЕНДЛЕРОВ =====

def register_schema_create_mvm_handlers(dp, bot) -> None:
    """Регистрация обработчиков создания МВМ-схем."""
    from functools import partial

    dp.message.register(
        mvm_schema_name_entered,
        SchemaMvmStates.waiting_schema_name,
    )
    dp.message.register(
        partial(handle_mvm_mp_file, bot=bot),
        SchemaMvmStates.waiting_mp_files,
        F.document,
    )
    dp.message.register(
        handle_mvm_mp_text,
        SchemaMvmStates.waiting_mp_files,
        F.text,
    )
    dp.message.register(
        partial(handle_mvm_xml_file, bot=bot),
        SchemaMvmStates.waiting_xml_file,
        F.document,
    )
    dp.message.register(
        handle_mvm_xml_text,
        SchemaMvmStates.waiting_xml_file,
        F.text,
    )
    dp.message.register(
        handle_category_search,
        SchemaMvmStates.waiting_category_search,
        F.text,
    )
    dp.callback_query.register(
        handle_category_toggle,
        SchemaMvmStates.waiting_category_selection,
        F.data.startswith("cat_toggle:"),
    )
    dp.callback_query.register(
        handle_category_confirm,
        SchemaMvmStates.waiting_category_selection,
        F.data == "cat_confirm",
    )
    dp.callback_query.register(
        handle_category_search_again,
        SchemaMvmStates.waiting_category_selection,
        F.data == "cat_search_again",
    )
    dp.callback_query.register(
        handle_category_cancel,
        SchemaMvmStates.waiting_category_selection,
        F.data == "cat_cancel",
    )
    dp.message.register(
        handle_category_selection_text,
        SchemaMvmStates.waiting_category_selection,
        F.text,
    )
    dp.message.register(
        finalize_mvm_schema,
        SchemaMvmStates.finalizing,
    )