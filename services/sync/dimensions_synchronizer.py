"""
Модуль синхронизации габаритов между маркетплейсами.

Отвечает за одну задачу: синхронизировать значения длины, ширины
и высоты упаковки между Wildberries, Ozon и Яндекс.Маркет
с учётом различных форматов каждого маркетплейса:
    - WB:     раздельные столбцы в сантиметрах (имена определяются динамически).
    - Ozon:   раздельные столбцы в миллиметрах.
    - Яндекс: один композитный столбец «длина/ширина/высота» в сантиметрах.

Паттерн: Strategy — форматы маркетплейсов описаны в DIMENSIONS_MAPPING,
добавление нового МП не требует изменения алгоритма синхронизации.
"""

from typing import Dict, Optional, Tuple

import pandas as pd

from config.config import WB_DIMENSION_PATTERNS
from utils.logger_config import setup_logger

logger = setup_logger("dimensions_synchronizer")


class DimensionsSynchronizer:
    """
    Синхронизирует габариты упаковки между всеми маркетплейсами.

    Маппинг форматов хранится в DIMENSIONS_MAPPING.
    Для WB названия столбцов определяются динамически через паттерны,
    потому что разные категории товаров используют разные имена.

    Паттерн: Strategy — каждый МП описан своей конфигурацией в словаре,
    алгоритм синхронизации единый для всех.
    """

    # Маппинг форматов габаритов для каждого маркетплейса.
    # WB использует паттерны — реальное имя столбца определяется динамически.
    DIMENSIONS_MAPPING: Dict = {
        "wildberries": {
            "length_patterns": WB_DIMENSION_PATTERNS["length"],
            "width_patterns": WB_DIMENSION_PATTERNS["width"],
            "height_patterns": WB_DIMENSION_PATTERNS["height"],
            "unit": "cm",
        },
        "ozon": {
            "length": "Длина упаковки, мм*",
            "width": "Ширина упаковки, мм*",
            "height": "Высота упаковки, мм*",
            "unit": "mm",
        },
        "yandex": {
            "composite": "Габариты с упаковкой, см",
            "unit": "cm",
        },
    }

    # Названия столбцов артикулов для каждого МП
    _ARTICLE_COLUMNS: Dict[str, str] = {
        "wildberries": "Артикул продавца",
        "ozon": "Артикул*",
        "yandex": "Ваш SKU *",
    }

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    @classmethod
    def sync_dimensions(
        cls,
        dfs: Dict[str, pd.DataFrame],
    ) -> Tuple[int, Optional[Dict[str, str]]]:
        """
        Синхронизирует габариты между всеми маркетплейсами.

        Порядок: Яндекс → WB/Ozon, затем WB → Яндекс/Ozon,
        затем Ozon → Яндекс/WB. Заполняются только пустые ячейки.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Кортеж (количество синхронизированных значений, resolved_wb_dims).
            resolved_wb_dims — словарь {'length': '...', 'width': '...', 'height': '...'}
            или None, если столбцы WB не найдены. Передаётся оркестратору
            для использования в последующих этапах.
        """
        logger.info("=" * 80)
        logger.info("🔧 НАЧАЛО СИНХРОНИЗАЦИИ ГАБАРИТОВ (DimensionsSynchronizer)")
        logger.info("=" * 80)

        synced_count = 0

        # Динамически определяем названия столбцов WB
        resolved_wb_dims: Optional[Dict[str, str]] = None
        if "wildberries" in dfs:
            logger.info("\n🔍 Определяю названия столбцов габаритов WB...")
            resolved_wb_dims = cls._resolve_wb_columns(dfs["wildberries"])
            if resolved_wb_dims:
                logger.info(
                    f"   📋 Найдены столбцы WB: "
                    f"length='{resolved_wb_dims['length']}', "
                    f"width='{resolved_wb_dims['width']}', "
                    f"height='{resolved_wb_dims['height']}'"
                )
            else:
                logger.error("   ⛔ Не удалось определить столбцы габаритов WB!")

        # Читаем габариты из каждого МП
        yandex_dimensions = cls._read_yandex_dimensions(dfs)
        wb_dimensions = cls._read_wb_dimensions(dfs, resolved_wb_dims)
        ozon_dimensions = cls._read_ozon_dimensions(dfs)

        logger.info("\n📊 СВОДКА СОБРАННЫХ ДАННЫХ:")
        logger.info(f"   • Яндекс: {len(yandex_dimensions)} артикулов с габаритами")
        logger.info(f"   • WB:     {len(wb_dimensions)} артикулов с габаритами")
        logger.info(f"   • Ozon:   {len(ozon_dimensions)} артикулов с габаритами")

        # Синхронизация в трёх направлениях
        synced_count += cls._sync_yandex_to_others(
            dfs, yandex_dimensions, resolved_wb_dims
        )
        synced_count += cls._sync_wb_to_others(
            dfs, wb_dimensions, yandex_dimensions, resolved_wb_dims
        )
        synced_count += cls._sync_ozon_to_others(
            dfs, ozon_dimensions, yandex_dimensions, resolved_wb_dims
        )

        logger.info("\n" + "=" * 80)
        logger.info(f"✅ ГАБАРИТЫ: синхронизировано {synced_count} значений")
        logger.info("=" * 80)

        return synced_count, resolved_wb_dims

    @staticmethod
    def parse_composite_dimensions(value: str) -> Optional[Dict[str, float]]:
        """
        Парсит строку «71/68/197» в словарь {length, width, height}.

        Args:
            value: строка в формате «длина/ширина/высота».

        Returns:
            Словарь с числовыми значениями или None, если парсинг не удался.
        """
        if pd.isna(value) or not str(value).strip():
            return None

        try:
            parts = str(value).strip().split("/")
            if len(parts) != 3:
                return None

            dimensions = {
                "length": float(parts[0].strip()),
                "width": float(parts[1].strip()),
                "height": float(parts[2].strip()),
            }

            if all(v > 0 for v in dimensions.values()):
                return dimensions

        except (ValueError, AttributeError):
            pass

        return None

    @staticmethod
    def format_composite_dimensions(
        length: float, width: float, height: float
    ) -> str:
        """
        Форматирует габариты в строку «Длина/Ширина/Высота».

        Целые числа выводятся без дробной части, дробные — с одним знаком.

        Args:
            length: длина в сантиметрах.
            width:  ширина в сантиметрах.
            height: высота в сантиметрах.

        Returns:
            Строка вида «71/68/197» или «71.5/68.0/197.0».
        """
        def smart_format(val: float) -> str:
            if abs(val - round(val)) < 0.01:
                return str(int(round(val)))
            return f"{val:.1f}"

        return f"{smart_format(length)}/{smart_format(width)}/{smart_format(height)}"

    @staticmethod
    def mm_to_cm(value: float) -> float:
        """Конвертирует миллиметры в сантиметры."""
        return value / 10

    @staticmethod
    def cm_to_mm(value: float) -> float:
        """Конвертирует сантиметры в миллиметры."""
        return value * 10

    # ------------------------------------------------------------------
    # Чтение габаритов из каждого МП
    # ------------------------------------------------------------------

    @classmethod
    def _read_yandex_dimensions(
        cls, dfs: Dict[str, pd.DataFrame]
    ) -> Dict[str, Dict[str, float]]:
        """
        Читает габариты из Яндекс (композитный формат «д/ш/в» в см).

        Args:
            dfs: словарь DataFrame маркетплейсов.

        Returns:
            Словарь {артикул: {length, width, height}} в сантиметрах.
        """
        logger.info("\n📖 ЭТАП 1: Чтение габаритов из Яндекс (композитный формат)")

        result: Dict[str, Dict[str, float]] = {}
        yandex_col = cls.DIMENSIONS_MAPPING["yandex"]["composite"]
        article_col = cls._ARTICLE_COLUMNS["yandex"]

        if "yandex" not in dfs:
            logger.warning("   ❌ DataFrame 'yandex' отсутствует!")
            return result

        if yandex_col not in dfs["yandex"].columns:
            logger.warning(f"   ❌ Столбец '{yandex_col}' не найден в Яндекс!")
            return result

        rows_with_dimensions = 0
        for _, row in dfs["yandex"].iterrows():
            article = row.get(article_col)
            if not (pd.notna(article) and str(article).strip()):
                continue

            dimensions = cls.parse_composite_dimensions(row.get(yandex_col))
            if dimensions:
                result[str(article).strip()] = dimensions
                rows_with_dimensions += 1
                if rows_with_dimensions <= 3:
                    logger.info(
                        f"   ✓ Яндекс [{article}]: "
                        f"{dimensions['length']}/{dimensions['width']}/{dimensions['height']} см"
                    )

        logger.info(f"   📊 Итого Яндекс: {rows_with_dimensions} артикулов с габаритами")
        return result

    @classmethod
    def _read_wb_dimensions(
        cls,
        dfs: Dict[str, pd.DataFrame],
        resolved_wb_dims: Optional[Dict[str, str]],
    ) -> Dict[str, Dict[str, float]]:
        """
        Читает габариты из WB (раздельные столбцы в сантиметрах).

        Args:
            dfs:              словарь DataFrame маркетплейсов.
            resolved_wb_dims: словарь с реальными именами столбцов WB или None.

        Returns:
            Словарь {артикул: {length, width, height}} в сантиметрах.
        """
        logger.info("\n📖 ЭТАП 2: Чтение габаритов из WB (раздельные столбцы, см)")

        result: Dict[str, Dict[str, float]] = {}

        if "wildberries" not in dfs:
            logger.warning("   ❌ DataFrame 'wildberries' отсутствует!")
            return result

        if not resolved_wb_dims:
            logger.error("   ⛔ Пропускаю WB — столбцы габаритов не определены")
            return result

        article_col = cls._ARTICLE_COLUMNS["wildberries"]
        rows_with_dimensions = 0

        for _, row in dfs["wildberries"].iterrows():
            article = row.get(article_col)
            if not (pd.notna(article) and str(article).strip()):
                continue

            length = row.get(resolved_wb_dims["length"])
            width = row.get(resolved_wb_dims["width"])
            height = row.get(resolved_wb_dims["height"])

            if all(pd.notna(v) and str(v).strip() for v in [length, width, height]):
                try:
                    result[str(article).strip()] = {
                        "length": float(length),
                        "width": float(width),
                        "height": float(height),
                    }
                    rows_with_dimensions += 1
                    if rows_with_dimensions <= 3:
                        logger.info(
                            f"   ✓ WB [{article}]: {length}/{width}/{height} см"
                        )
                except ValueError:
                    pass

        logger.info(f"   📊 Итого WB: {rows_with_dimensions} артикулов с полными габаритами")
        return result

    @classmethod
    def _read_ozon_dimensions(
        cls, dfs: Dict[str, pd.DataFrame]
    ) -> Dict[str, Dict[str, float]]:
        """
        Читает габариты из Ozon (раздельные столбцы в мм, конвертирует в см).

        Args:
            dfs: словарь DataFrame маркетплейсов.

        Returns:
            Словарь {артикул: {length, width, height}} в сантиметрах.
        """
        logger.info("\n📖 ЭТАП 3: Чтение габаритов из Ozon (раздельные столбцы, мм)")

        result: Dict[str, Dict[str, float]] = {}

        if "ozon" not in dfs:
            logger.warning("   ❌ DataFrame 'ozon' отсутствует!")
            return result

        ozon_map = cls.DIMENSIONS_MAPPING["ozon"]
        article_col = cls._ARTICLE_COLUMNS["ozon"]

        missing_cols = [
            col
            for col in [ozon_map["length"], ozon_map["width"], ozon_map["height"]]
            if col not in dfs["ozon"].columns
        ]
        if missing_cols:
            logger.error(f"   ⛔ Пропускаю Ozon — отсутствуют столбцы: {missing_cols}")
            return result

        rows_with_dimensions = 0
        for _, row in dfs["ozon"].iterrows():
            article = row.get(article_col)
            if not (pd.notna(article) and str(article).strip()):
                continue

            length_mm = row.get(ozon_map["length"])
            width_mm = row.get(ozon_map["width"])
            height_mm = row.get(ozon_map["height"])

            if all(pd.notna(v) and str(v).strip() for v in [length_mm, width_mm, height_mm]):
                try:
                    result[str(article).strip()] = {
                        "length": cls.mm_to_cm(float(length_mm)),
                        "width": cls.mm_to_cm(float(width_mm)),
                        "height": cls.mm_to_cm(float(height_mm)),
                    }
                    rows_with_dimensions += 1
                    if rows_with_dimensions <= 3:
                        logger.info(
                            f"   ✓ Ozon [{article}]: "
                            f"{length_mm}/{width_mm}/{height_mm} мм"
                        )
                except ValueError:
                    pass

        logger.info(f"   📊 Итого Ozon: {rows_with_dimensions} артикулов с полными габаритами")
        return result

    # ------------------------------------------------------------------
    # Синхронизация в трёх направлениях
    # ------------------------------------------------------------------

    @classmethod
    def _sync_yandex_to_others(
        cls,
        dfs: Dict[str, pd.DataFrame],
        yandex_dimensions: Dict[str, Dict[str, float]],
        resolved_wb_dims: Optional[Dict[str, str]],
    ) -> int:
        """Синхронизирует габариты из Яндекс → WB и Ozon."""
        logger.info("\n🔄 ЭТАП 4: Синхронизация Яндекс → WB/Ozon")

        to_wb = 0
        to_ozon = 0

        for article, dimensions in yandex_dimensions.items():
            to_wb += cls._write_wb_dimensions(
                dfs, article, dimensions, resolved_wb_dims
            )
            to_ozon += cls._write_ozon_dimensions(dfs, article, dimensions)

        logger.info(f"   📊 Яндекс → WB:   {to_wb} значений")
        logger.info(f"   📊 Яндекс → Ozon: {to_ozon} значений")
        return to_wb + to_ozon

    @classmethod
    def _sync_wb_to_others(
        cls,
        dfs: Dict[str, pd.DataFrame],
        wb_dimensions: Dict[str, Dict[str, float]],
        yandex_dimensions: Dict[str, Dict[str, float]],
        resolved_wb_dims: Optional[Dict[str, str]],
    ) -> int:
        """Синхронизирует габариты из WB → Яндекс и Ozon."""
        logger.info("\n🔄 ЭТАП 5: Синхронизация WB → Яндекс и Ozon")

        to_yandex = 0
        to_ozon = 0
        skipped = 0

        for article, dimensions in wb_dimensions.items():
            if article in yandex_dimensions:
                skipped += 1
            else:
                to_yandex += cls._write_yandex_dimensions(dfs, article, dimensions)

            to_ozon += cls._write_ozon_dimensions(dfs, article, dimensions)

        logger.info(f"   📊 WB → Яндекс:  {to_yandex} значений")
        logger.info(f"   📊 WB → Яндекс:  {skipped} пропущено (уже есть)")
        logger.info(f"   📊 WB → Ozon:    {to_ozon} значений")
        return to_yandex + to_ozon

    @classmethod
    def _sync_ozon_to_others(
        cls,
        dfs: Dict[str, pd.DataFrame],
        ozon_dimensions: Dict[str, Dict[str, float]],
        yandex_dimensions: Dict[str, Dict[str, float]],
        resolved_wb_dims: Optional[Dict[str, str]],
    ) -> int:
        """Синхронизирует габариты из Ozon → Яндекс и WB."""
        logger.info("\n🔄 ЭТАП 6: Синхронизация Ozon → Яндекс и WB")

        to_yandex = 0
        to_wb = 0
        skipped = 0

        for article, dimensions in ozon_dimensions.items():
            if article in yandex_dimensions:
                skipped += 1
            else:
                to_yandex += cls._write_yandex_dimensions(dfs, article, dimensions)

            to_wb += cls._write_wb_dimensions(dfs, article, dimensions, resolved_wb_dims)

        logger.info(f"   📊 Ozon → Яндекс: {to_yandex} значений")
        logger.info(f"   📊 Ozon → Яндекс: {skipped} пропущено (ячейка заполнена)")
        logger.info(f"   📊 Ozon → WB:     {to_wb} значений")
        return to_yandex + to_wb

    # ------------------------------------------------------------------
    # Запись значений в каждый МП
    # ------------------------------------------------------------------

    @classmethod
    def _write_wb_dimensions(
        cls,
        dfs: Dict[str, pd.DataFrame],
        article: str,
        dimensions: Dict[str, float],
        resolved_wb_dims: Optional[Dict[str, str]],
    ) -> int:
        """Записывает габариты (см) в раздельные столбцы WB."""
        if "wildberries" not in dfs or not resolved_wb_dims:
            return 0

        df = dfs["wildberries"]
        article_col = cls._ARTICLE_COLUMNS["wildberries"]
        mask = df[article_col].astype(str).str.strip() == article
        if not mask.any():
            return 0

        idx = df[mask].index[0]
        written = 0

        for dim_key in ("length", "width", "height"):
            col_name = resolved_wb_dims[dim_key]
            current = df.at[idx, col_name]
            if pd.isna(current) or not str(current).strip():
                df.at[idx, col_name] = dimensions[dim_key]
                written += 1
                logger.debug(f"   [→WB] {article}: {dim_key}={dimensions[dim_key]}")

        return written

    @classmethod
    def _write_ozon_dimensions(
        cls,
        dfs: Dict[str, pd.DataFrame],
        article: str,
        dimensions: Dict[str, float],
    ) -> int:
        """Записывает габариты (мм) в раздельные столбцы Ozon."""
        if "ozon" not in dfs:
            return 0

        ozon_map = cls.DIMENSIONS_MAPPING["ozon"]
        df = dfs["ozon"]
        article_col = cls._ARTICLE_COLUMNS["ozon"]
        mask = df[article_col].astype(str).str.strip() == article
        if not mask.any():
            return 0

        idx = df[mask].index[0]
        written = 0

        for dim_key, col_name in [
            ("length", ozon_map["length"]),
            ("width", ozon_map["width"]),
            ("height", ozon_map["height"]),
        ]:
            current = df.at[idx, col_name]
            if pd.isna(current) or not str(current).strip():
                value_mm = int(cls.cm_to_mm(dimensions[dim_key]))
                df.at[idx, col_name] = value_mm
                written += 1
                logger.debug(f"   [→Ozon] {article}: {dim_key}={value_mm} мм")

        return written

    @classmethod
    def _write_yandex_dimensions(
        cls,
        dfs: Dict[str, pd.DataFrame],
        article: str,
        dimensions: Dict[str, float],
    ) -> int:
        """Записывает габариты в композитный столбец Яндекс."""
        if "yandex" not in dfs:
            return 0

        yandex_col = cls.DIMENSIONS_MAPPING["yandex"]["composite"]
        df = dfs["yandex"]
        article_col = cls._ARTICLE_COLUMNS["yandex"]
        mask = df[article_col].astype(str).str.strip() == article
        if not mask.any():
            return 0

        idx = df[mask].index[0]
        current = df.at[idx, yandex_col]

        if pd.isna(current) or not str(current).strip():
            composite = cls.format_composite_dimensions(
                dimensions["length"], dimensions["width"], dimensions["height"]
            )
            df.at[idx, yandex_col] = composite
            logger.info(f"   ✓ [→Яндекс] {article}: {composite}")
            return 1

        return 0

    # ------------------------------------------------------------------
    # Определение столбцов WB
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_wb_columns(
        cls, df_wb: pd.DataFrame
    ) -> Optional[Dict[str, str]]:
        """
        Динамически определяет реальные названия столбцов габаритов WB.

        Разные категории товаров используют разные имена:
            - «Длина упаковки (целое число)» (Кроссовки)
            - «Длина упаковки» (Куртки)

        Args:
            df_wb: DataFrame Wildberries.

        Returns:
            Словарь {'length': '...', 'width': '...', 'height': '...'} или None.
        """
        wb_map = cls.DIMENSIONS_MAPPING["wildberries"]
        resolved: Dict[str, str] = {}

        for dim_key in ("length", "width", "height"):
            patterns = wb_map[f"{dim_key}_patterns"]
            found = False

            for pattern in patterns:
                if pattern in df_wb.columns:
                    resolved[dim_key] = pattern
                    found = True
                    logger.info(f"   ✅ WB столбец '{dim_key}' найден как '{pattern}'")
                    break

            if not found:
                logger.warning(
                    f"   ❌ WB столбец '{dim_key}' не найден! "
                    f"Проверенные варианты: {patterns}"
                )
                return None

        return resolved