"""
Вспомогательные функции бота
"""
import os
import logging
from pathlib import Path
from typing import Optional, Tuple

from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger('bot_utils')


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


async def download_xml_from_telegram(
    bot,
    message,
    user_id: int,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Скачивает XML-документ из Telegram с обработкой ошибки большого файла.

    Args:
        bot: экземпляр Bot
        message: сообщение с документом
        user_id: ID пользователя

    Returns:
        (file_path, error_message):
            - (путь_к_файлу, None) при успехе
            - (None, текст_ошибки) при ошибке
    """
    file_name = message.document.file_name or "catalog.xml"

    downloads_dir = Path("downloads") / str(user_id)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    xml_path = str(downloads_dir / file_name)

    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, xml_path)
        return xml_path, None

    except TelegramBadRequest as e:
        error_text = str(e)
        if "file is too big" in error_text.lower():
            logger.warning(
                f"XML файл слишком большой для Telegram API (user={user_id}, "
                f"file={file_name}, size={message.document.file_size} байт)"
            )
            return None, "file_too_big"
        else:
            logger.error(f"Ошибка Telegram при скачивании XML: {e}", exc_info=True)
            return None, f"Ошибка Telegram: {e}"

    except Exception as e:
        logger.error(f"Неожиданная ошибка скачивания XML: {e}", exc_info=True)
        return None, f"Ошибка: {e}"


async def download_file_by_url(
    url: str,
    user_id: int,
    filename: str = "catalog.xml",
) -> Tuple[Optional[str], Optional[str]]:
    """
    Скачивает файл по прямой HTTP/HTTPS ссылке.

    Используется как альтернатива для файлов > 20 МБ,
    которые Telegram Bot API не может отдать через get_file().

    Args:
        url: прямая ссылка на файл
        user_id: ID пользователя (для папки downloads)
        filename: имя для сохранения файла

    Returns:
        (file_path, error_message):
            - (путь_к_файлу, None) при успехе
            - (None, текст_ошибки) при ошибке
    """
    import aiohttp

    downloads_dir = Path("downloads") / str(user_id)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    file_path = str(downloads_dir / filename)

    try:
        timeout = aiohttp.ClientTimeout(total=300)  # 5 минут на скачивание
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None, (
                        f"Сервер вернул ошибку {response.status}. "
                        f"Проверь ссылку и попробуй снова."
                    )

                # Проверяем размер (лимит 200 МБ)
                content_length = response.content_length
                if content_length and content_length > 200 * 1024 * 1024:
                    return None, "Файл слишком большой (> 200 МБ)."

                # Скачиваем чанками
                with open(file_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(8192):
                        f.write(chunk)

        # Проверяем что файл не пустой
        if os.path.getsize(file_path) == 0:
            os.remove(file_path)
            return None, "Скачанный файл пуст. Проверь ссылку."

        logger.info(
            f"Файл скачан по URL: {file_path} "
            f"({os.path.getsize(file_path)} байт)"
        )
        return file_path, None

    except aiohttp.ClientError as e:
        logger.error(f"Ошибка скачивания по URL '{url}': {e}", exc_info=True)
        return None, f"Не удалось скачать файл: {e}"

    except Exception as e:
        logger.error(f"Неожиданная ошибка скачивания по URL: {e}", exc_info=True)
        return None, f"Ошибка: {e}"
