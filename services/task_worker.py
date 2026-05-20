"""
Фоновый воркер обработки задач синхронизации.

Паттерн: Service Layer — координирует очередь, синхронизацию и отправку результатов.
Паттерн: Semaphore — ограничивает количество одновременных обработок.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from aiogram import Bot
from aiogram.types import FSInputFile

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot import storage
from services.ai_comparator import AIComparator
from services.data_synchronizer import DataSynchronizer
from services.task_queue import TaskQueue, Task
from utils.excel_writer import ExcelWriter
from utils.logger_config import setup_logger
from utils.xml_reader import XmlReader

logger = setup_logger("task_worker")


class TaskWorker:
    """
    Фоновый воркер для обработки задач синхронизации.

    Читает задачи из очереди и обрабатывает их с ограничением
    на количество одновременных обработок (по умолчанию 5).

    Паттерн: Service Layer — бизнес-логика обработки задач.
    """

    def __init__(self, task_queue: TaskQueue, max_concurrent: int = 5) -> None:
        self._queue = task_queue
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._bot: Optional[Bot] = None
        self._active_tasks: Set[asyncio.Task] = set()

    async def start(self, bot: Bot) -> None:
        """Запускает фоновый цикл обработки задач."""
        if self._running:
            logger.warning("TaskWorker уже запущен")
            return

        self._bot = bot
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info(
            "TaskWorker запущен (максимум %d параллельных обработок)",
            self._semaphore._value,
        )

    async def stop(self) -> None:
        """Останавливает воркер с ожиданием активных задач."""
        if not self._running:
            return

        logger.info("Остановка TaskWorker...")
        self._running = False

        # Ждем завершения цикла извлечения задач (dequeue имеет таймаут)
        if self._worker_task and not self._worker_task.done():
            try:
                await asyncio.wait_for(self._worker_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass

        # Ждем завершения активных обработок
        if self._active_tasks:
            logger.info(
                "Ожидание завершения %d активных задач...", len(self._active_tasks)
            )
            pending = list(self._active_tasks)
            self._active_tasks.clear()
            await asyncio.gather(*pending, return_exceptions=True)

        logger.info("TaskWorker остановлен")

    async def _worker_loop(self) -> None:
        """Основной цикл: извлекает задачи и запускает их обработку."""
        while self._running:
            try:
                task = await self._queue.dequeue()
                if task is None:
                    continue
            except Exception as e:
                logger.error("Ошибка при извлечении задачи из очереди: %s", e)
                await asyncio.sleep(1)
                continue

            # Запускаем обработку как фоновую задачу с семафором
            process_task = asyncio.create_task(self._process_task(task))
            self._active_tasks.add(process_task)
            process_task.add_done_callback(self._active_tasks.discard)

    async def _process_task(self, task: Task) -> None:
        """Обрабатывает одну задачу с учетом семафора."""
        async with self._semaphore:
            await self._execute_task(task)

    async def _execute_task(self, task: Task) -> None:
        """Выполняет полный цикл обработки задачи."""
        if not self._bot:
            logger.error("Bot не установлен, невозможно отправить результат")
            return

        logger.info(
            "Начало обработки задачи %s (пользователь %s)", task.id, task.user_id
        )

        await self._queue.update_status(task.id, "processing")
        await self._notify_user(task.chat_id, "⏳ Ваша задача начала обработку...")

        processing_id: Optional[int] = None

        try:
            # Регистрируем обработку в БД
            processing_id = await storage.db.start_processing(task.user_id)

            # Добавляем файлы в историю
            for marketplace, file_path in task.file_paths.items():
                await storage.db.add_file(
                    task.user_id,
                    processing_id,
                    marketplace,
                    os.path.basename(file_path),
                    file_path,
                )

            # Получаем схему сопоставлений
            comparison_result = await storage.db.get_schema_matches(task.schema_id)

            # Создаем AI компаратор
            comparator = AIComparator()

            # Подготовка DataSynchronizer
            xml_offer_data = None
            xml_categories = None
            selected_category_ids = None

            if task.task_type == "mvm" and task.xml_file_path:
                xml_reader = XmlReader()
                xml_offer_data = xml_reader.get_offer_data(task.xml_file_path)
                xml_categories = xml_reader.get_categories(task.xml_file_path)
                selected_category_ids = await storage.db.get_schema_category_ids(
                    task.schema_id
                )
                logger.info(
                    "МВМ задача: %d офферов, %d категорий",
                    len(xml_offer_data),
                    len(xml_categories),
                )

            synchronizer = DataSynchronizer(
                comparison_result,
                ai_comparator=comparator,
                xml_offer_data=xml_offer_data,
                xml_categories=xml_categories,
                selected_category_ids=selected_category_ids,
            )

            # Синхронизация
            output_sync_paths = {
                "wildberries": os.path.join(
                    task.output_dir, "WB_синхронизировано.xlsx"
                ),
                "ozon": os.path.join(task.output_dir, "Ozon_синхронизировано.xlsx"),
                "yandex": os.path.join(
                    task.output_dir, "Яндекс_синхронизировано.xlsx"
                ),
            }

            synced_dfs, changes_log = await synchronizer.synchronize_data(
                task.file_paths, output_sync_paths
            )

            # Создание отчета
            writer = ExcelWriter()
            writer.create_report_with_changes(
                comparison_result, changes_log, task.report_path
            )
            synchronizer.create_ai_log_in_report(task.report_path)

            # Статистика
            wb_count = len(synced_dfs["wildberries"])
            ozon_count = len(synced_dfs["ozon"])
            yandex_count = len(synced_dfs["yandex"])
            total_synced = sum(len(changes_log[mp]) for mp in changes_log)
            xml_filled = (
                sum(
                    1
                    for mp in changes_log
                    for change in changes_log[mp]
                    if change.get("source_marketplace") == "xml"
                )
                if task.task_type == "mvm"
                else None
            )

            # Завершаем обработку в БД
            await storage.db.complete_processing(
                processing_id, wb_count, ozon_count, yandex_count, total_synced
            )

            # Отправка результатов
            await self._notify_user(task.chat_id, "📤 Отправляю результаты...")
            for path in output_sync_paths.values():
                await self._bot.send_document(task.chat_id, FSInputFile(path))
            await self._bot.send_document(
                task.chat_id,
                FSInputFile(task.report_path),
                caption="📊 Отчет",
            )

            # Формируем текст результата
            result_text = (
                f"✅ Готово!\n\n📦 Обработка товаров:\n"
                f"• WB: {wb_count}\n• Ozon: {ozon_count}\n• Яндекс: {yandex_count}\n\n"
                f"🔄 Синхронизировано ячеек: {total_synced}"
            )
            if xml_filled and xml_filled > 0:
                result_text += f"\n📦 Из XML каталога: {xml_filled}"

            await self._notify_user(task.chat_id, result_text)

            # Обновляем статус в очереди
            await self._queue.update_status(
                task.id,
                "completed",
                result_message=result_text,
                total_synced=total_synced,
                wb_count=wb_count,
                ozon_count=ozon_count,
                yandex_count=yandex_count,
                xml_filled=xml_filled,
            )

            logger.info("Задача %s завершена успешно", task.id)

        except Exception as e:
            error_msg = str(e)
            logger.error(
                "Ошибка обработки задачи %s: %s", task.id, e, exc_info=True
            )

            if processing_id:
                await storage.db.fail_processing(processing_id, error_msg)

            await self._queue.update_status(
                task.id, "failed", error_message=error_msg
            )
            await self._notify_user(
                task.chat_id, f"❌ Ошибка обработки: {error_msg}"
            )

    async def _notify_user(self, chat_id: int, text: str) -> None:
        """Отправляет сообщение пользователю."""
        if not self._bot:
            return
        try:
            await self._bot.send_message(chat_id, text)
        except Exception as e:
            logger.error(
                "Не удалось отправить сообщение пользователю %s: %s", chat_id, e
            )