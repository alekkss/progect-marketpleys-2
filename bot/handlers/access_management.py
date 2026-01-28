"""
Обработчики для управления доступами (только для владельца)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiogram import types, F, Router
from aiogram.fsm.context import FSMContext
from bot.keyboards import (
    get_access_management_keyboard,
    get_admin_change_confirmation_keyboard,
    get_admin_removal_confirmation_keyboard,
    get_main_menu_keyboard,
    get_cancel_keyboard,
    get_whitelist_management_keyboard,        # ← ДОБАВИТЬ
    get_whitelist_confirmation_keyboard,      # ← ДОБАВИТЬ (не используется, но для будущего)
    get_whitelist_removal_confirmation_keyboard,
    get_role_selection_keyboard
)
from bot.states import AccessManagementStates
from bot.security import AccessManager
from bot.storage import db

# Создаём роутер для этого модуля
router = Router()


async def show_access_management_menu(message: types.Message, state: FSMContext):
    """Главное меню управления доступами (владелец и администратор)"""
    user_id = message.from_user.id
    
    # Проверка: владелец или администратор
    if not AccessManager.has_access_management_rights(user_id):
        await message.answer("⛔ Только владелец и администратор могут управлять доступами.")
        return
    
    # Получаем текущего админа
    admin_id = AccessManager.get_admin_user_id()
    
    if admin_id:
        admin_info = f"👤 Текущий администратор: `{admin_id}`"
    else:
        admin_info = "👤 Администратор не назначен"
    
    await state.set_state(AccessManagementStates.main_menu)
    await message.answer(
        f"🔐 Управление доступами\n\n{admin_info}",
        reply_markup=get_access_management_keyboard(),
        parse_mode="Markdown"
    )


async def show_current_admin(message: types.Message, state: FSMContext):
    """Показать текущего администратора (ТОЛЬКО владелец)"""
    user_id = message.from_user.id
    
    # ← ДОБАВИТЬ ПРОВЕРКУ
    if not AccessManager.is_owner(user_id):
        await message.answer("⛔ Только владелец может просматривать администратора.")
        return
    
    admin_id = AccessManager.get_admin_user_id()
    
    if admin_id:
        admin_text = f"👤 Администратор: `{admin_id}`"
    else:
        admin_text = "👤 Администратор: не назначен"
    
    await message.answer(
        f"🔐 Текущие доступы:\n\n"
        f"👑 Владелец: `{user_id}` (вы)\n"
        f"{admin_text}",
        parse_mode="Markdown"
    )


async def start_admin_change(message: types.Message, state: FSMContext):
    """Начало процесса изменения администратора (ТОЛЬКО владелец)"""
    user_id = message.from_user.id
    
    # ← ДОБАВИТЬ ПРОВЕРКУ
    if not AccessManager.is_owner(user_id):
        await message.answer("⛔ Только владелец может изменять администратора.")
        return
    
    await state.set_state(AccessManagementStates.waiting_for_admin_id)
    await message.answer(
        "✏️ Введите Telegram ID нового администратора:\n\n"
        "Чтобы узнать ID пользователя, попросите его:\n"
        "1. Написать боту @userinfobot\n"
        "2. Прислать вам полученный ID\n\n"
        "Или отправьте /cancel для отмены",
        reply_markup=get_cancel_keyboard()
    )


async def process_new_admin_id(message: types.Message, state: FSMContext):
    """Обработка введённого ID администратора"""
    user_id = message.from_user.id
    
    if not AccessManager.is_owner(user_id):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.")
        return
    
    # Проверяем, что это число
    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ Ошибка: ID должен быть числом.\n\n"
            "Попробуйте ещё раз или /cancel для отмены"
        )
        return
    
    # Проверяем, что это не сам владелец
    if new_admin_id == user_id:
        await message.answer(
            "❌ Вы не можете назначить себя администратором (вы уже владелец).\n\n"
            "Введите другой ID или /cancel для отмены"
        )
        return
    
    # Сохраняем ID в FSM для подтверждения
    await state.update_data(new_admin_id=new_admin_id)
    await state.set_state(AccessManagementStates.confirming_admin_change)
    
    await message.answer(
        f"✅ Подтвердите назначение администратора:\n\n"
        f"👤 Новый администратор: `{new_admin_id}`\n\n"
        f"После подтверждения этот пользователь получит доступ к управлению схемами и другим функциям.",
        reply_markup=get_admin_change_confirmation_keyboard(),
        parse_mode="Markdown"
    )


async def confirm_admin_change(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if not AccessManager.is_owner(user_id):
        await state.clear()
        await message.answer("У вас нет прав для этого действия.")
        return
    
    # Получаем ID нового админа из FSM
    data = await state.get_data()
    new_admin_id = data.get('new_admin_id')
    
    if not new_admin_id:
        await message.answer("❌ Ошибка: ID нового администратора не найден.\n\nОперация отменена.")
        await state.clear()
        return
    
    # Назначаем нового администратора
    success = AccessManager.set_admin(new_admin_id, user_id)
    
    await state.clear()
    
    if success:
        await message.answer(
            f"✅ *Администратор успешно назначен!*\n\n"
            f"ID нового администратора: {new_admin_id}",
            reply_markup=get_main_menu_keyboard(show_access_management=AccessManager.has_access_management_rights(user_id)),  # ✅
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "❌ Не удалось назначить администратора.\n\nВозможно, произошла ошибка.",
            reply_markup=get_main_menu_keyboard(show_access_management=AccessManager.has_access_management_rights(user_id))  # ✅
        )


async def start_admin_removal(message: types.Message, state: FSMContext):
    """Начало процесса удаления администратора (ТОЛЬКО владелец)"""
    user_id = message.from_user.id
    
    # ← ДОБАВИТЬ ПРОВЕРКУ
    if not AccessManager.is_owner(user_id):
        await message.answer("⛔ Только владелец может удалять администратора.")
        return
    
    admin_id = AccessManager.get_admin_user_id()
    
    if not admin_id:
        await message.answer(
            "ℹ️ Администратор не назначен.",
            reply_markup=get_access_management_keyboard()
        )
        return
    
    await state.set_state(AccessManagementStates.confirming_admin_removal)
    await message.answer(
        f"⚠️ Вы уверены, что хотите удалить администратора?\n\n"
        f"👤 Текущий администратор: `{admin_id}`\n\n"
        f"После удаления этот пользователь потеряет все права доступа.",
        reply_markup=get_admin_removal_confirmation_keyboard(),
        parse_mode="Markdown"
    )


async def confirm_admin_removal(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if not AccessManager.is_owner(user_id):
        await state.clear()
        await message.answer("У вас нет прав для этого действия.")
        return
    
    # Удаляем администратора
    success = AccessManager.remove_admin(user_id)
    
    await state.clear()
    
    if success:
        await message.answer(
            "✅ Администратор успешно удалён.",
            reply_markup=get_main_menu_keyboard(show_access_management=AccessManager.has_access_management_rights(user_id))  # ✅
        )
    else:
        await message.answer(
            "❌ Не удалось удалить администратора.\n\nВозможно, администратор не был назначен.",
            reply_markup=get_main_menu_keyboard(show_access_management=AccessManager.has_access_management_rights(user_id))  # ✅
        )


async def cancel_operation(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    await state.clear()
    await message.answer(
        "❌ Операция отменена.",
        reply_markup=get_main_menu_keyboard(show_access_management=AccessManager.has_access_management_rights(user_id))  # ✅ Показываем кнопку, если есть права
    )


def register_access_management_handlers(dp):
    """Регистрация всех обработчиков управления доступами"""
    
    # Главное меню (переход из common.py)
    dp.message.register(
        show_access_management_menu,
        F.text == "🔐 Управление доступами"
    )
    
    # Кнопки главного меню
    dp.message.register(
        show_current_admin,
        AccessManagementStates.main_menu,
        F.text == "👤 Текущий администратор"
    )
    
    dp.message.register(
        start_admin_change,
        AccessManagementStates.main_menu,
        F.text == "✏️ Изменить администратора"
    )
    
    dp.message.register(
        start_admin_removal,
        AccessManagementStates.main_menu,
        F.text == "🗑 Удалить администратора"
    )
    
    # Процесс изменения администратора
    dp.message.register(
        process_new_admin_id,
        AccessManagementStates.waiting_for_admin_id,
        F.text != "❌ Отмена"
    )
    
    dp.message.register(
        confirm_admin_change,
        AccessManagementStates.confirming_admin_change,
        F.text == "✅ Подтвердить"
    )
    
    # Процесс удаления администратора
    dp.message.register(
        confirm_admin_removal,
        AccessManagementStates.confirming_admin_removal,
        F.text == "✅ Да, удалить"
    )
    
    # Отмена операций admin
    dp.message.register(
        cancel_operation,
        AccessManagementStates.waiting_for_admin_id,
        F.text == "❌ Отмена"
    )
    
    dp.message.register(
        cancel_operation,
        AccessManagementStates.confirming_admin_change,
        F.text == "❌ Отмена"
    )
    
    dp.message.register(
        cancel_operation,
        AccessManagementStates.confirming_admin_removal,
        F.text == "❌ Отмена"
    )
    
    # ============ WHITELIST HANDLERS ============
    
    # Меню whitelist
    dp.message.register(
        show_whitelist_menu,
        AccessManagementStates.main_menu,
        F.text == "📋 Белый список пользователей"
    )
    
    # Просмотр списка
    dp.message.register(
        show_whitelist_users,
        AccessManagementStates.whitelist_menu,
        F.text == "👥 Список пользователей"
    )
    
    # Добавление пользователя с выбором роли
    dp.message.register(
        start_whitelist_addition,
        AccessManagementStates.whitelist_menu,
        F.text == "➕ Добавить пользователя"
    )
    
    dp.message.register(
        process_role_selection,  # ← Обработка выбора роли
        AccessManagementStates.selecting_role,
    )
    
    dp.message.register(
        process_whitelist_user_id,
        AccessManagementStates.waiting_for_whitelist_user_id,
        F.text != "❌ Отмена"
    )
    
    # Удаление пользователя
    dp.message.register(
        start_whitelist_removal,
        AccessManagementStates.whitelist_menu,
        F.text == "➖ Удалить пользователя"
    )
    
    dp.message.register(
        process_whitelist_removal_selection,
        AccessManagementStates.selecting_user_to_remove,
        F.text != "❌ Отмена"
    )
    
    dp.message.register(
        confirm_whitelist_removal,
        AccessManagementStates.confirming_whitelist_removal,
        F.text == "✅ Да, удалить из списка"
    )
    
    # Кнопка "Назад к управлению доступами"
    dp.message.register(
        back_to_access_management,
        AccessManagementStates.whitelist_menu,
        F.text == "◀️ Назад к управлению доступами"
    )
    
    
    
    dp.message.register(
        cancel_operation,
        AccessManagementStates.waiting_for_whitelist_user_id,
        F.text == "❌ Отмена"
    )
    
    dp.message.register(
        cancel_operation,
        AccessManagementStates.selecting_user_to_remove,
        F.text == "❌ Отмена"
    )
    
    dp.message.register(
        cancel_operation,
        AccessManagementStates.confirming_whitelist_removal,
        F.text == "❌ Отмена"
    )

# ============ УПРАВЛЕНИЕ БЕЛЫМ СПИСКОМ ============

async def show_whitelist_menu(message: types.Message, state: FSMContext):
    """Меню управления белым списком"""
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await message.answer("⛔ Только владелец и администратор могут управлять белым списком.")
        return
    
    # Получаем информацию о слотах с разбивкой
    slots_info = AccessManager.get_whitelist_slots_info()
    
    await state.set_state(AccessManagementStates.whitelist_menu)
    await message.answer(
        f"📋 *Управление белым списком*\n\n"
        f"👤 Редакторы: {slots_info['editor']['used']}\n"
        f"👥 Пользователи: {slots_info['user']['used']}\n"
        f"📊 Всего: {slots_info['total_used']}",
        reply_markup=get_whitelist_management_keyboard(),
        parse_mode="Markdown"
    )


async def show_whitelist_users(message: types.Message, state: FSMContext):
    """Показать список пользователей из whitelist"""
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await message.answer("⛔ Нет прав на просмотр.")
        return
    
    # Получаем детальную информацию
    users = db.get_whitelist_details()
    
    if not users:
        await message.answer(
            "📋 Белый список пуст\n\n"
            "Доступно:\n"
            "• 1 слот редактора (расширенные права)\n"
            "• 2 слота пользователей (базовый доступ)"
        )
        return
    
    # Формируем список с указанием ролей
    text = "📋 Пользователи в белом списке:\n\n"
    
    for i, user in enumerate(users, 1):
        user_id_display = user['user_id']
        role = user['role']
        role_emoji = "✏️" if role == 'editor' else "👤"
        role_text = "Редактор" if role == 'editor' else "Пользователь"
        added_at = user['added_at']
        notes = user['notes'] or 'без заметки'
        
        text += f"{i}. {role_emoji} {role_text}\n"
        text += f"   ID: `{user_id_display}`\n"
        text += f"   Заметка: {notes}\n"
        text += f"   Добавлен: {added_at[:10]}\n\n"
    
    await message.answer(text, parse_mode="Markdown")


async def start_whitelist_addition(message: types.Message, state: FSMContext):
    """Начало добавления пользователя - выбор роли"""
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await message.answer("⛔ Нет прав на добавление.")
        return
    
    await state.set_state(AccessManagementStates.selecting_role)
    
    text = "🎯 *Выберите роль для нового пользователя*\n\n"
    text += "*1️⃣ Редактор*\n"
    text += "   • Видит все схемы\n"
    text += "   • Может редактировать все схемы\n\n"
    text += "*2️⃣ Пользователь*\n"
    text += "   • Видит только свои схемы\n\n"
        
    await message.answer(
        text,
        reply_markup=get_role_selection_keyboard(),
        parse_mode="Markdown"
    )

async def process_role_selection(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await state.clear()
        await message.answer("У вас нет прав для этого действия.")
        return
    
    # СНАЧАЛА проверяем отмену!
    if message.text == "❌ Отмена":
        await state.set_state(AccessManagementStates.whitelist_menu)  # ✅ Возвращаемся в меню whitelist
        await message.answer(
            "❌ Операция отменена.",
            reply_markup=get_whitelist_management_keyboard()  # ✅ Правильная клавиатура с кнопками белого списка
        )
        return
    
    # Проверяем выбранную роль
    selected_role = None
    if message.text == "1️⃣ Редактор":
        selected_role = "editor"
    elif message.text == "2️⃣ Пользователь":
        selected_role = "user"
    else:
        await message.answer(
            "Пожалуйста, выберите роль из предложенных вариантов.",
            reply_markup=get_role_selection_keyboard()
        )
        return
    
    # Сохраняем выбранную роль
    await state.update_data(selected_role=selected_role)
    await state.set_state(AccessManagementStates.waiting_for_whitelist_user_id)
    
    role_text = "👤 *Редактор*" if selected_role == "editor" else "👥 *Пользователь*"
    
    await message.answer(
        f"📝 *Добавление {role_text}*\n\n"
        f"Отправьте Telegram ID пользователя:\n\n"
        f"💡 *Как узнать ID:*\n"
        f"1. Перешлите сообщение от пользователя в @userinfobot\n"
        f"2. Скопируйте его ID\n\n"
        f"Или нажмите \"❌ Отмена\" для отмены.",
        reply_markup=get_cancel_keyboard()
    )

async def process_whitelist_user_id(message: types.Message, state: FSMContext):
    """Обработка введённого ID для whitelist"""
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.")
        return
    
    # Получаем выбранную роль из state
    data = await state.get_data()
    selected_role = data.get('selected_role', 'user')
    
    # Проверяем, что это число
    try:
        new_user_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ Ошибка: ID должен быть числом.\n\n"
            "Попробуйте ещё раз или /cancel для отмены"
        )
        return
    
    # Проверяем, что не добавляет себя
    if new_user_id == user_id:
        await message.answer(
            "❌ Вы не можете добавить себя.\n\n"
            f"Вы уже {'владелец' if AccessManager.is_owner(user_id) else 'администратор'}.\n\n"
            "Введите другой ID или /cancel для отмены"
        )
        return
    
    # Валидация через AccessManager (с ролью!)
    success, error = AccessManager.add_whitelist_user(new_user_id, user_id, role=selected_role, notes=None)
    
    if not success:
        await message.answer(
            f"❌ Ошибка: {error}\n\n"
            "Попробуйте другой ID или /cancel для отмены"
        )
        return
    
    # ✅ Успешно добавлен
    slots_info = AccessManager.get_whitelist_slots_info()
    role_text = "✏️ Редактор" if selected_role == 'editor' else "👤 Пользователь"
    
    # ✅ УСТАНАВЛИВАЕМ STATE ОБРАТНО В WHITELIST MENU!
    await state.set_state(AccessManagementStates.whitelist_menu)
    
    await message.answer(
        f"✅ Пользователь добавлен в белый список!\n\n"
        f"Роль: {role_text}\n"
        f"👤 ID: `{new_user_id}`\n\n"
        f"Текущая статистика:\n"
        f"✏️ Редактор: {slots_info['editor']['used']}\n"
        f"👤 Пользователи: {slots_info['user']['used']}\n"
        f"Всего: {slots_info['total_used']}",
        reply_markup=get_whitelist_management_keyboard(),
        parse_mode="Markdown"
    )


async def start_whitelist_removal(message: types.Message, state: FSMContext):
    """Начало удаления пользователя из whitelist"""
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await message.answer("⛔ Нет прав на удаление.")
        return
    
    # Получаем список
    users = db.get_whitelist_details()
    
    if not users:
        await message.answer(
            "ℹ️ Белый список пуст.\n\n"
            "Никого удалять не нужно.",
            reply_markup=get_whitelist_management_keyboard()
        )
        return
    
    # Формируем список для выбора
    text = "➖ Удаление пользователя из белого списка\n\n"
    text += "Выберите ID пользователя для удаления:\n\n"
    
    for i, user in enumerate(users, 1):
        user_id_display = user['user_id']
        notes = user['notes'] or 'без заметки'
        text += f"{i}. ID: `{user_id_display}` ({notes})\n"
    
    text += "\nВведите ID пользователя или /cancel для отмены"
    
    await state.set_state(AccessManagementStates.selecting_user_to_remove)
    await message.answer(text, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")


async def process_whitelist_removal_selection(message: types.Message, state: FSMContext):
    """Обработка выбора пользователя для удаления"""
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.")
        return
    
    # Проверяем, что это число
    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ Ошибка: введите числовой ID.\n\n"
            "Попробуйте ещё раз или /cancel для отмены"
        )
        return
    
    # Проверяем, что пользователь есть в whitelist
    if not AccessManager.is_in_whitelist(target_user_id):
        await message.answer(
            f"❌ Пользователь с ID `{target_user_id}` не найден в белом списке.\n\n"
            "Проверьте ID и попробуйте снова или /cancel для отмены",
            parse_mode="Markdown"
        )
        return
    
    # Сохраняем ID для подтверждения
    await state.update_data(target_user_id=target_user_id)
    await state.set_state(AccessManagementStates.confirming_whitelist_removal)
    
    await message.answer(
        f"⚠️ Подтвердите удаление пользователя:\n\n"
        f"👤 ID: `{target_user_id}`\n\n"
        f"После удаления этот пользователь потеряет доступ к боту.",
        reply_markup=get_whitelist_removal_confirmation_keyboard(),
        parse_mode="Markdown"
    )


async def confirm_whitelist_removal(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if not AccessManager.has_access_management_rights(user_id):
        await state.clear()
        await message.answer("У вас нет прав для этого действия.")
        return
    
    # Получаем ID пользователя для удаления
    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    
    if not target_user_id:
        await message.answer("❌ Ошибка: ID пользователя не найден.\n\nОперация отменена.")
        await state.set_state(AccessManagementStates.whitelist_menu)  # ✅ Возвращаемся в меню
        return
    
    # Удаляем пользователя
    success, error = AccessManager.remove_whitelist_user(target_user_id, user_id)
    
    if success:
        slots_info = AccessManager.get_whitelist_slots_info()
        
        await state.set_state(AccessManagementStates.whitelist_menu)  # ✅ Возвращаемся в меню вместо clear()
        await message.answer(
            f"✅ *Пользователь удален из белого списка!*\n\n"
            f"ID: {target_user_id}\n\n"
            f"📊 *Текущая статистика:*\n"
            f"Редакторы: {slots_info['editor']['used']}\n"
            f"Пользователи: {slots_info['user']['used']}\n"
            f"Всего: {slots_info['total_used']}",
            reply_markup=get_whitelist_management_keyboard(),
            parse_mode="Markdown"
        )
    else:
        # Обработка ошибки
        await state.set_state(AccessManagementStates.whitelist_menu)
        await message.answer(
            f"❌ {error}",
            reply_markup=get_whitelist_management_keyboard()
        )


async def back_to_access_management(message: types.Message, state: FSMContext):
    """Возврат к главному меню управления доступами"""
    await show_access_management_menu(message, state)
