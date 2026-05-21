import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot

import db

logger = logging.getLogger(__name__)


async def send_sos(
    bot: Bot,
    owner: db.Owner,
    extra_message: str = "",
    contact_ids: list[int] | None = None,
) -> tuple[int, int]:
    """
    Sends SOS on behalf of owner to their contacts.
    If contact_ids is None, sends to all owner's contacts.
    Returns (sent_count, failed_count).
    """
    all_contacts = await db.get_contacts(owner.chat_id)

    if contact_ids is not None:
        contacts = {cid: all_contacts[cid] for cid in contact_ids if cid in all_contacts}
    else:
        contacts = all_contacts

    if not contacts:
        logger.warning("SOS triggered by owner %d but no contacts", owner.chat_id)
        return 0, 0

    tz = timezone(timedelta(hours=owner.tz_offset))
    timestamp = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")
    text = (
        f"{owner.sos_message}\n\n"
        f"👤 От: {owner.name}\n"
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
            logger.info("SOS sent to %s (%d) for owner %d", name, chat_id, owner.chat_id)
        except Exception as e:
            failed += 1
            logger.error("Failed SOS to %s (%d): %s", name, chat_id, e)

    await bot.send_message(
        owner.chat_id,
        f"📊 SOS разослан: ✅ {sent} получили, ❌ {failed} ошибок",
    )
    return sent, failed
