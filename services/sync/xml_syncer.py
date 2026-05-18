"""
Модуль синхронизации МП-файлов из XML-каталога (YML-фид).

Отвечает за одну задачу: заполнить пустые ячейки маркетплейсов
данными из XML-каталога по артикулу (vendorCode).

Два прохода:
    1. sync_from_xml      — обычные поля из сопоставлений схемы.
    2. sync_dimensions_from_xml — габариты из поля [XML] dimensions.

Поле [XML] dimensions обрабатывается отдельно, потому что требует
парсинга строки «д/ш/в» и конвертации единиц для каждого МП.

Паттерн: Dependency Injection — все зависимости передаются через конструктор.
"""

from typing import Dict, List, Optional

import pandas as pd

from services.sync.ai_validator import AiValidator
from services.sync.dimensions_synchronizer import DimensionsSynchronizer
from services.sync.value_converter import ValueConverter
from utils.logger_config import setup_logger

logger = setup_logger("xml_syncer")

# Группы сопоставлений схемы, содержащие column_4 (XML-источник)
_XML_GROUPS: List[str] = [
    "matches_all_four",
    "matches_triple_1_2_4",
    "matches_triple_1_3_4",
    "matches_triple_2_3_4",
    "matches_pair_1_4",
    "matches_pair_2_4",
    "matches_pair_3_4",
]

# Поле XML с габаритами — обрабатывается отдельно через sync_dimensions_from_xml
_XML_DIMENSIONS_FIELD = "[XML] dimensions"

# Столбцы габаритов Ozon
_OZON_DIM_COLUMNS: Dict[str, str] = {
    "length": "Длина упаковки, мм*",
    "width":  "Ширина упаковки, мм*",
    "height": "Высота упаковки, мм*",
}

# Составной столбец габаритов Яндекс
_YANDEX_COMPOSITE_COL = "Габариты с упаковкой, см"


class XmlSyncer:
    """
    Заполняет пустые ячейки МП-файлов данными из XML-каталога.

    Привязка товаров выполняется по артикулу:
        XML vendorCode = Артикул продавца (WB) = Артикул* (Ozon) = Ваш SKU * (Яндекс).

    Принимает зависимости через конструктор (Dependency Inversion):
        - comparison_result:   схема сопоставлений из БД.
        - article_columns:     маппинг {маркетплейс: столбец артикула}.
        - value_converter:     экземпляр ValueConverter.
        - ai_validator:        экземпляр AiValidator.
        - changes_log:         общий лог изменений (передаётся по ссылке).
        - resolved_wb_dims:    реальные имена столбцов габаритов WB.
        - xml_offer_data:      список офферов из XML.
        - xml_categories:      справочник категорий XML.
        - selected_category_ids: фильтр по категориям (пустое множество = все).
    """

    def __init__(
        self,
        comparison_result: Dict,
        article_columns: Dict[str, str],
        value_converter: ValueConverter,
        ai_validator: AiValidator,
        changes_log: Dict[str, List],
        resolved_wb_dims: Optional[Dict[str, str]] = None,
        xml_offer_data: Optional[List[Dict]] = None,
        xml_categories: Optional[Dict[str, str]] = None,
        selected_category_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Args:
            comparison_result:     словарь сопоставлений из схемы.
            article_columns:       маппинг {маркетплейс: название столбца артикула}.
            value_converter:       конвертер единиц измерения.
            ai_validator:          валидатор значений через AI.
            changes_log:           общий лог изменений — изменяется на месте.
            resolved_wb_dims:      словарь {'length': '...', ...} или None.
            xml_offer_data:        список офферов из XML (ключи с префиксами [XML]).
            xml_categories:        словарь {category_id: category_name}.
            selected_category_ids: список ID категорий для фильтрации офферов.
        """
        self._comparison_result = comparison_result
        self._article_columns = article_columns
        self._value_converter = value_converter
        self._ai_validator = ai_validator
        self._changes_log = changes_log
        self._resolved_wb_dims = resolved_wb_dims
        self._xml_offer_data: List[Dict] = xml_offer_data or []
        self._xml_categories: Dict[str, str] = xml_categories or {}
        self._selected_category_ids: set = (
            set(selected_category_ids) if selected_category_ids else set()
        )

        # Индекс XML-офферов по артикулу — строится при первом вызове build_index
        self.xml_article_map: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    def build_index(self) -> None:
        """
        Строит индекс XML-офферов по артикулу (vendorCode).

        Если заданы selected_category_ids — в индекс попадают только
        офферы из выбранных категорий. Вызывать до sync_from_xml
        и sync_dimensions_from_xml.
        """
        self.xml_article_map = {}
        skipped_by_category = 0

        for offer in self._xml_offer_data:
            if self._selected_category_ids:
                offer_cat_id = offer.get("[XML] categoryId", "").strip()
                if offer_cat_id not in self._selected_category_ids:
                    skipped_by_category += 1
                    continue

            vendor_code = offer.get("[XML] vendorCode", "").strip()
            if vendor_code:
                self.xml_article_map[vendor_code] = offer

        logger.info(
            f"📦 XML индекс: {len(self.xml_article_map)} офферов "
            f"из {len(self._xml_offer_data)} всего"
        )

        if skipped_by_category > 0:
            logger.info(
                f"   📂 Пропущено по фильтру категорий: {skipped_by_category} офферов"
            )

        for i, (article, data) in enumerate(self.xml_article_map.items()):
            if i >= 3:
                break
            logger.debug(f"   [{article}]: {len(data)} полей")

    async def sync_from_xml(self, dfs: Dict[str, pd.DataFrame]) -> int:
        """
        Заполняет пустые ячейки МП из XML-каталога по обычным полям схемы.

        Проходит по всем группам сопоставлений с column_4 (XML).
        Поле [XML] dimensions пропускается — оно обрабатывается отдельно
        в sync_dimensions_from_xml.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Количество заполненных ячеек.
        """
        if not self.xml_article_map:
            logger.info("   XML индекс пуст — нечего заполнять")
            return 0

        filled_count = 0

        # Маппинг column_key → (маркетплейс, столбец артикула)
        mp_info = {
            "column_1": ("wildberries", self._article_columns["wildberries"]),
            "column_2": ("ozon",        self._article_columns["ozon"]),
            "column_3": ("yandex",      self._article_columns["yandex"]),
        }

        for group_key in _XML_GROUPS:
            matches = self._comparison_result.get(group_key, [])
            if not matches:
                continue

            logger.info(
                f"\n📦 Обработка группы '{group_key}': {len(matches)} сопоставлений"
            )

            for match in matches:
                xml_field = match.get("column_4")
                if not xml_field:
                    continue

                # Габариты обрабатываются отдельно
                if xml_field == _XML_DIMENSIONS_FIELD:
                    logger.debug(
                        f"   ⏭️ Пропуск '{xml_field}' — "
                        f"обработка в sync_dimensions_from_xml"
                    )
                    continue

                filled_count += await self._fill_mp_from_xml_field(
                    dfs, match, xml_field, mp_info
                )

        logger.info(f"\n📦 Итого из XML заполнено: {filled_count} ячеек")
        return filled_count

    def sync_dimensions_from_xml(self, dfs: Dict[str, pd.DataFrame]) -> int:
        """
        Заполняет габариты МП из поля [XML] dimensions.

        XML хранит габариты в формате «длина/ширина/высота» в сантиметрах.
        Конвертация единиц:
            - WB:     раздельные столбцы в см (как есть).
            - Ozon:   раздельные столбцы в мм (см * 10).
            - Яндекс: составной формат «д/ш/в» в см.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Количество заполненных ячеек.
        """
        if not self.xml_article_map:
            return 0

        filled_count = 0
        logger.info("\n📐 Заполнение габаритов из XML ([XML] dimensions)...")

        for article, xml_offer in self.xml_article_map.items():
            raw_dimensions = xml_offer.get(_XML_DIMENSIONS_FIELD, "")
            if not raw_dimensions or not str(raw_dimensions).strip():
                continue

            parsed = DimensionsSynchronizer.parse_composite_dimensions(
                str(raw_dimensions).strip()
            )
            if not parsed:
                logger.warning(
                    f"   ⚠️ Не удалось распарсить XML dimensions "
                    f"для [{article}]: '{raw_dimensions}'"
                )
                continue

            length_cm = parsed["length"]
            width_cm  = parsed["width"]
            height_cm = parsed["height"]

            filled_count += self._write_wb_dimensions_from_xml(
                dfs, article, length_cm, width_cm, height_cm
            )
            filled_count += self._write_ozon_dimensions_from_xml(
                dfs, article, length_cm, width_cm, height_cm
            )
            filled_count += self._write_yandex_dimensions_from_xml(
                dfs, article, length_cm, width_cm, height_cm
            )

        logger.info(f"   📐 Из XML габаритов заполнено: {filled_count} ячеек")
        return filled_count

    # ------------------------------------------------------------------
    # Заполнение обычных полей
    # ------------------------------------------------------------------

    async def _fill_mp_from_xml_field(
        self,
        dfs: Dict[str, pd.DataFrame],
        match: Dict,
        xml_field: str,
        mp_info: Dict[str, tuple],
    ) -> int:
        """
        Заполняет пустые ячейки одного сопоставления из XML-поля.

        Args:
            dfs:      словарь DataFrame.
            match:    словарь одного сопоставления из схемы.
            xml_field: название XML-поля (например, '[XML param] Цвет').
            mp_info:  маппинг column_key → (маркетплейс, столбец артикула).

        Returns:
            Количество заполненных ячеек.
        """
        filled_count = 0

        # Собираем МП-столбцы, участвующие в этом сопоставлении
        mp_columns = {}
        for col_key, (mp_name, article_col) in mp_info.items():
            mp_col_name = match.get(col_key)
            if mp_col_name and mp_name in dfs:
                if mp_col_name in dfs[mp_name].columns:
                    mp_columns[col_key] = {
                        "marketplace":   mp_name,
                        "column_name":   mp_col_name,
                        "article_column": article_col,
                    }

        if not mp_columns:
            return 0

        for col_key, info in mp_columns.items():
            mp_name    = info["marketplace"]
            col_name   = info["column_name"]
            article_col = info["article_column"]
            df         = dfs[mp_name]

            if article_col not in df.columns:
                continue

            filled_in_match = 0

            for idx, row in df.iterrows():
                article = row.get(article_col)
                if pd.isna(article) or not str(article).strip():
                    continue

                article_str = str(article).strip()

                # Пропускаем уже заполненные ячейки
                current_value = row.get(col_name)
                if isinstance(current_value, pd.Series):
                    current_value = (
                        current_value.iloc[0] if not current_value.empty else None
                    )
                if pd.notna(current_value) and str(current_value).strip():
                    continue

                # Ищем артикул в XML
                xml_offer = self.xml_article_map.get(article_str)
                if not xml_offer:
                    continue

                xml_value = xml_offer.get(xml_field, "")
                if not xml_value or not str(xml_value).strip():
                    continue

                # Конвертируем единицы если нужно
                source_unit = self._value_converter.detect_unit(xml_field)
                target_unit = self._value_converter.detect_unit(col_name)
                converted   = self._value_converter.convert_value(
                    str(xml_value).strip(), source_unit, target_unit
                )

                # Валидация через AI
                final_value = await self._ai_validator.validate_multiple_values(
                    converted, mp_name, col_name
                )

                written = self._write_xml_value(
                    df, mp_name, idx, col_name, article_str,
                    converted, final_value
                )
                filled_count += written
                filled_in_match += written

            if filled_in_match > 0:
                logger.info(
                    f"   ✓ {xml_field} → {mp_name}:'{col_name}': "
                    f"заполнено {filled_in_match} ячеек"
                )

        return filled_count

    def _write_xml_value(
        self,
        df: pd.DataFrame,
        mp_name: str,
        idx: int,
        col_name: str,
        article_str: str,
        converted_value: object,
        final_value: Optional[str],
    ) -> int:
        """
        Записывает значение из XML в ячейку DataFrame.

        Args:
            df:              DataFrame маркетплейса.
            mp_name:         название маркетплейса.
            idx:             индекс строки.
            col_name:        название столбца.
            article_str:     артикул (для лога).
            converted_value: конвертированное значение.
            final_value:     результат AI-валидации.

        Returns:
            1 если записано, 0 если пропущено.
        """
        if final_value:
            value_to_set = final_value
        elif not self._ai_validator._column_validations.get(mp_name, {}).get(col_name):
            value_to_set = converted_value
        else:
            logger.warning(
                f"⚠️ [{mp_name.upper()}] XML→МП пропущено: "
                f"'{converted_value}' для '{col_name}' (не прошло validation)"
            )
            return 0

        series = df[col_name]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[:, 0]
        if pd.api.types.is_numeric_dtype(series.dtype):
            value_to_set = pd.to_numeric(value_to_set, errors="coerce")

        try:
            df.at[idx, col_name] = value_to_set
            self._log_change(mp_name, article_str, col_name, value_to_set)
            return 1
        except Exception as e:
            logger.error(
                f"Ошибка записи XML→{mp_name}: "
                f"артикул={article_str}, столбец={col_name}: {e}"
            )
            return 0

    # ------------------------------------------------------------------
    # Запись габаритов из XML
    # ------------------------------------------------------------------

    def _write_wb_dimensions_from_xml(
        self,
        dfs: Dict[str, pd.DataFrame],
        article: str,
        length_cm: float,
        width_cm: float,
        height_cm: float,
    ) -> int:
        """Записывает габариты (см) в раздельные столбцы WB из XML."""
        if "wildberries" not in dfs or not self._resolved_wb_dims:
            return 0

        df = dfs["wildberries"]
        article_col = self._article_columns["wildberries"]

        if article_col not in df.columns:
            return 0

        mask = df[article_col].astype(str).str.strip() == article
        if not mask.any():
            return 0

        idx = df[mask].index[0]
        written = 0

        for dim_key, value_cm in [
            ("length", length_cm),
            ("width",  width_cm),
            ("height", height_cm),
        ]:
            col_name = self._resolved_wb_dims[dim_key]
            if col_name not in df.columns:
                continue

            current = df.at[idx, col_name]
            if pd.notna(current) and str(current).strip():
                continue  # Уже заполнено

            df.at[idx, col_name] = value_cm
            written += 1
            self._log_change("wildberries", article, col_name, value_cm)
            logger.debug(f"   [XML→WB] {article}: {col_name}={value_cm} см")

        return written

    def _write_ozon_dimensions_from_xml(
        self,
        dfs: Dict[str, pd.DataFrame],
        article: str,
        length_cm: float,
        width_cm: float,
        height_cm: float,
    ) -> int:
        """Записывает габариты (мм) в раздельные столбцы Ozon из XML."""
        if "ozon" not in dfs:
            return 0

        df = dfs["ozon"]
        article_col = self._article_columns["ozon"]

        if article_col not in df.columns:
            return 0

        mask = df[article_col].astype(str).str.strip() == article
        if not mask.any():
            return 0

        idx = df[mask].index[0]
        written = 0

        for dim_key, value_cm in [
            ("length", length_cm),
            ("width",  width_cm),
            ("height", height_cm),
        ]:
            col_name = _OZON_DIM_COLUMNS[dim_key]
            if col_name not in df.columns:
                continue

            current = df.at[idx, col_name]
            if pd.notna(current) and str(current).strip():
                continue  # Уже заполнено

            value_mm = int(DimensionsSynchronizer.cm_to_mm(value_cm))
            df.at[idx, col_name] = value_mm
            written += 1
            self._log_change("ozon", article, col_name, value_mm)
            logger.debug(f"   [XML→Ozon] {article}: {col_name}={value_mm} мм")

        return written

    def _write_yandex_dimensions_from_xml(
        self,
        dfs: Dict[str, pd.DataFrame],
        article: str,
        length_cm: float,
        width_cm: float,
        height_cm: float,
    ) -> int:
        """Записывает габариты в составной столбец Яндекс из XML."""
        if "yandex" not in dfs:
            return 0

        df = dfs["yandex"]
        article_col = self._article_columns["yandex"]

        if (
            article_col not in df.columns
            or _YANDEX_COMPOSITE_COL not in df.columns
        ):
            return 0

        mask = df[article_col].astype(str).str.strip() == article
        if not mask.any():
            return 0

        idx = df[mask].index[0]
        current = df.at[idx, _YANDEX_COMPOSITE_COL]

        if pd.notna(current) and str(current).strip():
            return 0  # Уже заполнено

        composite = DimensionsSynchronizer.format_composite_dimensions(
            length_cm, width_cm, height_cm
        )
        df.at[idx, _YANDEX_COMPOSITE_COL] = composite
        self._log_change("yandex", article, _YANDEX_COMPOSITE_COL, composite)
        logger.debug(
            f"   [XML→Яндекс] {article}: {_YANDEX_COMPOSITE_COL}={composite}"
        )
        return 1

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _log_change(
        self,
        mp: str,
        article: str,
        column: str,
        value: object,
    ) -> None:
        """
        Записывает изменение из XML в общий лог.

        Args:
            mp:      маркетплейс-получатель.
            article: артикул товара.
            column:  название столбца.
            value:   записанное значение.
        """
        self._changes_log[mp].append({
            "article":            article,
            "column":             column,
            "new_value":          str(value),
            "source_marketplace": "xml",
        })