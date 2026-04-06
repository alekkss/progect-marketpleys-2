"""
Модуль для работы с данными из XML файлов каталога (МВидео/xway).

Извлекает список полей офферов из XML: фиксированные теги (<vendor>, <barcode> и т.д.)
и все уникальные <param name="..."> по всем офферам.

Принцип Single Responsibility: только чтение и парсинг XML, без бизнес-логики.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from collections import OrderedDict

from utils.logger_config import setup_logger

logger = setup_logger('xml_reader')


# Фиксированные теги оффера, которые считаются «столбцами» XML.
# Порядок важен — он определяет порядок в итоговом списке полей.
OFFER_FIXED_TAGS: List[str] = [
    'name',
    'vendor',
    'vendorCode',
    'barcode',
    'description',
    'price',
    'oldprice',
    'currencyId',
    'categoryId',
    'weight',
    'dimensions',
    'vat',
    'tnved',
    'picture',
    'video',
    'url',
    'count',
    'lifeCycle',
    'linked-parts-count',
]


class XmlReader:
    """Класс для чтения полей офферов из XML файлов каталога."""

    @staticmethod
    def get_field_names(file_path: str) -> List[str]:
        """
        Извлекает список уникальных полей (столбцов) из XML файла.

        Собирает фиксированные теги офферов и все уникальные param name.
        Порядок: сначала фиксированные теги (в порядке OFFER_FIXED_TAGS),
        затем param name в порядке первого появления.

        Args:
            file_path: путь к XML файлу

        Returns:
            Список уникальных названий полей

        Raises:
            FileNotFoundError: если файл не найден
            ValueError: если файл не содержит офферов или имеет некорректный формат
        """
        file_p = Path(file_path)
        logger.info(f"Чтение полей из XML файла: {file_p.name}")

        if not file_p.exists():
            raise FileNotFoundError(f"XML файл не найден: {file_path}")

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML файла {file_p.name}: {e}", exc_info=True)
            raise ValueError(
                f"Не удалось прочитать XML файл '{file_p.name}'. "
                f"Файл повреждён или имеет некорректный формат."
            ) from e

        # Ищем все офферы в XML (поддерживаем разные структуры)
        offers = root.findall('.//offer')

        if not offers:
            raise ValueError(
                f"XML файл '{file_p.name}' не содержит элементов <offer>. "
                f"Проверьте структуру файла."
            )

        logger.info(f"Найдено офферов: {len(offers)}")

        # Собираем фиксированные теги, которые реально встречаются в офферах
        found_fixed_tags: OrderedDict = OrderedDict()
        # Собираем уникальные param name в порядке первого появления
        found_params: OrderedDict = OrderedDict()

        for offer in offers:
            # Проверяем фиксированные теги
            for tag_name in OFFER_FIXED_TAGS:
                if tag_name in found_fixed_tags:
                    continue  # Уже нашли этот тег
                element = offer.find(tag_name)
                if element is not None:
                    found_fixed_tags[tag_name] = True

            # Собираем param name
            for param in offer.findall('param'):
                param_name = param.get('name')
                if param_name and param_name not in found_params:
                    found_params[param_name] = True

        # Формируем итоговый список: сначала фиксированные, затем param
        result: List[str] = []

        for tag_name in found_fixed_tags:
            result.append(f"[XML] {tag_name}")

        for param_name in found_params:
            result.append(f"[XML param] {param_name}")

        logger.info(
            f"Извлечено полей: {len(result)} "
            f"(фиксированных тегов: {len(found_fixed_tags)}, "
            f"param: {len(found_params)})"
        )

        return result

    @staticmethod
    def get_offer_data(file_path: str) -> List[Dict[str, Any]]:
        """
        Извлекает данные всех офферов из XML файла.

        Каждый оффер представлен как словарь, где ключи —
        фиксированные теги и param name, значения — текстовое содержимое.

        Для тегов с множественными значениями (picture, video) —
        значения объединяются через точку с запятой.

        Args:
            file_path: путь к XML файлу

        Returns:
            Список словарей с данными офферов
        """
        file_p = Path(file_path)
        logger.info(f"Извлечение данных офферов из XML: {file_p.name}")

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML: {e}", exc_info=True)
            raise ValueError(f"Не удалось прочитать XML файл: {e}") from e

        offers = root.findall('.//offer')
        if not offers:
            return []

        # Теги, у которых может быть несколько значений в одном оффере
        multi_value_tags = {'picture', 'video'}

        result: List[Dict[str, Any]] = []

        for offer in offers:
            offer_data: Dict[str, Any] = {}

            # Атрибут id оффера
            offer_id = offer.get('id')
            if offer_id:
                offer_data['offer_id'] = offer_id

            # Фиксированные теги
            for tag_name in OFFER_FIXED_TAGS:
                if tag_name in multi_value_tags:
                    # Собираем все значения тега (например, несколько <picture>)
                    elements = offer.findall(tag_name)
                    if elements:
                        values = [
                            el.text.strip()
                            for el in elements
                            if el.text and el.text.strip()
                        ]
                        if values:
                            offer_data[f"[XML] {tag_name}"] = '; '.join(values)
                else:
                    element = offer.find(tag_name)
                    if element is not None and element.text:
                        offer_data[f"[XML] {tag_name}"] = element.text.strip()

            # Param
            for param in offer.findall('param'):
                param_name = param.get('name')
                if param_name and param.text:
                    offer_data[f"[XML param] {param_name}"] = param.text.strip()

            result.append(offer_data)

        logger.info(f"Извлечено офферов с данными: {len(result)}")
        return result

    @staticmethod
    def get_categories(file_path: str) -> Dict[str, str]:
        """
        Извлекает словарь категорий из XML файла.

        Args:
            file_path: путь к XML файлу

        Returns:
            Словарь {category_id: category_name}
        """
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError:
            return {}

        categories: Dict[str, str] = {}
        for cat in root.findall('.//category'):
            cat_id = cat.get('id')
            cat_name = cat.text
            if cat_id and cat_name:
                categories[cat_id] = cat_name.strip()

        return categories

    @staticmethod
    def get_offer_count(file_path: str) -> int:
        """
        Быстро подсчитывает количество офферов в XML файле.

        Args:
            file_path: путь к XML файлу

        Returns:
            Количество офферов
        """
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            return len(root.findall('.//offer'))
        except (ET.ParseError, FileNotFoundError):
            return 0

    @staticmethod
    def search_categories(file_path: str, query: str) -> List[Dict[str, Any]]:
        """
        Ищет категории по текстовому запросу (нечёткий поиск по подстроке).

        Поиск без учёта регистра. Для каждой найденной категории
        подсчитывает количество офферов, принадлежащих ей.

        Args:
            file_path: путь к XML файлу
            query: поисковый запрос (например, "Холодильник")

        Returns:
            Список словарей с информацией о найденных категориях:
            [
                {
                    'id': '16530',
                    'name': 'Холодильники двухкамерные',
                    'parent_id': '16529',
                    'offer_count': 42
                },
                ...
            ]
            Отсортирован по количеству офферов (убывание).
        """
        if not query or not query.strip():
            return []

        query_lower = query.strip().lower()

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML при поиске категорий: {e}", exc_info=True)
            return []

        # Собираем все категории с parentId
        all_categories: Dict[str, Dict[str, Any]] = {}
        for cat in root.findall('.//category'):
            cat_id = cat.get('id')
            cat_name = cat.text
            if cat_id and cat_name:
                all_categories[cat_id] = {
                    'id': cat_id,
                    'name': cat_name.strip(),
                    'parent_id': cat.get('parentId', ''),
                    'offer_count': 0,
                }

        if not all_categories:
            logger.warning("XML файл не содержит категорий")
            return []

        # Подсчитываем офферы для каждой категории
        for offer in root.findall('.//offer'):
            cat_id_el = offer.find('categoryId')
            if cat_id_el is not None and cat_id_el.text:
                cat_id = cat_id_el.text.strip()
                if cat_id in all_categories:
                    all_categories[cat_id]['offer_count'] += 1

        # Фильтруем по запросу: ищем подстроку без учёта регистра
        matched: List[Dict[str, Any]] = []
        for cat_data in all_categories.values():
            cat_name_lower = cat_data['name'].lower()
            if query_lower in cat_name_lower:
                matched.append(cat_data)

        # Сортируем: сначала категории с офферами (по убыванию), потом без офферов
        matched.sort(key=lambda c: c['offer_count'], reverse=True)

        logger.info(
            f"Поиск категорий по '{query}': найдено {len(matched)} "
            f"из {len(all_categories)} всего"
        )

        return matched

    @staticmethod
    def get_offer_data_by_categories(
        file_path: str,
        category_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        """
        Извлекает данные только тех офферов, которые принадлежат указанным категориям.

        Логика аналогична get_offer_data(), но с фильтрацией по categoryId.

        Args:
            file_path: путь к XML файлу
            category_ids: множество ID категорий для фильтрации

        Returns:
            Список словарей с данными отфильтрованных офферов
        """
        if not category_ids:
            logger.warning("Пустой набор категорий — возвращаю все офферы")
            return XmlReader.get_offer_data(file_path)

        file_p = Path(file_path)
        logger.info(
            f"Извлечение офферов из XML по категориям: {file_p.name}, "
            f"категорий: {len(category_ids)}"
        )

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML: {e}", exc_info=True)
            raise ValueError(f"Не удалось прочитать XML файл: {e}") from e

        offers = root.findall('.//offer')
        if not offers:
            return []

        multi_value_tags = {'picture', 'video'}
        result: List[Dict[str, Any]] = []
        skipped_count = 0

        for offer in offers:
            # Проверяем categoryId оффера
            cat_id_el = offer.find('categoryId')
            if cat_id_el is None or not cat_id_el.text:
                skipped_count += 1
                continue

            offer_cat_id = cat_id_el.text.strip()
            if offer_cat_id not in category_ids:
                skipped_count += 1
                continue

            # Оффер принадлежит выбранной категории — извлекаем данные
            offer_data: Dict[str, Any] = {}

            offer_id = offer.get('id')
            if offer_id:
                offer_data['offer_id'] = offer_id

            # Фиксированные теги
            for tag_name in OFFER_FIXED_TAGS:
                if tag_name in multi_value_tags:
                    elements = offer.findall(tag_name)
                    if elements:
                        values = [
                            el.text.strip()
                            for el in elements
                            if el.text and el.text.strip()
                        ]
                        if values:
                            offer_data[f"[XML] {tag_name}"] = '; '.join(values)
                else:
                    element = offer.find(tag_name)
                    if element is not None and element.text:
                        offer_data[f"[XML] {tag_name}"] = element.text.strip()

            # Param
            for param in offer.findall('param'):
                param_name = param.get('name')
                if param_name and param.text:
                    offer_data[f"[XML param] {param_name}"] = param.text.strip()

            result.append(offer_data)

        logger.info(
            f"Отфильтровано офферов: {len(result)} из {len(offers)} "
            f"(пропущено: {skipped_count})"
        )

        return result
