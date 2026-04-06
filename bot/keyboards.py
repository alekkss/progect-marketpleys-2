"""
Клавиатуры для бота
"""
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from typing import List, Dict, Any, Set


def get_main_menu_keyboard(show_access_management=False):
    """Главное меню"""
    keyboard = [
        [KeyboardButton(text="📤 Загрузить файлы")],
        [KeyboardButton(text="📋 Управление схемами")],
        [KeyboardButton(text="📊 Моя статистика")]
    ]
    
    # Добавляем кнопку управления доступами только для владельца/админа
    if show_access_management:
        keyboard.append([KeyboardButton(text="🔐 Управление доступами")])
    
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True
    )


def get_schema_management_keyboard():
    """Меню управления схемами"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Создать схему")],
            [KeyboardButton(text="✏️ Редактировать схему")],
            [KeyboardButton(text="🔄 Обновить схему")],
            [KeyboardButton(text="🗑 Удалить схему")],
            [KeyboardButton(text="📋 Мои схемы")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )


def get_schema_edit_keyboard():
    """Меню редактирования схемы"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👁 Просмотреть текущие сопоставления")],
            [KeyboardButton(text="✏️ Изменить сопоставление")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )


def get_cancel_keyboard():
    """Клавиатура отмены (универсальная)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_process_keyboard():
    """Кнопка обработки"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🚀 Обработать")]],
        resize_keyboard=True
    )


def get_create_schema_keyboard():
    """Кнопка создания схемы"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Создать схему")]],
        resize_keyboard=True
    )


def get_update_schema_keyboard():
    """Кнопка обновления схемы"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Обновить схему")]],
        resize_keyboard=True
    )


def get_edit_column_keyboard():
    """Меню выбора столбца для редактирования (стандартная схема: 3 МП)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Изменить WB столбец")],
            [KeyboardButton(text="📝 Изменить Ozon столбец")],
            [KeyboardButton(text="📝 Изменить Яндекс столбец")],
            [KeyboardButton(text="🗑 Удалить сопоставление")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_back_to_edit_keyboard():
    """Возврат к редактированию"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Редактировать схему")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )


def get_edit_match_menu_keyboard():
    """Меню после загрузки файлов для редактирования"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Редактировать сопоставление")],
            [KeyboardButton(text="➕ Добавить сопоставление")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_schema_list_keyboard(schemas):
    """Клавиатура со списком схем"""
    keyboard_buttons = []
    for schema in schemas:
        if schema.get('name'):
            keyboard_buttons.append([KeyboardButton(text=schema['name'])])
    
    if not keyboard_buttons:
        return None
    
    keyboard_buttons.append([KeyboardButton(text="❌ Отмена")])
    return ReplyKeyboardMarkup(keyboard=keyboard_buttons, resize_keyboard=True)


def get_access_management_keyboard():
    """Меню управления доступами"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Текущий администратор")],
            [KeyboardButton(text="✏️ Изменить администратора")],
            [KeyboardButton(text="🗑 Удалить администратора")],
            [KeyboardButton(text="📋 Белый список пользователей")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )


def get_whitelist_confirmation_keyboard():
    """Подтверждение добавления в whitelist"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Подтвердить добавление")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_whitelist_removal_confirmation_keyboard():
    """Подтверждение удаления из whitelist"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, удалить из списка")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_whitelist_management_keyboard():
    """Меню управления белым списком"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Список пользователей")],
            [KeyboardButton(text="➕ Добавить пользователя")],
            [KeyboardButton(text="➖ Удалить пользователя")],
            [KeyboardButton(text="◀️ Назад к управлению доступами")]
        ],
        resize_keyboard=True
    )


def get_role_selection_keyboard():
    """Клавиатура для выбора роли при добавлении в whitelist"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1️⃣ Редактор")],
            [KeyboardButton(text="2️⃣ Пользователь")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True
    )


def get_admin_change_confirmation_keyboard():
    """Подтверждение изменения администратора"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Подтвердить")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_admin_removal_confirmation_keyboard():
    """Подтверждение удаления администратора"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, удалить")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_filter_matches_keyboard():
    """Клавиатура фильтрации сопоставлений по типу (стандартная схема: 3 МП)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎯 Показать тройные")],
            [KeyboardButton(text="🔗 Показать парные (WB+Ozon)")],
            [KeyboardButton(text="🔗 Показать парные (WB+Яндекс)")],
            [KeyboardButton(text="🔗 Показать парные (Ozon+Яндекс)")],
            [KeyboardButton(text="📋 Показать всё")],
            [KeyboardButton(text="➕ Добавить сопоставление")],
            [KeyboardButton(text="✏️ Редактировать сопоставление")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


# ===== КЛАВИАТУРЫ ДЛЯ СЦЕНАРИЯ МВМ (3 МП + XML) =====

def get_schema_type_keyboard():
    """Клавиатура выбора типа создаваемой схемы"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Загрузить 3 МП")],
            [KeyboardButton(text="📦 Создать схему МВМ")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_mvm_create_schema_keyboard():
    """Кнопка финализации создания схемы МВМ"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Создать схему МВМ")]],
        resize_keyboard=True
    )


def get_mvm_waiting_xml_keyboard():
    """Клавиатура ожидания XML файла после загрузки 3 МП"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_edit_column_keyboard_mvm():
    """
    Меню выбора столбца для редактирования (МВМ-схема: 3 МП + XML).

    Используется вместо get_edit_column_keyboard() когда редактируется
    сопоставление из схемы типа 'mvm'. Добавляет кнопку для изменения
    XML-поля.

    Принцип Open/Closed: новая клавиатура расширяет возможности без
    изменения существующей get_edit_column_keyboard().
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Изменить WB столбец")],
            [KeyboardButton(text="📝 Изменить Ozon столбец")],
            [KeyboardButton(text="📝 Изменить Яндекс столбец")],
            [KeyboardButton(text="📝 Изменить XML поле")],
            [KeyboardButton(text="🗑 Удалить сопоставление")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_filter_matches_mvm_keyboard():
    """
    Упрощённая клавиатура для МВМ-схем (3 МП + XML).

    МВМ-схемы имеют до 11 групп сопоставлений — показывать все
    фильтры в виде кнопок нецелесообразно. Вместо этого предлагается
    просмотр всего списка и переход к редактированию.

    Принцип Single Responsibility: отдельная клавиатура для отдельного
    контекста, не смешивается с логикой стандартных фильтров.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Показать всё")],
            [KeyboardButton(text="✏️ Редактировать сопоставление")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


# ===== КЛАВИАТУРЫ ДЛЯ ВЫБОРА КАТЕГОРИЙ XML =====

def get_category_search_keyboard():
    """
    Reply-клавиатура для этапа ввода поискового запроса по категориям.

    Показывается после загрузки XML — пользователь вводит текст
    для поиска категорий или нажимает отмену.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


def get_category_selection_inline_keyboard(
    categories: List[Dict[str, Any]],
    selected_ids: Set[str],
) -> InlineKeyboardMarkup:
    """
    Inline-клавиатура для множественного выбора категорий.

    Каждая категория отображается как кнопка с toggle-состоянием:
    ✅ — выбрана, пустой квадрат — не выбрана.
    Показывает название категории и количество офферов.

    В конце — кнопка подтверждения (активна только при выборе ≥1)
    и кнопка повторного поиска.

    Args:
        categories: список категорий из XmlReader.search_categories()
        selected_ids: множество уже выбранных category_id

    Returns:
        InlineKeyboardMarkup с toggle-кнопками
    """
    buttons: List[List[InlineKeyboardButton]] = []

    for cat in categories:
        cat_id = cat['id']
        cat_name = cat['name']
        offer_count = cat['offer_count']

        # Toggle-иконка: выбрана или нет
        is_selected = cat_id in selected_ids
        icon = "✅" if is_selected else "⬜"

        # Текст кнопки: иконка + название + количество офферов
        button_text = f"{icon} {cat_name} ({offer_count} шт.)"

        # callback_data: префикс cat_toggle: + id категории
        buttons.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"cat_toggle:{cat_id}",
            )
        ])

    # Кнопка подтверждения выбора (только если что-то выбрано)
    if selected_ids:
        buttons.append([
            InlineKeyboardButton(
                text=f"✅ Подтвердить выбор ({len(selected_ids)} кат.)",
                callback_data="cat_confirm",
            )
        ])

    # Кнопка повторного поиска
    buttons.append([
        InlineKeyboardButton(
            text="🔍 Искать другую категорию",
            callback_data="cat_search_again",
        )
    ])

    # Кнопка отмены
    buttons.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="cat_cancel",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)
