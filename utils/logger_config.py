# utils/logger_config.py
"""
Модуль конфигурации логирования.

Предоставляет единый логгер для всего проекта:
- консоль: человекочитаемый формат с белым списком ключей контекста
- файл: полный JSON-формат с ротацией (RotatingFileHandler)

Паттерн: Singleton на уровне корневого файлового handler —
файл app.log создаётся один раз при первом вызове setup_logger().
"""

import json
import logging
import logging.handlers
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, MutableMapping

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
_LOG_FILE_PATH: str = os.getenv("LOG_FILE_PATH", "./logs/app.log")
_LOG_LEVEL_STR: str = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_LEVEL: int = getattr(logging, _LOG_LEVEL_STR, logging.INFO)

# Ключи контекста, разрешённые к выводу в консоль
_CONSOLE_CONTEXT_WHITELIST: frozenset[str] = frozenset(
    {"path", "file", "error", "error_type", "step", "current", "total", "directory"}
)

# Флаг: файловый handler уже добавлен в корневой логгер
_file_handler_installed: bool = False


# ---------------------------------------------------------------------------
# Форматтеры
# ---------------------------------------------------------------------------

class _ConsoleFormatter(logging.Formatter):
    """
    Человекочитаемый форматтер для консоли.

    Формат: 2026-02-28 17:26:48 | INFO  | сообщение | key=value, ...
    Показывает только ключи из белого списка _CONSOLE_CONTEXT_WHITELIST.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        level = record.levelname.ljust(5)
        message = record.getMessage()

        # Извлекаем context из extra, если он был передан
        context: dict[str, Any] = getattr(record, "context", {})
        allowed = {k: v for k, v in context.items() if k in _CONSOLE_CONTEXT_WHITELIST}

        if allowed:
            ctx_str = ", ".join(f"{k}={v}" for k, v in allowed.items())
            return f"{timestamp} | {level} | {message} | {ctx_str}"

        return f"{timestamp} | {level} | {message}"


class _JsonFileFormatter(logging.Formatter):
    """
    JSON-форматтер для файла логов.

    Пишет полную запись: timestamp, level, message, logger,
    trace_id, context (все ключи), exception.
    """

    def format(self, record: logging.LogRecord) -> str:
        context: dict[str, Any] = getattr(record, "context", {})
        trace_id: str = getattr(record, "trace_id", "")

        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            ),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "trace_id": trace_id,
            "context": context,
        }

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LoggerAdapter с поддержкой trace_id и context
# ---------------------------------------------------------------------------

class AppLogger(logging.LoggerAdapter):
    """
    Адаптер логгера с поддержкой trace_id и произвольного контекста.

    Пример использования:
        logger = setup_logger('service')
        logger.info("задача_завершена", context={"file": "report.xlsx", "total": 42})
        logger.info("запрос", context={"user_id": 123}, trace_id="abc-123")
    """

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        extra = kwargs.setdefault("extra", {})
        extra["context"] = kwargs.pop("context", {})
        extra["trace_id"] = kwargs.pop("trace_id", self.extra.get("trace_id", ""))
        return msg, kwargs

    def new_trace(self) -> "AppLogger":
        """Возвращает новый адаптер с уникальным trace_id для отслеживания цепочки."""
        return AppLogger(self.logger, {"trace_id": uuid.uuid4().hex[:8]})


# ---------------------------------------------------------------------------
# Установка файлового handler (один раз для всего процесса)
# ---------------------------------------------------------------------------

def _install_file_handler() -> None:
    """
    Добавляет RotatingFileHandler в корневой логгер.

    Вызывается только один раз благодаря флагу _file_handler_installed.
    Все дочерние логгеры автоматически наследуют этот handler.
    """
    global _file_handler_installed
    if _file_handler_installed:
        return

    log_path = Path(_LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=10 * 1024 * 1024,  # 10 МБ
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFileFormatter())

    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.DEBUG)

    _file_handler_installed = True


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def setup_logger(name: str) -> AppLogger:
    """
    Создаёт и возвращает именованный логгер с двумя handler-ами.

    При первом вызове устанавливает единый файловый handler
    на корневой логгер (RotatingFileHandler → logs/app.log).
    Каждый вызов возвращает логгер с консольным handler для модуля.

    Args:
        name: имя модуля, например 'upload', 'task_worker', 'ai_comparator'

    Returns:
        AppLogger — адаптер с поддержкой context и trace_id
    """
    # Устанавливаем файловый handler один раз
    _install_file_handler()

    logger = logging.getLogger(name)
    logger.setLevel(_LOG_LEVEL)

    # Консольный handler добавляем только если его ещё нет
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
               for h in logger.handlers):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(_LOG_LEVEL)
        console_handler.setFormatter(_ConsoleFormatter())
        logger.addHandler(console_handler)

    # Не передаём записи в корневой логгер повторно через консоль
    logger.propagate = True

    return AppLogger(logger, {"trace_id": ""})