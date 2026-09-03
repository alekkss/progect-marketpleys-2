# Marketplace Sync — Актуальная документация проекта (v6.1)

## Назначение

Telegram-бот и веб-приложение для автоматической синхронизации данных товаров между тремя маркетплейсами: Wildberries, Ozon и Яндекс.Маркет. Система использует AI для интеллектуального сопоставления столбцов Excel-файлов и валидации значений с учётом справочников (validation lists).

Три канала доступа:
- **Telegram-бот** — существующий интерфейс, работает без изменений
- **Веб-сайт** (`https://ecommpedia.ru`) — браузерный интерфейс с drag&drop загрузкой, real-time прогрессом через WebSocket, скачиванием результатов
- **Внешний REST API** (`https://ecommpedia.ru/v1/mapping-tasks`) — AI-агент маппинга PIM+FDM (v6.0): принимает задания от внешней системы FDM на сопоставление атрибутов категорий и справочных значений между маркетплейсами, Bearer-аутентификация, асинхронная обработка с GET-поллингом статуса

Все каналы используют общую бизнес-логику, единый пул БД и общий AIComparator с глобальным семафором AI-запросов. Файловые задачи идут через Redis TaskQueue, задания агента — через таблицу mapping_jobs в PostgreSQL (разные жизненные циклы).


---

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                     ОБЩЕЕ ЯДРО (shared services)                │
│                                                                   │
│   services/           database/          utils/       config/   │
│   ├─ data_synchronizer ├─ database.py    ├─ excel_reader config.py│
│   ├─ ai_comparator     └─ migrations.py  ├─ xml_reader          │
│   ├─ task_queue.py                       ├─ excel_writer        │
│   ├─ task_worker.py                      └─ logger_config       │
│   ├─ sync/ (7 компонентов)                                      │
│   └─ mapping/ (AI-агент PIM+FDM, v6.0):                          │
│      models, validators, attribute_mapper,                      │
│      reference_value_mapper, job_worker                         │
└───────────────────────────────┬──────────────────────────────────┘
                                 │
                ┌────────────────┼─────────────────┐
                │                │                  │
         ┌──────▼──────┐  ┌──────▼──────┐   ┌──────▼──────┐
         │    bot/     │  │    web/     │   │   shared/   │
         │  (aiogram)  │  │  (aiohttp)  │   │ (адаптеры)  │
         │  handlers   │  │  routes     │   │  delivery   │
         │  keyboards  │  │  templates  │   │  notify     │
         │  states     │  │  static     │   │             │
         │             │  │  middleware │   │             │
         └─────────────┘  └──────┬──────┘   └─────────────┘
                                 │ /v1/* (HTTPS + Bearer)
                                 ▲
                          ┌──────┴──────┐
                          │    FDM      │
                          │ (внешняя    │
                          │  система)   │
                          └─────────────┘

```

### Технологии

| Компонент | Технология | Назначение |
|-----------|-----------|-----------|
| Bot Framework | aiogram 3.x (async) | Telegram-интерфейс |
| Web Framework | aiohttp 3.x (async) | HTTP-сервер, REST API |
| Шаблонизатор | aiohttp-jinja2 + Jinja2 | Серверный рендеринг HTML |
| Веб-сессии | aiohttp-session[redis] | Cookie-based авторизация |
| Пароли | bcrypt | Хеширование паролей |
| БД | PostgreSQL (asyncpg) | Основное хранилище |
| Кэш/очередь | Redis | FSM, сессии, очередь задач |
| AI | OpenRouter API (AsyncOpenAI) | Сопоставление и валидация |
| Файлы | pandas + openpyxl | Чтение/запись Excel |
| Real-time | WebSocket (aiohttp встроенный) | Прогресс обработки |
| Reverse Proxy | Nginx | SSL, static, WebSocket proxy |
| SSL | certbot (Let's Encrypt) | HTTPS для ecommpedia.ru |
| Шрифт | Inter (Google Fonts, CDN) | Типографика веб-интерфейса (v6.1) |

### Ключевые архитектурные принципы

- **Один event loop** — бот (polling) и веб-сервер (aiohttp) работают в одном asyncio event loop через `asyncio.gather()`. Общие ресурсы: DB pool, Redis, AIComparator, TaskQueue.
- **Единая очередь** — и бот, и веб ставят задачи в одну TaskQueue. TaskWorker обрабатывает их одинаково, доставка результата зависит от `delivery_channel` в Task.
- **Strategy для доставки** — `ResultDelivery` абстракция с двумя реализациями: `TelegramDelivery` (Bot.send_document) и `WebDelivery` (сохранение + WebSocket-уведомление).
- **Бот не изменён** — все существующие хендлеры, FSM, клавиатуры работают как раньше. Веб-интерфейс — параллельный канал, а не замена.
- **AI-агент маппинга PIM+FDM (v6.0)** — асинхронные задания из PostgreSQL (не Redis-очередь): `POST /v1/mapping-tasks` → таблица `mapping_jobs` → `MappingJobWorker` → результат через GET-поллинг FDM. Использует общий AIComparator: семафор(5) ограничивает AI-запросы суммарно с синхронизацией файлов.
- **Два независимых контура безопасности** — сайт: cookie-сессии + CSRF; API `/v1/*`: Bearer-токен `FDM_API_TOKEN`. `/v1/*` исключён из auth/csrf middleware, проверяется `api_auth_middleware` (constant-time сравнение, 401/503).

---

## Структура проекта

```
/
├── main.py                          # Точка входа: бот + веб в одном event loop
├── .env                              # Конфигурация (API ключи, DB, Redis, Web)
├── .env.example                      # Шаблон переменных окружения с описаниями
│
├── /prompts/                         # === ПРОМПТЫ AI (читаются с диска при старте) ===
│   ├── column_matching.txt           # Сопоставление столбцов (3 МП, первый проход)
│   ├── schema_comparison.txt         # Второй проход (оставшиеся столбцы)
│   ├── value_validation.txt          # Валидация значений против справочников
│   ├── mvm_column_matching.txt       # МВМ-сопоставление (4 источника)
│   ├── attribute_mapping.txt         # Маппинг атрибутов PIM+FDM (v6.0)
│   └── reference_value_mapping.txt   # Маппинг справочных значений PIM+FDM (v6.0)
├── /bot/                             # === TELEGRAM БОТ (без изменений) ===
│   ├── bot.py                        # Инициализация бота, веб-сервера, жизненный цикл
│   ├── storage.py                    # Глобальный Database + SessionStorage + init/shutdown
│   ├── session_storage.py            # Redis-сессии (TTL 30 мин) с in-memory fallback
│   ├── security.py -> access.py      # Псевдоним для AccessManager
│   ├── utils.py                      # download_file, download_xml_from_telegram, download_file_by_url
│   │
│   ├── /handlers/
│   │   ├── common.py                 # /start, меню
│   │   ├── upload.py                 # Загрузка файлов (standard + mvm)
│   │   ├── schema_create.py          # Создание стандартных схем
│   │   ├── schema_create_mvm.py      # Создание МВМ-схем
│   │   ├── schema_edit.py            # Редактирование схем
│   │   ├── schema_update.py          # Обновление схем
│   │   ├── schema_delete.py          # Удаление схем
│   │   ├── stats.py                  # Статистика пользователя
│   │   └── access_management.py      # Управление доступами
│   │
│   ├── /middlewares/
│   │   └── access_control.py         # Проверка прав (Telegram)
│   │
│   ├── keyboards.py                  # Telegram-клавиатуры
│   ├── states.py                     # FSM-состояния
│   └── access.py                     # AccessManager (async, TTL-кэш)
│
├── /web/                             # === ВЕБ-ПРИЛОЖЕНИЕ ===
│   ├── __init__.py                   # Пакет, экспорт create_web_app
│   ├── app.py                        # Создание aiohttp Application (Factory)
│   │
│   ├── /auth/
│   │   ├── __init__.py               # Пакет аутентификации, экспорты
│   │   ├── password.py               # bcrypt хеширование (asyncio.to_thread)
│   │   ├── session.py                # WebSessionManager (cookie sessions через PostgreSQL)
│   │   ├── permissions.py            # WebAccessManager (проверка ролей)
│   │   └── decorators.py             # @login_required, @admin_required, @editor_required
│   │
│   ├── /middleware/
│   │   ├── __init__.py               # setup_middlewares: errors → auth → csrf → api_auth
│   │   ├── auth.py                   # Проверка cookie-сессии (пропускает /v1/*)
│   │   ├── errors.py                 # 404, 500, JSON errors (+ JSON для /v1/*, статус 422)
│   │   ├── csrf.py                   # CSRF-токены для форм (пропускает /v1/*)
│   │   └── api_auth.py               # Bearer-аутентификация /v1/* (v6.0)
│   │
│   ├── /routes/
│   │   ├── __init__.py               # setup_routes(app) — регистрация всех маршрутов
│   │   ├── auth.py                   # /auth/login, /auth/register, /auth/logout
│   │   ├── dashboard.py              # /dashboard — главная панель
│   │   ├── schemas.py                # /schemas — CRUD схем
│   │   ├── upload.py                 # /upload — загрузка файлов
│   │   ├── tasks.py                  # /tasks — статусы, скачивание результатов
│   │   ├── admin.py                  # /admin — управление пользователями
│   │   ├── categories.py             # /api/categories — AJAX поиск категорий XML
│   │   ├── websocket.py              # /ws/tasks/{id} — real-time прогресс
│   │   ├── api.py                    # /api/* — JSON API для AJAX
│   │   ├── v1_api.py                 # /v1/mapping-tasks — внешний REST API агента (v6.0)
│   │   └── agent.py                  # /agent — дашборд оператора агента (v6.0, admin+)

│   │
│   ├── /templates/
│   │   ├── base.html                 # Layout: навигация (2 группы + SVG-иконки), Inter +
│   │   │                             # Tailwind CDN, flash, scripts (v6.1)
│   │   ├── /auth/
│   │   │   ├── login.html
│   │   │   └── register.html
│   │   ├── dashboard.html
│   │   ├── /schemas/
│   │   │   ├── list.html
│   │   │   ├── detail.html           # Просмотр схемы (группы сопоставлений)
│   │   │   ├── create.html           # Wizard стандартной схемы
│   │   │   ├── create_mvm.html       # Wizard МВМ-схемы (Фаза будущая)
│   │   │   └── edit.html             # Редактирование сопоставлений (Фаза будущая)
│   │   ├── /upload/
│   │   │   └── index.html            # Drag&drop + выбор схемы
│   │   ├── /tasks/
│   │   │   └── list.html             # Задачи + прогресс
│   │   ├── /admin/
│   │   │   └── users.html            # Whitelist управление
│   │   └── /agent/                   # Дашборд AI-агента (v6.0)
│   │       ├── list.html             # История заданий FDM: поиск, пагинация
│   │       └── detail.html           # Детализация: связки, confidence, unresolved

│   │
│   └── /static/
│       ├── /css/
│       │   └── style.css             # Кастомные стили: nav-link/nav-icon, badges,
│       │                             # drop-zone, progress, modal (Tailwind через CDN)
│       └── /js/                      # Зарезервировано для будущего выноса JS
│
├── /shared/                          # === ОБЩИЕ АДАПТЕРЫ ===
│   ├── __init__.py
│   ├── delivery.py                   # ResultDelivery (ABC), TelegramDelivery, WebDelivery,
│   │                                 # ResultDeliveryFactory. Импорты Bot/Task через TYPE_CHECKING
│   │                                 # для избежания циклических зависимостей.
│   └── notifications.py              # NotificationService (Telegram / WebSocket)
│
├── /services/                        # === БИЗНЕС-ЛОГИКА ===
│   ├── data_synchronizer.py          # Оркестратор синхронизации (Facade)
│   ├── ai_comparator.py              # AI-сопоставление столбцов (3 МП + МВМ 4 источника)
│   ├── task_queue.py                 # Очередь задач: Task (dataclass), TaskQueue (Repository),
│   │                                 # RedisTaskQueue, InMemoryTaskQueue, фабрика create_task_queue()
│   ├── task_worker.py                # Фоновый воркер: читает очередь, обрабатывает Task через
│   │                                 # DataSynchronizer, отправляет результат пользователю.
│   │                                 # _FileCleanupService — раз в 24 ч удаляет файлы старше FILE_MAX_AGE_DAYS
│   ├── /mapping/                     # === AI-АГЕНТ МАППИНГА PIM+FDM (v6.0) ===
│   │   ├── __init__.py               # Пакет, экспорты
│   │   ├── models.py                 # Dataclass-модели задач/результатов + to_dict() по протоколу
│   │   ├── validators.py             # Валидация payload (MappingValidationError: 400/422, путь до поля)
│   │   ├── attribute_mapper.py       # Стратегия attribute_mapping + sanitize_confidence/truncate_comment
│   │   ├── reference_value_mapper.py # Стратегия reference_value_mapping (гарантия полноты matches)
│   │   └── job_worker.py             # MappingJobWorker: цикл очереди из БД, таймауты, graceful stop
│   └── /sync/                        # Подпакет компонентов синхронизации (v4.2)
│       ├── __init__.py               # Экспорты подпакета
│       ├── value_converter.py        # ValueConverter — единицы измерения и конвертация
│       ├── ai_validator.py           # AiValidator — 5-уровневая валидация через AI
│       ├── article_aligner.py        # ArticleAligner — выравнивание артикулов
│       ├── dimensions_synchronizer.py # DimensionsSynchronizer — синхронизация габаритов
│       ├── column_syncer.py          # ColumnSyncer — синхронизация столбцов МП
│       ├── photo_syncer.py           # PhotoSyncer — синхронизация фото-ссылок между МП
│       ├── xml_syncer.py             # XmlSyncer — заполнение МП из XML-каталога
│       └── excel_io.py               # ExcelFileManager — загрузка и сохранение xlsx
│
├── /utils/
│   ├── excel_reader.py               # Чтение Excel файлов
│   ├── xml_reader.py                 # Чтение XML файлов каталога (YML-фид)
│   ├── excel_writer.py               # Создание отчётов Excel
│   ├── logger_config.py              # Конфигурация логирования
│   └── utils.py                      # Вспомогательные функции (download_file_by_url и др.)
│
├── /database/
│   ├── database.py                   # PostgreSQL ORM (asyncpg, connection pool)
│   └── migrations.py                 # Идемпотентные миграции таблиц
│
├── /config/
│   └── config.py                     # Конфигурация (API keys, маппинги, DB/Redis/Web)
│
├── /nginx/
│   └── ecommpedia.conf               # Nginx: SSL, reverse proxy, static, WebSocket
│
├── /scripts/
│   ├── setup_ssl.sh                  # certbot --nginx -d ecommpedia.ru
│   └── deploy.sh                     # git pull + pip install + systemctl restart
│
└── /systemd/
    └── marketplace-bot.service       # Systemd unit (бот + веб в одном процессе)
```

---

## Жизненный цикл приложения

Порядок запуска в `bot/bot.py → start_bot()`:

1. **Валидация конфигурации** — `Config.validate()` бросает `ValueError` при отсутствии обязательных переменных. Если `WEB_HOST` задан, проверяет наличие `WEB_SECRET_KEY`.

2. **Инициализация хранилищ** — `await init_storage()`:
   - Создаёт `Database(...)` → `await db.connect()` → connection pool PostgreSQL
   - `await run_migrations(db.pool)` — создаёт все таблицы (включая новые `web_users`, `web_sessions`, `task_results`)
   - `await session_storage.connect()` — Redis для Telegram-сессий

3. **Инициализация очереди задач** — `await task_queue.connect()`:
   - `RedisTaskQueue` или `InMemoryTaskQueue` (fallback)

4. **Создание AIComparator** — один экземпляр на весь жизненный цикл. Промпты читаются с диска один раз.

5. **Создание бота** — `create_bot(task_queue, ai_comparator)`:
   - Регистрация Telegram middleware и handlers
   - `ai_comparator` → `dp["ai_comparator"]` для DI в хендлеры

6. **[Условно] Создание веб-приложения** (если `WEB_HOST` задан):
   - Создание `WebSocketManager` — хранит активные WS-соединения `{task_id: [connections]}`
   - `create_web_app(task_queue, ai_comparator, ws_manager)` — инициализация `aiohttp.web.Application`
   - Jinja2 templates, middleware (errors → auth → csrf), routes
   - Shared resources в app context: `task_queue`, `ai_comparator`, `ws_manager`, `db`
   - Запуск `web.TCPSite(runner, WEB_HOST, WEB_PORT)`
   - Graceful degradation: если ImportError (нет веб-зависимостей) или ошибка — бот продолжает без веба

7. **Запуск TaskWorker** — `await task_worker.start(bot)`:
   - `ws_manager` уже передан в конструктор `TaskWorker(..., ws_manager=ws_manager)`
   - `bot` передаётся в `start(bot)` — сохраняется для `TelegramDelivery`
   - `ResultDeliveryFactory` выбирает стратегию по `task.delivery_channel`
   - `_FileCleanupService` — очистка файлов каждые 24ч
   - `Semaphore(MAX_CONCURRENT_TASKS)`

7.1. **[Условно] Запуск MappingJobWorker** (v6.0, если `FDM_API_TOKEN` задан):
   - Создаётся с общим `ai_comparator` из шага 4 — НЕ собственный (глобальный семафор AI-запросов)
   - `recover_stale_mapping_jobs(0)` — зависшие processing-задания (следы падения процесса) → failed
   - Основной цикл: `claim_pending_mapping_job()` (FOR UPDATE SKIP LOCKED, FIFO) + маршрутизация по `task_type` в мапперы
   - Цикл обслуживания: раз в 24 ч `cleanup_old_mapping_jobs(AGENT_JOBS_RETENTION_DAYS)`
   - Graceful degradation: ошибка запуска агента (нет промпта, сбой БД) — бот и веб продолжают работу, `/v1/*` отвечает 503

8. **Параллельный запуск**:
   ```python
   runner = web.AppRunner(web_app)
   await runner.setup()
   site = web.TCPSite(runner, Config.WEB_HOST, Config.WEB_PORT)
   await site.start()
   # Бот + веб работают одновременно:
   await dp.start_polling(bot)
   ```

9. **Graceful shutdown** — строгий порядок:
   - `await runner.cleanup()` — остановка веб-сервера
   - `await task_worker.stop()` — ожидание задач → `comparator.close()` → `cleanup_service.stop()`
   - `await mapping_worker.stop()` — воркер агента: grace 30 сек, прерванные задания → failed (v6.0; пишет в БД — обязан завершиться до закрытия пула; Redis-очередь не использует)
   - `await task_queue.disconnect()` — Redis
   - `await shutdown_storage()` — PostgreSQL pool

---

## Система прав доступа (общая для бота и веба)

### Иерархия ролей

| Роль | Telegram | Web | Права |
|---|---|---|---|
| Owner | `ACCESS_OWNER_ID` из `.env` | `role='owner'` в `web_users` | Полный контроль |
| Admin | `system_settings` или `.env` | `role='admin'` | Управление whitelist, все схемы |
| Editor | whitelist `role='editor'` | `role='editor'` | Все схемы (чтение/запись), свои CRUD |
| User | whitelist `role='user'` | `role='user'` | Только свои схемы, загрузка, статистика |

### Telegram: AccessManager (`bot/access.py`)

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

### Web: WebAccessManager (`web/auth/permissions.py`)

Адаптер, который преобразует проверки из контекста `web_user_id`:

```python
class WebAccessManager:
    """
    Все методы принимают user_data (Dict из request["user"]),
    а не web_user_id. Это исключает лишние запросы в БД —
    данные уже загружены auth middleware.
    """

    @staticmethod
    def get_role(user_data: Optional[Dict]) -> str: ...
    @staticmethod
    def can_see_all_schemas(user_data: Optional[Dict]) -> bool: ...
    @staticmethod
    def can_manage_users(user_data: Optional[Dict]) -> bool: ...
    @staticmethod
    def is_owner(user_data: Optional[Dict]) -> bool: ...
    @staticmethod
    def is_admin_or_owner(user_data: Optional[Dict]) -> bool: ...
    @staticmethod
    def can_upload_files(user_data: Optional[Dict]) -> bool: ...
    @staticmethod
    def can_delete_schema(user_data: Optional[Dict], schema_owner_id: int) -> bool: ...
    @staticmethod
    def has_minimum_role(user_data: Optional[Dict], minimum_role: str) -> bool: ...
```

### Регистрация веб-пользователей

- По умолчанию: закрытая регистрация (`WEB_REGISTRATION_OPEN=false`)
- Создание аккаунтов — через admin-панель (owner/admin)
- При открытой регистрации — новый пользователь получает `role='user'`
- После успешной регистрации — автоматический вход (создание сессии + cookie)
- Привязка Telegram → проверка `whitelist_users` и синхронизация роли
- Owner (`ACCESS_OWNER_ID`) автоматически получает `role='owner'` на вебе

---

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
  - user_id (FK, nullable)           # Telegram user_id
  - web_user_id (FK, nullable)       # Веб-пользователь (миграция 006)
  - schema_name
  - schema_type TEXT DEFAULT 'standard'  -- 'standard' или 'mvm'
  - created_at, updated_at (TIMESTAMPTZ)
  - full_comparison_json TEXT        -- полный результат AI (основное хранилище)
  -- Partial unique indexes:
  --   idx_schemas_unique_user_name ON (user_id, schema_name) WHERE user_id IS NOT NULL
  --   idx_schemas_unique_web_user_name ON (web_user_id, schema_name) WHERE web_user_id IS NOT NULL

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

web_users               # Веб-пользователи (миграция 003)
  - id (SERIAL PK)
  - email TEXT UNIQUE NOT NULL
  - password_hash TEXT NOT NULL
  - display_name TEXT
  - telegram_user_id BIGINT UNIQUE
  - role TEXT NOT NULL DEFAULT 'user'
  - is_active BOOLEAN DEFAULT TRUE
  - created_at TIMESTAMPTZ DEFAULT NOW()
  - last_login_at TIMESTAMPTZ
  - CONSTRAINT web_users_role_check CHECK (role IN ('owner', 'admin', 'editor', 'user'))

web_sessions            # Веб-сессии (миграция 004)
  - id TEXT PRIMARY KEY
  - web_user_id INTEGER REFERENCES web_users(id) ON DELETE CASCADE
  - created_at TIMESTAMPTZ DEFAULT NOW()
  - expires_at TIMESTAMPTZ NOT NULL
  - ip_address TEXT
  - user_agent TEXT

task_results            # Результаты задач для веба (миграция 005)
  - id (SERIAL PK)
  - task_id TEXT UNIQUE NOT NULL
  - web_user_id INTEGER REFERENCES web_users(id)
  - status TEXT NOT NULL DEFAULT 'pending'
  - output_files JSONB
  - report_path TEXT
  - stats JSONB
  - created_at TIMESTAMPTZ DEFAULT NOW()
  - completed_at TIMESTAMPTZ
  - error_message TEXT

mapping_jobs            # Задания AI-агента маппинга PIM+FDM (миграция 007, v6.0)
  - id TEXT PK                       -- jobId (secrets.token_hex(16), 32 hex-символа)
  - task_type TEXT                   -- 'attribute_mapping' | 'reference_value_mapping' (CHECK)
  - schema_id BIGINT NOT NULL        -- ИД схемы FDM (внешняя система, НЕ FK)
  - status TEXT DEFAULT 'pending'    -- pending → processing → completed | failed (CHECK; 'cancelled' зарезервирован)
  - payload JSONB NOT NULL           -- исходный JSON запроса FDM
  - result JSONB                     -- итоговый JSON по протоколу
  - channels JSONB DEFAULT '[]'      -- [{platform, name, schemaChannelId}] для дашборда
  - category_name TEXT               -- заголовок задания в дашборде (задача 1)
  - attribute_name TEXT              -- заголовок задания в дашборде (задача 2)
  - matched_count INTEGER            -- сопоставлено связок/значений
  - unresolved_count INTEGER         -- без соответствий
  - duration_sec REAL                -- completed_at − started_at (вычисляется в SQL)
  - error_message TEXT
  - created_at, started_at, completed_at TIMESTAMPTZ
  -- Индексы: (status, created_at) — очередь воркера; (created_at DESC) — дашборд; (schema_id) — поиск

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
- `003_create_web_users_table` — создаёт таблицу `web_users`
- `004_create_web_sessions_table` — создаёт таблицу `web_sessions`
- `005_create_task_results_table` — создаёт таблицу `task_results`
- `006_add_web_user_id_to_schemas` — добавляет `web_user_id` в `schemas`, делает `user_id` nullable, partial unique indexes
- `007_create_mapping_jobs_table` — таблица заданий AI-агента `mapping_jobs` + индексы (v6.0)


### Ключевые особенности реализации

- **`RETURNING id`** вместо `lastrowid` для получения ID вставленной записи
- **`ON CONFLICT ... DO UPDATE`** вместо `INSERT OR REPLACE`
- **`asyncpg.UniqueViolationError`** вместо `sqlite3.IntegrityError`
- **`result == 'DELETE 1'`** для проверки успешного удаления (asyncpg возвращает строку вида `'DELETE N'`)
- **Транзакции**: `async with conn.transaction()` для атомарных операций (например, `complete_processing`)
- **`full_comparison_json`** — основное хранилище сопоставлений; `schema_matches` — legacy для обратной совместимости

### Новые методы Database (для веба)

```python
# === Веб-пользователи ===
async def create_web_user(self, email, password_hash, display_name, role='user') -> int
async def get_web_user_by_email(self, email) -> Optional[Dict]
async def get_web_user_by_id(self, user_id) -> Optional[Dict]
async def update_web_user_last_login(self, user_id) -> None
async def link_telegram_to_web_user(self, web_user_id, telegram_user_id) -> bool
async def get_web_users_list(self, limit=50) -> List[Dict]
async def set_web_user_active(self, user_id, is_active) -> None

# === Схемы для веб-пользователей ===
async def create_schema_for_web_user(self, web_user_id, schema_name, schema_type='standard', telegram_user_id=None) -> Optional[int]
    # Создаёт схему с web_user_id. Если telegram_user_id передан — записывает оба ID.
async def get_web_user_schemas(self, web_user_id) -> List[Dict]
    # Находит схемы по web_user_id ИЛИ по привязанному telegram_user_id.
async def get_schema_by_name_for_web_user(self, web_user_id, schema_name) -> Optional[Dict]
    # Проверка уникальности имени для веб-пользователя (учитывает обе привязки).

# === Веб-сессии ===
async def create_web_session(self, web_user_id, session_id, expires_at, ip, ua) -> None
async def get_web_session(self, session_id) -> Optional[Dict]
    # Возвращает: session_id, web_user_id, expires_at, email, display_name, role, is_active, telegram_user_id
async def delete_web_session(self, session_id) -> None
async def delete_user_sessions(self, web_user_id) -> None
async def cleanup_expired_web_sessions(self) -> int

# === Результаты задач (для веб-скачивания) ===
async def create_task_result(self, task_id, web_user_id) -> None
async def update_task_result(self, task_id, status=None, output_files=None,
                              report_path=None, stats=None, error_message=None) -> None
async def get_task_result(self, task_id) -> Optional[Dict]
async def get_user_task_results(self, web_user_id, limit=20) -> List[Dict]
```

---

### Методы Database для AI-агента (v6.0)

```python
# === Задания агента (mapping_jobs) ===
async def create_mapping_job(self, job_id, task_type, schema_id, payload, channels,
                             category_name=None, attribute_name=None) -> None
    # Создаёт задание в статусе pending (вызывается POST /v1/mapping-tasks)
async def claim_pending_mapping_job(self) -> Optional[Dict]
    # Атомарно забирает старейшее pending-задание:
    # UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) — FIFO, конкурентная безопасность
    # Возвращает {job_id, task_type, schema_id, payload} или None
async def mark_mapping_job_completed(self, job_id, result, matched_count=None, unresolved_count=None) -> None
    # duration_sec вычисляется В SQL: EXTRACT(EPOCH FROM (NOW() - started_at))
async def mark_mapping_job_failed(self, job_id, error_message) -> None
async def get_mapping_job(self, job_id) -> Optional[Dict]
    # Для GET-поллинга FDM: {job_id, task_type, status, result, error_message}
async def get_mapping_jobs_list(self, search='', limit=50, offset=0) -> List[Dict]
    # Дашборд /agent: поиск по schema_id (текст) / category_name / attribute_name, новые сверху
async def get_mapping_jobs_count(self, search='') -> int
async def get_mapping_job_detail(self, job_id) -> Optional[Dict]
    # Полная запись с payload и result для страницы детализации
async def recover_stale_mapping_jobs(self, stale_seconds=0) -> int
    # Зависшие processing → failed; вызывается при старте MappingJobWorker
async def cleanup_old_mapping_jobs(self, retention_days) -> int
    # Удаляет completed/failed старше retention_days; активные не удаляются никогда
```

---


## AI-агент маппинга PIM+FDM (v6.0)

### Назначение

Внешняя система FDM (PIM) присылает через REST API задания на AI-сопоставление:
- **attribute_mapping** — атрибуты конечной категории ↔ атрибуты категорий каналов (маркетплейсов)
- **reference_value_mapping** — значения справочника атрибута ↔ значения справочников каналов (в контексте одной связки атрибута)

Результаты (связки с confidence + нераспознанное) возвращаются FDM для создания маппингов в их системе. История доступна оператору через дашборд `/agent`.

### Протокол HTTP

| Метод | URL | Назначение | Ответ |
|---|---|---|---|
| POST | `/v1/mapping-tasks` | Создать задание | `202 {jobId, status: "pending"}` |
| GET | `/v1/mapping-tasks/{jobId}` | Статус/результат (поллинг) | `200` (см. ниже) |
| DELETE | `/v1/mapping-tasks/{jobId}` | Не реализован (отложен) | `405` от aiohttp |

Аутентификация: заголовок `Authorization: Bearer <FDM_API_TOKEN>` обязателен на все `/v1/*`.

GET-ответы по статусам:
- `pending` / `processing` → `{"jobId", "status"}`
- `completed` → `{"jobId", "status", ...result}` — ключи result разворачиваются на верхний уровень (`results`+`unresolved` для задачи 1, `channels` для задачи 2)
- `failed` → `{"jobId", "status", "error": "<причина>"}`

Коды ошибок POST: 400 (тело не JSON), 422 (семантические ошибки валидации — с путём до поля), 401 (нет/невалиден токен), 503 (агент выключен: `FDM_API_TOKEN` пуст). Все ответы — JSON (включая 404/405/500 на `/v1/*` — см. errors-middleware).

Рекомендации FDM: поллинг каждые 3 сек, бюджет 5 минут — сверх него задание гарантированно в терминальном статусе (таймаут обработки 240 сек).

### Поток обработки

FDM → POST /v1/mapping-tasks (Bearer)
→ валидация payload (validators.py; 400/422)
→ create_mapping_job → mapping_jobs (pending) → 202 {jobId}
FDM → GET /v1/mapping-tasks/{jobId} каждые 3 сек

MappingJobWorker (цикл, пауза 5 сек при пустой очереди)
→ claim_pending_mapping_job (FOR UPDATE SKIP LOCKED, FIFO)
→ parse_mapping_task → маршрутизация по task_type
→ AttributeMapper | ReferenceValueMapper
→ ОДИН AI-запрос через общий AIComparator (call_ai_json)
→ пост-валидация всех ID против входных данных
→ mark_mapping_job_completed(result, счётчики) | failed(error)
→ FDM получает result при следующем GET


### Валидация payload (`services/mapping/validators.py`)

- Разбор JSON → dataclass-модели (`services/mapping/models.py`); ошибки — `MappingValidationError(status, message)` с путём до поля: `channels[3].attributes[10].channelAttributeId: обязательное поле`
- Лимиты защиты промпта (Config, override в .env): `AGENT_MAX_ATTRIBUTES=100`, `AGENT_MAX_CHANNELS=20`, `AGENT_MAX_CHANNEL_ATTRIBUTES=500`, `AGENT_MAX_REFERENCE_VALUES=1000`, `AGENT_MAX_REFERENCE_CHANNEL_VALUES=2000`
- Проверки: уникальность mappingId/channelAttributeId/schemaChannelId, согласованность reference_type и полей задачи 2

### Мапперы (паттерн Strategy, общий контракт: задача → один AI-запрос → пост-валидация → result)

**AttributeMapper** (`attribute_mapper.py`, промпт `prompts/attribute_mapping.txt`):
- Плейсхолдеры промпта: `category_name`, `category_path`, `attributes_json`, `channels_json`
- LLM возвращает `matches[]`: `{mappingId, confidence, comment, channelMatches[{schemaChannelId, channelAttributeId, confidence}]}`
- Пост-валидация: mappingId существует во входных атрибутах; schemaChannelId/channelAttributeId существуют и в ЭТОМ канале; атрибут канала используется один раз по результату; связка без валидных channelMatches → unresolved
- `infomodelAttributeId` берётся из входных данных по mappingId (LLM его не возвращает)
- `unresolved` вычисляется детерминированно: все входные mappingId минус решённые — списку AI не доверяется

**ReferenceValueMapper** (`reference_value_mapper.py`, промпт `prompts/reference_value_mapping.txt`):
- Плейсхолдеры: `attribute_name`, `attribute_slug`, `reference_type`, `category_values_json`, `channels_json`
- LLM возвращает `channels[].matches[]`: `{infoValue, channelValue, channelValueId, confidence}`
- Пара (channelValueId, channelValue) обязана ссылаться на ОДНО значение справочника канала — любое несовпадение → null (`channelValue` сохраняется в FDM как есть, ошибка привязки хуже пустой)
- Полнота гарантируется кодом: ровно одна запись на каждое значение категории в каждом канале; недостающие/отклонённые → детерминированные null
- Быстрый путь: все справочники каналов пусты → null-результат без AI-запроса

**Общие хелперы** (публичные функции `attribute_mapper.py`): `sanitize_confidence()` (число 0..1, проценты 87 → 0.87), `truncate_comment()` (≤200 символов).

### MappingJobWorker (`services/mapping/job_worker.py`)

- Параллелизм: `Semaphore(AGENT_MAX_CONCURRENT_JOBS=3)` — слот берётся ПОСЛЕ claim и ДО создания Task (backpressure)
- Таймаут: `wait_for(AGENT_JOB_TIMEOUT_SEC=240)` → failed «Превышен таймаут обработки» (укладывается в 5-минутный бюджет поллинга FDM)
- Отличие таймаута от остановки: таймаут → TimeoutError → «таймаут»; stop() → CancelledError → «воркер остановлен» — FDM в обоих случаях получает внятный error
- Остановка: grace 30 сек, затем отмена остатка; `_safe_fail` пишет причину best-effort
- Обслуживание: раз в 24 ч `cleanup_old_mapping_jobs(AGENT_JOBS_RETENTION_DAYS=30)`
- При старте: `recover_stale_mapping_jobs(0)` — processing-задания от упавшего процесса → failed

### Публичный API AIComparator (v6.0)

```python
await comparator.call_ai(prompt, model=None, temperature=None) -> str
await comparator.call_ai_json(prompt, model=None, temperature=None) -> Dict
# Семафор(5), retry с backoff, прокси — общие со синхронизацией файлов.
# model=None → AI_MODEL; мапперы передают AGENT_AI_MODEL (пусто → None → общая модель).
```

Безопасность: два независимых контура
Сайт (/auth, /schemas, /agent, ...)	API (/v1/*)
Аутентификация	cookie MARKETPLACE_SESSION	Bearer FDM_API_TOKEN
Защита форм	CSRF (Double Submit Cookie)	не нужна — Bearer-заголовок сам доказывает происхождение
Middleware	auth + csrf (обе пропускают /v1/*)	api_auth (constant-time, 401/503)
secrets.compare_digest — исключает timing-атаки; WWW-Authenticate: Bearer по RFC 6750
Отказы логируются с IP; текст единый — без раскрытия причин (защита от разведки)
Пустой FDM_API_TOKEN → 503 на все /v1/* — безопасный деплой по умолчанию
HTTP→HTTPS редирект nginx: токен ходит только по TLS
Дашборд оператора (/agent)
GET /agent — таблица: jobId (обрезан до 12 симв.), заголовок, тип, схема #, каналы, дата, длительность, статус-бейдж, счётчики ✓/?; поиск по schema_id/категории/атрибуту; пагинация 20/стр
GET /agent/{job_id} — шапка задания, причина ошибки (failed), таблица связок (атрибут → соответствия по каналам, confidence-бейджи ≥85%/≥70%/ниже, комментарий LLM), блок unresolved-чипов; для задачи 2 — матрица «значение категории × каналы»
Доступ: @admin_required (owner/admin); пункт меню «Агент» в base.html внутри условия owner/admin
Названия атрибутов/каналов восстанавливаются из payload по ID (result хранит только ID по протоколу)
Мониторинг
Access-лог nginx: /var/log/nginx/agent_api_access.log (ротация стандартным logrotate)
Логгеры приложения: mapping.job_worker, mapping.attribute_mapper, mapping.reference_value_mapper, web.routes.v1_api
Дашборд /agent — история с длительностями и счётчиками

### Безопасность: два независимых контура

| | Сайт (/auth, /schemas, /agent, ...) | API (/v1/*) |
|---|---|---|
| Аутентификация | cookie MARKETPLACE_SESSION | Bearer FDM_API_TOKEN |
| Защита форм | CSRF (Double Submit Cookie) | не нужна — Bearer-заголовок сам доказывает происхождение |
| Middleware | auth + csrf (обе пропускают /v1/*) | api_auth (constant-time, 401/503) |

- `secrets.compare_digest` — исключает timing-атаки; `WWW-Authenticate: Bearer` по RFC 6750
- Отказы логируются с IP; текст единый — без раскрытия причин (защита от разведки)
- Пустой FDM_API_TOKEN → 503 на все /v1/* — безопасный деплой по умолчанию
- HTTP→HTTPS редирект nginx: токен ходит только по TLS

### Дашборд оператора (/agent)

- `GET /agent` — таблица: jobId (обрезан до 12 симв.), заголовок, тип, схема #, каналы, дата, длительность, статус-бейдж, счётчики ✓/?; поиск по schema_id/категории/атрибуту; пагинация 20/стр
- `GET /agent/{job_id}` — шапка задания, причина ошибки (failed), таблица связок (атрибут → соответствия по каналам, confidence-бейджи ≥85%/≥70%/ниже, комментарий LLM), блок unresolved-чипов; для задачи 2 — матрица «значение категории × каналы»
- Доступ: @admin_required (owner/admin); пункт меню «Агент» в base.html внутри условия owner/admin
- Названия атрибутов/каналов восстанавливаются из payload по ID (result хранит только ID по протоколу)

### Мониторинг

- Access-лог nginx: /var/log/nginx/agent_api_access.log (ротация стандартным logrotate)
- Логгеры приложения: mapping.job_worker, mapping.attribute_mapper, mapping.reference_value_mapper, web.routes.v1_api
- Дашборд /agent — история с длительностями и счётчиками


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

---

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
5. Синхронизация остальных столбцов по схеме (`ColumnSyncer.sync_all_matches`):
   5.1. Тройные совпадения (все 3 МП)
   5.2. Парные совпадения (все комбинации двух МП)
   5.3. Фото-столбцы (`PhotoSyncer.sync_photos`) — специальная логика
        разбивки и сборки ссылок для каждого МП
6. [МВМ] Заполнение МП из XML-каталога (`XmlSyncer.sync_from_xml` + `sync_dimensions_from_xml`)
7. Сохранение МП-файлов (`ExcelFileManager.save_results` с `changes_log` для точечной AI-валидации)

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
- Яндекс: раздельные столбцы в сантиметрах ("Длина, см *", "Ширина, см *", "Высота, см *")

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

Пары МП описаны декларативно в константе _PAIR_CONFIGS — добавить новую пару значит
дописать одну строку в список. Габаритные столбцы, исключённые столбцы и фото-столбцы
пропускаются автоматически. Фото-столбцы делегируются в PhotoSyncer — стандартная
логика «скопировать значение как есть» для них неприменима.

Специальная обработка ТН ВЭД: при записи значения ТНВЭД в WB или Яндекс из Ozon извлекается только числовой код ("2103909009 - Описание..." → "2103909009"). Столбцы ТНВЭД и МП с числовым форматом настраиваются в Config.TNVED_COLUMN_NAMES и Config.TNVED_NUMERIC_ONLY_MARKETPLACES.

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

Поле [XML] dimensions обрабатывается отдельно в sync_dimensions_from_xml — оно требует парсинга строки «д/ш/в» и конвертации единиц для каждого МП. Для Яндекс значения записываются в три раздельных столбца ("Длина, см *", "Ширина, см *", "Высота, см *").

#### ExcelFileManager (`services/sync/excel_io.py`)

Загружает xlsx через openpyxl (с сохранением data validation и стилей), сохраняет результаты обратно в файлы, записывает лист «AI_Логи».

```python
manager = ExcelFileManager(ai_comparator=comparator)
dfs = manager.load_all_dataframes(file_paths)
# После загрузки доступны:
# manager.column_validations    — {маркетплейс: {столбец: [значения]}}
# manager.original_column_names — маппинг переименованных дубликатов
# manager.original_file_paths   — пути к исходным файлам

await manager.save_results(dfs, output_paths, ai_validation_log, changes_log=changes_log)
manager.create_ai_log_in_report(report_path, ai_validation_log)
```

Обрабатывает дублирующиеся заголовки: добавляет числовой суффикс при загрузке («Вес (кг)1») и восстанавливает оригинальные имена при сохранении.

**Важно — validation-списки при сохранении (v4.6, обновлено v4.8):**
`save_results` использует `self.column_validations` (заполняется при `load_all_dataframes`)
для проверки допустимых значений ячеек. Это O(1) dict-lookup вместо повторного
сканирования всех DV-правил листа для каждой ячейки. `_get_validation_list_values`
используется только внутри `_load_column_validations` — не вызывать в цикле по ячейкам.

**Оптимизация AI-валидации при сохранении (v4.8):**
`save_results` принимает опциональный параметр `changes_log`. Если передан —
AI-валидация применяется ТОЛЬКО к ячейкам, которые были изменены ботом при
синхронизации (определяются по `changes_log`). Ячейки, заполненные пользователем
изначально, записываются без AI-проверки. Это устраняет избыточные AI-запросы:
вместо проверки всех непустых ячеек (до 500 000) проверяются только изменённые (обычно 100-500).

#### PhotoSyncer (`services/sync/photo_syncer.py`)

Синхронизирует фото-ссылки между маркетплейсами с учётом того, что у каждого МП
разная структура фото-столбцов:
    - WB:     один столбец «Фото» — все ссылки через «;»
    - Ozon:   два столбца — «Ссылка на главное фото*» и
              «Ссылки на дополнительные фото» (через пробел)
    - Яндекс: один столбец «Ссылка на изображение *» — все ссылки через «,»

Вызывается из ColumnSyncer.sync_all_matches() последним шагом — после тройных
и парных совпадений.

Логика переноса:
    Ozon → WB:      главное + дополнительные → один столбец через «;»
    Ozon → Яндекс:  главное + дополнительные → один столбец через «,»
    WB → Ozon:      первая ссылка → «Ссылка на главное фото*»,
                    остальные → «Ссылки на дополнительные фото» через пробел
    Яндекс → Ozon:  первая ссылка → «Ссылка на главное фото*»,
                    остальные → «Ссылки на дополнительные фото» через пробел

Во всех случаях трогаются только пустые ячейки — заполненные данные пользователя
не перезаписываются.

Настройки столбцов и разделителей задаются в Config.PHOTO_COLUMNS,
Config.PHOTO_READ_SEPARATORS, Config.PHOTO_WRITE_SEPARATORS.

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
  Столбец «Фото» синхронизируется через PhotoSyncer (не через стандартный механизм).
- **Ozon**: все значения через `;`
- **Яндекс**: все значения через `,`

### 8. Специальная обработка

- **Габариты**: раздельные столбцы для всех МП (WB — см, Ozon — мм, Яндекс — см)
- **Единицы измерения**: автоконвертация (мм ↔ см, г ↔ кг) через `ValueConverter`
- **Артикулы**: выравнивание по всем маркетплейсам через `ArticleAligner`
- **Фильтрация**: максимум 50 символов в артикуле
- **ТН ВЭД**: при синхронизации из Ozon в WB/Яндекс извлекается только числовой код (без пояснения)
- **Принудительные парные**: столбцы Видео (WB+Яндекс, Ozon=NA) и НДС (WB+Ozon, Яндекс=NA) жёстко заданы в конфигурации и не могут быть переопределены AI
- **Фото**: специальная логика разбивки и сборки ссылок через PhotoSyncer.
  Ozon является основным донором (два столбца). WB и Яндекс имеют по одному
  столбцу со всеми ссылками через разные разделители («;» и «,» соответственно).
  Фото-столбцы исключены из стандартной схемы сопоставлений AI —
  PhotoSyncer работает независимо от схемы, напрямую из Config.PHOTO_COLUMNS.

---

## AI Integration

### Настройка модели (.env)

```env
OPENROUTER_API_KEY=sk-or-v1-xxx
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=google/gemini-3-flash-preview
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
- `AIComparator.call_ai()` / `AIComparator.call_ai_json()` — `async def` (v6.0, публичный API для маппинга PIM+FDM; семафор и retry наследуются)
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

---

## TaskQueue и TaskWorker (v4.5–v5.1)

### TaskQueue (`services/task_queue.py`)
Абстракция над брокером задач (паттерн Repository). Поддерживает:
- `enqueue(task)` — постановка задачи в очередь (Redis LPUSH)
- `dequeue()` — блокирующее извлечение (Redis BRPOP, таймаут 5 сек)
- `update_status()` — обновление статуса и метаданных в Hash
- `get_user_tasks()` — история задач пользователя

### Task (`services/task_queue.py`)
Dataclass-модель задачи. Поля: `user_id`, `chat_id`, `task_type` (standard/mvm),
`schema_id`, `file_paths`, `output_dir`, `report_path`, `xml_file_path`,
`status` (pending/processing/completed/failed), `created_at`, статистика результатов.

**Добавлены в v5.0:**
```python
DeliveryChannel = Literal["telegram", "web"]

@dataclass
class Task:
    # ... все существующие поля без изменений ...
    delivery_channel: DeliveryChannel = "telegram"
    web_user_id: Optional[int] = None
```

Совместимость: старый код создаёт `Task` без этих полей → используются defaults. `from_json()` игнорирует неизвестные поля через `filtered = {k: v ... if k in known}`.

### TaskWorker (`services/task_worker.py`)
Фоновый сервис (паттерн Service Layer):
- `AIComparator` создаётся **один раз** в `__init__` — промпты читаются с диска при старте воркера, не на каждую задачу
- Читает задачи из очереди по одной
- Выполняет полный цикл: `DataSynchronizer` → `ExcelWriter` → отправка файлов
- Ограничение параллелизма: `asyncio.Semaphore(5)` — максимум 5 одновременных обработок
- Семафор `AIComparator` теперь глобальный: максимум 5 AI-запросов суммарно по всем задачам
- При shutdown корректно дожидается завершения активных задач, затем закрывает `AIComparator`
- `_FileCleanupService` запускается вместе с воркером — раз в 24 часа удаляет файлы
  старше `FILE_MAX_AGE_DAYS` дней из папок `UPLOAD_DIR`, `DOWNLOAD_DIR`, `OUTPUT_DIR`

**Изменения в v5.0:**
- Конструктор принимает `ws_manager: Optional[object] = None`
- `ResultDelivery` создаётся через `ResultDeliveryFactory.create(task, bot, ws_manager)` в начале `_execute_task`
- При невозможности создать канал доставки (неизвестный `delivery_channel`, отсутствие `bot`) — задача помечается `failed` без обработки
- Все файлы результатов (3 МП + отчёт) отправляются одним вызовом `delivery.send_files(result_files)`
- Метод `_notify_user` удалён — заменён на `delivery.send_progress/send_result/send_error`

### _FileCleanupService (`services/task_worker.py`)
Приватный класс фоновой очистки временных файлов (паттерн Single Responsibility).
- Интервал проверки: каждые 24 часа
- Срок хранения файлов: `Config.FILE_MAX_AGE_DAYS` (default=7 дней)
- Сканируемые директории: `Config.UPLOAD_DIR`, `Config.DOWNLOAD_DIR`, `Config.OUTPUT_DIR`
- Удаляет только файлы (`is_file()`), директории не трогает
- Тяжёлый обход файловой системы выполняется в `run_in_executor` — не блокирует event loop
- Логирует каждый удалённый файл и итоговую статистику (сколько файлов, сколько МБ)
- При недоступности директории — предупреждение в логах, остальные директории обрабатываются
- Запускается в `TaskWorker.start()`, останавливается в `TaskWorker.stop()` через `task.cancel()`

### Поток обработки

```
Пользователь нажимает "🚀 Обработать" (Telegram) или "Обработать" (Web)
↓
Хендлер создаёт Task и вызывает task_queue.enqueue(task)
↓
Пользователь получает: "✅ Задача принята. Задач в очереди: N" (Telegram)
                    или {"task_id": "uuid", "queue_position": N} (Web)
↓
TaskWorker извлекает задачу из очереди
↓
Semaphore(5) → DataSynchronizer.synchronize_data() с self._comparator
↓
Worker отправляет результат пользователю через ResultDelivery
   Telegram: Bot.send_document + Bot.send_message
   Web: WebSocket notify + update_task_result в БД
```

### Почему это решает проблему параллельных обработок
Раньше 5 пользователей одновременно запускали 5×N AI-запросов + 5 процессов pandas inline.
Теперь максимум 5 задач обрабатываются параллельно, остальные ждут в очереди.
Event loop бота остаётся свободным — бот отвечает на сообщения даже во время обработки.

---

## Веб-приложение: детали реализации

### `web/app.py` — создание Application

```python
async def create_web_app(
    task_queue: TaskQueue,
    ai_comparator: AIComparator,
    ws_manager: WebSocketManager,
) -> web.Application:
    """
    Создаёт aiohttp Application с полной настройкой.

    В app context сохраняются shared-ресурсы:
        app['task_queue']     — общая очередь задач
        app['ai_comparator']  — общий AI-компаратор
        app['ws_manager']     — менеджер WebSocket-соединений
        app['db']             — экземпляр Database из bot/storage.py
    """
```

### Маршруты (`web/routes/`)

| Метод | URL | Описание | Требует авторизации |
|---|---|---|---|
| GET | `/` | Редирект → `/dashboard` или `/auth/login` | — |
| GET | `/health` | Health-check (JSON: status, service) | Нет |
| GET | `/auth/login` | Форма входа | Нет |
| POST | `/auth/login` | Обработка входа | Нет |
| GET | `/auth/register` | Форма регистрации | Нет* |
| POST | `/auth/register` | Создание аккаунта | Нет* |
| POST | `/auth/logout` | Выход | Да |
| GET | `/dashboard` | Статистика, быстрые действия | Да |
| GET | `/schemas` | Список схем | Да (@login_required) |
| GET | `/schemas/{id}` | Детали схемы (группы сопоставлений) | Да |
| GET | `/schemas/create` | Страница создания стандартной схемы (wizard) | Да |
| POST | `/schemas/create` | Загрузка файлов + AI-сопоставление + сохранение (multipart) | Да |
| GET | `/schemas/create-mvm` | Wizard МВМ-схемы | Да |
| POST | `/schemas/create-mvm` | Создание МВМ | Да |
| GET | `/schemas/{id}` | Детали схемы (группы сопоставлений, read-only) | Да |
| GET | `/schemas/{id}/edit` | Страница редактирования (Фаза 4) | Да |
| POST | `/schemas/{id}/edit` | Сохранение изменений (Фаза 4) | Да |
| DELETE | `/api/schemas/{id}` | Удаление схемы (AJAX) | Да |
| GET | `/upload` | Страница загрузки | Да |
| POST | `/upload/files` | Multipart загрузка файлов | Да |
| POST | `/upload/process` | Запуск обработки → TaskQueue | Да |
| GET | `/tasks` | Список задач пользователя | Да |
| GET | `/api/tasks/{id}/status` | JSON-статус (polling fallback) | Да |
| GET | `/tasks/{id}/download/{filename}` | Скачивание результата | Да |
| GET | `/admin/users` | Управление whitelist | Admin+ |
| POST | `/admin/users/add` | Создать веб-аккаунт | Admin+ |
| POST | `/admin/users/toggle` | Блокировка/разблокировка пользователя | Admin+ |
| POST | `/admin/users/role` | Изменение роли пользователя | Admin+ |
| POST | `/api/categories/search` | Поиск категорий XML | Да |
| GET | `/ws/tasks/{id}` | WebSocket прогресса | Да |
| POST | `/v1/mapping-tasks` | Создание задания маппинга (внешний API FDM) | Bearer (api_auth) |
| GET | `/v1/mapping-tasks/{jobId}` | Статус/результат задания (поллинг FDM) | Bearer (api_auth) |
| GET | `/agent` | Дашборд заданий агента | Admin+ |
| GET | `/agent/{job_id}` | Детализация задания агента | Admin+ |


*Регистрация доступна только если `WEB_REGISTRATION_OPEN=true`

### Jinja2 глобальные функции

Регистрируются в `web/app.py → _setup_jinja2()`:

| Функция | Источник | Назначение |
|---------|----------|-----------|
| `_shorten_filename(filename)` | `web/routes/tasks.py` | Сокращает имя файла для кнопок скачивания (WB/Ozon/Яндекс/Отчёт) |

### Аутентификация

**Вход (логин):**
1. `POST /auth/login` с `email` + `password`
2. Проверка bcrypt hash
3. Создание записи в `web_sessions` (UUID, `expires_at`)
4. Установка подписанного cookie `MARKETPLACE_SESSION=<session_id>`
5. Редирект на `/dashboard`

**Middleware проверки:**
1. Проверяет путь — публичные (`/health`, `/auth/login`, `/auth/register`, `/static/`) пропускаются без проверки
2. Извлекает `session_id` из cookie `MARKETPLACE_SESSION`
3. `SELECT` из `web_sessions` JOIN `web_users` (проверка `expires_at` и `is_active`)
4. Загружает данные в `request['user']` (Dict или None)
5. Решение о блокировке принимают декораторы (`@login_required`, `@admin_required`), не middleware

**Связь с Telegram:**
- Поле `telegram_user_id` в `web_users` — опциональная привязка
- При привязке роль синхронизируется из `whitelist_users`
- Owner (`ACCESS_OWNER_ID`) автоматически получает `role='owner'` на вебе

### WebSocket прогресса

```javascript
// Фронтенд (web/static/js/websocket.js)
const ws = new WebSocket(`wss://ecommpedia.ru/ws/tasks/${taskId}`);
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    switch (data.type) {
        case 'progress': updateProgressBar(data.message); break;
        case 'files_ready': showDownloadButtons(data.count); break;
        case 'completed': showSuccess(data.message); break;
        case 'error': showError(data.message); break;
    }
};
```

```python
# Бэкенд (web/routes/websocket.py)
class WebSocketManager:
    """Хранит {task_id: [ws_response, ...]} для broadcast."""

    def __init__(self):
        self._connections: Dict[str, List[web.WebSocketResponse]] = {}

    async def connect(self, task_id: str, ws: web.WebSocketResponse) -> None: ...
    async def disconnect(self, task_id: str, ws: web.WebSocketResponse) -> None: ...
    async def notify(self, task_id: str, message: dict) -> None:
        """Отправляет JSON всем подписчикам задачи."""
```

### Загрузка файлов через веб

1. Drag&drop или `<input type="file" multiple>` на `/upload`
2. JavaScript отправляет `POST /upload/files` (multipart/form-data)
3. Сервер: определяет маркетплейс по имени файла (wb/ozon/yandex)
4. Сохраняет в `UPLOAD_DIR/{web_user_id}_{upload_id}/`, возвращает JSON: `{"files": {"wildberries": "/path/...", ...}, "xml": "/path/..." | null, "errors": [...]}`
5. Пользователь выбирает схему, нажимает "Обработать"
6. `POST /upload/process` → создаёт `Task(delivery_channel="web")` → `task_queue.enqueue()`
7. Ответ: `{"task_id": "uuid", "queue_position": int}`
8. Фронтенд подключает WebSocket, показывает прогресс
9. По завершении — кнопки "Скачать WB", "Скачать Ozon", "Скачать Яндекс", "Скачать отчёт"

### Интерфейс (v6.1)

Редизайн веб-интерфейса без изменения логики, URL и Python-кода (кроме регистрации
маршрутов агента — см. Правку 5). Изменены только `base.html`, `style.css`, `dashboard.html`.

**Навигация (`base.html`):**
- Две смысловые группы: рабочие разделы (Обзор, Схемы, Загрузка, Задачи) слева;
  служебные (Админ, Агент, только owner/admin) — справа за разделителем с подписью
  «УПРАВЛЕНИЕ»
- Пункт «Dashboard» переименован в «Обзор» (URL `/dashboard` НЕ менялся)
- SVG-иконки у каждого пункта (класс `nav-icon`), активный пункт — «пилюля»
  с тонким кольцом (`nav-link-active` + inset box-shadow)
- Логотип: градиентный бейдж + двухцветный «Marketplace**Sync**»
- Блок пользователя: аватар-инициал (первая буква display_name/email), имя и роль
  текстом (эмодзи-бейджи удалены)
- Шапка: `sticky` + полупрозрачный фон с `backdrop-blur`
- Мобильное меню повторяет группировку («Разделы» / «Управление»)

**Типографика:** шрифт Inter (400/500/600/700/800) через Google Fonts CDN,
подключён в `tailwind.config.fontFamily.sans`.

**Dashboard (`dashboard.html`):** шапка с главным действием (кнопка «Загрузить файлы»),
вертикальные карточки статистики, секции с капс-заголовками, статусы в таблице —
точка-индикатор в бейдже вместо эмодзи.

**Стили (`style.css`):** все имена классов СОХРАНЕНЫ (badge-*, drop-zone,
progress-bar-*, table-hover, modal-*, pulse-dot, skeleton, btn-icon,
truncate-text, scroll-hidden) — остальные шаблоны подхватили обновление без правок.
Новые классы: `nav-icon`, `nav-link-muted`.


---



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

Всего 11 обязательных сопоставлений (артикул, баркод, бренд, название, описание,
вес с упаковкой, вес без упаковки, высота, длина, ширина, цвет).

Фото намеренно исключены из MANDATORY_MATCHES — их синхронизация выполняется
отдельно через PhotoSyncer, потому что у каждого МП разная структура фото-столбцов
(WB: один столбец, Ozon: два столбца, Яндекс: один столбец).

### Принудительные парные сопоставления (FORCED_PAIR_ONLY_MATCHES)

Некоторые столбцы существуют только в 2 из 3 маркетплейсов. AI может ошибочно сопоставить их с произвольным столбцом 3-го МП. FORCED_PAIR_ONLY_MATCHES задаёт жёсткие правила:

| WB | Ozon | Яндекс | Описание |
|---|---|---|---|
| Видео | NA | Ссылка на видео | В Ozon нет поля видео в основном листе |
| Ставка НДС | НДС, %* | NA | В Яндекс нет поля НДС в основном листе |

Обработка в `AIComparator._enforce_forced_pairs()`:
- Если AI поместил эти столбцы в тройное — запрещённый МП удаляется, сопоставление перемещается в парное
- Если AI создал парное с запрещённым МП — оно удаляется
- Освободившийся столбец возвращается в уникальные (only_in_*)
- Правильное парное добавляется если отсутствует

### Столбцы ТН ВЭД (TNVED_COLUMN_NAMES)

В Ozon код ТНВЭД хранится с пояснением: "2103909009 - Прочие продукты для приготовления соуса...". WB и Яндекс принимают только числовой код: "2103909009".

При синхронизации из Ozon в WB/Яндекс ColumnSyncer автоматически извлекает только числовую часть. Настраивается через Config.TNVED_COLUMN_NAMES и Config.TNVED_NUMERIC_ONLY_MARKETPLACES.

### Исключённые столбцы

- Цена (все маркетплейсы)
- Rich-контент JSON
- Цена до скидки
- Старая цена
- SKU / SKU на Маркете / Артикул WB

---

## Конфигурация (.env) — полная

```env
# === Telegram Bot ===
TELEGRAM_BOT_TOKEN=xxx

# === OpenRouter API ===
OPENROUTER_API_KEY=sk-or-v1-xxx
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=google/gemini-3-flash-preview
AI_TEMPERATURE=0.1

# === PostgreSQL (обязательно) ===
DATABASE_URL=postgresql://bot_user:secret@localhost:5432/marketplace_sync
DATABASE_POOL_MIN_SIZE=2
DATABASE_POOL_MAX_SIZE=10

# === Redis (рекомендуется) ===
REDIS_URL=redis://localhost:6379/0

# === Права доступа ===
ACCESS_OWNER_ID=181957530
ACCESS_ADMIN_ID=436816068

# === Веб-сервер (опционально — если не задан WEB_HOST, веб не запускается) ===
WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_DOMAIN=ecommpedia.ru
WEB_SECRET_KEY=<сгенерировать: python3 -c 'import secrets; print(secrets.token_hex(32))'>
WEB_SESSION_MAX_AGE=86400
WEB_REGISTRATION_OPEN=false
WEB_CSRF_ENABLED=true

# === Логирование ===
LOG_FILE_PATH=./logs/app.log
LOG_LEVEL=INFO

# === Очередь задач ===
MAX_CONCURRENT_TASKS=5
TASK_QUEUE_KEY=bot:task_queue

# === Директории ===
UPLOAD_DIR=/root/progect/uploads
DOWNLOAD_DIR=/root/progect/downloads
OUTPUT_DIR=/root/progect/output
FILE_MAX_AGE_DAYS=7

# === AI-агент маппинга PIM+FDM (v6.0; ПУСТОЙ FDM_API_TOKEN = агент ВЫКЛЮЧЕН, /v1/* → 503) ===
FDM_API_TOKEN=            # python3 -c "import secrets; print(secrets.token_hex(32))"; тот же токен у FDM (FDM_AI_AGENT_API_KEY)
AGENT_AI_MODEL=           # модель для заданий маппинга (пусто = AI_MODEL)
AGENT_AI_TEMPERATURE=0.1
AGENT_MAX_CONCURRENT_JOBS=3
AGENT_POLL_INTERVAL_SEC=5
AGENT_JOB_TIMEOUT_SEC=240
AGENT_JOBS_RETENTION_DAYS=30
AGENT_MAX_ATTRIBUTES=100
AGENT_MAX_CHANNELS=20
AGENT_MAX_CHANNEL_ATTRIBUTES=500
AGENT_MAX_REFERENCE_VALUES=1000
AGENT_MAX_REFERENCE_CHANNEL_VALUES=2000

# === Прокси (опционально) ===
PROXY_ENABLED=false
PROXY_URL=http://user:pass@host:port

```

### Обязательные переменные

Бот не запустится без: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `DATABASE_URL`, `ACCESS_OWNER_ID`.

Веб не запустится без (если `WEB_HOST` задан): `WEB_SECRET_KEY`.

`Config.validate()` бросает `ValueError` при отсутствии обязательных параметров. Дополнительно: если `WEB_HOST` задан, но `WEB_SECRET_KEY` пустой — бросает `ValueError` с инструкцией генерации ключа. Предупреждение при `WEB_SESSION_MAX_AGE <= 0`.

---

## Логирование

### Конфигурация (v4.7)

Модуль `utils/logger_config.py` — единая точка логирования для всего проекта.

**Два форматтера:**
- `_ConsoleFormatter` — человекочитаемый вывод в stdout: `2026-05-24 17:26:48 | INFO  | сообщение | key=value`
  Показывает только ключи из белого списка: `path`, `file`, `error`, `error_type`, `step`, `current`, `total`, `directory`
- `_JsonFileFormatter` — полный JSON в файл: `timestamp`, `level`, `message`, `logger`, `trace_id`, `context`, `exception`

**Один файл на весь проект:**
`_install_file_handler()` добавляет `RotatingFileHandler` в корневой логгер **один раз** (флаг `_file_handler_installed`).
Все дочерние логгеры автоматически наследуют этот handler. Ротация: 10 МБ, 5 бэкапов.

**Путь к файлу и уровень — из `.env`:**
```env
LOG_FILE_PATH=./logs/app.log
LOG_LEVEL=INFO
```

### AppLogger — адаптер с trace_id и context

`setup_logger()` возвращает `AppLogger` (подкласс `logging.LoggerAdapter`).

```python
from utils.logger_config import setup_logger
logger = setup_logger('module_name')

# Базовое использование
logger.info("задача завершена")
logger.error("ошибка подключения", exc_info=True)

# С контекстом (попадает в файл и консоль)
logger.info("файл загружен", context={"file": "wb.xlsx", "total": 42})

# С trace_id для отслеживания цепочки
logger.info("запрос", context={"user_id": 123}, trace_id="abc-123")

# Создать новый trace для отдельной цепочки
trace_logger = logger.new_trace()
trace_logger.info("начало обработки")   # trace_id генерируется автоматически
```

**ВАЖНО:** `setup_logger()` больше не создаёт новый файл при каждом вызове.
До v4.7 каждый вызов создавал `sync_YYYYMMDD_HHMMSS.log` — теперь всегда один `app.log`.

---

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

---

## Middleware и безопасность

### AccessControlMiddleware (Telegram)

Регистрируется для всех `message` и `callback_query` событий:

```python
dp.message.middleware(AccessControlMiddleware())
dp.callback_query.middleware(AccessControlMiddleware())
```

- Проверяет `user_id` перед каждым обработчиком через `await AccessManager.has_access(user_id)`
- Блокирует неавторизованных пользователей
- Отправляет сообщение "⛔ У вас нет доступа"

### Web Middleware

**Порядок:** errors → auth → csrf → api_auth

- **errors** — внешний слой: обрабатывает 404, 500, JSON errors; для `/v1/*` и `/api/*` ВСЕГДА JSON-тело (включая 422 — статус семантических ошибок валидации API)
- **auth** — загружает `request["user"]` из cookie-сессии. НЕ блокирует запросы, только загружает данные. `/v1/*` пропускает без проверки
- **csrf** — Double Submit Cookie: cookie httponly=False + hidden field / X-CSRF-Token header. Сравнение через `secrets.compare_digest` (constant-time)
- **api_auth** — Bearer-аутентификация `/v1/*` (v6.0): `Authorization: Bearer <FDM_API_TOKEN>`, constant-time сравнение, 401 с `WWW-Authenticate` (RFC 6750), 503 при пустом токене. Прочие пути пропускает

CSRF отключается через `WEB_CSRF_ENABLED=false` — только для отладки.
CSRF НЕ проверяется для путей `/health`, `/ws/*`, `/v1/*`.


### Иерархия проверок

1. Middleware → загружает данные пользователя
2. Handler → дополнительные проверки через декораторы (`@login_required`, `@admin_required`, etc.)
3. Database → ограничения на уровне SQL (`UNIQUE`, `CHECK` constraints)

---

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

---

## Deployment

### Требования

```
aiogram==3.4.1 asyncpg==0.31.0 pandas==2.3.3 openpyxl==3.1.5 openai==2.14.0
python-dotenv==1.2.1 aiohttp==3.9.5 httpx==0.28.1 redis==7.4.0 aiofiles==23.2.1
socksio==1.0.0

# Web (новое)
aiohttp-jinja2==1.6 aiohttp-session==2.12.0 cryptography==43.0.0
bcrypt==4.2.0 jinja2==3.1.4
```

### Запуск

```bash
# Установка зависимостей
pip install -r requirements.txt

# PostgreSQL должен быть запущен и доступен по DATABASE_URL
# Миграции применяются автоматически при старте

# Запуск бота (для отладки)
python main.py
```

#### Запуск через systemd (production)

Сервис `/etc/systemd/system/marketplace-bot.service`:

```ini
[Unit]
Description=Marketplace Sync Bot + Web Server
After=network.target postgresql.service redis-server.service nginx.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/progect
ExecStart=/root/progect/venv/bin/python3 main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

#### Управление
```bash
systemctl start marketplace-bot      # Запуск
systemctl stop marketplace-bot       # Остановка
systemctl restart marketplace-bot    # Перезапуск
systemctl status marketplace-bot     # Статус
journalctl -u marketplace-bot -f     # Логи в реальном времени
```

Бот автоматически перезапускается при падении (Restart=always, задержка 5 сек).

### Переменные окружения

Создайте файл `.env` с обязательными параметрами (см. раздел "Конфигурация").
Без `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY` и `ACCESS_OWNER_ID` бот не запустится.

### Установка на новый сервер (Ubuntu 24.04)

```bash
# 1. Системные пакеты
sudo apt update && sudo apt install -y postgresql postgresql-contrib redis-server nginx

# 2. Создание БД
sudo -u postgres psql -c "CREATE USER bot_user WITH PASSWORD 'secret';"
sudo -u postgres psql -c "CREATE DATABASE marketplace_sync OWNER bot_user;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE marketplace_sync TO bot_user;"

# 3. Виртуальное окружение
cd /root/progect
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# 4. Зависимости
pip install aiogram==3.4.1 asyncpg==0.31.0 pandas==2.3.3 openpyxl==3.1.5 openai==2.14.0     python-dotenv==1.2.1 httpx==0.28.1 redis==7.4.0 aiohttp==3.9.5 aiofiles==23.2.1     socksio==1.0.0 aiohttp-jinja2==1.6 aiohttp-session==2.12.0 cryptography==43.0.0     bcrypt==4.2.0 jinja2==3.1.4

# 5. Директории
mkdir -p /root/progect/uploads /root/progect/downloads /root/progect/output /root/progect/logs

# 6. Файл .env — создать/перенести с обязательными параметрами

# 7. Миграция БД со старого сервера (опционально)
# На старом: pg_dump -U bot_user -h localhost marketplace_sync > /tmp/db_backup.sql
# Перенести файл, на новом: sudo -u postgres psql marketplace_sync < /root/db_backup.sql

# 8. SSL-сертификат
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d ecommpedia.ru -d www.ecommpedia.ru

# 9. Конфигурация Nginx
sudo cp /root/progect/nginx/ecommpedia.conf /etc/nginx/sites-available/ecommpedia
sudo ln -sf /etc/nginx/sites-available/ecommpedia /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo nginx -t && sudo systemctl reload nginx
# Smoke-тест API: ожидаем JSON 401
curl -s https://ecommpedia.ru/v1/mapping-tasks/abc123

# 9.1. Права Nginx на статику (ОБЯЗАТЕЛЬНО — иначе /static/ отдаёт 403)
# Nginx работает от www-data, а проект лежит в /root (закрыт для остальных пользователей)
sudo apt install -y acl
setfacl -m u:www-data:x /root
setfacl -R -m u:www-data:rX /root/progect/web/static
# Default-ACL: новые файлы (после git pull) автоматически получат права
setfacl -R -d -m u:www-data:rX /root/progect/web/static
# Проверка: должно вывести начало CSS-файла
sudo -u www-data head -3 /root/progect/web/static/css/style.css


# 10. Тестовый запуск
python3 main.py

# 11. Systemd сервис
systemctl daemon-reload
systemctl enable marketplace-bot
systemctl start marketplace-bot
```

### Nginx (`nginx/ecommpedia.conf`)

```nginx
# HTTP → HTTPS redirect
server {
    listen 80;
    server_name ecommpedia.ru www.ecommpedia.ru;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ecommpedia.ru www.ecommpedia.ru;

        # SSL (certbot)
    ssl_certificate /etc/letsencrypt/live/ecommpedia.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ecommpedia.ru/privkey.pem;

    # Внешний REST API AI-агента PIM+FDM (v6.0):
    # POST/GET /v1/mapping-tasks, Bearer-аутентификация в приложении.
    # Таймауты не задаются: POST → 202 мгновенно, обработка в фоновом воркере.
    location /v1/ {
        proxy_pass http://marketplace_bot_web;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 10M;
        access_log /var/log/nginx/agent_api_access.log;
    }

```

---

## Важные предостережения

### НЕ изменять без обсуждения

**Бот (существующее):**
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
- В `task_worker.stop()` порядок: активные задачи → `comparator.close()` → `cleanup_service.stop()`
- `_FileCleanupService` НЕ удаляет директории — только файлы (`is_file()`)
- Пути к директориям файлов берутся из `Config` (`UPLOAD_DIR`, `DOWNLOAD_DIR`, `OUTPUT_DIR`) — не хардкодить
- Порядок записи AI-лога: `synchronize_data` → `create_report_with_changes` → `create_ai_log_in_report`
- Хендлеры upload.py НЕ вызывают `synchronize_data()` inline — только `task_queue.enqueue()`
- `create_bot()` теперь принимает `task_queue: TaskQueue` и передаёт его в `register_upload_handlers()`
- Импорт сессий: `from bot import storage` → `storage.session_storage` (модуль, не переменная)
- `AIComparator` создаётся в `TaskWorker.__init__` — НЕ пересоздавать на каждую задачу
- `await comparator.close()` вызывается в `TaskWorker.stop()` — НЕ вызывать раньше (активные задачи ещё используют компаратор)
- Конфигурацию FORCED_PAIR_ONLY_MATCHES — принудительные парные сопоставления (Видео, НДС)
- Конфигурацию TNVED_COLUMN_NAMES и TNVED_NUMERIC_ONLY_MARKETPLACES — очистка кодов ТН ВЭД
- Конфигурацию PHOTO_COLUMNS, PHOTO_READ_SEPARATORS, PHOTO_WRITE_SEPARATORS —
  структуру фото-столбцов и разделители для каждого МП

**Веб (новое):**
- Бот работает через polling — НЕ переводить на webhook
- `ResultDelivery` — ровно 4 метода (`send_progress`, `send_files`, `send_result`, `send_error`)
- `ResultDeliveryFactory.create()` — единственное место выбора стратегии доставки
- `ws_manager` передаётся в конструктор `TaskWorker`, НЕ в метод `start()`
- Graceful degradation: при ImportError веб-модулей бот продолжает работать без веба
- WebSocket URI: `/ws/tasks/{task_id}`
- Cookie name: `MARKETPLACE_SESSION`
- Порядок middleware: errors → auth → csrf (errors — внешний слой)
- Nginx отдаёт `/static/` напрямую — aiohttp НЕ обслуживает статику в production
- Статика лежит в `/root/progect/web/static/`, Nginx читает её от www-data — ОБЯЗАТЕЛЬНЫ ACL-права (см. «Установка», шаг 9.1). После переноса проекта/переезда сервера проверять: `curl -s -o /dev/null -w "%{http_code}" https://ecommpedia.ru/static/css/style.css` → должно быть 200
- Nginx кэширует статику в браузерах (`expires 7d`) — при каждом изменении `style.css` повышать версию в ссылке в `base.html` (`style.css?v=N`) — иначе пользователи до 7 дней будут видеть старые стили
- `web_app['task_queue']` — та же очередь, что и бот (НЕ создавать отдельную)
- `web_app['ai_comparator']` — тот же экземпляр (НЕ создавать новый)
- Один event loop для бота и веба — НЕ разделять на процессы
- Auth middleware НЕ блокирует запросы — только загружает `request["user"]`. Блокировка — ответственность декораторов.
- `WebAccessManager` принимает `user_data: Dict` (из request["user"]), НЕ `web_user_id: int` — без лишних запросов к БД
- `PasswordHasher` всегда использует `asyncio.to_thread()` — НЕ вызывать bcrypt синхронно в event loop
- Публичные пути (`/health`, `/auth/login`, `/auth/register`, `/static/`) НЕ проверяют сессию
- CSRF: Double Submit Cookie (cookie httponly=False + hidden field / X-CSRF-Token header)
- CSRF отключается через `WEB_CSRF_ENABLED=false` — только для отладки
- CSRF НЕ проверяется для путей `/health`, `/ws/*`
- Скачивание файлов: проверяется принадлежность задачи пользователю + filename в output_files + path traversal
- `create_task_result()` вызывается ДО `task_queue.enqueue()` — запись в БД должна существовать до начала обработки
- Определение МП по имени файла: ключевые слова wb/ozon/yandex (регистронезависимо)
- Admin НЕ может: заблокировать owner, изменить роль owner, изменить свою роль
- При блокировке пользователя все его сессии удаляются (принудительный logout)
- Jinja2 globals регистрируются ТОЛЬКО в `web/app.py → _setup_jinja2()` — НЕ в route-модулях
- `base.html` ожидает `user` и `csrf_token` в контексте каждого шаблона (передаются из route-обработчиков)
- Flash-сообщения передаются через переменные контекста `success_message` / `error_message`, НЕ через отдельный механизм сессий
- Навигация в `base.html` подсвечивает активную ссылку через `request.path` — пути маршрутов НЕ менять без обновления шаблона
- `POST /schemas/create` — синхронный (Вариант A): multipart-загрузка + AI inline. НЕ использует TaskQueue — результат возвращается как redirect после завершения AI
- `/schemas/create` регистрируется ПЕРЕД `/schemas/{id:\d+}` в `setup_schemas_routes()` — иначе aiohttp интерпретирует "create" как {id}
- В словарях для Jinja2 шаблонов НЕ использовать ключ "items" — конфликтует с dict.items(). Использовать "matches" или другое имя
- Для создания схемы через веб привязка telegram_user_id НЕ обязательна — схемы могут храниться с web_user_id (FK на web_users(id)), user_id nullable
- Если у веб-пользователя есть привязанный telegram_user_id — при создании схемы записываются оба ID для совместимости с ботом
- Проверка владения схемой выполняется по web_user_id ИЛИ по telegram_user_id (user_id в schemas)

**AI-агент маппинга PIM+FDM (v6.0):**
- `FDM_API_TOKEN` пустой = агент ВЫКЛЮЧЕН (`/v1/*` → 503) — безопасный деплой по умолчанию; токен передаётся FDM как `FDM_AI_AGENT_API_KEY`
- `MappingJobWorker` использует ОБЩИЙ AIComparator из `bot/bot.py` — НЕ создавать собственный (глобальный семафор(5) на всё приложение)
- Задания агента читаются из PostgreSQL (`mapping_jobs`), НЕ из Redis TaskQueue — разные очереди для разных жизненных циклов
- `claim_pending_mapping_job()` — единственный способ забрать задание (FOR UPDATE SKIP LOCKED): конкурентные воркеры не заберут одно задание дважды
- `duration_sec` вычисляется В SQL (`completed_at − started_at`) — НЕ измерять в Python
- Пост-валидация ответов LLM в мапперах обязательна: каждый ID сверяется со входными данными; `unresolved` вычисляется детерминированно, списку AI не доверяется ни одна ссылка
- Пара `(channelValueId, channelValue)` в reference_value_mapping обязана ссылаться на одно значение справочника канала — несовпадение → null
- `channelValue` — авторитетная строка для FDM: сохраняется как есть, НЕ переформулировать
- Порядок shutdown: `web_runner.cleanup()` → `task_worker.stop()` → `mapping_worker.stop()` → `task_queue.disconnect()` → `shutdown_storage()` — воркер агента пишет в БД, обязан завершиться до закрытия пула
- При старте воркер вызывает `recover_stale_mapping_jobs(0)` — processing-задания прошлого процесса → failed
- Таймаут `AGENT_JOB_TIMEOUT_SEC=240` укладывается в 5-минутный бюджет поллинга FDM
- `v1_api.py` возвращает ошибки напрямую через `web.json_response` (точные тексты валидации для FDM), НЕ через HTTPException — errors-middleware содержит только обобщённые тексты
- jobId — `secrets.token_hex(16)` (32 hex): непредсказуем, защита от перебора чужих заданий
- Пустой справочник канала (`referenceValues=[]`) — все значения категории получают null, AI-запрос не нужен
- Дашборд `/agent` — `@admin_required`; названия в детализации восстанавливаются из payload (result хранит только ID)
- Промпты маппинга (`attribute_mapping.txt`, `reference_value_mapping.txt`) читаются один раз в конструкторах мапперов — отсутствие файла = ошибка запуска воркера (fail fast)


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
- _enforce_forced_pairs() вызывается в compare_columns() после _add_mandatory_matches и ДО _deduplicate_matches — порядок критичен
- Очистка ТНВЭД в ColumnSyncer._write_value() применяется ПОСЛЕ AI-валидации и ДО записи в DataFrame
- Бот и веб делят: Database pool, Redis, TaskQueue, AIComparator
- WebSocketManager передаётся в конструктор TaskWorker (не в start()) → WebDelivery уведомляет браузер
- `Task.delivery_channel` определяет стратегию доставки
- `ResultDeliveryFactory.create()` бросает RuntimeError если bot=None для Telegram-задачи
- Если `WEB_HOST` пустой — веб не запускается, бот работает как раньше
- Если ImportError при импорте веб-модулей — бот продолжает работу, ws_manager=None
- SSL-сертификат обновляется автоматически (certbot timer)
- `web/routes/upload.py` использует `request.app["task_queue"]` — тот же экземпляр из DI
- `web/routes/tasks.py` проверяет `task_result["web_user_id"] != web_user_id` перед скачиванием
- `web/routes/schemas.py` использует `storage.db.pool.acquire()` напрямую для `_get_schema_meta` (нет отдельного метода в Database)
- `web/routes/admin.py` использует `storage.db.pool.acquire()` для UPDATE role (нет отдельного метода)
- CSRF middleware выполняется ПОСЛЕ auth — порядок в `setup_middlewares()` критичен
- Все route-обработчики, рендерящие HTML, ОБЯЗАНЫ передавать `"user": user_data` в контекст — `base.html` использует это для навигации
- `admin/users.html` читает flash из `success_message`/`error_message` контекста — POST-обработчики передают их через query params `?success=`/`?error=`, GET-обработчик извлекает и кладёт в контекст
- `schemas/detail.html` ожидает `groups` в формате `[{"key", "label", "matches": [{"columns": [{"mp", "name"}], "confidence"}]}]` — подготовка в `_prepare_groups_for_template()`. Ключ "matches", НЕ "items" — в Jinja2 dict.items конфликтует со встроенным методом dict.items()
- `tasks/list.html` использует `{{ _shorten_filename(filename) }}` — функция должна быть в Jinja2 globals
- Tailwind CSS подключён через CDN в `base.html` — при отсутствии интернета на клиенте стили не загрузятся
- `get_web_session()` ОБЯЗАН возвращать `telegram_user_id` из `web_users` — маршруты schemas, upload используют его для привязки к таблице users
- Создание схемы через веб возможно без привязки Telegram-аккаунта — используется web_user_id
- Владение схемой проверяется двусторонне: по schemas.web_user_id И по schemas.user_id (telegram)
- `get_web_user_schemas(web_user_id)` ищет схемы по web_user_id ИЛИ по привязанному telegram_user_id
- `setup_agent_routes(app)` ОБЯЗАНА вызываться в `setup_routes()` (`web/routes/__init__.py`) — без неё `/agent` отдаёт 404, хотя шаблоны и обработчики существуют (исправлено в v6.1)
- Обработчики `/agent` передают в контекст `user` + `csrf_token` (через `get_csrf_token` из `web.middleware.csrf`) — как и все HTML-маршруты, иначе `base.html` не отрисует навигацию


---

## Roadmap

### Выполнено
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
- [x] Исправлен логгер: единый app.log с RotatingFileHandler вместо нового файла при каждом вызове (v4.7)
- [x] Добавлен AppLogger с поддержкой trace_id и context (v4.7)
- [x] Добавлены UPLOAD_DIR, DOWNLOAD_DIR, OUTPUT_DIR, FILE_MAX_AGE_DAYS в Config (v4.7)
- [x] _FileCleanupService в TaskWorker — фоновая очистка файлов старше 7 дней раз в 24 ч (v4.7)
- [x] Оптимизация save_results: AI-валидация только для изменённых ячеек через changes_log (v4.8)
- [x] Габариты Яндекс: переход на три раздельных столбца (v4.9)
- [x] Очистка ТН ВЭД при синхронизации из Ozon (v4.9)
- [x] Принудительные парные сопоставления (Видео, НДС) (v4.9)
- [x] Фаза 0: `shared/delivery.py` + изменения Task и TaskWorker (v5.0)
- [x] Фаза 1: Каркас aiohttp (app.py, middleware, routes/__init__) (v5.0)
- [x] Фаза 2: Аутентификация (web_users, bcrypt, sessions, permissions) (v5.0)
- [x] Фаза 3: Бизнес-маршруты (dashboard, schemas CRUD, upload + process, tasks + download, admin users, CSRF middleware) (v5.0)
- [x] Фаза 4: Шаблоны и фронтенд (base.html, auth, dashboard, schemas list/detail, upload, tasks, admin, style.css) (v5.0)
- [x] Фаза 5: Nginx + SSL + systemd (v5.0)
- [x] Фаза 6: Интеграция, тестирование, deploy (v5.0)
- [x] Создание стандартных схем через веб (wizard, drag&drop, синхронный AI) (v5.1)
- [x] Исправлен конфликт dict.items в Jinja2 (ключ "matches" вместо "items") (v5.1)
- [x] Исправлена модалка удаления схемы (v5.1)
- [x] Добавлен telegram_user_id в данные веб-сессии (v5.1)
- [x] Исправлена ошибка FK constraint при обработке веб-задач (user_id=0) (v5.1)
- [x] Убрано требование привязки Telegram для создания схем через веб (миграция 006) (v5.1)
- [x] Миграция домена с galina-blanka.ru на ecommpedia.ru (v5.1.2)
- [x] AI-агент маппинга PIM+FDM: REST API `/v1/mapping-tasks` (POST 202 + GET-поллинг) (v6.0)
- [x] Маппинг атрибутов категорий (attribute_mapping) с пост-валидацией ID против входных данных (v6.0)
- [x] Маппинг справочных значений (reference_value_mapping) с гарантией полноты matches (v6.0)
- [x] Таблица mapping_jobs + миграция 007 + методы Database (очередь FOR UPDATE SKIP LOCKED) (v6.0)
- [x] MappingJobWorker: параллелизм 3, таймаут 240 сек, восстановление зависших, ретеншен 30 дн (v6.0)
- [x] Bearer-аутентификация `/v1/*` — независимый контур безопасности (constant-time, 401/503) (v6.0)
- [x] Дашборд оператора `/agent` с поиском, пагинацией и детализацией связок (v6.0)
- [x] Nginx `location /v1/` + отдельный access-лог `agent_api_access.log` (v6.0)
- [x] Редизайн веб-интерфейса: группировка меню (рабочие/управление), SVG-иконки, шрифт Inter, обновлённый dashboard,刷新лённые badges/drop-zone/modal (v6.1)
- [x] Исправлена 404 на /agent: добавлена setup_agent_routes + регистрация в setup_routes, user/csrf_token в контексте шаблонов (v6.1)
- [x] Исправлена 403 на /static/: ACL-права www-data на /root/progect/web/static + default-ACL для новых файлов (v6.1)
- [ ] DELETE /v1/mapping-tasks/{jobId} — отмена задания (статус 'cancelled' уже зарезервирован в CHECK)



### Будущее
- [ ] Создание МВМ-схем через веб (create_mvm.html + выбор категорий)
- [ ] Редактирование сопоставлений через веб (edit.html)
- [ ] Unit-тесты (pytest)
- [ ] CI/CD pipeline
- [ ] Поддержка 5-го маркетплейса
- [ ] Email-уведомления о завершении обработки
- [ ] PWA (Progressive Web App) для мобильных

---

## Ограничения

**Существующие (бот):**
- Python 3.12+
- Максимум 50 символов в артикуле
- МП-файлы только `.xlsx`
- XML только `.xml` (YML-фид)
- XML > 20 МБ — нужна прямая ссылка
- URL: лимит 200 МБ, таймаут 5 минут
- `MAX_CONCURRENT_TASKS` (default=5) одновременных обработок
- Файлы старше `FILE_MAX_AGE_DAYS` удаляются автоматически

**Новые (веб):**
- Nginx обязателен в production (aiohttp не экспонируется наружу)
- `client_max_body_size 250M` в Nginx для загрузки файлов
- WebSocket-соединение живёт до завершения задачи (или 24ч максимум)
- Регистрация закрыта по умолчанию — аккаунты создаёт admin
- Одна активная сессия на пользователя (при новом логине старая истекает)
- Пароль: 8-72 символа (ограничение bcrypt — обрезает после 72 байт)
- bcrypt хеширование выполняется в thread pool (asyncio.to_thread) — не блокирует event loop
- Загрузка файлов: максимум 250 МБ на файл (проверяется и Nginx, и aiohttp `client_max_size`)
- Допустимые форматы загрузки: только `.xlsx` и `.xml`
- Минимум 2 МП-файла для запуска обработки (из 3 возможных)
- Файлы результатов удаляются `_FileCleanupService` через FILE_MAX_AGE_DAYS — ссылки на скачивание перестают работать
- Polling fallback для задач: каждые 5 секунд (если WebSocket недоступен)
- Tailwind CSS (cdn.tailwindcss.com) и шрифт Inter (fonts.googleapis.com) загружаются через CDN — требуется интернет на стороне клиента; без Inter сайт деградирует до системного шрифта (без Tailwind — до нестилизованного HTML)
- Шаблон create_mvm.html для МВМ-схем пока не реализован — создание МВМ-схем доступно только через Telegram-бота
- Шаблон edit.html для редактирования сопоставлений пока не реализован — редактирование доступно только через Telegram-бота
- Создание стандартных схем доступно через веб без привязки Telegram-аккаунта (POST /schemas/create, multipart, синхронный AI-запрос 5-15 сек)
- Схемы, созданные через веб без telegram_user_id, НЕ видны в Telegram-боте до привязки аккаунта

**AI-агент маппинга PIM+FDM (v6.0):**
- `FDM_API_TOKEN` пустой — агент выключен: `/v1/*` отвечает 503, бот/веб работают
- Таймаут обработки задания 240 сек (`AGENT_JOB_TIMEOUT_SEC`) — сверх него задание → failed
- Максимум 3 параллельных задания агента (`AGENT_MAX_CONCURRENT_JOBS`), остальные ждут в БД (FIFO)
- Payload ограничен валидатором: ≤100 атрибутов категории, ≤20 каналов, ≤500 атрибутов канала, ≤1000 значений категории, ≤2000 значений канала (сверх — 422)
- Поллинг FDM: рекомендация 3 сек, бюджет 5 мин — сверх бюджета задание гарантированно терминальное
- DELETE-эндпоинт не реализован (отложен) — aiohttp отвечает 405
- История заданий хранится 30 дней (`AGENT_JOBS_RETENTION_DAYS`), затем удаляется
- Тело запроса `/v1/*`: nginx ограничивает 10 МБ (`client_max_body_size`); превышение — HTML 413 от nginx (на практике недостижимо: валидатор режет раньше)


---

## FAQ

### Бот

**Как работает `from bot import storage`?**
Импортируется модуль, не переменная. На момент импорта `storage.db = None`. После `init_storage()` — готовый экземпляр Database.

**Почему AI-логи пропадают?**
Неправильный порядок: нужно `synchronize_data` → `create_report_with_changes` → `create_ai_log_in_report`.

**Почему AIComparator один на всех?**
Промпты с диска читаются один раз. Семафор(5) — глобальный лимит AI-запросов.

**Как удалить столбец из тройного сопоставления?**
При редактировании сопоставления введи `NA` (без учета регистра) в поле столбца, который хочешь удалить. Сопоставление автоматически станет парным.

**Что произойдет, если ввести NA для единственного заполненного столбца?**
Система выдаст ошибку: "❌ Сопоставление должно содержать минимум 2 источника!". Нельзя создать сопоставление с одним столбцом.

**Чем отличается стандартная схема от МВМ?**
Стандартная схема работает с 3 МП (WB + Ozon + Яндекс) и имеет 4 группы сопоставлений. МВМ-схема добавляет 4-й источник — XML-каталог — и имеет 11 групп сопоставлений. При обработке по МВМ-схеме пустые ячейки МП заполняются данными из XML.

**Как XML используется при обработке?**
XML — это источник данных (донор). Он НЕ заполняется. Данные из XML переносятся в пустые ячейки МП-файлов по сопоставлениям схемы. Привязка товаров — по vendorCode (артикулу).

**Можно ли конвертировать стандартную схему в МВМ?**
Нет, тип схемы задаётся при создании. Нужно создать новую МВМ-схему.

**Что если XML файл слишком большой для Telegram?**
Telegram Bot API ограничивает скачивание файлов до 20 МБ. Если XML больше — бот предложит отправить прямую HTTP/HTTPS ссылку на файл. Загрузи XML на Google Drive, Dropbox или Яндекс.Диск, скопируй прямую ссылку и отправь боту.

**Почему `from bot.storage import db` не работает?**
На момент импорта модуля переменная `db = None`. Нужно импортировать модуль целиком: `from bot import storage`, затем использовать `storage.db`. Это гарантирует, что обращение к `db` происходит уже после `await init_database()`.

**Почему AI-логи пропадают из отчёта?**
`ExcelWriter.create_report_with_changes()` создаёт файл с нуля. Если вызвать `create_ai_log_in_report` до него — лог будет перезаписан. Правильный порядок: `synchronize_data` → `create_report_with_changes` → `create_ai_log_in_report`.

**Где теперь логика валидации и синхронизации?**
После рефакторинга v4.2 `DataSynchronizer` — только оркестратор. Логика разнесена по подпакету `services/sync/`: валидация — `AiValidator`, габариты — `DimensionsSynchronizer`, синхронизация столбцов — `ColumnSyncer`, XML — `XmlSyncer`, файлы — `ExcelFileManager`.

**Почему бот использует Redis?**
Redis используется для двух целей:
1. **FSM-состояния** — переживают перезапуск бота, доступны из нескольких процессов
2. **Сессии загрузки** — временные данные (пути к файлам) с TTL 30 минут, предотвращает утечку памяти

При недоступности Redis бот автоматически переключается на in-memory fallback с предупреждением в логах.

**Почему AIComparator создаётся один раз, а не на каждую задачу?**
С v4.6 `AIComparator` инициализируется в `TaskWorker.__init__` и переиспользуется для всех задач.
Это даёт два эффекта: промпты читаются с диска один раз при старте (не на каждую задачу),
а семафор `asyncio.Semaphore(5)` внутри компаратора становится глобальным — ограничивает
суммарное число AI-запросов по всем параллельным задачам, а не по каждой в отдельности.

### Веб

**Можно ли без веба?**
Да. Если `WEB_HOST` пустой — веб не стартует, бот работает как раньше.

**Можно ли без бота?**
Нет. Единый процесс, общие ресурсы. В будущем можно разделить через shared Redis.

**Как связать Telegram с вебом?**
На странице профиля ввести Telegram user_id → система проверит whitelist и синхронизирует роль.

**Почему aiohttp, не FastAPI?**
aiohttp уже в зависимостях, работает в одном event loop с aiogram без конфликтов. FastAPI потребовал бы uvicorn и усложнил совместный запуск.

**Как работает прогресс на вебе?**
WebSocket `/ws/tasks/{id}`. TaskWorker через WebDelivery вызывает `ws_manager.notify()` — JSON-сообщения `type: progress/files_ready/completed/error`.

**Как скачать результат?**
`GET /tasks/{id}/download/{filename}` → `aiohttp.web.FileResponse` с файлом из `output_dir`. Проверяется: принадлежность задачи пользователю, наличие filename в output_files БД, отсутствие `..` в имени файла, существование файла на диске.

**Как определяется маркетплейс при загрузке?**
По ключевым словам в имени файла (без учёта регистра): wb/wildberries → wildberries, ozon/озон → ozon, yandex/яндекс/маркет → yandex. Если ключевое слово не найдено — файл отклоняется с ошибкой.

**Как работает CSRF-защита?**
Double Submit Cookie: при GET генерируется токен в cookie (httponly=False). При POST токен должен совпасть в cookie и в form field `csrf_token` (или header `X-CSRF-Token` для AJAX). Сравнение через `secrets.compare_digest` (constant-time).

**Может ли admin заблокировать другого admin?**
Нет. Только owner может блокировать admin. Admin может блокировать editor и user. Owner не может быть заблокирован никем.

**Можно ли создать схему через веб-интерфейс?**
Да, стандартные схемы (3 МП) можно создавать через веб: /schemas/create. Wizard на одной странице: название + drag&drop файлов + кнопка запуска. AI-сопоставление выполняется синхронно (5-15 сек). МВМ-схемы (3 МП + XML) пока создаются только через Telegram-бота.

**Видна ли в боте схема, созданная через веб без привязки Telegram?**
Нет. Бот ищет схемы по `user_id` (Telegram ID). Если при создании схемы `telegram_user_id` не был привязан — схема будет видна только на вебе. После привязки Telegram-аккаунта в профиле все схемы пользователя станут доступны в обоих каналах.

**Почему сайт мог отдавать 403 на /static/?**
Nginx отдаёт статику от пользователя www-data, а проект лежит в /root, закрытом
для всех, кроме root. Лечится ACL: `setfacl -m u:www-data:x /root` и
`setfacl -R -m u:www-data:rX /root/progect/web/static` (+ default-ACL для новых
файлов). Диагностика: `curl -sI https://ecommpedia.ru/static/css/style.css` → 403.

**Почему после изменения стилей пользователи видят старый сайт?**
Nginx кэширует статику в браузере на 7 дней (`expires 7d`). При правке style.css
нужно повышать версию в ссылке (`style.css?v=2`, `?v=3`, ...) в base.html.


### AI-агент маппинга (v6.0)

**Как включить агента?**
Сгенерировать токен: `python3 -c "import secrets; print(secrets.token_hex(32))"`, записать в `.env` как `FDM_API_TOKEN`, перезапустить бота. Токен и base URL (`https://ecommpedia.ru/v1`) передать команде FDM (у них — `FDM_AI_AGENT_API_KEY`). Миграция БД применится автоматически при старте.

**Почему задания агента в PostgreSQL, а не в Redis-очереди?**
Внешний API требует персистентности и статусов: FDM поллит GET, дашборд показывает историю, зависшие задания восстанавливаются при рестарте. Redis TaskQueue — для файловых задач с доставкой пользователю; у них другой жизненный цикл.

**Почему один AI-запрос на задание?**
Задание — одна связка (категория или атрибут). Промпт получает компактный JSON всех атрибутов/значений и всех каналов сразу; один ответ покрывает всё. Дешевле и быстрее, чем N запросов.

**Что если LLM придумает несуществующий ID?**
Пост-валидация в мапперах сверяет каждый ID со входными данными: галлюцинации отбрасываются, недостающие записи дополняются null (задача 2) или попадают в unresolved (задача 1). Результату AI не доверяет ни одна ссылка.

**Чем отличается таймаут от остановки воркера?**
Таймаут (240 сек) → failed «Превышен таймаут обработки». Остановка сервиса → failed «Воркер агента остановлен во время обработки». FDM в обоих случаях получает внятный `error` вместо вечного поллинга.

**Где смотреть историю запросов FDM?**
Веб → пункт «Агент» (owner/admin): список с поиском, детализация со связками и confidence. Access-лог: `/var/log/nginx/agent_api_access.log`.

**Можно ли агенту отдельную модель?**
Да: `AGENT_AI_MODEL` в `.env` (пусто = общая `AI_MODEL`). Температура — `AGENT_AI_TEMPERATURE`.

**Почему /v1/* исключён из CSRF?**
CSRF-атака эксплуатирует автоматическую отправку cookie браузером. FDM сознательно кладёт `Authorization` в заголовок — сторонняя страница не способна его прочитать или заставить браузер отправить. Bearer-заголовок сам является proof-of-origin.

---

**Версия документации:** 6.1
**Дата обновления:** Сентябрь 2026
**Автор проекта:** Александр
