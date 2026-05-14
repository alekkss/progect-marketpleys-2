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
    get_whitelist_management_keyboard,
    get_whitelist_removal_confirmation_keyboard,
    get_role_selection_keyboard
)
from bot.states import AccessManagementStates
from bot.security import AccessManager
from bot import storage

router = Router()

async def show_access_management_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not await AccessManager.has_access_management_rights(user_id):
        await message.answer("⛔ Только владелец и администратор могут управлять доступами.")
        return

    admin_id = await AccessManager.get_admin_user_id()
    admin_info = f"👤 Текущий администратор: `{admin_id}`" if admin_id else "👤 Администратор не назначен"

    await state.set_state(AccessManagementStates.main_menu)
    await message.answer(
        f"🔐 Управление доступами\n\n{admin_info}",
        reply_markup=get_access_management_keyboard(),
        parse_mode="Markdown"
    )

async def show_current_admin(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not AccessManager.is_owner(user_id):
        await message.answer("⛔ Только владелец может просматривать администратора.")
        return

    admin_id = await AccessManager.get_admin_user_id()
    admin_text = f"👤 Администратор: `{admin_id}`" if admin_id else "👤 Администратор: не назначен"

    await message.answer(
        f"🔐 Текущие доступы:\n\n👑 Владелец: `{user_id}` (вы)\n{admin_text}",
        parse_mode="Markdown"
    )

async def start_admin_change(message: types.Message, state: FSMContext):
    if not AccessManager.is_owner(message.from_user.id):
        await message.answer("⛔ Только владелец может изменять администратора.")
        return
    await state.set_state(AccessManagementStates.waiting_for_admin_id)
    await message.answer(
        "✏️ Введите Telegram ID нового администратора:\n\n"
        "Чтобы узнать ID, попросите пользователя написать @userinfobot\n\n"
        "Или отправьте /cancel для отмены",
        reply_markup=get_cancel_keyboard()
    )

async def process_new_admin_id(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not AccessManager.is_owner(user_id):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.")
        return

    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Ошибка: ID должен быть числом.\n\nПопробуйте ещё раз или /cancel")
        return

    if new_admin_id == user_id:
        await message.answer("❌ Вы не можете назначить себя администратором (вы уже владелец).\n\nВведите другой ID или /cancel")
        return

    await state.update_data(new_admin_id=new_admin_id)
    await state.set_state(AccessManagementStates.confirming_admin_change)
    await message.answer(
        f"✅ Подтвердите назначение администратора:\n\n👤 Новый администратор: `{new_admin_id}`",
        reply_markup=get_admin_change_confirmation_keyboard(),
        parse_mode="Markdown"
    )

async def confirm_admin_change(message: types.Message, state: FSMContext):
    if not AccessManager.is_owner(message.from_user.id):
        await state.clear()
        await message.answer("У вас нет прав для этого действия.")
        return

    data = await state.get_data()
    new_admin_id = data.get('new_admin_id')
    if not new_admin_id:
        await message.answer("❌ Ошибка: ID нового администратора не найден.")
        await state.clear()
        return

    success = await AccessManager.set_admin(new_admin_id, message.from_user.id)
    await state.clear()

    if success:
        await message.answer(
            f"✅ *Администратор успешно назначен!*\n\nID: {new_admin_id}",
            reply_markup=get_main_menu_keyboard(show_access_management=await AccessManager.has_access_management_rights(message.from_user.id)),
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Не удалось назначить администратора.", reply_markup=get_main_menu_keyboard())

async def start_admin_removal(message: types.Message, state: FSMContext):
    if not AccessManager.is_owner(message.from_user.id):
        await message.answer("⛔ Только владелец может удалять администратора.")
        return
    admin_id = await AccessManager.get_admin_user_id()
    if not admin_id:
        await message.answer("ℹ️ Администратор не назначен.", reply_markup=get_access_management_keyboard())
        return

    await state.set_state(AccessManagementStates.confirming_admin_removal)
    await message.answer(
        f"⚠️ Удалить администратора?\n\n👤 Текущий: `{admin_id}`",
        reply_markup=get_admin_removal_confirmation_keyboard(),
        parse_mode="Markdown"
    )

async def confirm_admin_removal(message: types.Message, state: FSMContext):
    if not AccessManager.is_owner(message.from_user.id):
        await state.clear()
        await message.answer("У вас нет прав для этого действия.")
        return

    success = await AccessManager.remove_admin(message.from_user.id)
    await state.clear()

    if success:
        await message.answer("✅ Администратор успешно удалён.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("❌ Администратор не был назначен или ошибка БД.", reply_markup=get_main_menu_keyboard())

async def cancel_operation(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Операция отменена.", reply_markup=get_main_menu_keyboard())

# ============ WHITELIST ============

async def show_whitelist_menu(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await message.answer("⛔ Только владелец и администратор могут управлять белым списком.")
        return
    slots_info = await AccessManager.get_whitelist_slots_info()
    await state.set_state(AccessManagementStates.whitelist_menu)
    await message.answer(
        f"📋 *Управление белым списком*\n\n"
        f"✏️ Редакторы: {slots_info['editor']['used']}\n"
        f"👥 Пользователи: {slots_info['user']['used']}\n"
        f"📊 Всего: {slots_info['total_used']}",
        reply_markup=get_whitelist_management_keyboard(),
        parse_mode="Markdown"
    )

async def show_whitelist_users(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await message.answer("⛔ Нет прав на просмотр.")
        return
    users = await storage.db.get_whitelist_details()
    if not users:
        await message.answer("📋 Белый список пуст")
        return
    text = "📋 Пользователи в белом списке:\n\n"
    for i, user in enumerate(users, 1):
        role_emoji = "✏️" if user['role'] == 'editor' else "👤"
        role_text = "Редактор" if user['role'] == 'editor' else "Пользователь"
        text += f"{i}. {role_emoji} {role_text}\n   ID: `{user['user_id']}`\n   Заметка: {user['notes'] or 'нет'}\n   Добавлен: {user['added_at'][:10]}\n\n"
    await message.answer(text, parse_mode="Markdown")

async def start_whitelist_addition(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await message.answer("⛔ Нет прав на добавление.")
        return
    await state.set_state(AccessManagementStates.selecting_role)
    await message.answer(
        "🎯 *Выберите роль*\n\n"
        "*1️⃣ Редактор* — видит все схемы\n"
        "*2️⃣ Пользователь* — видит только свои",
        reply_markup=get_role_selection_keyboard(),
        parse_mode="Markdown"
    )

async def process_role_selection(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await state.clear()
        await message.answer("У вас нет прав для этого действия.")
        return
    if message.text == "❌ Отмена":
        await state.set_state(AccessManagementStates.whitelist_menu)
        await message.answer("❌ Операция отменена.", reply_markup=get_whitelist_management_keyboard())
        return

    selected_role = 'editor' if message.text == "1️⃣ Редактор" else 'user' if message.text == "2️⃣ Пользователь" else None
    if not selected_role:
        await message.answer("Пожалуйста, выберите роль.", reply_markup=get_role_selection_keyboard())
        return

    await state.update_data(selected_role=selected_role)
    await state.set_state(AccessManagementStates.waiting_for_whitelist_user_id)
    await message.answer(f"📝 *Добавление: {'Редактор' if selected_role == 'editor' else 'Пользователь'}*\n\nОтправьте Telegram ID:", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")

async def process_whitelist_user_id(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.")
        return
    data = await state.get_data()
    selected_role = data.get('selected_role', 'user')
    try:
        new_user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Ошибка: ID должен быть числом.")
        return
    if new_user_id == message.from_user.id:
        await message.answer("❌ Вы не можете добавить себя.")
        return

    success, error = await AccessManager.add_whitelist_user(new_user_id, message.from_user.id, role=selected_role)
    if not success:
        await message.answer(f"❌ Ошибка: {error}")
        return

    slots_info = await AccessManager.get_whitelist_slots_info()
    await state.set_state(AccessManagementStates.whitelist_menu)
    await message.answer(
        f"✅ Пользователь добавлен!\n👤 ID: `{new_user_id}`\n✏️ Ред: {slots_info['editor']['used']} | 👤 Польз: {slots_info['user']['used']}",
        reply_markup=get_whitelist_management_keyboard(),
        parse_mode="Markdown"
    )

async def start_whitelist_removal(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await message.answer("⛔ Нет прав на удаление.")
        return
    users = await storage.db.get_whitelist_details()
    if not users:
        await message.answer("ℹ️ Белый список пуст.", reply_markup=get_whitelist_management_keyboard())
        return
    text = "➖ Выберите ID для удаления:\n\n" + "\n".join(f"{i}. ID: `{u['user_id']}` ({u['notes'] or 'нет'})" for i, u in enumerate(users, 1))
    await state.set_state(AccessManagementStates.selecting_user_to_remove)
    await message.answer(text + "\n\nВведите ID или /cancel", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")

async def process_whitelist_removal_selection(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Доступ запрещён.")
        return
    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой ID.")
        return
    if not await AccessManager.is_in_whitelist(target_user_id):
        await message.answer(f"❌ Пользователь `{target_user_id}` не найден в списке.")
        return
    await state.update_data(target_user_id=target_user_id)
    await state.set_state(AccessManagementStates.confirming_whitelist_removal)
    await message.answer(f"⚠️ Удалить `{target_user_id}`?", reply_markup=get_whitelist_removal_confirmation_keyboard(), parse_mode="Markdown")

async def confirm_whitelist_removal(message: types.Message, state: FSMContext):
    if not await AccessManager.has_access_management_rights(message.from_user.id):
        await state.clear()
        await message.answer("У вас нет прав.")
        return
    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    if not target_user_id:
        await message.answer("❌ Ошибка: ID не найден.")
        await state.set_state(AccessManagementStates.whitelist_menu)
        return

    success, error = await AccessManager.remove_whitelist_user(target_user_id, message.from_user.id)
    if success:
        slots_info = await AccessManager.get_whitelist_slots_info()
        await state.set_state(AccessManagementStates.whitelist_menu)
        await message.answer(f"✅ Удалён `{target_user_id}`\n📊 Ред: {slots_info['editor']['used']} | Польз: {slots_info['user']['used']}", reply_markup=get_whitelist_management_keyboard(), parse_mode="Markdown")
    else:
        await state.set_state(AccessManagementStates.whitelist_menu)
        await message.answer(f"❌ {error}", reply_markup=get_whitelist_management_keyboard())

async def back_to_access_management(message: types.Message, state: FSMContext):
    await show_access_management_menu(message, state)

def register_access_management_handlers(dp):
    dp.message.register(show_access_management_menu, F.text == "🔐 Управление доступами")
    dp.message.register(show_current_admin, AccessManagementStates.main_menu, F.text == "👤 Текущий администратор")
    dp.message.register(start_admin_change, AccessManagementStates.main_menu, F.text == "✏️ Изменить администратора")
    dp.message.register(start_admin_removal, AccessManagementStates.main_menu, F.text == "🗑 Удалить администратора")
    dp.message.register(process_new_admin_id, AccessManagementStates.waiting_for_admin_id, F.text != "❌ Отмена")
    dp.message.register(confirm_admin_change, AccessManagementStates.confirming_admin_change, F.text == "✅ Подтвердить")
    dp.message.register(confirm_admin_removal, AccessManagementStates.confirming_admin_removal, F.text == "✅ Да, удалить")
    dp.message.register(cancel_operation, AccessManagementStates.waiting_for_admin_id, F.text == "❌ Отмена")
    dp.message.register(cancel_operation, AccessManagementStates.confirming_admin_change, F.text == "❌ Отмена")
    dp.message.register(cancel_operation, AccessManagementStates.confirming_admin_removal, F.text == "❌ Отмена")

    dp.message.register(show_whitelist_menu, AccessManagementStates.main_menu, F.text == "📋 Белый список пользователей")
    dp.message.register(show_whitelist_users, AccessManagementStates.whitelist_menu, F.text == "👥 Список пользователей")
    dp.message.register(start_whitelist_addition, AccessManagementStates.whitelist_menu, F.text == "➕ Добавить пользователя")
    dp.message.register(process_role_selection, AccessManagementStates.selecting_role)
    dp.message.register(process_whitelist_user_id, AccessManagementStates.waiting_for_whitelist_user_id, F.text != "❌ Отмена")
    dp.message.register(start_whitelist_removal, AccessManagementStates.whitelist_menu, F.text == "➖ Удалить пользователя")
    dp.message.register(process_whitelist_removal_selection, AccessManagementStates.selecting_user_to_remove, F.text != "❌ Отмена")
    dp.message.register(confirm_whitelist_removal, AccessManagementStates.confirming_whitelist_removal, F.text == "✅ Да, удалить из списка")
    dp.message.register(back_to_access_management, AccessManagementStates.whitelist_menu, F.text == "◀️ Назад к управлению доступами")
    dp.message.register(cancel_operation, AccessManagementStates.waiting_for_whitelist_user_id, F.text == "❌ Отмена")
    dp.message.register(cancel_operation, AccessManagementStates.selecting_user_to_remove, F.text == "❌ Отмена")
    dp.message.register(cancel_operation, AccessManagementStates.confirming_whitelist_removal, F.text == "❌ Отмена")