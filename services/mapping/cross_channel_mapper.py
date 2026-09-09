"""
Стратегия межканального маппинга атрибутов (этап 2 задачи 1, v6.2).

Сопоставляет между собой атрибуты каналов (маркетплейсов), которые
НЕ вошли ни в одну связку results первого этапа: находит семантически
эквивалентные атрибуты разных каналов (например, атрибут Ozon ↔
атрибут WB), не имеющие аналога в категории каталога.

Жизненный цикл вызова:
    1. Быстрые пути без AI-запроса: каналов с остатками нет или
       канал один — сопоставлять не с чем, возвращается пустой
       результат.
    2. Подготовка компактного JSON остаточных атрибутов и
       форматирование промпта prompts/cross_channel_attribute_mapping.txt.
    3. Один запрос к LLM через ОБЩИЙ AIComparator приложения —
       семафор компаратора ограничивает суммарную нагрузку на
       LLM-провайдера (общую для синхронизации и маппинга).
    4. Пост-валидация: каждый идентификатор ответа сверяется со
       входными данными, галлюцинации LLM отбрасываются. Группой
       считается связка атрибутов минимум из ДВУХ РАЗНЫХ каналов —
       одиночный атрибут межканальной связкой не является.

Паттерн: Strategy — третий алгоритм маппинга наряду с
AttributeMapper и ReferenceValueMapper; вызывается AttributeMapper'ом
как этап 2 внутри задания attribute_mapping.
Паттерн: Dependency Injection — AIComparator инжектируется извне
(общий экземпляр приложения, НЕ создаётся здесь).
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from config.config import Config
from services.mapping.attribute_mapper import (
    sanitize_confidence,
    truncate_comment,
)
from services.mapping.models import (
    ChannelInfo,
    ChannelMatch,
    CrossChannelMatch,
    CrossChannelMappingResult,
)
from utils.logger_config import setup_logger

if TYPE_CHECKING:
    from services.ai_comparator import AIComparator

logger = setup_logger("mapping.cross_channel_mapper")

# Путь к промпту (prompts/ на уровне корня проекта)
_CROSS_CHANNEL_PROMPT_PATH = (
    Path(__file__).parent.parent.parent
    / "prompts"
    / "cross_channel_attribute_mapping.txt"
)

# Минимальное число РАЗНЫХ каналов в межканальной группе:
# одиночный атрибут группой не является (раздел 4 протокола v6.2)
_MIN_CHANNELS_IN_GROUP: int = 2


class CrossChannelMapper:
    """
    Стратегия межканального маппинга остаточных атрибутов каналов.

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
            FileNotFoundError: если prompts/cross_channel_attribute_mapping.txt
                отсутствует (fail fast при старте воркера)
        """
        self._comparator = ai_comparator
        with open(_CROSS_CHANNEL_PROMPT_PATH, "r", encoding="utf-8") as f:
            self._prompt_template = f.read()
        logger.info(
            "Промпт межканального маппинга загружен: %s",
            _CROSS_CHANNEL_PROMPT_PATH,
        )

    async def map_cross_channel(
        self,
        channels_with_remaining: List[ChannelInfo],
        category_name: str = "",
        category_path: Optional[str] = None,
    ) -> CrossChannelMappingResult:
        """
        Выполняет маппинг остаточных атрибутов каналов одним AI-запросом.

        Args:
            channels_with_remaining: Список ChannelInfo, у которых в поле
                attributes — ТОЛЬКО остаточные атрибуты (не вошедшие
                в results первого этапа). Каналы без остатков не передаются.
            category_name: Название категории каталога — контекст для LLM
                (подставляется в промпт, помогает ей понимать предметную
                область при группировке).
            category_path: Путь категории каталога или None.

        Returns:
            CrossChannelMappingResult с гарантией валидности всех ID.
            Если сопоставлять не с кем (каналов меньше двух) —
            пустой результат БЕЗ AI-запроса.

        Raises:
            Exception: ошибки AI-запроса поднимаются наверх — решение
                о статусе задания (failed) принимает MappingJobWorker
        """
        # --- Быстрые пути: каналы без остатков не дают материала ---
        active_channels = [
            channel for channel in channels_with_remaining
            if channel.attributes
        ]
        if len(active_channels) < _MIN_CHANNELS_IN_GROUP:
            logger.info(
                "Межканальный маппинг пропущен: каналов с остаточными "
                "атрибутами=%d (минимум %d) — AI-запрос не выполнялся",
                len(active_channels),
                _MIN_CHANNELS_IN_GROUP,
            )
            return CrossChannelMappingResult(matches=[])

        total_attributes = sum(
            len(channel.attributes) for channel in active_channels
        )
        logger.info(
            "Межканальный маппинг: каналов=%d, остаточных атрибутов=%d "
            "(категория='%s')",
            len(active_channels),
            total_attributes,
            category_name,
        )

        prompt = self._build_prompt(active_channels, category_name, category_path)
        response = await self._comparator.call_ai_json(
            prompt,
            model=self._model_override(),
            temperature=Config.AGENT_AI_TEMPERATURE,
        )
        result = self._validate_and_build(active_channels, response)

        logger.info(
            "Межканальный маппинг завершён: групп=%d",
            len(result.matches),
        )
        return result

    # ===================================================================
    # Подготовка промпта
    # ===================================================================

    def _build_prompt(
        self,
        channels: List[ChannelInfo],
        category_name: str,
        category_path: Optional[str],
    ) -> str:
        """
        Форматирует промпт из prompts/cross_channel_attribute_mapping.txt.

        Контракт промпта: плейсхолдеры category_name, category_path,
        channels_json.
        """
        return self._prompt_template.format(
            category_name=category_name,
            category_path=category_path or "не указан",
            channels_json=self._channels_to_json(channels),
        )

    @staticmethod
    def _channels_to_json(channels: List[ChannelInfo]) -> str:
        """
        Сериализует остаточные атрибуты каналов для промпта.

        Схема полей идентична AttributeMapper._channels_to_json —
        намеренно продублирована: приватный метод чужой стратегии
        не переиспользуется извне (инкапсуляция), а вынос в общий
        модуль оправдан только при третьем потребителе.
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
        Возвращает модель для маппинга или None (модель компаратора).

        AGENT_AI_MODEL="" → используем общую AI_MODEL приложения —
        та же логика, что у AttributeMapper.
        """
        model = Config.AGENT_AI_MODEL.strip()
        return model if model else None

    # ===================================================================
    # Пост-валидация ответа LLM
    # ===================================================================

    def _validate_and_build(
        self,
        channels: List[ChannelInfo],
        response: Dict,
    ) -> CrossChannelMappingResult:
        """
        Строит результат из сырого ответа LLM с проверкой каждой ссылки.

        Правила (зеркалят правила промпта, но НЕ доверяют им):
            - schemaChannelId обязан существовать в переданных каналах;
            - channelAttributeId обязан существовать в атрибутах
              ЭТОГО канала;
            - каждый channelAttributeId используется не более одного
              раза по всему результату (атрибут принадлежит одной
              группе);
            - группа валидна только при минимум ДВУХ РАЗНЫХ каналах —
              одиночный атрибут и однотипная (в пределах одного
              канала) подборка группой не являются.

        Отличие от этапа 1: фиксация занятых атрибутов выполняется
        ПОСЛЕ принятия группы. Элементы отклонённой группы не
        блокируют атрибуты для следующих групп LLM — иначе одна
        неполная группа поглотила бы валидные связи другой.

        Args:
            channels: Каналы с остаточными атрибутами
                (источник истины для всех ID)
            response: Распарсенный JSON ответа LLM

        Returns:
            CrossChannelMappingResult с гарантией валидности всех ID
        """
        # Индекс входных данных — источник истины
        channel_attribute_ids: Dict[int, set] = {
            channel.schema_channel_id: {
                attr.channel_attribute_id for attr in channel.attributes
            }
            for channel in channels
        }

        # Занятые атрибуты: (schemaChannelId, channelAttributeId)
        used_keys: Set[Tuple[int, int]] = set()

        matches: List[CrossChannelMatch] = []
        rejected_count = 0

        raw_groups = response.get("groups", []) if isinstance(response, dict) else []
        if not isinstance(raw_groups, list):
            logger.warning("Ответ LLM: поле 'groups' не является массивом — игнорируется")
            raw_groups = []

        for item in raw_groups:
            if not isinstance(item, dict):
                rejected_count += 1
                continue

            # --- Валидация элементов группы (без фиксации занятости) ---
            channel_matches: List[ChannelMatch] = []
            seen_in_group: Set[Tuple[int, int]] = set()

            raw_channel_matches = item.get("channelMatches", [])
            if not isinstance(raw_channel_matches, list):
                raw_channel_matches = []

            for raw_match in raw_channel_matches:
                match = self._validate_channel_match(raw_match, channel_attribute_ids)
                if match is None:
                    rejected_count += 1
                    continue

                key = (match.schema_channel_id, match.channel_attribute_id)
                if key in used_keys or key in seen_in_group:
                    logger.warning(
                        "Ответ LLM: channelAttributeId=%s канала %s уже "
                        "использован в другой группе или продублирован — отброшен",
                        match.channel_attribute_id,
                        match.schema_channel_id,
                    )
                    rejected_count += 1
                    continue

                seen_in_group.add(key)
                channel_matches.append(match)

            # --- Группа обязана охватывать минимум два разных канала ---
            distinct_channels = {cm.schema_channel_id for cm in channel_matches}
            if len(distinct_channels) < _MIN_CHANNELS_IN_GROUP:
                logger.info(
                    "Ответ LLM: группа отклонена — валидных элементов=%d из "
                    "разных каналов=%d (минимум %d); атрибуты освобождены "
                    "для других групп",
                    len(channel_matches),
                    len(distinct_channels),
                    _MIN_CHANNELS_IN_GROUP,
                )
                rejected_count += 1
                continue

            # Группа принята — только теперь фиксируем занятость
            used_keys.update(seen_in_group)

            # Общая уверенность: ответ LLM, иначе максимум по каналам
            group_confidence = sanitize_confidence(item.get("confidence"))
            if group_confidence is None:
                group_confidence = max(
                    (cm.confidence if cm.confidence is not None else 0.0)
                    for cm in channel_matches
                )

            matches.append(CrossChannelMatch(
                confidence=group_confidence,
                comment=truncate_comment(item.get("comment")),
                channel_matches=channel_matches,
            ))

        if rejected_count > 0:
            logger.warning(
                "Пост-валидация ответа LLM: отброшено некорректных "
                "элементов и групп=%d",
                rejected_count,
            )

        return CrossChannelMappingResult(matches=matches)

    @staticmethod
    def _validate_channel_match(
        raw_match: Any,
        channel_attribute_ids: Dict[int, set],
    ) -> Optional[ChannelMatch]:
        """
        Валидирует один элемент channelMatches[] из ответа LLM.

        Проверяет только существование ID во входных данных —
        уникальность проверяется вызывающим кодом (двухфазная схема).

        Args:
            raw_match: Сырой элемент ответа
            channel_attribute_ids: {schemaChannelId: {channelAttributeId}}

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

        if schema_channel_id not in channel_attribute_ids:
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

        return ChannelMatch(
            schema_channel_id=schema_channel_id,
            channel_attribute_id=channel_attribute_id,
            confidence=sanitize_confidence(raw_match.get("confidence")),
        )
