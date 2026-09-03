"""
Конфигурация приложения
"""

import os
import re
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Загрузка переменных окружения
load_dotenv()


def _safe_int_env(name: str, default: int = 0) -> int:
    """
    Безопасное чтение int из переменной окружения

    Args:
        name: Имя переменной окружения
        default: Значение по умолчанию если парсинг не удался

    Returns:
        int: Числовое значение или default
    """
    raw = os.getenv(name, "")
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _safe_float_env(name: str, default: float = 0.0) -> float:
    """
    Безопасное чтение float из переменной окружения.

    При невалидном значении (например, "abc") возвращает default
    вместо падения с ValueError при импорте модуля.

    Args:
        name: Имя переменной окружения
        default: Значение по умолчанию если парсинг не удался

    Returns:
        float: Числовое значение или default
    """
    raw = os.getenv(name, "")
    try:
        return float(str(raw).strip()) if raw.strip() else default
    except (TypeError, ValueError):
        return default


def _validate_redis_url(url: str) -> bool:
    """
    Проверяет формат URL Redis.

    Поддерживает:
        - redis://host:port/db
        - rediss://host:port/db (TLS)
        - redis://user:password@host:port/db (аутентификация)
        - redis://host:port (без номера БД)
        - redis://host:port/db?timeout=5 (query params)

    Args:
        url: Строка подключения к Redis

    Returns:
        True если формат корректный
    """
    if not url:
        return False
    pattern = r'^redis(s)?://([^@]+@)?[^\s/:]+:\d+(/\d+)?(\?.*)?$'
    return bool(re.match(pattern, url))


class Config:
    """Класс конфигурации приложения, следующий принципу Single Responsibility"""

    # OpenRouter API настройки
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    AI_MODEL: str = os.getenv("AI_MODEL", "google/gemini-2.5-flash-preview-09-2025")
    AI_TEMPERATURE: float = _safe_float_env("AI_TEMPERATURE", 0.1)

    # Права доступа
    ACCESS_OWNER_ID: int = _safe_int_env("ACCESS_OWNER_ID", 0)
    ACCESS_ADMIN_ID: int = _safe_int_env("ACCESS_ADMIN_ID", 0)

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # ===================================================================
    # PostgreSQL — основная база данных (asyncpg)
    # ===================================================================
    # Формат: postgresql://user:password@host:port/dbname
    # Пример: postgresql://bot_user:secret@localhost:5432/marketplace_sync
    # ===================================================================
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DATABASE_POOL_MIN_SIZE: int = _safe_int_env("DATABASE_POOL_MIN_SIZE", 2)
    DATABASE_POOL_MAX_SIZE: int = _safe_int_env("DATABASE_POOL_MAX_SIZE", 10)

    # ===================================================================
    # Redis — хранилище FSM-состояний и сессий
    # ===================================================================
    # Формат: redis://host:port/db_number
    # Пример: redis://localhost:6379/0
    # По умолчанию: redis://localhost:6379/0
    # ===================================================================
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # ===================================================================
    # Очередь задач (фоновая обработка синхронизации)
    # ===================================================================
    # MAX_CONCURRENT_TASKS — максимум одновременных обработок (Semaphore).
    # При 5 пользователях одновременно — 5 задач в параллели, остальные
    # ждут в очереди. Это предотвращает перегрузку CPU и AI API.
    # TASK_QUEUE_KEY — ключ Redis List для хранения ID задач.
    # ===================================================================
    MAX_CONCURRENT_TASKS: int = _safe_int_env("MAX_CONCURRENT_TASKS", 5)
    TASK_QUEUE_KEY: str = os.getenv("TASK_QUEUE_KEY", "bot:task_queue")

    # ===================================================================
    # Логирование
    # ===================================================================
    LOG_FILE_PATH: str = os.getenv("LOG_FILE_PATH", "./logs/app.log")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # ===================================================================
    # Директории для временных файлов
    # ===================================================================
    # UPLOAD_DIR    — входные Excel/XML файлы от пользователей
    # DOWNLOAD_DIR  — промежуточные файлы загрузки
    # OUTPUT_DIR    — результаты синхронизации и отчёты
    # FILE_MAX_AGE_DAYS — файлы старше этого срока удаляются уборщиком
    # ===================================================================
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "/root/progect/uploads")
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "/root/progect/downloads")
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "/root/progect/output")
    FILE_MAX_AGE_DAYS: int = _safe_int_env("FILE_MAX_AGE_DAYS", 7)

    # ===================================================================
    # Веб-сервер (опционально)
    # ===================================================================
    # Если WEB_HOST пустой — веб-сервер НЕ запускается, бот работает
    # как раньше. Это позволяет использовать тот же код на серверах
    # без веб-компонента.
    #
    # WEB_HOST            — адрес для прослушивания (127.0.0.1 за Nginx)
    # WEB_PORT            — порт aiohttp (Nginx проксирует сюда)
    # WEB_DOMAIN          — публичный домен (для формирования ссылок)
    # WEB_SECRET_KEY      — секрет для подписи cookie-сессий (обязателен!)
    # WEB_SESSION_MAX_AGE — время жизни сессии в секундах (24 ч)
    # WEB_REGISTRATION_OPEN — разрешить свободную регистрацию
    # WEB_CSRF_ENABLED    — включить CSRF-защиту форм
    # ===================================================================
    WEB_HOST: str = os.getenv("WEB_HOST", "")
    WEB_PORT: int = _safe_int_env("WEB_PORT", 8080)
    WEB_DOMAIN: str = os.getenv("WEB_DOMAIN", "galina-blanka.ru")
    WEB_SECRET_KEY: str = os.getenv("WEB_SECRET_KEY", "")
    WEB_SESSION_MAX_AGE: int = _safe_int_env("WEB_SESSION_MAX_AGE", 86400)
    WEB_REGISTRATION_OPEN: bool = os.getenv("WEB_REGISTRATION_OPEN", "false").lower() == "true"
    WEB_CSRF_ENABLED: bool = os.getenv("WEB_CSRF_ENABLED", "true").lower() == "true"

        # ===================================================================
    # AI-агент маппинга PIM+FDM (REST API /v1/mapping-tasks)
    # ===================================================================
    # Внешний контур: FDM отправляет задания на маппинг атрибутов и
    # справочных значений (POST /v1/mapping-tasks) и поллит статус
    # (GET /v1/mapping-tasks/{jobId}). Аутентификация — Bearer-токен,
    # полностью независимый от cookie-сессий сайта.
    #
    # FDM_API_TOKEN               — Bearer-токен для /v1/*. Пустой —
    #                               агент выключен, /v1/* отвечает 503
    # AGENT_AI_MODEL              — модель для маппинга; "" → AI_MODEL
    # AGENT_AI_TEMPERATURE        — температура (0.0 — детерминированность)
    # AGENT_MAX_CONCURRENT_JOBS   — одновременных заданий агента (свой семафор)
    # AGENT_POLL_INTERVAL_SEC     — интервал опроса БД воркером агента
    # AGENT_JOB_TIMEOUT_SEC       — таймаут задания → failed (поллинг FDM 5 мин)
    # AGENT_JOBS_RETENTION_DAYS   — срок хранения заданий в БД
    # AGENT_MAX_ATTRIBUTES        — лимит атрибутов категории (422 сверх)
    # AGENT_MAX_CHANNEL_ATTRIBUTES — лимит атрибутов на один канал (422 сверх)
    # AGENT_MAX_REFERENCE_VALUES  — лимит значений справочника категории (422 сверх)
    # AGENT_MAX_REFERENCE_CHANNEL_VALUES — лимит значений справочника ОДНОГО канала
    # AGENT_MAX_CHANNELS          — лимит каналов в задании (422 сверх)
    # ===================================================================
    FDM_API_TOKEN: str = os.getenv("FDM_API_TOKEN", "")
    AGENT_AI_MODEL: str = os.getenv("AGENT_AI_MODEL", "")
    AGENT_AI_TEMPERATURE: float = _safe_float_env("AGENT_AI_TEMPERATURE", 0.0)
    AGENT_MAX_CONCURRENT_JOBS: int = _safe_int_env("AGENT_MAX_CONCURRENT_JOBS", 3)
    AGENT_POLL_INTERVAL_SEC: float = _safe_float_env("AGENT_POLL_INTERVAL_SEC", 5.0)
    AGENT_JOB_TIMEOUT_SEC: int = _safe_int_env("AGENT_JOB_TIMEOUT_SEC", 240)
    AGENT_JOBS_RETENTION_DAYS: int = _safe_int_env("AGENT_JOBS_RETENTION_DAYS", 30)
    AGENT_MAX_ATTRIBUTES: int = _safe_int_env("AGENT_MAX_ATTRIBUTES", 100)
    AGENT_MAX_CHANNEL_ATTRIBUTES: int = _safe_int_env("AGENT_MAX_CHANNEL_ATTRIBUTES", 500)
    AGENT_MAX_REFERENCE_VALUES: int = _safe_int_env("AGENT_MAX_REFERENCE_VALUES", 1000)
    AGENT_MAX_REFERENCE_CHANNEL_VALUES: int = _safe_int_env(
        "AGENT_MAX_REFERENCE_CHANNEL_VALUES", 2000
    )
    AGENT_MAX_CHANNELS: int = _safe_int_env("AGENT_MAX_CHANNELS", 20)



    # Конфигурация файлов для каждого маркетплейса
    FILE_CONFIGS: Dict[str, Dict[str, Any]] = {
        "wildberries": {
            "sheet_name": "Товары",
            "header_row": 3,
            "data_start_row": 5,
            "display_name": "WB Товары"
        },
        "ozon": {
            "sheet_name": "Шаблон",
            "header_row": 2,
            "data_start_row": 5,
            "display_name": "Ozon Шаблон"
        },
        "yandex": {
            "sheet_name": "Данные о товарах",
            "header_row": 4,
            "data_start_row": 8,
            "display_name": "Яндекс Данные"
        }
    }

    # Паттерны для динамического поиска столбцов габаритов WB
    # Разные категории WB используют разные названия столбцов
    WB_DIMENSION_PATTERNS: Dict[str, List[str]] = {
        'length': [
            'Длина упаковки (целое число)',
            'Длина упаковки',
        ],
        'width': [
            'Ширина упаковки (целое число)',
            'Ширина упаковки',
        ],
        'height': [
            'Высота упаковки (целое число)',
            'Высота упаковки',
        ],
    }

    # Все возможные названия столбцов габаритов (для пропуска в обычной синхронизации)
    ALL_DIMENSION_COLUMN_NAMES: set = {
        # WB — вариант 1 (например, категория "Кроссовки")
        'Длина упаковки (целое число)',
        'Ширина упаковки (целое число)',
        'Высота упаковки (целое число)',
        # WB — вариант 2 (например, категория "Куртки")
        'Длина упаковки',
        'Ширина упаковки',
        'Высота упаковки',
        # Ozon
        'Длина упаковки, мм*',
        'Ширина упаковки, мм*',
        'Высота упаковки, мм*',
        # Яндекс — три раздельных столбца в сантиметрах
        'Длина, см *',
        'Ширина, см *',
        'Высота, см *',
    }

    # ===================================================================
    # Столбцы ТН ВЭД (код товарной номенклатуры)
    # ===================================================================
    # В Ozon код хранится с пояснением: "2103909009 - Прочие продукты..."
    # В WB и Яндекс принимается только числовой код: "2103909009"
    # При синхронизации из Ozon в WB/Яндекс извлекается только число.
    # ===================================================================
    TNVED_COLUMN_NAMES: set = {
        'Код ТН ВЭД',           # WB и Яндекс
        'ТН ВЭД коды ЕАЭС*',   # Ozon
    }

    # МП, которые принимают только числовой код ТНВЭД (без пояснений)
    TNVED_NUMERIC_ONLY_MARKETPLACES: set = {
        'wildberries',
        'yandex',
    }

    # ===================================================================
    # Десятичный разделитель — маркетплейсы, требующие точку
    # ===================================================================
    # Некоторые МП (Ozon) требуют точку как десятичный разделитель.
    # Значения из других МП могут содержать запятую (например, "1234,5").
    # При записи в эти МП запятая автоматически заменяется на точку.
    #
    # Применяется в ColumnSyncer._normalize_decimal_separator() и
    # XmlSyncer — ко всем значениям, похожим на десятичные числа.
    # ===================================================================
    DECIMAL_DOT_MARKETPLACES: set = {
        'ozon',
    }

    # ===================================================================
    # Принудительные парные сопоставления (без определённого МП)
    # ===================================================================
    # Некоторые столбцы существуют только в 2 из 3 маркетплейсов.
    # AI может ошибочно сопоставить их с произвольным столбцом 3-го МП.
    # Этот список задаёт жёсткие правила: для каждого набора столбцов
    # указан column_key, который ВСЕГДА должен быть None (NA).
    #
    # Формат: список словарей с ключами column_1, column_2, column_3.
    # Значение None означает, что этот МП НЕ участвует в сопоставлении.
    #
    # Обработка в AIComparator._enforce_forced_pairs():
    #   1. Если AI поместил эти столбцы в тройное — запрещённый МП удаляется,
    #      сопоставление перемещается в правильное парное.
    #   2. Если AI создал парное с запрещённым МП — оно удаляется.
    #   3. Принудительное парное добавляется если отсутствует.
    # ===================================================================
    FORCED_PAIR_ONLY_MATCHES: List[Dict[str, Optional[str]]] = [
        {
            "column_1": "Видео",
            "column_2": None,
            "column_3": "Ссылка на видео",
            "description": "Видео товара (в Ozon нет поля видео в основном листе)"
        },
        {
            "column_1": "Ставка НДС",
            "column_2": "НДС, %*",
            "column_3": None,
            "description": "Ставка НДС (в Яндекс нет поля НДС в основном листе)"
        },
    ]

    # ===================================================================
    # Фото-столбцы маркетплейсов
    # ===================================================================
    # Описывает роли фото-столбцов для каждого МП.
    # Используется PhotoSyncer для специальной логики синхронизации фото.
    #
    # Роли столбцов:
    #   main  — главное фото (одна ссылка, всегда первая)
    #   extra — дополнительные фото (несколько ссылок через разделитель)
    #   all   — все фото в одном столбце (главное + дополнительные)
    #
    # Разделители при ЗАПИСИ в столбец:
    #   wildberries: ";"  — ссылки через точку с запятой
    #   ozon:        "\n" — ссылки через перенос строки (реальный формат Ozon)
    #   yandex:      ","  — ссылки через запятую
    #
    # Разделители при ЧТЕНИИ — не используются напрямую.
    # PhotoSyncer._split_links() использует универсальный re.split
    # по паттерну [\r\n;,]+ — обрабатывает все форматы автоматически.
    # PHOTO_READ_SEPARATORS оставлен для документирования фактического
    # формата хранения в каждом МП.
    # ===================================================================
    PHOTO_COLUMNS: Dict[str, Dict[str, str]] = {
        "wildberries": {
            # Единственный столбец WB — хранит все фото через ";"
            "all": "Фото",
        },
        "ozon": {
            # Два отдельных столбца Ozon
            "main":  "Ссылка на главное фото*",
            "extra": "Ссылки на дополнительные фото",
        },
        "yandex": {
            # Единственный столбец Яндекс — хранит все фото через ","
            "all": "Ссылка на изображение *",
        },
    }

    # Фактический формат хранения ссылок в столбцах МП (для документирования).
    # При ЧТЕНИИ PhotoSyncer использует универсальный сплиттер — эти значения
    # не передаются в _split_links напрямую.
    PHOTO_READ_SEPARATORS: Dict[str, str] = {
        "wildberries": ";",
        "ozon":        "\n",   # Реальный разделитель в файлах Ozon — перенос строки
        "yandex":      ",",
    }

    # Разделители при ЗАПИСИ фото-ссылок в столбцы МП.
    # Используются в PhotoSyncer при формировании итоговых строк.
    PHOTO_WRITE_SEPARATORS: Dict[str, str] = {
        "wildberries": ";",
        "ozon":        "\n",   # Ozon хранит дополнительные фото через перенос строки
        "yandex":      ",",
    }

    # ===================================================================
    # Маппинг единиц измерения для XML-полей
    # ===================================================================
    XML_UNIT_MAPPING: Dict[str, str] = {
        # Вес
        '[XML] weight': 'kg',
        '[XML param] Вес': 'kg',
        '[XML param] Вес, кг': 'kg',
        '[XML param] Вес, г': 'g',
        # Габариты
        '[XML] dimensions': 'cm',
        '[XML param] Длина': 'cm',
        '[XML param] Ширина': 'cm',
        '[XML param] Высота': 'cm',
        '[XML param] Длина, мм': 'mm',
        '[XML param] Ширина, мм': 'mm',
        '[XML param] Высота, мм': 'mm',
    }

    # ===================================================================
    # Все весовые столбцы МП (для специальной обработки конвертации)
    # ===================================================================
    ALL_WEIGHT_COLUMN_NAMES: Dict[str, str] = {
        # Вес С упаковкой
        'Вес с упаковкой (кг)': 'kg',
        'Вес в упаковке, г*': 'g',
        'Вес с упаковкой, кг': 'kg',
        # Вес БЕЗ упаковки
        'Вес без упаковки (кг)': 'kg',
        'Вес, кг': 'kg',
    }

    # Обязательные совпадения (всегда должны быть сопоставлены)
    # ВАЖНО: фото-столбцы намеренно исключены отсюда —
    # их синхронизация выполняется отдельно через PhotoSyncer,
    # потому что у каждого МП разная структура фото-столбцов.
    MANDATORY_MATCHES: List[Dict[str, str]] = [
        {
            "column_1": "Артикул продавца",
            "column_2": "Артикул*",
            "column_3": "Ваш SKU *",
            "description": "Уникальный артикул товара"
        },
        {
            "column_1": "Баркоды",
            "column_2": "Штрихкод (Серийный номер / EAN)",
            "column_3": "Штрихкод *",
            "description": "Штрихкод товара"
        },
        {
            "column_1": "Бренд",
            "column_2": "Бренд*",
            "column_3": "Бренд *",
            "description": "Бренд производителя"
        },
        {
            "column_1": "Наименование",
            "column_2": "Название товара",
            "column_3": "Название товара *",
            "description": "Название товара"
        },
        {
            "column_1": "Описание",
            "column_2": "Аннотация",
            "column_3": "Описание товара *",
            "description": "Описание товара"
        },
        {
            "column_1": "Вес с упаковкой (кг)",
            "column_2": "Вес в упаковке, г*",
            "column_3": "Вес с упаковкой, кг",
            "description": "Вес товара с упаковкой"
        },
        {
            "column_1": "Вес без упаковки (кг)",
            "column_2": "Вес, кг",
            "column_3": "Вес, кг",
            "description": "Вес товара без упаковки"
        },
        {
            "column_1": "Высота упаковки",
            "column_2": "Высота упаковки, мм*",
            "column_3": "Высота, см *",
            "description": "Высота товара с упаковкой"
        },
        {
            "column_1": "Длина упаковки",
            "column_2": "Длина упаковки, мм*",
            "column_3": "Длина, см *",
            "description": "Длина товара с упаковкой"
        },
        {
            "column_1": "Ширина упаковки",
            "column_2": "Ширина упаковки, мм*",
            "column_3": "Ширина, см *",
            "description": "Ширина товара с упаковкой"
        },
        {
            "column_1": "Цвет",
            "column_2": "Цвет товара",
            "column_3": "Название цвета от производителя",
            "description": "Цвет товара"
        }
    ]

    # Список столбцов-исключений (не сравнивать и не синхронизировать)
    EXCLUDED_COLUMNS: List[str] = [
        # Цены (каждый маркетплейс устанавливает свои цены)
        "Цена",
        "Цена, руб.*",
        "Цена *",
        "Розничная цена",
        "Цена до скидки",
        "Старая цена",
        "Rich-контент JSON",
        "Цена до скидки, руб.",
        "SKU",
        "SKU на Маркете",
        "Артикул WB",
    ]

    @classmethod
    def validate(cls) -> bool:
        """
        Валидация обязательных параметров конфигурации.

        Проверяет наличие всех критически важных переменных окружения.
        Вызывается при старте приложения. При отсутствии обязательного
        параметра выбрасывает ValueError с понятным описанием.

        Returns:
            bool: True если все обязательные параметры заполнены

        Raises:
            ValueError: если обязательный параметр отсутствует
        """
        if not cls.TELEGRAM_BOT_TOKEN:
            raise ValueError(
                "Не задан TELEGRAM_BOT_TOKEN в .env. "
                "Бот не может работать без токена Telegram."
            )

        if not cls.OPENROUTER_API_KEY:
            raise ValueError(
                "Не задан OPENROUTER_API_KEY в .env. "
                "AI-сопоставление и валидация не будут работать."
            )

        if not cls.DATABASE_URL:
            raise ValueError(
                "Не задан DATABASE_URL в .env. "
                "Формат: postgresql://user:password@host:port/dbname"
            )

        if not cls.ACCESS_OWNER_ID:
            raise ValueError(
                "Не задан ACCESS_OWNER_ID в .env. "
                "Система доступа не может работать без ID владельца."
            )

        if not (0.0 <= cls.AI_TEMPERATURE <= 2.0):
            raise ValueError(
                f"AI_TEMPERATURE должен быть в диапазоне [0.0, 2.0], "
                f"получено: {cls.AI_TEMPERATURE}. "
                f"Рекомендуемое значение: 0.1"
            )

        if cls.MAX_CONCURRENT_TASKS <= 0:
            raise ValueError(
                f"MAX_CONCURRENT_TASKS должен быть положительным числом, "
                f"получено: {cls.MAX_CONCURRENT_TASKS}. "
                f"Установите значение от 1 до 10 (рекомендуется 5)."
            )
        if cls.MAX_CONCURRENT_TASKS > 20:
            import logging
            logging.getLogger('config').warning(
                "MAX_CONCURRENT_TASKS=%s слишком высокое. "
                "Это может привести к перегрузке CPU и AI API. "
                "Рекомендуется значение 5.",
                cls.MAX_CONCURRENT_TASKS,
            )

        if not cls.REDIS_URL:
            import logging
            logging.getLogger('config').warning(
                "REDIS_URL не задан в .env. "
                "FSM-состояния и сессии загрузки будут храниться в памяти "
                "(потеряются при перезапуске бота). "
                "Установите REDIS_URL=redis://localhost:6379/0 для сохранения состояний."
            )
        elif not _validate_redis_url(cls.REDIS_URL):
            import logging
            logging.getLogger('config').warning(
                "REDIS_URL имеет некорректный формат: %s. "
                "Ожидается: redis://host:port/db или rediss://host:port/db. "
                "FSM-состояния и сессии будут храниться в памяти.",
                cls.REDIS_URL,
            )

        # ===============================================================
        # Валидация веб-сервера (условная)
        # ===============================================================
        # Веб-сервер запускается ТОЛЬКО если WEB_HOST задан.
        # Если он задан — WEB_SECRET_KEY обязателен для безопасности
        # cookie-сессий. Без него злоумышленник сможет подделать сессию.
        # ===============================================================
        if cls.WEB_HOST and not cls.WEB_SECRET_KEY:
            raise ValueError(
                "WEB_SECRET_KEY обязателен при включённом веб-сервере (WEB_HOST задан). "
                "Сгенерируйте: python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )

        if cls.WEB_HOST and cls.WEB_SESSION_MAX_AGE <= 0:
            import logging
            logging.getLogger('config').warning(
                "WEB_SESSION_MAX_AGE=%d — некорректное значение. "
                "Установлено значение по умолчанию: 86400 (24 часа).",
                cls.WEB_SESSION_MAX_AGE,
            )

        # ===============================================================
        # Валидация AI-агента маппинга PIM+FDM
        # ===============================================================
        # Пустой FDM_API_TOKEN — НЕ ошибка: агент просто выключен,
        # маршруты /v1/* отвечают 503. Безопасный деплой по умолчанию.
        # ===============================================================
        if cls.FDM_API_TOKEN:
            if not cls.WEB_HOST:
                import logging
                logging.getLogger('config').warning(
                    "FDM_API_TOKEN задан, но WEB_HOST пуст — веб-сервер "
                    "не запускается, AI-агент маппинга недоступен."
                )
            if len(cls.FDM_API_TOKEN) < 16:
                import logging
                logging.getLogger('config').warning(
                    "FDM_API_TOKEN короче 16 символов — слабый токен. "
                    "Сгенерируйте: python3 -c 'import secrets; print(secrets.token_hex(32))'"
                )
            if not (0.0 <= cls.AGENT_AI_TEMPERATURE <= 2.0):
                raise ValueError(
                    f"AGENT_AI_TEMPERATURE должен быть в диапазоне [0.0, 2.0], "
                    f"получено: {cls.AGENT_AI_TEMPERATURE}. Рекомендуется 0.0."
                )
            if cls.AGENT_MAX_CONCURRENT_JOBS <= 0:
                raise ValueError(
                    f"AGENT_MAX_CONCURRENT_JOBS должен быть >= 1, "
                    f"получено: {cls.AGENT_MAX_CONCURRENT_JOBS}."
                )
            if cls.AGENT_JOB_TIMEOUT_SEC <= 0:
                raise ValueError(
                    f"AGENT_JOB_TIMEOUT_SEC должен быть >= 1, "
                    f"получено: {cls.AGENT_JOB_TIMEOUT_SEC}."
                )
            if cls.AGENT_POLL_INTERVAL_SEC <= 0:
                raise ValueError(
                    f"AGENT_POLL_INTERVAL_SEC должен быть > 0, "
                    f"получено: {cls.AGENT_POLL_INTERVAL_SEC}."
                )
        else:
            import logging
            logging.getLogger('config').info(
                "FDM_API_TOKEN не задан — AI-агент маппинга выключен "
                "(маршруты /v1/* будут отвечать 503)."
            )

        return True

    # Прокси настройки
    PROXY_ENABLED: bool = os.getenv("PROXY_ENABLED", "false").lower() == "true"
    PROXY_URL: str = os.getenv("PROXY_URL", "")


class ColumnValidator:
    """Класс для валидации столбцов (Single Responsibility Principle)"""

    @staticmethod
    def is_excluded_column(column_name: str) -> bool:
        """
        Проверяет, находится ли столбец в списке исключений

        Args:
            column_name: название столбца

        Returns:
            True если столбец исключен, False в противном случае
        """
        if not column_name:
            return False

        column_lower = column_name.strip().lower()

        for excluded in Config.EXCLUDED_COLUMNS:
            if excluded.strip().lower() == column_lower:
                return True

        return False


# ===================================================================
# Экспорты для обратной совместимости
# ===================================================================
OPENROUTER_API_KEY = Config.OPENROUTER_API_KEY
OPENROUTER_BASE_URL = Config.OPENROUTER_BASE_URL
AI_MODEL = Config.AI_MODEL
AI_TEMPERATURE = Config.AI_TEMPERATURE
TELEGRAM_BOT_TOKEN = Config.TELEGRAM_BOT_TOKEN
FILE_CONFIGS = Config.FILE_CONFIGS
MANDATORY_MATCHES = Config.MANDATORY_MATCHES
EXCLUDED_COLUMNS = Config.EXCLUDED_COLUMNS
is_excluded_column = ColumnValidator.is_excluded_column
PROXY_ENABLED = Config.PROXY_ENABLED
PROXY_URL = Config.PROXY_URL
ACCESS_OWNER_ID = Config.ACCESS_OWNER_ID
ACCESS_ADMIN_ID = Config.ACCESS_ADMIN_ID
WB_DIMENSION_PATTERNS = Config.WB_DIMENSION_PATTERNS
ALL_DIMENSION_COLUMN_NAMES = Config.ALL_DIMENSION_COLUMN_NAMES
WB_MULTI_VALUE_COLUMNS: set = {
    'Цвет', 'Размер', 'Материал', 'Состав', 'Особенности модели',
    'Страна производства', 'Комплектация', 'Рекомендуемый возраст',
    'Пол', 'Назначение'
}
XML_UNIT_MAPPING = Config.XML_UNIT_MAPPING
ALL_WEIGHT_COLUMN_NAMES = Config.ALL_WEIGHT_COLUMN_NAMES
DATABASE_URL = Config.DATABASE_URL
DATABASE_POOL_MIN_SIZE = Config.DATABASE_POOL_MIN_SIZE
DATABASE_POOL_MAX_SIZE = Config.DATABASE_POOL_MAX_SIZE
REDIS_URL = Config.REDIS_URL
LOG_FILE_PATH = Config.LOG_FILE_PATH
LOG_LEVEL = Config.LOG_LEVEL
MAX_CONCURRENT_TASKS = Config.MAX_CONCURRENT_TASKS
TASK_QUEUE_KEY = Config.TASK_QUEUE_KEY
UPLOAD_DIR = Config.UPLOAD_DIR
DOWNLOAD_DIR = Config.DOWNLOAD_DIR
OUTPUT_DIR = Config.OUTPUT_DIR
FILE_MAX_AGE_DAYS = Config.FILE_MAX_AGE_DAYS
TNVED_COLUMN_NAMES = Config.TNVED_COLUMN_NAMES
TNVED_NUMERIC_ONLY_MARKETPLACES = Config.TNVED_NUMERIC_ONLY_MARKETPLACES
FORCED_PAIR_ONLY_MATCHES = Config.FORCED_PAIR_ONLY_MATCHES
PHOTO_COLUMNS = Config.PHOTO_COLUMNS
PHOTO_READ_SEPARATORS = Config.PHOTO_READ_SEPARATORS
PHOTO_WRITE_SEPARATORS = Config.PHOTO_WRITE_SEPARATORS
DECIMAL_DOT_MARKETPLACES = Config.DECIMAL_DOT_MARKETPLACES

# Веб-сервер
WEB_HOST = Config.WEB_HOST
WEB_PORT = Config.WEB_PORT
WEB_DOMAIN = Config.WEB_DOMAIN
WEB_SECRET_KEY = Config.WEB_SECRET_KEY
WEB_SESSION_MAX_AGE = Config.WEB_SESSION_MAX_AGE
WEB_REGISTRATION_OPEN = Config.WEB_REGISTRATION_OPEN
WEB_CSRF_ENABLED = Config.WEB_CSRF_ENABLED
# AI-агент маппинга PIM+FDM
FDM_API_TOKEN = Config.FDM_API_TOKEN
AGENT_AI_MODEL = Config.AGENT_AI_MODEL
AGENT_AI_TEMPERATURE = Config.AGENT_AI_TEMPERATURE
AGENT_MAX_CONCURRENT_JOBS = Config.AGENT_MAX_CONCURRENT_JOBS
AGENT_POLL_INTERVAL_SEC = Config.AGENT_POLL_INTERVAL_SEC
AGENT_JOB_TIMEOUT_SEC = Config.AGENT_JOB_TIMEOUT_SEC
AGENT_JOBS_RETENTION_DAYS = Config.AGENT_JOBS_RETENTION_DAYS
AGENT_MAX_ATTRIBUTES = Config.AGENT_MAX_ATTRIBUTES
AGENT_MAX_CHANNEL_ATTRIBUTES = Config.AGENT_MAX_CHANNEL_ATTRIBUTES
AGENT_MAX_REFERENCE_VALUES = Config.AGENT_MAX_REFERENCE_VALUES
AGENT_MAX_REFERENCE_CHANNEL_VALUES = Config.AGENT_MAX_REFERENCE_CHANNEL_VALUES
AGENT_MAX_CHANNELS = Config.AGENT_MAX_CHANNELS

