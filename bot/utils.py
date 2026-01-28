"""
Вспомогательные функции бота
"""
import os


def detect_marketplace(filename: str) -> str:
    """
    Определяет маркетплейс по имени файла
    
    Args:
        filename: имя файла
        
    Returns:
        marketplace или None
    """
    fn = filename.lower()
    
    if 'wb' in fn or 'wildberries' in fn:
        return 'wildberries'
    elif 'ozon' in fn or 'озон' in fn:
        return 'ozon'
    elif 'yandex' in fn or 'яндекс' in fn or 'market' in fn:
        return 'yandex'
    
    return None


async def download_file(bot, message, user_id: int) -> tuple:
    """
    Скачивает файл от пользователя
    
    Returns:
        (file_path, file_name, marketplace) или (None, None, None) при ошибке
    """
    file = await bot.get_file(message.document.file_id)
    file_name = message.document.file_name
    
    os.makedirs(f"uploads/{user_id}", exist_ok=True)
    file_path = f"uploads/{user_id}/{file_name}"
    await bot.download_file(file.file_path, file_path)
    
    marketplace = detect_marketplace(file_name)
    
    return file_path, file_name, marketplace
