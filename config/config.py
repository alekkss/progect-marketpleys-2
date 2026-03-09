"""
Конфигурация приложения
"""

import os
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
        Валидация обязательных параметров конфигурации
        
        Returns:
            bool: True если все обязательные параметры заполнены
        """
        required_fields = [cls.OPENROUTER_API_KEY, cls.TELEGRAM_BOT_TOKEN]
        return all(field for field in required_fields)
    
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


# Экспортируем для обратной совместимости
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
