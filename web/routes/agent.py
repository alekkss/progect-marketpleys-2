"""
Дашборд оператора AI-агента маппинга PIM+FDM (маршруты /agent).

Отображает историю заданий, поступивших от FDM через
POST /v1/mapping-tasks (п. 6 доработок):

    GET /agent           — таблица истории запросов:
                           схема, каналы, тип маппинга, дата,
                           длительность, статус, счётчики
    GET /agent/{job_id}  — детализация: произведённые связки
                           с confidence и нераспознанное

Доступ — через СУЩЕСТВУЮЩУЮ cookie-авторизацию сайта,
только роли admin и owner (@admin_required, п. 6.4 доработок).
Контур Bearer-аутентификации /v1/* сюда не относится.

Паттерн: Controller — тонкие обработчики; данные готовятся
в структуры для Jinja2-шаблонов web/templates/agent/*.
Хранение — database.Database (mapping_jobs).
Регистрация маршрутов — setup_agent_routes(app), вызывается
из web/routes/__init__.py (единая точка setup_routes).
"""

import math
from typing import Any, Dict, List, Optional

import aiohttp_jinja2
from aiohttp import web
from aiohttp.web import Request, Response

from bot import storage
from utils.logger_config import setup_logger
from web.auth.decorators import admin_required
from web.middleware.csrf import get_csrf_token

logger = setup_logger("web.routes.agent")

# Количество заданий на странице списка
_PAGE_SIZE: int = 20

# Максимальная длина поисковой строки (защита от перегрузки ILIKE)
_MAX_SEARCH_LENGTH: int = 100


def _task_type_label(task_type: Optional[str]) -> str:
    """
    Человекочитаемое название типа задания для шаблона.

    Args:
        task_type: 'attribute_mapping' | 'reference_value_mapping' | None

    Returns:
        Подпись для отображения в таблице
    """
    if task_type == "attribute_mapping":
        return "Атрибуты"
    if task_type == "reference_value_mapping":
        return "Значения"
    return task_type or "—"


def _status_label(status: Optional[str]) -> Dict[str, str]:
    """
    Данные о статусе для цветового оформления в шаблоне.

    Args:
        status: Статус задания из БД

    Returns:
        Словарь {text, color}: подпись и CSS-класс Tailwind
    """
    mapping = {
        "pending": ("В ожидании", "bg-yellow-100 text-yellow-800"),
        "processing": ("Обрабатывается", "bg-blue-100 text-blue-800"),
        "completed": ("Завершено", "bg-green-100 text-green-800"),
        "failed": ("Ошибка", "bg-red-100 text-red-800"),
        "cancelled": ("Отменено", "bg-slate-100 text-slate-600"),
    }
    text, color = mapping.get(status or "", (status or "—", "bg-slate-100 text-slate-600"))
    return {"text": text, "color": color}


def _format_duration(duration_sec: Optional[float]) -> str:
    """
    Форматирует длительность обработки для таблицы.

    Args:
        duration_sec: Длительность в секундах (REAL из БД) или None

    Returns:
        Строка вида "12.4 сек" или "—"
    """
    if duration_sec is None:
        return "—"
    if duration_sec < 10:
        return f"{duration_sec:.1f} сек"
    return f"{duration_sec:.0f} сек"


def _format_channels_list(channels: List[Dict[str, Any]]) -> str:
    """
    Собирает строку названий каналов для таблицы.

    Args:
        channels: [{platform, name, schemaChannelId}] из БД

    Returns:
        Строка "Ozon, Wildberries" или "—"
    """
    if not channels:
        return "—"
    names = []
    for channel in channels:
        name = channel.get("name") or channel.get("platform")
        if name:
            names.append(str(name))
    return ", ".join(names) if names else "—"


@admin_required
async def agent_dashboard(request: Request) -> Response:
    """
    GET /agent — история запросов от FDM (п. 6.1-6.2).

    Возможности:
        - Таблица заданий, новые сверху (сортировка в SQL)
        - Поиск по schemaId, названию категории и атрибута
        - Пагинация по _PAGE_SIZE записей

    Контекст шаблона agent/list.html:
        jobs            — строки таблицы
        search          — текущая поисковая строка
        page, pages     — текущая страница и их общее число
        total           — всего записей по фильтру
        prev_offset, next_offset — оффсеты соседних страниц
        user, csrf_token — обязательные для base.html
    """
    user_data = request["user"]
    csrf_token = get_csrf_token(request)

    search = (request.query.get("search") or "").strip()[:_MAX_SEARCH_LENGTH]

    try:
        page = max(1, int(request.query.get("page") or "1"))
    except ValueError:
        page = 1

    total = await storage.db.get_mapping_jobs_count(search)
    pages = max(1, math.ceil(total / _PAGE_SIZE))
    page = min(page, pages)

    offset = (page - 1) * _PAGE_SIZE
    jobs_raw = await storage.db.get_mapping_jobs_list(
        search=search, limit=_PAGE_SIZE, offset=offset
    )

    jobs: List[Dict[str, Any]] = []
    for job in jobs_raw:
        jobs.append({
            "job_id": job["job_id"],
            "task_type": job["task_type"],
            "task_type_label": _task_type_label(job["task_type"]),
            "schema_id": job["schema_id"],
            "status": job["status"],
            "status_data": _status_label(job["status"]),
            "title": job["category_name"] or job["attribute_name"] or "—",
            "channels_count": job["channels_count"],
            "matched_count": job["matched_count"],
            "unresolved_count": job["unresolved_count"],
            "duration": _format_duration(job["duration_sec"]),
            "created_at": job["created_at"],
            "error_message": job["error_message"],
        })

    return aiohttp_jinja2.render_template(
        "agent/list.html",
        request,
        {
            "jobs": jobs,
            "search": search,
            "page": page,
            "pages": pages,
            "total": total,
            "prev_offset": page - 1 if page > 1 else None,
            "next_offset": page + 1 if page < pages else None,
            "user": user_data,
            "csrf_token": csrf_token,
        },
    )


@admin_required
async def agent_job_detail(request: Request) -> Response:
    """
    GET /agent/{job_id} — детализация задания (п. 6.3).

    Показывает произведённые связки (атрибуты или значения),
    уверенность каждой связки и нераспознанное.

    Контекст шаблона agent/detail.html:
        job             — общие данные задания (заголовок, статусы)
        attribute_rows  — строки для attribute_mapping
        unresolved_rows — названия нераспознанных атрибутов
        value_rows      — строки для reference_value_mapping
        user, csrf_token — обязательные для base.html
    """
    user_data = request["user"]
    csrf_token = get_csrf_token(request)

    job_id = request.match_info["job_id"]

    job = await storage.db.get_mapping_job_detail(job_id)
    if job is None:
        raise web.HTTPNotFound(
            text=f"Задание агента {job_id} не найдено"
        )

    context: Dict[str, Any] = {
        "user": user_data,
        "csrf_token": csrf_token,
        "job": {
            "job_id": job["job_id"],
            "task_type": job["task_type"],
            "task_type_label": _task_type_label(job["task_type"]),
            "schema_id": job["schema_id"],
            "status": job["status"],
            "status_data": _status_label(job["status"]),
            "title": job["category_name"] or job["attribute_name"] or "—",
            "channels": _format_channels_list(job["channels"]),
            "duration": _format_duration(job["duration_sec"]),
            "created_at": job["created_at"],
            "completed_at": job["completed_at"],
            "error_message": job["error_message"],
        },
        "attribute_rows": [],
        "unresolved_rows": [],
        "value_rows": [],
    }

    result = job.get("result")

    # --- Задача 1: связки атрибутов ---
    if job["task_type"] == "attribute_mapping":
        context["attribute_rows"], context["unresolved_rows"] = (
            _build_attribute_rows(job)
        )
    # --- Задача 2: пары значений ---
    elif job["task_type"] == "reference_value_mapping" and result:
        context["value_rows"] = _build_value_rows(job)

    return aiohttp_jinja2.render_template(
        "agent/detail.html",
        request,
        context,
    )


# ===================================================================
# Подготовка строк детализации
# ===================================================================
# Хелперы собирают плоские структуры из payload и result — шаблон
# (шаг 23) остаётся чистым отображением без вложенной логики.
# Данные достаются из payload потому, что результат хранит только ID,
# а оператору нужны человекочитаемые названия атрибутов и каналов.

def _build_attribute_rows(job: Dict[str, Any]) -> tuple:
    """
    Строит строки связок для detail-страницы attribute_mapping.

    Для каждой связки из result.results[] подставляет названия
    атрибутов из payload (по mappingId / channelAttributeId) и
    названия каналов (по schemaChannelId).

    Returns:
        Кортеж (attribute_rows, unresolved_rows):
        attribute_rows — [{attribute_name, confidence, comment, matches:
                          [{channel_name, attribute_name, confidence}]}]
        unresolved_rows — названия нераспознанных атрибутов
    """
    payload = job.get("payload") or {}
    result = job.get("result") or {}

    # Индексы payload: ID → названия
    attrs_by_mapping: Dict[int, Dict[str, Any]] = {
        attr.get("mappingId"): attr
        for attr in payload.get("category", {}).get("attributes", [])
        if isinstance(attr, dict)
    }
    channels_by_id: Dict[int, Dict[str, Any]] = {
        ch.get("schemaChannelId"): ch
        for ch in payload.get("channels", [])
        if isinstance(ch, dict)
    }

    def _channel_name(schema_channel_id: Any) -> str:
        channel = channels_by_id.get(schema_channel_id)
        if not channel:
            return f"Канал {schema_channel_id}"
        return str(channel.get("name") or channel.get("platform") or schema_channel_id)

    def _channel_attr_name(schema_channel_id: Any, channel_attribute_id: Any) -> str:
        channel = channels_by_id.get(schema_channel_id)
        if not channel:
            return f"Атрибут {channel_attribute_id}"
        for attr in channel.get("attributes", []):
            if isinstance(attr, dict) and attr.get("channelAttributeId") == channel_attribute_id:
                return str(attr.get("name") or channel_attribute_id)
        return f"Атрибут {channel_attribute_id}"

    rows: List[Dict[str, Any]] = []
    for match in result.get("results", []):
        if not isinstance(match, dict):
            continue

        source_attr = attrs_by_mapping.get(match.get("mappingId"))
        attribute_name = (
            str(source_attr.get("name")) if source_attr
            else f"Атрибут {match.get('mappingId')}"
        )

        channel_matches: List[Dict[str, Any]] = []
        for channel_match in match.get("channelMatches", []):
            if not isinstance(channel_match, dict):
                continue
            channel_matches.append({
                "channel_name": _channel_name(
                    channel_match.get("schemaChannelId")
                ),
                "attribute_name": _channel_attr_name(
                    channel_match.get("schemaChannelId"),
                    channel_match.get("channelAttributeId"),
                ),
                "confidence": channel_match.get("confidence"),
            })

        rows.append({
            "attribute_name": attribute_name,
            "confidence": match.get("confidence"),
            "comment": match.get("comment"),
            "matches": channel_matches,
        })

    unresolved_rows: List[str] = []
    for mapping_id in result.get("unresolved", []):
        source_attr = attrs_by_mapping.get(mapping_id)
        unresolved_rows.append(
            str(source_attr.get("name")) if source_attr
            else f"Атрибут {mapping_id}"
        )

    return rows, unresolved_rows


def _build_value_rows(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Строит таблицу значений для detail-страницы reference_value_mapping.

    Каждая строка — значение категории; столбцы — каналы связки
    (берутся из payload.channels в исходном порядке).

    Returns:
        Список строк [{info_value, cells: [{channel_name,
        channel_value, confidence}]}]
    """
    payload = job.get("payload") or {}
    result = job.get("result") or {}

    # Порядок каналов и их названия — из payload
    channels: List[Dict[str, Any]] = []
    for channel in payload.get("channels", []):
        if isinstance(channel, dict):
            channels.append({
                "schema_channel_id": channel.get("schemaChannelId"),
                "name": str(
                    channel.get("name") or channel.get("platform") or "—"
                ),
            })

    # Индекс результата: schemaChannelId → {infoValue → match}
    result_index: Dict[Any, Dict[Any, Dict[str, Any]]] = {}
    for channel_result in result.get("channels", []):
        if not isinstance(channel_result, dict):
            continue
        matches: Dict[Any, Dict[str, Any]] = {}
        for match in channel_result.get("matches", []):
            if isinstance(match, dict):
                matches[match.get("infoValue")] = match
        result_index[channel_result.get("schemaChannelId")] = matches

    rows: List[Dict[str, Any]] = []
    for info_value in payload.get("attribute", {}).get("referenceValues", []):
        cells: List[Dict[str, Any]] = []
        for channel in channels:
            match = result_index.get(
                channel["schema_channel_id"], {}
            ).get(info_value)
            cells.append({
                "channel_name": channel["name"],
                "channel_value": (
                    match.get("channelValue") if match else None
                ),
                "confidence": match.get("confidence") if match else None,
            })
        rows.append({"info_value": info_value, "cells": cells})

    return rows


def setup_agent_routes(app: web.Application) -> None:
    """
    Регистрирует маршруты дашборда агента маппинга.

    Вызывается из web/routes/__init__.py → setup_routes().
    Порядок внутри не критичен: /agent — фиксированный путь,
    /agent/{job_id} — динамический, конфликта нет.

    Args:
        app: Экземпляр aiohttp Application
    """
    app.router.add_get("/agent", agent_dashboard)
    app.router.add_get("/agent/{job_id}", agent_job_detail)
