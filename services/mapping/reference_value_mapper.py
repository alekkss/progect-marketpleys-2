"""
Стратегия маппинга справочных значений (задача 2 протокола PIM+FDM).

Сопоставляет значения справочника атрибута категории со значениями
справочников каналов в контексте ОДНОЙ связки атрибута. Работает
одним AI-запросом по всем каналам сразу.

Обработка ответа LLM в три этапа:

    1. Подготовка компактного JSON входных данных и форматирование
       промпта prompts/reference_value_mapping.txt.
    2. Один запрос к LLM через ОБЩИЙ AIComparator приложения —
       семафор компаратора ограничивает суммарную нагрузку на
       LLM-провайдера (общую для синхронизации и маппинга).
    3. Пост-валидация: каждая пара (channelValueId, channelValue)
       сверяется со справочником канала — они обязаны ссылаться на
       ОДНО И ТО ЖЕ значение из входных данных (channelValue по
       протоколу — авторитетная строка для сохранения в FDM).
       Полнота результата гарантируется принудительно: для каждого
       значения категории в каждом канале создаётся ровно одна
       запись, отсутствующим в валидном ответе LLM соответствуют
       детерминированные null-записи.

Особый случай: пустой справочник канала (referenceValues=[]) —
все значения категории получают null (валидно по протоколу),
AI-запрос не требуется для такого канала.

Паттерн: Strategy — вторая из двух взаимозаменяемых стратегий
обработки заданий (первая — attribute_mapper).
Паттерн: Dependency Injection — AIComparator инжектируется извне
(общий экземпляр приложения, НЕ создаётся здесь).
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from config.config import Config
from services.mapping.attribute_mapper import sanitize_confidence
from services.mapping.models import (
    ChannelReferenceValue,
    ChannelValueMappingResult,
    ReferenceChannel,
    ReferenceValueMappingResult,
    ReferenceValueMappingTask,
    ValueMatch,
)
from utils.logger_config import setup_logger

if TYPE_CHECKING:
    from services.ai_comparator import AIComparator

logger = setup_logger("mapping.reference_value_mapper")

# Путь к промпту (prompts/ на уровне корня проекта)
_REFERENCE_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "prompts" / "reference_value_mapping.txt"
)


class ReferenceValueMapper:
    """
    Стратегия обработки заданий reference_value_mapping.

    Жизненный цикл: один экземпляр на всё приложение (создаётся
    в MappingJobWorker), промпт читается с диска один раз.
    """

    def __init__(self, ai_comparator: "AIComparator") -> None:
        """
        Args:
            ai_comparator: Общий AIComparator приложения (DI).
                НЕ создаёт собственный экземпляр.

        Raises:
            FileNotFoundError: если prompts/reference_value_mapping.txt
                отсутствует
        """
        self._comparator = ai_comparator
        with open(_REFERENCE_PROMPT_PATH, "r", encoding="utf-8") as f:
            self._prompt_template = f.read()
        logger.info("Промпт маппинга значений загружен: %s", _REFERENCE_PROMPT_PATH)

    async def map_values(self, task: ReferenceValueMappingTask) -> ReferenceValueMappingResult:
        """
        Выполняет маппинг значений задания одним AI-запросом.

        Args:
            task: Валидированное задание reference_value_mapping

        Returns:
            Готовый результат: channels[] с matches[]. Для каждого
            значения категории в каждом канале — ровно одна запись;
            все не-null значения гарантированно существуют в
            справочниках каналов из входных данных.

        Raises:
            Exception: ошибки AI-запроса поднимаются наверх — решение
                о статусе задания (failed) принимает MappingJobWorker
        """
        logger.info(
            "Маппинг значений: атрибут='%s' (mappingId=%s), значений=%d, каналов=%d",
            task.attribute.name,
            task.mapping_id,
            len(task.attribute.reference_values),
            len(task.channels),
        )

        # Быстрый путь: все каналы с пустыми справочниками — AI не нужен
        if all(not channel.reference_values for channel in task.channels):
            logger.info("Все справочники каналов пусты — AI-запрос не требуется")
            return self._build_null_result(task)

        prompt = self._build_prompt(task)
        response = await self._comparator.call_ai_json(
            prompt,
            model=self._model_override(),
            temperature=Config.AGENT_AI_TEMPERATURE,
        )
        result = self._validate_and_build(task, response)

        matched_total = sum(
            1
            for channel_result in result.channels
            for match in channel_result.matches
            if match.channel_value is not None
        )
        total_records = sum(len(c.matches) for c in result.channels)
        logger.info(
            "Маппинг значений завершён: записей=%d, найдено соответствий=%d",
            total_records,
            matched_total,
        )
        return result

    # ===================================================================
    # Подготовка промпта
    # ===================================================================

    def _build_prompt(self, task: ReferenceValueMappingTask) -> str:
        """
        Форматирует промпт из prompts/reference_value_mapping.txt.

        Контракт промпта (шаг 6): плейсхолдеры attribute_name,
        attribute_slug, reference_type, category_values_json,
        channels_json.
        """
        return self._prompt_template.format(
            attribute_name=task.attribute.name,
            attribute_slug=task.attribute.slug,
            reference_type=task.attribute.reference_type,
            category_values_json=json.dumps(
                task.attribute.reference_values, ensure_ascii=False, indent=2
            ),
            channels_json=self._channels_to_json(task.channels),
        )

    @staticmethod
    def _channels_to_json(channels: List[ReferenceChannel]) -> str:
        """
        Сериализует каналы со справочниками для промпта.

        Каналы с пустыми справочниками включаются — промпт и пост-валидация
        рассчитаны на их присутствие (LLM вернёт null-записи, а валидатор
        гарантирует null независимо от ответа).
        """
        channel_items: List[Dict[str, Any]] = []
        for channel in channels:
            channel_items.append({
                "schemaChannelId": channel.schema_channel_id,
                "platform": channel.platform,
                "name": channel.name,
                "referenceValues": [
                    {"id": value.id, "value": value.value}
                    for value in channel.reference_values
                ],
            })
        return json.dumps(channel_items, ensure_ascii=False, indent=2)

    @staticmethod
    def _model_override() -> Optional[str]:
        """
        Возвращает модель для маппинга или None (модель компаратора по умолчанию).
        """
        model = Config.AGENT_AI_MODEL.strip()
        return model if model else None

    # ===================================================================
    # Пост-валидация ответа LLM
    # ===================================================================

    def _validate_and_build(
        self,
        task: ReferenceValueMappingTask,
        response: Dict,
    ) -> ReferenceValueMappingResult:
        """
        Строит результат из сырого ответа LLM с гарантией полноты.

        Правила (зеркалят правила промпта, но НЕ доверяют им):
            - рассматриваются только каналы со schemaChannelId из
              входных данных; посторонние каналы ответа игнорируются
            - infoValue обязан существовать в справочнике категории;
              на каждое значение — максимум одна запись на канал
            - пара (channelValueId, channelValue) обязана ссылаться
              на одно и то же значение справочника канала: любое
              несовпадение → null (лучше пустое соответствие, чем
              ошибочная привязка — строка сохраняется в FDM как есть)
            - channelValue="..." без channelValueId (или наоборот)
              не принимается: половинчатая ссылка невалидна
            - все значения категории, не покрытые валидными записями,
              получают детерминированные null-записи — итоговая
              полнота гарантируется кодом, а не LLM

        Args:
            task: Входное задание (источник истины для всех ссылок)
            response: Распарсенный JSON ответа LLM

        Returns:
            ReferenceValueMappingResult с каналами в порядке входных данных
        """
        # Индексы входных данных — источник истины
        values_by_id: Dict[int, ChannelReferenceValue] = {}
        channel_values: Dict[int, Dict[str, ChannelReferenceValue]] = {}
        for channel in task.channels:
            lookup: Dict[str, ChannelReferenceValue] = {}
            for value in channel.reference_values:
                values_by_id[value.id] = value
                lookup[value.value] = value
            channel_values[channel.schema_channel_id] = lookup

        category_values = task.attribute.reference_values
        category_values_set = set(category_values)

        # match_key → ValueMatch по каждому каналу;
        # ключ — infoValue (значения категории уникальны по валидатору входа
        # не формально, но дубликаты в справочнике категории на практике
        # исключены; при дубликате обе записи получают одинаковый результат)
        accepted: Dict[int, Dict[str, ValueMatch]] = {
            channel.schema_channel_id: {} for channel in task.channels
        }
        rejected_count = 0

        raw_channels = response.get("channels", []) if isinstance(response, dict) else []
        if not isinstance(raw_channels, list):
            logger.warning("Ответ LLM: поле 'channels' не является массивом — игнорируется")
            raw_channels = []

        for raw_channel in raw_channels:
            if not isinstance(raw_channel, dict):
                rejected_count += 1
                continue

            schema_channel_id = raw_channel.get("schemaChannelId")
            if (
                isinstance(schema_channel_id, bool)
                or not isinstance(schema_channel_id, int)
                or schema_channel_id not in accepted
            ):
                logger.warning(
                    "Ответ LLM: schemaChannelId=%r отсутствует во входных данных — "
                    "канал проигнорирован",
                    schema_channel_id,
                )
                rejected_count += 1
                continue

            raw_matches = raw_channel.get("matches", [])
            if not isinstance(raw_matches, list):
                raw_matches = []

            channel_accepted = accepted[schema_channel_id]

            for raw_match in raw_matches:
                match = self._validate_single_match(
                    raw_match,
                    channel_values[schema_channel_id],
                    category_values_set,
                )
                if match is None:
                    rejected_count += 1
                    continue
                # Первый валидный вариант побеждает, дубликаты отбрасываются
                if match.info_value not in channel_accepted:
                    channel_accepted[match.info_value] = match
                else:
                    rejected_count += 1

        # Сборка результата: каналы в порядке входных данных,
        # полнота matches обеспечивается null-записями
        channels_result: List[ChannelValueMappingResult] = []
        for channel in task.channels:
            channel_accepted = accepted[channel.schema_channel_id]
            matches: List[ValueMatch] = []
            for value in category_values:
                matches.append(
                    channel_accepted.get(value, ValueMatch(info_value=value))
                )
            channels_result.append(ChannelValueMappingResult(
                schema_channel_id=channel.schema_channel_id,
                matches=matches,
            ))

        if rejected_count > 0:
            logger.warning(
                "Пост-валидация ответа LLM: отброшено некорректных записей=%d",
                rejected_count,
            )

        return ReferenceValueMappingResult(channels=channels_result)

    def _validate_single_match(
        self,
        raw_match: Any,
        channel_lookup: Dict[str, ChannelReferenceValue],
        category_values_set: set,
    ) -> Optional[ValueMatch]:
        """
        Валидирует одну запись matches[] из ответа LLM.

        Args:
            raw_match: Сырая запись ответа
            channel_lookup: {value: ChannelReferenceValue} справочника канала
            category_values_set: Множество значений справочника категории

        Returns:
            Валидный ValueMatch (может быть null-соответствием, если LLM
            так решил) или None при некорректной записи
        """
        if not isinstance(raw_match, dict):
            return None

        info_value = raw_match.get("infoValue")
        if not isinstance(info_value, str) or not info_value:
            logger.warning("Ответ LLM: infoValue=%r не является строкой — отброшено", info_value)
            return None

        if info_value not in category_values_set:
            logger.warning(
                "Ответ LLM: infoValue='%s' отсутствует в справочнике категории — отброшено",
                info_value[:50],
            )
            return None

        channel_value = raw_match.get("channelValue")
        channel_value_id = raw_match.get("channelValueId")

        # Явное отсутствие соответствия — валидная null-запись
        if channel_value is None and channel_value_id is None:
            confidence = sanitize_confidence(raw_match.get("confidence"))
            return ValueMatch(
                info_value=info_value,
                channel_value=None,
                channel_value_id=None,
                confidence=confidence,
            )

        # Половинчатая ссылка (только строка или только ID) — невалидна:
        # пара обязана ссылаться на одно значение справочника
        if not isinstance(channel_value, str) or (
            isinstance(channel_value_id, bool) or not isinstance(channel_value_id, int)
        ):
            logger.warning(
                "Ответ LLM: некорректная пара channelValue=%r / channelValueId=%r "
                "для infoValue='%s' — заменено на null",
                channel_value, channel_value_id, info_value[:50],
            )
            return ValueMatch(info_value=info_value)

        # Строка обязана существовать в справочнике канала
        reference_value = channel_lookup.get(channel_value)
        if reference_value is None:
            logger.warning(
                "Ответ LLM: channelValue='%s' отсутствует в справочнике канала "
                "(infoValue='%s') — заменено на null",
                channel_value[:50], info_value[:50],
            )
            return ValueMatch(info_value=info_value)

        # ID обязан ссылаться на ту же запись, что и строка
        if reference_value.id != channel_value_id:
            logger.warning(
                "Ответ LLM: channelValueId=%s не совпадает со значением "
                "channelValue='%s' (ожидается id=%s) — заменено на null",
                channel_value_id, channel_value[:50], reference_value.id,
            )
            return ValueMatch(info_value=info_value)

        confidence = sanitize_confidence(raw_match.get("confidence"))
        if confidence is None:
            # Точная ссылка на значение справочника — высокое доверие
            confidence = 0.9

        return ValueMatch(
            info_value=info_value,
            channel_value=channel_value,
            channel_value_id=reference_value.id,
            confidence=confidence,
        )

    @staticmethod
    def _build_null_result(task: ReferenceValueMappingTask) -> ReferenceValueMappingResult:
        """
        Строит результат из null-записей для всех каналов.

        Используется, когда все справочники каналов пусты —
        AI-запрос не нужен: соответствий не существует по определению.
        """
        channels_result = [
            ChannelValueMappingResult(
                schema_channel_id=channel.schema_channel_id,
                matches=[
                    ValueMatch(info_value=value) for value in task.attribute.reference_values
                ],
            )
            for channel in task.channels
        ]
        return ReferenceValueMappingResult(channels=channels_result)
