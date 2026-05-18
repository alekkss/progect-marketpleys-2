"""
Модуль AI-валидации значений против списков допустимых значений.

Отвечает за одну задачу: проверить значение и вернуть подходящий
вариант из списка, используя каскад методов — от быстрых локальных
до AI-запроса.

Паттерн: Strategy — цепочка методов проверки применяется последовательно,
каждый следующий используется только если предыдущий не дал результата.
"""

import re
from typing import Dict, List, Optional

from config.config import WB_MULTI_VALUE_COLUMNS
from utils.logger_config import setup_logger

logger = setup_logger("ai_validator")


class AiValidator:
    """
    Валидирует значения против списков допустимых значений.

    Каскад проверок (от быстрых к медленным):
        1. Точное совпадение.
        2. Совпадение с нормализацией (регистр, ё→е).
        3. Совпадение по извлечённому числу.
        4. Частичное совпадение по словам.
        5. AI-запрос через AIComparator (только если предыдущие не сработали).

    Принимает зависимости через конструктор (Dependency Inversion):
        - ai_comparator: экземпляр AIComparator (может быть None).
        - column_validations: словарь {маркетплейс: {столбец: [значения]}}.
        - ai_validation_log: список для записи всех сопоставлений.

    Паттерн: Dependency Injection — не создаёт зависимости сам,
    получает их снаружи, что упрощает тестирование.
    """

    def __init__(
        self,
        ai_comparator: object,
        column_validations: Dict[str, Dict[str, List[str]]],
        ai_validation_log: List[Dict],
    ) -> None:
        """
        Args:
            ai_comparator:      экземпляр AIComparator или None.
            column_validations: кэш validation-списков, заполняется ExcelFileManager.
            ai_validation_log:  общий лог сопоставлений (передаётся по ссылке).
        """
        self._ai_comparator = ai_comparator
        self._column_validations = column_validations
        self._ai_validation_log = ai_validation_log

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    async def validate_multiple_values(
        self,
        value: object,
        marketplace: str,
        column_name: str,
    ) -> Optional[str]:
        """
        Валидирует значение с учётом разделителей (;) и правил маркетплейса.

        Правила форматирования множественных значений:
            - WB (обычные столбцы):          только ПЕРВОЕ значение.
            - WB (WB_MULTI_VALUE_COLUMNS):    все значения через «;».
            - Ozon:                           все значения через «;».
            - Яндекс:                         все значения через «,».

        Args:
            value:        исходное значение (может содержать «;»).
            marketplace:  'wildberries', 'ozon' или 'yandex'.
            column_name:  название столбца для поиска validation-списка.

        Returns:
            Отформатированная строка или None.
        """
        if not value:
            return None

        value_str = str(value).strip()

        # Одно значение — обычная валидация
        if ";" not in value_str:
            return await self._validate_with_ai(value_str, marketplace, column_name)

        # Множественные значения — разбиваем по «;»
        parts = [part.strip() for part in value_str.split(";") if part.strip()]
        if not parts:
            return None

        return await self._validate_parts(parts, marketplace, column_name)

    # ------------------------------------------------------------------
    # Приватные методы
    # ------------------------------------------------------------------

    async def _validate_parts(
        self,
        parts: List[str],
        marketplace: str,
        column_name: str,
    ) -> Optional[str]:
        """
        Валидирует список частей и форматирует результат по правилам маркетплейса.

        Args:
            parts:       список отдельных значений после разбивки по «;».
            marketplace: идентификатор маркетплейса.
            column_name: название столбца.

        Returns:
            Отформатированная строка или None.
        """
        # Wildberries: проверяем тип столбца
        if marketplace == "wildberries":
            if column_name not in WB_MULTI_VALUE_COLUMNS:
                # Обычный столбец WB — только первое значение
                validated = await self._validate_with_ai(
                    parts[0], marketplace, column_name
                )
                return validated if validated else parts[0]

            # Столбец из WB_MULTI_VALUE_COLUMNS (Фото, Видео и др.) —
            # сохраняем все значения через «;»
            validated_parts: List[str] = []
            for part in parts:
                validated = await self._validate_with_ai(part, marketplace, column_name)
                if validated and validated not in validated_parts:
                    validated_parts.append(validated)
                elif not validated:
                    # Для ссылок (фото/видео) validation обычно отсутствует —
                    # сохраняем как есть
                    has_validation = bool(
                        self._column_validations.get(marketplace, {}).get(column_name)
                    )
                    if not has_validation and part not in validated_parts:
                        validated_parts.append(part)

            return ";".join(validated_parts) if validated_parts else None

        # Ozon и Яндекс: валидируем каждое значение
        validated_parts = []
        for part in parts:
            validated = await self._validate_with_ai(part, marketplace, column_name)
            if validated and validated not in validated_parts:
                validated_parts.append(validated)

        if not validated_parts:
            return None

        # Форматируем согласно требованиям маркетплейса
        if marketplace == "yandex":
            return ",".join(validated_parts)
        if marketplace == "ozon":
            return ";".join(validated_parts)

        return validated_parts[0]

    async def _validate_with_ai(
        self,
        value: str,
        marketplace: str,
        column_name: str,
    ) -> Optional[str]:
        """
        Проверяет одно значение против validation-списка каскадом методов.

        Если для столбца нет validation-списка или нет ai_comparator — возвращает None.

        Args:
            value:        строковое значение для проверки.
            marketplace:  идентификатор маркетплейса.
            column_name:  название столбца.

        Returns:
            Найденное допустимое значение или None.
        """
        allowed_values = (
            self._column_validations.get(marketplace, {}).get(column_name)
        )

        if not allowed_values or not self._ai_comparator:
            return None

        value_str = value.strip()

        # 1. Точное совпадение
        if value_str in allowed_values:
            logger.info(f"Точное совпадение: '{value_str}'")
            self._log_match(value_str, value_str, marketplace, column_name, "Точное совпадение")
            return value_str

        # 2. Совпадение с нормализацией (регистр + ё→е)
        value_normalized = self._normalize(value_str)
        for allowed in allowed_values:
            if self._normalize(allowed) == value_normalized:
                logger.info(f"Совпадение с нормализацией: '{value_str}' → '{allowed}'")
                self._log_match(
                    value_str, allowed, marketplace, column_name,
                    "Нормализация (регистр/ё-е)"
                )
                return allowed

        # 3. Совпадение по извлечённому числу
        number = self._extract_number(value_str)
        if number:
            if number in allowed_values:
                logger.info(f"Извлечено число: '{value_str}' → '{number}'")
                self._log_match(
                    value_str, number, marketplace, column_name, "Извлечение числа"
                )
                return number

            for allowed in allowed_values:
                if self._extract_number(allowed) == number:
                    logger.info(f"Совпадение по числу: '{value_str}' → '{allowed}'")
                    self._log_match(
                        value_str, allowed, marketplace, column_name, "Извлечение числа"
                    )
                    return allowed

        # 4. Частичное совпадение по словам
        value_words = set(value_normalized.split())
        for allowed in allowed_values:
            allowed_words = set(self._normalize(allowed).split())
            if value_words and value_words.issubset(allowed_words):
                logger.info(f"Частичное совпадение: '{value_str}' → '{allowed}'")
                self._log_match(
                    value_str, allowed, marketplace, column_name,
                    "Частичное совпадение (слова)"
                )
                return allowed

        # 5. AI-запрос (только если все предыдущие методы не сработали)
        logger.info(f"AI-проверка: '{value_str}' для столбца '{column_name}'...")
        matched_value = await self._ai_comparator.match_value_with_list(
            value_str, allowed_values, column_name=column_name
        )

        if matched_value:
            logger.info(f"AI нашло: '{value_str}' → '{matched_value}'")
            self._log_match(
                value_str, matched_value, marketplace, column_name, "AI запрос"
            )
            return matched_value

        logger.warning(f"AI не нашло совпадение для '{value_str}'")
        return None

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """Нормализует текст: нижний регистр, замена ё→е."""
        return text.lower().replace("ё", "е").strip()

    @staticmethod
    def _extract_number(text: str) -> Optional[str]:
        """Извлекает первое число из строки типа '1 шт', '2 компрессора'."""
        numbers = re.findall(r"\d+", text)
        return numbers[0] if numbers else None

    def _log_match(
        self,
        original: str,
        matched: str,
        marketplace: str,
        column_name: str,
        method: str,
    ) -> None:
        """Записывает успешное сопоставление в общий лог."""
        self._ai_validation_log.append({
            "Маркетплейс": marketplace.upper(),
            "Столбец": column_name,
            "Исходное значение": original,
            "Сопоставлено с": matched,
            "Метод": method,
        })