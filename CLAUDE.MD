# Назначение проекта

Telegram-бот для автоматической синхронизации данных товаров между тремя маркетплейсами: Wildberries, Ozon и Яндекс.Маркет. Система использует AI для интеллектуального сопоставления столбцов Excel-файлов и валидации значений с учётом справочников (validation lists).

## Архитектура

- **Bot Framework**: aiogram 3.x (async)
- **База данных**: PostgreSQL (asyncpg, connection pool)
- **Миграции**: отдельный модуль `database/migrations.py`, идемпотентные
- **AI Integration**: OpenRouter API (настраиваемые модели через .env)
- **File Processing**: pandas + openpyxl
- **State Management**: FSM (Finite State Machine), хранилище — MemoryStorage
- **Access Control**: Многоуровневая система прав доступа (async)

## Структура проекта

```
/
├── main.py                          # Точка входа приложения
├── .env                             # Конфигурация (API ключи, DATABASE_URL и др.)
│
├── /bot/
│   ├── bot.py                       # Инициализация бота и диспетчера, жизненный цикл
│   ├── storage.py                   # Глобальный экземпляр Database + init/shutdown
│   ├── security.py -> access.py     # Псевдоним для AccessManager
│   │
│   ├── /handlers/                   # Обработчики команд бота
│   │   ├── common.py                # Общие команды (/start, меню)
│   │   ├── upload.py                # Загрузка и обработка файлов (3 МП + МВМ)
│   │   ├── schema_create.py         # Создание схем (выбор типа + стандартный флоу)
│   │   ├── schema_create_mvm.py     # Создание МВМ-схем (3 МП + XML)
│   │   ├── schema_edit.py           # Редактирование схем (стандартные + МВМ)
│   │   ├── schema_update.py         # Обновление схем (стандартные + МВМ)
│   │   ├── schema_delete.py         # Удаление схем
│   │   ├── stats.py                 # Статистика пользователя
│   │   └── access_management.py     # Управление доступами (владелец/админ)
│   │
│   ├── /middlewares/
│   │   └── access_control.py        # Middleware проверки прав доступа (async)
│   │
│   ├── keyboards.py                 # Все клавиатуры бота
│   ├── states.py                    # FSM состояния
│   └── access.py                    # AccessManager - управление правами (async)
│
├── /services/
│   ├── data_synchronizer.py         # Синхронизация данных (3 МП + XML источник)
│   └── ai_comparator.py             # AI-сопоставление столбцов (3 МП + МВМ 4 источника)
│
├── /utils/
│   ├── excel_reader.py              # Чтение Excel файлов
│   ├── xml_reader.py                # Чтение XML файлов каталога (YML-фид)
│   ├── excel_writer.py              # Создание отчётов Excel
│   ├── logger_config.py             # Конфигурация логирования
│   └── utils.py                     # Вспомогательные функции
│
├── /database/
│   ├── database.py                  # PostgreSQL ORM (asyncpg, connection pool)
│   └── migrations.py                # Идемпотентные миграции таблиц
│
└── /config/
    └── config.py                    # Конфигурация (API keys, маппинги, DB/Redis)
```

## Жизненный цикл приложения

Порядок запуска в `bot/bot.py → start_bot()`:

1. **Валидация конфигурации** — `Config.validate()` бросает `ValueError` при отсутствии обязательных переменных (fail-fast)
2. **Инициализация БД** — `await init_database()` из `bot/storage.py`:
   - Создаёт экземпляр `Database(database_url, pool_min, pool_max)`
   - `await db.connect()` — открывает connection pool
   - `await run_migrations(db.pool)` — создаёт таблицы и применяет миграции
3. **Создание бота** — `create_bot()` регистрирует middleware и хендлеры
4. **Polling** — `await dp.start_polling(bot)`
5. **Graceful shutdown** — `await shutdown_database()` закрывает pool при остановке

## Система прав доступа

### Иерархия ролей

1. **👑 Владелец (Owner)** - 1 слот
   - ID задаётся в `.env` (`ACCESS_OWNER_ID`) — обязателен
   - Полный контроль над ботом
   - Управление администратором
   - Управление белым списком (whitelist)
   - Доступ ко всем схемам (своим и чужим)

2. **👨‍💼 Администратор (Admin)** - 1 слот (опционально)
   - Назначается владельцем через БД или `.env` (`ACCESS_ADMIN_ID`)
   - Приоритет: БД → `.env`
   - Управление белым списком пользователей
   - Доступ ко всем схемам
   - НЕ может управлять владельцем

3. **✏️ Редактор (Editor)** — в whitelist
   - Добавляется через меню управления доступами
   - Доступ ко всем схемам (просмотр и редактирование)
   - Создание/редактирование/удаление СВОИХ схем
   - Загрузка и обработка файлов
   - НЕ может управлять доступами других пользователей

4. **👤 Пользователь (User)** — в whitelist
   - Базовый доступ к боту
   - Видит ТОЛЬКО свои схемы
   - Создание/редактирование/удаление СВОИХ схем
   - Загрузка и обработка файлов
   - Просмотр статистики

### Whitelist

- **Лимиты**: отсутствуют — неограниченное количество редакторов и пользователей
- **Управление**: только владелец и администратор
- **Проверка прав**: `AccessControlMiddleware` на уровне dispatcher

### AccessManager (bot/security/access.py)

Все методы **асинхронные**. Единственное исключение — `is_owner()`, читает только `Config`.

**Кэширование данных доступа (TTL 60 сек):**
- `get_admin_user_id()` — кэшируется в `_TTLCache` с ключом `admin_id`
- `get_whitelist_users()` — кэшируется в `_TTLCache` с ключом `whitelist`
- `get_user_role(user_id)` — кэшируется в `_TTLCache` с ключом `role:{user_id}`
- **Инвалидация** при изменении whitelist/admin через `add_whitelist_user`, `remove_whitelist_user`, `set_admin`, `remove_admin`
- Это снижает нагрузку на PostgreSQL с ~2 запросов на сообщение до 0 при актуальном кэше

```python
# Асинхронные методы
await AccessManager.has_access(user_id)                    # Есть ли доступ к боту
await AccessManager.has_access_management_rights(user_id)  # Может ли управлять доступами
await AccessManager.is_admin(user_id)                      # Является ли администратором
await AccessManager.is_editor(user_id)                     # Является ли редактором
await AccessManager.can_see_all_schemas(user_id)           # Видит ли все схемы
await AccessManager.can_manage_schemas(user_id)            # Может ли управлять схемами

# Синхронный (только Config, без БД)
AccessManager.is_owner(user_id)
```

Импорт в хендлерах:
```python
from bot import storage          # Модуль, не переменная — для отложенной инициализации
from bot.security import AccessManager
```

## База данных

### Подключение

Используется `asyncpg` с connection pool. Экземпляр создаётся в `bot/storage.py`:

```python
db = Database(
    database_url=Config.DATABASE_URL,
    pool_min_size=Config.DATABASE_POOL_MIN_SIZE,
    pool_max_size=Config.DATABASE_POOL_MAX_SIZE,
)
await db.connect()
```

Все методы `Database` асинхронны и берут соединение из пула через `async with self.pool.acquire() as conn`.

### Таблицы

```sql
users                   # Пользователи бота
  - user_id (BIGINT PK)
  - username, first_name, last_name
  - registered_at (TIMESTAMPTZ)
  - total_processings

processing_history      # История обработок файлов
  - id (SERIAL PK)
  - user_id (FK)
  - started_at, completed_at (TIMESTAMPTZ)
  - wb_products_count, ozon_products_count, yandex_products_count
  - synced_cells_count
  - status, error_message

files                   # Загруженные файлы
  - id (SERIAL PK)
  - user_id (FK), processing_id (FK)
  - marketplace, original_filename, file_path
  - uploaded_at (TIMESTAMPTZ)

schemas                 # Схемы сопоставлений
  - id (SERIAL PK)
  - user_id (FK)
  - schema_name
  - schema_type TEXT DEFAULT 'standard'  -- 'standard' или 'mvm'
  - created_at, updated_at (TIMESTAMPTZ)
  - full_comparison_json TEXT            -- полный результат AI (основное хранилище)
  - UNIQUE(user_id, schema_name)

schema_matches          # Тройные совпадения (legacy, для совместимости)
  - id (SERIAL PK), schema_id (FK ON DELETE CASCADE)
  - wb_column, ozon_column, yandex_column
  - confidence REAL, is_mandatory BOOLEAN

system_settings         # Системные настройки
  - setting_key TEXT PK              -- например: 'admin_user_id'
  - setting_value TEXT
  - updated_at (TIMESTAMPTZ), updated_by BIGINT

whitelist_users         # Белый список (без лимитов)
  - id (SERIAL PK)
  - user_id BIGINT UNIQUE NOT NULL
  - role TEXT DEFAULT 'user'         -- 'editor' или 'user'
  - added_at (TIMESTAMPTZ), added_by BIGINT
  - notes TEXT
  - CONSTRAINT whitelist_role_check CHECK (role IN ('editor', 'user'))
```

### Миграции

Модуль `database/migrations.py`:
- **`CREATE_TABLES_SQL`** — создание всех таблиц (`IF NOT EXISTS`) + индексы
- **`MIGRATIONS`** — список `(название, SQL)` для инкрементальных изменений
- **`run_migrations(pool)`** — применяется при каждом старте (идемпотентно)

Каждая миграция обёрнута в `DO $$ BEGIN IF NOT EXISTS ... END $$` — безопасный повторный запуск.

Нумерация: `001_`, `002_`, ... — добавлять новые миграции ТОЛЬКО В КОНЕЦ списка.

### Ключевые особенности реализации

- **`RETURNING id`** вместо `lastrowid` для получения ID вставленной записи
- **`ON CONFLICT ... DO UPDATE`** вместо `INSERT OR REPLACE`
- **`asyncpg.UniqueViolationError`** вместо `sqlite3.IntegrityError`
- **`result == 'DELETE 1'`** для проверки успешного удаления (asyncpg возвращает строку вида `'DELETE N'`)
- **Транзакции**: `async with conn.transaction()` для атомарных операций (например, `complete_processing`)
- **`full_comparison_json`** — основное хранилище сопоставлений; `schema_matches` — legacy для обратной совместимости

## Стандарты кодирования

### Основные принципы

- **SOLID**: Разделение ответственности
  - `handlers` → взаимодействие с пользователем
  - `services` → бизнес-логика
  - `database` → работа с данными
  - `config` → конфигурация
- **Async/Await**: Все IO операции асинхронны (включая все обращения к БД)
- **Type Hints**: Обязательная типизация функций
- **Logging**: Структурированное логирование через `logger_config.py`

### Конвенции именования

- **Классы**: `PascalCase` (DataSynchronizer, AIComparator, AccessManager)
- **Функции**: `snake_case` (sync_three_columns, validate_with_ai)
- **Константы**: `UPPER_SNAKE_CASE` (FILE_CONFIGS, MANDATORY_MATCHES)
- **Private методы**: `_leading_underscore` (_validate_with_ai)

### Импорт db в хендлерах

```python
# ПРАВИЛЬНО — модуль, не переменная, для отложенной инициализации
from bot import storage
await storage.db.get_user_schemas(user_id)

# НЕПРАВИЛЬНО — переменная None на момент импорта
from bot.storage import db
```

### Обработка ошибок

```python
try:
    # Основная логика
except SpecificException as e:
    logger.error(f"Описание ошибки: {e}", exc_info=True)
    # Graceful degradation
```

## Ключевые компоненты

### 1. Схемы сопоставлений (Schema)

Поддерживаются два типа схем:

#### Стандартная схема (`schema_type = 'standard'`)
- Содержат:
  - **Тройные совпадения** (`matches_all_three`) — столбцы присутствуют во всех 3 файлах
  - **Парные совпадения**: `matches_1_2`, `matches_1_3`, `matches_2_3`
  - **Уникальные**: `only_in_first`, `only_in_second`, `only_in_third`
- **Confidence threshold**: 85% (совпадения ниже не сохраняются в `schema_matches`)
- **Формат**: полный JSON в поле `full_comparison_json` таблицы `schemas`

#### МВМ-схема (`schema_type = 'mvm'`) — v3.0

МВМ-схема расширяет стандартную, добавляя 4-й источник — XML-каталог (YML-фид МВидео/xway).

- **4 источника**: WB (column_1), Ozon (column_2), Яндекс (column_3), XML (column_4)
- **11 групп сопоставлений**:
  - `matches_all_four` — четверные (все 4 источника)
  - `matches_triple_1_2_3` — WB + Ozon + Яндекс (без XML)
  - `matches_triple_1_2_4` — WB + Ozon + XML
  - `matches_triple_1_3_4` — WB + Яндекс + XML
  - `matches_triple_2_3_4` — Ozon + Яндекс + XML
  - `matches_pair_1_2` — WB + Ozon
  - `matches_pair_1_3` — WB + Яндекс
  - `matches_pair_1_4` — WB + XML
  - `matches_pair_2_3` — Ozon + Яндекс
  - `matches_pair_2_4` — Ozon + XML
  - `matches_pair_3_4` — Яндекс + XML
- **XML как источник данных**: XML-файл не заполняется, а используется для наполнения МП-файлов
- **Привязка товаров**: XML vendorCode = Артикул продавца (WB) = Артикул* (Ozon) = Ваш SKU * (Яндекс)

#### XML-каталог (YML-фид)

Формат: Yandex Market Language (YML) — выгрузка из МВидео/xway.

Структура оффера:
- **Фиксированные теги**: name, vendor, vendorCode, barcode, description, price, oldprice, weight, dimensions, picture, video, url, categoryId, tnved, vat, count
- **Динамические параметры**: `<param name="...">значение</param>` — набор различается по категориям товаров
- **Категории**: иерархический справочник с parentId

Поля XML в схеме имеют префиксы:
- `[XML] тег` — для фиксированных тегов (например, `[XML] vendor`)
- `[XML param] Название` — для параметров (например, `[XML param] Цвет`)

### Работа с парными и тройными сопоставлениями (v2.1)

#### Просмотр сопоставлений
Функция `show_schema_matches` в `bot/handlers/schema_edit.py` отображает:
- **Тройные сопоставления** — все 3 маркетплейса (WB + Ozon + Яндекс)
- **Парные сопоставления**:
  - `matches_1_2` — WB + Ozon (Яндекс = N/A)
  - `matches_1_3` — WB + Яндекс (Ozon = N/A)
  - `matches_2_3` — Ozon + Яндекс (WB = N/A)

Формат вывода с явным указанием N/A для отсутствующих маркетплейсов.

#### Редактирование сопоставлений
Пользователь может:
1. **Редактировать любое сопоставление** (тройное или парное)
2. **Превращать парное в тройное** — заполнить N/A значением из шаблона
3. **Превращать тройное в парное** — ввести `NA` для удаления столбца

#### Логика автоперемещения между группами
При изменении столбца система автоматически определяет новый тип:
- Если заполнены все 3 столбца → перемещается в `matches_all_three`
- Если заполнены только 2 → перемещается в соответствующую парную группу
- Если остался 1 столбец → ошибка (минимум 2 маркетплейса)

**Вспомогательные функции** (`bot/handlers/schema_edit.py`):
- `_get_group_key(match_type, schema_type)` — получить ключ БД для типа сопоставления
- `_format_type(match_type, schema_type)` — форматирование типа для отображения пользователю
- `_build_columns_text(display_name, columns_list)` — формирует полный нумерованный список столбцов для безопасной отправки через `_send_long_text()` (защита от Telegram Flood control)

#### Фильтрация по типам (v2.1.1)

Функция `edit_action_selected` в `bot/handlers/schema_edit.py` обрабатывает выбор фильтра:
- **Кнопки фильтра**: `get_filter_matches_keyboard()` из `bot/keyboards.py`
- **Логика фильтрации**: Отображаются только сопоставления выбранного типа
- **Сохранение номеров**: Номера соответствуют индексам в `edit_all_matches`

**Типы фильтров:**
- `🎯 Показать тройные` → `filter_type = 'triple'`
- `🔗 Показать парные (WB+Ozon)` → `filter_type = 'pair_1_2'`
- `🔗 Показать парные (WB+Яндекс)` → `filter_type = 'pair_1_3'`
- `🔗 Показать парные (Ozon+Яндекс)` → `filter_type = 'pair_2_3'`
- `📋 Показать всё` → `filter_type = None`

### Работа с МВМ-сопоставлениями (v3.0)

#### Создание МВМ-схемы

1. Меню "📋 Управление схемами" → "➕ Создать схему"
2. Выбрать "📦 Создать схему МВМ"
3. Ввести название схемы
4. Загрузить 3 файла МП (WB, Ozon, Яндекс)
5. Загрузить XML файл каталога (как документ или по URL для файлов > 20 МБ)
6. Выбрать категории товаров из XML (поиск по названию → множественный выбор)
7. AI сопоставляет 4 источника (только поля из выбранных категорий)
8. Схема сохраняется с типом `mvm` (включая `selected_category_ids` в JSON)

**FSM-состояния**: `SchemaMvmStates` (choosing_schema_type → waiting_schema_name → waiting_mp_files → waiting_xml_file → waiting_category_search → waiting_category_selection → finalizing)

#### AI-сопоставление 4 источников

Метод `AIComparator.compare_columns_mvm()`:
1. **Шаг 1**: Стандартное сопоставление 3 МП через `compare_columns()`
2. **Шаг 2**: Отдельный AI-запрос для сопоставления XML-полей с МП (промпт `mvm_column_matching.txt`)
3. **Шаг 3**: Объединение результатов через `_merge_mvm_results()`:
   - Тройные 3 МП + XML-аналог → четверные (`matches_all_four`)
   - Тройные 3 МП без XML → `matches_triple_1_2_3`
   - Парные МП + XML → тройные с XML
   - Новые парные с XML → `matches_pair_1_4` / `matches_pair_2_4` / `matches_pair_3_4`
4. **Дедупликация**: `_deduplicate_mvm_matches()` — удаляет столбцы из нижних групп, если они уже в четверных

#### Обновление МВМ-схемы

1. Выбрать МВМ-схему → загрузить 3 МП + XML
2. AI пересопоставляет только несопоставленные столбцы из 4 источников
3. Новые совпадения (≥ 85%) добавляются в существующую схему

**FSM-состояния**: `SchemaUpdateMvmStates` (selecting_schema → waiting_mp_files → waiting_xml_file)

#### Обработка по МВМ-схеме (синхронизация + XML)

1. "📤 Загрузить файлы" → выбрать МВМ-схему (отмечена иконкой 📦)
2. Загрузить 3 файла МП + XML файл каталога
3. Синхронизация между МП (стандартная: габариты → тройные → парные)
4. **Заполнение из XML**: `DataSynchronizer._sync_from_xml()`:
   - Строит индекс XML по vendorCode: `_build_xml_article_map()`
   - **Проход 1**: Проходит по группам с `column_4` (7 групп) — заполняет пустые ячейки МП из XML
   - **Проход 2**: Добавление новых артикулов из XML: `_add_new_articles_from_xml()`
     - Находит артикулы из XML, которых нет ни в одном МП
     - Добавляет строку во все 3 МП-файла, заполняет из XML по сопоставлениям
   - Конвертация единиц (`_detect_unit` / `_convert_value`)
   - Валидация через AI (5-уровневая), логирование с `source_marketplace='xml'`

**FSM-состояния**: `UploadMvmStates` (selecting_schema → waiting_for_mp_files → waiting_for_xml_file)

### 2. Валидация значений (5 уровней)

1. **Точное совпадение** (case-sensitive)
2. **Нормализованное совпадение** (lowercase + strip)
3. **Извлечение чисел** из строк (regex `\d+`)
4. **Subset matching** (совпадение по словам)
5. **AI-сопоставление** через OpenRouter (финальный уровень)

### 3. Множественные значения

- **Разделитель входной**: `;` (точка с запятой)
- **WB**: только первое значение
- **Ozon**: выход через `"; "` (точка с запятой + пробел)
- **Яндекс**: выход через `", "` (запятая + пробел)

### 4. Специальная обработка

- **Габариты**: композитный формат `"длина/ширина/высота"` для Яндекса
- **Единицы измерения**: автоконвертация (мм ↔ см)
- **Артикулы**: выравнивание по всем маркетплейсам
- **Фильтрация**: Максимум 50 символов в артикуле

## Формат данных

### Обязательные столбцы (MANDATORY_MATCHES)

```python
{
    "column_1": "Артикул продавца",   # WB
    "column_2": "Артикул*",            # Ozon
    "column_3": "Ваш SKU *",           # Яндекс
    "description": "Уникальный артикул товара"
}
```

Всего 12 обязательных сопоставлений (артикул, баркод, бренд, название, описание, вес с упаковкой, вес без упаковки, фото, высота, длина, ширина, цвет).

### Исключённые столбцы

- Цена (все маркетплейсы)
- Rich-контент JSON
- Цена до скидки
- Старая цена
- SKU / SKU на Маркете / Артикул WB

## AI Integration

### Настройка модели (.env)

```env
OPENROUTER_API_KEY=sk-or-v1-xxx
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=google/gemini-2.5-flash-preview-09-2025
AI_TEMPERATURE=0.1
```

### Асинхронный клиент и ограничение нагрузки (v4.1)

- **Клиент**: `AsyncOpenAI` из библиотеки `openai` (вместо синхронного `OpenAI`)
- **Семафор**: `asyncio.Semaphore(5)` в `AIComparator` — максимум 5 параллельных AI-запросов к OpenRouter
- **HTTP-клиент**: `httpx.AsyncClient` для поддержки прокси
- **Преимущества**:
  - AI-запросы (5–30 сек) не блокируют event loop бота
  - Бот продолжает обрабатывать сообщения других пользователей во время ожидания ответа от OpenRouter
  - Семафор предотвращает перегрузку OpenRouter и исчерпание rate limits

### Асинхронная цепочка вызовов

Все методы, обращающиеся к AI, стали асинхронными:
- `AIComparator.compare_columns()` — `async def`
- `AIComparator.compare_columns_mvm()` — `async def`
- `AIComparator.match_value_with_list()` — `async def`
- `DataSynchronizer.synchronize_data()` — `async def`
- `DataSynchronizer._validate_with_ai()` — `async def`
- `DataSynchronizer._validate_multiple_values()` — `async def`

Хендлеры вызывают эти методы с `await`.

### Два прохода AI (стандартная схема)

1. **Первый проход**: Сопоставление всех столбцов трёх файлов
2. **Второй проход**: Оставшиеся столбцы после первого прохода (если нужно)

### МВМ-сопоставление (4 источника)

1. **Шаг 1**: Стандартные 2 прохода для 3 МП (через `compare_columns`)
2. **Шаг 2**: Отдельный AI-запрос для XML → МП (промпт `prompts/mvm_column_matching.txt`)
3. **Шаг 3**: Объединение и дедупликация результатов

### Логирование AI

- Все AI-валидации сохраняются в список `ai_validation_log`
- Экспортируются в отдельный лист Excel **"AI_Логи"**
- Формат: `[Артикул, Столбец, Исходное значение, Найденное значение, Confidence]`

## Конфигурация (.env)

```env
# Telegram Bot
TELEGRAM_BOT_TOKEN=xxx

# OpenRouter API
OPENROUTER_API_KEY=sk-or-v1-xxx
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=google/gemini-2.5-flash-preview-09-2025
AI_TEMPERATURE=0.1

# PostgreSQL — обязательно
DATABASE_URL=postgresql://user:password@localhost:5432/marketplace_sync
DATABASE_POOL_MIN_SIZE=2
DATABASE_POOL_MAX_SIZE=10

# Redis (зарезервировано для FSM-хранилища)
REDIS_URL=redis://localhost:6379/0

# Права доступа
ACCESS_OWNER_ID=123456789          # Обязательно! ID владельца
ACCESS_ADMIN_ID=987654321          # Опционально (можно через БД)

# Логирование
LOG_FILE_PATH=./logs/app.log
LOG_LEVEL=INFO

# Прокси (опционально)
PROXY_ENABLED=false
PROXY_URL=http://user:pass@host:port
```

`Config.validate()` бросает `ValueError` (не возвращает `False`) при отсутствии `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `DATABASE_URL` или `ACCESS_OWNER_ID`. Оборачивать в `try/except ValueError` в `main.py`.

## Тестирование

### Ручное тестирование

1. Загрузка 3 файлов (WB, Ozon, Яндекс)
2. Создание схемы сопоставления
3. Проверка синхронизации по артикулу
4. Валидация значений из справочников

### Debug режим

```python
logger.setLevel(logging.DEBUG)  # Подробные логи в utils/logger_config.py
```

## Deployment

### Требования

```
Python 3.10+
aiogram==3.x
asyncpg
pandas
openpyxl
openai (для OpenRouter)
python-dotenv
aiohttp (для загрузки XML по URL)
```

### Запуск

```bash
# Установка зависимостей
pip install -r requirements.txt

# PostgreSQL должен быть запущен и доступен по DATABASE_URL
# Миграции применяются автоматически при старте

# Запуск бота
python main.py
```

### Переменные окружения

Создайте файл `.env` с обязательными параметрами (см. раздел "Конфигурация").
Без `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY` и `ACCESS_OWNER_ID` бот не запустится.

## Roadmap & Known Issues

### TODO

- [x] Просмотр и редактирование парных сопоставлений (v2.1)
- [x] Автоматическое перемещение между группами при изменении типа (v2.1)
- [x] Фильтрация сопоставлений по типам (v2.1.1)
- [x] Поддержка МВМ-схем: 3 МП + XML каталог (v3.0)
- [x] Создание/редактирование/обновление МВМ-схем (v3.0)
- [x] Заполнение МП из XML каталога при обработке (v3.0)
- [x] AI-сопоставление 4 источников (v3.0)
- [x] Добавление новых артикулов из XML в МП при обработке (v3.1)
- [x] Добавление сопоставлений в МВМ-схемах + защита от Flood control (v3.2)
- [x] Миграция с SQLite на PostgreSQL + async архитектура (v4.0)
- [x] Кэширование данных доступа в AccessManager с TTL 60 сек (v4.1)
- [x] AsyncOpenAI + Semaphore(5) для неблокирующих AI-запросов (v4.1)
- [ ] Перевод FSM-хранилища с MemoryStorage на RedisStorage (REDIS_URL уже в конфиге)
- [ ] Добавление unit-тестов (pytest)
- [ ] CI/CD pipeline
- [ ] Поддержка 5-го маркетплейса (расширение архитектуры)

### Ограничения

- Максимум 50 символов в артикуле (фильтрация некорректных данных)
- МП-файлы только .xlsx формата
- XML-файлы только .xml формата (YML-фид)
- XML-файл должен содержать элементы `<offer>` с vendorCode
- XML-файлы > 20 МБ не скачиваются через Telegram API (нужна прямая ссылка)
- Скачивание по URL: лимит 200 МБ, таймаут 5 минут
- OpenRouter API rate limits
- FSM-состояния теряются при перезапуске бота (MemoryStorage)

## Важные предостережения

### НЕ изменять без обсуждения

- Формат обязательных столбцов (`MANDATORY_MATCHES`)
- Логику 5-уровневой валидации
- Разделители множественных значений (`;` → `"; "` / `", "`)
- Структуру прав доступа (роли и whitelist без лимитов)
- Префиксы XML-полей (`[XML]` и `[XML param]`)
- Порядок этапов синхронизации (габариты → МП → XML)
- Привязку XML по vendorCode
- Порядок инициализации: `Config.validate()` → `init_database()` → `create_bot()` → `polling`

### Критичные зависимости

- Порядок вызовов: схема → синхронизация → валидация
- Порядок синхронизации МВМ: загрузка → выравнивание → габариты → МП-столбцы → XML-заполнение (проход 1: существующие + проход 2: новые артикулы) → сохранение
- FSM states должны быть последовательными
- AI-логи обязательны для каждой валидации
- `AccessControlMiddleware` должен быть зарегистрирован ПЕРЕД handlers
- `ACCESS_OWNER_ID` в .env обязателен для работы системы доступа
- `register_schema_create_mvm_handlers` должен регистрироваться ПОСЛЕ `register_schema_create_handlers`
- XML-данные передаются в `DataSynchronizer` через параметры конструктора (`xml_offer_data`, `xml_categories`, `selected_category_ids`)
- `selected_category_ids` хранятся в `full_comparison_json` и извлекаются через `await db.get_schema_category_ids()`
- `from bot import storage` (модуль) — НЕ `from bot.storage import db` (переменная), иначе получишь `None` на момент импорта

## Типичные сценарии работы

### Добавление нового уровня валидации

1. Добавить метод в `DataSynchronizer` (`services/data_synchronizer.py`)
2. Вставить в цепочку валидации перед AI
3. Обновить формат AI_Логов в `excel_writer.py`

### Добавление нового маркетплейса

1. Расширить `FILE_CONFIGS` в `config/config.py`
2. Добавить `MANDATORY_MATCHES` для нового файла
3. Обновить логику `sync_three_columns` → `sync_four_columns`
4. Адаптировать `AIComparator` для 4 файлов

### Добавление новой миграции БД

1. Добавить кортеж `("003_название", "SQL...")` В КОНЕЦ списка `MIGRATIONS` в `database/migrations.py`
2. SQL должен быть идемпотентным (`IF NOT EXISTS` или `DO $$ IF NOT EXISTS ... END $$`)
3. Миграция применится автоматически при следующем старте

### Работа с МВМ-схемами (3 МП + XML каталог)

#### Создание МВМ-схемы
1. Меню "📋 Управление схемами" → "➕ Создать схему"
2. Выбрать "📦 Создать схему МВМ"
3. Ввести название
4. Загрузить 3 файла МП
5. Загрузить XML файл каталога (или прямую ссылку для файлов > 20 МБ)
6. Ввести название категории для поиска (например, "Холодильник")
7. Выбрать одну или несколько категорий из найденных (inline-кнопки)
8. Дождаться AI-сопоставления 4 источников (только поля выбранных категорий)

#### Выбор категорий XML (v3.1)

После загрузки XML файла бот предлагает выбрать категории товаров.

**Сценарий:**
1. Бот показывает статистику XML (офферов, полей, категорий)
2. Пользователь вводит текст для поиска (например, "Холодильник")
3. Бот ищет по подстроке без учёта регистра среди всех категорий XML
4. Найденные категории отображаются как inline-кнопки с toggle-выбором
5. Можно выбрать несколько категорий, искать повторно, добавлять к выбору
6. После подтверждения — переход к AI-сопоставлению

**Хранение в схеме**: `selected_category_ids` сохраняется в `full_comparison_json`:
```json
{
    "matches_all_four": [...],
    "selected_category_ids": ["16530", "16531"]
}
```

#### Загрузка больших XML файлов (> 20 МБ)
Telegram Bot API ограничивает скачивание файлов до 20 МБ.

**Вспомогательные функции** (`bot/utils.py`):
- `download_xml_from_telegram(bot, message, user_id)` — скачивание через Telegram API с обработкой ошибки "file too big"
- `download_file_by_url(url, user_id, filename)` — скачивание по HTTP/HTTPS ссылке (лимит 200 МБ)

Работает в обоих флоу: создание МВМ-схемы (`schema_create_mvm.py`) и обработка (`upload.py`).

### Управление пользователями

#### Добавление администратора (владелец)

1. Меню "🔐 Управление доступами"
2. "✏️ Изменить администратора"
3. Ввести Telegram ID

#### Добавление пользователя в whitelist (владелец/админ)

1. Меню "🔐 Управление доступами"
2. "📋 Белый список пользователей"
3. "➕ Добавить пользователя"
4. Выбрать роль (Редактор/Пользователь)
5. Ввести Telegram ID

#### Удаление из whitelist

1. Меню "🔐 Управление доступами"
2. "📋 Белый список пользователей"
3. "➖ Удалить пользователя"
4. Ввести ID пользователя

## Middleware и безопасность

### AccessControlMiddleware

Регистрируется для всех `message` и `callback_query` событий:

```python
dp.message.middleware(AccessControlMiddleware())
dp.callback_query.middleware(AccessControlMiddleware())
```

- Проверяет `user_id` перед каждым обработчиком через `await AccessManager.has_access(user_id)`
- Блокирует неавторизованных пользователей
- Отправляет сообщение "⛔ У вас нет доступа"

### Иерархия проверок

1. Middleware → `await AccessManager.has_access(user_id)`
2. Handler → дополнительные проверки через `await AccessManager.is_owner()`, `is_admin()`, etc.
3. Database → ограничения на уровне SQL (`UNIQUE`, `CHECK` constraints)

## Логирование

### Конфигурация

Модуль `utils/logger_config.py` настраивает:
- Формат: `[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s`
- Ротация файлов (если настроено через `LOG_FILE_PATH`)
- Вывод в консоль + файл

Уровень логирования задаётся через `LOG_LEVEL` в `.env`.

### Использование

```python
from utils.logger_config import setup_logger
logger = setup_logger('module_name')

logger.info("Информационное сообщение")
logger.error("Ошибка", exc_info=True)
logger.debug("Детальная информация для отладки")
```

## Обработка файлов Excel

### Чтение (utils/excel_reader.py)

```python
from utils.excel_reader import ExcelReader
reader = ExcelReader()
columns = reader.get_column_names(filepath, sheet_name, header_row)
```

### Запись (utils/excel_writer.py)

- Создаёт 3 листа: WB, Ozon, Яндекс
- Добавляет лист "AI_Логи" с детализацией всех AI-валидаций
- Форматирование (заливка заголовков, выравнивание)
- Auto-width для колонок

## FAQ

### Как узнать Telegram ID пользователя?

Попросить пользователя написать боту `@userinfobot` и прислать полученный ID.

### Где хранится администратор?

Приоритет: БД (`system_settings` таблица) → `.env` (`ACCESS_ADMIN_ID`).

### Как удалить столбец из тройного сопоставления?

При редактировании сопоставления введи `NA` (без учета регистра) в поле столбца, который хочешь удалить. Сопоставление автоматически станет парным.

### Что произойдет, если ввести NA для единственного заполненного столбца?

Система выдаст ошибку: "❌ Сопоставление должно содержать минимум 2 источника!". Нельзя создать сопоставление с одним столбцом.

### Чем отличается стандартная схема от МВМ?

Стандартная схема работает с 3 МП (WB + Ozon + Яндекс) и имеет 4 группы сопоставлений. МВМ-схема добавляет 4-й источник — XML-каталог — и имеет 11 групп сопоставлений. При обработке по МВМ-схеме пустые ячейки МП заполняются данными из XML.

### Как XML используется при обработке?

XML — это источник данных (донор). Он НЕ заполняется. Данные из XML переносятся в пустые ячейки МП-файлов по сопоставлениям схемы. Привязка товаров — по vendorCode (артикулу).

### Можно ли конвертировать стандартную схему в МВМ?

Нет, тип схемы задаётся при создании. Нужно создать новую МВМ-схему.

### Что если XML файл слишком большой для Telegram?

Telegram Bot API ограничивает скачивание файлов до 20 МБ. Если XML больше — бот предложит отправить прямую HTTP/HTTPS ссылку на файл. Загрузи XML на Google Drive, Dropbox или Яндекс.Диск, скопируй прямую ссылку и отправь боту.

### Почему `from bot.storage import db` не работает?

На момент импорта модуля переменная `db = None`. Нужно импортировать модуль целиком: `from bot import storage`, затем использовать `storage.db`. Это гарантирует, что обращение к `db` происходит уже после `await init_database()`.

---

**Версия документации**: 4.1
**Дата обновления**: Май 2026
**Последние изменения**:
- v4.1: AccessManager — in-memory кэш с TTL 60 сек (admin_id, whitelist, user_roles) с инвалидацией при мутациях. AIComparator — миграция на AsyncOpenAI с asyncio.Semaphore(5) для ограничения параллельных запросов. DataSynchronizer — полная асинхронизация цепочки AI-валидации.
- v4.0: Миграция с SQLite на PostgreSQL (asyncpg, connection pool), полностью асинхронная архитектура, async AccessManager, отдельный модуль миграций, новые переменные конфигурации (DATABASE_URL, REDIS_URL, LOG_FILE_PATH, LOG_LEVEL)
- v3.2: Добавление сопоставлений в МВМ-схемах (кнопка "➕ Добавить сопоставление"), защита от Telegram Flood control при отображении списков столбцов
- v3.1: Автоматическое добавление новых артикулов из XML в МП-файлы при обработке по МВМ-схеме
- v3.0: Поддержка МВМ-схем (3 МП + XML каталог): создание, редактирование, обновление, обработка с заполнением из XML
- v2.1.1: Добавлена фильтрация сопоставлений по типам (тройные/парные) для удобного просмотра больших схем
- v2.1: Поддержка просмотра и редактирования парных сопоставлений с автоматическим перемещением между группами
**Автор проекта**: Александр (@your_username)
