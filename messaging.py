"""Platform-agnostic outbound messaging.

Routes a message to the correct messenger (Telegram or MAX) based on the
recipient's stored ``platform``. Both bot instances are registered once at
startup via :func:`set_bots`; the rest of the codebase only deals with
``platform`` strings and never imports a specific bot library.
"""

import logging

import db

logger = logging.getLogger(__name__)

_tg_bot = None   # aiogram Bot
_max_bot = None  # maxapi Bot


def set_bots(*, telegram=None, max=None) -> None:
    global _tg_bot, _max_bot
    if telegram is not None:
        _tg_bot = telegram
    if max is not None:
        _max_bot = max


def max_enabled() -> bool:
    return _max_bot is not None


def telegram_enabled() -> bool:
    return _tg_bot is not None


def get_telegram_bot():
    return _tg_bot


def get_max_bot():
    return _max_bot


async def send(platform: str, chat_id: int, text: str) -> None:
    """Send a plain-text message to a recipient on the given platform.

    For MAX, uses the PyMax userbot (by MAX dialog ``chat_id``) when it is
    configured; otherwise the official MAX bot.
    Raises on delivery failure so callers can count successes/failures.
    """
    if platform == db.MAX:
        import max_user
        if max_user.enabled():
            await max_user.send(chat_id, text)
            return
        if _max_bot is None:
            raise RuntimeError("MAX is not configured")
        await _max_bot.send_message(chat_id=chat_id, text=text)
    else:
        if _tg_bot is None:
            raise RuntimeError("Telegram bot is not configured")
        await _tg_bot.send_message(chat_id, text)
