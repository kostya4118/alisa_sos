"""Shared handling of an incoming subscriber reply.

Used by the MAX userbot inbound path (and reusable elsewhere): given a
contact who wrote in, store the reply and forward it to the right owner(s)
using the same routing rules as the rest of the app (owner who last sent
an SOS; if ambiguous, everyone in the ambiguous set).

Owners are matched two ways, then merged (one delivery per owner):
  - direct contact — someone the owner added on this same platform;
  - by phone — a phone-linked contact reached via dual Telegram+MAX
    delivery, so a MAX reply still finds the owner who alarmed them.
"""

import logging

import db
import messaging

logger = logging.getLogger(__name__)


async def handle_incoming(contact_id: int, platform: str, sender_name: str,
                          text: str, phone: str | None = None) -> int:
    """Route an incoming reply to its owner(s). Returns how many were notified.

    ``contact_id`` is the reply channel's id (for MAX, the dialog chat_id).
    ``phone`` (optional) also matches phone-linked contacts on other
    platforms, so a MAX reply reaches an owner who added the person by phone.
    """
    # owner_id -> (Owner, display_name). Direct matches win the name.
    picked: dict[int, tuple[db.Owner, str]] = {}

    for owner in await db.get_owners_for_contact(contact_id, platform):
        name = await db.get_contact_name(owner.chat_id, contact_id, platform) or sender_name
        picked[owner.chat_id] = (owner, name)

    if phone:
        for row in await db.get_contacts_by_phone(phone):
            oid = row["owner_id"]
            if oid in picked:
                continue
            owner = await db.get_owner(oid)
            if owner is not None:
                picked[oid] = (owner, row["name"])

    if not picked:
        return 0

    owners = [ov for ov, _ in picked.values()]
    target, ask_among = await db.route_reply(contact_id, owners, platform)
    chosen = [target] if target is not None else ask_among

    for owner in chosen:
        _, display = picked[owner.chat_id]
        await db.add_reply(owner.chat_id, contact_id, display, text, platform)
        try:
            await messaging.send(owner.platform, owner.chat_id,
                                 f"💬 Ответ от {display}:\n{text}")
        except Exception:
            logger.exception("Failed to forward reply to owner %d", owner.chat_id)

    return len(chosen)
