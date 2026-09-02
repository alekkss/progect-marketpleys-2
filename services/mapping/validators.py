"""
Валидация входящих заданий AI-агента маппинга PIM+FDM.

Проверяет тело POST /v1/mapping-tasks на соответствие спецификации
протокола (разделы 3-4) и преобразует его в dataclass-модели
из services/mapping/models.py.

Валидация полностью ручная — только isinstance-проверки и if/raise,
без сторонних библиотек валидации.

Статусы ошибок (используются маршрутом v1_api.py):
    - 400 — невалидный JSON-синтаксис (ловит маршрут, не этот модуль)
    - 422 — семантические ошибки: отсутствующие поля, неверные типы,
            неизвестные enum-значения, дубликаты ID, превышение лимитов

Все функции — чистые (без побочных эффектов и IO), что делает
модуль тривиальным для покрытия unit-тестами.
"""

from typing import Any, Dict, List, Optional

from config.config import Config
from services.mapping.models import (
    AttributeMappingTask,
    CategoryAttribute,
    CategoryInfo,
    ChannelAttribute,
    ChannelInfo,
    ChannelReferenceValue,
    MappingTask,
    ReferenceAttribute,
    ReferenceChannel,
    ReferenceValueMappingTask,
)
from utils.logger_config import setup_logger

logger = setup_logger("mapping.validators")

# Допустимые значения taskType (разделы 3.1 и 4.1 спецификации)
_VALID_TASK_TYPES = {"attribute_mapping", "reference_value_mapping"}

# Допустимые значения referenceType (раздел 4.1.1 спецификации)
_VALID_REFERENCE_TYPES = {"single", "multiple", "boolean"}


class MappingValidationError(Exception):
    """
    Ошибка валидации тела задания.

    Атрибуты:
        message: Человекочитаемое описание ошибки (в ответе API)
        status: HTTP-статус для ответа (422 по умолчанию)
    """

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = int(status)


# ===================================================================
# Проверки примитивных типов
# ===================================================================
# ВАЖНО: bool в Python — подкласс int, поэтому isinstance(True, int)
# возвращает True. Для строгой проверки integer исключаем bool.

def _is_int(value: Any) -> bool:
    """Проверяет, что значение — целое число (но не bool)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _describe_type(value: Any) -> str:
    """
    Возвращает человекочитаемое описание типа значения для сообщения ошибки.

    Args:
        value: Проверяемое значение

    Returns:
        Строка вида "integer", "string ('Цвет')", "значение отсутствует или null"
    """
    if value is None:
        return "значение отсутствует или null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "число с плавающей точкой"
    if isinstance(value, str):
        preview = value[:50] + ("..." if len(value) > 50 else "")
        return f"строка ('{preview}')"
    if isinstance(value, list):
        return "массив"
    if isinstance(value, dict):
        return "объект"
    return type(value).__name__


# ===================================================================
# Хелперы извлечения полей (бросают MappingValidationError)
# ===================================================================

def _require_dict(container: Dict[str, Any], key: str, path: str) -> Dict[str, Any]:
    """
    Извлекает обязательное вложенное поле-объект.

    Args:
        container: Родительский объект
        key: Имя поля
        path: Путь до родительского объекта (для сообщения об ошибке)

    Returns:
        Словарь — значение поля

    Raises:
        MappingValidationError: если поле отсутствует или не объект
    """
    value = container.get(key)
    if not isinstance(value, dict):
        raise MappingValidationError(
            f"{path}.{key}: ожидается объект, получено: {_describe_type(value)}"
        )
    return value


def _require_list(
    container: Dict[str, Any],
    key: str,
    path: str,
    allow_empty: bool = False,
) -> List[Any]:
    """
    Извлекает обязательное поле-массив.

    Args:
        container: Родительский объект
        key: Имя поля
        path: Путь до родительского объекта
        allow_empty: Допустим ли пустой массив

    Returns:
        Список — значение поля

    Raises:
        MappingValidationError: если поле отсутствует, не массив или пуст при запрете
    """
    value = container.get(key)
    if not isinstance(value, list):
        raise MappingValidationError(
            f"{path}.{key}: ожидается массив, получено: {_describe_type(value)}"
        )
    if not allow_empty and not value:
        raise MappingValidationError(
            f"{path}.{key}: массив не должен быть пустым"
        )
    return value


def _require_str(container: Dict[str, Any], key: str, path: str) -> str:
    """
    Извлекает обязательное поле-строку (непустую, не из пробелов).

    Raises:
        MappingValidationError: если поле отсутствует или не непустая строка
    """
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MappingValidationError(
            f"{path}.{key}: ожидается непустая строка, получено: {_describe_type(value)}"
        )
    return value


def _require_int(container: Dict[str, Any], key: str, path: str) -> int:
    """
    Извлекает обязательное поле-целое число (bool не допускается).

    Raises:
        MappingValidationError: если поле отсутствует или не integer
    """
    value = container.get(key)
    if not _is_int(value):
        raise MappingValidationError(
            f"{path}.{key}: ожидается целое число (integer), "
            f"получено: {_describe_type(value)}"
        )
    return value


def _require_bool(container: Dict[str, Any], key: str, path: str) -> bool:
    """
    Извлекает обязательное поле-булево значение.

    Raises:
        MappingValidationError: если поле отсутствует или не boolean
    """
    value = container.get(key)
    if not isinstance(value, bool):
        raise MappingValidationError(
            f"{path}.{key}: ожидается boolean, получено: {_describe_type(value)}"
        )
    return value


def _optional_str(container: Dict[str, Any], key: str, path: str) -> Optional[str]:
    """
    Извлекает необязательное поле-строку.

    Returns:
        Строка или None (поле отсутствует / null)
    """
    value = container.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MappingValidationError(
            f"{path}.{key}: ожидается строка или null, "
            f"получено: {_describe_type(value)}"
        )
    return value


def _optional_int(container: Dict[str, Any], key: str, path: str) -> Optional[int]:
    """
    Извлекает необязательное поле-целое число.

    Returns:
        Целое число или None (поле отсутствует / null)
    """
    value = container.get(key)
    if value is None:
        return None
    if not _is_int(value):
        raise MappingValidationError(
            f"{path}.{key}: ожидается целое число или null, "
            f"получено: {_describe_type(value)}"
        )
    return value


def _check_unique_int_ids(ids: List[int], field_name: str, path: str) -> None:
    """
    Проверяет уникальность списка целочисленных идентификаторов.

    Дубликаты ID ломают применение результата на стороне FDM
    и пост-валидацию маппера, поэтому отвергаются на входе.

    Raises:
        MappingValidationError: при первом найденном дубликате
    """
    seen = set()
    for index, item_id in enumerate(ids):
        if item_id in seen:
            raise MappingValidationError(
                f"{path}: дубликат {field_name}={item_id} "
                f"(элемент с индексом {index}) — идентификаторы должны быть уникальны"
            )
        seen.add(item_id)


# ===================================================================
# Точка входа
# ===================================================================

def parse_mapping_task(payload: Any) -> MappingTask:
    """
    Валидирует тело задания и возвращает соответствующую dataclass-модель.

    Единая точка входа для маршрута POST /v1/mapping-tasks.

    Args:
        payload: Распарсенный JSON тела запроса (результат request.json())

    Returns:
        AttributeMappingTask или ReferenceValueMappingTask

    Raises:
        MappingValidationError: при любой ошибке валидации (status 422)
    """
    if not isinstance(payload, dict):
        raise MappingValidationError(
            f"Корневой элемент запроса должен быть объектом, "
            f"получено: {_describe_type(payload)}"
        )

    task_type = payload.get("taskType")
    if not isinstance(task_type, str) or not task_type:
        raise MappingValidationError(
            f"taskType: ожидается непустая строка, получено: {_describe_type(task_type)}"
        )
    if task_type not in _VALID_TASK_TYPES:
        raise MappingValidationError(
            f"taskType: неизвестный тип задания '{task_type}'. "
            f"Допустимые значения: {', '.join(sorted(_VALID_TASK_TYPES))}"
        )

    if task_type == "attribute_mapping":
        task = _parse_attribute_mapping(payload)
        _check_attribute_task_limits(task)
    else:
        task = _parse_reference_value_mapping(payload)
        _check_reference_task_limits(task)

    logger.info(
        "Задание валидно: тип=%s, схема=%s",
        task.task_type,
        task.schema_id,
    )
    return task


# ===================================================================
# Задача 1: attribute_mapping — парсинг
# ===================================================================

def _parse_attribute_mapping(payload: Dict[str, Any]) -> AttributeMappingTask:
    """
    Разбирает тело задания маппинга атрибутов (раздел 3.1 спецификации).
    """
    schema_id = _require_int(payload, "schemaId", "корень")

    category_payload = _require_dict(payload, "category", "корень")
    category = _parse_category(category_payload)

    channels_payload = _require_list(payload, "channels", "корень")
    channels = [
        _parse_channel(item, f"channels[{i}]")
        for i, item in enumerate(channels_payload)
    ]

    return AttributeMappingTask(
        task_type="attribute_mapping",
        schema_id=schema_id,
        category=category,
        channels=channels,
    )


def _parse_category(payload: Dict[str, Any]) -> CategoryInfo:
    """
    Разбирает объект category (раздел 3.1.1 спецификации).
    """
    attributes_payload = _require_list(payload, "attributes", "category")
    attributes = [
        _parse_category_attribute(item, f"category.attributes[{i}]")
        for i, item in enumerate(attributes_payload)
    ]
    _check_unique_int_ids(
        [a.mapping_id for a in attributes], "mappingId", "category.attributes"
    )

    return CategoryInfo(
        id=_require_int(payload, "id", "category"),
        name=_require_str(payload, "name", "category"),
        attributes=attributes,
        path=_optional_str(payload, "path", "category"),
    )


def _parse_category_attribute(payload: Dict[str, Any], path: str) -> CategoryAttribute:
    """
    Разбирает элемент category.attributes[] (раздел 3.1.1.1 спецификации).
    """
    return CategoryAttribute(
        mapping_id=_require_int(payload, "mappingId", path),
        infomodel_attribute_id=_require_int(payload, "infomodelAttributeId", path),
        name=_require_str(payload, "name", path),
        slug=_require_str(payload, "slug", path),
        kind=_require_str(payload, "kind", path),
        group_id=_optional_int(payload, "groupId", path),
        group_name=_optional_str(payload, "groupName", path),
    )


def _parse_channel(payload: Dict[str, Any], path: str) -> ChannelInfo:
    """
    Разбирает элемент channels[] задачи 1 (раздел 3.1.2 спецификации).

    platform проверяется как непустая строка БЕЗ whitelist-значений —
    список платформ расширяется на стороне FDM без правок этого валидатора
    (Open/Closed). Значение используется только как контекст в промпте.
    """
    attributes_payload = _require_list(
        payload, "attributes", path, allow_empty=True
    )
    attributes = [
        _parse_channel_attribute(item, f"{path}.attributes[{i}]")
        for i, item in enumerate(attributes_payload)
    ]
    _check_unique_int_ids(
        [a.channel_attribute_id for a in attributes],
        "channelAttributeId",
        f"{path}.attributes",
    )

    return ChannelInfo(
        schema_channel_id=_require_int(payload, "schemaChannelId", path),
        channel_id=_require_int(payload, "channelId", path),
        platform=_require_str(payload, "platform", path),
        name=_require_str(payload, "name", path),
        attributes=attributes,
        template_id=_optional_int(payload, "templateId", path),
    )


def _parse_channel_attribute(payload: Dict[str, Any], path: str) -> ChannelAttribute:
    """
    Разбирает элемент channels[].attributes[] (раздел 3.1.2.1 спецификации).
    """
    return ChannelAttribute(
        channel_attribute_id=_require_int(payload, "channelAttributeId", path),
        name=_require_str(payload, "name", path),
        kind=_require_str(payload, "kind", path),
        required=_require_bool(payload, "required", path),
        external_id=_optional_str(payload, "externalId", path),
        unit=_optional_str(payload, "unit", path),
        description=_optional_str(payload, "description", path),
    )


# ===================================================================
# Задача 2: reference_value_mapping — парсинг
# ===================================================================

def _parse_reference_value_mapping(payload: Dict[str, Any]) -> ReferenceValueMappingTask:
    """
    Разбирает тело задания маппинга значений (раздел 4.1 спецификации).
    """
    schema_id = _require_int(payload, "schemaId", "корень")
    mapping_id = _require_int(payload, "mappingId", "корень")

    attribute_payload = _require_dict(payload, "attribute", "корень")
    attribute = _parse_reference_attribute(attribute_payload)

    channels_payload = _require_list(payload, "channels", "корень")
    channels = [
        _parse_reference_channel(item, f"channels[{i}]")
        for i, item in enumerate(channels_payload)
    ]
    _check_unique_int_ids(
        [c.schema_channel_id for c in channels], "schemaChannelId", "channels"
    )

    return ReferenceValueMappingTask(
        task_type="reference_value_mapping",
        schema_id=schema_id,
        mapping_id=mapping_id,
        attribute=attribute,
        channels=channels,
    )


def _parse_reference_attribute(payload: Dict[str, Any]) -> ReferenceAttribute:
    """
    Разбирает объект attribute (раздел 4.1.1 спецификации).

    referenceType — закрытый enum по спецификации (single | multiple | boolean),
    поэтому проверяется жёстко: защита от опечаток интеграции.
    """
    reference_type = _require_str(payload, "referenceType", "attribute")
    if reference_type not in _VALID_REFERENCE_TYPES:
        raise MappingValidationError(
            f"attribute.referenceType: неизвестное значение '{reference_type}'. "
            f"Допустимые значения: {', '.join(sorted(_VALID_REFERENCE_TYPES))}"
        )

    values_payload = _require_list(payload, "referenceValues", "attribute")
    reference_values: List[str] = []
    for i, item in enumerate(values_payload):
        if not isinstance(item, str) or not item.strip():
            raise MappingValidationError(
                f"attribute.referenceValues[{i}]: ожидается непустая строка, "
                f"получено: {_describe_type(item)}"
            )
        reference_values.append(item)

    return ReferenceAttribute(
        infomodel_attribute_id=_require_int(
            payload, "infomodelAttributeId", "attribute"
        ),
        name=_require_str(payload, "name", "attribute"),
        slug=_require_str(payload, "slug", "attribute"),
        reference_type=reference_type,
        reference_values=reference_values,
    )


def _parse_reference_channel(payload: Dict[str, Any], path: str) -> ReferenceChannel:
    """
    Разбирает элемент channels[] задачи 2 (раздел 4.1.2 спецификации).

    Справочник канала допускается пустым: результатом будут записи
    с channelValue=null для всех значений (валидно по протоколу).
    """
    values_payload = _require_list(
        payload, "referenceValues", path, allow_empty=True
    )
    reference_values: List[ChannelReferenceValue] = []
    for i, item in enumerate(values_payload):
        item_path = f"{path}.referenceValues[{i}]"
        if not isinstance(item, dict):
            raise MappingValidationError(
                f"{item_path}: ожидается объект, получено: {_describe_type(item)}"
            )
        reference_values.append(ChannelReferenceValue(
            id=_require_int(item, "id", item_path),
            value=_require_str(item, "value", item_path),
        ))
    _check_unique_int_ids(
        [v.id for v in reference_values], "id", f"{path}.referenceValues"
    )

    return ReferenceChannel(
        schema_channel_id=_require_int(payload, "schemaChannelId", path),
        platform=_require_str(payload, "platform", path),
        name=_require_str(payload, "name", path),
        reference_values=reference_values,
        channel_attribute_id=_optional_int(payload, "channelAttributeId", path),
    )


# ===================================================================
# Проверка лимитов (после успешного парсинга)
# ===================================================================
# Лимиты защищают от гигантских промптов: один AI-запрос на задание
# должен предсказуемо укладываться в контекстное окно модели.
# Превышение любого лимита — 422 с указанием переменной окружения.

def _check_attribute_task_limits(task: AttributeMappingTask) -> None:
    """
    Проверяет лимиты задания маппинга атрибутов.

    Raises:
        MappingValidationError: при превышении любого лимита
    """
    attrs_count = len(task.category.attributes)
    if attrs_count > Config.AGENT_MAX_ATTRIBUTES:
        raise MappingValidationError(
            f"category.attributes: превышен лимит — {attrs_count} атрибутов "
            f"при максимуме {Config.AGENT_MAX_ATTRIBUTES} "
            f"(переменная AGENT_MAX_ATTRIBUTES)"
        )

    channels_count = len(task.channels)
    if channels_count > Config.AGENT_MAX_CHANNELS:
        raise MappingValidationError(
            f"channels: превышен лимит — {channels_count} каналов "
            f"при максимуме {Config.AGENT_MAX_CHANNELS} "
            f"(переменная AGENT_MAX_CHANNELS)"
        )

    for channel in task.channels:
        count = len(channel.attributes)
        if count > Config.AGENT_MAX_CHANNEL_ATTRIBUTES:
            raise MappingValidationError(
                f"channels[schemaChannelId={channel.schema_channel_id}].attributes: "
                f"превышен лимит — {count} атрибутов при максимуме "
                f"{Config.AGENT_MAX_CHANNEL_ATTRIBUTES} "
                f"(переменная AGENT_MAX_CHANNEL_ATTRIBUTES)"
            )


def _check_reference_task_limits(task: ReferenceValueMappingTask) -> None:
    """
    Проверяет лимиты задания маппинга значений.

    Raises:
        MappingValidationError: при превышении любого лимита
    """
    channels_count = len(task.channels)
    if channels_count > Config.AGENT_MAX_CHANNELS:
        raise MappingValidationError(
            f"channels: превышен лимит — {channels_count} каналов "
            f"при максимуме {Config.AGENT_MAX_CHANNELS} "
            f"(переменная AGENT_MAX_CHANNELS)"
        )

    values_count = len(task.attribute.reference_values)
    if values_count > Config.AGENT_MAX_REFERENCE_VALUES:
        raise MappingValidationError(
            f"attribute.referenceValues: превышен лимит — {values_count} значений "
            f"при максимуме {Config.AGENT_MAX_REFERENCE_VALUES} "
            f"(переменная AGENT_MAX_REFERENCE_VALUES)"
        )

    for channel in task.channels:
        count = len(channel.reference_values)
        if count > Config.AGENT_MAX_REFERENCE_VALUES:
            raise MappingValidationError(
                f"channels[schemaChannelId={channel.schema_channel_id}]"
                f".referenceValues: превышен лимит — {count} значений "
                f"при максимуме {Config.AGENT_MAX_REFERENCE_VALUES} "
                f"(переменная AGENT_MAX_REFERENCE_VALUES)"
            )
