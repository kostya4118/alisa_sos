import logging
from datetime import datetime

from aiogram import Bot

import storage
from config import settings

logger = logging.getLogger(__name__)


async def send_sos(bot: Bot, extra_message: str = "") -> tuple[int, int]:
    """
    Sends SOS to all contacts.
    Returns (sent_count, failed_count).
    """
    contacts = await storage.get_contacts()
    if not contacts:
        logger.warning("SOS triggered but no contacts registered")
        return 0, 0

    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    text = (
        f"{settings.sos_message}\n\n"
        f"👤 От: {settings.owner_name}\n"
        f"🕐 Время: {timestamp}"
    )
    if extra_message:
        text += f"\n\n💬 Сообщение: {extra_message}"

    sent = 0
    failed = 0
    for chat_id, name in contacts.items():
        try:
            await bot.send_message(chat_id, text)
            sent += 1
            logger.info("SOS sent to %s (%d)", name, chat_id)
        except Exception as e:
            failed += 1
            logger.error("Failed to send SOS to %s (%d): %s", name, chat_id, e)

    await bot.send_message(
        settings.admin_chat_id,
        f"📊 SOS разослан: ✅ {sent} получили, ❌ {failed} ошибок",
    )
    return sent, failed
