# Доработка v6.2 — Межканальный маппинг атрибутов (Cross-Channel)

## Содержание

- [1. Контекст и проблема](#1-контекст-и-проблема)
- [2. Решение](#2-решение)
- [3. Архитектура изменений](#3-архитектура-изменений)
- [4. Изменения протокола HTTP](#4-изменения-протокола-http)
- [5. Новые модели данных](#5-новые-модели-данных)
- [6. Новый маппер: CrossChannelMapper](#6-новый-маппер-crosschannelmapper)
- [7. Изменения AttributeMapper](#7-изменения-attributemapper)
- [8. Изменения MappingJobWorker](#8-изменения-mappingjobworker)
- [9. Изменения v1_api.py](#9-изменения-v1_apipy)
- [10. Новый промпт](#10-новый-промпт)
- [11. Дашборд /agent — изменения](#11-дашборд-agent--изменения)
- [12. Счётчики в БД](#12-счётчики-в-бд)
- [13. Список затронутых файлов](#13-список-затронутых-файлов)
- [14. Сценарий работы end-to-end](#14-сценарий-работы-end-to-end)
- [15. Обратная совместимость](#15-обратная-совместимость)

---

## 1. Контекст и проблема

### Как работает агент сейчас (v6.1)

Задание `attribute_mapping` сопоставляет атрибуты конечной категории каталога
с атрибутами категорий каналов (маркетплейсов). Итоговый ответ содержит два блока:

- **`results`** — список успешных связок: атрибут категории ↔ атрибуты каналов
- **`unresolved`** — список `mappingId` связок, для которых соответствие не найдено
  ни в одном канале

При этом у каналов могут быть атрибуты, которые в `results` не попали — то есть ни
одна связка категории с ними не сопоставилась. Например, у Ozon есть атрибут
«Особенности модели», а в категории каталога такого атрибута нет вовсе.
**Сейчас эти атрибуты каналов просто игнорируются.**

### Требование заказчика

> «У нас ещё может быть такое, что в категории каталога пока-что нет какого-то
> атрибута, который соответствует атрибутам каналов. Нужно чтобы агент помимо
> соответствия атрибутов каналов к категории каталога, ещё связал и вернул
> атрибуты каналов между собой — не соответствующие категории каталога,
> а мы допишем функцию, чтобы у связок только по каналам потом автоматом
> атрибут общий присваивался к категории, копированием атрибута канала либо
> вручную, либо автоматом.»

> «То есть сначала агент маппит всё что относится к категории каталога, как
> сейчас есть, а следующим этапом маппит всё что осталось по каналам между собой.»

### Что нужно сделать

После основного маппинга (этап 1, без изменений) запустить второй этап:
взять атрибуты каналов, которые **не вошли ни в одну связку** `results`,
и найти соответствия **между ними** (например, атрибут Ozon ↔ атрибут WB
↔ атрибут Яндекс.Маркет). Вернуть эти межканальные связки в ответе отдельным
блоком **`crossChannelMatches`**.

---

## 2. Решение

### Принцип работы

Два последовательных этапа в рамках одного задания `attribute_mapping`:

```
Этап 1 (существующий, без изменений):
  Атрибуты категории ↔ Атрибуты каналов
  → results[] + unresolved[]

Этап 2 (новый):
  Атрибуты каналов, НЕ вошедшие в results[]
  → связи между собой
  → crossChannelMatches[]
```

Если после первого этапа все атрибуты каналов распределены по связкам — второй
этап пропускается (нет входных данных), AI-запрос не выполняется.

### Активация

Второй этап активируется **автоматически** для каждого задания
`attribute_mapping`. Никакого нового флага в payload не требуется.

Если остаточных атрибутов каналов нет — поле `crossChannelMatches` в ответе
содержит пустой массив `[]`. FDM получает стабильную структуру в любом случае.

---

## 3. Архитектура изменений

```
services/mapping/
│
├── models.py                      ← ИЗМЕНЁН: 3 новых dataclass
│
├── attribute_mapper.py            ← ИЗМЕНЁН: сбор остаточных атрибутов,
│                                     вызов CrossChannelMapper, объединение
│
├── cross_channel_mapper.py        ← НОВЫЙ: Strategy второго этапа
│
├── job_worker.py                  ← ИЗМЕНЁН: регистрация нового маппера,
│                                     обновление счётчиков
│
└── validators.py                  ← БЕЗ ИЗМЕНЕНИЙ

web/routes/v1_api.py               ← ИЗМЕНЁН: crossChannelMatches в GET-ответе

prompts/
└── cross_channel_attribute_mapping.txt  ← НОВЫЙ промпт
```

**Паттерны:**

- **Strategy** — `CrossChannelMapper` является третьей стратегией наряду
  с `AttributeMapper` и `ReferenceValueMapper`. Воркер вызывает её через
  общий интерфейс.
- **Dependency Injection** — `CrossChannelMapper` получает тот же общий
  `AIComparator`, что и остальные стратегии. Собственный экземпляр не создаётся.
- **Single Responsibility** — `AttributeMapper` отвечает только за первый этап
  и за подготовку входных данных для второго. Логика второго этапа вынесена
  в отдельный класс.
- **Open/Closed** — существующий `AttributeMapper` расширяется без изменения
  его публичного интерфейса. `MappingJobWorker` получает новый маппер через DI.

---

## 4. Изменения протокола HTTP

### POST /v1/mapping-tasks

**Без изменений.** Структура тела запроса не меняется.

### GET /v1/mapping-tasks/{jobId} — новый блок в ответе

**До (v6.1):**

```json
{
  "jobId": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "status": "completed",
  "results": [
    {
      "mappingId": 1001,
      "infomodelAttributeId": 5001,
      "confidence": 0.95,
      "comment": "Точное совпадение по названию и типу",
      "channelMatches": [
        { "schemaChannelId": 10, "channelAttributeId": 901, "confidence": 0.97 },
        { "schemaChannelId": 20, "channelAttributeId": 204, "confidence": 0.93 }
      ]
    }
  ],
  "unresolved": [1002, 1005]
}
```

**После (v6.2):** добавляется блок `crossChannelMatches`:

```json
{
  "jobId": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "status": "completed",
  "results": [
    {
      "mappingId": 1001,
      "infomodelAttributeId": 5001,
      "confidence": 0.95,
      "comment": "Точное совпадение по названию и типу",
      "channelMatches": [
        { "schemaChannelId": 10, "channelAttributeId": 901, "confidence": 0.97 },
        { "schemaChannelId": 20, "channelAttributeId": 204, "confidence": 0.93 }
      ]
    }
  ],
  "unresolved": [1002, 1005],
  "crossChannelMatches": [
    {
      "confidence": 0.88,
      "comment": "Атрибут присутствует в Ozon и WB, в каталоге аналога нет",
      "channelMatches": [
        { "schemaChannelId": 10, "channelAttributeId": 910, "confidence": 0.91 },
        { "schemaChannelId": 20, "channelAttributeId": 215, "confidence": 0.85 }
      ]
    },
    {
      "confidence": 0.79,
      "comment": null,
      "channelMatches": [
        { "schemaChannelId": 10, "channelAttributeId": 911, "confidence": 0.79 }
      ]
    }
  ]
}
```

### Описание полей `crossChannelMatches[]`

| Поле | Тип | Обязательное | Описание |
|---|---|---|---|
| `confidence` | `float` [0,1] | да | Итоговая уверенность по группе (максимум из `channelMatches`) |
| `comment` | `string \| null` | нет | Пояснение AI для оператора, до 200 символов |
| `channelMatches` | `array` | да | Атрибуты каналов, входящие в группу (минимум 2) |
| `channelMatches[].schemaChannelId` | `int` | да | ID канала из входных данных |
| `channelMatches[].channelAttributeId` | `int` | да | ID атрибута канала из входных данных |
| `channelMatches[].confidence` | `float \| null` | нет | Уверенность по конкретному каналу |

**Ключевые гарантии:**

- Все `schemaChannelId` и `channelAttributeId` в `crossChannelMatches` гарантированно
  присутствуют во входных данных задания (пост-валидация аналогична первому этапу).
- Один и тот же `channelAttributeId` канала встречается не более одного раза
  по всему результату — ни в `results`, ни в `crossChannelMatches`.
- Одиночные атрибуты без пары (нашлись только в одном канале) в ответ не включаются.

---

## 5. Новые модели данных

**Файл:** `services/mapping/models.py`

Добавляются три новых dataclass. Существующие модели не изменяются.

```python
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
    Пустой список — норма (все атрибуты каналов распределены в results).
    """
    matches: List[CrossChannelMatch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "crossChannelMatches": [m.to_dict() for m in self.matches],
        }


@dataclass
class FullAttributeMappingResult:
    """
    Объединённый результат обоих этапов маппинга атрибутов.
    Заменяет AttributeMappingResult как возвращаемый тип map_attributes().
    """
    results: List[MatchedBundle] = field(default_factory=list)
    unresolved: List[int] = field(default_factory=list)
    cross_channel_matches: List[CrossChannelMatch] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "unresolved": list(self.unresolved),
            "crossChannelMatches": [m.to_dict() for m in self.cross_channel_matches],
        }
```

> **Примечание для реализации.** `AttributeMappingResult` остаётся в файле
> для обратной совместимости (используется в тестах и дашборде). Метод
> `map_attributes()` начинает возвращать `FullAttributeMappingResult`.

---

## 6. Новый маппер: CrossChannelMapper

**Файл:** `services/mapping/cross_channel_mapper.py` *(новый)*

### Назначение

Стратегия второго этапа. Принимает список «остаточных» атрибутов каналов
(не вошедших в `results` первого этапа), находит семантически близкие группы
между каналами одним AI-запросом и возвращает `CrossChannelMappingResult`.

### Конструктор

```python
class CrossChannelMapper:
    def __init__(self, ai_comparator: "AIComparator") -> None:
        """
        Raises:
            FileNotFoundError: если prompts/cross_channel_attribute_mapping.txt
                               отсутствует — fail fast при старте воркера
        """
```

Промпт читается с диска один раз в конструкторе. Экземпляр создаётся один раз
в `MappingJobWorker.__init__` наряду с `AttributeMapper` и `ReferenceValueMapper`.

### Публичный метод

```python
async def map_cross_channel(
    self,
    channels_with_remaining: List[ChannelInfo],
) -> CrossChannelMappingResult:
    """
    Выполняет маппинг остаточных атрибутов каналов одним AI-запросом.

    Args:
        channels_with_remaining: Список ChannelInfo, у которых в поле
            attributes — ТОЛЬКО остаточные атрибуты (не вошедшие
            в results первого этапа). Каналы без остатков не передаются.

    Returns:
        CrossChannelMappingResult с гарантией валидности всех ID.
        Если передан список из одного канала (нет с кем сопоставлять) —
        возвращает пустой результат без AI-запроса.
    """
```

### Быстрые пути (без AI-запроса)

Два случая, когда второй этап пропускается немедленно:

1. `channels_with_remaining` пуст — нет остатков вообще.
2. `channels_with_remaining` содержит только один канал — сопоставлять не с чем.

В обоих случаях возвращается `CrossChannelMappingResult(matches=[])`.

### Пост-валидация ответа LLM

Логика аналогична `AttributeMapper._validate_and_build`, но адаптирована
под задачу без категории:

- `schemaChannelId` обязан существовать в переданных `channels_with_remaining`.
- `channelAttributeId` обязан существовать в атрибутах этого канала.
- Один `channelAttributeId` канала — в одной группе (уникальность).
- Группы с менее чем двумя валидными `channelMatches` отбрасываются
  (одиночный атрибут — не межканальная связка).

---

## 7. Изменения AttributeMapper

**Файл:** `services/mapping/attribute_mapper.py`

### Изменение возвращаемого типа

```python
# Было:
async def map_attributes(self, task: AttributeMappingTask) -> AttributeMappingResult

# Стало:
async def map_attributes(self, task: AttributeMappingTask) -> FullAttributeMappingResult
```

### Новая логика в map_attributes

После получения результата первого этапа добавляется шаг сбора остаточных атрибутов
и вызов `CrossChannelMapper`:

```
1. Первый этап (без изменений):
   result_stage1 = _validate_and_build(task, ai_response)
   → results[] + unresolved[]

2. Сбор использованных channelAttributeId:
   used_ids = {schemaChannelId: {channelAttributeId, ...}}
   (собирается из result_stage1.results[].channelMatches[])

3. Построение channels_with_remaining:
   Для каждого канала из task.channels —
   оставляем только атрибуты, чей channelAttributeId
   НЕ входит в used_ids[schemaChannelId]

4. Вызов второго этапа:
   result_stage2 = await self._cross_channel_mapper.map_cross_channel(
       channels_with_remaining
   )

5. Сборка FullAttributeMappingResult:
   return FullAttributeMappingResult(
       results=result_stage1.results,
       unresolved=result_stage1.unresolved,
       cross_channel_matches=result_stage2.matches,
   )
```

### Инжекция CrossChannelMapper

`CrossChannelMapper` инжектируется в `AttributeMapper` через конструктор:

```python
class AttributeMapper:
    def __init__(
        self,
        ai_comparator: "AIComparator",
        cross_channel_mapper: "CrossChannelMapper",
    ) -> None:
```

`MappingJobWorker` создаёт оба маппера и передаёт `CrossChannelMapper`
в `AttributeMapper` при инициализации.

---

## 8. Изменения MappingJobWorker

**Файл:** `services/mapping/job_worker.py`

### Изменения в __init__

```python
# Добавляется создание CrossChannelMapper:
self._cross_channel_mapper = CrossChannelMapper(ai_comparator)

# AttributeMapper теперь получает cross_channel_mapper:
self._attribute_mapper = AttributeMapper(ai_comparator, self._cross_channel_mapper)
```

### Изменения в _execute_job

Результат `attribute_mapping` теперь `FullAttributeMappingResult`.
Счётчик `matched_count` включает оба этапа:

```python
# Было:
result: AttributeMappingResult = await self._attribute_mapper.map_attributes(task)
matched_count = len(result.results)
unresolved_count = len(result.unresolved)

# Стало:
result: FullAttributeMappingResult = await self._attribute_mapper.map_attributes(task)
matched_count = len(result.results) + len(result.cross_channel_matches)
unresolved_count = len(result.unresolved)
```

> **Логика счётчика.** В поле `matched_count` БД теперь хранится суммарное число
> успешных связок обоих этапов. Это позволяет оператору видеть в дашборде полную
> картину без разбиения. Поле `unresolved_count` — только атрибуты категории
> без соответствия (как и сейчас), межканальные группы туда не входят.

---

## 9. Изменения v1_api.py

**Файл:** `web/routes/v1_api.py`

### Изменения в get_mapping_task_status

Блок `completed` разворачивает сохранённый `result` на верхний уровень ответа.
Никаких структурных изменений в этой логике нет — новый ключ `crossChannelMatches`
просто появится в `result` JSONB автоматически, так как `FullAttributeMappingResult.to_dict()`
его включает.

Изменение минимальное: обновляется только тип аннотации в комментарии к функции,
фактическая логика `response.update(result)` остаётся прежней.

---

## 10. Новый промпт

**Файл:** `prompts/cross_channel_attribute_mapping.txt` *(новый)*

### Плейсхолдеры

| Плейсхолдер | Источник |
|---|---|
| `{category_name}` | `task.category.name` |
| `{category_path}` | `task.category.path` или `"не указан"` |
| `{channels_json}` | JSON остаточных атрибутов каналов |

### Контракт формата ответа LLM

```json
{
  "groups": [
    {
      "confidence": 0.88,
      "comment": "Пояснение для оператора",
      "channelMatches": [
        { "schemaChannelId": 10, "channelAttributeId": 910, "confidence": 0.91 },
        { "schemaChannelId": 20, "channelAttributeId": 215, "confidence": 0.85 }
      ]
    }
  ]
}
```

LLM **не возвращает** имена атрибутов — только ID из входных данных.
Группы с одним `channelMatches` не создаются (как минимум два канала).

### Ключевые правила промпта

- Использовать **только** `schemaChannelId` и `channelAttributeId` из входных данных.
- Группировать атрибуты по семантическому смыслу: «Цвет» у Ozon и «Цвет товара»
  у WB — одна группа.
- Шкала `confidence` та же, что и в основном промпте (0.50–1.0).
- Атрибут не попавший ни в одну группу — просто не включается в ответ
  (нет аналога `unresolved` для второго этапа).

---

## 11. Дашборд /agent — изменения

### list.html

Изменений не требуется. Счётчик `matched_count` в таблице теперь отражает
сумму связок обоих этапов — это корректно и не требует пояснений для оператора.

### detail.html

Требуется добавить новый блок после таблицы `results` и блока `unresolved`:

**Новый блок «Межканальные связки»:**

```
┌────────────────────────────────────────────────────────┐
│  Межканальные связки (без атрибута категории)    [N]   │
├──────────────────────────────────────────────────────────┤
│  Канал A: «Название атрибута A»    [confidence]         │
│  Канал B: «Название атрибута B»    [confidence]         │
│  Комментарий AI: «...»                                  │
│  ─────────────────────────────────────────────────────  │
│  Канал A: «Название атрибута C»    [confidence]         │
│  Канал C: «Название атрибута D»    [confidence]         │
└────────────────────────────────────────────────────────┘
```

- Названия атрибутов и каналов восстанавливаются из `payload` по ID
  (аналогично существующей логике для `results`).
- Confidence-бейджи той же цветовой шкалы: ≥85% зелёный, ≥70% жёлтый, ниже — красный.
- Блок не отображается, если `crossChannelMatches` пуст.

> **Важно.** Реализация изменений `detail.html` выносится в отдельную задачу
> и согласовывается с заказчиком по UI. API-часть (`crossChannelMatches` в ответе)
> полностью реализуется в данной доработке и не блокируется дашбордом.

---

## 12. Счётчики в БД

Таблица `mapping_jobs` — **без изменений структуры**. Поля используются так:

| Поле | Значение в v6.2 |
|---|---|
| `matched_count` | `len(results) + len(crossChannelMatches)` |
| `unresolved_count` | `len(unresolved)` — только атрибуты категории без пары |
| `result` JSONB | Содержит ключи `results`, `unresolved`, `crossChannelMatches` |

Миграция БД не требуется.

---

## 13. Список затронутых файлов

| Файл | Статус | Суть изменения |
|---|---|---|
| `services/mapping/models.py` | **Изменён** | +3 dataclass: `CrossChannelMatch`, `CrossChannelMappingResult`, `FullAttributeMappingResult` |
| `services/mapping/cross_channel_mapper.py` | **Новый** | Strategy второго этапа, промпт, пост-валидация |
| `services/mapping/attribute_mapper.py` | **Изменён** | Сбор остатков, вызов `CrossChannelMapper`, новый тип возврата |
| `services/mapping/job_worker.py` | **Изменён** | Создание `CrossChannelMapper`, новые счётчики |
| `web/routes/v1_api.py` | **Изменён** | Обновление аннотации, фактическая логика не меняется |
| `prompts/cross_channel_attribute_mapping.txt` | **Новый** | Промпт второго этапа |
| `web/templates/agent/detail.html` | **Изменён (отдельная задача)** | Блок межканальных связок |
| `services/mapping/validators.py` | **Без изменений** | — |
| `database/database.py` | **Без изменений** | — |
| `database/migrations.py` | **Без изменений** | — |

---

## 14. Сценарий работы end-to-end

```
FDM → POST /v1/mapping-tasks (attribute_mapping)
   payload: {category: {attributes: [A, B, C]},
             channels: [WB: [w1,w2,w3], Ozon: [o1,o2,o3]]}
   ↓
   Воркер забирает задание из БД

── Этап 1 (AttributeMapper, существующий) ──────────────────
   AI: A→w1+o1, B→w2+o2, C→ничего
   results:    [{A, w1+o1}, {B, w2+o2}]
   unresolved: [C]

── Сбор остатков ────────────────────────────────────────────
   Использованы в каналах: WB={w1,w2}, Ozon={o1,o2}
   Остатки:                WB={w3},    Ozon={o3}

── Этап 2 (CrossChannelMapper, новый) ──────────────────────
   AI: w3 ↔ o3 — семантически близкие
   crossChannelMatches: [{w3+o3, confidence=0.84}]

── Сохранение в БД ─────────────────────────────────────────
   result JSONB = {results, unresolved, crossChannelMatches}
   matched_count   = 2 + 1 = 3
   unresolved_count = 1

FDM → GET /v1/mapping-tasks/{jobId}
   ← 200 {results, unresolved: [C], crossChannelMatches: [{w3+o3}]}
```

---

## 15. Обратная совместимость

| Аспект | Совместимость |
|---|---|
| POST /v1/mapping-tasks | ✅ Тело запроса не изменилось |
| GET /v1/mapping-tasks — `results` | ✅ Структура не изменилась |
| GET /v1/mapping-tasks — `unresolved` | ✅ Структура не изменилась |
| GET /v1/mapping-tasks — `crossChannelMatches` | ⚠️ Новое поле. FDM должен быть готов к его наличию. Безопасно игнорируется при отсутствии обработки |
| `reference_value_mapping` | ✅ Не затронут вообще |
| Дашборд /agent list | ✅ Счётчик `matched_count` суммарный — отображается корректно |
| Дашборд /agent detail | ⚠️ Новый блок добавляется отдельной задачей, до реализации — блок просто отсутствует |
| БД — схема таблиц | ✅ Без изменений, миграция не нужна |
