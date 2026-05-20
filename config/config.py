"""
Конфигурация приложения
"""

import os
import re
from typing import Dict, List, Any
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


def _validate_redis_url(url: str) -> bool:
    """
    Проверяет формат URL Redis.

    Args:
        url: Строка подключения к Redis

    Returns:
        True если формат корректный (redis://host:port/db)
    """
    if not url:
        return False
    # Допустимые схемы: redis://, rediss:// (TLS)
    pattern = r'^redis(s)?://[^\s/]+:\d+(/\d+)?$'
    return bool(re.match(pattern, url))


class Config:
    """Класс конфигурации приложения, следующий принципу Single Responsibility"""

    # OpenRouter API настройки
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    AI_MODEL: str = os.getenv("AI_MODEL", "google/gemini-2.5-flash-preview-09-2025")
    AI_TEMPERATURE: float = float(os.getenv("AI_TEMPERATURE", "0.1"))

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
        # Яндекс
        'Габариты с упаковкой, см',
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
            "column_1": "Фото",
            "column_2": "Ссылки на дополнительные фото",
            "column_3": "Ссылка на изображение *",
            "description": "Фотографии товара"
        },
        {
            "column_1": "Высота упаковки",
            "column_2": "Высота упаковки, мм*",
            "column_3": "Габариты с упаковкой, см",
            "description": "Высота товара с упаковкой"
        },
        {
            "column_1": "Длина упаковки",
            "column_2": "Длина упаковки, мм*",
            "column_3": "Габариты с упаковкой, см",
            "description": "Длина товара с упаковкой"
        },
        {
            "column_1": "Ширина упаковки",
            "column_2": "Ширина упаковки, мм*",
            "column_3": "Габариты с упаковкой, см",
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
        # Критические параметры — без них бот не может работать
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

        # ===================================================================
        # Валидация параметров очереди задач
        # ===================================================================
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

        # ===================================================================
        # Опциональная проверка Redis (не критичная — есть fallback)
        # ===================================================================
        # Redis используется для хранения FSM-состояний и сессий загрузки.
        # Если Redis недоступен — бот автоматически переключается на
        # in-memory fallback (данные сессий будут потеряны при перезапуске).
        # ===================================================================
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


# Экспорты для обратной совместимости
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