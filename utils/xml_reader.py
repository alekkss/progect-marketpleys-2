"""
Модуль для чтения данных из XML файлов каталога (МВидео/xway).

Извлекает список полей офферов из XML: фиксированные теги (<vendor>, <barcode> и т.д.)
и все уникальные <param name="..."> по всем офферам.

Принцип Single Responsibility: только чтение и парсинг XML, без бизнес-логики.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional
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
