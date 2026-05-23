# Назначение проекта

Telegram-бот для автоматической синхронизации данных товаров между тремя маркетплейсами: Wildberries, Ozon и Яндекс.Маркет. Система использует AI для интеллектуального сопоставления столбцов Excel-файлов и валидации значений с учётом справочников (validation lists).

## Архитектура

- **Bot Framework**: aiogram 3.x (async)
- **База данных**: PostgreSQL (asyncpg, connection pool)
- **Миграции**: отдельный модуль `database/migrations.py`, идемпотентные
- **AI Integration**: OpenRouter API (настраиваемые модели через .env)
- **File Processing**: pandas + openpyxl
- **State Management**: FSM (Finite State Machine), хранилище — RedisStorage (с fallback на MemoryStorage при недоступности Redis)
- **Access Control**: Многоуровневая система прав доступа (async)
- **Task Queue**: Фоновая очередь задач на Redis List (с fallback на in-memory Queue).
  TaskWorker с asyncio.Semaphore(MAX_CONCURRENT_TASKS) ограничивает параллельные
  тяжёлые обработки (pandas + AI), предотвращая перегрузку CPU.
  `AIComparator` создаётся **один раз** при инициализации `TaskWorker` и
  переиспользуется для всех задач — промпты читаются с диска только при старте.

## Структура проекта

```
/
├── main.py                          # Точка входа приложения
├── .env                             # Конфигурация (API ключи, DATABASE_URL и др.)
│
├── /bot/
│   ├── bot.py                       # Инициализация бота и диспетчера, жизненный цикл
│   ├── storage.py                   # Глобальный экземпляр Database + SessionStorage + init/shutdown
│   ├── session_storage.py           # Абстракция над Redis (TTL 30 мин) с in-memory fallback
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
│   ├── data_synchronizer.py         # Оркестратор синхронизации (Facade)
│   ├── ai_comparator.py             # AI-сопоставление столбцов (3 МП + МВМ 4 источника)
│   ├── task_queue.py                # Очередь задач: Task (dataclass), TaskQueue (Repository),
│   │                                # RedisTaskQueue, InMemoryTaskQueue, фабрика create_task_queue()
│   ├── task_worker.py               # Фоновый воркер: читает очередь, обрабатывает Task через
│   │                                # DataSynchronizer, отправляет результат пользователю через Bot
│   └── /sync/                       # Подпакет компонентов синхронизации (v4.2)
│       ├── __init__.py              # Экспорты подпакета
│       ├── value_converter.py       # ValueConverter — единицы измерения и конвертация
│       ├── ai_validator.py          # AiValidator — 5-уровневая валидация через AI
│       ├── article_aligner.py       # ArticleAligner — выравнивание артикулов
│       ├── dimensions_synchronizer.py # DimensionsSynchronizer — синхронизация габаритов
│       ├── column_syncer.py         # ColumnSyncer — синхронизация столбцов МП
│       ├── xml_syncer.py            # XmlSyncer — заполнение МП из XML-каталога
│       └── excel_io.py              # ExcelFileManager — загрузка и сохранение xlsx
│
├── /utils/
│   ├── excel_reader.py              # Чтение Excel файлов
│   ├── xml_reader.py                # Чтение XML файлов каталога (YML-фид)
│   ├── excel_writer.py              # Создание отчётов Excel
│   ├── logger_config.py             # Конфигурация логирования
│   └── utils.py                     # Вспомогательные функции (download_file_by_url и др.)
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

1. **Валидация конфигурации** — `Config.validate()` бросает `ValueError`...
2. **Инициализация хранилищ** — `await init_storage()` из `bot/storage.py`:
   - Создаёт экземпляр `Database(...)`
   - `await db.connect()` — открывает connection pool PostgreSQL
   - `await run_migrations(db.pool)` — создаёт таблицы
   - `await session_storage.connect()` — подключает Redis для сессий
3. **Инициализация очереди задач** — `await task_queue.connect()`:
   - RedisTaskQueue (основная) или InMemoryTaskQueue (fallback)
   - Хранит задачи в Redis List + Hash, индекс по пользователю
4. **Создание бота** — `create_bot(task_queue)` регистрирует middleware и хендлеры.
   Хендлеры upload.py получают `task_queue` через dependency injection.
5. **Запуск фонового воркера** — `await task_worker.start(bot)`:
   - `AIComparator` создаётся один раз в `TaskWorker.__init__` — промпты читаются здесь
   - Запускает цикл `while running: task = await queue.dequeue()`
   - Ограничивает обработку `asyncio.Semaphore(Config.MAX_CONCURRENT_TASKS)`
   - Семафор AIComparator становится глобальным — максимум 5 AI-запросов суммарно по всем задачам
   - Worker сам отправляет результаты через `Bot.send_message/document`
6. **Polling** — `await dp.start_polling(bot)`
7. **Graceful shutdown** — строгий порядок освобождения ресурсов:
   - `await task_worker.stop()` — ждёт завершения активных задач, закрывает `AIComparator` (`await comparator.close()`)
   - `await task_queue.disconnect()` — закрывает соединение с Redis
   - `await shutdown_storage()` — закрывает pool PostgreSQL

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

# SessionStorage — для временных данных FSM-сессий
session_storage = SessionStorage(redis_url=Config.REDIS_URL)
await session_storage.connect()  # Redis или in-memory fallback
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

Нумерация: `001_`, `002_`, `003_`, ... — добавлять новые миграции ТОЛЬКО В КОНЕЦ списка.
**ВАЖНО**: нумерация должна быть уникальной и последовательной. Дублирующийся номер
(например, две миграции `001_`) не вызовет ошибку сейчас, но сломает любой механизм
отслеживания применённых миграций в будущем.

Текущие миграции:
- `001_add_role_column` — добавляет столбец `role` в `whitelist_users`
- `002_add_schema_type_column` — добавляет столбец `schema_type` в `schemas`

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
  - `services/sync` → компоненты синхронизации (каждый за одну задачу)
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

### 1. DataSynchronizer — оркестратор (v4.2)

`services/data_synchronizer.py` — **Facade**-оркестратор. Создаёт компоненты подпакета `sync/`, передаёт им разделяемое состояние и вызывает в строго определённом порядке.

**Публичный интерфейс не изменился** — все хендлеры продолжают работать без правок.

```python
from services.data_synchronizer import DataSynchronizer

synchronizer = DataSynchronizer(comparison_result, ai_comparator=comparator)
synced_dfs, changes_log = await synchronizer.synchronize_data(file_paths, output_paths)

# AI-лог записывается ПОСЛЕ создания отчёта ExcelWriter'ом
writer.create_report_with_changes(comparison_result, changes_log, report_path)
synchronizer.create_ai_log_in_report(report_path)
```

**CPU-операции и event loop (v4.3):**
Тяжёлые синхронные шаги выносятся в поток через `asyncio.to_thread()` —
event loop остаётся свободным для других пользователей во время обработки:
- Загрузка файлов (`load_all_dataframes`) — openpyxl + pandas
- Построение XML-индекса (`build_index`) — итерация по офферам
- Выравнивание артикулов (`article_aligner.align`) — pandas concat
- Синхронизация габаритов (`sync_dimensions`) — шесть проходов по DataFrame
- Габариты из XML (`sync_dimensions_from_xml`) — парсинг + запись

Три метода остаются в event loop — их нельзя перенести в поток,
так как внутри есть `await` AI-запросов:
`sync_all_matches`, `sync_from_xml`, `save_results`.

**КРИТИЧЕСКИ ВАЖНО — порядок записи AI-лога:**
1. `synchronizer.synchronize_data(...)` — синхронизация и сохранение МП-файлов
2. `writer.create_report_with_changes(...)` — ExcelWriter создаёт файл отчёта с нуля
3. `synchronizer.create_ai_log_in_report(report_path)` — добавляет лист «AI_Логи» в уже готовый файл

Если вызвать `create_ai_log_in_report` до `create_report_with_changes`, ExcelWriter перезапишет файл и лог будет потерян.

#### Порядок этапов внутри synchronize_data

1. Загрузка DataFrame и validation-списков (`ExcelFileManager`)
2. Построение XML-индекса по vendorCode (`XmlSyncer.build_index`)
3. Выравнивание артикулов между МП — включая артикулы из XML (`ArticleAligner.align`)
4. Синхронизация габаритов (`DimensionsSynchronizer.sync_dimensions`)
5. Синхронизация остальных столбцов по схеме (`ColumnSyncer.sync_all_matches`)
6. [МВМ] Заполнение МП из XML-каталога (`XmlSyncer.sync_from_xml` + `sync_dimensions_from_xml`)
7. Сохранение МП-файлов (`ExcelFileManager.save_results`)

#### Разделяемое состояние

Передаётся по ссылке во все компоненты — изменения накапливаются в одном месте:
- `synchronizer.changes_log` — лог всех изменений ячеек по маркетплейсам
- `synchronizer.ai_validation_log` — лог всех AI-валидаций (используется для листа «AI_Логи»)

### 2. Подпакет services/sync/ (v4.2)

Семь специализированных классов, каждый отвечает за одну задачу. Все зависимости инжектируются через конструктор.

#### ValueConverter (`services/sync/value_converter.py`)

Определяет единицу измерения по названию столбца и конвертирует числовые значения.

```python
converter = ValueConverter()
unit = converter.detect_unit("Длина упаковки, мм*")  # → 'mm'
value = converter.convert_value(150, 'mm', 'cm')      # → 15.0
```

Порядок определения единицы: XML_UNIT_MAPPING → ALL_WEIGHT_COLUMN_NAMES → эвристика по ключевым словам.

#### AiValidator (`services/sync/ai_validator.py`)

5-уровневая валидация значений против списков допустимых значений. Принимает `column_validations` и `ai_validation_log` по ссылке.

```python
validator = AiValidator(ai_comparator, column_validations, ai_validation_log)
result = await validator.validate_multiple_values(value, 'wildberries', 'Цвет')
```

Каскад проверок (от быстрых к медленным):
1. Точное совпадение
2. Нормализация (lowercase + ё→е)
3. Извлечение числа из строки (`\d+`)
4. Subset matching по словам
5. AI-запрос через AIComparator (только если все предыдущие не сработали)

#### ArticleAligner (`services/sync/article_aligner.py`)

Выравнивает артикулы между МП (добавляет недостающие строки) и строит article_map для быстрого поиска.

```python
aligner = ArticleAligner(article_columns, xml_article_map)
dfs = aligner.align(dfs)
article_map = aligner.create_article_map(df, article_col, value_col)
```

Фильтрация некорректных артикулов: служебные строки-описания и строки длиннее 50 символов автоматически исключаются.

#### DimensionsSynchronizer (`services/sync/dimensions_synchronizer.py`)

Синхронизирует габариты упаковки между всеми МП с учётом форматов:
- WB: раздельные столбцы в сантиметрах (имена определяются динамически через паттерны)
- Ozon: раздельные столбцы в миллиметрах
- Яндекс: составной столбец «длина/ширина/высота» в сантиметрах

```python
synced_count, resolved_wb_dims = DimensionsSynchronizer.sync_dimensions(dfs)
```

**Важно:** `sync_dimensions` возвращает кортеж `(int, Optional[Dict])`. `resolved_wb_dims` — словарь с реальными именами столбцов WB (`{'length': '...', 'width': '...', 'height': '...'}`). Этот словарь передаётся оркестратором в `ColumnSyncer` и `XmlSyncer`, чтобы не определять имена столбцов повторно.

#### ColumnSyncer (`services/sync/column_syncer.py`)

Синхронизирует тройные и парные совпадения между МП по схеме сопоставлений.

```python
syncer = ColumnSyncer(
    comparison_result, article_columns,
    value_converter, ai_validator, article_aligner,
    changes_log, resolved_wb_dims
)
synced_dfs = await syncer.sync_all_matches(dfs)
```

Пары МП описаны декларативно в константе `_PAIR_CONFIGS` — добавить новую пару значит дописать одну строку в список. Габаритные столбцы и исключённые столбцы пропускаются автоматически.

#### XmlSyncer (`services/sync/xml_syncer.py`)

Строит индекс XML-офферов по vendorCode и заполняет пустые ячейки МП данными из XML.

```python
syncer = XmlSyncer(
    comparison_result, article_columns,
    value_converter, ai_validator, changes_log,
    resolved_wb_dims, xml_offer_data, xml_categories, selected_category_ids
)
syncer.build_index()                           # строит xml_article_map
filled = await syncer.sync_from_xml(dfs)       # обычные поля
dims   = syncer.sync_dimensions_from_xml(dfs)  # габариты из [XML] dimensions
```

Поле `[XML] dimensions` обрабатывается отдельно в `sync_dimensions_from_xml` — оно требует парсинга строки «д/ш/в» и конвертации единиц для каждого МП.

#### ExcelFileManager (`services/sync/excel_io.py`)

Загружает xlsx через openpyxl (с сохранением data validation и стилей), сохраняет результаты обратно в файлы, записывает лист «AI_Логи».

```python
manager = ExcelFileManager(ai_comparator=comparator)
dfs = manager.load_all_dataframes(file_paths)
# После загрузки доступны:
# manager.column_validations    — {маркетплейс: {столбец: [значения]}}
# manager.original_column_names — маппинг переименованных дубликатов
# manager.original_file_paths   — пути к исходным файлам

await manager.save_results(dfs, output_paths, ai_validation_log)
manager.create_ai_log_in_report(report_path, ai_validation_log)
```

Обрабатывает дублирующиеся заголовки: добавляет числовой суффикс при загрузке («Вес (кг)1») и восстанавливает оригинальные имена при сохранении.

**Важно — validation-списки при сохранении (v4.6):**
`save_results` использует `self.column_validations` (заполняется при `load_all_dataframes`)
для проверки допустимых значений ячеек. Это O(1) dict-lookup вместо повторного
сканирования всех DV-правил листа для каждой ячейки. `_get_validation_list_values`
используется только внутри `_load_column_validations` — не вызывать в цикле по ячейкам.

### 3. Схемы сопоставлений (Schema)

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

### 4. Работа с парными и тройными сопоставлениями (v2.1)

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

### 5. Работа с МВМ-сопоставлениями (v3.0)

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
4. **Заполнение из XML** (`XmlSyncer`):
   - Строит индекс XML по vendorCode: `build_index()`
   - **Проход 1**: `sync_from_xml()` — проходит по группам с `column_4` (7 групп), заполняет пустые ячейки МП из XML
   - **Проход 2**: Новые артикулы из XML добавляются в МП через `ArticleAligner.align()` ещё до синхронизации
   - `sync_dimensions_from_xml()` — конвертирует и записывает габариты из `[XML] dimensions`
   - Конвертация единиц (`ValueConverter`)
   - Валидация через AI (5-уровневая), логирование с `source_marketplace='xml'`

**FSM-состояния**: `UploadMvmStates` (selecting_schema → waiting_for_mp_files → waiting_for_xml_file)

### 6. Валидация значений (5 уровней)

Реализована в `AiValidator` (`services/sync/ai_validator.py`):

1. **Точное совпадение** (case-sensitive)
2. **Нормализованное совпадение** (lowercase + ё→е)
3. **Извлечение чисел** из строк (regex `\d+`)
4. **Subset matching** (совпадение по словам)
5. **AI-сопоставление** через OpenRouter (финальный уровень)

### 7. Множественные значения

- **Разделитель входной**: `;` (точка с запятой)
- **WB**: только первое значение (кроме столбцов из `WB_MULTI_VALUE_COLUMNS`)
- **WB (Фото, Видео и др.)**: все значения через `";"`
- **Ozon**: все значения через `";"`
- **Яндекс**: все значения через `","`

### 8. Специальная обработка

- **Габариты**: композитный формат `"длина/ширина/высота"` для Яндекса
- **Единицы измерения**: автоконвертация (мм ↔ см, г ↔ кг) через `ValueConverter`
- **Артикулы**: выравнивание по всем маркетплейсам через `ArticleAligner`
- **Фильтрация**: максимум 50 символов в артикуле

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

### Асинхронный клиент и ограничение нагрузки (v4.1, обновлено v4.6)

- **Клиент**: `AsyncOpenAI` из библиотеки `openai` (вместо синхронного `OpenAI`)
- **Семафор**: `asyncio.Semaphore(5)` в `AIComparator` — максимум 5 параллельных AI-запросов к OpenRouter.
  С v4.6 семафор стал **глобальным** — `AIComparator` создаётся один раз в `TaskWorker`,
  семафор ограничивает 5 AI-запросов суммарно по всем задачам (раньше каждая задача
  имела свой семафор и могла запускать до 5 запросов независимо).
- **HTTP-клиент**: `httpx.AsyncClient` для поддержки прокси — хранится в `self._http_client`,
  корректно закрывается через `await comparator.close()` при остановке воркера.
- **Метод `close()`**: освобождает HTTP-соединения. Вызывается в `TaskWorker.stop()`
  после завершения всех активных задач.
- **Преимущества**:
  - AI-запросы (5–30 сек) не блокируют event loop бота
  - Бот продолжает обрабатывать сообщения других пользователей во время ожидания ответа от OpenRouter
  - Семафор предотвращает перегрузку OpenRouter и исчерпание rate limits

### Асинхронная цепочка вызовов

Все методы, обращающиеся к AI, асинхронны:
- `AIComparator.compare_columns()` — `async def`
- `AIComparator.compare_columns_mvm()` — `async def`
- `AIComparator.match_value_with_list()` — `async def`
- `DataSynchronizer.synchronize_data()` — `async def`
- `AiValidator.validate_multiple_values()` — `async def`
- `AiValidator._validate_with_ai()` — `async def`

Хендлеры вызывают эти методы с `await`.

### Два прохода AI (стандартная схема)

1. **Первый проход**: Сопоставление всех столбцов трёх файлов
2. **Второй проход**: Оставшиеся столбцы после первого прохода (если нужно)

### МВМ-сопоставление (4 источника)

1. **Шаг 1**: Стандартные 2 прохода для 3 МП (через `compare_columns`)
2. **Шаг 2**: Отдельный AI-запрос для XML → МП (промпт `prompts/mvm_column_matching.txt`)
3. **Шаг 3**: Объединение и дедупликация результатов

### Логирование AI

- Все AI-валидации накапливаются в `synchronizer.ai_validation_log`
- Записываются в лист **«AI_Логи»** через `synchronizer.create_ai_log_in_report(report_path)`
- Формат: `[Маркетплейс, Столбец, Исходное значение, Сопоставлено с, Метод]`

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

# Redis — хранилище FSM-состояний и сессий загрузки (опционально, есть fallback)
REDIS_URL=redis://localhost:6379/0
# Если не задан — бот работает на MemoryStorage (FSM теряются при перезапуске)

# Права доступа
ACCESS_OWNER_ID=123456789          # Обязательно! ID владельца
ACCESS_ADMIN_ID=987654321          # Опционально (можно через БД)

# Логирование
LOG_FILE_PATH=./logs/app.log
LOG_LEVEL=INFO

# Очередь задач (опционально)
MAX_CONCURRENT_TASKS=5          # Максимум одновременных обработок (Semaphore)
TASK_QUEUE_KEY=bot:task_queue  # Ключ Redis List для хранения ID задач

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
httpx (для прокси в AIComparator)
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
- [x] Рефакторинг DataSynchronizer: подпакет services/sync/ с 7 компонентами (v4.2)
- [x] asyncio.to_thread() для CPU-тяжёлых операций pandas/openpyxl (v4.3)
- [x] Перевод FSM-хранилища с MemoryStorage на RedisStorage с fallback (v4.4)
- [x] Устранение утечки памяти: глобальные dict user_files/user_schemas заменены на SessionStorage с TTL (v4.4)
- [x] Фоновая очередь задач с ограничением параллельных обработок (v4.5)
- [x] AIComparator singleton в TaskWorker — промпты читаются один раз при старте (v4.6)
- [x] Устранение утечки httpx.AsyncClient в AIComparator (v4.6)
- [x] Оптимизация save_results: validation-списки из кэша вместо сканирования DV-правил на ячейку (v4.6)
- [x] Исправлен обход лимита 200 МБ в download_file_by_url при chunked encoding (v4.6)
- [x] Исправлена нумерация миграций (дублирующийся 001_) (v4.6)
- [ ] Добавление unit-тестов (pytest)
- [ ] CI/CD pipeline
- [ ] Поддержка 5-го маркетплейса (расширение архитектуры)

### Ограничения

- Максимум 50 символов в артикуле (фильтрация некорректных данных)
- МП-файлы только .xlsx формата
- XML-файлы только .xml формата (YML-фид)
- XML-файл должен содержать элементы `<offer>` с vendorCode
- XML-файлы > 20 МБ не скачиваются через Telegram API (нужна прямая ссылка)
- Скачивание по URL: лимит 200 МБ по фактически скачанным байтам, таймаут 5 минут
- OpenRouter API rate limits
- FSM-состояния сохраняются в Redis (если доступен), иначе теряются при перезапуске
- Сессии загрузки файлов (user_files, user_schemas) хранятся в Redis с TTL 30 минут
- Задачи в очереди (pending) хранятся в Redis. При недоступности Redis используется
  in-memory очередь — задачи будут потеряны при перезапуске бота.
- Максимум `MAX_CONCURRENT_TASKS` (default=5) одновременных обработок. 6-я задача
  ждёт в очереди до освобождения слота.

## Важные предостережения

### НЕ изменять без обсуждения

- Формат обязательных столбцов (`MANDATORY_MATCHES`)
- Логику 5-уровневой валидации в `AiValidator`
- Разделители множественных значений (`;` → `";"` / `","`)
- Структуру прав доступа (роли и whitelist без лимитов)
- Префиксы XML-полей (`[XML]` и `[XML param]`)
- Порядок этапов синхронизации (габариты → МП → XML)
- Привязку XML по vendorCode
- Порядок инициализации: `Config.validate()` → `init_storage()` → `task_queue.connect()`
  → `create_bot(task_queue)` → `task_worker.start(bot)` → `polling`
- Порядок shutdown: `task_worker.stop()` → `task_queue.disconnect()` → `shutdown_storage()`
- Порядок записи AI-лога: `synchronize_data` → `create_report_with_changes` → `create_ai_log_in_report`
- Хендлеры upload.py НЕ вызывают `synchronize_data()` inline — только `task_queue.enqueue()`
- `create_bot()` теперь принимает `task_queue: TaskQueue` и передаёт его в `register_upload_handlers()`
- Импорт сессий: `from bot import storage` → `storage.session_storage` (модуль, не переменная)
- `AIComparator` создаётся в `TaskWorker.__init__` — НЕ пересоздавать на каждую задачу
- `await comparator.close()` вызывается в `TaskWorker.stop()` — НЕ вызывать раньше (активные задачи ещё используют компаратор)

### Критичные зависимости

- Порядок вызовов: схема → синхронизация → валидация
- Порядок синхронизации МВМ: загрузка → выравнивание (включая XML-артикулы) → габариты → МП-столбцы → XML-заполнение → сохранение
- `resolved_wb_dims` возвращается из `DimensionsSynchronizer.sync_dimensions()` и передаётся в `ColumnSyncer` и `XmlSyncer` — не вычислять повторно
- FSM states должны быть последовательными
- AI-логи обязательны для каждой валидации
- `AccessControlMiddleware` должен быть зарегистрирован ПЕРЕД handlers
- `ACCESS_OWNER_ID` в .env обязателен для работы системы доступа
- `register_schema_create_mvm_handlers` должен регистрироваться ПОСЛЕ `register_schema_create_handlers`
- `from bot import storage` (модуль) — НЕ `from bot.storage import db` (переменная), иначе получишь `None` на момент импорта
- Методы с внутренними `await` (`sync_all_matches`, `sync_from_xml`, `save_results`) нельзя оборачивать в `asyncio.to_thread()` — внутри потока нет event loop

## Типичные сценарии работы

### TaskQueue и TaskWorker (v4.5, обновлено v4.6)

#### TaskQueue (`services/task_queue.py`)
Абстракция над брокером задач (паттерн Repository). Поддерживает:
- `enqueue(task)` — постановка задачи в очередь (Redis LPUSH)
- `dequeue()` — блокирующее извлечение (Redis BRPOP, таймаут 5 сек)
- `update_status()` — обновление статуса и метаданных в Hash
- `get_user_tasks()` — история задач пользователя

#### Task (`services/task_queue.py`)
Dataclass-модель задачи. Поля: `user_id`, `chat_id`, `task_type` (standard/mvm),
`schema_id`, `file_paths`, `output_dir`, `report_path`, `xml_file_path`,
`status` (pending/processing/completed/failed), `created_at`, статистика результатов.

#### TaskWorker (`services/task_worker.py`)
Фоновый сервис (паттерн Service Layer):
- `AIComparator` создаётся **один раз** в `__init__` — промпты читаются с диска при старте воркера, не на каждую задачу
- Читает задачи из очереди по одной
- Выполняет полный цикл: `DataSynchronizer` → `ExcelWriter` → отправка файлов
- Ограничение параллелизма: `asyncio.Semaphore(5)` — максимум 5 одновременных обработок
- Семафор `AIComparator` теперь глобальный: максимум 5 AI-запросов суммарно по всем задачам
- При shutdown корректно дожидается завершения активных задач, затем закрывает `AIComparator`

#### Поток обработки

```
Пользователь нажимает "🚀 Обработать"
↓
Хендлер upload.py создаёт Task и вызывает task_queue.enqueue(task)
↓
Пользователь получает: "✅ Задача принята. Задач в очереди: N"
↓
TaskWorker извлекает задачу из очереди
↓
Semaphore(5) → DataSynchronizer.synchronize_data() с self._comparator
↓
Worker отправляет результат пользователю через Bot
```

#### Почему это решает проблему параллельных обработок
Раньше 5 пользователей одновременно запускали 5×N AI-запросов + 5 процессов pandas inline.
Теперь максимум 5 задач обрабатываются параллельно, остальные ждут в очереди.
Event loop бота остаётся свободным — бот отвечает на сообщения даже во время обработки.

### Добавление нового уровня валидации

1. Добавить метод в `AiValidator` (`services/sync/ai_validator.py`)
2. Вставить в каскад внутри `_validate_with_ai()` между нужными шагами
3. Обновить формат AI_Логов в `excel_io.py` если нужен новый столбец

### Добавление нового маркетплейса

1. Расширить `FILE_CONFIGS` в `config/config.py`
2. Добавить `MANDATORY_MATCHES` для нового файла
3. Добавить пару в `_PAIR_CONFIGS` в `services/sync/column_syncer.py`
4. Добавить запись в `_ARTICLE_COLUMNS` в `DataSynchronizer` и `ArticleAligner`
5. Адаптировать `AIComparator` для нового файла

### Добавление новой миграции БД

1. Добавить кортеж `("003_название", "SQL...")` В КОНЕЦ списка `MIGRATIONS` в `database/migrations.py`
2. SQL должен быть идемпотентным (`IF NOT EXISTS` или `DO $$ IF NOT EXISTS ... END $$`)
3. Нумерация должна быть уникальной и строго последовательной — `003_`, `004_` и т.д.
4. Миграция применится автоматически при следующем старте

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
- `download_file_by_url(url, user_id, filename)` — скачивание по HTTP/HTTPS ссылке (лимит 200 МБ).
  Лимит применяется по **фактически скачанным байтам** — защищает от серверов с chunked encoding
  (Google Drive, Dropbox, Яндекс.Диск), которые не отдают заголовок `Content-Length`.

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
- Форматирование (заливка заголовков, выравнивание)
- Auto-width для колонок

### AI-логи

Лист «AI_Логи» создаётся отдельно через `synchronizer.create_ai_log_in_report(report_path)` после создания отчёта ExcelWriter'ом. Содержит все события AI-валидации: маркетплейс, столбец, исходное значение, найденное значение и метод сопоставления.

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

### Почему AI-логи пропадают из отчёта?

`ExcelWriter.create_report_with_changes()` создаёт файл с нуля. Если вызвать `create_ai_log_in_report` до него — лог будет перезаписан. Правильный порядок: `synchronize_data` → `create_report_with_changes` → `create_ai_log_in_report`.

### Где теперь логика валидации и синхронизации?

После рефакторинга v4.2 `DataSynchronizer` — только оркестратор. Логика разнесена по подпакету `services/sync/`: валидация — `AiValidator`, габариты — `DimensionsSynchronizer`, синхронизация столбцов — `ColumnSyncer`, XML — `XmlSyncer`, файлы — `ExcelFileManager`.

### Почему бот использует Redis?

Redis используется для двух целей:
1. **FSM-состояния** — переживают перезапуск бота, доступны из нескольких процессов
2. **Сессии загрузки** — временные данные (пути к файлам) с TTL 30 минут, предотвращает утечку памяти

При недоступности Redis бот автоматически переключается на in-memory fallback с предупреждением в логах.

### Почему AIComparator создаётся один раз, а не на каждую задачу?

С v4.6 `AIComparator` инициализируется в `TaskWorker.__init__` и переиспользуется для всех задач.
Это даёт два эффекта: промпты читаются с диска один раз при старте (не на каждую задачу),
а семафор `asyncio.Semaphore(5)` внутри компаратора становится глобальным — ограничивает
суммарное число AI-запросов по всем параллельным задачам, а не по каждой в отдельности.

---

**Версия документации**: 4.6
**Дата обновления**: Май 2026
**Последние изменения**:
- v4.6: Оптимизация и исправление ошибок.
  `AIComparator` вынесен в `TaskWorker.__init__` — промпты читаются один раз при старте,
  семафор стал глобальным (max 5 AI-запросов суммарно по всем задачам).
  Добавлен метод `AIComparator.close()` — корректно закрывает `httpx.AsyncClient`
  при остановке воркера, устраняя утечку соединений при включённом прокси.
  `ExcelFileManager.save_results` теперь использует `self.column_validations` (O(1))
  вместо повторного сканирования DV-правил листа для каждой ячейки.
  `download_file_by_url` теперь считает фактически скачанные байты — лимит 200 МБ
  работает корректно для серверов с chunked encoding (Google Drive, Dropbox).
  Исправлена нумерация миграций: `001_add_schema_type_column` переименована в `002_`.
- v4.5: Добавлена фоновая очередь задач (Redis-backed / in-memory fallback).
  TaskWorker ограничивает количество одновременных обработок через Semaphore(5).
  Хендлеры upload.py больше не выполняют синхронизацию inline — только ставят
  задачу в очередь. Результат приходит пользователю автоматически от воркера.
  Новые переменные окружения: MAX_CONCURRENT_TASKS, TASK_QUEUE_KEY.
- v4.4: Рефакторинг хранения сессий — устранена утечка памяти в глобальных dict user_files/user_schemas.
  Добавлен SessionStorage (bot/session_storage.py) с поддержкой Redis (TTL 30 мин) и in-memory fallback.
  FSM-хранилище переведено с MemoryStorage на RedisStorage. Обновлена валидация REDIS_URL в Config.
- v4.2: Рефакторинг `DataSynchronizer` — вынесен в оркестратор (Facade). Создан подпакет `services/sync/` с 7 компонентами: `ValueConverter`, `AiValidator`, `ArticleAligner`, `DimensionsSynchronizer`, `ColumnSyncer`, `XmlSyncer`, `ExcelFileManager`. Исправлен порядок создания AI-лога: теперь `create_ai_log_in_report()` вызывается после `ExcelWriter.create_report_with_changes()`. `DimensionsSynchronizer.sync_dimensions()` возвращает `Tuple[int, resolved_wb_dims]` вместо просто `int` — устранено скрытое глобальное состояние.
- v4.1: AccessManager — in-memory кэш с TTL 60 сек (admin_id, whitelist, user_roles) с инвалидацией при мутациях. AIComparator — миграция на AsyncOpenAI с asyncio.Semaphore(5) для ограничения параллельных запросов. DataSynchronizer — полная асинхронизация цепочки AI-валидации.
- v4.0: Миграция с SQLite на PostgreSQL (asyncpg, connection pool), полностью асинхронная архитектура, async AccessManager, отдельный модуль миграций, новые переменные конфигурации (DATABASE_URL, REDIS_URL, LOG_FILE_PATH, LOG_LEVEL)
- v3.2: Добавление сопоставлений в МВМ-схемах (кнопка "➕ Добавить сопоставление"), защита от Telegram Flood control при отображении списков столбцов
- v3.1: Автоматическое добавление новых артикулов из XML в МП-файлы при обработке по МВМ-схеме
- v3.0: Поддержка МВМ-схем (3 МП + XML каталог): создание, редактирование, обновление, обработка с заполнением из XML
- v2.1.1: Добавлена фильтрация сопоставлений по типам (тройные/парные) для удобного просмотра больших схем
- v2.1: Поддержка просмотра и редактирования парных сопоставлений с автоматическим перемещением между группами
**Автор проекта**: Александр (@your_username)
