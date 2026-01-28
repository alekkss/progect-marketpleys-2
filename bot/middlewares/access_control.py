"""
Middleware для контроля доступа к боту
Блокирует неавторизованных пользователей
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
    Middleware для проверки прав доступа
    Пропускает только владельца, администратора и пользователей из whitelist
    """
    
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        """
        Проверяет права доступа перед обработкой события
        
        Args:
            handler: Следующий обработчик в цепочке
            event: Событие (Message или CallbackQuery)
            data: Данные для передачи обработчику
        
        Returns:
            Результат обработки или None если доступ запрещён
        """
        # Получаем user_id из события
        user_id = None
        if isinstance(event, Message):
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id
        
        # Если user_id не удалось получить - блокируем
        if user_id is None:
            return None
        
        # Проверяем доступ
        if not AccessManager.has_access(user_id):
            # Отправляем сообщение о блокировке
            if isinstance(event, Message):
                await event.answer(
                    "⛔ У вас нет доступа к этому боту.\n\n"
                    "Для получения доступа обратитесь к администратору."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "⛔ У вас нет доступа",
                    show_alert=True
                )
            
            # Блокируем дальнейшую обработку
            return None
        
        # Пользователь авторизован - передаём управление следующему обработчику
        return await handler(event, data)
