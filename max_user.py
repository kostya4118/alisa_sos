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
Telegram (``/maxcode 1234``). If the account has 2FA, the password comes from
``MAX_USERBOT_PASSWORD`` or is requested via ``/maxpassword``. The session is
saved under ``data/`` and reused.

The MAX SMS code expires fast (~1–2 min). If login fails (expired code, rate
limit, dropped connection) the userbot waits with backoff and retries from
scratch — requesting a fresh SMS — instead of dying until the next container
restart.
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

# SMS-code / 2FA-password handshakes with the admin (via the Telegram bot)
_code_future: "asyncio.Future[str] | None" = None
_code_phone: str = ""
_password_future: "asyncio.Future[str] | None" = None
# Last failure reason we alerted the admin about — so we notify once per new
# problem instead of on every retry (was spamming every 10 min all night).
_last_fail_notice: str | None = None


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


# ---------------------------------------------------------------------------
# 2FA password provider — from env, or asks the admin in Telegram
# ---------------------------------------------------------------------------

class _BotPasswordProvider:
    async def get_password(self, hint: str | None = None) -> str:
        # Preferred: password from .env (no interaction).
        if settings.max_userbot_password:
            logger.info("MAX userbot: using 2FA password from env")
            return settings.max_userbot_password
        global _password_future
        loop = asyncio.get_event_loop()
        _password_future = loop.create_future()
        logger.info("MAX userbot: 2FA password requested (hint=%r)", hint)
        h = f"\nПодсказка: {hint}" if hint else ""
        await _notify_admin(
            "🔐 MAX-аккаунт требует пароль 2FA." + h + "\n"
            "Пришлите его командой:\n/maxpassword ВАШ_ПАРОЛЬ\n"
            "(после входа удалите сообщение с паролем)"
        )
        password = await _password_future
        return password.strip()


def submit_password(password: str) -> bool:
    """Feed the 2FA password the admin sent. Returns True if it was awaited."""
    if _password_future is not None and not _password_future.done():
        _password_future.set_result(password)
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


def _clear_session() -> bool:
    """Delete the saved MAX session (+ its WAL/SHM). Returns True if anything
    was removed. Used when MAX invalidates the token (FAIL_LOGIN_TOKEN) so the
    next start falls back to a fresh SMS login instead of looping on a dead
    token."""
    base = os.path.join(_work_dir(), "userbot.db")
    removed = False
    for path in (base, base + "-wal", base + "-shm", base + "-journal"):
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("failed to remove MAX session file %s", path)
    return removed


def _is_stale_session_error(err: Exception) -> bool:
    """MAX rejected the saved session token — need a fresh SMS login."""
    msg = str(err).lower()
    return "fail_login_token" in msg or "login.token" in msg


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


def _attach_handlers(client) -> None:
    """Register on_start / on_message handlers on a fresh client instance."""

    @client.on_start()
    async def _on_start(started) -> None:  # noqa: ANN001, ANN202
        # PyMax calls on_start handlers as ``handler(client)`` — must accept it.
        global _me_id
        me = started.me
        logger.info("MAX userbot connected. me=%r", me)
        _me_id = _extract_my_id(me)
        if _me_id is None:
            logger.error("MAX userbot: could not read own user id from profile %r", me)
        global _last_fail_notice
        await _on_account_check(me)
        _ready.set()
        _last_fail_notice = None  # recovered — allow a fresh alert on next failure
        # No "connected" ping to the admin — notify only when action is needed
        # (SMS code, 2FA password, wrong number, errors). Success is silent.
        logger.info("MAX userbot ready (me_id=%s)", _me_id)

    # Inbound: a MAX user wrote to our service account → treat as a subscriber
    # reply and forward it to the owner(s), matching by the dialog chat_id.
    @client.on_message()
    async def _on_incoming(message, c) -> None:  # noqa: ANN001, ANN202
        try:
            sender = getattr(message, "sender", None)
            text = getattr(message, "text", None)
            chat_id = getattr(message, "chat_id", None)
            if not text or chat_id is None or sender == _me_id:
                return
            # Learn phone↔ids when we can (best-effort, for outbound caching).
            if sender is not None:
                phone = await db.get_max_phone_by_user(sender)
                if not phone:
                    try:
                        u = await c.get_user(sender)
                        raw = getattr(u, "phone", None)
                        if raw:
                            phone = db.normalize_phone(str(raw))
                    except Exception:
                        phone = None
                if phone:
                    await db.set_max_peer(phone, chat_id, sender)
            import replies
            n = await replies.handle_incoming(int(chat_id), db.MAX, "Контакт MAX",
                                              text, phone=phone)
            if n:
                logger.info("MAX inbound chat=%s → %d owner(s)", chat_id, n)
        except Exception:
            logger.exception("MAX inbound handler error")


def _is_transient_auth_error(err: Exception) -> bool:
    """Expired SMS code / attempt-limit / code errors — worth requesting anew."""
    msg = str(err).lower()
    return any(k in msg for k in ("устарел", "attempt.limit", "code", "sms", "устар"))


def _extract_my_phone(me) -> str:
    """Digits of the logged-in account's own phone (from Profile.contact)."""
    contact = getattr(me, "contact", None) or me
    raw = getattr(contact, "phone", None)
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


async def _on_account_check(me) -> None:
    """Guard against a leftover session from a previous service number.

    - If the logged-in account changed, drop cached MAX dialogs (their ids are
      derived from the account's own id and would be wrong for the new one).
    - If the logged-in phone differs from ``MAX_USERBOT_PHONE``, the saved
      session still belongs to the OLD number — warn loudly (the operator must
      delete ``data/max_session`` to switch).
    """
    try:
        # Account-change → invalidate the dialog cache.
        if _me_id is not None:
            prev = await db.get_meta("max_me_id")
            cur = str(_me_id)
            if prev != cur:
                n = await db.clear_max_peers()
                if prev:
                    logger.warning("MAX account changed (%s→%s); cleared %d cached peers",
                                   prev, cur, n)
                await db.set_meta("max_me_id", cur)

        # Phone-mismatch → stale session for a different number.
        want = "".join(ch for ch in str(settings.max_userbot_phone or "") if ch.isdigit())
        got = _extract_my_phone(me)
        if want and got and want != got:
            logger.warning("MAX session is for +%s but MAX_USERBOT_PHONE=+%s — "
                           "delete data/max_session to switch numbers", got, want)
            await _notify_admin(
                f"⚠️ MAX вошёл под старым номером +{got}, а в настройках +{want}.\n"
                "Чтобы сменить сервисный номер: останови бот, удали папку "
                "data/max_session и запусти снова — тогда попросит SMS на новый номер."
            )
    except Exception:
        logger.exception("MAX account check failed")


async def run() -> None:
    """Start the userbot and keep it connected, retrying login on failure."""
    global _client, _me_id
    from pymax import Client

    backoff = 60
    while True:
        _ready.clear()
        _me_id = None
        _client = Client(
            phone=settings.max_userbot_phone,
            session_name="userbot.db",
            work_dir=_work_dir(),
            sms_code_provider=_BotSmsCodeProvider(),
            password_provider=_BotPasswordProvider(),
        )
        _attach_handlers(_client)

        logger.info("Starting MAX userbot (%s)...", settings.max_userbot_phone)
        try:
            # connects, authorizes, then runs the receive loop until disconnect
            await _client.start()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            global _last_fail_notice
            _ready.clear()
            logger.exception("MAX userbot login/run failed")

            # Stale saved session (MAX invalidated the token): wipe it so the
            # next attempt does a fresh SMS login instead of looping forever.
            if _is_stale_session_error(err) and _clear_session():
                logger.warning("MAX session token rejected — cleared session, "
                               "will re-login via SMS")
                if _last_fail_notice != "stale":
                    _last_fail_notice = "stale"
                    await _notify_admin(
                        "⚠️ MAX-аккаунт разлогинен (сессия устарела). "
                        "Вхожу заново — как придёт SMS, пришлите /maxcode 1234."
                    )
                await asyncio.sleep(5)
                backoff = 60
                continue

            if _is_transient_auth_error(err):
                wait = max(backoff, 90)
                kind = "auth"
                msg = ("⚠️ MAX-аккаунт: код устарел или превышен лимит. "
                       "Как придёт SMS — пришлите /maxcode 1234.")
            else:
                wait = backoff
                kind = "conn"
                msg = ("⚠️ MAX-аккаунт: проблема с подключением, переподключаюсь "
                       "в фоне. Действий не требуется — сообщу, только если "
                       "понадобится SMS-код.")
            # Notify once per new problem, not on every retry (anti-spam).
            if kind != _last_fail_notice:
                _last_fail_notice = kind
                await _notify_admin(msg)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, 600)
            continue

        # start() returned cleanly = the connection dropped; reconnect (the
        # saved session is reused, so no SMS is needed).
        _ready.clear()
        backoff = 60
        logger.info("MAX userbot disconnected; reconnecting in 30s")
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Resolving & sending
# ---------------------------------------------------------------------------

def _phone_variants(phone: str) -> list[str]:
    """Formats MAX might expect: as stored, digits-only, and +digits.

    MAX returns its own numbers as bare digits (e.g. 79991234567), so a stored
    ``+7…`` may not match search_by_phone — we try both.
    """
    p = (phone or "").strip()
    digits = "".join(ch for ch in p if ch.isdigit())
    out: list[str] = []
    for v in (p, digits, ("+" + digits) if digits else ""):
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out


async def resolve(phone: str) -> tuple[int, int]:
    """Resolve a phone → (dialog chat_id, user_id). Cached in max_peer.

    Tries several phone formats (MAX is picky about the leading ``+``) and
    surfaces the underlying MAX error if the person can't be found.
    """
    if not is_ready():
        raise RuntimeError("MAX userbot не готов (нет входа в аккаунт)")
    if _me_id is None:
        raise RuntimeError("MAX: неизвестен собственный id аккаунта")

    their_id = None
    last_err: Exception | None = None
    for variant in _phone_variants(phone):
        try:
            user = await _client.search_by_phone(variant)
        except Exception as e:  # noqa: BLE001 — try the next format
            last_err = e
            logger.info("MAX search_by_phone(%s) error: %s", variant, e)
            continue
        tid = getattr(user, "id", None) or getattr(user, "user_id", None)
        if tid is not None:
            their_id = tid
            break
        logger.info("MAX search_by_phone(%s) → no user", variant)

    if their_id is None:
        if last_err is not None:
            raise RuntimeError(f"MAX: не удалось найти {phone}: {last_err}") from last_err
        raise RuntimeError(f"MAX: пользователь с номером {phone} не зарегистрирован в MAX")

    chat_id = _client.get_chat_id(_me_id, their_id)  # sync — computed locally
    await db.set_max_peer(phone, chat_id, their_id)
    return chat_id, their_id


async def send(target, text: str) -> None:
    """Send a MAX message. ``target`` = MAX dialog chat_id (int) or phone (+7…)."""
    if not is_ready():
        raise RuntimeError("MAX userbot не готов (нет входа в аккаунт)")
    if isinstance(target, str) and target.strip().startswith("+"):
        chat_id, _ = await resolve(target.strip())
    else:
        chat_id = int(target)
    await _client.send_message(chat_id, text)
