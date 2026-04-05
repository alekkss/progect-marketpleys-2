"""
Обработчики создания схемы МВМ (3 маркетплейса + XML каталог).

Сценарий:
    1. Ввод названия схемы
    2. Загрузка 3 файлов МП (WB, Ozon, Яндекс)
    3. Загрузка XML файла каталога
    4. AI-сопоставление 4 источников
    5. Сохранение схемы с типом 'mvm'

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
    get_mvm_waiting_xml_keyboard
)
from bot.storage import user_schemas, db
from bot.utils import download_file
from bot.handlers.common import schema_management

from config.config import FILE_CONFIGS
from utils.excel_reader import ExcelReader
from utils.xml_reader import XmlReader
from services.ai_comparator import AIComparator
from bot.security import AccessManager

logger = logging.getLogger('schema_create_mvm')


# ===== ШАГ 1: ВВОД НАЗВАНИЯ =====

async def mvm_schema_name_entered(message: types.Message, state: FSMContext):
    """Имя МВМ-схемы введено — валидация уникальности и переход к загрузке файлов"""
    if message.text == "❌ Отмена":
        await state.clear()
        await schema_management(message, state)
        return

    schema_name = message.text.strip()
    user_id = message.from_user.id

    # Проверяем уникальность имени (с учётом прав)
    can_see_all = AccessManager.can_see_all_schemas(user_id)

    if can_see_all:
        existing = db.get_schema_by_name_global(schema_name)
        if existing:
            owner_info = (
                f" (владелец: ID {existing['owner_id']})"
                if existing['owner_id'] != user_id else ""
            )
            await message.answer(
                f"❌ Схема с названием '{schema_name}' уже существует{owner_info}.\n\n"
                "Введи другое название:"
            )
            return
    else:
        if db.get_schema(user_id, schema_name):
            await message.answer(
                "❌ Схема с таким названием уже существует.\n"
                "Введи другое название:"
            )
            return

    # Сохраняем название и переходим к загрузке 3 МП
    await state.update_data(schema_name=schema_name, mp_files_processed=False)
    user_schemas[user_id] = {}
    await state.set_state(SchemaMvmStates.waiting_mp_files)

    await message.answer(
        f"✅ Название МВМ-схемы: '{schema_name}'\n\n"
        "📎 Отправь 3 файла Excel шаблонов маркетплейсов:\n"
        "• WB (файл с 'wb' в названии)\n"
        "• Ozon (файл с 'ozon' в названии)\n"
        "• Яндекс (файл с 'yandex' в названии)",
        reply_markup=get_cancel_keyboard()
    )


# ===== ШАГ 2: ЗАГРУЗКА 3 МП ФАЙЛОВ =====

async def handle_mvm_mp_file(message: types.Message, state: FSMContext, bot):
    """Обработка файла маркетплейса для МВМ-схемы (аналог стандартного флоу)"""
    user_id = message.from_user.id

    if user_id not in user_schemas:
        user_schemas[user_id] = {}

    # Защита от дублирования
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

    # Когда все 3 МП загружены — переходим к XML
    if loaded == 3:
        data = await state.get_data()
        if data.get('mp_files_processed'):
            return

        await state.update_data(mp_files_processed=True)
        await state.set_state(SchemaMvmStates.waiting_xml_file)

        await message.answer(
            "✅ Все 3 шаблона МП загружены!\n\n"
            "📎 Теперь отправь XML файл каталога (МВидео/xway):",
            reply_markup=get_mvm_waiting_xml_keyboard()
        )


async def handle_mvm_mp_text(message: types.Message, state: FSMContext):
    """Обработка текста в состоянии загрузки МП файлов (отмена)"""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
    else:
        await message.answer("📎 Отправь файл Excel как документ или нажми ❌ Отмена")


# ===== ШАГ 3: ЗАГРУЗКА XML ФАЙЛА =====

async def handle_mvm_xml_file(message: types.Message, state: FSMContext, bot):
    """Обработка XML файла каталога — валидация и сохранение пути"""
    user_id = message.from_user.id

    if not message.document:
        await message.answer("📎 Отправь XML файл как документ")
        return

    file_name = message.document.file_name or ""

    # Проверяем расширение
    if not file_name.lower().endswith('.xml'):
        await message.answer(
            "❌ Нужен файл с расширением .xml\n\n"
            "Отправь XML файл каталога:"
        )
        return

    # Скачиваем файл
    file = await bot.get_file(message.document.file_id)
    downloads_dir = Path("downloads") / str(user_id)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    xml_path = str(downloads_dir / file_name)
    await bot.download_file(file.file_path, xml_path)

    # Валидируем XML — проверяем наличие офферов
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

    except ValueError as e:
        await message.answer(
            f"❌ Ошибка чтения XML: {e}\n\n"
            "Отправь корректный файл:"
        )
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error(f"Ошибка чтения XML: {e}", exc_info=True)
        return

    # Сохраняем путь к XML в FSM
    await state.update_data(xml_file_path=xml_path)

    await message.answer(
        f"✅ XML файл загружен!\n\n"
        f"📦 Офферов: {offer_count}\n"
        f"📋 Полей XML: {len(xml_fields)}\n\n"
        "Нажми кнопку для запуска AI-сопоставления:",
        reply_markup=get_mvm_create_schema_keyboard()
    )


async def handle_mvm_xml_text(message: types.Message, state: FSMContext):
    """Обработка текста в состоянии ожидания XML (отмена)"""
    if message.text == "❌ Отмена":
        user_id = message.from_user.id
        if user_id in user_schemas:
            user_schemas[user_id] = {}
        await state.clear()
        await schema_management(message, state)
    else:
        await message.answer("📎 Отправь XML файл как документ или нажми ❌ Отмена")


# ===== ШАГ 4: ФИНАЛИЗАЦИЯ (AI + СОХРАНЕНИЕ) =====

async def finalize_mvm_schema(message: types.Message, state: FSMContext):
    """Финализация создания МВМ-схемы: AI-сопоставление 4 источников и сохранение"""
    user_id = message.from_user.id

    # Проверяем наличие файлов МП
    if user_id not in user_schemas or len(user_schemas[user_id]) != 3:
        await message.answer("❌ Не хватает файлов МП. Начни заново.")
        return

    data = await state.get_data()
    schema_name = data.get('schema_name')
    xml_file_path = data.get('xml_file_path')

    if not schema_name:
        await message.answer("❌ Название схемы потеряно. Начни заново.")
        return

    if not xml_file_path:
        await message.answer("❌ XML файл не загружен. Начни заново.")
        return

    await message.answer(
        "⏳ Анализирую столбцы из 4 источников...",
        reply_markup=ReplyKeyboardRemove()
    )

    try:
        file_paths = user_schemas[user_id]

        # === Читаем столбцы из 3 МП ===
        reader = ExcelReader()
        columns = {}

        for marketplace, file_path in file_paths.items():
            config = FILE_CONFIGS[marketplace]
            columns[marketplace] = reader.get_column_names(
                file_path,
                config['sheet_name'],
                config['header_row']
            )

        # === Читаем поля из XML ===
        xml_reader = XmlReader()
        xml_fields = xml_reader.get_field_names(xml_file_path)

        await message.answer(
            f"📊 Столбцы прочитаны:\n"
            f"• WB: {len(columns['wildberries'])}\n"
            f"• Ozon: {len(columns['ozon'])}\n"
            f"• Яндекс: {len(columns['yandex'])}\n"
            f"• XML: {len(xml_fields)}\n\n"
            "🤖 AI сопоставляет 4 источника..."
        )

        # === AI-сопоставление 4 источников ===
        # ИСПРАВЛЕНО: правильное имя метода compare_columns_mvm
        comparator = AIComparator()
        comparison_result = comparator.compare_columns_mvm(
            columns['wildberries'],
            columns['ozon'],
            columns['yandex'],
            xml_fields
        )

        # === Подсчёт статистики ===
        stats = _count_match_stats(comparison_result)
        total_saved = stats['total']

        # === Создаём схему в БД с типом 'mvm' ===
        schema_id = db.create_schema(user_id, schema_name, schema_type='mvm')

        if not schema_id:
            await message.answer("❌ Схема с таким названием уже существует!")
            return

        # Сохраняем сопоставления (полный JSON)
        db.save_schema_matches(schema_id, comparison_result)

        # Очистка
        user_schemas[user_id] = {}
        await state.clear()

        # === Формируем итоговое сообщение ===
        text = _build_result_message(schema_name, stats)
        await message.answer(text, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка создания МВМ-схемы: {e}", exc_info=True)


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

def _count_match_stats(comparison_result: dict) -> dict:
    """
    Подсчитывает статистику по типам сопоставлений.

    Ключи соответствуют тому, что возвращает AIComparator._merge_mvm_results:
        matches_all_four         — WB + Ozon + Яндекс + XML
        matches_triple_1_2_3     — WB + Ozon + Яндекс (без XML)
        matches_triple_1_2_4     — WB + Ozon + XML
        matches_triple_1_3_4     — WB + Яндекс + XML
        matches_triple_2_3_4     — Ozon + Яндекс + XML
        matches_pair_1_2         — WB + Ozon
        matches_pair_1_3         — WB + Яндекс
        matches_pair_2_3         — Ozon + Яндекс
        matches_pair_1_4         — WB + XML
        matches_pair_2_4         — Ozon + XML
        matches_pair_3_4         — Яндекс + XML

    Args:
        comparison_result: результат AI-сопоставления

    Returns:
        Словарь со счётчиками по каждому типу и общим итогом
    """
    # ИСПРАВЛЕНО: ключи приведены к именованию из _merge_mvm_results
    match_keys = [
        'matches_all_four',
        'matches_triple_1_2_3',
        'matches_triple_1_2_4',
        'matches_triple_1_3_4',
        'matches_triple_2_3_4',
        'matches_pair_1_2',
        'matches_pair_1_3',
        'matches_pair_2_3',
        'matches_pair_1_4',
        'matches_pair_2_4',
        'matches_pair_3_4',
    ]

    stats = {}
    total = 0

    for key in match_keys:
        count = len(comparison_result.get(key, []))
        stats[key] = count
        total += count

    stats['total'] = total
    return stats


def _build_result_message(schema_name: str, stats: dict) -> str:
    """
    Формирует итоговое сообщение о созданной МВМ-схеме.

    Args:
        schema_name: название схемы
        stats: словарь статистики из _count_match_stats

    Returns:
        Текст сообщения для пользователя
    """
    text = f"✅ МВМ-схема '{schema_name}' создана!\n\n"
    text += f"📊 Всего сопоставлений: {stats['total']}\n\n"

    # ИСПРАВЛЕНО: ключи приведены к именованию из _merge_mvm_results
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

def register_schema_create_mvm_handlers(dp, bot):
    """Регистрация обработчиков создания МВМ-схем"""
    from functools import partial

    # Ввод названия
    dp.message.register(
        mvm_schema_name_entered,
        SchemaMvmStates.waiting_schema_name
    )

    # Загрузка 3 МП файлов (документы)
    dp.message.register(
        partial(handle_mvm_mp_file, bot=bot),
        SchemaMvmStates.waiting_mp_files,
        F.document
    )

    # Текст в состоянии ожидания МП (отмена)
    dp.message.register(
        handle_mvm_mp_text,
        SchemaMvmStates.waiting_mp_files,
        F.text
    )

    # Загрузка XML файла (документ)
    dp.message.register(
        partial(handle_mvm_xml_file, bot=bot),
        SchemaMvmStates.waiting_xml_file,
        F.document
    )

    # Текст в состоянии ожидания XML (отмена)
    dp.message.register(
        handle_mvm_xml_text,
        SchemaMvmStates.waiting_xml_file,
        F.text
    )

    # Финализация
    dp.message.register(
        finalize_mvm_schema,
        F.text == "✅ Создать схему МВМ"
    )
