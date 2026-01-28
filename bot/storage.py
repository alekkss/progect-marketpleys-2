"""
Глобальное хранилище данных бота
"""
from database.database import Database

# Временное хранилище файлов пользователей
user_files = {}

# Временное хранилище для создания схем
user_schemas = {}

# Экземпляр базы данных
db = Database()
