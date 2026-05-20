"""
Очередь задач для фоновой обработки файлов.

Модуль реализует паттерн Repository — абстракцию над брокером задач.
Поддерживает Redis (основной) и in-memory (fallback) реализации.

Паттерн: Repository — изолирует логику хранения задач от бизнес-логики.
Паттерн: Factory — create_task_queue() выбирает реализацию по URL.
"""

import asyncio
import json
import sys
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger_config import setup_logger

logger = setup_logger("task_queue")

TaskStatus = Literal["pending", "processing", "completed", "failed", "cancelled"]


@dataclass
class Task:
    """
    Модель задачи на синхронизацию.

    Использует dataclass (стандартная библиотека) — без Pydantic.
    """

    user_id: int
    chat_id: int
    task_type: Literal["standard", "mvm"]
    schema_id: int
    file_paths: Dict[str, str]
    output_dir: str
    report_path: str
    xml_file_path: Optional[str] = None
    status: TaskStatus = "pending"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    error_message: Optional[str] = None
    result_message: Optional[str] = None
    total_synced: Optional[int] = None
    wb_count: Optional[int] = None
    ozon_count: Optional[int] = None
    yandex_count: Optional[int] = None
    xml_filled: Optional[int] = None

    def to_json(self) -> str:
        """Сериализация в JSON."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Task":
        """Десериализация из JSON."""
        data = json.loads(raw)
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class TaskQueue(ABC):
    """
    Абстракция очереди задач.

    Паттерн: Repository — скрывает детали хранения
    (Redis / in-memory / PostgreSQL) за единым интерфейсом.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Установка соединения с брокером."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Закрытие соединения с брокером."""

    @abstractmethod
    async def enqueue(self, task: Task) -> None:
        """Добавляет задачу в конец очереди."""

    @abstractmethod
    async def dequeue(self) -> Optional[Task]:
        """
        Извлекает задачу из начала очереди.

        Блокирует до появления задачи или таймаута.
        """

    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[Task]:
        """Возвращает задачу по ID."""

    @abstractmethod
    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        error_message: Optional[str] = None,
        result_message: Optional[str] = None,
        total_synced: Optional[int] = None,
        wb_count: Optional[int] = None,
        ozon_count: Optional[int] = None,
        yandex_count: Optional[int] = None,
        xml_filled: Optional[int] = None,
    ) -> None:
        """Обновляет статус и метаданные задачи."""

    @abstractmethod
    async def get_user_tasks(self, user_id: int, limit: int = 10) -> List[Task]:
        """Возвращает последние задачи пользователя."""

    @abstractmethod
    async def get_queue_length(self) -> int:
        """Возвращает количество задач в очереди (pending)."""


class RedisTaskQueue(TaskQueue):
    """
    Реализация очереди на Redis.

    Хранение:
      - task:queue — Redis List (LPUSH / BRPOP)
      - task:data:{id} — Redis Hash (поля задачи)
      - task:user:{user_id} — Redis Set (индекс задач пользователя)
    """

    def __init__(self, redis_url: str, queue_key: str = "bot:task_queue") -> None:
        self._redis_url = redis_url
        self._queue_key = queue_key
        self._redis: Optional["redis.asyncio.Redis"] = None  # type: ignore[name-defined]

    async def connect(self) -> None:
        """Подключение к Redis."""
        try:
            import redis.asyncio as redis_lib
            self._redis = redis_lib.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("Очередь задач подключена к Redis")
        except Exception as e:
            logger.error("Не удалось подключиться к Redis для очереди задач: %s", e)
            raise ConnectionError(
                f"Не удалось подключиться к Redis для очереди задач: {e}"
            ) from e

    async def disconnect(self) -> None:
        """Закрытие соединения."""
        if self._redis:
            await self._redis.close()
            logger.info("Соединение очереди задач с Redis закрыто")

    async def enqueue(self, task: Task) -> None:
        """Добавляет задачу в очередь."""
        if not self._redis:
            raise RuntimeError("Redis не подключен. Вызовите connect() перед enqueue().")

        task_data = json.loads(task.to_json())
        pipe = self._redis.pipeline()
        # Сохраняем данные задачи в Hash
        pipe.hset(
            f"task:data:{task.id}",
            mapping={
                k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                for k, v in task_data.items()
            },
        )
        # Индекс по пользователю
        pipe.sadd(f"task:user:{task.user_id}", task.id)
        # Добавляем в очередь
        pipe.lpush(self._queue_key, task.id)
        await pipe.execute()
        logger.info("Задача %s поставлена в очередь (пользователь %s)", task.id, task.user_id)

    async def dequeue(self) -> Optional[Task]:
        """Извлекает задачу из очереди (блокирующее чтение с таймаутом)."""
        if not self._redis:
            raise RuntimeError("Redis не подключен.")

        result = await self._redis.brpop(self._queue_key, timeout=5)
        if not result:
            return None

        task_id = result[1]
        logger.debug("Задача %s извлечена из очереди", task_id)
        return await self.get_task(task_id)

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Возвращает задачу по ID из Hash."""
        if not self._redis:
            raise RuntimeError("Redis не подключен.")

        raw = await self._redis.hgetall(f"task:data:{task_id}")
        if not raw:
            return None

        data: Dict = {}
        for k, v in raw.items():
            try:
                data[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                data[k] = v

        try:
            return Task.from_json(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            logger.error("Ошибка десериализации задачи %s: %s", task_id, e)
            return None

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        error_message: Optional[str] = None,
        result_message: Optional[str] = None,
        total_synced: Optional[int] = None,
        wb_count: Optional[int] = None,
        ozon_count: Optional[int] = None,
        yandex_count: Optional[int] = None,
        xml_filled: Optional[int] = None,
    ) -> None:
        """Обновляет поля задачи в Hash."""
        if not self._redis:
            raise RuntimeError("Redis не подключен.")

        fields: Dict[str, str] = {
            "status": status,
            "updated_at": datetime.now().isoformat(),
        }
        if error_message is not None:
            fields["error_message"] = error_message
        if result_message is not None:
            fields["result_message"] = result_message
        if total_synced is not None:
            fields["total_synced"] = str(total_synced)
        if wb_count is not None:
            fields["wb_count"] = str(wb_count)
        if ozon_count is not None:
            fields["ozon_count"] = str(ozon_count)
        if yandex_count is not None:
            fields["yandex_count"] = str(yandex_count)
        if xml_filled is not None:
            fields["xml_filled"] = str(xml_filled)

        await self._redis.hset(f"task:data:{task_id}", mapping=fields)
        logger.debug("Задача %s: статус обновлён на '%s'", task_id, status)

    async def get_user_tasks(self, user_id: int, limit: int = 10) -> List[Task]:
        """Возвращает последние задачи пользователя."""
        if not self._redis:
            raise RuntimeError("Redis не подключен.")

        task_ids = await self._redis.smembers(f"task:user:{user_id}")
        selected = list(task_ids)[:limit]

        tasks: List[Task] = []
        for tid in selected:
            task = await self.get_task(tid)
            if task:
                tasks.append(task)

        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    async def get_queue_length(self) -> int:
        """Возвращает длину очереди."""
        if not self._redis:
            raise RuntimeError("Redis не подключен.")
        return await self._redis.llen(self._queue_key)


class InMemoryTaskQueue(TaskQueue):
    """
    In-memory реализация очереди (fallback при недоступности Redis).

    Не переживает перезапуск, но позволяет боту работать без Redis.
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._user_index: Dict[int, List[str]] = {}

    async def connect(self) -> None:
        """Нет необходимости в подключении."""

    async def disconnect(self) -> None:
        """Нет необходимости в отключении."""

    async def enqueue(self, task: Task) -> None:
        self._tasks[task.id] = task
        self._queue.put_nowait(task.id)
        self._user_index.setdefault(task.user_id, []).append(task.id)
        logger.info(
            "Задача %s поставлена в in-memory очередь (пользователь %s)",
            task.id,
            task.user_id,
        )

    async def dequeue(self) -> Optional[Task]:
        try:
            task_id = await asyncio.wait_for(self._queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return None

        return self._tasks.get(task_id)

    async def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        error_message: Optional[str] = None,
        result_message: Optional[str] = None,
        total_synced: Optional[int] = None,
        wb_count: Optional[int] = None,
        ozon_count: Optional[int] = None,
        yandex_count: Optional[int] = None,
        xml_filled: Optional[int] = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return

        task.status = status
        task.updated_at = datetime.now().isoformat()
        if error_message is not None:
            task.error_message = error_message
        if result_message is not None:
            task.result_message = result_message
        if total_synced is not None:
            task.total_synced = total_synced
        if wb_count is not None:
            task.wb_count = wb_count
        if ozon_count is not None:
            task.ozon_count = ozon_count
        if yandex_count is not None:
            task.yandex_count = yandex_count
        if xml_filled is not None:
            task.xml_filled = xml_filled

    async def get_user_tasks(self, user_id: int, limit: int = 10) -> List[Task]:
        ids = self._user_index.get(user_id, [])[-limit:]
        tasks = [self._tasks[tid] for tid in ids if tid in self._tasks]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    async def get_queue_length(self) -> int:
        return self._queue.qsize()


def create_task_queue(
    redis_url: Optional[str] = None, queue_key: str = "bot:task_queue"
) -> TaskQueue:
    """
    Фабрика создания очереди задач.

    Паттерн: Factory — скрывает выбор конкретной реализации.
    При наличии redis_url возвращает RedisTaskQueue,
    иначе — InMemoryTaskQueue (graceful degradation).
    """
    if redis_url:
        try:
            return RedisTaskQueue(redis_url, queue_key)
        except Exception as e:
            logger.warning(
                "Не удалось создать RedisTaskQueue (%s), используем in-memory", e
            )
    return InMemoryTaskQueue()