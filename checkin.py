"""Daily check-in / dead-man switch.

Each owner can enable a daily check-in at a chosen local time.
If they don't respond:
  - after 1 hour  → 2nd question
  - after 30 min  → 3rd (final) question
  - after 10 min  → automatic SOS to all contacts
"""

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
import notifier

logger = logging.getLogger(__name__)

_ASK_TEXTS = {
    1: "👋 Ежедневная проверка. Всё в порядке?",
    2: "⚠️ Вы не ответили час назад. Всё хорошо?",
    3: (
        "🚨 Последняя проверка! "
        "Если не ответите в течение 10 минут — SOS уйдёт автоматически."
    ),
}

CHECKIN_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Всё хорошо", callback_data="checkin_ok"),
    InlineKeyboardButton(text="🆘 Нужна помощь", callback_data="checkin_sos"),
]])


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def parse_time(text: str) -> int | None:
    """Parse 'HH:MM' or 'H:MM' → minutes since midnight. None on error."""
    text = text.strip().replace(".", ":")
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except ValueError:
        pass
    return None


def fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def confirm(owner_id: int) -> bool:
    """Reset active check-in to idle. Returns True if there was one."""
    row = await db.get_checkin(owner_id)
    if row and row["state"] not in ("idle", "sos_sent"):
        await db.update_checkin(owner_id, state="idle", attempts=0)
        return True
    return False


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def run_loop(bot: Bot) -> None:
    while True:
        try:
            await _tick(bot)
        except Exception:
            logger.exception("checkin tick error")
        await asyncio.sleep(60)


async def _tick(bot: Bot) -> None:
    now = int(time.time())
    for row in await db.get_pending_checkins():
        owner_id = row["owner_id"]
        tz_offset = row["tz_offset"]
        state = row["state"]
        last_asked_at = row["last_asked_at"]
        time_minutes = row["time_minutes"]

        local_minute = ((now + tz_offset * 3600) % 86400) // 60

        if state == "idle":
            in_window = time_minutes <= local_minute < time_minutes + 2
            not_asked_recently = (now - last_asked_at) > 43200  # >12 h
            if in_window and not_asked_recently:
                await _ask(bot, owner_id, 1, now)

        elif state == "asked_1":
            if now - last_asked_at >= 3600:       # 1 hour
                await _ask(bot, owner_id, 2, now)

        elif state == "asked_2":
            if now - last_asked_at >= 1800:       # 30 min
                await _ask(bot, owner_id, 3, now)

        elif state == "asked_3":
            if now - last_asked_at >= 600:        # 10 min
                await _auto_sos(bot, owner_id, now)


async def _ask(bot: Bot, owner_id: int, attempt: int, now: int) -> None:
    try:
        await bot.send_message(owner_id, _ASK_TEXTS[attempt], reply_markup=CHECKIN_KB)
        await db.update_checkin(
            owner_id, state=f"asked_{attempt}", attempts=attempt, last_asked_at=now
        )
        logger.info("checkin: asked owner=%d attempt=%d", owner_id, attempt)
    except Exception:
        logger.exception("checkin: failed to message owner=%d", owner_id)


async def _auto_sos(bot: Bot, owner_id: int, now: int) -> None:
    owner = await db.get_owner(owner_id)
    if not owner:
        return
    try:
        await bot.send_message(
            owner_id,
            "🆘 Вы не ответили на проверку. Отправляю SOS вашим контактам...",
        )
        sent, failed = await notifier.send_sos(
            bot, owner,
            extra_message="[АВТО-SOS: владелец не ответил на ежедневную проверку]",
        )
        await bot.send_message(
            owner_id,
            f"📊 SOS отправлен: ✅ {sent} доставлено, ❌ {failed} ошибок",
        )
        logger.info("checkin: auto-SOS owner=%d sent=%d failed=%d", owner_id, sent, failed)
    except Exception:
        logger.exception("checkin: auto-SOS failed owner=%d", owner_id)
    finally:
        await db.update_checkin(owner_id, state="sos_sent", last_asked_at=now)
