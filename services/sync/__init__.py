"""
Подпакет sync — компоненты синхронизации данных между маркетплейсами.

Архитектура подпакета следует принципу Single Responsibility:
каждый модуль отвечает ровно за одну задачу.
Все компоненты принимают зависимости через конструктор (Dependency Inversion).
"""

from services.sync.value_converter import ValueConverter
from services.sync.ai_validator import AiValidator
from services.sync.article_aligner import ArticleAligner
from services.sync.dimensions_synchronizer import DimensionsSynchronizer
from services.sync.column_syncer import ColumnSyncer
from services.sync.xml_syncer import XmlSyncer
from services.sync.excel_io import ExcelFileManager

__all__ = [
    "ValueConverter",
    "AiValidator",
    "ArticleAligner",
    "DimensionsSynchronizer",
    "ColumnSyncer",
    "XmlSyncer",
    "ExcelFileManager",
]