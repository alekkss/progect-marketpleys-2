"""
WebSocket менеджер и маршрут для real-time прогресса задач.

WebSocketManager хранит активные соединения, сгруппированные по task_id.
Когда TaskWorker через WebDelivery вызывает ws_manager.notify(task_id, payload),
менеджер рассылает JSON всем подключённым браузерам, наблюдающим за этой задачей.

Паттерн: Observer — WebSocketManager является издателем (publisher),
браузеры подписываются на события конкретной задачи через WS-соединение.

Протокол сообщений (сервер → клиент):
    {"type": "progress",    "message": "⏳ Синхронизация..."}
    {"type": "files_ready", "count": 4, "filenames": [...]}
    {"type": "completed",   "message": "✅ Готово! ..."}
    {"type": "error",       "message": "Описание ошибки"}

Клиент → сервер:
    {"type": "ping"} — keepalive (ответ: {"type": "pong"})
"""

import asyncio
import json
from typing import Dict, List, Any

from aiohttp import web, WSMsgType
from aiohttp.web import Request, WebSocketResponse

from utils.logger_config import setup_logger

logger = setup_logger("web.websocket")


class WebSocketManager:
    """
    Менеджер WebSocket-соединений для real-time уведомлений.

    Хранит словарь {task_id: [ws_response, ...]} — для каждой задачи
    может быть несколько подписчиков (например, несколько вкладок
    одного пользователя).

    Потокобезопасность: все операции выполняются в одном event loop
    (aiohttp однопоточный), поэтому блокировки не нужны.

    Жизненный цикл соединения:
        1. Браузер подключается к /ws/tasks/{task_id}
        2. Соединение добавляется в _connections[task_id]
        3. TaskWorker вызывает notify() — сообщение рассылается
        4. При отключении браузера — соединение удаляется из словаря
    """

    def __init__(self) -> None:
        """Инициализация пустого хранилища соединений."""
        self._connections: Dict[str, List[WebSocketResponse]] = {}

    @property
    def active_connections_count(self) -> int:
        """
        Общее количество активных WebSocket-соединений.

        Полезно для мониторинга и health-check.

        Returns:
            Суммарное количество соединений по всем задачам
        """
        return sum(len(conns) for conns in self._connections.values())

    async def connect(self, task_id: str, ws: WebSocketResponse) -> None:
        """
        Регистрирует новое WebSocket-соединение для задачи.

        Args:
            task_id: ID задачи, за которой наблюдает клиент
            ws: Установленное WebSocket-соединение
        """
        if task_id not in self._connections:
            self._connections[task_id] = []
        self._connections[task_id].append(ws)
        logger.debug(
            "WebSocket подключён для задачи %s (всего подписчиков: %d)",
            task_id, len(self._connections[task_id]),
        )

    async def disconnect(self, task_id: str, ws: WebSocketResponse) -> None:
        """
        Удаляет WebSocket-соединение из хранилища.

        Вызывается при закрытии соединения (клиент ушёл, таймаут,
        ошибка сети). Если для задачи не осталось подписчиков —
        ключ удаляется из словаря для предотвращения утечки памяти.

        Args:
            task_id: ID задачи
            ws: Закрываемое соединение
        """
        connections = self._connections.get(task_id)
        if connections is None:
            return

        try:
            connections.remove(ws)
        except ValueError:
            pass

        if not connections:
            del self._connections[task_id]

        logger.debug(
            "WebSocket отключён от задачи %s (осталось подписчиков: %d)",
            task_id, len(self._connections.get(task_id, [])),
        )

    async def notify(self, task_id: str, payload: Dict[str, Any]) -> None:
        """
        Отправляет JSON-сообщение всем подписчикам задачи.

        Best-effort доставка: если соединение закрыто или ошибка
        при отправке — оно молча удаляется из списка. Потеря
        сообщения не критична — пользователь увидит результат
        при обновлении страницы (polling fallback через /api/tasks/{id}/status).

        Args:
            task_id: ID задачи
            payload: Словарь с данными (будет сериализован в JSON)
        """
        connections = self._connections.get(task_id)
        if not connections:
            return

        message = json.dumps(payload, ensure_ascii=False)
        dead_connections: List[WebSocketResponse] = []

        for ws in connections:
            try:
                if ws.closed:
                    dead_connections.append(ws)
                    continue
                await ws.send_str(message)
            except (ConnectionResetError, RuntimeError, Exception) as e:
                logger.debug(
                    "Ошибка отправки WebSocket для задачи %s: %s",
                    task_id, e,
                )
                dead_connections.append(ws)

        # Очистка мёртвых соединений
        for ws in dead_connections:
            try:
                connections.remove(ws)
            except ValueError:
                pass

        if not connections:
            del self._connections[task_id]

    async def close_all(self) -> None:
        """
        Закрывает все активные соединения.

        Вызывается при graceful shutdown веб-сервера.
        Каждому клиенту отправляется close frame.
        """
        total = self.active_connections_count
        if total == 0:
            return

        logger.info("Закрытие %d WebSocket-соединений...", total)

        for task_id, connections in list(self._connections.items()):
            for ws in connections:
                try:
                    if not ws.closed:
                        await ws.close(
                            code=WSMsgType.CLOSE,
                            message=b"Server shutting down",
                        )
                except Exception:
                    pass

        self._connections.clear()
        logger.info("Все WebSocket-соединения закрыты")


async def websocket_handler(request: Request) -> WebSocketResponse:
    """
    Обработчик WebSocket-соединения для прогресса задачи.

    URL: /ws/tasks/{task_id}

    Протокол:
        1. Клиент подключается, передавая task_id в URL
        2. Сервер регистрирует соединение в WebSocketManager
        3. Сервер слушает входящие сообщения (ping/pong keepalive)
        4. TaskWorker отправляет прогресс через ws_manager.notify()
        5. При закрытии — соединение удаляется из менеджера

    Авторизация:
        В Фазе 2 будет добавлена проверка cookie-сессии.
        Пока — соединение доступно без авторизации (для тестирования).

    Args:
        request: HTTP-запрос на upgrade до WebSocket

    Returns:
        WebSocket-ответ (keep-alive до закрытия)
    """
    task_id = request.match_info.get("task_id", "")
    if not task_id:
        raise web.HTTPBadRequest(reason="task_id обязателен")

    ws_manager: WebSocketManager = request.app["ws_manager"]

    ws = WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    await ws_manager.connect(task_id, ws)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Обработка keepalive от клиента
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                except (json.JSONDecodeError, TypeError):
                    pass

            elif msg.type == WSMsgType.ERROR:
                logger.warning(
                    "WebSocket ошибка для задачи %s: %s",
                    task_id, ws.exception(),
                )
                break

            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING):
                break
    finally:
        await ws_manager.disconnect(task_id, ws)

    return ws


def setup_websocket_routes(app: "web.Application") -> None:
    """
    Регистрирует WebSocket-маршрут.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/ws/tasks/{task_id}", websocket_handler)


__all__ = ["WebSocketManager", "setup_websocket_routes", "websocket_handler"]
