"""
Модуль синхронизации фото-ссылок между маркетплейсами.

Отвечает за одну задачу: перенос фото-ссылок между МП с учётом
того, что у каждого маркетплейса разная структура фото-столбцов:
    - WB:     один столбец «Фото» — все ссылки через «;»
    - Ozon:   два столбца — «Ссылка на главное фото*» и
              «Ссылки на дополнительные фото» (через пробел)
    - Яндекс: один столбец «Ссылка на изображение *» — все ссылки через «,»

Логика переноса:
    Ozon → WB:      главное + дополнительные → один столбец через «;»
    Ozon → Яндекс:  главное + дополнительные → один столбец через «,»
    WB → Ozon:      первая ссылка → главное фото,
                    остальные → дополнительные через пробел
    Яндекс → Ozon:  первая ссылка → главное фото,
                    остальные → дополнительные через пробел

Принцип Single Responsibility: этот класс знает только о фото.
Принцип Dependency Inversion: все зависимости передаются через конструктор.
Принцип Open/Closed: добавление нового МП — только новый метод +
    запись в PHOTO_COLUMNS конфига, без изменения существующей логики.
"""

from typing import Dict, List, Optional, Tuple

import pandas as pd

from config.config import (
    PHOTO_COLUMNS,
    PHOTO_READ_SEPARATORS,
    PHOTO_WRITE_SEPARATORS,
)
from utils.logger_config import setup_logger

logger = setup_logger("photo_syncer")


class PhotoSyncer:
    """
    Синхронизирует фото-ссылки между маркетплейсами.

    Обрабатывает только те артикулы, у которых целевые фото-столбцы
    полностью пусты. Если хотя бы один фото-столбец МП уже заполнен —
    этот артикул пропускается (не перезаписываем данные пользователя).

    Принимает зависимости через конструктор (Dependency Inversion):
        - article_columns: маппинг {маркетплейс: столбец артикула}.
        - changes_log:     общий лог изменений (передаётся по ссылке).
    """

    def __init__(
        self,
        article_columns: Dict[str, str],
        changes_log: Dict[str, List],
    ) -> None:
        """
        Args:
            article_columns: маппинг {маркетплейс: название столбца артикула}.
                             Пример: {'wildberries': 'Артикул продавца', ...}
            changes_log:     общий лог изменений — изменяется на месте.
                             Ключи: 'wildberries', 'ozon', 'yandex'.
        """
        self._article_columns = article_columns
        self._changes_log = changes_log

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    def sync_photos(
        self, dfs: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """
        Запускает полную синхронизацию фото между всеми МП.

        Порядок обработки:
            1. Ozon → WB
            2. Ozon → Яндекс
            3. WB → Ozon
            4. Яндекс → Ozon

        Шаги 3 и 4 выполняются только если после шага 1/2
        в Ozon остались пустые фото-столбцы.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Обновлённый словарь DataFrame.
        """
        logger.info("\n" + "=" * 60)
        logger.info("СИНХРОНИЗАЦИЯ ФОТО МЕЖДУ МАРКЕТПЛЕЙСАМИ")
        logger.info("=" * 60)

        total_filled = 0

        # Ozon → WB
        filled = self._sync_ozon_to_wb(dfs)
        total_filled += filled
        if filled > 0:
            logger.info(f"  📷 Ozon → WB: заполнено {filled} артикулов")

        # Ozon → Яндекс
        filled = self._sync_ozon_to_yandex(dfs)
        total_filled += filled
        if filled > 0:
            logger.info(f"  📷 Ozon → Яндекс: заполнено {filled} артикулов")

        # WB → Ozon (только пустые после шага выше)
        filled = self._sync_wb_to_ozon(dfs)
        total_filled += filled
        if filled > 0:
            logger.info(f"  📷 WB → Ozon: заполнено {filled} артикулов")

        # Яндекс → Ozon (только пустые после шагов выше)
        filled = self._sync_yandex_to_ozon(dfs)
        total_filled += filled
        if filled > 0:
            logger.info(f"  📷 Яндекс → Ozon: заполнено {filled} артикулов")

        logger.info(f"\n[+] Фото: итого заполнено {total_filled} артикулов")

        return dfs

    # ------------------------------------------------------------------
    # Ozon → WB
    # ------------------------------------------------------------------

    def _sync_ozon_to_wb(self, dfs: Dict[str, pd.DataFrame]) -> int:
        """
        Переносит фото из Ozon в WB.

        Собирает ссылки из двух столбцов Ozon:
            1. «Ссылка на главное фото*» — первой
            2. «Ссылки на дополнительные фото» — следом (если есть)
        Объединяет через «;» и записывает в столбец WB «Фото».

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Количество заполненных артикулов.
        """
        if not self._has_required_columns(dfs, "ozon", ["main", "extra"]):
            return 0
        if not self._has_required_columns(dfs, "wildberries", ["all"]):
            return 0

        df_ozon = dfs["ozon"]
        df_wb   = dfs["wildberries"]

        ozon_main_col  = PHOTO_COLUMNS["ozon"]["main"]
        ozon_extra_col = PHOTO_COLUMNS["ozon"]["extra"]
        wb_all_col     = PHOTO_COLUMNS["wildberries"]["all"]

        article_col_ozon = self._article_columns["ozon"]
        article_col_wb   = self._article_columns["wildberries"]

        filled_count = 0

        for _, ozon_row in df_ozon.iterrows():
            article = self._get_article(ozon_row, article_col_ozon)
            if not article:
                continue

            # Ищем строку в WB по артикулу
            wb_idx = self._find_row_index(df_wb, article_col_wb, article)
            if wb_idx is None:
                continue

            # Проверяем — WB уже заполнен?
            if self._is_filled(df_wb, wb_idx, wb_all_col):
                continue

            # Собираем ссылки из Ozon
            links = self._collect_ozon_links(ozon_row, ozon_main_col, ozon_extra_col)
            if not links:
                continue

            # Записываем в WB через ";"
            value = PHOTO_WRITE_SEPARATORS["wildberries"].join(links)
            df_wb.at[wb_idx, wb_all_col] = value
            self._log_change("wildberries", article, wb_all_col, value, "ozon")
            filled_count += 1

            logger.debug(
                f"  [Ozon→WB] {article}: {len(links)} фото → «{wb_all_col}»"
            )

        return filled_count

    # ------------------------------------------------------------------
    # Ozon → Яндекс
    # ------------------------------------------------------------------

    def _sync_ozon_to_yandex(self, dfs: Dict[str, pd.DataFrame]) -> int:
        """
        Переносит фото из Ozon в Яндекс.

        Собирает ссылки из двух столбцов Ozon:
            1. «Ссылка на главное фото*» — первой
            2. «Ссылки на дополнительные фото» — следом (если есть)
        Объединяет через «,» и записывает в «Ссылка на изображение *».

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Количество заполненных артикулов.
        """
        if not self._has_required_columns(dfs, "ozon", ["main", "extra"]):
            return 0
        if not self._has_required_columns(dfs, "yandex", ["all"]):
            return 0

        df_ozon   = dfs["ozon"]
        df_yandex = dfs["yandex"]

        ozon_main_col   = PHOTO_COLUMNS["ozon"]["main"]
        ozon_extra_col  = PHOTO_COLUMNS["ozon"]["extra"]
        yandex_all_col  = PHOTO_COLUMNS["yandex"]["all"]

        article_col_ozon   = self._article_columns["ozon"]
        article_col_yandex = self._article_columns["yandex"]

        filled_count = 0

        for _, ozon_row in df_ozon.iterrows():
            article = self._get_article(ozon_row, article_col_ozon)
            if not article:
                continue

            # Ищем строку в Яндекс по артикулу
            yandex_idx = self._find_row_index(df_yandex, article_col_yandex, article)
            if yandex_idx is None:
                continue

            # Проверяем — Яндекс уже заполнен?
            if self._is_filled(df_yandex, yandex_idx, yandex_all_col):
                continue

            # Собираем ссылки из Ozon
            links = self._collect_ozon_links(ozon_row, ozon_main_col, ozon_extra_col)
            if not links:
                continue

            # Записываем в Яндекс через ","
            value = PHOTO_WRITE_SEPARATORS["yandex"].join(links)
            df_yandex.at[yandex_idx, yandex_all_col] = value
            self._log_change("yandex", article, yandex_all_col, value, "ozon")
            filled_count += 1

            logger.debug(
                f"  [Ozon→Яндекс] {article}: {len(links)} фото → «{yandex_all_col}»"
            )

        return filled_count

    # ------------------------------------------------------------------
    # WB → Ozon
    # ------------------------------------------------------------------

    def _sync_wb_to_ozon(self, dfs: Dict[str, pd.DataFrame]) -> int:
        """
        Переносит фото из WB в Ozon.

        Разбивает столбец WB «Фото» по «;»:
            - Первая ссылка → «Ссылка на главное фото*»
            - Остальные (если есть) → «Ссылки на дополнительные фото» через пробел

        Пропускает артикул если оба столбца Ozon уже заполнены,
        или если заполнен хотя бы «Ссылка на главное фото*».

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Количество заполненных артикулов.
        """
        if not self._has_required_columns(dfs, "wildberries", ["all"]):
            return 0
        if not self._has_required_columns(dfs, "ozon", ["main", "extra"]):
            return 0

        df_wb   = dfs["wildberries"]
        df_ozon = dfs["ozon"]

        wb_all_col     = PHOTO_COLUMNS["wildberries"]["all"]
        ozon_main_col  = PHOTO_COLUMNS["ozon"]["main"]
        ozon_extra_col = PHOTO_COLUMNS["ozon"]["extra"]

        article_col_wb   = self._article_columns["wildberries"]
        article_col_ozon = self._article_columns["ozon"]

        filled_count = 0

        for _, wb_row in df_wb.iterrows():
            article = self._get_article(wb_row, article_col_wb)
            if not article:
                continue

            # Ищем строку в Ozon по артикулу
            ozon_idx = self._find_row_index(df_ozon, article_col_ozon, article)
            if ozon_idx is None:
                continue

            # Пропускаем если главное фото Ozon уже заполнено
            if self._is_filled(df_ozon, ozon_idx, ozon_main_col):
                continue

            # Читаем ссылки из WB
            wb_value = wb_row.get(wb_all_col)
            links = self._split_links(wb_value, PHOTO_READ_SEPARATORS["wildberries"])
            if not links:
                continue

            # Первая ссылка → главное фото Ozon
            df_ozon.at[ozon_idx, ozon_main_col] = links[0]
            self._log_change("ozon", article, ozon_main_col, links[0], "wildberries")

            # Остальные ссылки → дополнительные фото Ozon
            if len(links) > 1:
                extra_value = PHOTO_WRITE_SEPARATORS["ozon"].join(links[1:])
                df_ozon.at[ozon_idx, ozon_extra_col] = extra_value
                self._log_change(
                    "ozon", article, ozon_extra_col, extra_value, "wildberries"
                )

            filled_count += 1

            logger.debug(
                f"  [WB→Ozon] {article}: {len(links)} фото "
                f"(главное + {len(links) - 1} доп.)"
            )

        return filled_count

    # ------------------------------------------------------------------
    # Яндекс → Ozon
    # ------------------------------------------------------------------

    def _sync_yandex_to_ozon(self, dfs: Dict[str, pd.DataFrame]) -> int:
        """
        Переносит фото из Яндекс в Ozon.

        Разбивает столбец Яндекс «Ссылка на изображение *» по «,»:
            - Первая ссылка → «Ссылка на главное фото*»
            - Остальные (если есть) → «Ссылки на дополнительные фото» через пробел

        Пропускает артикул если «Ссылка на главное фото*» в Ozon уже заполнена.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Количество заполненных артикулов.
        """
        if not self._has_required_columns(dfs, "yandex", ["all"]):
            return 0
        if not self._has_required_columns(dfs, "ozon", ["main", "extra"]):
            return 0

        df_yandex = dfs["yandex"]
        df_ozon   = dfs["ozon"]

        yandex_all_col = PHOTO_COLUMNS["yandex"]["all"]
        ozon_main_col  = PHOTO_COLUMNS["ozon"]["main"]
        ozon_extra_col = PHOTO_COLUMNS["ozon"]["extra"]

        article_col_yandex = self._article_columns["yandex"]
        article_col_ozon   = self._article_columns["ozon"]

        filled_count = 0

        for _, yandex_row in df_yandex.iterrows():
            article = self._get_article(yandex_row, article_col_yandex)
            if not article:
                continue

            # Ищем строку в Ozon по артикулу
            ozon_idx = self._find_row_index(df_ozon, article_col_ozon, article)
            if ozon_idx is None:
                continue

            # Пропускаем если главное фото Ozon уже заполнено
            if self._is_filled(df_ozon, ozon_idx, ozon_main_col):
                continue

            # Читаем ссылки из Яндекс
            yandex_value = yandex_row.get(yandex_all_col)
            links = self._split_links(
                yandex_value, PHOTO_READ_SEPARATORS["yandex"]
            )
            if not links:
                continue

            # Первая ссылка → главное фото Ozon
            df_ozon.at[ozon_idx, ozon_main_col] = links[0]
            self._log_change("ozon", article, ozon_main_col, links[0], "yandex")

            # Остальные ссылки → дополнительные фото Ozon
            if len(links) > 1:
                extra_value = PHOTO_WRITE_SEPARATORS["ozon"].join(links[1:])
                df_ozon.at[ozon_idx, ozon_extra_col] = extra_value
                self._log_change(
                    "ozon", article, ozon_extra_col, extra_value, "yandex"
                )

            filled_count += 1

            logger.debug(
                f"  [Яндекс→Ozon] {article}: {len(links)} фото "
                f"(главное + {len(links) - 1} доп.)"
            )

        return filled_count

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _collect_ozon_links(
        self,
        ozon_row: pd.Series,
        main_col: str,
        extra_col: str,
    ) -> List[str]:
        """
        Собирает все фото-ссылки из строки Ozon в упорядоченный список.

        Порядок: сначала главное фото, затем дополнительные.
        Пустые строки и пробелы отфильтровываются.

        Args:
            ozon_row: строка DataFrame Ozon.
            main_col:  название столбца главного фото.
            extra_col: название столбца дополнительных фото.

        Returns:
            Список ссылок (может быть пустым если оба столбца пусты).
        """
        links: List[str] = []

        # Главное фото — одна ссылка
        main_value = ozon_row.get(main_col)
        if pd.notna(main_value) and str(main_value).strip():
            links.append(str(main_value).strip())

        # Дополнительные фото — несколько через пробел
        extra_value = ozon_row.get(extra_col)
        extra_links = self._split_links(
            extra_value, PHOTO_READ_SEPARATORS["ozon"]
        )
        links.extend(extra_links)

        return links

    @staticmethod
    def _split_links(value: object, separator: str) -> List[str]:
        """
        Разбивает строку ссылок на список по разделителю.

        Отфильтровывает пустые элементы и лишние пробелы.

        Args:
            value:     значение ячейки (строка или NaN).
            separator: символ-разделитель.

        Returns:
            Список непустых ссылок.
        """
        if pd.isna(value) or not str(value).strip():
            return []

        return [
            link.strip()
            for link in str(value).split(separator)
            if link.strip()
        ]

    @staticmethod
    def _get_article(row: pd.Series, article_col: str) -> Optional[str]:
        """
        Извлекает артикул из строки DataFrame.

        Args:
            row:         строка DataFrame.
            article_col: название столбца с артикулом.

        Returns:
            Строка артикула или None если пусто.
        """
        article = row.get(article_col)
        if pd.isna(article) or not str(article).strip():
            return None
        return str(article).strip()

    @staticmethod
    def _find_row_index(
        df: pd.DataFrame,
        article_col: str,
        article: str,
    ) -> Optional[int]:
        """
        Находит индекс строки с нужным артикулом в DataFrame.

        Args:
            df:          DataFrame для поиска.
            article_col: название столбца с артикулами.
            article:     искомый артикул.

        Returns:
            Индекс строки или None если не найден.
        """
        if article_col not in df.columns:
            return None

        mask = df[article_col].astype(str).str.strip() == article
        matches = df[mask]

        if matches.empty:
            return None

        return matches.index[0]

    @staticmethod
    def _is_filled(df: pd.DataFrame, idx: int, col: str) -> bool:
        """
        Проверяет, заполнена ли ячейка в DataFrame.

        Args:
            df:  DataFrame.
            idx: индекс строки.
            col: название столбца.

        Returns:
            True если ячейка содержит непустое значение.
        """
        if col not in df.columns:
            return False

        value = df.at[idx, col]
        return pd.notna(value) and bool(str(value).strip())

    def _has_required_columns(
        self,
        dfs: Dict[str, pd.DataFrame],
        marketplace: str,
        roles: List[str],
    ) -> bool:
        """
        Проверяет наличие всех необходимых фото-столбцов в DataFrame МП.

        Args:
            dfs:         словарь DataFrame.
            marketplace: ключ маркетплейса.
            roles:       список ролей столбцов для проверки
                         (например, ['main', 'extra'] или ['all']).

        Returns:
            True если все столбцы присутствуют в DataFrame.
        """
        if marketplace not in dfs:
            logger.warning(
                f"⚠️ Маркетплейс «{marketplace}» отсутствует в dfs"
            )
            return False

        df = dfs[marketplace]
        mp_photo_cols = PHOTO_COLUMNS.get(marketplace, {})

        for role in roles:
            col_name = mp_photo_cols.get(role)
            if not col_name:
                logger.warning(
                    f"⚠️ [{marketplace}] Роль фото «{role}» "
                    f"не найдена в PHOTO_COLUMNS"
                )
                return False

            if col_name not in df.columns:
                logger.warning(
                    f"⚠️ [{marketplace}] Столбец «{col_name}» "
                    f"не найден в DataFrame"
                )
                return False

        return True

    def _log_change(
        self,
        mp: str,
        article: str,
        column: str,
        value: object,
        source_mp: str,
    ) -> None:
        """
        Записывает изменение фото в общий лог.

        Args:
            mp:        маркетплейс-получатель.
            article:   артикул товара.
            column:    название столбца.
            value:     записанное значение.
            source_mp: маркетплейс-источник.
        """
        self._changes_log[mp].append({
            "article":            article,
            "column":             column,
            "new_value":          str(value),
            "source_marketplace": source_mp,
        })
