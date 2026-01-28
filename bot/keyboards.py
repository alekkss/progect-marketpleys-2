"""
Клавиатуры для бота
"""
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove


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
    """Меню выбора столбца для редактирования"""
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
            [KeyboardButton(text="✏️ Редактировать сопоставление")],  # ✅ УНИКАЛЬНЫЙ ТЕКСТ
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
    """Клавиатура фильтрации сопоставлений по типу"""
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


