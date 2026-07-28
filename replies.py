"""Shared handling of an incoming subscriber reply.

Used by the MAX userbot inbound path (and reusable elsewhere): given a
contact who wrote in, store the reply and forward it to the right owner(s)
using the same routing rules as the rest of the app (owner who last sent
an SOS; if ambiguous, everyone in the ambiguous set).
"""

import logging

import db
import messaging

logger = logging.getLogger(__name__)


async def _deliver_to_owner(owner: db.Owner, contact_id: int, platform: str,
                            fallback_name: str, text: str) -> None:
    display = await db.get_contact_name(owner.chat_id, contact_id, platform) or fallback_name
    await db.add_reply(owner.chat_id, contact_id, display, text, platform)
    try:
        await messaging.send(owner.platform, owner.chat_id, f"💬 Ответ от {display}:\n{text}")
    except Exception:
        logger.exception("Failed to forward reply to owner %d", owner.chat_id)


async def handle_incoming(contact_id: int, platform: str, sender_name: str, text: str) -> int:
    """Route an incoming reply to its owner(s). Returns how many were notified."""
    owners = await db.get_owners_for_contact(contact_id, platform)
    if not owners:
        return 0
    target, ask_among = await db.route_reply(contact_id, owners, platform)
    targets = [target] if target is not None else ask_among
    for owner in targets:
        await _deliver_to_owner(owner, contact_id, platform, sender_name, text)
    return len(targets)
