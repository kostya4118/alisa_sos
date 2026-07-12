import asyncio
import logging
from datetime import datetime, timezone, timedelta

import db
import messaging
import webhook_out

logger = logging.getLogger(__name__)


async def send_sos(
    owner: db.Owner,
    extra_message: str = "",
    contacts: list[db.Contact] | None = None,
    kind: str = "sos",
) -> tuple[int, int]:
    """
    Sends SOS on behalf of owner to their contacts.
    Each contact is messaged on its own platform (Telegram or MAX).
    If ``contacts`` is None, sends to all owner's contacts.
    ``kind`` labels the outbound webhook payload ("sos" | "test" | "auto").
    Returns (sent_count, failed_count).
    """
    targets = contacts if contacts is not None else await db.get_contacts(owner.chat_id)

    if not targets:
        logger.warning("SOS triggered by owner %d but no contacts", owner.chat_id)
        return 0, 0

    tz = timezone(timedelta(hours=owner.tz_offset))
    now = datetime.now(tz)
    timestamp = now.strftime("%d.%m.%Y %H:%M:%S")
    text = (
        f"{owner.sos_message}\n\n"
        f"👤 От: {owner.name}\n"
        f"🕐 Время: {timestamp}"
    )
    if extra_message:
        text += f"\n\n💬 Сообщение: {extra_message}"

    sent = 0
    failed = 0
    for contact in targets:
        try:
            await messaging.send(contact.platform, contact.chat_id, text)
            sent += 1
            logger.info("SOS sent to %s (%d/%s) for owner %d",
                        contact.name, contact.chat_id, contact.platform, owner.chat_id)
        except Exception as e:
            failed += 1
            logger.error("Failed SOS to %s (%d/%s): %s",
                         contact.name, contact.chat_id, contact.platform, e)
        # Remember who alarmed this contact, so their reply routes back here.
        try:
            await db.record_sos_recipient(contact.chat_id, contact.platform, owner.chat_id)
        except Exception:
            logger.exception("Failed to record SOS recipient %d", contact.chat_id)

    # Fire the owner's outbound webhook (fire-and-forget — never blocks SOS).
    if owner.sos_webhook_url:
        payload = {
            "event": kind,
            "owner": owner.name,
            "owner_id": owner.chat_id,
            "time": now.isoformat(),
            "message": extra_message,
            "recipients": sent,
            "failed": failed,
        }
        try:
            asyncio.create_task(webhook_out.fire(owner.sos_webhook_url, payload))
        except RuntimeError:
            # No running loop (shouldn't happen in the bot) — send inline.
            await webhook_out.fire(owner.sos_webhook_url, payload)

    try:
        await messaging.send(
            owner.platform, owner.chat_id,
            f"📊 SOS разослан: ✅ {sent} получили, ❌ {failed} ошибок",
        )
    except Exception:
        logger.exception("Failed to send SOS summary to owner %d", owner.chat_id)
    return sent, failed
