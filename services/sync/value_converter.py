"""
Модуль конвертации единиц измерения между маркетплейсами.

Отвечает за две задачи:
    - определение единицы измерения по названию столбца;
    - конвертацию числовых значений между единицами (кг↔г, мм↔см).
"""

from typing import Optional

from config.config import XML_UNIT_MAPPING, ALL_WEIGHT_COLUMN_NAMES
from utils.logger_config import setup_logger

logger = setup_logger("value_converter")


class ValueConverter:
    """
    Определяет единицы измерения столбцов и конвертирует значения.

    Порядок определения единицы (detect_unit):
        1. Точный маппинг для XML-полей (XML_UNIT_MAPPING из конфига).
        2. Точный маппинг для весовых столбцов МП (ALL_WEIGHT_COLUMN_NAMES).
        3. Эвристика по ключевым словам в названии столбца.

    Поддерживаемые конвертации (convert_value):
        - кг  ↔ г   (вес)
        - мм  ↔ см  (размер)

    Паттерн: Strategy — алгоритм определения единицы можно расширить,
    не изменяя код конвертации.
    """

    def detect_unit(self, column_name: str) -> Optional[str]:
        """
        Определяет единицу измерения из названия столбца.

        Args:
            column_name: название столбца МП или XML-поля с префиксом.

        Returns:
            Строка единицы ('kg', 'g', 'mm', 'cm') или None, если не определено.
        """
        if not column_name:
            return None

        # 1. Точный маппинг для XML-полей (например, '[XML] weight')
        if column_name in XML_UNIT_MAPPING:
            unit = XML_UNIT_MAPPING[column_name]
            logger.debug(f"XML маппинг: '{column_name}' → {unit}")
            return unit

        # 2. Точный маппинг для весовых столбцов МП
        if column_name in ALL_WEIGHT_COLUMN_NAMES:
            unit = ALL_WEIGHT_COLUMN_NAMES[column_name]
            logger.debug(f"Весовой маппинг: '{column_name}' → {unit}")
            return unit

        # 3. Эвристика по ключевым словам
        column_lower = column_name.lower()

        if "кг" in column_lower or "kg" in column_lower:
            return "kg"

        if (
            " г" in column_lower
            or ",г" in column_lower
            or "gram" in column_lower
            or column_lower.endswith("г")
        ):
            return "g"

        if "мм" in column_lower or "mm" in column_lower:
            return "mm"

        if "см" in column_lower or "cm" in column_lower:
            return "cm"

        return None

    def convert_value(
        self,
        value: object,
        from_unit: Optional[str],
        to_unit: Optional[str],
    ) -> object:
        """
        Конвертирует числовое значение между единицами измерения.

        Если единицы не определены или совпадают — возвращает значение без изменений.
        Если значение не числовое — возвращает как есть.

        Args:
            value:     исходное значение.
            from_unit: исходная единица ('kg', 'g', 'mm', 'cm').
            to_unit:   целевая единица.

        Returns:
            Сконвертированное значение или исходное, если конвертация невозможна.
        """
        import pandas as pd

        # Единицы не заданы или одинаковые — конвертация не нужна
        if not from_unit or not to_unit or from_unit == to_unit:
            return value

        # Пустое значение — возвращаем как есть
        if pd.isna(value):
            return value

        try:
            numeric_value = float(value)
        except (ValueError, TypeError):
            return value

        # --- Конвертация веса ---
        if from_unit == "kg" and to_unit == "g":
            result = numeric_value * 1000
            logger.debug(f"Конвертация: {numeric_value} кг → {result} г")
            return result

        if from_unit == "g" and to_unit == "kg":
            result = numeric_value / 1000
            logger.debug(f"Конвертация: {numeric_value} г → {result} кг")
            return result

        # --- Конвертация размеров ---
        if from_unit == "mm" and to_unit == "cm":
            result = numeric_value / 10
            logger.debug(f"Конвертация: {numeric_value} мм → {result} см")
            return result

        if from_unit == "cm" and to_unit == "mm":
            result = numeric_value * 10
            logger.debug(f"Конвертация: {numeric_value} см → {result} мм")
            return result

        # Неподдерживаемая комбинация — возвращаем исходное значение
        logger.warning(
            f"Неподдерживаемая конвертация: {from_unit} → {to_unit} "
            f"для значения '{value}'"
        )
        return value