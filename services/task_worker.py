"""
Фоновый воркер обработки задач синхронизации.

Паттерн: Service Layer — координирует очередь, синхронизацию и отправку результатов.
Паттерн: Semaphore — ограничивает количество одновременных обработок.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional, Set
import time
from config.config import Config

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

class _FileCleanupService:
    """
    Фоновый сервис очистки устаревших временных файлов.

    Раз в 24 часа сканирует папки uploads, downloads, output
    и удаляет файлы старше FILE_MAX_AGE_DAYS дней.
    Директории берёт из Config, не из захардкоженных путей.

    Паттерн: Single Responsibility — отвечает только за очистку диска.
    """

    # Интервал между запусками уборки (в секундах)
    _CLEANUP_INTERVAL_SEC: int = 24 * 60 * 60  # 24 часа

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._dirs: list[Path] = [
            Path(Config.UPLOAD_DIR),
            Path(Config.DOWNLOAD_DIR),
            Path(Config.OUTPUT_DIR),
        ]
        self._max_age_days: int = Config.FILE_MAX_AGE_DAYS

    async def start(self) -> None:
        """Запускает фоновый цикл очистки."""
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "Уборщик файлов запущен — проверка каждые 24 ч, "
            "удаляем файлы старше %d дн.",
            self._max_age_days,
        )

    async def stop(self) -> None:
        """Останавливает фоновый цикл очистки."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Уборщик файлов остановлен")

    async def _cleanup_loop(self) -> None:
        """Основной цикл: ждёт интервал, затем запускает очистку."""
        while True:
            try:
                await asyncio.sleep(self._CLEANUP_INTERVAL_SEC)
                await asyncio.get_running_loop().run_in_executor(None, self._cleanup_sync)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Ошибка в цикле уборщика файлов: %s", e, exc_info=True)

    def _cleanup_sync(self) -> None:
        """
        Синхронная очистка — выполняется в executor, не блокирует event loop.

        Удаляет файлы (не директории) старше _max_age_days из всех папок.
        Логирует каждый удалённый файл и итоговую статистику.
        """
        cutoff = time.time() - self._max_age_days * 86400
        total_deleted = 0
        total_freed_bytes = 0

        for directory in self._dirs:
            if not directory.exists():
                logger.warning(
                    "Директория не найдена, пропускаем: %s", directory
                )
                continue

            for file_path in directory.iterdir():
                if not file_path.is_file():
                    continue
                try:
                    file_mtime = file_path.stat().st_mtime
                    if file_mtime < cutoff:
                        file_size = file_path.stat().st_size
                        file_path.unlink()
                        total_deleted += 1
                        total_freed_bytes += file_size
                        logger.info(
                            "Удалён устаревший файл: %s (возраст > %d дн.)",
                            file_path.name,
                            self._max_age_days,
                        )
                except Exception as e:
                    logger.error(
                        "Не удалось удалить файл %s: %s", file_path, e
                    )

        if total_deleted > 0:
            freed_mb = total_freed_bytes / (1024 * 1024)
            logger.info(
                "Уборка завершена: удалено %d файлов, освобождено %.1f МБ",
                total_deleted,
                freed_mb,
            )
        else:
            logger.info("Уборка завершена: устаревших файлов не найдено")


class TaskWorker:
    """
    Фоновый воркер для обработки задач синхронизации.

    Читает задачи из очереди и обрабатывает их с ограничением
    на количество одновременных обработок (по умолчанию 5).

    Backpressure: семафор захватывается ДО создания asyncio.Task.
    Это гарантирует, что при всплеске задач не будет создано больше
    корутин, чем позволяет лимит. Задачи ждут в очереди Redis/memory,
    а не в виде висящих корутин в event loop.

    AIComparator передаётся извне при создании воркера —
    тот же экземпляр используется в хендлерах через aiogram DI.
    Семафор внутри AIComparator глобальный для всех задач и хендлеров,
    что корректно ограничивает суммарное число AI-запросов.

    Паттерн: Service Layer — бизнес-логика обработки задач.
    Паттерн: Dependency Injection — comparator инжектируется извне.
    """

    def __init__(
        self,
        task_queue: TaskQueue,
        max_concurrent: int = 5,
        ai_comparator: Optional[AIComparator] = None,
    ) -> None:
        self._queue = task_queue
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._bot: Optional[Bot] = None
        self._active_tasks: Set[asyncio.Task] = set()

        # AIComparator передаётся извне (из start_bot) —
        # один экземпляр на весь жизненный цикл приложения.
        # Промпты читаются с диска один раз при создании comparator,
        # семафор общий для воркера и хендлеров.
        if ai_comparator is None:
            # Fallback: если не передан — создаём свой (обратная совместимость)
            self._comparator = AIComparator()
            logger.warning(
                "AIComparator не передан в TaskWorker — создан локальный экземпляр. "
                "Рекомендуется передавать общий экземпляр через параметр ai_comparator."
            )
        else:
            self._comparator = ai_comparator

        self._cleanup_service = _FileCleanupService()

    async def start(self, bot: Bot) -> None:
        """Запускает фоновый цикл обработки задач."""
        if self._running:
            logger.warning("TaskWorker уже запущен")
            return

        self._bot = bot
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        await self._cleanup_service.start()
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

        # Ждём завершения цикла извлечения задач (dequeue имеет таймаут)
        if self._worker_task and not self._worker_task.done():
            try:
                await asyncio.wait_for(self._worker_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass

        # Ждём завершения активных обработок
        if self._active_tasks:
            logger.info(
                "Ожидание завершения %d активных задач...", len(self._active_tasks)
            )
            pending = list(self._active_tasks)
            self._active_tasks.clear()
            await asyncio.gather(*pending, return_exceptions=True)

        # Закрываем HTTP-клиент компаратора после завершения всех задач
        await self._comparator.close()
        await self._cleanup_service.stop()

        logger.info("TaskWorker остановлен")

    async def _worker_loop(self) -> None:
        """
        Основной цикл: извлекает задачи и запускает их обработку.

        Backpressure реализован через acquire() ДО создания asyncio.Task:
        - dequeue() достаёт задачу из очереди (блокирует до 5 сек)
        - acquire() ждёт свободный слот семафора
        - Только после получения слота создаётся asyncio.Task

        Это гарантирует: максимум max_concurrent корутин одновременно.
        При всплеске 100 задач — они остаются в очереди Redis, а не
        висят как 100 корутин в памяти event loop.
        """
        while self._running:
            try:
                task = await self._queue.dequeue()
                if task is None:
                    continue
            except Exception as e:
                logger.error("Ошибка при извлечении задачи из очереди: %s", e)
                await asyncio.sleep(1)
                continue

            # Ждём свободный слот ДО создания asyncio.Task
            await self._semaphore.acquire()

            # Проверяем: не остановились ли пока ждали семафор
            if not self._running:
                self._semaphore.release()
                break

            # Создаём задачу — слот уже захвачен, release() в finally
            process_task = asyncio.create_task(self._process_with_release(task))
            self._active_tasks.add(process_task)
            process_task.add_done_callback(self._active_tasks.discard)

    async def _process_with_release(self, task: Task) -> None:
        """
        Обёртка над _execute_task с гарантированным освобождением семафора.

        Семафор уже захвачен в _worker_loop (acquire). Здесь гарантируем
        release() в finally — даже при исключении или отмене задачи.
        """
        try:
            await self._execute_task(task)
        finally:
            self._semaphore.release()

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

            # Используем общий экземпляр компаратора вместо создания нового
            synchronizer = DataSynchronizer(
                comparison_result,
                ai_comparator=self._comparator,
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

            # Создание отчёта
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
                caption="📊 Отчёт",
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
