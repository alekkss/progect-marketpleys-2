"""
Фоновый воркер заданий AI-агента маппинга PIM+FDM.

Управляет жизненным циклом заданий из таблицы mapping_jobs:

    pending → processing → completed | failed

Цикл работы:
    1. При старте восстанавливает зависшие processing-задания
       (следы падения предыдущего процесса) в failed.
    2. Основной цикл атомарно забирает старейшее pending-задание
       из БД (FOR UPDATE SKIP LOCKED) и запускает обработку.
    3. Маршрутизация по task_type (Strategy):
        - attribute_mapping       → AttributeMapper
        - reference_value_mapping → ReferenceValueMapper
    4. Таймаут AGENT_JOB_TIMEOUT_SEC: зависшее задание помечается
       failed — укладывается в 5-минутный бюджет поллинга FDM.
    5. Раз в сутки удаляются завершённые задания старше
       AGENT_JOBS_RETENTION_DAYS.

AI-запросы идут через ОБЩИЙ AIComparator приложения: глобальный
семафор компаратора ограничивает суммарную нагрузку на LLM-провайдера
вместе с синхронизацией файлов (п. 4.3 доработок).

Паттерн: Service Layer — координация очереди, стратегий и БД.
Паттерн: Strategy — выбор маппера по task_type.
Паттерн: Dependency Injection — AIComparator инжектируется извне,
Database берётся из bot.storage (глобальный пул приложения).
"""

import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from config.config import Config
from services.mapping.attribute_mapper import AttributeMapper
from services.mapping.models import (
    AttributeMappingResult,
    ReferenceValueMappingResult,
)
from services.mapping.reference_value_mapper import ReferenceValueMapper
from services.mapping.validators import parse_mapping_task
from utils.logger_config import setup_logger

if TYPE_CHECKING:
    from services.ai_comparator import AIComparator

logger = setup_logger("mapping.job_worker")

# Интервал цикла обслуживания (очистка ретеншена) — 24 часа
_MAINTENANCE_INTERVAL_SEC: int = 24 * 60 * 60

# Сколько ждать активные задания при остановке, прежде чем отменять.
# Таймаут задания может быть 240 сек — держать остановку сервиса
# столько нельзя (systemd по умолчанию убивает через 90 сек),
# поэтому после ожидания оставшиеся задачи отменяются.
_STOP_GRACE_SEC: float = 30.0


class MappingJobWorker:
    """
    Фоновый воркер обработки заданий AI-агента.

    Один экземпляр на процесс (создаётся в bot/bot.py при включённом
    FDM_API_TOKEN). Мапперы создаются в конструкторе — промпты
    читаются с диска один раз за жизненный цикл приложения.

    Backpressure: семафор захватывается ПОСЛЕ claim (задание уже в
    processing в БД) и ДО создания asyncio.Task — параллельно
    существует не более max_concurrent корутин обработки.
    """

    def __init__(
        self,
        ai_comparator: "AIComparator",
        max_concurrent: Optional[int] = None,
        poll_interval: Optional[float] = None,
    ) -> None:
        """
        Args:
            ai_comparator: Общий AIComparator приложения (DI).
            max_concurrent: Максимум параллельных заданий
                (None — AGENT_MAX_CONCURRENT_JOBS из Config).
            poll_interval: Интервал опроса очереди в секундах
                (None — AGENT_POLL_INTERVAL_SEC из Config).

        Raises:
            FileNotFoundError: если отсутствует промпт одного
                из мапперов (fail fast — агент не стартует без промптов)
        """
        self._comparator = ai_comparator
        self._max_concurrent = max_concurrent or Config.AGENT_MAX_CONCURRENT_JOBS
        self._poll_interval = (
            poll_interval if poll_interval is not None else Config.AGENT_POLL_INTERVAL_SEC
        )
        self._job_timeout = Config.AGENT_JOB_TIMEOUT_SEC

        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._maintenance_task: Optional[asyncio.Task] = None
        self._active_tasks: Set[asyncio.Task] = set()

        # Стратегии обработки: создаются один раз, промпты с диска — один раз
        self._attribute_mapper = AttributeMapper(ai_comparator)
        self._reference_mapper = ReferenceValueMapper(ai_comparator)

    # ===================================================================
    # Жизненный цикл воркера
    # ===================================================================

    async def start(self) -> None:
        """
        Запускает воркер агента.

        Порядок:
            1. Восстановление зависших processing-заданий в failed
               (единственный воркер процесса не может иметь активных
               processing-заданий до запуска).
            2. Основной цикл обработки.
            3. Цикл обслуживания (очистка ретеншена).
        """
        if self._running:
            logger.warning("MappingJobWorker уже запущен")
            return

        from bot import storage  # модуль, не переменная — конвенция проекта

        recovered = await storage.db.recover_stale_mapping_jobs(stale_seconds=0)
        if recovered:
            logger.warning(
                "Восстановлено (failed) зависших заданий после перезапуска: %d",
                recovered,
            )

        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        logger.info(
            "MappingJobWorker запущен (параллельных заданий: %d, "
            "интервал опроса: %.1f сек, таймаут задания: %d сек)",
            self._max_concurrent,
            self._poll_interval,
            self._job_timeout,
        )

    async def stop(self) -> None:
        """
        Останавливает воркер.

        Порядок:
            1. Снятие флага running (новые задания не забираются).
            2. Отмена циклов (poll/maintenance спят — отмена мгновенна).
            3. Ожидание активных заданий до _STOP_GRACE_SEC.
            4. Отмена оставшихся: прерванные задания помечаются
               failed самим обработчиком (БД ещё доступна — воркер
               агента останавливается раньше shutdown_storage).
        """
        if not self._running:
            return

        logger.info("Остановка MappingJobWorker...")
        self._running = False

        for loop_task in (self._worker_task, self._maintenance_task):
            if loop_task and not loop_task.done():
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass

        if self._active_tasks:
            logger.info(
                "Ожидание %d активных заданий агента (до %.0f сек)...",
                len(self._active_tasks),
                _STOP_GRACE_SEC,
            )
            pending = list(self._active_tasks)
            self._active_tasks.clear()
            done, still_pending = await asyncio.wait(
                pending, timeout=_STOP_GRACE_SEC
            )
            if still_pending:
                logger.warning(
                    "Отмена %d заданий агента, не завершившихся за grace-период",
                    len(still_pending),
                )
                for task in still_pending:
                    task.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)

        logger.info("MappingJobWorker остановлен")

    # ===================================================================
    # Основной цикл
    # ===================================================================

    async def _worker_loop(self) -> None:
        """
        Цикл извлечения и запуска заданий.

        Пока в очереди есть pending-задания — забирает их подряд
        без паузы (быстрая разгрузка очереди). Пауза poll_interval
        выполняется только при пустой очереди.
        """
        from bot import storage

        while self._running:
            try:
                job = await storage.db.claim_pending_mapping_job()

                if job is None:
                    # Очередь пуста — ждём до следующего опроса
                    await asyncio.sleep(self._poll_interval)
                    continue

                logger.info(
                    "Задание агента %s забрано в обработку (тип=%s, схема=%s)",
                    job["job_id"], job["task_type"], job["schema_id"],
                )

                # Слот семафора — до создания asyncio.Task (backpressure)
                await self._semaphore.acquire()

                # Пока ждали слот — воркер могли остановить
                if not self._running:
                    self._semaphore.release()
                    break

                process_task = asyncio.create_task(
                    self._process_with_release(job)
                )
                self._active_tasks.add(process_task)
                process_task.add_done_callback(self._active_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Ошибка цикла воркера агента: %s", e, exc_info=True
                )
                await asyncio.sleep(self._poll_interval)

    async def _maintenance_loop(self) -> None:
        """Цикл обслуживания: раз в сутки чистит устаревшие задания."""
        from bot import storage

        while True:
            try:
                await asyncio.sleep(_MAINTENANCE_INTERVAL_SEC)
                await storage.db.cleanup_old_mapping_jobs(
                    Config.AGENT_JOBS_RETENTION_DAYS
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Ошибка цикла обслуживания агента: %s", e, exc_info=True
                )

    # ===================================================================
    # Обработка одного задания
    # ===================================================================

    async def _process_with_release(self, job: Dict) -> None:
        """
        Обёртка обработки с таймаутом и гарантированным release семафора.

        Обработка обёрнута в wait_for: зависшее задание (AI не ответил,
        сеть зависла) помечается failed по истечении AGENT_JOB_TIMEOUT_SEC.

        Отличие таймаута от остановки воркера:
            - таймаут → TimeoutError → пометка «таймаут»;
            - stop() → CancelledError при _running=False → пометка
              «воркер остановлен».
        """
        job_id = job["job_id"]
        try:
            try:
                await asyncio.wait_for(
                    self._execute_job(job),
                    timeout=self._job_timeout,
                )
            except TimeoutError:
                logger.error(
                    "Задание агента %s: превышен таймаут %d сек — failed",
                    job_id, self._job_timeout,
                )
                await self._safe_fail(
                    job_id,
                    f"Превышен таймаут обработки задания "
                    f"({self._job_timeout} сек)",
                )
            except asyncio.CancelledError:
                # Отмена из stop(): таймаут даёт TimeoutError (выше),
                # сюда попадаем только при остановке воркера
                if not self._running:
                    await self._safe_fail(
                        job_id,
                        "Воркер агента остановлен во время обработки — "
                        "задание прервано, повторите запрос",
                    )
                raise
            except Exception as e:
                logger.error(
                    "Задание агента %s завершено с ошибкой: %s",
                    job_id, e, exc_info=True,
                )
                await self._safe_fail(job_id, str(e))
        finally:
            self._semaphore.release()

    async def _safe_fail(self, job_id: str, error_message: str) -> None:
        """
        Помечает задание failed, проглатывая ошибки самой записи.

        При отмене задачи повторная отмена не должна маскировать
        исходную ошибку — запись в БД best effort.
        """
        from bot import storage

        try:
            await storage.db.mark_mapping_job_failed(job_id, error_message)
        except Exception as db_error:
            logger.error(
                "Не удалось пометить задание %s как failed: %s",
                job_id, db_error,
            )

    async def _execute_job(self, job: Dict) -> None:
        """
        Выполняет полный цикл обработки одного задания.

        Шаги:
            1. Повторный парсинг payload из БД (данные уже валидированы
               при POST — парсинг дешёвый и даёт dataclass-модели
               для маппера; повреждённый payload → failed).
            2. Маршрутизация по task_type в стратегию.
            3. Запись результата и счётчиков в БД (completed).

        Args:
            job: {job_id, task_type, schema_id, payload}

        Raises:
            Exception: ошибки валидации payload или AI-запроса —
                перехватываются _process_with_release → failed
        """
        from bot import storage

        job_id = job["job_id"]
        task = parse_mapping_task(job["payload"])

        if task.task_type == "attribute_mapping":
            result: AttributeMappingResult = await self._attribute_mapper.map_attributes(task)
            result_dict = result.to_dict()
            matched_count = len(result.results)
            unresolved_count = len(result.unresolved)
        else:
            value_result: ReferenceValueMappingResult = (
                await self._reference_mapper.map_values(task)
            )
            result_dict = value_result.to_dict()
            matched_count = sum(
                1
                for channel in value_result.channels
                for match in channel.matches
                if match.channel_value is not None
            )
            total_records = sum(
                len(channel.matches) for channel in value_result.channels
            )
            unresolved_count = total_records - matched_count

        await storage.db.mark_mapping_job_completed(
            job_id,
            result=result_dict,
            matched_count=matched_count,
            unresolved_count=unresolved_count,
        )
        logger.info(
            "Задание агента %s: обработано за воркером, сопоставлено=%d, "
            "без соответствий=%d",
            job_id, matched_count, unresolved_count,
        )
