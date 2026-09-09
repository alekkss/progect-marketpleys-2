"""
Модели данных заданий AI-агента маппинга PIM+FDM.

Структуры соответствуют спецификации протокола обмена
между PIM+FDM и AI-агентом (разделы 3-4):

    Входные данные:
        - AttributeMappingTask      — задание маппинга атрибутов схемы
        - ReferenceValueMappingTask — задание маппинга справочных значений

    Результаты:
        - AttributeMappingResult      — results[] + unresolved[] (этап 1)
        - CrossChannelMatch           — группа межканальных атрибутов (этап 2)
        - CrossChannelMappingResult   — crossChannelMatches[] (этап 2)
        - FullAttributeMappingResult  — объединённый результат этапов 1+2
        - ReferenceValueMappingResult — channels[] с matches[]

Модели — ТОЛЬКО данные (dataclasses из стандартной библиотеки).
Валидация входящего JSON выполняется в validators.py,
бизнес-логика маппинга — в attribute_mapper.py,
cross_channel_mapper.py и reference_value_mapper.py.

Сериализация результатов (to_dict) формирует camelCase-ключи
СТРОГО по протоколу — маршруты API отдают их без преобразований.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Union

# ===================================================================
# Общие типы
# ===================================================================

MappingTaskType = Literal["attribute_mapping", "reference_value_mapping"]
MappingJobStatus = Literal["pending", "processing", "completed", "failed"]

# ===================================================================
# Задача 1: маппинг атрибутов схемы — входные данные
# ===================================================================

@dataclass
class CategoryAttribute:
    """
    Атрибут конечной категории со статусом связки unmatched.

    Соответствует category.attributes[] из запроса.
    """

    mapping_id: int
    infomodel_attribute_id: int
    name: str
    slug: str
    kind: str
    group_id: Optional[int] = None
    group_name: Optional[str] = None

@dataclass
class CategoryInfo:
    """
    Конечная категория и её несопоставленные атрибуты.

    Соответствует объекту category из запроса.
    """

    id: int
    name: str
    attributes: List[CategoryAttribute] = field(default_factory=list)
    path: Optional[str] = None

@dataclass
class ChannelAttribute:
    """
    Атрибут категории канала (маркетплейса).

    Соответствует channels[].attributes[] из запроса.
    """

    channel_attribute_id: int
    name: str
    kind: str
    required: bool = False
    external_id: Optional[str] = None
    unit: Optional[str] = None
    description: Optional[str] = None

@dataclass
class ChannelInfo:
    """
    Канал, подключённый к схеме (ozon / wildberries / yandex_market).

    Соответствует элементу channels[] из запроса.
    """

    schema_channel_id: int
    channel_id: int
    platform: str
    name: str
    attributes: List[ChannelAttribute] = field(default_factory=list)
    template_id: Optional[int] = None

@dataclass
class AttributeMappingTask:
    """
    Задание маппинга атрибутов схемы (taskType = attribute_mapping).
    """

    task_type: Literal["attribute_mapping"]
    schema_id: int
    category: CategoryInfo
    channels: List[ChannelInfo]

# ===================================================================
# Задача 2: маппинг справочных значений — входные данные
# ===================================================================

@dataclass
class ReferenceAttribute:
    """
    Атрибут конечной категории со справочником значений.

    Соответствует объекту attribute из запроса.
    """

    infomodel_attribute_id: int
    name: str
    slug: str
    reference_type: str
    reference_values: List[str] = field(default_factory=list)

@dataclass
class ChannelReferenceValue:
    """
    Значение справочника канала (id + человекочитаемая строка).

    Соответствует channels[].referenceValues[] из запроса.
    """

    id: int
    value: str

@dataclass
class ReferenceChannel:
    """
    Канал связки со своим справочником значений.

    Соответствует элементу channels[] из запроса задачи 2.
    """

    schema_channel_id: int
    platform: str
    name: str
    reference_values: List[ChannelReferenceValue] = field(default_factory=list)
    channel_attribute_id: Optional[int] = None

@dataclass
class ReferenceValueMappingTask:
    """
    Задание маппинга справочных значений (taskType = reference_value_mapping).
    """

    task_type: Literal["reference_value_mapping"]
    schema_id: int
    mapping_id: int
    attribute: ReferenceAttribute
    channels: List[ReferenceChannel]

# Объединение для сигнатур воркера и валидатора
MappingTask = Union[AttributeMappingTask, ReferenceValueMappingTask]

# ===================================================================
# Задача 1: маппинг атрибутов схемы — результат
# ===================================================================

@dataclass
class ChannelMatch:
    """
    Найденное соответствие атрибута в конкретном канале.

    Соответствует results[].channelMatches[] из ответа.
    """

    schema_channel_id: int
    channel_attribute_id: int
    confidence: Optional[float] = None

    def to_dict(self) -> Dict:
        """Сериализация в camelCase по протоколу (confidence опционален)."""
        result: Dict = {
            "schemaChannelId": self.schema_channel_id,
            "channelAttributeId": self.channel_attribute_id,
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        return result

@dataclass
class MatchedBundle:
    """
    Предложенное соответствие по связке (строка results[]).

    channel_matches может быть пустым списком — FDM такие строки
    не привязывает (справочно, раздел 3.3 протокола).
    """

    mapping_id: int
    infomodel_attribute_id: int
    confidence: float
    comment: Optional[str] = None
    channel_matches: List[ChannelMatch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Сериализация в camelCase по протоколу (comment опционален)."""
        result: Dict = {
            "mappingId": self.mapping_id,
            "infomodelAttributeId": self.infomodel_attribute_id,
            "confidence": self.confidence,
            "channelMatches": [m.to_dict() for m in self.channel_matches],
        }
        if self.comment is not None:
            result["comment"] = self.comment
        return result

@dataclass
class AttributeMappingResult:
    """
    Результат первого этапа маппинга: results[] + unresolved[].

    unresolved — mappingId связок, для которых соответствие
    не найдено ни в одном канале.

    Начиная с v6.2 остаётся в пакете для обратной совместимости
    (этап 1 в изоляции); публичный тип возврата AttributeMapper —
    FullAttributeMappingResult.
    """

    results: List[MatchedBundle] = field(default_factory=list)
    unresolved: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Сериализация для GET-ответа при status=completed."""
        return {
            "results": [r.to_dict() for r in self.results],
            "unresolved": list(self.unresolved),
        }

# ===================================================================
# Задача 1: межканальные связки — результат этапа 2 (v6.2)
# ===================================================================

@dataclass
class CrossChannelMatch:
    """
    Группа атрибутов каналов, семантически эквивалентных друг другу,
    но не имеющих соответствия в категории каталога.

    Соответствует элементу crossChannelMatches[] в GET-ответе.
    Минимум 2 channelMatches — одиночный атрибут группой не является.
    """

    confidence: float
    comment: Optional[str] = None
    channel_matches: List[ChannelMatch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """
        Сериализация в camelCase по протоколу.

        comment отдаётся всегда (в т.ч. null) — стабильно для FDM:
        поле читается без проверки наличия ключа.
        """
        result: Dict = {
            "confidence": self.confidence,
            "comment": self.comment,
            "channelMatches": [m.to_dict() for m in self.channel_matches],
        }
        return result

@dataclass
class CrossChannelMappingResult:
    """
    Результат второго этапа: список межканальных групп.

    Пустой список — норма: все атрибуты каналов уже распределены
    по связкам results первого этапа (или сопоставлять не с кем).
    """

    matches: List[CrossChannelMatch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Сериализация: единый ключ crossChannelMatches по протоколу."""
        return {
            "crossChannelMatches": [m.to_dict() for m in self.matches],
        }

@dataclass
class FullAttributeMappingResult:
    """
    Объединённый результат обоих этапов маппинга атрибутов.

    Возвращаемый тип AttributeMapper.map_attributes() начиная с v6.2.
    to_dict() формирует полный JSON result для БД и GET-ответа.
    """

    results: List[MatchedBundle] = field(default_factory=list)
    unresolved: List[int] = field(default_factory=list)
    cross_channel_matches: List[CrossChannelMatch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Сериализация для GET-ответа при status=completed."""
        return {
            "results": [r.to_dict() for r in self.results],
            "unresolved": list(self.unresolved),
            "crossChannelMatches": [
                m.to_dict() for m in self.cross_channel_matches
            ],
        }

# ===================================================================
# Задача 2: маппинг справочных значений — результат
# ===================================================================

@dataclass
class ValueMatch:
    """
    Пара значений: значение категории → значение канала.

    Соответствует channels[].matches[] из ответа.
    channel_value/channel_value_id = None — соответствие не найдено
    (в JSON отдаётся явно как null, как в примере протокола).
    """

    info_value: str
    channel_value: Optional[str] = None
    channel_value_id: Optional[int] = None
    confidence: Optional[float] = None

    def to_dict(self) -> Dict:
        """Сериализация в camelCase по протоколу (confidence опционален)."""
        result: Dict = {
            "infoValue": self.info_value,
            "channelValueId": self.channel_value_id,
            "channelValue": self.channel_value,
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        return result

@dataclass
class ChannelValueMappingResult:
    """
    Результат сопоставления по одному каналу.

    matches содержит по одной записи на каждое значение
    attribute.referenceValues из запроса.
    """

    schema_channel_id: int
    matches: List[ValueMatch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Сериализация в camelCase по протоколу."""
        return {
            "schemaChannelId": self.schema_channel_id,
            "matches": [m.to_dict() for m in self.matches],
        }

@dataclass
class ReferenceValueMappingResult:
    """
    Результат маппинга значений: channels[] с matches[].
    """

    channels: List[ChannelValueMappingResult] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Сериализация для GET-ответа при status=completed."""
        return {
            "channels": [c.to_dict() for c in self.channels],
        }
