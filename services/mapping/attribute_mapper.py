"""
Стратегия маппинга атрибутов схемы (задача 1 протокола PIM+FDM).

Сопоставляет атрибуты конечной категории с атрибутами категорий
каналов (маркетплейсов) одним AI-запросом. Обрабатывает ответ LLM
в три этапа:

    1. Подготовка компактного JSON входных данных и форматирование
       промпта prompts/attribute_mapping.txt.
    2. Один запрос к LLM через ОБЩИЙ AIComparator приложения —
       семафор компаратора ограничивает суммарную нагрузку на
       LLM-провайдера (общую для синхронизации и маппинга).
    3. Пост-валидация: каждый идентификатор ответа сверяется со
       входными данными. Галлюцинации LLM отбрасываются, а список
       unresolved вычисляется детерминированно из входных данных —
       результату AI не доверяет ни одно поле-ссылка.

Паттерн: Strategy — одна из двух взаимозаменяемых стратегий
обработки заданий (вторая — reference_value_mapper).
Паттерн: Dependency Injection — AIComparator инжектируется извне
(общий экземпляр приложения, НЕ создаётся здесь).
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from config.config import Config
from services.mapping.models import (
    AttributeMappingResult,
    AttributeMappingTask,
    CategoryAttribute,
    ChannelInfo,
    ChannelMatch,
    MatchedBundle,
)
from utils.logger_config import setup_logger

if TYPE_CHECKING:
    from services.ai_comparator import AIComparator

logger = setup_logger("mapping.attribute_mapper")

# Путь к промпту (prompts/ на уровне корня проекта)
_ATTRIBUTE_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "prompts" / "attribute_mapping.txt"
)

# Ограничение длины комментария оператору (согласовано с промптом)
_MAX_COMMENT_LENGTH = 200


# ===================================================================
# Общие хелперы нормализации полей ответа AI
# ===================================================================
# Публичные чистые функции: reference_value_mapper переиспользует их
# на шаге 8. Если общих хелперов станет больше — выделим отдельный
# модуль пакета (пока двух функций отдельный модуль избыточен).

def sanitize_confidence(value: Any) -> Optional[float]:
    """
    Нормализует значение confidence из ответа LLM.

    Допускает числа 0..1 и строки вида "0.87". Эвристика процентов:
    значения из диапазона (1.0, 100.0] делятся на 100 (LLM иногда
    возвращает "87" вместо "0.87"). Значения вне [0, 1] после
    нормализации обрезаются до границ.

    Args:
        value: Сырое значение из ответа LLM (любого типа)

    Returns:
        Число в диапазоне [0.0, 1.0] или None, если значение
        не удалось интерпретировать
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    # NaN (float('nan') != float('nan')) — невалидное значение
    if number != number:
        return None

    # Эвристика процентов: 87 → 0.87, 99.5 → 0.995
    if 1.0 < number <= 100.0:
        number = number / 100.0

    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def truncate_comment(value: Any, max_length: int = _MAX_COMMENT_LENGTH) -> Optional[str]:
    """
    Нормализует комментарий оператору из ответа LLM.

    Args:
        value: Сырое значение (ожидается строка)
        max_length: Максимальная длина с учётом многоточия

    Returns:
        Строка без краевых пробелов (обрезанная до max_length)
        или None для пустых/нестроковых значений
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


class AttributeMapper:
    """
    Стратегия обработки заданий attribute_mapping.

    Жизненный цикл: один экземпляр на всё приложение (создаётся
    в MappingJobWorker), промпт читается с диска один раз
    в конструкторе. Отсутствие файла промпта — ошибка запуска
    (fail fast): воркер агента не должен стартовать без промпта.
    """

    def __init__(self, ai_comparator: "AIComparator") -> None:
        """
        Args:
            ai_comparator: Общий AIComparator приложения (DI).
                НЕ создаёт собственный экземпляр — семафор компаратора
                глобально ограничивает AI-запросы всех задач.

        Raises:
            FileNotFoundError: если prompts/attribute_mapping.txt отсутствует
        """
        self._comparator = ai_comparator
        with open(_ATTRIBUTE_PROMPT_PATH, "r", encoding="utf-8") as f:
            self._prompt_template = f.read()
        logger.info("Промпт маппинга атрибутов загружен: %s", _ATTRIBUTE_PROMPT_PATH)

    async def map_attributes(self, task: AttributeMappingTask) -> AttributeMappingResult:
        """
        Выполняет маппинг атрибутов задания одним AI-запросом.

        Args:
            task: Валидированное задание attribute_mapping

        Returns:
            Готовый результат: results[] + unresolved[].
            Все ID результата гарантированно существуют во входных данных.

        Raises:
            Exception: ошибки AI-запроса поднимаются наверх — решение
                о статусе задания (failed) принимает MappingJobWorker
        """
        logger.info(
            "Маппинг атрибутов: категория='%s' (schemaId=%s), атрибутов=%d, каналов=%d",
            task.category.name,
            task.schema_id,
            len(task.category.attributes),
            len(task.channels),
        )

        prompt = self._build_prompt(task)
        response = await self._comparator.call_ai_json(
            prompt,
            model=self._model_override(),
            temperature=Config.AGENT_AI_TEMPERATURE,
        )
        result = self._validate_and_build(task, response)

        logger.info(
            "Маппинг атрибутов завершён: сопоставлено=%d, unresolved=%d",
            len(result.results),
            len(result.unresolved),
        )
        return result

    # ===================================================================
    # Подготовка промпта
    # ===================================================================

    def _build_prompt(self, task: AttributeMappingTask) -> str:
        """
        Форматирует промпт из prompts/attribute_mapping.txt.

        Контракт промпта (шаг 5): плейсхолдеры category_name,
        category_path, attributes_json, channels_json.
        """
        return self._prompt_template.format(
            category_name=task.category.name,
            category_path=task.category.path or "не указан",
            attributes_json=self._attributes_to_json(task.category.attributes),
            channels_json=self._channels_to_json(task.channels),
        )

    @staticmethod
    def _attributes_to_json(attributes: List[CategoryAttribute]) -> str:
        """
        Сериализует атрибуты категории для промпта.

        Только поля, значимые для сопоставления; пустые
        необязательные поля опускаются для компактности промпта.
        """
        items: List[Dict[str, Any]] = []
        for attr in attributes:
            item: Dict[str, Any] = {
                "mappingId": attr.mapping_id,
                "name": attr.name,
                "slug": attr.slug,
                "kind": attr.kind,
            }
            if attr.group_name:
                item["group"] = attr.group_name
            items.append(item)
        return json.dumps(items, ensure_ascii=False, indent=2)

    @staticmethod
    def _channels_to_json(channels: List[ChannelInfo]) -> str:
        """
        Сериализует каналы с атрибутами для промпта.

        unit и description включаются только при наличии —
        они опциональны по протоколу и часто отсутствуют.
        """
        channel_items: List[Dict[str, Any]] = []
        for channel in channels:
            attr_items: List[Dict[str, Any]] = []
            for attr in channel.attributes:
                attr_item: Dict[str, Any] = {
                    "channelAttributeId": attr.channel_attribute_id,
                    "name": attr.name,
                    "kind": attr.kind,
                    "required": attr.required,
                }
                if attr.unit:
                    attr_item["unit"] = attr.unit
                if attr.description:
                    attr_item["description"] = attr.description
                attr_items.append(attr_item)

            channel_items.append({
                "schemaChannelId": channel.schema_channel_id,
                "platform": channel.platform,
                "name": channel.name,
                "attributes": attr_items,
            })
        return json.dumps(channel_items, ensure_ascii=False, indent=2)

    @staticmethod
    def _model_override() -> Optional[str]:
        """
        Возвращает модель для маппинга или None (модель компаратора по умолчанию).

        AGENT_AI_MODEL="" → используем общую AI_MODEL приложения.
        """
        model = Config.AGENT_AI_MODEL.strip()
        return model if model else None

    # ===================================================================
    # Пост-валидация ответа LLM
    # ===================================================================

    def _validate_and_build(
        self,
        task: AttributeMappingTask,
        response: Dict,
    ) -> AttributeMappingResult:
        """
        Строит результат из сырого ответа LLM с проверкой каждой ссылки.

        Правила (зеркалят правила промпта, но НЕ доверяют им):
            - mappingId обязан существовать во входных атрибутах категории,
              валидный mappingId учитывается один раз (дубликаты отбрасываются)
            - schemaChannelId и channelAttributeId обязаны существовать
              во входных данных, причём атрибут — в ЭТОМ канале
            - channelAttributeId канала используется один раз по всему
              результату (атрибут канала принадлежит одной связке)
            - элемент matches без валидных channelMatches не считается
              решённым — его mappingId попадает в unresolved
            - infomodelAttributeId берётся ИЗ ВХОДНЫХ данных по mappingId
              (LLM не возвращает его вовсе)
            - список unresolved вычисляется детерминированно:
              все входные mappingId, не вошедшие в валидные results

        Args:
            task: Входное задание (источник истины для всех ID)
            response: Распарсенный JSON ответа LLM

        Returns:
            AttributeMappingResult с гарантией валидности всех ID
        """
        # Индексы входных данных — источник истины
        attributes_by_mapping_id: Dict[int, CategoryAttribute] = {
            attr.mapping_id: attr for attr in task.category.attributes
        }
        channel_attribute_ids: Dict[int, set] = {
            channel.schema_channel_id: {
                attr.channel_attribute_id for attr in channel.attributes
            }
            for channel in task.channels
        }

        # Признак валидности канала (schemaChannelId из входных данных)
        known_channel_ids = set(channel_attribute_ids.keys())

        # Занятые атрибуты каналов (уникальность по результату)
        used_channel_attributes: Dict[int, set] = {
            channel_id: set() for channel_id in known_channel_ids
        }

        results: List[MatchedBundle] = []
        resolved_mapping_ids: set = set()
        rejected_count = 0

        raw_matches = response.get("matches", []) if isinstance(response, dict) else []
        if not isinstance(raw_matches, list):
            logger.warning("Ответ LLM: поле 'matches' не является массивом — игнорируется")
            raw_matches = []

        for item in raw_matches:
            if not isinstance(item, dict):
                rejected_count += 1
                continue

            mapping_id = item.get("mappingId")
            if isinstance(mapping_id, bool) or not isinstance(mapping_id, int):
                rejected_count += 1
                continue

            source_attribute = attributes_by_mapping_id.get(mapping_id)
            if source_attribute is None:
                logger.warning(
                    "Ответ LLM: mappingId=%s отсутствует во входных данных — отброшен",
                    mapping_id,
                )
                rejected_count += 1
                continue

            if mapping_id in resolved_mapping_ids:
                logger.warning(
                    "Ответ LLM: дубликат mappingId=%s — отброшен", mapping_id
                )
                rejected_count += 1
                continue

            # --- Валидация канальных соответствий ---
            channel_matches: List[ChannelMatch] = []

            raw_channel_matches = item.get("channelMatches", [])
            if not isinstance(raw_channel_matches, list):
                raw_channel_matches = []

            for raw_match in raw_channel_matches:
                match = self._validate_channel_match(
                    raw_match,
                    known_channel_ids,
                    channel_attribute_ids,
                    used_channel_attributes,
                )
                if match is None:
                    rejected_count += 1
                    continue
                channel_matches.append(match)

            # Нет ни одного валидного канала — связка не решена
            if not channel_matches:
                logger.info(
                    "Ответ LLM: mappingId=%s без валидных channelMatches — unresolved",
                    mapping_id,
                )
                continue

            resolved_mapping_ids.add(mapping_id)

            # Общая уверенность: ответ LLM, иначе максимум по каналам
            bundle_confidence = sanitize_confidence(item.get("confidence"))
            if bundle_confidence is None:
                bundle_confidence = max(
                    (cm.confidence if cm.confidence is not None else 0.0)
                    for cm in channel_matches
                )

            results.append(MatchedBundle(
                mapping_id=mapping_id,
                infomodel_attribute_id=source_attribute.infomodel_attribute_id,
                confidence=bundle_confidence,
                comment=truncate_comment(item.get("comment")),
                channel_matches=channel_matches,
            ))

        # unresolved — детерминированно из входных данных, в их порядке
        unresolved: List[int] = [
            attr.mapping_id
            for attr in task.category.attributes
            if attr.mapping_id not in resolved_mapping_ids
        ]

        if rejected_count > 0:
            logger.warning(
                "Пост-валидация ответа LLM: отброшено некорректных элементов=%d",
                rejected_count,
            )

        return AttributeMappingResult(results=results, unresolved=unresolved)

    def _validate_channel_match(
        self,
        raw_match: Any,
        known_channel_ids: set,
        channel_attribute_ids: Dict[int, set],
        used_channel_attributes: Dict[int, set],
    ) -> Optional[ChannelMatch]:
        """
        Валидирует один элемент channelMatches[] из ответа LLM.

        Args:
            raw_match: Сырой элемент ответа
            known_channel_ids: Множество schemaChannelId из входных данных
            channel_attribute_ids: {schemaChannelId: {channelAttributeId}}
            used_channel_attributes: {schemaChannelId: занятые channelAttributeId}

        Returns:
            Валидный ChannelMatch или None (с логированием причины)
        """
        if not isinstance(raw_match, dict):
            return None

        schema_channel_id = raw_match.get("schemaChannelId")
        if isinstance(schema_channel_id, bool) or not isinstance(schema_channel_id, int):
            logger.warning(
                "Ответ LLM: schemaChannelId=%r не является целым числом — отброшен",
                schema_channel_id,
            )
            return None

        if schema_channel_id not in known_channel_ids:
            logger.warning(
                "Ответ LLM: schemaChannelId=%s отсутствует во входных данных — отброшен",
                schema_channel_id,
            )
            return None

        channel_attribute_id = raw_match.get("channelAttributeId")
        if isinstance(channel_attribute_id, bool) or not isinstance(
            channel_attribute_id, int
        ):
            logger.warning(
                "Ответ LLM: channelAttributeId=%r не является целым числом — отброшен",
                channel_attribute_id,
            )
            return None

        if channel_attribute_id not in channel_attribute_ids[schema_channel_id]:
            logger.warning(
                "Ответ LLM: channelAttributeId=%s отсутствует в канале %s — отброшен",
                channel_attribute_id,
                schema_channel_id,
            )
            return None

        if channel_attribute_id in used_channel_attributes[schema_channel_id]:
            logger.warning(
                "Ответ LLM: channelAttributeId=%s канала %s уже занят другой связкой — "
                "дубликат отброшен",
                channel_attribute_id,
                schema_channel_id,
            )
            return None

        used_channel_attributes[schema_channel_id].add(channel_attribute_id)

        return ChannelMatch(
            schema_channel_id=schema_channel_id,
            channel_attribute_id=channel_attribute_id,
            confidence=sanitize_confidence(raw_match.get("confidence")),
        )
