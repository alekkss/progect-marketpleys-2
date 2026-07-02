"""
Модуль синхронизации совпадающих столбцов между маркетплейсами.

Отвечает за одну задачу: по готовой схеме сопоставлений заполнить
пустые ячейки МП-файлов значениями из других маркетплейсов.

Поддерживаемые типы сопоставлений:
    - Тройные (matches_all_three): WB ↔ Ozon ↔ Яндекс.
    - Парные (matches_1_2, matches_1_3, matches_2_3): любые два МП.

Паттерн: Strategy — пары маркетплейсов описаны декларативно в PAIR_CONFIGS,
алгоритм синхронизации единый для всех пар.
Паттерн: Dependency Injection — все зависимости передаются через конструктор.
"""

import re
from typing import Dict, List, Optional

import pandas as pd

from config.config import (
    ALL_DIMENSION_COLUMN_NAMES,
    PHOTO_COLUMNS,
    TNVED_COLUMN_NAMES,
    TNVED_NUMERIC_ONLY_MARKETPLACES,
    is_excluded_column,
)
from services.sync.ai_validator import AiValidator
from services.sync.article_aligner import ArticleAligner
from services.sync.photo_syncer import PhotoSyncer
from services.sync.value_converter import ValueConverter
from utils.logger_config import setup_logger

logger = setup_logger("column_syncer")

# Декларативное описание пар маркетплейсов для парной синхронизации.
# Паттерн Strategy: изменение/добавление пары — только в этом списке.
_PAIR_CONFIGS: List[tuple] = [
    ("matches_1_2", "wildberries", "ozon",    "column_1", "column_2"),
    ("matches_1_3", "wildberries", "yandex",  "column_1", "column_3"),
    ("matches_2_3", "ozon",        "yandex",  "column_2", "column_3"),
]

# Регулярное выражение для извлечения числового кода ТНВЭД
# Берёт первую непрерывную последовательность цифр из строки
_TNVED_CODE_PATTERN = re.compile(r"^(\d+)")

# Множество всех фото-столбцов всех МП для быстрой проверки в _should_skip_match.
# Фото-столбцы исключаются из стандартной синхронизации — ими управляет PhotoSyncer.
_ALL_PHOTO_COLUMNS: set = {
    col_name
    for mp_cols in PHOTO_COLUMNS.values()
    for col_name in mp_cols.values()
}


class ColumnSyncer:
    """
    Синхронизирует данные между столбцами МП по схеме сопоставлений.

    Принимает зависимости через конструктор (Dependency Inversion):
        - comparison_result: схема сопоставлений из БД.
        - article_columns:   маппинг {маркетплейс: столбец артикула}.
        - value_converter:   экземпляр ValueConverter.
        - ai_validator:      экземпляр AiValidator.
        - article_aligner:   экземпляр ArticleAligner.
        - changes_log:       общий лог изменений (передаётся по ссылке).
        - resolved_wb_dims:  реальные имена столбцов габаритов WB
                             (результат DimensionsSynchronizer.sync_dimensions).

    Фото-столбцы делегируются в PhotoSyncer — стандартная логика
    «скопировать значение как есть» для них неприменима.
    """

    def __init__(
        self,
        comparison_result: Dict,
        article_columns: Dict[str, str],
        value_converter: ValueConverter,
        ai_validator: AiValidator,
        article_aligner: ArticleAligner,
        changes_log: Dict[str, List],
        resolved_wb_dims: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Args:
            comparison_result: словарь сопоставлений из схемы.
            article_columns:   маппинг {маркетплейс: название столбца артикула}.
            value_converter:   конвертер единиц измерения.
            ai_validator:      валидатор значений через AI.
            article_aligner:   строитель article_map.
            changes_log:       общий лог изменений — изменяется на месте.
            resolved_wb_dims:  словарь {'length': '...', 'width': '...', 'height': '...'}
                               или None, если столбцы WB не найдены.
        """
        self._comparison_result = comparison_result
        self._article_columns = article_columns
        self._value_converter = value_converter
        self._ai_validator = ai_validator
        self._article_aligner = article_aligner
        self._changes_log = changes_log
        self._resolved_wb_dims = resolved_wb_dims

        # PhotoSyncer создаётся один раз — получает те же ссылки на
        # article_columns и changes_log, что и ColumnSyncer.
        # Dependency Inversion: PhotoSyncer зависит от абстракций,
        # а не от конкретных МП.
        self._photo_syncer = PhotoSyncer(
            article_columns=article_columns,
            changes_log=changes_log,
        )

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    async def sync_all_matches(
        self, dfs: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """
        Синхронизирует все совпадающие столбцы из схемы сопоставлений.

        Порядок:
            1. Выравнивание артикулов (повторно после XML-индекса).
            2. Тройные совпадения (все 3 МП).
            3. Парные совпадения (все комбинации двух МП).
            4. Фото-столбцы (через PhotoSyncer — отдельная логика).

        Фото обрабатываются последними, потому что стандартные шаги 2–3
        могут заполнить другие столбцы того же артикула, а PhotoSyncer
        проверяет только пустые фото-ячейки — порядок не критичен,
        но так логичнее для отладки.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Обновлённый словарь DataFrame.
        """
        synced_dfs = {
            "wildberries": dfs["wildberries"].copy(),
            "ozon": dfs["ozon"].copy(),
            "yandex": dfs["yandex"].copy(),
        }

        # Выравниваем артикулы перед синхронизацией
        synced_dfs = self._article_aligner.align(synced_dfs)

        logger.info("\n[*] Синхронизирую совпадения всех 3 маркетплейсов...")
        synced_dfs = await self._sync_three_way_matches(synced_dfs)

        logger.info("\n[*] Синхронизирую совпадения между парами маркетплейсов...")
        synced_dfs = await self._sync_two_way_matches(synced_dfs)

        logger.info("\n[*] Синхронизирую фото-столбцы...")
        synced_dfs = self._photo_syncer.sync_photos(synced_dfs)

        return synced_dfs

    def postprocess_wb_dimensions(
        self, dfs: Dict[str, pd.DataFrame]
    ) -> int:
        """
        Постобработка габаритов WB: конвертирует мм → см для значений,
        записанных из Ozon (защита от записи миллиметровых значений в WB).

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Количество сконвертированных значений.
        """
        if "wildberries" not in dfs or not self._resolved_wb_dims:
            return 0

        df_wb = dfs["wildberries"]
        wb_dimension_columns = [
            self._resolved_wb_dims["length"],
            self._resolved_wb_dims["width"],
            self._resolved_wb_dims["height"],
        ]

        # Проверяем наличие всех столбцов
        if not all(col in df_wb.columns for col in wb_dimension_columns):
            return 0

        converted_count = 0
        article_col = self._article_columns["wildberries"]

        for change in self._changes_log.get("wildberries", []):
            if (
                change.get("source_marketplace") != "ozon"
                or change.get("column") not in wb_dimension_columns
            ):
                continue

            article = change.get("article")
            column = change.get("column")

            mask = df_wb[article_col].astype(str).str.strip() == str(article).strip()
            if not mask.any():
                continue

            idx = df_wb[mask].index[0]
            value = df_wb.at[idx, column]

            if pd.notna(value):
                try:
                    numeric_value = float(value)
                    # Конвертируем только если >= 100 (защита от повторной конвертации)
                    if numeric_value >= 100:
                        converted = round(numeric_value / 10, 1)
                        df_wb.at[idx, column] = converted
                        converted_count += 1
                        logger.info(
                            f"  ✓ [{article}] {column}: {numeric_value} мм → {converted} см"
                        )
                except (ValueError, TypeError):
                    pass

        return converted_count

    # ------------------------------------------------------------------
    # Тройные совпадения
    # ------------------------------------------------------------------

    async def _sync_three_way_matches(
        self, dfs: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """
        Синхронизирует совпадения всех трёх маркетплейсов.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Обновлённый словарь DataFrame.
        """
        matches = self._comparison_result.get("matches_all_three", [])
        if not matches:
            logger.info("  Нет тройных совпадений для синхронизации")
            return dfs

        total_filled = 0
        skipped_count = 0

        for match in matches:
            col_wb     = match.get("column_1")
            col_ozon   = match.get("column_2")
            col_yandex = match.get("column_3")

            if not all([col_wb, col_ozon, col_yandex]):
                continue

            if self._should_skip_match(col_wb, col_ozon, col_yandex):
                skipped_count += 1
                continue

            if not self._columns_exist(
                dfs,
                [("wildberries", col_wb), ("ozon", col_ozon), ("yandex", col_yandex)],
            ):
                continue

            filled = await self._sync_three_columns(
                dfs, col_wb, col_ozon, col_yandex
            )

            if filled > 0:
                confidence = int(match.get("confidence", 0) * 100)
                logger.info(
                    f"  ✓ Заполнено {filled} значений: "
                    f"'{col_wb}' ↔ '{col_ozon}' ↔ '{col_yandex}' ({confidence}%)"
                )
                total_filled += filled

        if skipped_count > 0:
            logger.info(f"[!] Пропущено {skipped_count} исключённых столбцов")
        logger.info(
            f"[+] Тройные совпадения: заполнено {total_filled} ячеек"
        )

        return dfs

    # ------------------------------------------------------------------
    # Парные совпадения
    # ------------------------------------------------------------------

    async def _sync_two_way_matches(
        self, dfs: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """
        Синхронизирует совпадения между парами маркетплейсов.

        Пары описаны в константе _PAIR_CONFIGS.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Обновлённый словарь DataFrame.
        """
        total_filled = 0
        skipped_count = 0

        for match_key, mp1, mp2, col_key1, col_key2 in _PAIR_CONFIGS:
            matches = self._comparison_result.get(match_key, [])
            if not matches:
                continue

            for match in matches:
                col1 = match.get(col_key1)
                col2 = match.get(col_key2)

                if not all([col1, col2]):
                    continue

                if self._should_skip_match(col1, col2):
                    skipped_count += 1
                    continue

                if not self._columns_exist(dfs, [(mp1, col1), (mp2, col2)]):
                    continue

                filled = await self._sync_two_columns(dfs, mp1, mp2, col1, col2)

                if filled > 0:
                    confidence = int(match.get("confidence", 0) * 100)
                    logger.info(
                        f"  ✓ Заполнено {filled} значений: "
                        f"{mp1}:'{col1}' ↔ {mp2}:'{col2}' ({confidence}%)"
                    )
                    total_filled += filled

        if skipped_count > 0:
            logger.info(f"[!] Пропущено {skipped_count} исключённых столбцов")
        logger.info(
            f"[+] Парные совпадения: заполнено {total_filled} ячеек"
        )

        return dfs

    # ------------------------------------------------------------------
    # Синхронизация отдельных столбцов
    # ------------------------------------------------------------------

    async def _sync_three_columns(
        self,
        dfs: Dict[str, pd.DataFrame],
        col_wb: str,
        col_ozon: str,
        col_yandex: str,
    ) -> int:
        """
        Синхронизирует данные между тремя столбцами по артикулам.

        Args:
            dfs:        словарь {маркетплейс: DataFrame}.
            col_wb:     название столбца Wildberries.
            col_ozon:   название столбца Ozon.
            col_yandex: название столбца Яндекс.

        Returns:
            Количество заполненных ячеек.
        """
        filled_count = 0

        unit_wb     = self._value_converter.detect_unit(col_wb)
        unit_ozon   = self._value_converter.detect_unit(col_ozon)
        unit_yandex = self._value_converter.detect_unit(col_yandex)

        wb_data     = self._article_aligner.create_article_map(
            dfs["wildberries"], self._article_columns["wildberries"], col_wb
        )
        ozon_data   = self._article_aligner.create_article_map(
            dfs["ozon"], self._article_columns["ozon"], col_ozon
        )
        yandex_data = self._article_aligner.create_article_map(
            dfs["yandex"], self._article_columns["yandex"], col_yandex
        )

        all_articles = set(wb_data) | set(ozon_data) | set(yandex_data)

        for article in all_articles:
            if not article:
                continue

            values = {
                "wildberries": self._scalar(wb_data.get(article, {}).get("value")),
                "ozon":        self._scalar(ozon_data.get(article, {}).get("value")),
                "yandex":      self._scalar(yandex_data.get(article, {}).get("value")),
            }

            # Находим первое непустое значение и его источник
            source_value, source_mp = self._find_source(values)
            if source_value is None:
                continue

            source_unit = {
                "wildberries": unit_wb,
                "ozon":        unit_ozon,
                "yandex":      unit_yandex,
            }[source_mp]

            # Заполняем каждый МП по очереди
            for mp, col, unit, data in [
                ("wildberries", col_wb,     unit_wb,     wb_data),
                ("ozon",        col_ozon,   unit_ozon,   ozon_data),
                ("yandex",      col_yandex, unit_yandex, yandex_data),
            ]:
                if article not in data:
                    continue
                if pd.notna(values[mp]) and str(values[mp]).strip():
                    continue  # Уже заполнено

                idx = data[article]["index"]

                converted = self._value_converter.convert_value(
                    source_value, source_unit, unit
                )
                final = await self._ai_validator.validate_multiple_values(
                    converted, mp, col
                )
                written = self._write_value(
                    dfs, mp, idx, col, converted, final, source_mp
                )
                filled_count += written

        return filled_count

    async def _sync_two_columns(
        self,
        dfs: Dict[str, pd.DataFrame],
        mp1: str,
        mp2: str,
        col1: str,
        col2: str,
    ) -> int:
        """
        Синхронизирует данные между двумя столбцами по артикулам.

        Args:
            dfs:  словарь {маркетплейс: DataFrame}.
            mp1:  первый маркетплейс.
            mp2:  второй маркетплейс.
            col1: название столбца первого МП.
            col2: название столбца второго МП.

        Returns:
            Количество заполненных ячеек.
        """
        filled_count = 0

        unit1 = self._value_converter.detect_unit(col1)
        unit2 = self._value_converter.detect_unit(col2)

        data1 = self._article_aligner.create_article_map(
            dfs[mp1], self._article_columns[mp1], col1
        )
        data2 = self._article_aligner.create_article_map(
            dfs[mp2], self._article_columns[mp2], col2
        )

        all_articles = set(data1) | set(data2)

        for article in all_articles:
            if not article:
                continue

            val1 = self._scalar(data1.get(article, {}).get("value"))
            val2 = self._scalar(data2.get(article, {}).get("value"))

            # Первый пуст, второй заполнен → записываем в первый
            if (
                article in data1
                and article in data2
                and (pd.isna(val1) or not str(val1).strip())
                and pd.notna(val2)
                and str(val2).strip()
            ):
                idx = data1[article]["index"]
                converted = self._value_converter.convert_value(val2, unit2, unit1)
                final = await self._ai_validator.validate_multiple_values(
                    converted, mp1, col1
                )
                filled_count += self._write_value(
                    dfs, mp1, idx, col1, converted, final, mp2
                )

            # Второй пуст, первый заполнен → записываем во второй
            elif (
                article in data1
                and article in data2
                and (pd.isna(val2) or not str(val2).strip())
                and pd.notna(val1)
                and str(val1).strip()
            ):
                idx = data2[article]["index"]
                converted = self._value_converter.convert_value(val1, unit1, unit2)
                final = await self._ai_validator.validate_multiple_values(
                    converted, mp2, col2
                )
                filled_count += self._write_value(
                    dfs, mp2, idx, col2, converted, final, mp1
                )

        return filled_count

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _write_value(
        self,
        dfs: Dict[str, pd.DataFrame],
        mp: str,
        idx: int,
        col: str,
        converted_value: object,
        final_value: Optional[str],
        source_mp: str,
    ) -> int:
        """
        Записывает значение в ячейку DataFrame с учётом типа столбца.

        Если столбец является ТНВЭД и целевой МП принимает только числовой
        код — извлекает числовую часть перед записью.
        Если есть validation и AI не нашло совпадение — ячейка не обновляется.

        Args:
            dfs:             словарь DataFrame.
            mp:              маркетплейс-получатель.
            idx:             индекс строки.
            col:             название столбца.
            converted_value: конвертированное значение.
            final_value:     результат AI-валидации (None если не прошло).
            source_mp:       маркетплейс-источник (для лога).

        Returns:
            1 если значение записано, 0 если пропущено.
        """
        if final_value:
            value_to_set = final_value
        elif not self._ai_validator._column_validations.get(mp, {}).get(col):
            # Нет validation — записываем как есть
            value_to_set = converted_value
        else:
            # Есть validation, но AI не нашло совпадение — пропускаем
            logger.warning(
                f"⚠️ [{mp.upper()}] Пропущено '{converted_value}' "
                f"для '{col}' (не прошло validation)"
            )
            return 0

        # Очистка ТНВЭД: извлекаем только числовой код для WB и Яндекс
        value_to_set = self._apply_tnved_cleanup(mp, col, value_to_set)

        try:
            series = dfs[mp][col]
            if isinstance(series, pd.DataFrame):
                series = series.iloc[:, 0]

            if pd.api.types.is_numeric_dtype(series.dtype):
                value_to_set = pd.to_numeric(value_to_set, errors="coerce")

            dfs[mp].at[idx, col] = value_to_set
            self._log_change(mp, dfs[mp], idx, col, value_to_set, source_mp)
            return 1

        except Exception as e:
            logger.error(f"Ошибка записи [{mp}] '{col}': {e}")
            return 0

    @staticmethod
    def _extract_tnved_code(value: str) -> str:
        """
        Извлекает числовой код ТНВЭД из строки с пояснением.

        Примеры:
            "2103909009 - Прочие продукты..." → "2103909009"
            "2103909009"                      → "2103909009"
            ""                                → ""

        Args:
            value: строковое значение ТНВЭД (возможно, с пояснением).

        Returns:
            Числовой код ТНВЭД или исходная строка, если число не найдено.
        """
        if not value or not str(value).strip():
            return str(value) if value else ""

        text = str(value).strip()
        match = _TNVED_CODE_PATTERN.match(text)
        if match:
            return match.group(1)

        return text

    @staticmethod
    def _apply_tnved_cleanup(mp: str, col: str, value: object) -> object:
        """
        Применяет очистку ТНВЭД если столбец является кодом ТН ВЭД
        и целевой маркетплейс принимает только числовой код.

        Args:
            mp:    маркетплейс-получатель.
            col:   название столбца.
            value: значение для записи.

        Returns:
            Очищенное значение (только число) или исходное значение.
        """
        if (
            col in TNVED_COLUMN_NAMES
            and mp in TNVED_NUMERIC_ONLY_MARKETPLACES
        ):
            original = str(value) if value is not None else ""
            cleaned = ColumnSyncer._extract_tnved_code(original)
            if cleaned != original:
                logger.info(
                    f"  🔢 [{mp.upper()}] ТНВЭД очищен: "
                    f"'{original}' → '{cleaned}'"
                )
            return cleaned

        return value

    def _log_change(
        self,
        mp: str,
        df: pd.DataFrame,
        idx: int,
        col: str,
        value: object,
        source_mp: str,
    ) -> None:
        """
        Записывает изменение в общий лог.

        Args:
            mp:        маркетплейс-получатель.
            df:        DataFrame маркетплейса (для получения артикула).
            idx:       индекс строки.
            col:       название столбца.
            value:     записанное значение.
            source_mp: маркетплейс-источник.
        """
        article_col = self._article_columns.get(mp, "")
        article = str(df.at[idx, article_col]) if article_col in df.columns else ""

        self._changes_log[mp].append({
            "article":            article,
            "column":             col,
            "new_value":          str(value),
            "source_marketplace": source_mp,
        })

    @staticmethod
    def _scalar(value: object) -> object:
        """
        Извлекает скалярное значение из pd.Series если нужно.

        Args:
            value: значение из article_map (может быть Series при дублирующихся индексах).

        Returns:
            Скалярное значение или None.
        """
        if isinstance(value, pd.Series):
            return value.iloc[0] if not value.empty else None
        return value

    @staticmethod
    def _find_source(
        values: Dict[str, object]
    ) -> tuple[Optional[object], Optional[str]]:
        """
        Находит первое непустое значение среди МП и возвращает его с источником.

        Args:
            values: словарь {маркетплейс: значение}.

        Returns:
            Кортеж (значение, маркетплейс-источник) или (None, None).
        """
        for mp, val in values.items():
            if pd.notna(val) and str(val).strip():
                return val, mp
        return None, None

    @staticmethod
    def _should_skip_match(*columns: str) -> bool:
        """
        Проверяет, нужно ли пропустить сопоставление в стандартной обработке.

        Пропускаем если столбец:
            - исключён из синхронизации (EXCLUDED_COLUMNS)
            - относится к габаритам (обрабатываются DimensionsSynchronizer)
            - является фото-столбцом (обрабатываются PhotoSyncer)

        Args:
            columns: названия столбцов сопоставления.

        Returns:
            True если сопоставление нужно пропустить.
        """
        for col in columns:
            if is_excluded_column(col):
                return True
            if col in ALL_DIMENSION_COLUMN_NAMES:
                logger.info(f"⏭️ Пропуск (габариты): {' ↔ '.join(columns)}")
                return True
            if col in _ALL_PHOTO_COLUMNS:
                logger.info(f"⏭️ Пропуск (фото → PhotoSyncer): {' ↔ '.join(columns)}")
                return True
        return False

    @staticmethod
    def _columns_exist(
        dfs: Dict[str, pd.DataFrame],
        pairs: List[tuple],
    ) -> bool:
        """
        Проверяет, что все указанные столбцы существуют в своих DataFrame.

        Args:
            dfs:   словарь DataFrame.
            pairs: список кортежей (маркетплейс, столбец).

        Returns:
            True если все столбцы найдены.
        """
        return all(col in dfs[mp].columns for mp, col in pairs)
