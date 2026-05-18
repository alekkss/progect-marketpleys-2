"""
Оркестратор синхронизации данных между маркетплейсами.

Публичный интерфейс намеренно не изменился: все остальные модули
проекта (handlers, upload.py и т.д.) импортируют DataSynchronizer
из этого файла и продолжают работать без изменений.

Порядок синхронизации:
    1. Загрузка DataFrame и validation-списков из xlsx-файлов.
    2. Построение XML-индекса (если переданы XML-данные).
    3. Выравнивание артикулов между МП (включая артикулы из XML).
    4. Синхронизация габаритов (DimensionsSynchronizer).
    5. Синхронизация остальных столбцов по схеме сопоставлений.
    6. [МВМ] Заполнение МП из XML-каталога.
    7. Сохранение результатов.

Тяжёлые CPU-операции (pandas, openpyxl) выносятся в поток через
asyncio.to_thread(), чтобы не блокировать event loop бота во время
обработки. Async-методы с внутренними await (sync_all_matches,
sync_from_xml) остаются в event loop — их нельзя перенести в поток.

AI-лог в файл отчёта записывается отдельно через create_ai_log_in_report()
после того как ExcelWriter создаст отчёт.

Паттерн: Facade — скрывает сложность подсистемы за единым простым
интерфейсом, который использует остальной код проекта.
Паттерн: Dependency Injection — компоненты получают общее состояние
(changes_log, ai_validation_log, column_validations) через конструктор,
не создавая его сами.
"""

import asyncio
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.sync import (
    AiValidator,
    ArticleAligner,
    ColumnSyncer,
    DimensionsSynchronizer,
    ExcelFileManager,
    ValueConverter,
    XmlSyncer,
)
from utils.logger_config import setup_logger

logger = setup_logger("data_sync")


class DataSynchronizer:
    """
    Оркестратор синхронизации данных между тремя маркетплейсами.

    Создаёт все компоненты подсистемы sync/, передаёт им разделяемое
    состояние (changes_log, ai_validation_log) по ссылке и координирует
    вызовы в строго определённом порядке.

    CPU-тяжёлые синхронные операции (pandas, openpyxl) выносятся в поток
    через asyncio.to_thread() — event loop остаётся свободным для других
    пользователей бота во время обработки.

    Async-методы с внутренними await (sync_all_matches, sync_from_xml,
    save_results) остаются в event loop — перенос в поток невозможен,
    так как они содержат вызовы coroutine.

    После synchronize_data вызывающий код должен:
        1. Создать отчёт через ExcelWriter.create_report_with_changes()
        2. Добавить AI-лог через self.create_ai_log_in_report()

    Args:
        comparison_result:     словарь сопоставлений из схемы (из БД).
        ai_comparator:         экземпляр AIComparator или None.
        xml_offer_data:        список офферов из XML-каталога.
        xml_categories:        справочник категорий {id: name}.
        selected_category_ids: фильтр по категориям XML.
    """

    # Названия столбцов артикулов — фиксированы для всех МП
    _ARTICLE_COLUMNS: Dict[str, str] = {
        "wildberries": "Артикул продавца",
        "ozon":        "Артикул*",
        "yandex":      "Ваш SKU *",
    }

    def __init__(
        self,
        comparison_result: Dict,
        ai_comparator: object = None,
        xml_offer_data: Optional[List[Dict]] = None,
        xml_categories: Optional[Dict[str, str]] = None,
        selected_category_ids: Optional[List[str]] = None,
    ) -> None:
        self.comparison_result = comparison_result
        self.ai_comparator = ai_comparator
        self.xml_offer_data: List[Dict] = xml_offer_data or []
        self.xml_categories: Dict[str, str] = xml_categories or {}
        self.selected_category_ids = selected_category_ids

        # Разделяемое изменяемое состояние —
        # передаётся по ссылке во все компоненты, которые его пишут
        self.changes_log: Dict[str, List] = {
            "wildberries": [],
            "ozon":        [],
            "yandex":      [],
        }
        self.ai_validation_log: List[Dict] = []

        logger.info("Инициализация DataSynchronizer")
        logger.debug(f"AI comparator передан: {ai_comparator is not None}")

        if self.xml_offer_data:
            logger.info(f"XML данные: {len(self.xml_offer_data)} офферов")
            if self.selected_category_ids:
                logger.info(
                    f"Фильтр по категориям: "
                    f"{len(self.selected_category_ids)} категорий"
                )

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    async def synchronize_data(
        self,
        file_paths: Dict[str, str],
        output_paths: Optional[Dict[str, str]] = None,
        report_path: Optional[str] = None,
    ) -> Tuple[Dict[str, pd.DataFrame], Dict]:
        """
        Запускает полный цикл синхронизации данных.

        CPU-тяжёлые шаги (загрузка файлов, выравнивание артикулов,
        синхронизация габаритов, построение XML-индекса) выполняются
        через asyncio.to_thread() — event loop не блокируется.

        Параметр report_path оставлен для совместимости сигнатуры,
        но не используется внутри этого метода.

        Args:
            file_paths:   словарь {маркетплейс: путь к исходному файлу}.
            output_paths: словарь {маркетплейс: путь к выходному файлу}.
                          Если None — файлы не сохраняются.
            report_path:  не используется (оставлен для совместимости).

        Returns:
            Кортеж (словарь синхронизированных DataFrame, changes_log).
        """
        logger.info("=" * 60)
        logger.info("СИНХРОНИЗАЦИЯ ДАННЫХ МЕЖДУ МАРКЕТПЛЕЙСАМИ")
        logger.info("=" * 60)

        # ----------------------------------------------------------
        # Шаг 1: Загрузка файлов и validation-списков
        # CPU + IO: openpyxl читает xlsx, pandas строит DataFrame.
        # Выносим в поток — на 10k строк занимает 1–3 сек.
        # ----------------------------------------------------------
        excel_manager = self._build_excel_manager()
        logger.info("📂 Загрузка файлов (в потоке)...")
        dfs = await asyncio.to_thread(
            excel_manager.load_all_dataframes, file_paths
        )

        # ----------------------------------------------------------
        # Шаг 2: AI-валидатор
        # column_validations доступны только после load_all_dataframes
        # ----------------------------------------------------------
        ai_validator = self._build_ai_validator(excel_manager.column_validations)

        # ----------------------------------------------------------
        # Шаг 3: XML-индекс
        # CPU: итерация по списку офферов, фильтрация по категориям.
        # Выносим в поток — при 50k офферов заметная пауза.
        # ----------------------------------------------------------
        xml_syncer = self._build_xml_syncer(ai_validator)
        if self.xml_offer_data:
            logger.info("📦 Построение XML-индекса (в потоке)...")
            await asyncio.to_thread(xml_syncer.build_index)

        # ----------------------------------------------------------
        # Шаг 4: Выравнивание артикулов
        # CPU: pandas concat, set-операции, фильтрация строк.
        # Выносим в поток.
        # ----------------------------------------------------------
        article_aligner = self._build_article_aligner(xml_syncer.xml_article_map)
        logger.info("🔀 Выравнивание артикулов (в потоке)...")
        dfs = await asyncio.to_thread(article_aligner.align, dfs)

        # ----------------------------------------------------------
        # Шаг 5: Синхронизация габаритов
        # CPU: шесть проходов по трём DataFrame (чтение + запись).
        # Выносим в поток.
        # ----------------------------------------------------------
        logger.info("\n" + "=" * 60)
        logger.info("📐 СИНХРОНИЗАЦИЯ ГАБАРИТОВ (в потоке)")
        logger.info("=" * 60)
        _, resolved_wb_dims = await asyncio.to_thread(
            DimensionsSynchronizer.sync_dimensions, dfs
        )

        xml_syncer._resolved_wb_dims = resolved_wb_dims

        # ----------------------------------------------------------
        # Шаг 6: Синхронизация остальных столбцов по схеме
        # Async: внутри цикла есть await AI-запросов — нельзя в поток.
        # Event loop обрабатывает других пользователей между await.
        # ----------------------------------------------------------
        logger.info("\n" + "=" * 60)
        logger.info("📝 СИНХРОНИЗАЦИЯ ОСТАЛЬНЫХ СТОЛБЦОВ")
        logger.info("=" * 60)
        column_syncer = self._build_column_syncer(
            ai_validator, article_aligner, resolved_wb_dims
        )
        synced_dfs = await column_syncer.sync_all_matches(dfs)

        # ----------------------------------------------------------
        # Шаг 7: [МВМ] Заполнение из XML-каталога
        # Async: внутри цикла есть await AI-запросов — нельзя в поток.
        # sync_dimensions_from_xml — чистый CPU, выносим в поток.
        # ----------------------------------------------------------
        if self.xml_offer_data:
            logger.info("\n" + "=" * 60)
            logger.info("📦 ЗАПОЛНЕНИЕ ИЗ XML КАТАЛОГА")
            logger.info("=" * 60)

            xml_filled = await xml_syncer.sync_from_xml(synced_dfs)

            logger.info("📐 Габариты из XML (в потоке)...")
            xml_dims_filled = await asyncio.to_thread(
                xml_syncer.sync_dimensions_from_xml, synced_dfs
            )

            xml_total = xml_filled + xml_dims_filled
            logger.info(
                f"✅ Из XML заполнено: {xml_filled} ячеек (данные) + "
                f"{xml_dims_filled} ячеек (габариты) = {xml_total} итого"
            )
        else:
            logger.info(
                "\n[i] XML данные отсутствуют — пропускаю заполнение из каталога"
            )

        # ----------------------------------------------------------
        # Шаг 8: Сохранение МП-файлов
        # Async: внутри есть await AI-запросов при записи — нельзя в поток.
        # ----------------------------------------------------------
        if output_paths:
            await excel_manager.save_results(
                synced_dfs, output_paths, self.ai_validation_log
            )

        logger.info("\n" + "=" * 60)
        logger.info("✅ СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА!")
        logger.info("=" * 60)

        return synced_dfs, self.changes_log

    def create_ai_log_in_report(self, report_path: str) -> None:
        """
        Добавляет лист «AI_Логи» в файл отчёта.

        Вызывать ПОСЛЕ ExcelWriter.create_report_with_changes(),
        потому что ExcelWriter создаёт файл с нуля и затрёт лог
        если вызвать раньше.

        Args:
            report_path: путь к файлу отчёта (уже созданному ExcelWriter).
        """
        if not self.ai_validation_log:
            logger.info("AI-лог пуст — лист 'AI_Логи' не создаётся")
            return

        ExcelFileManager().create_ai_log_in_report(
            report_path, self.ai_validation_log
        )
        logger.info(
            f"📋 AI-лог добавлен в отчёт: {len(self.ai_validation_log)} записей"
        )

    # ------------------------------------------------------------------
    # Фабричные методы компонентов
    # ------------------------------------------------------------------

    def _build_excel_manager(self) -> ExcelFileManager:
        """Создаёт ExcelFileManager с инжектированным ai_comparator."""
        return ExcelFileManager(ai_comparator=self.ai_comparator)

    def _build_ai_validator(
        self,
        column_validations: Dict[str, Dict[str, List[str]]],
    ) -> AiValidator:
        """
        Создаёт AiValidator с разделяемым ai_validation_log.

        ai_validation_log передаётся по ссылке — все записи валидации
        автоматически попадают в self.ai_validation_log оркестратора.
        """
        return AiValidator(
            ai_comparator=self.ai_comparator,
            column_validations=column_validations,
            ai_validation_log=self.ai_validation_log,
        )

    def _build_xml_syncer(self, ai_validator: AiValidator) -> XmlSyncer:
        """
        Создаёт XmlSyncer без resolved_wb_dims.

        resolved_wb_dims будет задан после DimensionsSynchronizer.sync_dimensions
        через прямое присваивание xml_syncer._resolved_wb_dims.
        """
        return XmlSyncer(
            comparison_result=self.comparison_result,
            article_columns=self._ARTICLE_COLUMNS,
            value_converter=ValueConverter(),
            ai_validator=ai_validator,
            changes_log=self.changes_log,
            resolved_wb_dims=None,
            xml_offer_data=self.xml_offer_data,
            xml_categories=self.xml_categories,
            selected_category_ids=self.selected_category_ids,
        )

    def _build_article_aligner(
        self,
        xml_article_map: Dict[str, Dict],
    ) -> ArticleAligner:
        """
        Создаёт ArticleAligner с готовым xml_article_map.

        xml_article_map получается из XmlSyncer после build_index —
        артикулы из XML попадут в выравнивание.
        """
        return ArticleAligner(
            article_columns=self._ARTICLE_COLUMNS,
            xml_article_map=xml_article_map,
        )

    def _build_column_syncer(
        self,
        ai_validator: AiValidator,
        article_aligner: ArticleAligner,
        resolved_wb_dims: Optional[Dict[str, str]],
    ) -> ColumnSyncer:
        """
        Создаёт ColumnSyncer с разделяемым changes_log.

        changes_log передаётся по ссылке — все изменения из ColumnSyncer
        автоматически попадают в self.changes_log оркестратора.
        """
        return ColumnSyncer(
            comparison_result=self.comparison_result,
            article_columns=self._ARTICLE_COLUMNS,
            value_converter=ValueConverter(),
            ai_validator=ai_validator,
            article_aligner=article_aligner,
            changes_log=self.changes_log,
            resolved_wb_dims=resolved_wb_dims,
        )