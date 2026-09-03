# utils/logger_config.py
"""
Модуль конфигурации логирования.

Предоставляет единый логгер для всего проекта:
- консоль: человекочитаемый формат с белым списком ключей контекста
- файл: полный JSON-формат с ротацией (RotatingFileHandler)

Архитектура handler'ов (исправление дублей v6.1):
    - ОБА handler'а (консоль + файл) живут ТОЛЬКО на корневом логгере
      и устанавливаются один раз при первом вызове setup_logger();
    - модульные логгеры НЕ получают собственных handler'ов — записи
      доходят до корня через propagate и выводятся ровно один раз,
      независимо от вложенности имени (web.routes.v1_api → web.routes
      → root: свои handler'а есть только у root).

Уровни:
    - модульные логгеры (setup_logger) — LOG_LEVEL из .env (по умолчанию INFO);
      фильтр уровня проверяется на источнике записи, поэтому они
      НЕ зависят от уровня корневого логгера;
    - корневой логгер — WARNING: фильтрует INFO-шум «голых» логгеров
      сторонних библиотек (aiohttp, aiogram, asyncpg), у которых нет
      собственных handler'ов; их WARNING/ERROR проходят в консоль и файл.

Побочный эффект схемы: INFO-записи логгеров БЕЗ setup_logger
(например, 'config' из Config.validate()) в консоль не выводятся —
только WARNING и выше. Это осознанное решение: INFO-шум сторонних
библиотек в journalctl недопустим, а важные предупреждения config
пишутся именно уровнем WARNING.
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

# Уровень корневого логгера: фильтрует «голые» логгеры сторонних библиотек
_ROOT_LEVEL: int = logging.WARNING

# Ключи контекста, разрешённые к выводу в консоль
_CONSOLE_CONTEXT_WHITELIST: frozenset[str] = frozenset(
    {"path", "file", "error", "error_type", "step", "current", "total", "directory"}
)

# Флаг: handler'ы корневого логгера уже установлены
_root_handlers_installed: bool = False


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
# Установка handler'ов корневого логгера (один раз для всего процесса)
# ---------------------------------------------------------------------------

def _install_root_handlers() -> None:
    """
    Добавляет console + file handler'ы на КОРНЕВОЙ логгер.

    Вызывается только один раз благодаря флагу _root_handlers_installed.
    Все модульные логгеры (setup_logger) наследуют эти handler'ы через
    propagate — каждая запись выводится в консоль и файл РОВНО ОДИН РАЗ.
    """
    global _root_handlers_installed
    if _root_handlers_installed:
        return

    log_path = Path(_LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Консоль: один handler на всё приложение ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(_LOG_LEVEL)
    console_handler.setFormatter(_ConsoleFormatter())

    # --- Файл: ротация 10 МБ × 5 бэкапов ---
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=10 * 1024 * 1024,  # 10 МБ
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFileFormatter())

    root_logger = logging.getLogger()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    # WARNING для «голых» логгеров (aiohttp, aiogram, config):
    # их INFO не спамит консоль; наши модульные логгеры не зависят
    # от этого уровня (фильтр уровня проверяется на источнике записи)
    root_logger.setLevel(_ROOT_LEVEL)

    _root_handlers_installed = True


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def setup_logger(name: str) -> AppLogger:
    """
    Создаёт и возвращает именованный логгер приложения.

    При первом вызове устанавливает console + file handler'ы на
    корневой логгер (единая точка вывода). Сам модульный логгер
    handler'ов НЕ получает: записи доходят до корня через propagate
    и выводятся один раз — дублирование вложенных логгеров
    (web.routes.v1_api внутри web.routes) исключено.

    Args:
        name: имя модуля, например 'upload', 'task_worker', 'ai_comparator'

    Returns:
        AppLogger — адаптер с поддержкой context и trace_id
    """
    _install_root_handlers()

    logger = logging.getLogger(name)
    logger.setLevel(_LOG_LEVEL)
    # Handler'ов на модульном логгере НЕТ — вывод только через root.
    # propagate остаётся True по умолчанию: это и есть маршрут записи
    # к единственным console/file handler'ам приложения.

    return AppLogger(logger, {"trace_id": ""})
