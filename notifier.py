import asyncio
import logging
from datetime import datetime, timezone, timedelta

import db
import email_out
import messaging
import webhook_out

logger = logging.getLogger(__name__)


def _schedule(coro) -> None:
    """Run a coroutine fire-and-forget (never blocks the caller)."""
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        # No running loop (shouldn't happen in the bot) — run it now.
        asyncio.get_event_loop().run_until_complete(coro)


async def notify_integrations(
    owner: db.Owner,
    *,
    extra_message: str = "",
    target: str | None = None,
    target_phone: str = "",
    recipients: int = 0,
    failed: int = 0,
    kind: str = "sos",
) -> bool:
    """Fire the owner's external SOS integrations (fire-and-forget).

    - Outbound webhook: on every SOS if ``sos_webhook_url`` is set.
    - E-mail "Телефон" bridge: only when a specific ``target`` contact is
      chosen and ``sos_email`` + SMTP are configured.

    ``target`` is the chosen contact's name so the receiving automation can
    call/SMS that person. Returns True if at least one channel fired.
    Never blocks: Alice/Telegram stay responsive.
    """
    fired = False

    if owner.sos_webhook_url:
        tz = timezone(timedelta(hours=owner.tz_offset))
        payload = {
            "event": kind,
            "owner": owner.name,
            "owner_id": owner.chat_id,
            "time": datetime.now(tz).isoformat(),
            "message": extra_message,
            "target": target,
            "phone": target_phone,
            "recipients": recipients,
            "failed": failed,
        }
        _schedule(webhook_out.fire(owner.sos_webhook_url, payload))
        fired = True

    if target and owner.sos_email and email_out.enabled():
        subject = f"SOS: {target}"
        # Machine-parsable lines for the iPhone Shortcut (phone → call/SMS by number).
        body = (
            f"Телефон: {target_phone or 'не задан'}\n"
            f"Сообщение: {extra_message or 'Нужна помощь!'}\n"
            f"Контакт: {target}\n"
            f"От: {owner.name}"
        )
        _schedule(email_out.send(owner.sos_email, subject, body))
        fired = True

    return fired


async def _mirror_to_max(owner: db.Owner, contact: db.Contact, text: str) -> bool:
    """Also deliver an SOS to a phone-linked contact via the MAX userbot.

    For a non-MAX contact that has a phone, sends the same text to that person
    in MAX (resolved by phone) so they get it on both messengers and can reply
    wherever. Records the MAX dialog in ``sos_log`` so their reply routes back.
    Best-effort: never raises. Returns True if the MAX copy was sent.
    """
    if contact.platform == db.MAX or not contact.phone:
        return False
    try:
        import max_user
    except Exception:
        return False
    if not (max_user.enabled() and max_user.is_ready()):
        return False
    try:
        max_chat_id = await db.get_max_chat_id(contact.phone)
        if max_chat_id is None:
            max_chat_id, _ = await max_user.resolve(contact.phone)
        await max_user.send(max_chat_id, text)
        await db.record_sos_recipient(max_chat_id, db.MAX, owner.chat_id)
        logger.info("SOS mirrored to MAX for %s (%s) owner %d",
                    contact.name, contact.phone, owner.chat_id)
        return True
    except Exception:
        logger.info("MAX mirror to %s failed", contact.phone, exc_info=True)
        return False


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
    mirrored = 0
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
        # Dual delivery: also copy to MAX for phone-linked contacts (reply-anywhere).
        if await _mirror_to_max(owner, contact, text):
            mirrored += 1

    # Fire the owner's external integrations (fire-and-forget — never blocks SOS).
    await notify_integrations(owner, extra_message=extra_message,
                              recipients=sent, failed=failed, kind=kind)

    summary = f"📊 SOS разослан: ✅ {sent} получили, ❌ {failed} ошибок"
    if mirrored:
        summary += f"\n📲 продублировано в MAX: {mirrored}"
    try:
        await messaging.send(owner.platform, owner.chat_id, summary)
    except Exception:
        logger.exception("Failed to send SOS summary to owner %d", owner.chat_id)
    return sent, failed
