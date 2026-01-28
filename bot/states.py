"""
FSM состояния для бота
"""
from aiogram.fsm.state import State, StatesGroup


class UploadStates(StatesGroup):
    """Состояния для загрузки файлов"""
    waiting_for_files = State()
    selecting_schema = State()


class SchemaStates(StatesGroup):
    """Состояния для работы со схемами"""
    # Создание
    creating_schema = State()
    waiting_schema_name = State()
    waiting_schema_files = State()
    
    # Управление
    managing_schema = State()
    
    # Обновление
    selecting_schema_to_update = State()
    waiting_update_files = State()
    
    # Удаление
    selecting_schema_to_delete = State()
    
    # Просмотр
    selecting_schema_to_view = State()
    viewing_schema_matches = State()
    
    # Редактирование
    selecting_schema_to_edit = State()
    waiting_edit_files = State()
    choosing_edit_action = State()          # НОВОЕ: Выбор действия после загрузки файлов
    entering_match_number = State()
    selecting_column_to_edit = State()
    selecting_new_column_value = State()
    
    # Добавление нового сопоставления
    adding_new_match = State()
    selecting_wb_column = State()
    selecting_ozon_column = State()
    selecting_yandex_column = State()

class AccessManagementStates(StatesGroup):
    """Состояния для управления доступами"""
    
    # Главное меню управления доступами
    main_menu = State()
    
    # Установка/изменение администратора
    waiting_for_admin_id = State()
    confirming_admin_change = State()
    
    # Удаление администратора
    confirming_admin_removal = State()
    
    # Управление белым списком (НОВОЕ)
    whitelist_menu = State()
    selecting_role = State()
    waiting_for_whitelist_user_id = State()
    confirming_whitelist_addition = State()
    selecting_user_to_remove = State()
    confirming_whitelist_removal = State()


