# Marketplace Sync — Документация проекта

## Назначение

Telegram-бот и веб-приложение для автоматической синхронизации данных товаров между тремя маркетплейсами: Wildberries, Ozon и Яндекс.Маркет. Система использует AI для интеллектуального сопоставления столбцов Excel-файлов и валидации значений с учётом справочников (validation lists).

Два канала доступа к одному и тому же функционалу:
- **Telegram-бот** — существующий интерфейс, работает без изменений
- **Веб-сайт** (`https://galina-blanka.ru`) — браузерный интерфейс с drag&drop загрузкой, real-time прогрессом через WebSocket, скачиванием результатов

Оба канала используют общую бизнес-логику, единую очередь задач и единую базу данных.

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
│   └─ sync/ (7 компонентов)                                      │
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
         └─────────────┘  └─────────────┘   └─────────────┘
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
| SSL | certbot (Let's Encrypt) | HTTPS для galina-blanka.ru |

### Ключевые архитектурные принципы

- **Один event loop** — бот (polling) и веб-сервер (aiohttp) работают в одном asyncio event loop через `asyncio.gather()`. Общие ресурсы: DB pool, Redis, AIComparator, TaskQueue.
- **Единая очередь** — и бот, и веб ставят задачи в одну TaskQueue. TaskWorker обрабатывает их одинаково, доставка результата зависит от `delivery_channel` в Task.
- **Strategy для доставки** — `ResultDelivery` абстракция с двумя реализациями: `TelegramDelivery` (Bot.send_document) и `WebDelivery` (сохранение + WebSocket-уведомление).
- **Бот не изменён** — все существующие хендлеры, FSM, клавиатуры работают как раньше. Веб-интерфейс — параллельный канал, а не замена.

---

## Структура проекта

```
/
├── main.py                          # Точка входа: бот + веб в одном event loop
├── .env                              # Конфигурация (API ключи, DB, Redis, Web)
├── .env.example                      # Шаблон переменных окружения с описаниями
│
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
├── /web/                             # === ВЕБ-ПРИЛОЖЕНИЕ (НОВОЕ) ===
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
│   │   ├── __init__.py
│   │   ├── auth.py                   # Проверка cookie-сессии
│   │   ├── errors.py                 # 404, 500, JSON errors
│   │   └── csrf.py                   # CSRF-токены для форм
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
│   │   └── api.py                    # /api/* — JSON API для AJAX
│   │
│   ├── /templates/
│   │   ├── base.html                 # Layout: nav, footer, Tailwind CDN, scripts
│   │   ├── /auth/
│   │   │   ├── login.html
│   │   │   └── register.html
│   │   ├── dashboard.html
│   │   ├── /schemas/
│   │   │   ├── list.html
│   │   │   ├── detail.html           # Просмотр схемы (группы сопоставлений)
│   │   │   ├── create.html           # Wizard стандартной схемы (Фаза будущая)
│   │   │   ├── create_mvm.html       # Wizard МВМ-схемы (Фаза будущая)
│   │   │   └── edit.html             # Редактирование сопоставлений (Фаза будущая)
│   │   ├── /upload/
│   │   │   └── index.html            # Drag&drop + выбор схемы
│   │   ├── /tasks/
│   │   │   └── list.html             # Задачи + прогресс
│   │   └── /admin/
│   │       └── users.html            # Whitelist управление
│   │
│   └── /static/
│       ├── /css/
│       │   └── style.css             # Кастомные стили (Tailwind через CDN)
│       └── /js/                      # Зарезервировано для будущего выноса JS
│                                     # Текущая логика встроена в шаблоны ({% block scripts %})
│
├── /shared/                          # === ОБЩИЕ АДАПТЕРЫ (НОВОЕ) ===
│   ├── __init__.py
│   ├── delivery.py                   # ResultDelivery (ABC), TelegramDelivery, WebDelivery,
│   │                                 # ResultDeliveryFactory. Импорты Bot/Task через TYPE_CHECKING
│   │                                 # для избежания циклических зависимостей.
│   └── notifications.py              # NotificationService (Telegram / WebSocket)
│
├── /services/                        # === БИЗНЕС-ЛОГИКА (минимальные изменения) ===
│   ├── data_synchronizer.py          # Оркестратор (Facade) — без изменений
│   ├── ai_comparator.py              # AI-сопоставление — без изменений
│   ├── task_queue.py                 # Task + TaskQueue — добавлены 2 поля в Task
│   ├── task_worker.py                # Воркер — использует ResultDelivery вместо Bot
│   └── /sync/
│       ├── __init__.py
│       ├── value_converter.py
│       ├── ai_validator.py
│       ├── article_aligner.py
│       ├── dimensions_synchronizer.py
│       ├── column_syncer.py
│       ├── photo_syncer.py
│       ├── xml_syncer.py
│       └── excel_io.py
│
├── /utils/                           # === УТИЛИТЫ (без изменений) ===
│   ├── excel_reader.py
│   ├── xml_reader.py
│   ├── excel_writer.py
│   ├── logger_config.py
│   └── utils.py
│
├── /database/                        # === БД (добавлены таблицы и методы) ===
│   ├── database.py                   # + методы для web_users, web_sessions, task_results
│   └── migrations.py                 # + миграции 003, 004, 005
│
├── /config/
│   └── config.py                     # + веб-переменные (WEB_HOST, WEB_PORT, ...)
│
├── /nginx/
│   └── galina-blanka.conf            # Nginx: SSL, reverse proxy, static, WebSocket
│
├── /scripts/
│   ├── setup_ssl.sh                  # certbot --nginx -d galina-blanka.ru
│   └── deploy.sh                     # git pull + pip install + systemctl restart
│
└── /systemd/
    └── marketplace-bot.service       # Systemd unit (бот + веб в одном процессе)
```

---

## Жизненный цикл приложения (обновлённый)

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

9. **Параллельный запуск**:

   ```python
   runner = web.AppRunner(web_app)
   await runner.setup()
   site = web.TCPSite(runner, Config.WEB_HOST, Config.WEB_PORT)
   await site.start()
   # Бот + веб работают одновременно:
   await dp.start_polling(bot)
   ```

10. **Graceful shutdown** — строгий порядок:
    - `await runner.cleanup()` — остановка веб-сервера
    - `await task_worker.stop()` — ожидание задач → `comparator.close()` → `cleanup_service.stop()`
    - `await task_queue.disconnect()` — Redis
    - `await shutdown_storage()` — PostgreSQL pool

---

## Модуль `shared/delivery.py` (НОВЫЙ)

### Паттерн Strategy — абстракция доставки результатов

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from aiogram import Bot
from aiogram.types import FSInputFile


class ResultDelivery(ABC):
    """
    Абстракция доставки результатов обработки пользователю.

    Паттерн: Strategy — TaskWorker не знает, куда отправлять результат.
    Конкретная реализация выбирается по task.delivery_channel.
    """

    @abstractmethod
    async def send_progress(self, message: str) -> None:
        """Промежуточное сообщение о прогрессе."""

    @abstractmethod
    async def send_files(self, file_paths: List[str], caption: str = "") -> None:
        """Отправка файлов результатов."""

    @abstractmethod
    async def send_result(self, text: str) -> None:
        """Финальное сообщение с итогами."""

    @abstractmethod
    async def send_error(self, error_message: str) -> None:
        """Сообщение об ошибке."""


class TelegramDelivery(ResultDelivery):
    """Доставка через Telegram Bot API."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send_progress(self, message: str) -> None:
        await self._bot.send_message(self._chat_id, message)

    async def send_files(self, file_paths: List[str], caption: str = "") -> None:
        for path in file_paths:
            await self._bot.send_document(self._chat_id, FSInputFile(path))
        if caption:
            await self._bot.send_message(self._chat_id, caption)

    async def send_result(self, text: str) -> None:
        await self._bot.send_message(self._chat_id, text)

    async def send_error(self, error_message: str) -> None:
        await self._bot.send_message(self._chat_id, f"❌ Ошибка: {error_message}")


class WebDelivery(ResultDelivery):
    """
    Доставка для веб-интерфейса.

    Не отправляет файлы — сохраняет пути в task_results (БД).
    Уведомляет подключённый браузер через WebSocket.
    """

    def __init__(self, task_id: str, web_user_id: int, ws_manager) -> None:
        self._task_id = task_id
        self._web_user_id = web_user_id
        self._ws_manager = ws_manager

    async def send_progress(self, message: str) -> None:
        await self._ws_manager.notify(self._task_id, {
            "type": "progress",
            "message": message,
        })

    async def send_files(self, file_paths: List[str], caption: str = "") -> None:
        # Файлы уже на диске — просто обновляем статус в БД
        from bot import storage

        output_files: Dict[str, str] = {}
        for path in file_paths:
            filename = Path(path).name
            output_files[filename] = path

        await storage.db.update_task_result(
            self._task_id,
            output_files=output_files,
        )
        await self._ws_manager.notify(self._task_id, {
            "type": "files_ready",
            "count": len(file_paths),
            "filenames": [Path(p).name for p in file_paths],
        })

    async def send_result(self, text: str) -> None:
        await self._ws_manager.notify(self._task_id, {
            "type": "completed",
            "message": text,
        })

    async def send_error(self, error_message: str) -> None:
        await self._ws_manager.notify(self._task_id, {
            "type": "error",
            "message": error_message,
        })


class ResultDeliveryFactory:
    """Фабрика: создаёт нужную реализацию доставки по delivery_channel."""

    @staticmethod
    def create(task, bot: Optional[Bot], ws_manager) -> ResultDelivery:
        if task.delivery_channel == "web":
            return WebDelivery(task.id, task.web_user_id, ws_manager)
        return TelegramDelivery(bot, task.chat_id)
```

---

## Изменения в существующих файлах

### `services/task_queue.py` — Task dataclass

Добавлены 2 поля:

```python
DeliveryChannel = Literal["telegram", "web"]

@dataclass
class Task:
    # ... все существующие поля без изменений ...
    delivery_channel: DeliveryChannel = "telegram"
    web_user_id: Optional[int] = None
```

Совместимость: старый код создаёт `Task` без этих полей → используются defaults. `from_json()` игнорирует неизвестные поля через `filtered = {k: v ... if k in known}`.

### `services/task_worker.py` — использование ResultDelivery

Изменён метод `_execute_task`:

- Вместо прямого `self._bot.send_message(...)` → `delivery.send_progress(...)`
- Вместо `self._bot.send_document(...)` → `delivery.send_files(...)`
- Конструктор принимает `ws_manager: Optional[object] = None`
- `ResultDelivery` создаётся через `ResultDeliveryFactory.create(task, bot, ws_manager)` в начале `_execute_task`
- При невозможности создать канал доставки (неизвестный `delivery_channel`, отсутствие `bot`) — задача помечается `failed` без обработки
- Все файлы результатов (3 МП + отчёт) отправляются одним вызовом `delivery.send_files(result_files)`

Метод `_notify_user` удалён — заменён на `delivery.send_progress/send_result/send_error`.

### `config/config.py` — новые переменные

```python
# Веб-сервер
WEB_HOST: str = os.getenv("WEB_HOST", "")  # Пустая строка = веб не запускается
WEB_PORT: int = _safe_int_env("WEB_PORT", 8080)
WEB_DOMAIN: str = os.getenv("WEB_DOMAIN", "galina-blanka.ru")
WEB_SECRET_KEY: str = os.getenv("WEB_SECRET_KEY", "")
WEB_SESSION_MAX_AGE: int = _safe_int_env("WEB_SESSION_MAX_AGE", 86400)
WEB_REGISTRATION_OPEN: bool = os.getenv("WEB_REGISTRATION_OPEN", "false").lower() == "true"
WEB_CSRF_ENABLED: bool = os.getenv("WEB_CSRF_ENABLED", "true").lower() == "true"
```

`validate()` дополнение:

```python
if cls.WEB_HOST and not cls.WEB_SECRET_KEY:
    raise ValueError(
        "WEB_SECRET_KEY обязателен при включённом веб-сервере (WEB_HOST задан). "
        "Сгенерируй: python3 -c 'import secrets; print(secrets.token_hex(32))'"
    )
```

### `database/migrations.py` — миграции 003-005

```python
(
    "003_create_web_users_table",
    """
    CREATE TABLE IF NOT EXISTS web_users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT,
        telegram_user_id BIGINT UNIQUE,
        role TEXT NOT NULL DEFAULT 'user',
        is_active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        last_login_at TIMESTAMPTZ,
        CONSTRAINT web_users_role_check CHECK (role IN ('owner', 'admin', 'editor', 'user'))
    );
    CREATE INDEX IF NOT EXISTS idx_web_users_email ON web_users(email);
    CREATE INDEX IF NOT EXISTS idx_web_users_telegram_id ON web_users(telegram_user_id);
    """
),
(
    "004_create_web_sessions_table",
    """
    CREATE TABLE IF NOT EXISTS web_sessions (
        id TEXT PRIMARY KEY,
        web_user_id INTEGER REFERENCES web_users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        ip_address TEXT,
        user_agent TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_web_sessions_user_id ON web_sessions(web_user_id);
    CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at);
    """
),
(
    "005_create_task_results_table",
    """
    CREATE TABLE IF NOT EXISTS task_results (
        id SERIAL PRIMARY KEY,
        task_id TEXT UNIQUE NOT NULL,
        web_user_id INTEGER REFERENCES web_users(id),
        status TEXT NOT NULL DEFAULT 'pending',
        output_files JSONB,
        report_path TEXT,
        stats JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        error_message TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_task_results_user_id ON task_results(web_user_id);
    CREATE INDEX IF NOT EXISTS idx_task_results_task_id ON task_results(task_id);
    """
),
```

### `database/database.py` — новые методы

```python
# === Веб-пользователи ===
async def create_web_user(self, email, password_hash, display_name, role='user') -> int
async def get_web_user_by_email(self, email) -> Optional[Dict]
async def get_web_user_by_id(self, user_id) -> Optional[Dict]
async def update_web_user_last_login(self, user_id) -> None
async def link_telegram_to_web_user(self, web_user_id, telegram_user_id) -> bool
async def get_web_users_list(self, limit=50) -> List[Dict]
async def set_web_user_active(self, user_id, is_active) -> None

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

### `bot/bot.py` — запуск веб-сервера

В `start_bot()` добавлен условный запуск веб-сервера:

```python
# После создания бота и перед polling:
web_runner = None
ws_manager = None

if Config.WEB_HOST:
    try:
        from aiohttp import web
        from web.app import create_web_app

        ws_manager_module = __import__(
            'web.routes.websocket', fromlist=['WebSocketManager']
        )
        WebSocketManager = ws_manager_module.WebSocketManager
        ws_manager = WebSocketManager()

        web_app = await create_web_app(task_queue, ai_comparator, ws_manager)

        web_runner = web.AppRunner(web_app)
        await web_runner.setup()
        site = web.TCPSite(web_runner, Config.WEB_HOST, Config.WEB_PORT)
        await site.start()
        logger.info("Веб-сервер: http://%s:%s", Config.WEB_HOST, Config.WEB_PORT)
    except ImportError as e:
        logger.warning(
            "Не удалось импортировать веб-модули (%s). "
            "Веб-сервер не запущен. Установите: "
            "pip install aiohttp-jinja2 aiohttp-session bcrypt jinja2",
            e,
        )
        web_runner = None
        ws_manager = None
    except Exception as e:
        logger.error("Ошибка запуска веб-сервера: %s. Бот продолжит работу без веба.", e)
        web_runner = None
        ws_manager = None

# TaskWorker получает ws_manager:
task_worker = TaskWorker(task_queue, Config.MAX_CONCURRENT_TASKS, ai_comparator, ws_manager)

# В finally (порядок: веб → воркер → очередь → хранилища):
if web_runner:
    await web_runner.cleanup()
```

Если `WEB_HOST` не задан в `.env` — веб-сервер не запускается, бот работает как раньше.

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
const ws = new WebSocket(`wss://galina-blanka.ru/ws/tasks/${taskId}`);
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

---

## Система прав доступа (обновлённая)

### Иерархия ролей (общая для бота и веба)

| Роль | Telegram | Web | Права |
|---|---|---|---|
| Owner | `ACCESS_OWNER_ID` из `.env` | `role='owner'` в `web_users` | Полный контроль |
| Admin | `system_settings` или `.env` | `role='admin'` | Управление whitelist, все схемы |
| Editor | whitelist `role='editor'` | `role='editor'` | Все схемы (чтение/запись), свои CRUD |
| User | whitelist `role='user'` | `role='user'` | Только свои схемы, загрузка, статистика |

### WebAccessManager (`web/auth/permissions.py`)

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

---

## Конфигурация (`.env`) — полная

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
WEB_DOMAIN=galina-blanka.ru
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

# === Прокси (опционально) ===
PROXY_ENABLED=false
PROXY_URL=http://user:pass@host:port
```

### Обязательные переменные

Бот не запустится без: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `DATABASE_URL`, `ACCESS_OWNER_ID`.

Веб не запустится без (если `WEB_HOST` задан): `WEB_SECRET_KEY`.

`Config.validate()` бросает `ValueError` при отсутствии обязательных параметров. Дополнительно: если `WEB_HOST` задан, но `WEB_SECRET_KEY` пустой — бросает `ValueError` с инструкцией генерации ключа. Предупреждение при `WEB_SESSION_MAX_AGE <= 0`.

---

## Nginx (`nginx/galina-blanka.conf`)

```nginx
# HTTP → HTTPS redirect
server {
    listen 80;
    server_name galina-blanka.ru www.galina-blanka.ru;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name galina-blanka.ru www.galina-blanka.ru;

    # SSL (certbot)
    ssl_certificate /etc/letsencrypt/live/galina-blanka.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/galina-blanka.ru/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Безопасность
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";

    # Static files (Nginx отдаёт напрямую, минуя Python)
    location /static/ {
        alias /root/progect/web/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # Favicon
    location /favicon.ico {
        alias /root/progect/web/static/favicon.ico;
        expires 30d;
        access_log off;
    }

    # WebSocket (прогресс обработки)
    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;  # 24ч для долгих WS-соединений
    }

    # Загрузка файлов (увеличенный лимит)
    location /upload/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 250M;  # Для больших Excel/XML файлов
        proxy_request_buffering off;
        proxy_read_timeout 120;
    }

    # Скачивание результатов
    location /tasks/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;  # Streaming для больших файлов
    }

    # Все остальные запросы → aiohttp
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60;
    }
}
```

---

## Deployment

### Systemd unit (`systemd/marketplace-bot.service`)

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

### Установка веб-компонентов на сервер

```bash
# 1. Установка Nginx (если не установлен)
sudo apt install -y nginx

# 2. SSL-сертификат
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d galina-blanka.ru -d www.galina-blanka.ru

# 3. Конфигурация Nginx
sudo cp /root/progect/nginx/galina-blanka.conf /etc/nginx/sites-available/galina-blanka
sudo ln -sf /etc/nginx/sites-available/galina-blanka /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 4. Новые Python-зависимости
source /root/progect/venv/bin/activate
pip install aiohttp-jinja2==1.6 aiohttp-session==2.12.0 \
            cryptography==43.0.0 bcrypt==4.2.0 jinja2==3.1.4

# 5. Генерация WEB_SECRET_KEY
echo "WEB_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env

# 6. Добавить переменные в .env
cat >> .env << 'EOF'
WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_DOMAIN=galina-blanka.ru
WEB_SESSION_MAX_AGE=86400
WEB_REGISTRATION_OPEN=false
WEB_CSRF_ENABLED=true
EOF

# 7. Перезапуск
sudo systemctl restart marketplace-bot
sudo systemctl status marketplace-bot
```

### Управление

```bash
systemctl start marketplace-bot      # Запуск
systemctl stop marketplace-bot       # Остановка
systemctl restart marketplace-bot    # Перезапуск
systemctl status marketplace-bot     # Статус
journalctl -u marketplace-bot -f     # Логи в реальном времени

# Проверка веб-сервера
curl -I https://galina-blanka.ru     # Должен вернуть 302 → /auth/login

# Обновление SSL (автоматически через cron certbot)
sudo certbot renew --dry-run
```

### Зависимости (`requirements.txt` — обновлённый)

```
# Telegram Bot
aiogram==3.4.1

# Database
asyncpg==0.31.0

# Data Processing
pandas==2.3.3
openpyxl==3.1.5

# AI
openai==2.14.0

# Configuration
python-dotenv==1.2.1

# HTTP
aiohttp==3.9.5
httpx==0.28.1

# Redis
redis==7.4.0

# Async files
aiofiles==23.2.1

# Proxy
socksio==1.0.0

# === Web (новое) ===
aiohttp-jinja2==1.6
aiohttp-session==2.12.0
cryptography==43.0.0
bcrypt==4.2.0
jinja2==3.1.4
```

---

## Существующий функционал бота (без изменений)

Все описания ниже остаются актуальными и не затрагиваются добавлением веба.

### DataSynchronizer — оркестратор (v4.2)

Facade-оркестратор. Создаёт компоненты `sync/`, координирует вызовы в определённом порядке.

Публичный интерфейс: `synchronize_data(file_paths, output_paths)` → `(synced_dfs, changes_log)`.

Порядок этапов:

1. Загрузка DataFrame и validation-списков
2. Построение XML-индекса
3. Выравнивание артикулов
4. Синхронизация габаритов
5. Синхронизация столбцов по схеме (тройные → парные → фото)
6. [МВМ] Заполнение из XML
7. Сохранение файлов

### Подпакет `services/sync/` (7 компонентов)

- **ValueConverter** — единицы измерения, конвертация
- **AiValidator** — 5-уровневая валидация (exact → normalize → regex → subset → AI)
- **ArticleAligner** — выравнивание артикулов между МП
- **DimensionsSynchronizer** — габариты (WB см, Ozon мм, Яндекс см)
- **ColumnSyncer** — тройные + парные + фото
- **PhotoSyncer** — специальная логика фото (разные форматы МП)
- **XmlSyncer** — заполнение МП из XML-каталога
- **ExcelFileManager** — загрузка/сохранение xlsx

### Схемы сопоставлений

Два типа:
- **standard** — 3 МП, 4 группы (тройные + 3 парные)
- **mvm** — 4 источника, 11 групп (четверные + 4 тройные + 6 парных)

### Специальная обработка

- Габариты: раздельные столбцы (WB см, Ozon мм, Яндекс см)
- ТН ВЭД: из Ozon в WB/Яндекс — только числовой код
- Фото: Ozon (2 столбца) ↔ WB (1, через ";") ↔ Яндекс (1, через ",")
- Принудительные парные: Видео (WB+Яндекс), НДС (WB+Ozon)
- Множественные значения: WB — первое, Ozon — через ";", Яндекс — через ","

---

## Важные предостережения

### НЕ изменять без обсуждения

**Бот (существующее):**
- Формат `MANDATORY_MATCHES`
- Логику 5-уровневой валидации в `AiValidator`
- Разделители множественных значений
- Структуру прав доступа (роли и whitelist)
- Префиксы XML-полей (`[XML]` и `[XML param]`)
- Порядок синхронизации
- Порядок инициализации и shutdown
- `FORCED_PAIR_ONLY_MATCHES`, `TNVED_`, `PHOTO_`
- Порядок записи AI-лога

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
- Для создания схемы через веб пользователь ОБЯЗАН иметь привязанный telegram_user_id — схемы хранятся с FK на users(user_id)

### Критичные зависимости

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
- Создание схемы через веб невозможно без привязки Telegram-аккаунта (telegram_user_id = NULL → ошибка)

---

## Roadmap

### Выполнено (до v5.0)
- ✅ Все фичи из v1.0 — v4.9 (см. предыдущую документацию)
- ✅ Фаза 0: `shared/delivery.py` + изменения Task и TaskWorker
- ✅ Фаза 1: Каркас aiohttp (app.py, middleware, routes/__init__)
- ✅ Фаза 2: Аутентификация (web_users, bcrypt, sessions, permissions)
- ✅ Фаза 3: Бизнес-маршруты (dashboard, schemas CRUD, upload + process, tasks + download, admin users, CSRF middleware)

### v5.0 — Веб-интерфейс
- ✅ Фаза 4: Шаблоны и фронтенд (base.html, auth, dashboard, schemas list/detail, upload, tasks, admin, style.css). Маршруты переведены на aiohttp_jinja2.render_template(). JS встроен в шаблоны через {% block scripts %}.
- ✅ Фаза 5: Nginx + SSL + systemd
- ✅ Фаза 6: Интеграция, тестирование, deploy

### v5.1 — Веб: создание схем
- ✅ Создание стандартных схем через веб (wizard, drag&drop, синхронный AI)
- ✅ Исправлен конфликт dict.items в Jinja2 (ключ "matches" вместо "items")
- ✅ Исправлена модалка удаления схемы (display:none + правильная структура backdrop)
- ✅ Добавлен telegram_user_id в данные веб-сессии (get_web_session)
- ✅ Исправлена ошибка FK constraint при обработке веб-задач (user_id=0)
- ⬜ Создание МВМ-схем через веб (create_mvm.html + выбор категорий)
- ⬜ Редактирование сопоставлений через веб (edit.html)

### Будущее
- ⬜ Unit-тесты (pytest)
- ⬜ CI/CD pipeline
- ⬜ Поддержка 5-го маркетплейса
- ⬜ Email-уведомления о завершении обработки
- ⬜ PWA (Progressive Web App) для мобильных

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
- Tailwind CSS загружается через CDN (cdn.tailwindcss.com) — требуется интернет на стороне клиента
- Шаблон create_mvm.html для МВМ-схем пока не реализован — создание МВМ-схем доступно только через Telegram-бота
- Шаблон edit.html для редактирования сопоставлений пока не реализован — редактирование доступно только через Telegram-бота
- Создание стандартных схем доступно через веб (POST /schemas/create, multipart, синхронный AI-запрос 5-15 сек)

---

## FAQ

### Бот

**Как работает `from bot import storage`?**
Импортируется модуль, не переменная. На момент импорта `storage.db = None`. После `init_storage()` — готовый экземпляр Database.

**Почему AI-логи пропадают?**
Неправильный порядок: нужно `synchronize_data` → `create_report_with_changes` → `create_ai_log_in_report`.

**Почему AIComparator один на всех?**
Промпты с диска читаются один раз. Семафор(5) — глобальный лимит AI-запросов.

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

---

**Версия документации:** 5.0
**Дата обновления:** Июль 2026
**Автор проекта:** Александр
