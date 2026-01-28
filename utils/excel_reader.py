"""
Модуль для чтения данных из Excel файлов
"""
from openpyxl import load_workbook
from typing import List
import sys
from pathlib import Path
from utils.logger_config import setup_logger
sys.path.insert(0, str(Path(__file__).parent.parent))


class ExcelReader:
    """Класс для чтения столбцов из Excel файлов"""
    
    @staticmethod
    def get_column_names(file_path: str, sheet_name: str, row_number: int) -> List[str]:
        """
        Получает названия столбцов из указанной строки Excel файла
        
        Args:
            file_path: путь к файлу Excel
            sheet_name: название листа
            row_number: номер строки (1-индексация)
        
        Returns:
            Список названий столбцов
        """
        workbook = load_workbook(file_path)
        worksheet = workbook[sheet_name]
        
        # Получаем все значения из указанной строки
        row_values = []
        for cell in worksheet[row_number]:
            if cell.value is not None:
                row_values.append(str(cell.value).strip())
            else:
                row_values.append("")
        
        workbook.close()
        return row_values
    
    @staticmethod
    def find_column_fuzzy(columns: List[str], search_term: str) -> str:
        """
        Ищет столбец по части названия (нечеткий поиск)
        
        Args:
            columns: список названий столбцов
            search_term: искомый термин
        
        Returns:
            Найденное название столбца или None
        """
        if not search_term:
            return None
        
        # Точное совпадение
        if search_term in columns:
            return search_term
        
        # Ищем по ключевым словам
        search_lower = search_term.lower()
        for col in columns:
            if search_lower in col.lower():
                return col
        
        return None
