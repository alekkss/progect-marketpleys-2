"""
Модуль выравнивания артикулов между маркетплейсами.

Отвечает за две связанные задачи:
    - выровнять наборы артикулов во всех МП-файлах (добавить недостающие строки);
    - построить словарь {артикул: {value, index}} для быстрого поиска значений.

Паттерн: Repository — предоставляет абстракцию доступа к данным DataFrame,
скрывая детали поиска и вставки строк.
"""

from typing import Dict, List, Optional

import pandas as pd

from utils.logger_config import setup_logger

logger = setup_logger("article_aligner")

# Подстроки, характерные для служебных строк-описаний (не товарных артикулов)
_DESCRIPTION_PATTERNS: str = (
    "идентифицировать|описание|заполнить|пример|название товара|по которому"
)

# Максимальная длина строки артикула — длиннее считается описанием
_MAX_ARTICLE_LENGTH: int = 50


class ArticleAligner:
    """
    Выравнивает артикулы между маркетплейсами и строит article_map.

    Принимает зависимости через конструктор (Dependency Inversion):
        - article_columns: маппинг {маркетплейс: название столбца артикула}.
        - xml_article_map:  индекс XML-офферов по артикулу (может быть пустым).

    Паттерн: Dependency Injection — не обращается к глобальному состоянию,
    все данные передаются явно.
    """

    def __init__(
        self,
        article_columns: Dict[str, str],
        xml_article_map: Optional[Dict[str, Dict]] = None,
    ) -> None:
        """
        Args:
            article_columns: словарь вида
                {'wildberries': 'Артикул продавца', 'ozon': 'Артикул*', ...}.
            xml_article_map: индекс XML-офферов {vendor_code: offer_dict}.
                             Если передан — его артикулы включаются в выравнивание.
        """
        self._article_columns = article_columns
        self._xml_article_map = xml_article_map or {}

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    def align(self, dfs: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """
        Выравнивает артикулы между маркетплейсами.

        Собирает все уникальные артикулы из всех МП (и XML, если задан),
        затем для каждого МП добавляет строки с отсутствующими артикулами
        сразу после последней заполненной строки.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Обновлённый словарь DataFrame с добавленными строками.
        """
        logger.info("\n" + "=" * 60)
        logger.info("ВЫРАВНИВАНИЕ АРТИКУЛОВ МЕЖДУ МАРКЕТПЛЕЙСАМИ")
        logger.info("=" * 60)

        all_articles = self._collect_all_articles(dfs)
        total_added = 0

        for marketplace in ["wildberries", "ozon", "yandex"]:
            added = self._align_marketplace(dfs, marketplace, all_articles)
            total_added += added

        if total_added > 0:
            logger.info(f"\n✅ Итого добавлено {total_added} новых строк во все маркетплейсы")
        else:
            logger.info("\n✅ Выравнивание не требуется — все артикулы присутствуют")

        return dfs

    def create_article_map(
        self,
        df: pd.DataFrame,
        article_col: str,
        value_col: str,
    ) -> Dict[str, Dict]:
        """
        Строит словарь для быстрого поиска значений по артикулу.

        Args:
            df:          DataFrame маркетплейса.
            article_col: название столбца с артикулами.
            value_col:   название столбца со значениями для поиска.

        Returns:
            Словарь {артикул: {'value': значение, 'index': индекс строки}}.
        """
        article_map: Dict[str, Dict] = {}

        if article_col not in df.columns or value_col not in df.columns:
            return article_map

        for idx, row in df.iterrows():
            article = row.get(article_col)
            if pd.notna(article):
                article_str = str(article).strip()
                if article_str:
                    article_map[article_str] = {
                        "value": row.get(value_col),
                        "index": idx,
                    }

        return article_map

    # ------------------------------------------------------------------
    # Приватные методы
    # ------------------------------------------------------------------

    def _collect_all_articles(self, dfs: Dict[str, pd.DataFrame]) -> set:
        """
        Собирает уникальные артикулы из всех МП и XML-индекса.

        Args:
            dfs: словарь {маркетплейс: DataFrame}.

        Returns:
            Множество всех валидных артикулов.
        """
        all_articles: set = set()

        for marketplace in ["wildberries", "ozon", "yandex"]:
            article_col = self._article_columns.get(marketplace)
            if not article_col or article_col not in dfs[marketplace].columns:
                continue

            articles = self._extract_valid_articles(dfs[marketplace], article_col)
            all_articles.update(articles.tolist())
            logger.info(f"📊 {marketplace.upper()}: {len(articles)} артикулов")

        # Добавляем артикулы из XML-индекса
        if self._xml_article_map:
            before_count = len(all_articles)
            all_articles.update(self._xml_article_map.keys())
            xml_new = len(all_articles) - before_count
            logger.info(
                f"📊 XML: {len(self._xml_article_map)} артикулов в индексе, "
                f"{xml_new} новых (отсутствуют в МП)"
            )

        logger.info(f"\n🔍 Всего уникальных артикулов: {len(all_articles)}")
        return all_articles

    def _align_marketplace(
        self,
        dfs: Dict[str, pd.DataFrame],
        marketplace: str,
        all_articles: set,
    ) -> int:
        """
        Добавляет недостающие артикулы в DataFrame одного маркетплейса.

        Args:
            dfs:          словарь DataFrame (изменяется на месте).
            marketplace:  ключ маркетплейса.
            all_articles: полный набор артикулов для выравнивания.

        Returns:
            Количество добавленных строк.
        """
        article_col = self._article_columns.get(marketplace)
        if not article_col:
            return 0

        df = dfs[marketplace]

        if article_col not in df.columns:
            logger.warning(
                f"⚠️ {marketplace.upper()}: столбец '{article_col}' не найден, пропускаю"
            )
            return 0

        # Сбрасываем индексы перед обработкой
        dfs[marketplace] = df.reset_index(drop=True)
        df = dfs[marketplace]

        existing_articles, last_filled_position = self._get_existing_articles(
            df, article_col
        )
        missing_articles = all_articles - existing_articles

        if not missing_articles:
            logger.info(f"✅ {marketplace.upper()}: все артикулы присутствуют")
            return 0

        logger.info(f"\n➕ {marketplace.upper()}: добавляю {len(missing_articles)} артикулов")

        new_rows = [
            {col: (article if col == article_col else None) for col in df.columns}
            for article in sorted(missing_articles)
        ]
        new_df = pd.DataFrame(new_rows)

        if last_filled_position >= 0:
            before = df.iloc[: last_filled_position + 1].copy()
            after = df.iloc[last_filled_position + 1 :].copy()
            dfs[marketplace] = pd.concat([before, new_df, after], ignore_index=True)
            logger.info(
                f" ✓ Добавлено {len(new_rows)} строк после позиции {last_filled_position}"
            )
        else:
            dfs[marketplace] = pd.concat([new_df, df], ignore_index=True)
            logger.info(f" ✓ Добавлено {len(new_rows)} строк в начало")

        logger.info(
            f" 📊 Было: {len(df)}, стало: {len(dfs[marketplace])}"
        )
        return len(new_rows)

    def _get_existing_articles(
        self,
        df: pd.DataFrame,
        article_col: str,
    ) -> tuple[set, int]:
        """
        Возвращает множество существующих валидных артикулов и позицию
        последней заполненной строки.

        Args:
            df:          DataFrame маркетплейса.
            article_col: название столбца с артикулами.

        Returns:
            Кортеж (множество артикулов, позиционный индекс последней строки).
            Индекс равен -1, если заполненных строк нет.
        """
        article_series = df[article_col].dropna().astype(str).str.strip()
        article_series = article_series[article_series != ""]

        valid_mask = ~article_series.str.contains(
            _DESCRIPTION_PATTERNS, case=False, na=False
        ) & (article_series.str.len() < _MAX_ARTICLE_LENGTH)

        article_series = article_series[valid_mask]

        if len(article_series) > 0:
            last_label_idx = article_series.index[-1]
            last_filled_position: int = df.index.get_loc(last_label_idx)
        else:
            last_filled_position = -1

        return set(article_series.tolist()), last_filled_position

    @staticmethod
    def _extract_valid_articles(
        df: pd.DataFrame,
        article_col: str,
    ) -> pd.Series:
        """
        Извлекает валидные артикулы из столбца DataFrame,
        отфильтровывая служебные строки и слишком длинные значения.

        Args:
            df:          DataFrame маркетплейса.
            article_col: название столбца с артикулами.

        Returns:
            Series с валидными артикулами.
        """
        articles = df[article_col].dropna().astype(str).str.strip()
        articles = articles[articles != ""]
        articles = articles[
            ~articles.str.contains(_DESCRIPTION_PATTERNS, case=False, na=False)
        ]
        articles = articles[articles.str.len() < _MAX_ARTICLE_LENGTH]
        return articles