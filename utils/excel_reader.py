"""
Модуль для чтения данных из Excel файлов
"""
from openpyxl import load_workbook
from typing import List
import sys
from pathlib import Path
from utils.logger_config import setup_logger
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = setup_logger('excel_reader')


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
        # Попытка 1: Стандартная загрузка через openpyxl
        try:
            workbook = load_workbook(file_path)
            worksheet = workbook[sheet_name]
            
            row_values = []
            for cell in worksheet[row_number]:
                if cell.value is not None:
                    row_values.append(str(cell.value).strip())
                else:
                    row_values.append("")
            
            workbook.close()
            return row_values
        
        except TypeError as e:
            # Ошибка data validation (MultiCellRange) - пробуем через data_only
            if 'MultiCellRange' in str(e) or 'expected' in str(e):
                logger.warning(f"Ошибка data validation в файле, пробую альтернативный метод: {e}")
                return ExcelReader._read_with_data_only(file_path, sheet_name, row_number)
            raise
        
        except Exception as e:
            logger.error(f"Ошибка чтения Excel: {e}")
            # Пробуем fallback через pandas
            return ExcelReader._read_with_pandas(file_path, sheet_name, row_number)
    
    @staticmethod
    def _read_with_data_only(file_path: str, sheet_name: str, row_number: int) -> List[str]:
        """
        Читает файл с параметром data_only=True
        """
        try:
            workbook = load_workbook(file_path, data_only=True)
            worksheet = workbook[sheet_name]
            
            row_values = []
            for cell in worksheet[row_number]:
                if cell.value is not None:
                    row_values.append(str(cell.value).strip())
                else:
                    row_values.append("")
            
            workbook.close()
            logger.info(f"Успешно прочитано через data_only: {len(row_values)} столбцов")
            return row_values
        
        except Exception as e:
            logger.warning(f"data_only не помог, пробую pandas: {e}")
            return ExcelReader._read_with_pandas(file_path, sheet_name, row_number)
    
    @staticmethod
    def _read_with_pandas(file_path: str, sheet_name: str, row_number: int) -> List[str]:
        """
        Fallback: читает через pandas (более устойчив к ошибкам)
        """
        try:
            import pandas as pd
            
            # header=row_number-1 потому что pandas использует 0-индексацию
            df = pd.read_excel(
                file_path, 
                sheet_name=sheet_name, 
                header=row_number - 1,
                nrows=0,  # Читаем только заголовки
                engine='openpyxl'
            )
            
            row_values = [str(col).strip() if col is not None else "" for col in df.columns.tolist()]
            logger.info(f"Успешно прочитано через pandas: {len(row_values)} столбцов")
            return row_values
        
        except Exception as e:
            logger.error(f"Не удалось прочитать файл даже через pandas: {e}")
            raise RuntimeError(f"Не удалось прочитать Excel файл: {file_path}. Ошибка: {e}")
    
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