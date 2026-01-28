"""
Точка входа приложения
"""
import asyncio
import sys
from pathlib import Path

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent))

from bot.bot import start_bot

if __name__ == "__main__":
    asyncio.run(start_bot())