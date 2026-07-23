"""
Strategy доставки результатов обработки пользователю.

Паттерн: Strategy — TaskWorker вызывает методы ResultDelivery,
не зная конкретной реализации. Выбор реализации — ответственность
ResultDeliveryFactory, которая смотрит на task.delivery_channel.

Паттерн: Factory — ResultDeliveryFactory.create() скрывает логику
создания нужной стратегии.

Две реализации:
    - TelegramDelivery — отправка через Bot API (send_message, send_document)
    - WebDelivery — сохранение результатов в БД + уведомление через WebSocket
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger_config import setup_logger

if TYPE_CHECKING:
    from aiogram import Bot
    from services.task_queue import Task

logger = setup_logger("delivery")


class ResultDelivery(ABC):
    """
    Абстракция доставки результатов обработки пользователю.

    Определяет 4 метода, которые TaskWorker вызывает на разных
    этапах обработки задачи. Конкретная реализация решает, как
    именно доставить информацию (Telegram / WebSocket / email / ...).

    Принцип Open/Closed: новый канал доставки = новый подкласс,
    без изменения TaskWorker или существующих реализаций.
    """

    @abstractmethod
    async def send_progress(self, message: str) -> None:
        """
        Промежуточное сообщение о прогрессе обработки.

        Args:
            message: Текст прогресса (например, "⏳ Синхронизация столбцов...")
        """

    @abstractmethod
    async def send_files(self, file_paths: List[str], caption: str = "") -> None:
        """
        Отправка файлов результатов обработки.

        Args:
            file_paths: Список путей к файлам на диске
            caption: Подпись к файлам (опционально)
        """

    @abstractmethod
    async def send_result(self, text: str) -> None:
        """
        Финальное сообщение с итогами обработки.

        Args:
            text: Текст итогов (статистика, количество синхронизированных ячеек)
        """

    @abstractmethod
    async def send_error(self, error_message: str) -> None:
        """
        Сообщение об ошибке обработки.

        Args:
            error_message: Описание ошибки
        """


class TelegramDelivery(ResultDelivery):
    """
    Доставка результатов через Telegram Bot API.

    Отправляет сообщения и файлы напрямую в чат пользователя.
    Используется для задач, поставленных через Telegram-бота.
    """

    def __init__(self, bot: "Bot", chat_id: int) -> None:
        """
        Args:
            bot: Экземпляр aiogram Bot
            chat_id: Telegram chat_id для отправки
        """
        self._bot = bot
        self._chat_id = chat_id

    async def send_progress(self, message: str) -> None:
        """Отправляет текстовое сообщение о прогрессе."""
        try:
            await self._bot.send_message(self._chat_id, message)
        except Exception as e:
            logger.error(
                "Не удалось отправить прогресс в Telegram (chat_id=%s): %s",
                self._chat_id, e,
            )

    async def send_files(self, file_paths: List[str], caption: str = "") -> None:
        """Отправляет файлы как документы в чат."""
        from aiogram.types import FSInputFile

        for file_path in file_paths:
            try:
                await self._bot.send_document(
                    self._chat_id, FSInputFile(file_path),
                )
            except Exception as e:
                logger.error(
                    "Не удалось отправить файл %s в Telegram: %s",
                    file_path, e,
                )

        if caption:
            try:
                await self._bot.send_message(self._chat_id, caption)
            except Exception as e:
                logger.error(
                    "Не удалось отправить подпись в Telegram: %s", e,
                )

    async def send_result(self, text: str) -> None:
        """Отправляет финальное сообщение с итогами."""
        try:
            await self._bot.send_message(self._chat_id, text)
        except Exception as e:
            logger.error(
                "Не удалось отправить результат в Telegram (chat_id=%s): %s",
                self._chat_id, e,
            )

    async def send_error(self, error_message: str) -> None:
        """Отправляет сообщение об ошибке."""
        try:
            await self._bot.send_message(
                self._chat_id, f"❌ Ошибка обработки: {error_message}",
            )
        except Exception as e:
            logger.error(
                "Не удалось отправить ошибку в Telegram (chat_id=%s): %s",
                self._chat_id, e,
            )


class WebDelivery(ResultDelivery):
    """
    Доставка результатов для веб-интерфейса.

    Не отправляет файлы напрямую — они уже на диске.
    Вместо этого обновляет запись в БД (task_results) и
    уведомляет подключённый браузер через WebSocket.

    Принцип Single Responsibility: этот класс только уведомляет.
    Логика сохранения файлов — в DataSynchronizer/ExcelFileManager.
    """

    def __init__(
        self,
        task_id: str,
        web_user_id: int,
        ws_manager: Optional[object] = None,
    ) -> None:
        """
        Args:
            task_id: Уникальный ID задачи (для WebSocket-канала)
            web_user_id: ID пользователя в таблице web_users
            ws_manager: Экземпляр WebSocketManager (для broadcast)
        """
        self._task_id = task_id
        self._web_user_id = web_user_id
        self._ws_manager = ws_manager

    async def send_progress(self, message: str) -> None:
        """Отправляет прогресс через WebSocket."""
        await self._notify({
            "type": "progress",
            "message": message,
        })

    async def send_files(self, file_paths: List[str], caption: str = "") -> None:
        """
        Сохраняет пути к файлам в БД и уведомляет браузер.

        Файлы уже на диске (DataSynchronizer сохранил их в output_dir).
        Здесь мы только обновляем запись task_results для скачивания
        и отправляем WebSocket-событие "files_ready".
        """
        from bot import storage

        output_files: Dict[str, str] = {}
        for i, path in enumerate(file_paths):
            filename = Path(path).name
            output_files[filename] = path

        try:
            await storage.db.update_task_result(
                task_id=self._task_id,
                output_files=output_files,
            )
        except Exception as e:
            logger.error(
                "Не удалось обновить task_result для задачи %s: %s",
                self._task_id, e,
            )

        await self._notify({
            "type": "files_ready",
            "count": len(file_paths),
            "filenames": [Path(p).name for p in file_paths],
        })

    async def send_result(self, text: str) -> None:
        """Уведомляет браузер о завершении обработки."""
        from bot import storage

        try:
            await storage.db.update_task_result(
                task_id=self._task_id,
                status="completed",
            )
        except Exception as e:
            logger.error(
                "Не удалось обновить статус task_result %s: %s",
                self._task_id, e,
            )

        await self._notify({
            "type": "completed",
            "message": text,
        })

    async def send_error(self, error_message: str) -> None:
        """Уведомляет браузер об ошибке."""
        from bot import storage

        try:
            await storage.db.update_task_result(
                task_id=self._task_id,
                status="failed",
                error_message=error_message,
            )
        except Exception as e:
            logger.error(
                "Не удалось обновить ошибку task_result %s: %s",
                self._task_id, e,
            )

        await self._notify({
            "type": "error",
            "message": error_message,
        })

    async def _notify(self, payload: Dict) -> None:
        """
        Внутренний метод отправки через WebSocket.

        Если ws_manager не задан или нет подписчиков — молча пропускает.
        WebSocket — best-effort канал, потеря сообщения не критична
        (пользователь увидит результат при обновлении страницы).
        """
        if self._ws_manager is None:
            return

        try:
            await self._ws_manager.notify(self._task_id, payload)
        except Exception as e:
            logger.warning(
                "Не удалось отправить WebSocket-уведомление для задачи %s: %s",
                self._task_id, e,
            )


class ResultDeliveryFactory:
    """
    Фабрика создания стратегии доставки.

    Паттерн: Factory — скрывает выбор конкретной реализации.
    TaskWorker вызывает create() и получает готовый объект
    ResultDelivery, не зная деталей реализации.

    Выбор стратегии основан на task.delivery_channel:
        - "telegram" → TelegramDelivery(bot, chat_id)
        - "web" → WebDelivery(task_id, web_user_id, ws_manager)
    """

    @staticmethod
    def create(
        task: "Task",
        bot: Optional["Bot"] = None,
        ws_manager: Optional[object] = None,
    ) -> ResultDelivery:
        """
        Создаёт реализацию доставки по каналу задачи.

        Args:
            task: Задача с полями delivery_channel и web_user_id
            bot: Экземпляр aiogram Bot (для Telegram-доставки)
            ws_manager: Экземпляр WebSocketManager (для веб-доставки)

        Returns:
            Конкретная реализация ResultDelivery

        Raises:
            ValueError: если delivery_channel неизвестен
            RuntimeError: если bot=None для Telegram-задачи
        """
        if task.delivery_channel == "web":
            if task.web_user_id is None:
                logger.warning(
                    "Веб-задача %s без web_user_id — результаты не будут привязаны к аккаунту",
                    task.id,
                )
            return WebDelivery(
                task_id=task.id,
                web_user_id=task.web_user_id or 0,
                ws_manager=ws_manager,
            )

        if task.delivery_channel == "telegram":
            if bot is None:
                raise RuntimeError(
                    f"Bot не передан для Telegram-задачи {task.id}. "
                    f"Невозможно доставить результат."
                )
            return TelegramDelivery(bot=bot, chat_id=task.chat_id)

        raise ValueError(
            f"Неизвестный канал доставки: {task.delivery_channel!r} "
            f"(задача {task.id}). Допустимые: 'telegram', 'web'."
        )
