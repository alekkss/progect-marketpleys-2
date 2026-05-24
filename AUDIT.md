# Аудит качества проекта — Telegram-бот синхронизации маркетплейсов

**Дата аудита:** 2026-05-24  
**Версия проекта:** 4.6  
**Аудитор:** Senior Python Developer  

---

## Содержание

1. [Критичные проблемы](#1-критичные-проблемы)
2. [Архитектурные проблемы (нарушения SOLID)](#2-архитектурные-проблемы)
3. [Проблемы безопасности](#3-проблемы-безопасности)
4. [Проблемы производительности](#4-проблемы-производительности)
5. [Проблемы качества кода](#5-проблемы-качества-кода)
6. [Проблемы хендлеров и FSM](#6-проблемы-хендлеров-и-fsm)
7. [План исправлений по приоритету](#7-план-исправлений-по-приоритету)

---

## 1. Критичные проблемы

Проблемы, которые приводят к багам, утечкам ресурсов или потере данных в production.

---

### 1.1. AIComparator создаётся заново в хендлерах (утечка соединений)

**Файлы:** `bot/handlers/schema_create.py`, `bot/handlers/schema_update.py`

**Проблема:**

```python
# schema_create.py, строка ~130
comparator = AIComparator()
comparison_result = await comparator.compare_columns(...)

# schema_update.py, строка ~95
new_result = await AIComparator().compare_columns(...)
```

Каждый вызов создаёт новый `httpx.AsyncClient` (если прокси включён), который никогда не закрывается. При 10 обновлениях схем — 10 незакрытых HTTP-клиентов. Кроме того:

- Промпты читаются с диска заново при каждом создании.
- Семафор в этих экземплярах локальный — обходит глобальное ограничение на 5 AI-запросов.

Это прямо противоречит архитектурному решению v4.6, где `AIComparator` должен создаваться один раз в `TaskWorker`.

**Решение:**

Вариант A (минимальные изменения): передавать общий экземпляр `AIComparator` через aiogram DI (workflow data):

```python
# bot/bot.py — при создании бота
dp["ai_comparator"] = comparator  # тот же экземпляр из TaskWorker

# В хендлере — получаем через параметр
async def finalize_schema_creation(message: types.Message, state: FSMContext, ai_comparator: AIComparator):
    result = await ai_comparator.compare_columns(...)
```

Вариант B (рефакторинг): вынести создание/обновление схем в `TaskQueue` так же, как обработку файлов — через `Task` с `type="schema_create"`.

---

### 1.2. ExcelFileManager.save_results — AI-запрос на КАЖДУЮ ячейку

**Файл:** `services/sync/excel_io.py`, метод `_write_dataframe_to_sheet`

**Проблема:**

```python
for row_num, (_, row) in enumerate(df.iterrows()):
    for df_col_name, value in row.items():
        if allowed_values and self._ai_comparator:
            matched = await self._ai_comparator.match_value_with_list(str(value), allowed_values)
```

При DataFrame 10 000 строк × 50 столбцов с validation = до 500 000 потенциальных AI-вызовов. Даже при семафоре на 5 параллельных запросов — это дни работы и тысячи долларов за API.

**Решение:**

AI-валидация при сохранении должна применяться только к изменённым ячейкам. Передавать `set` изменённых координат `(row, col)` из `changes_log`:

```python
# В DataSynchronizer — собирать множество изменённых ячеек
changed_cells: Set[Tuple[str, int, str]] = set()  # (marketplace, row_idx, col_name)

# В save_results — проверять только изменённые
if (marketplace, row_num, df_col_name) in changed_cells:
    matched = await self._ai_comparator.match_value_with_list(...)
else:
    cell.value = value  # Записываем без проверки
```

---

### 1.3. sys.path.insert разбросан по всему проекту

**Файлы:** 15+ файлов содержат `sys.path.insert(0, ...)`

**Проблема:**

```python
sys.path.insert(0, str(Path(__file__).parent.parent))
```

Это хрупкий хак, зависящий от места запуска и структуры директорий. При переносе файлов, запуске из другой директории или в Docker — пути ломаются. Кроме того, это усложняет статический анализ и IDE-навигацию.

**Решение:**

Создать `pyproject.toml` с конфигурацией пакета и установить через `pip install -e .`:

```toml
# pyproject.toml
[project]
name = "marketplace-sync-bot"
version = "4.6.0"
requires-python = ">=3.10"

[tool.setuptools.packages.find]
where = ["."]
include = ["bot*", "services*", "utils*", "database*", "config*"]

[tool.setuptools]
py-modules = ["main"]
```

После `pip install -e .` все импорты работают без `sys.path`-хаков:

```python
from config.config import Config  # работает из любой директории
from services.ai_comparator import AIComparator
```

Удалить **ВСЕ** строки `sys.path.insert(...)` из всех файлов.

---

### 1.4. TaskWorker — нет backpressure на создание asyncio.Task

**Файл:** `services/task_worker.py`, метод `_worker_loop`

**Проблема:**

```python
async def _worker_loop(self) -> None:
    while self._running:
        task = await self._queue.dequeue()
        if task is None:
            continue
        process_task = asyncio.create_task(self._process_task(task))
        self._active_tasks.add(process_task)
```

Семафор внутри `_process_task` ограничивает одновременную обработку, но создание `asyncio.Task` не ограничено. При всплеске 100 задач за минуту будет создано 100 корутин, каждая из которых удерживает ресурсы (пути к файлам, ссылки на данные) ДО получения семафора.

**Решение:**

Перенести семафор на уровень СОЗДАНИЯ задач:

```python
async def _worker_loop(self) -> None:
    while self._running:
        task = await self._queue.dequeue()
        if task is None:
            continue
        # Ждём свободный слот ДО создания asyncio.Task
        await self._semaphore.acquire()
        process_task = asyncio.create_task(self._process_with_release(task))
        self._active_tasks.add(process_task)
        process_task.add_done_callback(self._active_tasks.discard)

async def _process_with_release(self, task: Task) -> None:
    try:
        await self._execute_task(task)
    finally:
        self._semaphore.release()
```

---

### 1.5. InMemoryTaskQueue — утечка памяти

**Файл:** `services/task_queue.py`, класс `InMemoryTaskQueue`

**Проблема:**

```python
class InMemoryTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._user_index: Dict[int, List[str]] = {}
```

`_tasks` и `_user_index` никогда не очищаются. За месяц работы при 1000 обработок/день накопится ~30 000 объектов `Task` в памяти (каждый содержит `file_paths`, `output_dir` и т.д.).

**Решение:**

Добавить TTL-очистку и лимит на количество хранимых задач:

```python
import time

MAX_STORED_TASKS: int = 1000
TASK_TTL_SECONDS: int = 3600  # 1 час

class InMemoryTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._task_timestamps: Dict[str, float] = {}
        self._user_index: Dict[int, List[str]] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def enqueue(self, task: Task) -> None:
        self._cleanup_expired()
        if len(self._tasks) >= MAX_STORED_TASKS:
            self._evict_oldest()
        self._tasks[task.id] = task
        self._task_timestamps[task.id] = time.monotonic()
        # ...

    def _cleanup_expired(self) -> None:
        now = time.monotonic()
        expired = [
            tid for tid, ts in self._task_timestamps.items()
            if now - ts > TASK_TTL_SECONDS
        ]
        for tid in expired:
            self._tasks.pop(tid, None)
            self._task_timestamps.pop(tid, None)
```

---

### 1.6. Баг в handle_mvm_update_mp_file — NameError

**Файл:** `bot/handlers/schema_update.py`

**Проблема:**

```python
async def handle_mvm_update_mp_file(message, state, bot):
    ...
    if mp in user_schemas:  # ← user_schemas НЕ ОПРЕДЕЛЕНА в этой точке!
        await message.answer(f"⚠️ {mp.upper()} уже загружен")
        return

    user_schemas = await storage.session_storage.get_files_dict(...)  # ← определяется ЗДЕСЬ
```

При загрузке дублирующего файла произойдёт `NameError: name 'user_schemas' is not defined`.

**Решение:**

Переместить `get_files_dict` выше первого использования:

```python
async def handle_mvm_update_mp_file(message, state, bot):
    user_id = message.from_user.id
    if (await state.get_data()).get('mvm_mp_files_processed'):
        return

    fp, fn, mp = await download_file(bot, message, user_id)
    if not mp:
        await message.answer("❌ Переименуй файл (добавь wb/ozon/yandex)")
        return

    user_schemas = await storage.session_storage.get_files_dict(user_id, _SESSION_KEY_UPDATE)
    if mp in user_schemas:
        await message.answer(f"⚠️ {mp.upper()} уже загружен")
        return
    # ...
```

---

### 1.7. Config — атрибуты класса вычисляются при импорте

**Файл:** `config/config.py`

**Проблема:**

```python
class Config:
    AI_TEMPERATURE: float = float(os.getenv("AI_TEMPERATURE", "0.1"))
```

Если переменная окружения содержит невалидное значение (например, `"abc"`), `float()` упадёт с `ValueError` при импорте модуля, а не при вызове `validate()`. Стектрейс будет указывать на строку определения класса, что крайне неинформативно.

**Решение:**

Обернуть потенциально падающие преобразования в safe-функции:

```python
def _safe_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default

class Config:
    AI_TEMPERATURE: float = _safe_float_env("AI_TEMPERATURE", 0.1)
```

---

## 2. Архитектурные проблемы

Нарушения SOLID, проблемы с расширяемостью и поддержкой.

---

### 2.1. Config — God Object (200+ строк, 5+ ответственностей)

**Файл:** `config/config.py`

**Проблема:**

Класс `Config` смешивает:

- Переменные окружения (API keys, DB URL)
- Конфигурацию маркетплейсов (`FILE_CONFIGS`)
- Бизнес-правила (`MANDATORY_MATCHES`, `EXCLUDED_COLUMNS`)
- Доменные маппинги (`WB_DIMENSION_PATTERNS`, `XML_UNIT_MAPPING`)
- Валидацию конфигурации

Нарушен Single Responsibility. Изменение бизнес-правил (новый обязательный столбец) требует редактирования того же файла, что и параметры подключения к БД.

**Решение:**

Разделить на 4 модуля:

```
config/
├── __init__.py              # Реэкспорт для обратной совместимости
├── settings.py              # Env-переменные: TELEGRAM_BOT_TOKEN, DATABASE_URL, etc.
├── marketplace.py           # FILE_CONFIGS, MANDATORY_MATCHES, EXCLUDED_COLUMNS
├── mappings.py              # WB_DIMENSION_PATTERNS, XML_UNIT_MAPPING, ALL_WEIGHT_COLUMN_NAMES
└── validators.py            # ColumnValidator, validate_config()
```

---

### 2.2. Module-level алиасы в config.py — два способа доступа

**Файл:** `config/config.py`, строки 200+

**Проблема:**

```python
OPENROUTER_API_KEY = Config.OPENROUTER_API_KEY
TELEGRAM_BOT_TOKEN = Config.TELEGRAM_BOT_TOKEN
FILE_CONFIGS = Config.FILE_CONFIGS
# ... 20+ строк
```

Создаёт два равноправных способа доступа к одним и тем же данным. При переименовании или удалении атрибута из `Config` алиас продолжит экспортировать старое значение. Код, использующий `from config.config import TELEGRAM_BOT_TOKEN`, работает с копией значения, а не с классом.

**Решение:**

Удалить все module-level алиасы. Весь код должен использовать `Config.X` напрямую:

```python
# Вместо:
from config.config import TELEGRAM_BOT_TOKEN
# Использовать:
from config.config import Config
token = Config.TELEGRAM_BOT_TOKEN
```

Для обратной совместимости на переходный период можно оставить алиасы с `DeprecationWarning`:

```python
import warnings

def __getattr__(name):
    if hasattr(Config, name):
        warnings.warn(
            f"Прямой импорт '{name}' из config.config устарел. "
            f"Используйте Config.{name}",
            DeprecationWarning, stacklevel=2
        )
        return getattr(Config, name)
    raise AttributeError(f"module 'config.config' has no attribute '{name}'")
```

---

### 2.3. ColumnValidator в config.py — нарушение SRP

**Файл:** `config/config.py`

**Проблема:**

Класс бизнес-логики (валидация столбцов) живёт в файле конфигурации.

**Решение:**

Перенести в `utils/validators.py` или `services/sync/column_validator.py`.

---

### 2.4. AIComparator — God Class (~600 строк)

**Файл:** `services/ai_comparator.py`

**Проблема:**

Класс совмещает 5+ ответственностей:

- HTTP-клиент с retry-логикой (`_call_ai`)
- Парсинг JSON-ответов (`_parse_response`)
- Бизнес-логика валидации столбцов (`_validate_ai_result`)
- Бизнес-логика дедупликации (`_deduplicate_matches`, `_deduplicate_mvm_matches`)
- Бизнес-логика МВМ-объединения (`_merge_mvm_results`)
- Форматирование промптов (`_build_prompt`)

**Решение:**

Разбить на 3–4 класса:

```
services/
├── ai/
│   ├── __init__.py
│   ├── client.py           # AIClient — HTTP, retry, семафор, close()
│   ├── column_matcher.py   # ColumnMatcher — compare_columns, compare_columns_mvm
│   ├── value_matcher.py    # ValueMatcher — match_value_with_list
│   ├── result_processor.py # ResultProcessor — валидация, дедупликация, merge
│   └── prompt_loader.py    # PromptLoader — чтение и форматирование промптов
```

---

### 2.5. DimensionsSynchronizer — classmethod everywhere

**Файл:** `services/sync/dimensions_synchronizer.py`

**Проблема:**

Все методы — `@classmethod`, класс не хранит состояние. Это функциональный стиль, замаскированный под ООП. Кроме того, `_ARTICLE_COLUMNS` дублирует `DataSynchronizer._ARTICLE_COLUMNS`.

**Решение:**

Два варианта:

A) Сделать обычным классом с состоянием (предпочтительно для DI):

```python
class DimensionsSynchronizer:
    def __init__(self, article_columns: Dict[str, str]) -> None:
        self._article_columns = article_columns

    def sync_dimensions(self, dfs: Dict[str, pd.DataFrame]) -> Tuple[int, Optional[Dict]]:
        ...
```

B) Если класс не нужен — превратить в модуль с функциями:

```python
# services/sync/dimensions_synchronizer.py
def sync_dimensions(dfs, article_columns) -> Tuple[int, Optional[Dict]]:
    ...
```

---

### 2.6. DataSynchronizer — прямое присваивание приватного атрибута

**Файл:** `services/data_synchronizer.py`

**Проблема:**

```python
xml_syncer = self._build_xml_syncer(ai_validator)
# ... позже ...
xml_syncer._resolved_wb_dims = resolved_wb_dims  # ← нарушение инкапсуляции
```

`resolved_wb_dims` ещё не известен при создании `XmlSyncer`, поэтому его приходится присваивать напрямую.

**Решение:**

Передавать `resolved_wb_dims` как параметр в методы, а не через атрибут:

```python
# Вместо:
xml_syncer._resolved_wb_dims = resolved_wb_dims
filled = await xml_syncer.sync_from_xml(synced_dfs)

# Использовать:
filled = await xml_syncer.sync_from_xml(synced_dfs, resolved_wb_dims=resolved_wb_dims)
dims = xml_syncer.sync_dimensions_from_xml(synced_dfs, resolved_wb_dims=resolved_wb_dims)
```

---

### 2.7. Database — возвращает Dict вместо dataclass

**Файл:** `database/database.py`

**Проблема:**

Все методы возвращают `Optional[Dict]` или `List[Dict]`. Это:

- Затрудняет IDE-автодополнение
- Нет проверки типов при доступе к ключам
- Риск `KeyError` при опечатке

**Решение:**

Создать dataclass-модели для возвращаемых данных:

```python
# database/models.py
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class UserStats:
    total_processings: int
    registered_at: Optional[str]
    successful: int
    failed: int
    total_synced_cells: int

@dataclass
class SchemaInfo:
    id: int
    name: str
    schema_type: str
    created_at: Optional[str]
    updated_at: Optional[str]
    matches_count: int = 0
    owner_id: Optional[int] = None
    owner_name: Optional[str] = None
```

---

### 2.8. Дублирование session_storage

**Файлы:** `bot/storage.py` и `bot/session_storage.py`

**Проблема:**

Оба файла создают экземпляр `SessionStorage`:

```python
# bot/storage.py
session_storage: SessionStorage = SessionStorage()

# bot/session_storage.py (конец файла)
session_storage: SessionStorage = SessionStorage()
```

Если кто-то импортирует из неправильного модуля — будет работать с неподключённым экземпляром.

**Решение:**

Удалить глобальный экземпляр из `bot/session_storage.py`. Оставить только определение класса. Единственный экземпляр живёт в `bot/storage.py`.

---

## 3. Проблемы безопасности

---

### 3.1. SSRF через download_file_by_url

**Файл:** `bot/utils.py`

**Проблема:**

URL, отправленный пользователем, может содержать redirect на `http://localhost:5432`, `http://169.254.169.254/` (AWS metadata) и т.д. Это позволяет сканировать внутреннюю сеть сервера.

**Решение:**

Добавить проверку resolved IP-адреса перед скачиванием:

```python
import ipaddress
import socket

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
]

def _is_safe_url(url: str) -> bool:
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        return not any(ip in net for net in _BLOCKED_NETWORKS)
    except (socket.gaierror, ValueError):
        return False
```

---

### 3.2. Нет rate limiting

**Проблема:**

Авторизованный пользователь может:

- Отправить 100 файлов за секунду (перегрузка storage)
- Запустить 100 операций создания схемы (100 AI-вызовов в обход семафора `TaskWorker`)
- Вызвать flood при отображении 1000+ сопоставлений через `_send_long_text`

**Решение:**

Добавить throttling middleware:

```python
# bot/middlewares/throttling.py
from aiogram import BaseMiddleware
from collections import defaultdict
import time

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 0.5) -> None:
        self._rate_limit = rate_limit
        self._last_call: dict[int, float] = defaultdict(float)

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        now = time.monotonic()
        if now - self._last_call[user_id] < self._rate_limit:
            return  # Пропускаем слишком частые запросы
        self._last_call[user_id] = now
        return await handler(event, data)
```

---

### 3.3. delete_schema — владелец не может удалить чужую схему

**Файл:** `bot/handlers/schema_delete.py`

**Проблема:**

```python
deleted = await storage.db.delete_schema(user_id, schema_name)
```

`delete_schema` фильтрует по `user_id`, поэтому владелец/админ не может удалить чужую схему, хотя по документации имеет «полный контроль».

**Решение:**

Добавить метод `delete_schema_by_id` для привилегированных пользователей:

```python
# database/database.py
async def delete_schema_by_id(self, schema_id: int) -> bool:
    async with self.pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM schemas WHERE id = $1", schema_id
        )
        return result == 'DELETE 1'

# bot/handlers/schema_delete.py
if AccessManager.is_owner(user_id) or await AccessManager.is_admin(user_id):
    schema = await storage.db.get_schema_by_name_global(schema_name)
    if schema:
        deleted = await storage.db.delete_schema_by_id(schema['id'])
else:
    deleted = await storage.db.delete_schema(user_id, schema_name)
```

---

## 4. Проблемы производительности

---

### 4.1. ArticleAligner.align вызывается дважды

**Файлы:** `services/data_synchronizer.py`, `services/sync/column_syncer.py`

**Проблема:**

```python
# DataSynchronizer.synchronize_data (шаг 4):
dfs = await asyncio.to_thread(article_aligner.align, dfs)

# ColumnSyncer.sync_all_matches (внутри шага 6):
synced_dfs = self._article_aligner.align(synced_dfs)  # ПОВТОРНО!
```

`align` вызывается дважды — в оркестраторе и внутри `ColumnSyncer`. Каждый вызов — `concat` + set-операции на больших DataFrame.

**Решение:**

Убрать вызов `align` из `ColumnSyncer.sync_all_matches`. Выравнивание должно выполняться ОДИН раз в оркестраторе, а `ColumnSyncer` должен работать с уже выровненными данными:

```python
# ColumnSyncer.sync_all_matches — убрать строку:
# synced_dfs = self._article_aligner.align(synced_dfs)
```

---

### 4.2. create_article_map вызывается для КАЖДОГО сопоставления

**Файл:** `services/sync/column_syncer.py`

**Проблема:**

В `_sync_three_columns` и `_sync_two_columns`:

```python
wb_data = self._article_aligner.create_article_map(dfs["wildberries"], article_col, col_wb)
```

При 50 тройных + 30 парных сопоставлениях `create_article_map` вызывается (50×3 + 30×2) = 210 раз. Каждый вызов итерирует весь DataFrame. При 10 000 строк — 2 100 000 итераций.

**Решение:**

Кэшировать `article_map` по ключу `(marketplace, column_name)`:

```python
class ColumnSyncer:
    def __init__(self, ...):
        ...
        self._article_map_cache: Dict[Tuple[str, str], Dict] = {}

    def _get_article_map(self, dfs, marketplace: str, col_name: str) -> Dict:
        cache_key = (marketplace, col_name)
        if cache_key not in self._article_map_cache:
            self._article_map_cache[cache_key] = self._article_aligner.create_article_map(
                dfs[marketplace], self._article_columns[marketplace], col_name
            )
        return self._article_map_cache[cache_key]
```

---

### 4.3. DimensionsSynchronizer — iterrows() вместо векторных операций

**Файл:** `services/sync/dimensions_synchronizer.py`

**Проблема:**

```python
for _, row in dfs["yandex"].iterrows():
    article = row.get(article_col)
    ...
```

`iterrows()` — самый медленный способ итерации DataFrame (100x медленнее vec-операций для 100k строк).

**Решение:**

Использовать векторизованные операции pandas:

```python
# Вместо iterrows:
mask = dfs["yandex"][article_col].notna() & (dfs["yandex"][article_col].str.strip() != "")
valid_rows = dfs["yandex"][mask]

for idx, article in valid_rows[article_col].items():
    composite_val = valid_rows.at[idx, yandex_col]
    dimensions = cls.parse_composite_dimensions(composite_val)
    if dimensions:
        result[str(article).strip()] = dimensions
```

Или через `apply` / `str.split`:

```python
# Парсинг всего столбца сразу
parts = dfs["yandex"][yandex_col].str.split("/", expand=True)
valid_mask = parts.notna().all(axis=1) & (parts.shape[1] == 3)
```

---

### 4.4. XmlReader — ET.parse() загружает весь файл в RAM

**Файл:** `utils/xml_reader.py`

**Проблема:**

Для файлов 200 МБ (лимит загрузки) `ElementTree` создаёт дерево ~1–2 ГБ в RAM. Метод `search_categories` вызывает `ET.parse()` и `root.findall('.//offer')` — полный обход дерева.

**Решение:**

Для файлов > 50 МБ использовать `iterparse()`:

```python
import xml.etree.ElementTree as ET

def get_offer_data_streaming(file_path: str) -> List[Dict]:
    """Потоковое чтение офферов — не загружает всё дерево в RAM."""
    result = []
    for event, elem in ET.iterparse(file_path, events=("end",)):
        if elem.tag == "offer":
            offer_data = _parse_single_offer(elem)
            result.append(offer_data)
            elem.clear()  # Освобождаем память
    return result
```

---

### 4.5. _validate_redis_url — слишком строгий regex

**Файл:** `config/config.py`

**Проблема:**

```python
pattern = r'^redis(s)?://[^\s/]+:\d+(/\d+)?$'
```

Не пропустит:

- URL с аутентификацией: `redis://user:password@host:6379/0`
- URL без номера БД: `redis://localhost:6379`
- URL с query params: `redis://host:6379/0?timeout=5`

**Решение:**

```python
def _validate_redis_url(url: str) -> bool:
    if not url:
        return False
    pattern = r'^redis(s)?://([^@]+@)?[^\s/:]+:\d+(/\d+)?(\?.*)?$'
    return bool(re.match(pattern, url))
```

---

## 5. Проблемы качества кода

---

### 5.1. print() вместо logger в AIComparator и ExcelWriter

**Файлы:** `services/ai_comparator.py` (~40 вызовов), `utils/excel_writer.py` (~5 вызовов)

**Проблема:**

```python
print(f"[+] Первый проход завершен!")
print(f"[*] Отправляю ПЕРВЫЙ запрос в OpenRouter AI...")
```

В production:

- Не попадают в файл логов
- Нет timestamp и level
- Нет trace_id для корреляции
- Невозможно отфильтровать по уровню

**Решение:**

```python
logger.info("Первый проход завершен")
logger.info("Отправка запроса в OpenRouter AI...")
logger.warning("ВАЛИДАЦИЯ: Отклонено %d несуществующих совпадений", rejected_count)
```

---

### 5.2. f-string в вызовах logger

**Файлы:** множество файлов

**Проблема:**

```python
logger.info(f"XML данные: {len(self.xml_offer_data)} офферов")
logger.debug(f"Конвертация: {numeric_value} кг → {result} г")
```

f-string вычисляется ВСЕГДА, даже если уровень логирования выше (например, `WARNING`). При тяжёлых вычислениях в аргументах это снижает производительность.

**Решение:**

Использовать `%`-форматирование (lazy evaluation):

```python
logger.info("XML данные: %d офферов", len(self.xml_offer_data))
logger.debug("Конвертация: %s кг → %s г", numeric_value, result)
```

---

### 5.3. logger propagate=True + console handler = дублирование

**Файл:** `utils/logger_config.py`

**Проблема:**

```python
logger.propagate = True
```

При `propagate = True` записи передаются в root logger (который имеет file handler). Но каждый модуль также получает свой console handler. Если корневой логгер тоже имеет console handler — сообщения дублируются.

**Решение:**

Установить `propagate = False` и явно добавлять оба handler'а. Или — убрать отдельные console handler'ы из модульных логгеров и добавить один console handler в root:

```python
def _install_handlers() -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

def setup_logger(name: str) -> AppLogger:
    _install_handlers()  # Один раз
    logger = logging.getLogger(name)
    logger.setLevel(_LOG_LEVEL)
    logger.propagate = True  # Наследует handlers от root
    return AppLogger(logger, {"trace_id": ""})
```

---

### 5.4. Миграции без tracking

**Файл:** `database/migrations.py`

**Проблема:**

Миграции идемпотентны (`IF NOT EXISTS`), но нет таблицы `applied_migrations`. При 100+ миграциях каждый запуск будет выполнять все SQL-запросы (даже если они уже применены).

**Решение:**

```python
TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS applied_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

async def run_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)
        await conn.execute(TRACKING_TABLE_SQL)

        for migration_name, migration_sql in MIGRATIONS:
            already_applied = await conn.fetchval(
                "SELECT 1 FROM applied_migrations WHERE name = $1",
                migration_name
            )
            if already_applied:
                continue

            await conn.execute(migration_sql)
            await conn.execute(
                "INSERT INTO applied_migrations (name) VALUES ($1)",
                migration_name
            )
            logger.info("Миграция '%s' применена.", migration_name)
```

---

### 5.5. Monkey-patching openpyxl при импорте

**Файл:** `utils/excel_reader.py`

**Проблема:**

```python
# Применяем патч при импорте модуля
_patch_data_validation()
```

Побочный эффект при импорте — антипаттерн. Проблемы:

- Непредсказуемость при повторных импортах
- Невозможность отключить в тестах
- Скрытая зависимость от порядка импортов

**Решение:**

Вызывать патч явно при инициализации приложения:

```python
# В main.py или bot/bot.py при старте:
from utils.excel_reader import apply_openpyxl_patch
apply_openpyxl_patch()
```

---

### 5.6. Неиспользуемые импорты и переменные

| Файл | Неиспользуемый импорт |
|---|---|
| `session_storage.py` | `from datetime import datetime, timezone` |
| `xml_reader.py` | `Optional` (в одном месте), `Set` из `typing` |
| `access_management.py` | `Router` (создан, но не используется) |
| `excel_writer.py` | `setup_logger` (импортирован, logger не создан) |

**Решение:** Удалить неиспользуемые импорты. Настроить `ruff` с правилом `F401`.

---

## 6. Проблемы хендлеров и FSM

---

### 6.1. functools.partial для DI — хрупкий паттерн

**Файлы:** все `register_*_handlers` функции

**Проблема:**

```python
dp.message.register(partial(handle_file, bot=bot), UploadStates.waiting_for_files, F.document)
dp.message.register(partial(process_files, bot=bot, task_queue=task_queue), ...)
```

Недостатки:

- Ручное отслеживание аргументов каждого хендлера
- При добавлении новой зависимости — правки во всех `partial(...)`
- Нет единого места управления зависимостями

**Решение:**

Использовать встроенный DI aiogram 3 через workflow data:

```python
# bot/bot.py — при создании
dp["bot_instance"] = bot
dp["task_queue"] = task_queue
dp["ai_comparator"] = comparator

# В хендлере — получаем через keyword argument
async def handle_file(message: types.Message, state: FSMContext, bot_instance: Bot):
    ...

async def process_files(message: types.Message, state: FSMContext, task_queue: TaskQueue):
    ...
```

Aiogram автоматически инжектирует значения из `dp[key]` в handler, если имя параметра совпадает с ключом.

---

### 6.2. Дублирование логики загрузки файлов (6 функций)

**Файлы:** `upload.py`, `schema_create.py`, `schema_create_mvm.py`, `schema_edit.py`, `schema_update.py`

**Проблема:**

6 функций с идентичной логикой:

1. `download_file(bot, message, user_id)`
2. Проверка marketplace
3. Проверка дубликата в session
4. Сохранение в session
5. Проверка «3 из 3»

**Решение:**

Вынести в общий helper:

```python
# bot/helpers/file_upload.py
async def handle_mp_file_upload(
    bot: Bot,
    message: types.Message,
    user_id: int,
    session_key: str,
    on_complete: Callable | None = None,
) -> tuple[bool, str | None]:
    """
    Универсальная загрузка MP-файла.

    Returns:
        (all_loaded: bool, error_message: str | None)
    """
    file_path, file_name, marketplace = await download_file(bot, message, user_id)
    if not marketplace:
        return False, "❌ Переименуй файл (добавь wb/ozon/yandex)"

    user_files = await storage.session_storage.get_files_dict(user_id, session_key)
    if marketplace in user_files:
        return False, f"⚠️ {marketplace.upper()} уже загружен"

    user_files[marketplace] = file_path
    await storage.session_storage.set_files_dict(user_id, session_key, user_files)
    await message.answer(f"✅ {marketplace.upper()} ({len(user_files)}/3)")

    if len(user_files) == 3:
        if on_complete:
            await on_complete()
        return True, None

    return False, None
```

---

### 6.3. schema_edit.py — 700+ строк, смешение ответственностей

**Файл:** `bot/handlers/schema_edit.py`

**Проблема:**

Один файл содержит:

- Константы (`STANDARD_MATCH_GROUPS`, `MVM_MATCH_GROUPS`)
- 15+ вспомогательных функций форматирования
- 10+ FSM-хендлеров
- Логику определения типов сопоставлений

**Решение:**

```
bot/handlers/
├── schema_edit/
│   ├── __init__.py           # register_schema_edit_handlers
│   ├── constants.py          # MATCH_GROUPS, COLUMN_DISPLAY_NAMES
│   ├── helpers.py            # Вспомогательные функции форматирования
│   └── handlers.py           # FSM-хендлеры
```

---

### 6.4. schema_update.py — несовместимый стиль кода

**Файл:** `bot/handlers/schema_update.py`

**Проблема:**

Сжатые однострочники, переменные `u`, `nc`, `sc`, `fn` — резко отличается от стиля остальных файлов:

```python
nc, sc = 0, 0
for gk in mvm_groups:
    eks = {tuple(m.get(f'column_{i}', '') for i in range(1,5)) for m in existing.get(gk, [])}
```

**Решение:**

Привести к единому стилю проекта: полные имена переменных, docstrings для каждой функции, разбивка длинных строк, форматирование через `ruff`.

---

## 7. План исправлений по приоритету

### Фаза 1 — Критичные (неделя 1)

| # | Проблема | Файл(ы) | Сложность |
|---|---|---|---|
| 1 | AIComparator создаётся в хендлерах | `schema_create.py`, `schema_update.py` | Средняя |
| 2 | save_results — AI на каждую ячейку | `services/sync/excel_io.py` | Средняя |
| 3 | sys.path.insert everywhere | 15+ файлов + `pyproject.toml` | Низкая |
| 4 | Баг NameError в schema_update | `schema_update.py` | Низкая |
| 5 | InMemoryTaskQueue утечка | `services/task_queue.py` | Низкая |
| 6 | TaskWorker без backpressure | `services/task_worker.py` | Низкая |
| 7 | Config float() при импорте | `config/config.py` | Низкая |

### Фаза 2 — Высокий приоритет (неделя 2–3)

| # | Проблема | Файл(ы) | Сложность |
|---|---|---|---|
| 8 | print() → logger | `ai_comparator.py`, `excel_writer.py` | Низкая |
| 9 | Дублирование session_storage | `bot/session_storage.py` | Низкая |
| 10 | logger propagate дублирование | `utils/logger_config.py` | Низкая |
| 11 | Общий helper загрузки файлов | `bot/helpers/file_upload.py` | Средняя |
| 12 | _validate_redis_url regex | `config/config.py` | Низкая |
| 13 | Кэш article_map | `services/sync/column_syncer.py` | Средняя |
| 14 | Убрать повторный align | `services/sync/column_syncer.py` | Низкая |
| 15 | Tracking миграций | `database/migrations.py` | Средняя |

### Фаза 3 — Средний приоритет (неделя 4–5)

| # | Проблема | Файл(ы) | Сложность |
|---|---|---|---|
| 16 | Разделить Config | `config/` | Средняя |
| 17 | aiogram DI вместо partial | `bot/bot.py`, `handlers/` | Средняя |
| 18 | Rate limiting middleware | `bot/middlewares/` | Средняя |
| 19 | Database → dataclass models | `database/` | Средняя |
| 20 | SSRF защита | `bot/utils.py` | Низкая |
| 21 | XmlReader iterparse | `utils/xml_reader.py` | Высокая |
| 22 | delete_schema для owner | `handlers/schema_delete.py` | Низкая |

### Фаза 4 — Рефакторинг (неделя 6+)

| # | Проблема | Файл(ы) | Сложность |
|---|---|---|---|
| 23 | Разбить AIComparator | `services/ai/` | Высокая |
| 24 | Разбить schema_edit.py | `bot/handlers/schema_edit/` | Средняя |
| 25 | DimensionsSynchronizer → instance | `services/sync/` | Средняя |
| 26 | Привести schema_update к стилю | `bot/handlers/schema_update.py` | Низкая |
| 27 | iterrows → vectorized | `services/sync/dimensions_synchronizer.py` | Средняя |
| 28 | Удалить module-level алиасы | `config/config.py` | Низкая |
| 29 | Monkey-patch → explicit init | `utils/excel_reader.py` | Низкая |
| 30 | Убрать неиспользуемые импорты | множество файлов | Низкая |

---

## Приложение: Метрики проекта

| Метрика | Значение |
|---|---|
| Файлов проанализировано | 32 |
| Критичных проблем | 7 |
| Архитектурных проблем | 8 |
| Проблем безопасности | 3 |
| Проблем производительности | 5 |
| Проблем качества кода | 6 |
| Проблем хендлеров/FSM | 4 |
| **Итого проблем** | **33** |
| Оценка технического долга | ~3–6 недель работы одного разработчика |
