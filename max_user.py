"""MAX delivery via a PyMax **userbot** (a real MAX account), used instead of
the official MAX bot.

Why: the official MAX bot needs a verified RU legal entity and subscribers
must "start" it. A userbot logs in as an ordinary MAX account (phone + SMS)
and can message any MAX user directly — so owners just add a MAX subscriber
by phone.

⚠️ Unofficial internal MAX API (PyMax). It can break on MAX updates and the
account may be banned — treat this as a best-effort SECONDARY channel, not a
lifeline. Enabled only when ``MAX_USERBOT_PHONE`` is set.

Login: on first start PyMax asks for an SMS code; we ask the admin for it in
Telegram (``/maxcode 1234``). The session is saved under ``data/`` and reused.
"""

import asyncio
import logging
import os

import db
from config import settings

logger = logging.getLogger(__name__)

_client = None            # pymax.Client
_ready = asyncio.Event()
_me_id: int | None = None

# SMS-code handshake with the admin (via the Telegram bot)
_code_future: "asyncio.Future[str] | None" = None
_code_phone: str = ""


def enabled() -> bool:
    return bool(settings.max_userbot_phone)


def is_ready() -> bool:
    return _ready.is_set() and _client is not None


# ---------------------------------------------------------------------------
# SMS code provider — asks the admin in Telegram instead of the console
# ---------------------------------------------------------------------------

class _BotSmsCodeProvider:
    async def get_code(self, phone: str) -> str:
        global _code_future, _code_phone
        _code_phone = phone
        loop = asyncio.get_event_loop()
        _code_future = loop.create_future()
        logger.info("MAX userbot: SMS code requested for %s", phone)
        await _notify_admin(
            f"🔐 MAX-аккаунт: на номер {phone} придёт SMS-код.\n"
            "Пришлите его командой:\n/maxcode 1234"
        )
        code = await _code_future
        return code.strip()


def submit_code(code: str) -> bool:
    """Feed the SMS code the admin sent. Returns True if it was awaited."""
    if _code_future is not None and not _code_future.done():
        _code_future.set_result(code)
        return True
    return False


async def _notify_admin(text: str) -> None:
    if not settings.admin_chat_id:
        return
    try:
        import messaging
        tg = messaging.get_telegram_bot()
        if tg is not None:
            await tg.send_message(settings.admin_chat_id, text)
    except Exception:
        logger.exception("failed to notify admin about MAX login")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _work_dir() -> str:
    d = os.path.join(os.path.dirname(settings.db_path) or ".", "max_session")
    os.makedirs(d, exist_ok=True)
    return d


def _extract_my_id(me) -> int | None:
    """Own user id from the Profile — tolerant to PyMax's shape."""
    for path in ("id", "user_id"):
        v = getattr(me, path, None)
        if isinstance(v, int):
            return v
    contact = getattr(me, "contact", None)
    if contact is not None:
        for path in ("id", "user_id"):
            v = getattr(contact, path, None)
            if isinstance(v, int):
                return v
    return None


async def run() -> None:
    """Start the userbot and keep it connected (background task)."""
    global _client, _me_id
    from pymax import Client

    _client = Client(
        phone=settings.max_userbot_phone,
        session_name="userbot.db",
        work_dir=_work_dir(),
        sms_code_provider=_BotSmsCodeProvider(),
    )

    @_client.on_start()
    async def _on_start() -> None:  # noqa: ANN202
        global _me_id
        me = _client.me
        logger.info("MAX userbot connected. me=%r", me)
        _me_id = _extract_my_id(me)
        if _me_id is None:
            logger.error("MAX userbot: could not read own user id from profile %r", me)
        _ready.set()
        await _notify_admin("✅ MAX-аккаунт подключён. Дозвон/сообщения в MAX активны.")

    logger.info("Starting MAX userbot (%s)...", settings.max_userbot_phone)
    try:
        await _client.start()   # connects, authorizes, then runs the receive loop
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("MAX userbot stopped with an error")
        _ready.clear()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

async def _resolve_chat_id(phone: str) -> int:
    user = await _client.search_by_phone(phone)
    their_id = _extract_my_id(user) if not hasattr(user, "id") else getattr(user, "id", None)
    their_id = getattr(user, "id", None) or getattr(user, "user_id", None) or their_id
    if their_id is None:
        raise RuntimeError(f"MAX: не удалось определить id пользователя {phone}")
    if _me_id is None:
        raise RuntimeError("MAX: неизвестен собственный id аккаунта")
    return await _client.get_chat_id(_me_id, their_id)


async def send(target, text: str) -> None:
    """Send a MAX message. ``target`` = phone string (+7…) or a chat_id int."""
    if not is_ready():
        raise RuntimeError("MAX userbot не готов (нет входа в аккаунт)")
    if isinstance(target, str) and target.strip().startswith("+"):
        chat_id = await _resolve_chat_id(target.strip())
    else:
        chat_id = int(target)
    await _client.send_message(chat_id, text)
