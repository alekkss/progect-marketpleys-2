"""
FSM состояния для бота
"""
from aiogram.fsm.state import State, StatesGroup


class UploadStates(StatesGroup):
    """Состояния для загрузки файлов (стандартная схема: 3 МП)"""
    waiting_for_files = State()
    selecting_schema = State()


class UploadMvmStates(StatesGroup):
    """
    Состояния для загрузки и обработки файлов по МВМ-схеме (3 МП + XML).

    Сценарий:
        1. Выбор МВМ-схемы
        2. Загрузка 3 файлов МП (WB, Ozon, Яндекс)
        3. Загрузка XML файла каталога (источник данных)
        4. Обработка и синхронизация
    """
    selecting_schema = State()
    waiting_for_mp_files = State()
    waiting_for_xml_file = State()


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
    waiting_edit_xml_file = State()       # Ожидание XML файла при редактировании МВМ-схемы
    choosing_edit_action = State()
    entering_match_number = State()
    selecting_column_to_edit = State()
    selecting_new_column_value = State()

    # Добавление нового сопоставления
    adding_new_match = State()
    selecting_wb_column = State()
    selecting_ozon_column = State()
    selecting_yandex_column = State()
    selecting_xml_column = State()        # Выбор XML-поля (4-й шаг для МВМ-схем)


class SchemaMvmStates(StatesGroup):
    """
    Состояния для создания схемы МВМ (3 МП + XML файл).

    Сценарий:
        1. Выбор типа схемы (стандартная / МВМ)
        2. Ввод названия схемы
        3. Загрузка 3 файлов маркетплейсов (WB, Ozon, Яндекс)
        4. Загрузка XML файла каталога
        5. Поиск и выбор категорий товаров из XML
        6. AI-сопоставление 4 источников
        7. Сохранение схемы
    """
    # Выбор типа создания схемы (общий для обоих сценариев)
    choosing_schema_type = State()

    # Ввод названия
    waiting_schema_name = State()

    # Загрузка 3 шаблонов МП
    waiting_mp_files = State()

    # Загрузка XML файла
    waiting_xml_file = State()

    # Поиск категорий: пользователь вводит текстовый запрос (например, "Холодильник")
    waiting_category_search = State()

    # Выбор категорий: пользователь отмечает нужные из найденного списка (inline-кнопки)
    waiting_category_selection = State()

    # Финализация (AI-сопоставление и сохранение)
    finalizing = State()


class SchemaUpdateMvmStates(StatesGroup):
    """
    Состояния для обновления МВМ-схемы (3 МП + XML).

    Сценарий:
        1. Выбор МВМ-схемы для обновления
        2. Загрузка 3 файлов МП
        3. Загрузка XML файла
        4. AI-пересопоставление несопоставленных столбцов
        5. Сохранение обновлённой схемы
    """
    selecting_schema = State()
    waiting_mp_files = State()
    waiting_xml_file = State()


class AccessManagementStates(StatesGroup):
    """Состояния для управления доступами"""

    # Главное меню управления доступами
    main_menu = State()

    # Установка/изменение администратора
    waiting_for_admin_id = State()
    confirming_admin_change = State()

    # Удаление администратора
    confirming_admin_removal = State()

    # Управление белым списком
    whitelist_menu = State()
    selecting_role = State()
    waiting_for_whitelist_user_id = State()
    confirming_whitelist_addition = State()
    selecting_user_to_remove = State()
    confirming_whitelist_removal = State()
