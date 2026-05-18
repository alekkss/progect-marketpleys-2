"""
Middleware для контроля доступа к боту.

Блокирует неавторизованных пользователей на уровне диспетчера,
до передачи события в хендлер. Регистрируется для message
и callback_query в bot/bot.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

from bot.security import AccessManager


class AccessControlMiddleware(BaseMiddleware):
    """
    Middleware для проверки прав доступа.

    Пропускает только владельца, администратора и пользователей
    из whitelist. Все остальные получают сообщение об отказе.
    """

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: Dict[str, Any],
    ) -> Any:
        """
        Проверяет права доступа перед обработкой события.

        Args:
            handler: Следующий обработчик в цепочке
            event: Событие (Message или CallbackQuery)
            data: Данные для передаче обработчику

        Returns:
            Результат обработки или None если доступ запрещён
        """
        # Получаем user_id из события
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
        else:
            return None

        # Проверяем доступ (await — метод теперь асинхронный)
        if not await AccessManager.has_access(user_id):
            if isinstance(event, Message):
                await event.answer(
                    "⛔ У вас нет доступа к этому боту.\n\n"
                    "Для получения доступа обратитесь к администратору."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "⛔ У вас нет доступа",
                    show_alert=True,
                )
            return None

        # Пользователь авторизован — передаём управление дальше
        return await handler(event, data)