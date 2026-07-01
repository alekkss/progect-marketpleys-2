"""
Модуль для сравнения столбцов с помощью AI
"""

import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx
from openai import AsyncOpenAI, RateLimitError, APIStatusError

from config.config import (
    AI_MODEL,
    AI_TEMPERATURE,
    FORCED_PAIR_ONLY_MATCHES,
    MANDATORY_MATCHES,
    is_excluded_column,
    Config,
)
from utils.excel_reader import ExcelReader
from utils.logger_config import setup_logger

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = setup_logger('ai_comparator')


class AIComparator:
    """Класс для сравнения столбцов с использованием AI"""

    # Пути к файлам с промптами
    PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
    COLUMN_MATCHING_PROMPT = PROMPTS_DIR / "column_matching.txt"
    SCHEMA_COMPARISON_PROMPT = PROMPTS_DIR / "schema_comparison.txt"
    VALUE_VALIDATION_PROMPT = PROMPTS_DIR / "value_validation.txt"
    MVM_MATCHING_PROMPT = PROMPTS_DIR / "mvm_column_matching.txt"

    # Параметры retry для AI-запросов
    _RETRY_MAX_ATTEMPTS: int = 3       # максимум попыток
    _RETRY_BASE_DELAY: float = 2.0     # базовая задержка в секундах
    _RETRY_MAX_DELAY: float = 30.0     # максимальная задержка в секундах
    _RETRY_JITTER: float = 0.3         # ±30% случайного отклонения

    def __init__(self):
        """Инициализация AI компаратора с поддержкой прокси"""
        # Создаем HTTP клиент с прокси если включено
        if Config.PROXY_ENABLED and Config.PROXY_URL:
            print(f"[🔒] Используется прокси для OpenRouter API")
            self._http_client = httpx.AsyncClient(  # ← сохраняем в self, чтобы можно было закрыть
                proxy=Config.PROXY_URL,
                timeout=120.0
            )
            self.client = AsyncOpenAI(
                api_key=Config.OPENROUTER_API_KEY,
                base_url=Config.OPENROUTER_BASE_URL,
                http_client=self._http_client
            )
        else:
            print("[⚠️] Прокси не настроен, прямое подключение")
            self._http_client = None  # ← явно None, чтобы close() мог проверить
            self.client = AsyncOpenAI(
                api_key=Config.OPENROUTER_API_KEY,
                base_url=Config.OPENROUTER_BASE_URL
            )

        self.model = Config.AI_MODEL

        # Ограничение параллельных запросов к AI
        self._semaphore = asyncio.Semaphore(5)

        # Загружаем промпты при инициализации
        self._load_prompts()

    def _load_prompts(self):
        """Загружает промпты из файлов"""
        try:
            with open(self.COLUMN_MATCHING_PROMPT, 'r', encoding='utf-8') as f:
                self.column_matching_template = f.read()

            with open(self.SCHEMA_COMPARISON_PROMPT, 'r', encoding='utf-8') as f:
                self.schema_comparison_template = f.read()

            with open(self.VALUE_VALIDATION_PROMPT, 'r', encoding='utf-8') as f:
                self.value_validation_template = f.read()

            print("[✓] Промпты загружены из файлов")

        except FileNotFoundError as e:
            print(f"[!] ОШИБКА: Файл с промптом не найден: {e.filename}")
            print(f"[!] Убедитесь что файлы существуют в директории: {self.PROMPTS_DIR}")
            raise

        # МВМ промпт загружаем опционально (может не существовать до первого использования)
        try:
            with open(self.MVM_MATCHING_PROMPT, 'r', encoding='utf-8') as f:
                self.mvm_matching_template = f.read()
            print("[✓] МВМ промпт загружен")
        except FileNotFoundError:
            self.mvm_matching_template = None
            print("[i] МВМ промпт не найден (будет загружен при первом использовании)")

    async def close(self) -> None:
        """
        Закрывает HTTP-клиент и освобождает сетевые соединения.

        Вызывать при завершении работы воркера или при уничтожении
        экземпляра класса. Безопасен при повторном вызове.
        """
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
            logger.info("HTTP-клиент AIComparator закрыт")

    async def compare_columns(
        self,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str]
    ) -> Dict:
        """
        Сравнивает столбцы из трех файлов с помощью AI (с двойной проверкой)

        Args:
            columns_1: столбцы из первого файла
            columns_2: столбцы из второго файла
            columns_3: столбцы из третьего файла

        Returns:
            Словарь с результатами сравнения
        """
        # НОВОЕ: Фильтруем исключенные столбцы
        filtered_1, excluded_1 = self._filter_excluded_columns(columns_1)
        filtered_2, excluded_2 = self._filter_excluded_columns(columns_2)
        filtered_3, excluded_3 = self._filter_excluded_columns(columns_3)

        if excluded_1 or excluded_2 or excluded_3:
            print(f"\n[!] Исключены из сравнения:")
            if excluded_1:
                print(f"   WB: {', '.join(excluded_1)}")
            if excluded_2:
                print(f"   Ozon: {', '.join(excluded_2)}")
            if excluded_3:
                print(f"   Яндекс: {', '.join(excluded_3)}")

        print("\n[*] Отправляю ПЕРВЫЙ запрос в OpenRouter AI...")

        # Первый проход (с отфильтрованными столбцами)
        prompt = self._build_prompt(filtered_1, filtered_2, filtered_3)
        response = await self._call_ai(prompt)
        result = self._parse_response(response)

        # ✅ ВАЛИДАЦИЯ РЕЗУЛЬТАТА ОТ AI
        result = self._validate_ai_result(result, filtered_1, filtered_2, filtered_3)

        result = self._add_mandatory_matches(result, filtered_1, filtered_2, filtered_3)

        print(f"[+] Первый проход завершен!")
        print(f"   Найдено совпадений (все 3): {len(result.get('matches_all_three', []))}")
        print(f"   Найдено совпадений (1-2): {len(result.get('matches_1_2', []))}")
        print(f"   Найдено совпадений (1-3): {len(result.get('matches_1_3', []))}")
        print(f"   Найдено совпадений (2-3): {len(result.get('matches_2_3', []))}")

        # Второй проход - проверяем оставшиеся несовпавшие столбцы
        print("\n[*] Запускаю ВТОРОЙ проход для проверки оставшихся столбцов...")
        remaining_columns = self._get_remaining_columns(result, filtered_1, filtered_2, filtered_3)

        if remaining_columns[0] or remaining_columns[1] or remaining_columns[2]:
            print(f"   Осталось проверить: WB={len(remaining_columns[0])}, "
                  f"Ozon={len(remaining_columns[1])}, Яндекс={len(remaining_columns[2])}")

            second_result = await self._second_pass_comparison(remaining_columns)

            # ✅ ВАЛИДАЦИЯ ВТОРОГО ПРОХОДА
            second_result = self._validate_ai_result(
                second_result,
                remaining_columns[0],
                remaining_columns[1],
                remaining_columns[2]
            )

            # Объединяем результаты
            result = self._merge_results(result, second_result)

            print(f"[+] Второй проход завершен!")
            print(f"   Дополнительно найдено совпадений (все 3): {len(second_result.get('matches_all_three', []))}")
            print(f"   Дополнительно найдено совпадений (1-2): {len(second_result.get('matches_1_2', []))}")
            print(f"   Дополнительно найдено совпадений (1-3): {len(second_result.get('matches_1_3', []))}")
            print(f"   Дополнительно найдено совпадений (2-3): {len(second_result.get('matches_2_3', []))}")
        else:
            print("   Все столбцы уже сопоставлены, второй проход не требуется")

        # НОВОЕ: Добавляем исключенные столбцы в результат
        result = self._add_excluded_to_result(result, excluded_1, excluded_2, excluded_3)

        # Принудительные парные сопоставления (перед дедупликацией)
        result = self._enforce_forced_pairs(result, filtered_1, filtered_2, filtered_3)

        # НОВОЕ: Удаляем дубли и пересечения между тройными и парными
        result = self._deduplicate_matches(result)

        print("\n[+] Итоговые результаты получены от AI")
        return result

    async def compare_columns_mvm(
        self,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str],
        columns_4: List[str]
    ) -> Dict:
        """
        Сравнивает столбцы из 4 источников: WB, Ozon, Яндекс + XML.

        Стратегия:
        1. Сначала вызываем стандартный compare_columns для 3 МП
        2. Затем дополняем результат сопоставлением XML-полей

        Args:
            columns_1: столбцы WB
            columns_2: столбцы Ozon
            columns_3: столбцы Яндекс
            columns_4: поля XML (с префиксами [XML] и [XML param])

        Returns:
            Словарь с результатами сравнения 4 источников
        """
        # === ШАГ 1: Стандартное сопоставление 3 МП ===
        print("\n" + "=" * 60)
        print("[МВМ] ШАГ 1: Сопоставление 3 маркетплейсов...")
        print("=" * 60)

        three_mp_result = await self.compare_columns(columns_1, columns_2, columns_3)

        # === ШАГ 2: Сопоставление XML с 3 МП ===
        print("\n" + "=" * 60)
        print("[МВМ] ШАГ 2: Сопоставление XML полей с маркетплейсами...")
        print("=" * 60)

        # Фильтруем XML-поля (исключения)
        filtered_4, excluded_4 = self._filter_excluded_columns(columns_4)

        if excluded_4:
            print(f"[!] Исключены XML поля: {', '.join(excluded_4)}")

        print(f"[*] XML полей для сопоставления: {len(filtered_4)}")

        # Загружаем МВМ промпт
        if not self.mvm_matching_template:
            try:
                with open(self.MVM_MATCHING_PROMPT, 'r', encoding='utf-8') as f:
                    self.mvm_matching_template = f.read()
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"Файл промпта для МВМ не найден: {self.MVM_MATCHING_PROMPT}\n"
                    f"Создайте файл prompts/mvm_column_matching.txt"
                )

        # Собираем все столбцы МП для промпта (без исключённых)
        filtered_1, _ = self._filter_excluded_columns(columns_1)
        filtered_2, _ = self._filter_excluded_columns(columns_2)
        filtered_3, _ = self._filter_excluded_columns(columns_3)

        # Формируем промпт для МВМ сопоставления
        mvm_prompt = self.mvm_matching_template.format(
            columns_1=json.dumps(filtered_1, ensure_ascii=False, indent=2),
            columns_2=json.dumps(filtered_2, ensure_ascii=False, indent=2),
            columns_3=json.dumps(filtered_3, ensure_ascii=False, indent=2),
            columns_4=json.dumps(filtered_4, ensure_ascii=False, indent=2)
        )

        print("[*] Отправляю запрос в AI для сопоставления XML...")
        mvm_response = await self._call_ai(mvm_prompt)
        mvm_result = self._parse_response(mvm_response)

        # Валидация результата МВМ
        mvm_result = self._validate_mvm_result(
            mvm_result, filtered_1, filtered_2, filtered_3, filtered_4
        )

        print(f"[+] МВМ сопоставление завершено!")
        self._print_mvm_stats(mvm_result)

        # === ШАГ 3: Объединяем результаты 3 МП + XML ===
        print("\n[*] Объединяю результаты 3 МП и XML...")

        final_result = self._merge_mvm_results(three_mp_result, mvm_result)

        # Добавляем исключённые XML-поля в уникальные
        if excluded_4:
            if 'only_in_fourth' not in final_result:
                final_result['only_in_fourth'] = []
            final_result['only_in_fourth'].extend(excluded_4)

        # Дедупликация для 4 источников
        final_result = self._deduplicate_mvm_matches(final_result)

        print("\n[+] Итоговые результаты МВМ получены")
        self._print_mvm_stats(final_result)

        return final_result

    def _validate_mvm_result(
        self,
        result: Dict,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str],
        columns_4: List[str]
    ) -> Dict:
        """
        Валидирует результат МВМ сопоставления от AI.
        Проверяет что каждый столбец реально существует в исходных списках.

        Args:
            result: результат от AI
            columns_1: столбцы WB
            columns_2: столбцы Ozon
            columns_3: столбцы Яндекс
            columns_4: поля XML

        Returns:
            Валидированный результат
        """
        col_sets = {
            'column_1': set(columns_1),
            'column_2': set(columns_2),
            'column_3': set(columns_3),
            'column_4': set(columns_4),
        }

        col_names = {
            'column_1': 'WB',
            'column_2': 'Ozon',
            'column_3': 'Яндекс',
            'column_4': 'XML',
        }

        validated = {}
        rejected_count = 0

        # Все ключи групп сопоставлений для 4 источников
        match_group_keys = [
            'matches_all_four',
            'matches_triple_1_2_3', 'matches_triple_1_2_4',
            'matches_triple_1_3_4', 'matches_triple_2_3_4',
            'matches_pair_1_2', 'matches_pair_1_3', 'matches_pair_1_4',
            'matches_pair_2_3', 'matches_pair_2_4', 'matches_pair_3_4',
            'only_in_first', 'only_in_second', 'only_in_third', 'only_in_fourth'
        ]

        for key in match_group_keys:
            source_list = result.get(key, [])

            # Для уникальных — просто копируем
            if key.startswith('only_in_'):
                validated[key] = source_list
                continue

            cleaned = []
            for match in source_list:
                is_valid = True

                for col_key, col_set in col_sets.items():
                    col_value = match.get(col_key)
                    if col_value and col_value not in col_set:
                        print(
                            f"[❌ ОТКЛОНЕНО МВМ] AI придумал несуществующий столбец "
                            f"{col_names[col_key]}: '{col_value}'"
                        )
                        rejected_count += 1
                        is_valid = False
                        break

                if is_valid:
                    cleaned.append(match)

            validated[key] = cleaned

        if rejected_count > 0:
            print(f"\n[⚠️] МВМ ВАЛИДАЦИЯ: Отклонено {rejected_count} несуществующих совпадений\n")

        return validated

    def _merge_mvm_results(self, three_mp_result: Dict, mvm_result: Dict) -> Dict:
        """
        Объединяет результат стандартного сопоставления 3 МП
        с результатом МВМ (XML) сопоставления.

        Логика:
        - Тройные совпадения 3 МП, которые имеют XML-аналог → четверные
        - Тройные совпадения 3 МП без XML → остаются тройными (matches_triple_1_2_3)
        - Парные из 3 МП, дополненные XML → тройные с XML
        - Новые парные с XML (1_4, 2_4, 3_4) → из mvm_result

        Args:
            three_mp_result: результат compare_columns() для 3 МП
            mvm_result: результат AI для XML

        Returns:
            Объединённый результат для 4 источников
        """
        final = {
            'matches_all_four': [],
            'matches_triple_1_2_3': [],
            'matches_triple_1_2_4': [],
            'matches_triple_1_3_4': [],
            'matches_triple_2_3_4': [],
            'matches_pair_1_2': [],
            'matches_pair_1_3': [],
            'matches_pair_1_4': [],
            'matches_pair_2_3': [],
            'matches_pair_2_4': [],
            'matches_pair_3_4': [],
            'only_in_first': three_mp_result.get('only_in_first', []),
            'only_in_second': three_mp_result.get('only_in_second', []),
            'only_in_third': three_mp_result.get('only_in_third', []),
            'only_in_fourth': mvm_result.get('only_in_fourth', []),
        }

        # Индекс XML-сопоставлений: column_1 → column_4, column_2 → column_4, column_3 → column_4
        xml_by_col1 = {}
        xml_by_col2 = {}
        xml_by_col3 = {}

        # Собираем все XML-сопоставления из mvm_result
        for key in ['matches_all_four', 'matches_triple_1_2_4', 'matches_triple_1_3_4',
                     'matches_triple_2_3_4', 'matches_pair_1_4', 'matches_pair_2_4',
                     'matches_pair_3_4']:
            for match in mvm_result.get(key, []):
                col_4 = match.get('column_4')
                if not col_4:
                    continue
                if match.get('column_1'):
                    xml_by_col1[match['column_1']] = col_4
                if match.get('column_2'):
                    xml_by_col2[match['column_2']] = col_4
                if match.get('column_3'):
                    xml_by_col3[match['column_3']] = col_4

        used_xml_fields = set()

        # === Обрабатываем тройные (3 МП) → проверяем есть ли XML ===
        for match in three_mp_result.get('matches_all_three', []):
            col1 = match.get('column_1', '')
            col2 = match.get('column_2', '')
            col3 = match.get('column_3', '')

            # Ищем XML-поле по любому из 3 столбцов
            col4 = xml_by_col1.get(col1) or xml_by_col2.get(col2) or xml_by_col3.get(col3)

            if col4:
                # Тройное → четверное
                new_match = {**match, 'column_4': col4}
                final['matches_all_four'].append(new_match)
                used_xml_fields.add(col4)
            else:
                # Остаётся тройным (1+2+3)
                final['matches_triple_1_2_3'].append(match)

        # === Обрабатываем парные (3 МП) → проверяем есть ли XML ===
        for match in three_mp_result.get('matches_1_2', []):
            col1 = match.get('column_1', '')
            col2 = match.get('column_2', '')
            col4 = xml_by_col1.get(col1) or xml_by_col2.get(col2)

            if col4:
                new_match = {**match, 'column_4': col4}
                final['matches_triple_1_2_4'].append(new_match)
                used_xml_fields.add(col4)
            else:
                final['matches_pair_1_2'].append(match)

        for match in three_mp_result.get('matches_1_3', []):
            col1 = match.get('column_1', '')
            col3 = match.get('column_3', '')
            col4 = xml_by_col1.get(col1) or xml_by_col3.get(col3)

            if col4:
                new_match = {**match, 'column_4': col4}
                final['matches_triple_1_3_4'].append(new_match)
                used_xml_fields.add(col4)
            else:
                final['matches_pair_1_3'].append(match)

        for match in three_mp_result.get('matches_2_3', []):
            col2 = match.get('column_2', '')
            col3 = match.get('column_3', '')
            col4 = xml_by_col2.get(col2) or xml_by_col3.get(col3)

            if col4:
                new_match = {**match, 'column_4': col4}
                final['matches_triple_2_3_4'].append(new_match)
                used_xml_fields.add(col4)
            else:
                final['matches_pair_2_3'].append(match)

        # === Добавляем оставшиеся чисто XML-парные (1_4, 2_4, 3_4) ===
        for match in mvm_result.get('matches_pair_1_4', []):
            col4 = match.get('column_4', '')
            if col4 not in used_xml_fields:
                final['matches_pair_1_4'].append(match)
                used_xml_fields.add(col4)

        for match in mvm_result.get('matches_pair_2_4', []):
            col4 = match.get('column_4', '')
            if col4 not in used_xml_fields:
                final['matches_pair_2_4'].append(match)
                used_xml_fields.add(col4)

        for match in mvm_result.get('matches_pair_3_4', []):
            col4 = match.get('column_4', '')
            if col4 not in used_xml_fields:
                final['matches_pair_3_4'].append(match)
                used_xml_fields.add(col4)

        return final

    def _deduplicate_mvm_matches(self, result: Dict) -> Dict:
        """
        Дедупликация для 4-источникового результата.
        Удаляет столбцы из парных/тройных, если они уже присутствуют
        в четверных сопоставлениях.

        Args:
            result: полный результат МВМ

        Returns:
            Очищенный результат
        """
        # Собираем все столбцы из четверных
        used = {f'col_{i}': set() for i in range(1, 5)}

        for match in result.get('matches_all_four', []):
            for i in range(1, 5):
                val = match.get(f'column_{i}')
                if val:
                    used[f'col_{i}'].add(val)

        total_removed = 0

        # Очищаем все тройные и парные группы
        group_col_map = {
            'matches_triple_1_2_3': ['column_1', 'column_2', 'column_3'],
            'matches_triple_1_2_4': ['column_1', 'column_2', 'column_4'],
            'matches_triple_1_3_4': ['column_1', 'column_3', 'column_4'],
            'matches_triple_2_3_4': ['column_2', 'column_3', 'column_4'],
            'matches_pair_1_2': ['column_1', 'column_2'],
            'matches_pair_1_3': ['column_1', 'column_3'],
            'matches_pair_1_4': ['column_1', 'column_4'],
            'matches_pair_2_3': ['column_2', 'column_3'],
            'matches_pair_2_4': ['column_2', 'column_4'],
            'matches_pair_3_4': ['column_3', 'column_4'],
        }

        for group_key, col_keys in group_col_map.items():
            original = result.get(group_key, [])
            cleaned = []

            for match in original:
                # Проверяем: хотя бы один столбец уже в четверных?
                is_dup = False
                for ck in col_keys:
                    val = match.get(ck)
                    col_idx = ck.replace('column_', 'col_')
                    if val and val in used.get(col_idx, set()):
                        is_dup = True
                        break

                if not is_dup:
                    cleaned.append(match)
                else:
                    total_removed += 1

            result[group_key] = cleaned

        if total_removed > 0:
            print(f"[DEDUPE МВМ] Удалено дублей из тройных/парных: {total_removed}")

        return result

    def _print_mvm_stats(self, result: Dict):
        """Выводит статистику МВМ результата"""
        stats = {
            'Четверные (все 4)': len(result.get('matches_all_four', [])),
            'Тройные (WB+Ozon+Яндекс)': len(result.get('matches_triple_1_2_3', [])),
            'Тройные (WB+Ozon+XML)': len(result.get('matches_triple_1_2_4', [])),
            'Тройные (WB+Яндекс+XML)': len(result.get('matches_triple_1_3_4', [])),
            'Тройные (Ozon+Яндекс+XML)': len(result.get('matches_triple_2_3_4', [])),
            'Парные (WB+Ozon)': len(result.get('matches_pair_1_2', [])),
            'Парные (WB+Яндекс)': len(result.get('matches_pair_1_3', [])),
            'Парные (WB+XML)': len(result.get('matches_pair_1_4', [])),
            'Парные (Ozon+Яндекс)': len(result.get('matches_pair_2_3', [])),
            'Парные (Ozon+XML)': len(result.get('matches_pair_2_4', [])),
            'Парные (Яндекс+XML)': len(result.get('matches_pair_3_4', [])),
        }

        for name, count in stats.items():
            if count > 0:
                print(f"   {name}: {count}")

    def _filter_excluded_columns(self, columns: List[str]) -> tuple:
        """
        Фильтрует исключенные столбцы

        Returns:
            Кортеж (список разрешенных столбцов, список исключенных столбцов)
        """
        allowed = []
        excluded = []

        for col in columns:
            if is_excluded_column(col):
                excluded.append(col)
            else:
                allowed.append(col)

        return allowed, excluded

    def _add_excluded_to_result(
        self,
        result: Dict,
        excluded_1: List[str],
        excluded_2: List[str],
        excluded_3: List[str]
    ) -> Dict:
        """
        Добавляет исключенные столбцы в уникальные
        """
        result['only_in_first'].extend(excluded_1)
        result['only_in_second'].extend(excluded_2)
        result['only_in_third'].extend(excluded_3)

        return result

    def _validate_ai_result(
        self,
        result: Dict,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str]
    ) -> Dict:
        """
        Валидирует результат от AI - удаляет несуществующие столбцы.

        Проверяет каждое совпадение и отклоняет те, где AI указал столбец,
        которого нет в исходных списках. Это предотвращает ошибки при
        синхронизации данных.

        Args:
            result: результат от AI с совпадениями
            columns_1: исходный список столбцов WB
            columns_2: исходный список столбцов Ozon
            columns_3: исходный список столбцов Яндекс

        Returns:
            Валидированный результат без несуществующих столбцов
        """
        validated = {
            'matches_all_three': [],
            'matches_1_2': [],
            'matches_1_3': [],
            'matches_2_3': [],
            'only_in_first': result.get('only_in_first', []),
            'only_in_second': result.get('only_in_second', []),
            'only_in_third': result.get('only_in_third', [])
        }

        rejected_count = 0

        # Валидируем совпадения всех трех
        for match in result.get('matches_all_three', []):
            col_1 = match.get('column_1')
            col_2 = match.get('column_2')
            col_3 = match.get('column_3')

            # ЖЕСТКАЯ ПРОВЕРКА: столбец должен существовать!
            if col_1 and col_1 not in columns_1:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец WB: '{col_1}'")
                rejected_count += 1
                continue
            if col_2 and col_2 not in columns_2:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец Ozon: '{col_2}'")
                rejected_count += 1
                continue
            if col_3 and col_3 not in columns_3:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец Яндекс: '{col_3}'")
                rejected_count += 1
                continue

            # Все столбцы существуют - добавляем
            validated['matches_all_three'].append(match)

        # Валидируем совпадения 1-2
        for match in result.get('matches_1_2', []):
            col_1 = match.get('column_1')
            col_2 = match.get('column_2')

            if col_1 and col_1 not in columns_1:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец WB: '{col_1}'")
                rejected_count += 1
                continue
            if col_2 and col_2 not in columns_2:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец Ozon: '{col_2}'")
                rejected_count += 1
                continue

            validated['matches_1_2'].append(match)

        # Валидируем совпадения 1-3
        for match in result.get('matches_1_3', []):
            col_1 = match.get('column_1')
            col_3 = match.get('column_3')

            if col_1 and col_1 not in columns_1:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец WB: '{col_1}'")
                rejected_count += 1
                continue
            if col_3 and col_3 not in columns_3:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец Яндекс: '{col_3}'")
                rejected_count += 1
                continue

            validated['matches_1_3'].append(match)

        # Валидируем совпадения 2-3
        for match in result.get('matches_2_3', []):
            col_2 = match.get('column_2')
            col_3 = match.get('column_3')

            if col_2 and col_2 not in columns_2:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец Ozon: '{col_2}'")
                rejected_count += 1
                continue
            if col_3 and col_3 not in columns_3:
                print(f"[❌ ОТКЛОНЕНО] AI придумал несуществующий столбец Яндекс: '{col_3}'")
                rejected_count += 1
                continue

            validated['matches_2_3'].append(match)

        if rejected_count > 0:
            print(f"\n[⚠️] ВАЛИДАЦИЯ: Отклонено {rejected_count} несуществующих совпадений от AI\n")

        return validated

    def _enforce_forced_pairs(
        self,
        result: Dict,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str],
    ) -> Dict:
        """
        Принудительно применяет правила из FORCED_PAIR_ONLY_MATCHES.

        Для каждого правила (например, Видео: WB+Яндекс, Ozon=NA):
            1. Удаляет эти столбцы из тройных сопоставлений (matches_all_three).
            2. Удаляет эти столбцы из ошибочных парных с запрещённым МП
               (например, matches_1_2 и matches_2_3 если column_2=None).
            3. Если освободившийся столбец Ozon остался без пары —
               возвращает его в only_in_second.
            4. Гарантирует наличие правильного парного сопоставления
               (например, matches_1_3 для WB+Яндекс).

        Вызывается после _add_mandatory_matches и до _deduplicate_matches.

        Args:
            result:    текущий результат сопоставлений.
            columns_1: столбцы WB (для fuzzy-поиска).
            columns_2: столбцы Ozon.
            columns_3: столбцы Яндекс.

        Returns:
            Обновлённый результат с принудительными парными.
        """
        if not FORCED_PAIR_ONLY_MATCHES:
            return result

        for rule in FORCED_PAIR_ONLY_MATCHES:
            rule_col_1 = rule.get("column_1")
            rule_col_2 = rule.get("column_2")
            rule_col_3 = rule.get("column_3")
            description = rule.get("description", "")

            # Определяем какие column_key заполнены, а какой None (запрещённый МП)
            # Для Видео: column_1="Видео", column_2=None, column_3="Ссылка на видео"
            present_keys = {}   # {column_key: искомое_название}
            none_key = None     # column_key, который должен быть NA

            for col_key, col_val in [
                ("column_1", rule_col_1),
                ("column_2", rule_col_2),
                ("column_3", rule_col_3),
            ]:
                if col_val is None:
                    none_key = col_key
                else:
                    present_keys[col_key] = col_val

            if none_key is None or len(present_keys) < 2:
                continue  # Некорректное правило — пропускаем

            # Fuzzy-поиск реальных названий столбцов в файлах
            col_lists = {
                "column_1": columns_1,
                "column_2": columns_2,
                "column_3": columns_3,
            }
            resolved = {}
            for col_key, col_name in present_keys.items():
                found = ExcelReader.find_column_fuzzy(col_lists[col_key], col_name)
                if found:
                    resolved[col_key] = found

            if len(resolved) < 2:
                # Столбцы не найдены в файлах — правило неприменимо
                continue

            # Множество найденных столбцов для быстрого поиска
            resolved_values = set(resolved.values())

            print(f"\n[FORCED PAIR] Применяю правило: {description}")
            print(f"   Столбцы: {resolved}")
            print(f"   Запрещённый МП: {none_key}")

            removed_count = 0
            freed_ozon_columns: List[str] = []

            # --- ШАГ 1: Удаляем из тройных (matches_all_three) ---
            cleaned_triple = []
            for match in result.get("matches_all_three", []):
                # Проверяем, содержит ли это тройное сопоставление
                # хотя бы один из столбцов правила
                match_has_forced = False
                for col_key, col_val in resolved.items():
                    if match.get(col_key) == col_val:
                        match_has_forced = True
                        break

                if match_has_forced:
                    # Запоминаем освободившийся столбец запрещённого МП
                    freed_col = match.get(none_key)
                    if freed_col:
                        freed_ozon_columns.append(freed_col)
                    removed_count += 1
                    print(
                        f"   ❌ Удалено из тройных: "
                        f"'{match.get('column_1')}' ↔ "
                        f"'{match.get('column_2')}' ↔ "
                        f"'{match.get('column_3')}'"
                    )
                else:
                    cleaned_triple.append(match)

            result["matches_all_three"] = cleaned_triple

            # --- ШАГ 2: Удаляем из ошибочных парных с запрещённым МП ---
            # Определяем какие парные группы содержат запрещённый МП
            pair_groups_with_none = []
            if none_key == "column_1":
                pair_groups_with_none = ["matches_1_2", "matches_1_3"]
            elif none_key == "column_2":
                pair_groups_with_none = ["matches_1_2", "matches_2_3"]
            elif none_key == "column_3":
                pair_groups_with_none = ["matches_1_3", "matches_2_3"]

            for group_key in pair_groups_with_none:
                cleaned_pair = []
                for match in result.get(group_key, []):
                    match_has_forced = False
                    for col_key, col_val in resolved.items():
                        if match.get(col_key) == col_val:
                            match_has_forced = True
                            break

                    if match_has_forced:
                        freed_col = match.get(none_key)
                        if freed_col:
                            freed_ozon_columns.append(freed_col)
                        removed_count += 1
                        print(f"   ❌ Удалено из {group_key}: {match}")
                    else:
                        cleaned_pair.append(match)

                result[group_key] = cleaned_pair

            # --- ШАГ 3: Возвращаем освободившиеся столбцы в уникальные ---
            if freed_ozon_columns:
                unique_key_map = {
                    "column_1": "only_in_first",
                    "column_2": "only_in_second",
                    "column_3": "only_in_third",
                }
                unique_key = unique_key_map.get(none_key, "")
                if unique_key:
                    existing_unique = set(result.get(unique_key, []))
                    for col in freed_ozon_columns:
                        if col not in existing_unique:
                            result[unique_key].append(col)
                            print(f"   ↩️ Возвращён в {unique_key}: '{col}'")

            # --- ШАГ 4: Гарантируем наличие правильного парного ---
            # Определяем целевую парную группу
            resolved_keys = sorted(resolved.keys())  # ['column_1', 'column_3']
            pair_key_map = {
                ("column_1", "column_2"): "matches_1_2",
                ("column_1", "column_3"): "matches_1_3",
                ("column_2", "column_3"): "matches_2_3",
            }
            target_pair_key = pair_key_map.get(tuple(resolved_keys))

            if target_pair_key:
                # Проверяем, есть ли уже такое парное
                existing_pairs = result.get(target_pair_key, [])
                already_exists = any(
                    all(m.get(k) == v for k, v in resolved.items())
                    for m in existing_pairs
                )

                if not already_exists:
                    new_pair = {
                        **{k: v for k, v in resolved.items()},
                        "confidence": 1.0,
                        "mandatory": True,
                    }
                    result[target_pair_key].insert(0, new_pair)
                    print(
                        f"   ✅ Добавлено парное в {target_pair_key}: {resolved}"
                    )

            if removed_count > 0:
                print(
                    f"   📊 Итого: удалено {removed_count} ошибочных, "
                    f"освобождено столбцов: {len(freed_ozon_columns)}"
                )

        return result

    def _get_remaining_columns(
        self,
        result: Dict,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str]
    ) -> tuple:
        """
        Получает списки столбцов, которые НЕ вошли в совпадения всех трех маркетплейсов

        Returns:
            Кортеж из трех списков несопоставленных столбцов
        """
        # Собираем только те столбцы, которые УЖЕ вошли в совпадения всех трех
        matched_in_all_three_1 = set()
        matched_in_all_three_2 = set()
        matched_in_all_three_3 = set()

        # Только из совпадений всех трех маркетплейсов!
        for match in result.get('matches_all_three', []):
            col_1 = match.get('column_1')
            col_2 = match.get('column_2')
            col_3 = match.get('column_3')

            if col_1:
                matched_in_all_three_1.add(col_1)
            if col_2:
                matched_in_all_three_2.add(col_2)
            if col_3:
                matched_in_all_three_3.add(col_3)

        # Убираем пустые значения
        matched_in_all_three_1.discard('')
        matched_in_all_three_2.discard('')
        matched_in_all_three_3.discard('')

        # Формируем списки оставшихся столбцов (которые НЕ вошли в matches_all_three)
        remaining_1 = [col for col in columns_1 if col and col not in matched_in_all_three_1]
        remaining_2 = [col for col in columns_2 if col and col not in matched_in_all_three_2]
        remaining_3 = [col for col in columns_3 if col and col not in matched_in_all_three_3]

        return (remaining_1, remaining_2, remaining_3)

    async def _second_pass_comparison(self, remaining_columns: tuple) -> Dict:
        """
        Выполняет второй проход сравнения для оставшихся столбцов

        Args:
            remaining_columns: кортеж из трех списков оставшихся столбцов

        Returns:
            Результаты второго прохода
        """
        remaining_1, remaining_2, remaining_3 = remaining_columns

        # Используем промпт из файла schema_comparison.txt
        prompt = self.schema_comparison_template.format(
            len_remaining_1=len(remaining_1),
            remaining_1=json.dumps(remaining_1, ensure_ascii=False, indent=2),
            len_remaining_2=len(remaining_2),
            remaining_2=json.dumps(remaining_2, ensure_ascii=False, indent=2),
            len_remaining_3=len(remaining_3),
            remaining_3=json.dumps(remaining_3, ensure_ascii=False, indent=2)
        )

        response = await self._call_ai(prompt)
        result = self._parse_response(response)

        return result

    def _merge_results(self, first_result: Dict, second_result: Dict) -> Dict:
        """
        Объединяет результаты первого и второго прохода

        Args:
            first_result: результаты первого прохода
            second_result: результаты второго прохода

        Returns:
            Объединенные результаты
        """
        merged = {
            'matches_all_three': first_result.get('matches_all_three', []) + second_result.get('matches_all_three', []),
            'matches_1_2': first_result.get('matches_1_2', []) + second_result.get('matches_1_2', []),
            'matches_1_3': first_result.get('matches_1_3', []) + second_result.get('matches_1_3', []),
            'matches_2_3': first_result.get('matches_2_3', []) + second_result.get('matches_2_3', []),
            'only_in_first': second_result.get('only_in_first', []),
            'only_in_second': second_result.get('only_in_second', []),
            'only_in_third': second_result.get('only_in_third', [])
        }

        return merged

    def _deduplicate_matches(self, result: Dict) -> Dict:
        """
        Удаляет дубли и пересечения между тройными и парными сопоставлениями.

        Правила:
        1. Если столбец уже есть в тройном сопоставлении — удаляем его из парных
        2. Если столбец повторяется в нескольких парных — оставляем только первое вхождение

        Args:
            result: словарь с результатами сравнения от AI

        Returns:
            Очищенный словарь без дублей и пересечений
        """
        # === ШАГ 1: Собираем все столбцы из тройных сопоставлений ===
        triple_columns_1 = set()  # WB столбцы в тройных
        triple_columns_2 = set()  # Ozon столбцы в тройных
        triple_columns_3 = set()  # Яндекс столбцы в тройных

        for match in result.get('matches_all_three', []):
            col_1 = match.get('column_1')
            col_2 = match.get('column_2')
            col_3 = match.get('column_3')

            if col_1:
                triple_columns_1.add(col_1)
            if col_2:
                triple_columns_2.add(col_2)
            if col_3:
                triple_columns_3.add(col_3)

        print(f"\n[DEDUPE] Столбцов в тройных: WB={len(triple_columns_1)}, Ozon={len(triple_columns_2)}, Яндекс={len(triple_columns_3)}")

        # === ШАГ 2: Функция очистки парных сопоставлений ===
        def clean_pair_matches(
            matches: List[Dict],
            col_key_1: str,
            col_key_2: str,
            used_cols_1: set,
            used_cols_2: set,
            pair_name: str
        ) -> List[Dict]:
            """
            Очищает список парных сопоставлений:
            - Удаляет если хотя бы один столбец уже в тройных
            - Удаляет дубли (каждый столбец только в одном сопоставлении)
            """
            cleaned = []
            seen_cols_1 = set()  # Уже использованные столбцы первого маркетплейса
            seen_cols_2 = set()  # Уже использованные столбцы второго маркетплейса

            removed_triple = 0
            removed_duplicate = 0

            for match in matches:
                col1 = match.get(col_key_1)
                col2 = match.get(col_key_2)

                # Проверка 1: столбец уже в тройных?
                if col1 in used_cols_1 or col2 in used_cols_2:
                    removed_triple += 1
                    continue

                # Проверка 2: столбец уже использован в этой группе парных (дубль)?
                if col1 in seen_cols_1 or col2 in seen_cols_2:
                    removed_duplicate += 1
                    continue

                # Всё ок — добавляем и отмечаем как использованные
                cleaned.append(match)
                if col1:
                    seen_cols_1.add(col1)
                if col2:
                    seen_cols_2.add(col2)

            if removed_triple > 0 or removed_duplicate > 0:
                print(f"[DEDUPE] {pair_name}: удалено {removed_triple} (есть в тройных) + {removed_duplicate} (дубли), осталось {len(cleaned)}")

            return cleaned

        # === ШАГ 3: Очищаем каждую группу парных ===
        original_1_2 = len(result.get('matches_1_2', []))
        original_1_3 = len(result.get('matches_1_3', []))
        original_2_3 = len(result.get('matches_2_3', []))

        result['matches_1_2'] = clean_pair_matches(
            result.get('matches_1_2', []),
            'column_1', 'column_2',
            triple_columns_1, triple_columns_2,
            'WB+Ozon'
        )

        result['matches_1_3'] = clean_pair_matches(
            result.get('matches_1_3', []),
            'column_1', 'column_3',
            triple_columns_1, triple_columns_3,
            'WB+Яндекс'
        )

        result['matches_2_3'] = clean_pair_matches(
            result.get('matches_2_3', []),
            'column_2', 'column_3',
            triple_columns_2, triple_columns_3,
            'Ozon+Яндекс'
        )

        # === ШАГ 4: Итоговая статистика ===
        total_removed = (
            (original_1_2 - len(result['matches_1_2'])) +
            (original_1_3 - len(result['matches_1_3'])) +
            (original_2_3 - len(result['matches_2_3']))
        )

        if total_removed > 0:
            print(f"[DEDUPE] ✅ Итого удалено из парных: {total_removed}")
        else:
            print(f"[DEDUPE] ✅ Дубликатов не найдено")

        return result

    def _build_prompt(
        self,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str]
    ) -> str:
        """Формирует промпт для AI (первый проход)"""
        mandatory_text = "\n".join([
            f"- Файл 1: '{m['column_1']}' ↔ Файл 2: '{m['column_2']}' ↔ Файл 3: '{m['column_3']}' ({m['description']})"
            for m in MANDATORY_MATCHES
        ])

        # Используем промпт из файла column_matching.txt
        return self.column_matching_template.format(
            mandatory_text=mandatory_text,
            columns_1=json.dumps(columns_1, ensure_ascii=False, indent=2),
            columns_2=json.dumps(columns_2, ensure_ascii=False, indent=2),
            columns_3=json.dumps(columns_3, ensure_ascii=False, indent=2)
        )

    async def _call_ai(self, prompt: str) -> str:
        """
        Вызывает AI API с ограничением параллельных запросов и retry.

        При временных ошибках (rate limit, 5xx, таймаут сети) повторяет
        запрос с экспоненциальным backoff и jitter ±30%.

        Retry применяется:
            - RateLimitError (429) — временный rate limit OpenRouter
            - APIStatusError 5xx   — временная недоступность сервера
            - httpx.TimeoutException / httpx.NetworkError — сетевые сбои

        Retry НЕ применяется:
            - APIStatusError 4xx (кроме 429) — ошибка запроса, повтор бесполезен
            - AuthenticationError            — неверный ключ API

        Returns:
            Текст ответа от AI.

        Raises:
            Exception: если все попытки исчерпаны или ошибка не подлежит retry.
        """
        async with self._semaphore:
            last_exception: Optional[Exception] = None

            for attempt in range(self._RETRY_MAX_ATTEMPTS):
                try:
                    response = await self.client.chat.completions.create(
                        model=AI_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=AI_TEMPERATURE,
                    )
                    return response.choices[0].message.content

                except RateLimitError as e:
                    # 429 — временный rate limit, всегда retry
                    last_exception = e
                    logger.warning(
                        "OpenRouter rate limit (попытка %d/%d): %s",
                        attempt + 1, self._RETRY_MAX_ATTEMPTS, e,
                    )

                except APIStatusError as e:
                    # 5xx — серверная ошибка, retry
                    # 4xx (кроме 429) — ошибка клиента, повтор не поможет
                    if e.status_code < 500:
                        logger.error(
                            "Ошибка AI API %d, retry не применяется: %s",
                            e.status_code, e,
                        )
                        raise
                    last_exception = e
                    logger.warning(
                        "Ошибка сервера AI %d (попытка %d/%d): %s",
                        e.status_code, attempt + 1, self._RETRY_MAX_ATTEMPTS, e,
                    )

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    # Сетевые сбои — retry
                    last_exception = e
                    logger.warning(
                        "Сетевая ошибка AI (попытка %d/%d): %s",
                        attempt + 1, self._RETRY_MAX_ATTEMPTS, e,
                    )

                # На последней попытке не ждём — сразу выходим из цикла
                if attempt >= self._RETRY_MAX_ATTEMPTS - 1:
                    break

                # Экспоненциальный backoff: 2с → 4с → (не дойдёт до 3-й паузы)
                delay = min(
                    self._RETRY_BASE_DELAY * (2 ** attempt),
                    self._RETRY_MAX_DELAY,
                )
                # Jitter ±30% — предотвращает одновременный retry нескольких задач
                jitter = delay * self._RETRY_JITTER * (random.random() * 2 - 1)
                wait = max(0.1, delay + jitter)

                logger.info(
                    "Повтор AI-запроса через %.1f сек (попытка %d/%d)...",
                    wait, attempt + 1, self._RETRY_MAX_ATTEMPTS,
                )
                await asyncio.sleep(wait)

            raise last_exception

    def _parse_response(self, response_text: str) -> Dict:
        """Парсит ответ от AI"""
        try:
            # 1. Сначала пробуем распарсить как есть (если это чистый JSON)
            return json.loads(response_text.strip())
        except json.JSONDecodeError:
            pass

        # 2. Ищем блоки кода ```json``` или ```
        json_code_block = re.search(r'```(?:json)?\s*({.*?})\s*```', response_text, re.DOTALL)
        if json_code_block:
            try:
                return json.loads(json_code_block.group(1))
            except json.JSONDecodeError:
                pass

        # 3. Ищем первую открывающую { и соответствующую ей закрывающую }
        try:
            # Находим первую {
            start_index = response_text.find('{')
            if start_index != -1:
                # Ищем баланс скобок, чтобы найти правильную закрывающую }
                balance = 0
                for i in range(start_index, len(response_text)):
                    char = response_text[i]
                    if char == '{':
                        balance += 1
                    elif char == '}':
                        balance -= 1
                        if balance == 0:
                            json_str = response_text[start_index : i+1]
                            return json.loads(json_str)
        except Exception:
            pass

        # 4. Если ничего не помогло - логируем и падаем
        print("[!] Ошибка при парсинге ответа AI")
        print("Raw response start:", response_text[:200])
        print("Raw response end:", response_text[-200:])
        raise ValueError("Не удалось распарсить ответ AI (JSON не найден)")

    async def match_value_with_list(
        self,
        value: str,
        allowed_values: List[str],
        column_name: str = "неизвестный столбец"
    ) -> Optional[str]:
        """
        Сопоставляет значение со списком допустимых через AI

        Args:
            value: значение для проверки
            allowed_values: список validation значений
            column_name: название столбца

        Returns:
            Сопоставленное значение или None
        """
        if not value or not allowed_values:
            return None

        # Функция нормализации
        def normalize(text: str) -> str:
            """Нормализует текст: нижний регистр, ё→е"""
            return text.lower().replace('ё', 'е').strip()

        # Нормализуем входное значение
        value_normalized = normalize(value)

        # СНАЧАЛА проверяем точное совпадение с нормализацией
        for allowed in allowed_values:
            if normalize(allowed) == value_normalized:
                print(f"   [normalize] Точное совпадение: '{value}' → '{allowed}'")
                return allowed

        # Проверяем частичное совпадение (одно слово содержится в другом)
        value_words = set(value_normalized.split())
        for allowed in allowed_values:
            allowed_words = set(normalize(allowed).split())

            # Если все слова из value есть в allowed
            if value_words.issubset(allowed_words):
                print(f"   [normalize] Частичное совпадение: '{value}' → '{allowed}'")
                return allowed

        # Если не нашли - спрашиваем AI
        print(f"   [AI] Отправляю запрос для '{value}'...")

        # Форматируем список для промпта
        allowed_values_formatted = "\n".join(f"- {v}" for v in allowed_values)

        # Используем промпт из файла value_validation.txt
        prompt = self.value_validation_template.format(
            column_name=column_name,
            value=value,
            allowed_values=allowed_values_formatted
        )

        try:
            response = await self._call_ai(prompt)
            matched = response.strip()

            # Проверяем что AI вернул что-то из списка
            if matched in allowed_values:
                return matched

            # Проверяем с нормализацией
            for allowed in allowed_values:
                if normalize(matched) == normalize(allowed):
                    return allowed

            # Если AI вернул "НЕТ_СОВПАДЕНИЯ" или что-то не из списка
            if "НЕТ" in matched.upper() or matched not in allowed_values:
                return None

            return None

        except Exception as e:
            print(f"   [ERROR] Ошибка AI: {e}")
            return None

    def _add_mandatory_matches(
        self,
        result: Dict,
        columns_1: List[str],
        columns_2: List[str],
        columns_3: List[str]
    ) -> Dict:
        """Добавляет обязательные совпадения в результат"""
        matches_all = result.get('matches_all_three', [])
        matches_1_2 = result.get('matches_1_2', [])
        matches_1_3 = result.get('matches_1_3', [])
        matches_2_3 = result.get('matches_2_3', [])

        for mandatory in MANDATORY_MATCHES:
            col_1 = ExcelReader.find_column_fuzzy(columns_1, mandatory['column_1'])
            col_2 = ExcelReader.find_column_fuzzy(columns_2, mandatory['column_2']) if mandatory['column_2'] else None
            col_3 = ExcelReader.find_column_fuzzy(columns_3, mandatory['column_3']) if mandatory['column_3'] else None

            if col_1 and col_2 and col_3:
                exists = any(
                    m.get('column_1') == col_1 or m.get('column_2') == col_2 or m.get('column_3') == col_3
                    for m in matches_all
                )

                if not exists:
                    matches_all.insert(0, {
                        "column_1": col_1,
                        "column_2": col_2,
                        "column_3": col_3,
                        "confidence": 1.0,
                        "mandatory": True
                    })
                    print(f"[+] Добавлено обязательное совпадение: {mandatory['description']}")

            elif col_1 and col_2 and not col_3:
                exists = any(
                    m.get('column_1') == col_1 or m.get('column_2') == col_2
                    for m in matches_1_2
                )

                if not exists:
                    matches_1_2.insert(0, {
                        "column_1": col_1,
                        "column_2": col_2,
                        "confidence": 1.0,
                        "mandatory": True
                    })

            elif col_1 and col_3 and not col_2:
                exists = any(
                    m.get('column_1') == col_1 or m.get('column_3') == col_3
                    for m in matches_1_3
                )

                if not exists:
                    matches_1_3.insert(0, {
                        "column_1": col_1,
                        "column_3": col_3,
                        "confidence": 1.0,
                        "mandatory": True
                    })

            elif col_2 and col_3 and not col_1:
                exists = any(
                    m.get('column_2') == col_2 or m.get('column_3') == col_3
                    for m in matches_2_3
                )

                if not exists:
                    matches_2_3.insert(0, {
                        "column_2": col_2,
                        "column_3": col_3,
                        "confidence": 1.0,
                        "mandatory": True
                    })

        result['matches_all_three'] = matches_all
        result['matches_1_2'] = matches_1_2
        result['matches_1_3'] = matches_1_3
        result['matches_2_3'] = matches_2_3

        return result
