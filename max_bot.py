"""MAX messenger bot — full parity with the Telegram bot.

Built on the third-party ``maxapi`` library (aiogram-style). Enabled only
when ``MAX_BOT_TOKEN`` is set; otherwise this module is never started.

Owners and subscribers on MAX get the same flows as on Telegram:
registration (with admin approval), settings, contacts, SOS, replies and
the daily check-in. Admin *approval* itself is handled on Telegram — when a
MAX user registers, the Telegram admin receives the approval request.

NOTE: the exact maxapi surface (event/attribute names) was derived from the
library's published examples and source. If a future maxapi release renames
something, the fixes are localized to this single module.
"""

import asyncio
import logging
import os

import db
import guide
import messaging
import notifier
from config import settings

logger = logging.getLogger(__name__)

# Imported lazily-safe: this module is only imported when MAX is enabled.
from maxapi import Bot, Dispatcher, F                                  # noqa: E402
from maxapi.filters.command import Command, CommandStart              # noqa: E402
from maxapi.types.updates.bot_started import BotStarted               # noqa: E402
from maxapi.types.updates.message_created import MessageCreated       # noqa: E402
from maxapi.types.updates.message_callback import MessageCallback     # noqa: E402
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder        # noqa: E402
from maxapi.types.attachments.buttons.callback_button import CallbackButton  # noqa: E402
from maxapi.utils.deep_linking import create_start_link, decode_payload      # noqa: E402

_bot: Bot | None = None
# chat_id → "set_name" | "set_message" | "set_tz" | "set_checkin_time"
_pending_state: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Keyboards (inline — MAX uses attachment keyboards)
# ---------------------------------------------------------------------------

def _menu_kb():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="👥 Контакты", payload="menu_contacts"),
           CallbackButton(text="🆘 Тест SOS", payload="menu_test"))
    kb.row(CallbackButton(text="📬 Ответы", payload="menu_replies"),
           CallbackButton(text="🚨 Отправить SOS", payload="menu_sos"))
    kb.row(CallbackButton(text="🔗 Ссылка для друзей", payload="menu_link"),
           CallbackButton(text="📊 Статус", payload="menu_status"))
    kb.row(CallbackButton(text="⚙️ Настройки", payload="menu_settings"))
    return kb.as_markup()


def _settings_kb():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✏️ Изменить имя", payload="cfg_name"))
    kb.row(CallbackButton(text="📝 Изменить текст SOS", payload="cfg_message"))
    kb.row(CallbackButton(text="🕐 Изменить часовой пояс", payload="cfg_tz"))
    kb.row(CallbackButton(text="⏰ Авточек", payload="cfg_checkin"))
    kb.row(CallbackButton(text="🗑 Удалить аккаунт", payload="cfg_delete"))
    kb.row(CallbackButton(text="⬅️ Меню", payload="menu_home"))
    return kb.as_markup()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отмена", payload="cfg_cancel"))
    return kb.as_markup()


def _register_kb():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📝 Зарегистрироваться", payload="do_register"))
    return kb.as_markup()


def _checkin_kb():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Всё хорошо", payload="checkin_ok"),
           CallbackButton(text="🆘 Нужна помощь", payload="checkin_sos"))
    return kb.as_markup()


def _confirm_kb(yes_payload: str, yes_text: str, no_payload: str = "noop", no_text: str = "Отмена"):
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text=yes_text, payload=yes_payload),
           CallbackButton(text=no_text, payload=no_payload))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Outbound helpers (used by other modules)
# ---------------------------------------------------------------------------

async def build_subscribe_link(token: str) -> str | None:
    """MAX deep-link that subscribes the opener to ``token``'s owner."""
    if _bot is None or getattr(_bot, "me", None) is None:
        return None
    try:
        res = create_start_link(username=_bot.me.username, payload=f"sub_{token}", encode=True)
        if asyncio.iscoroutine(res):
            res = await res
        return res
    except Exception:
        logger.exception("create_start_link failed")
        return None


async def send_checkin_prompt(chat_id: int, text: str) -> None:
    if _bot is None:
        raise RuntimeError("MAX bot not initialised")
    await _bot.send_message(chat_id=chat_id, text=text, attachments=[_checkin_kb()])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_admin(chat_id: int) -> bool:
    return bool(settings.admin_chat_id and chat_id == settings.admin_chat_id)


async def _send(chat_id: int, text: str, kb=None) -> None:
    if kb is not None:
        await _bot.send_message(chat_id=chat_id, text=text, attachments=[kb])
    else:
        await _bot.send_message(chat_id=chat_id, text=text)


async def _active_owner(chat_id: int) -> db.Owner | None:
    """Owner if registered & approved, else send a hint and return None."""
    owner = await db.get_owner(chat_id)
    if not owner or owner.platform != db.MAX:
        await _send(chat_id, "Вы не зарегистрированы. Нажмите кнопку ниже.", _register_kb())
        return None
    if owner.status != "active":
        await _send(chat_id, "⏳ Ваш аккаунт ожидает одобрения администратора.")
        return None
    return owner


def _user_name(user) -> str:
    if user is None:
        return "Пользователь"
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    full = " ".join(p for p in parts if p).strip()
    return full or getattr(user, "username", None) or "Пользователь"


async def _notify_admin_registration(chat_id: int, name: str) -> bool:
    """Send approval request to the (Telegram) admin. Returns True if delivered."""
    if not settings.admin_chat_id:
        return False
    tg = messaging.get_telegram_bot()
    if tg is None:
        return False
    try:
        import bot as tg_bot
        await tg.send_message(
            settings.admin_chat_id,
            f"📩 <b>Новая заявка на регистрацию (MAX)</b>\n\n"
            f"👤 Имя: {name}\n"
            f"🆔 ID: <code>{chat_id}</code>",
            parse_mode="HTML",
            reply_markup=tg_bot._approval_keyboard(chat_id),
        )
        return True
    except Exception:
        logger.exception("Failed to notify admin about MAX registration %d", chat_id)
        return False


async def _register(chat_id: int, name: str) -> None:
    existing = await db.get_owner(chat_id)
    if existing:
        if existing.status == "pending":
            await _send(chat_id, "⏳ Ваша заявка уже отправлена и ожидает одобрения.")
        else:
            await _send(chat_id, f"Вы уже зарегистрированы как {existing.name}.", _menu_kb())
        return

    # Approve instantly only if there is no admin gate at all.
    if not settings.admin_chat_id:
        try:
            owner = await db.create_owner(chat_id, name, status="active", platform=db.MAX)
        except Exception:
            logger.exception("MAX create_owner failed for %d", chat_id)
            await _send(chat_id, "Не удалось зарегистрироваться. Попробуйте позже.")
            return
        webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}"
        await _send(
            chat_id,
            "✅ Вы зарегистрированы!\n\n"
            f"Webhook URL для Яндекс Диалогов:\n{webhook_url}\n\n"
            "Поделитесь ссылкой для друзей из меню.",
            _menu_kb(),
        )
        await _send(chat_id, guide.alice_skill_setup(webhook_url))
        return

    # Admin gate: create pending, notify admin (on Telegram).
    try:
        await db.create_owner(chat_id, name, status="pending", platform=db.MAX)
    except Exception:
        logger.exception("MAX create_owner (pending) failed for %d", chat_id)
        await _send(chat_id, "Не удалось зарегистрироваться. Попробуйте позже.")
        return
    delivered = await _notify_admin_registration(chat_id, name)
    if delivered:
        await _send(chat_id, "⏳ Заявка на регистрацию отправлена администратору.")
    else:
        # No reachable admin channel — fall back to active so the user isn't stuck.
        await db.set_owner_status(chat_id, "active")
        owner = await db.get_owner(chat_id)
        webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}" if owner else ""
        await _send(
            chat_id,
            "✅ Вы зарегистрированы!\n\n"
            f"Webhook URL для Яндекс Диалогов:\n{webhook_url}",
            _menu_kb(),
        )
        await _send(chat_id, guide.alice_skill_setup(webhook_url))


async def _subscribe(chat_id: int, name: str, token: str) -> None:
    owner = await db.get_owner_by_token(token)
    if owner is None:
        await _send(chat_id, "Ссылка недействительна. Попросите новую ссылку.")
        return
    if owner.chat_id == chat_id and owner.platform == db.MAX:
        await _send(chat_id, "Нельзя подписаться на самого себя.")
        return
    added = await db.add_contact(owner.chat_id, chat_id, name, db.MAX)
    if added:
        await _send(
            chat_id,
            f"✅ Вы подписались на оповещения от {owner.name}.\n"
            "Если владелец активирует SOS — вы получите сообщение.\n\n"
            "Напишите боту что угодно — и ваш ответ будет передан владельцу.",
        )
        try:
            await messaging.send(owner.platform, owner.chat_id,
                                 f"👤 Новый подписчик (MAX): {name}")
        except Exception:
            logger.exception("Failed to notify owner %d about new MAX subscriber", owner.chat_id)
    else:
        await _send(chat_id, f"Вы уже подписаны на оповещения от {owner.name}.")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _on_start(chat_id: int, name: str, payload: str | None) -> None:
    if payload:
        decoded = payload
        try:
            res = decode_payload(payload)
            if asyncio.iscoroutine(res):
                res = await res
            decoded = res
        except Exception:
            logger.exception("decode_payload failed")
        if decoded and decoded.startswith("sub_"):
            await _subscribe(chat_id, name, decoded[4:])
            return

    owner = await db.get_owner(chat_id)
    if owner and owner.platform == db.MAX:
        if owner.status == "pending":
            await _send(chat_id, "⏳ Ваша заявка на регистрацию ожидает одобрения.")
        else:
            await _send(chat_id, f"👋 С возвращением, {owner.name}!", _menu_kb())
    else:
        await _send(
            chat_id,
            f"👋 Привет, {name}!\n\n"
            "Это сервис экстренного оповещения через Яндекс Алису.\n\n"
            "Чтобы настроить бота для себя — зарегистрируйтесь.\n"
            "Если вы получили ссылку от друга — откройте её, чтобы подписаться.",
            _register_kb(),
        )


# --- check-in confirmations -------------------------------------------------

import checkin as checkin_module  # noqa: E402


async def _menu_action(chat_id: int, payload: str) -> None:
    """Handle main-menu buttons that map to owner actions."""
    if payload == "menu_home":
        owner = await db.get_owner(chat_id)
        if owner and owner.platform == db.MAX and owner.status == "active":
            await _send(chat_id, "Меню:", _menu_kb())
        return

    owner = await _active_owner(chat_id)
    if not owner:
        return

    if payload == "menu_contacts":
        contacts = await db.get_contacts(owner.chat_id)
        if not contacts:
            link = await build_subscribe_link(owner.webhook_token)
            await _send(chat_id, f"Список контактов пуст.\n\nСсылка для друзей:\n{link or '—'}")
            return
        lines = [f"👥 Подписчики ({len(contacts)}):\n"]
        kb = InlineKeyboardBuilder()
        for i, c in enumerate(contacts, 1):
            tag = "🅼" if c.platform == db.MAX else "📱"
            lines.append(f"{i}. {tag} {c.name}")
            kb.row(CallbackButton(text=f"✏️ {tag} {c.name}",
                                  payload=f"rename:{c.platform}:{c.chat_id}"),
                   CallbackButton(text="❌",
                                  payload=f"remove:{c.platform}:{c.chat_id}"))
        kb.row(CallbackButton(text="⬅️ Меню", payload="menu_home"))
        lines.append("\n✏️ — переименовать (удобно для Алисы), ❌ — удалить")
        await _send(chat_id, "\n".join(lines), kb.as_markup())

    elif payload == "menu_test":
        await _send(chat_id, "Отправляю тестовый SOS...")
        sent, failed = await notifier.send_sos(owner, extra_message="[ТЕСТ — не паникуйте!]")
        await _send(chat_id, f"✅ Тест завершён: {sent} доставлено, {failed} ошибок", _menu_kb())

    elif payload == "menu_sos":
        await _send(chat_id, "⚠️ Отправить экстренный SOS всем контактам?",
                    _confirm_kb("confirm_sos", "🆘 ДА, ОТПРАВИТЬ SOS", "menu_home", "Отмена"))

    elif payload == "menu_replies":
        replies = await db.get_unread_replies(owner.chat_id)
        if not replies:
            await _send(chat_id, "Нет новых ответов от контактов.", _menu_kb())
            return
        lines = [f"📬 Новые ответы ({len(replies)}):\n"]
        for r in replies:
            lines.append(f"👤 {r['contact_name']}:\n{r['text']}\n")
        await _send(chat_id, "\n".join(lines), _menu_kb())
        await db.mark_replies_read(owner.chat_id)

    elif payload == "menu_link":
        link = await build_subscribe_link(owner.webhook_token)
        await _send(chat_id,
                    f"Ссылка для подписки (MAX):\n\n{link or '—'}\n\n"
                    "Отправьте её друзьям в MAX.", _menu_kb())

    elif payload == "menu_status":
        contacts = await db.get_contacts(owner.chat_id)
        webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}"
        link = await build_subscribe_link(owner.webhook_token)
        await _send(
            chat_id,
            f"📊 Ваши настройки\n\n"
            f"👤 Имя: {owner.name}\n"
            f"🕐 Часовой пояс: UTC{owner.tz_offset:+d}\n"
            f"👥 Подписчиков: {len(contacts)}\n\n"
            f"📢 Текст SOS:\n{owner.sos_message}\n\n"
            f"🔗 Webhook:\n{webhook_url}\n"
            f"👫 Ссылка для друзей (MAX):\n{link or '—'}",
            _menu_kb(),
        )

    elif payload == "menu_settings":
        await _send(
            chat_id,
            f"⚙️ Редактирование настроек\n\n"
            f"👤 Имя: {owner.name}\n"
            f"🕐 Часовой пояс: UTC{owner.tz_offset:+d}\n"
            f"📢 Текст SOS:\n{owner.sos_message}",
            _settings_kb(),
        )


async def _settings_action(chat_id: int, payload: str) -> None:
    owner = await _active_owner(chat_id)
    if not owner:
        return

    if payload == "cfg_name":
        _pending_state[chat_id] = "set_name"
        await _send(chat_id, "✏️ Введите новое имя:", _cancel_kb())
    elif payload == "cfg_message":
        _pending_state[chat_id] = "set_message"
        await _send(chat_id, "📝 Введите новый текст SOS-сообщения:", _cancel_kb())
    elif payload == "cfg_tz":
        _pending_state[chat_id] = "set_tz"
        await _send(chat_id, "🕐 Введите часовой пояс — число от −12 до +14 (Москва = 3):", _cancel_kb())
    elif payload == "cfg_cancel":
        _pending_state.pop(chat_id, None)
        await _send(chat_id, "Отменено.", _menu_kb())
    elif payload == "cfg_delete":
        contacts = await db.get_contacts(owner.chat_id)
        await _send(chat_id,
                    f"⚠️ Удалить аккаунт?\n\nБудут удалены ваш профиль и {len(contacts)} подписчиков. "
                    "Это действие необратимо.",
                    _confirm_kb("confirm_delete", "❌ Да, удалить всё", "menu_home", "Отмена"))
    elif payload == "cfg_checkin":
        row = await db.get_checkin(chat_id)
        if row and row["enabled"]:
            t = checkin_module.fmt_time(row["time_minutes"])
            kb = InlineKeyboardBuilder()
            kb.row(CallbackButton(text="⏰ Изменить время", payload="cfg_checkin_time"))
            kb.row(CallbackButton(text="🔴 Выключить", payload="cfg_checkin_off"))
            kb.row(CallbackButton(text="⬅️ Меню", payload="menu_home"))
            await _send(chat_id, f"⏰ Авточек включён: ежедневно в {t}.", kb.as_markup())
        else:
            kb = InlineKeyboardBuilder()
            kb.row(CallbackButton(text="✅ Включить", payload="cfg_checkin_on"))
            kb.row(CallbackButton(text="⬅️ Меню", payload="menu_home"))
            await _send(chat_id,
                        "⏰ Авточек выключен.\n\nЕсли включить — бот будет ежедневно спрашивать "
                        "«Всё в порядке?». Нет ответа 1 ч → повтор, ещё 30 мин → предупреждение, "
                        "ещё 10 мин → автоматический SOS.", kb.as_markup())
    elif payload in ("cfg_checkin_on", "cfg_checkin_time"):
        _pending_state[chat_id] = "set_checkin_time"
        await _send(chat_id, "Введите время ежедневной проверки в формате ЧЧ:ММ, например 09:00:", _cancel_kb())
    elif payload == "cfg_checkin_off":
        await db.ensure_checkin(chat_id)
        await db.update_checkin(chat_id, enabled=0, state="idle", attempts=0)
        await _send(chat_id, "🔴 Авточек выключен.", _menu_kb())


async def _confirm_action(chat_id: int, payload: str) -> None:
    if payload == "confirm_sos":
        owner = await _active_owner(chat_id)
        if not owner:
            return
        await _send(chat_id, "🆘 Отправляю SOS...")
        sent, failed = await notifier.send_sos(owner)
        await _send(chat_id, f"🆘 SOS отправлен!\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}", _menu_kb())

    elif payload == "confirm_delete":
        owner = await db.get_owner(chat_id)
        if owner and owner.platform == db.MAX:
            await db.delete_owner(chat_id)
            await _send(chat_id, "✅ Аккаунт и все контакты удалены.")

    elif payload.startswith("remove:"):
        owner = await _active_owner(chat_id)
        if not owner:
            return
        _, platform, cid_str = payload.split(":")
        cid = int(cid_str)
        contacts = await db.get_contacts(owner.chat_id)
        name = next((c.name for c in contacts if c.chat_id == cid and c.platform == platform), str(cid))
        await db.remove_contact(owner.chat_id, cid, platform)
        await _send(chat_id, f"❌ {name} удалён.", _menu_kb())

    elif payload.startswith("rename:"):
        owner = await _active_owner(chat_id)
        if not owner:
            return
        _, platform, cid_str = payload.split(":")
        contacts = await db.get_contacts(owner.chat_id)
        cur = next((c.name for c in contacts if c.chat_id == int(cid_str) and c.platform == platform), cid_str)
        _pending_state[chat_id] = f"rename:{platform}:{cid_str}"
        await _send(chat_id, f"✏️ Введите новое имя для «{cur}» (как Алисе удобнее произносить):", _cancel_kb())

    elif payload == "checkin_ok":
        await checkin_module.confirm(chat_id)
        await _send(chat_id, "✅ Отметка принята. Всё хорошо!")

    elif payload == "checkin_sos":
        owner = await db.get_owner(chat_id)
        if owner and owner.platform == db.MAX and owner.status == "active":
            await _send(chat_id, "🆘 Отправляю SOS вашим контактам...")
            sent, failed = await notifier.send_sos(owner)
            await _send(chat_id, f"🆘 SOS отправлен!\n✅ {sent}\n❌ {failed}")
            await db.update_checkin(chat_id, state="idle", attempts=0)


async def _handle_text(chat_id: int, name: str, text: str) -> None:
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return

    state = _pending_state.pop(chat_id, None)

    if state and state.startswith("rename:"):
        owner = await _active_owner(chat_id)
        if not owner:
            return
        _, platform, cid_str = state.split(":")
        ok = await db.rename_contact(owner.chat_id, int(cid_str), platform, text)
        if ok:
            await _send(chat_id, f"✅ Контакт переименован в «{text}»", _menu_kb())
        else:
            await _send(chat_id, "Контакт не найден (возможно, удалён).", _menu_kb())
        return

    if state == "set_name":
        if await _active_owner(chat_id):
            await db.update_owner(chat_id, name=text)
            await _send(chat_id, f"✅ Имя изменено на «{text}»", _menu_kb())
        return
    if state == "set_message":
        if await _active_owner(chat_id):
            await db.update_owner(chat_id, sos_message=text)
            await _send(chat_id, f"✅ Текст SOS изменён:\n{text}", _menu_kb())
        return
    if state == "set_tz":
        if not await _active_owner(chat_id):
            return
        try:
            offset = int(text.lstrip("+"))
            if not -12 <= offset <= 14:
                raise ValueError
            await db.update_owner(chat_id, tz_offset=offset)
            await _send(chat_id, f"✅ Часовой пояс: UTC{offset:+d}", _menu_kb())
        except ValueError:
            _pending_state[chat_id] = "set_tz"
            await _send(chat_id, "Введите число от -12 до +14, например: 3", _cancel_kb())
        return
    if state == "set_checkin_time":
        if not await _active_owner(chat_id):
            return
        minutes = checkin_module.parse_time(text)
        if minutes is None:
            _pending_state[chat_id] = "set_checkin_time"
            await _send(chat_id, "Неверный формат. Введите время как 09:00:", _cancel_kb())
            return
        await db.ensure_checkin(chat_id)
        await db.update_checkin(chat_id, enabled=1, time_minutes=minutes,
                                state="idle", attempts=0, last_asked_at=0)
        await _send(chat_id, f"✅ Авточек включён. Каждый день в {checkin_module.fmt_time(minutes)}.", _menu_kb())
        return

    # Any text proves the owner is alive — silently confirm an active check-in.
    await checkin_module.confirm(chat_id)

    # Forward as a subscriber reply to owner(s).
    owners = await db.get_owners_for_contact(chat_id, db.MAX)
    if not owners:
        return
    for owner in owners:
        # Use the name THIS owner gave the contact (may be renamed for Alice).
        display = await db.get_contact_name(owner.chat_id, chat_id, db.MAX) or name
        await db.add_reply(owner.chat_id, chat_id, display, text, db.MAX)
        try:
            await messaging.send(owner.platform, owner.chat_id, f"💬 Ответ от {display}:\n{text}")
        except Exception:
            logger.exception("Failed to forward MAX reply to owner %d", owner.chat_id)
    await _send(chat_id, "✅ Ваш ответ отправлен.")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def create_bot() -> Bot:
    global _bot
    if settings.max_bot_token:
        os.environ.setdefault("MAX_BOT_TOKEN", settings.max_bot_token)
    _bot = Bot()
    return _bot


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.bot_started()
    async def on_bot_started(event: BotStarted):
        await _on_start(event.chat_id, _user_name(event.user), event.payload)

    @dp.message_created(CommandStart())
    async def on_start_cmd(event: MessageCreated):
        chat_id, _ = event.get_ids()
        name = _user_name(event.message.sender)
        await _on_start(chat_id, name, None)

    @dp.message_created(Command("register"))
    async def on_register(event: MessageCreated):
        chat_id, _ = event.get_ids()
        await _register(chat_id, _user_name(event.message.sender))

    @dp.message_created(Command("cancel"))
    async def on_cancel(event: MessageCreated):
        chat_id, _ = event.get_ids()
        if _pending_state.pop(chat_id, None):
            await _send(chat_id, "Ввод отменён.", _menu_kb())
        else:
            await _send(chat_id, "Нечего отменять.")

    @dp.message_created(Command("unsubscribe"))
    async def on_unsubscribe(event: MessageCreated):
        chat_id, _ = event.get_ids()
        removed = await db.remove_subscriber(chat_id, db.MAX)
        await _send(chat_id, f"❌ Вы отписались от {removed} оповещений." if removed
                    else "Вы не были подписаны ни на одного владельца.")

    @dp.message_callback()
    async def on_callback(event: MessageCallback):
        chat_id, _ = event.get_ids()
        payload = event.callback.payload if event.callback else None
        try:
            await event.answer()  # acknowledge
        except Exception:
            logger.exception("callback ack failed")
        if not payload:
            return
        if payload == "do_register":
            user = event.callback.user if event.callback else None
            await _register(chat_id, _user_name(user))
        elif payload == "noop":
            return
        elif payload.startswith("menu_"):
            await _menu_action(chat_id, payload)
        elif payload.startswith("cfg_"):
            await _settings_action(chat_id, payload)
        else:
            await _confirm_action(chat_id, payload)

    @dp.message_created(F.message.body.text)
    async def on_text(event: MessageCreated):
        chat_id, _ = event.get_ids()
        await _handle_text(chat_id, _user_name(event.message.sender), event.message.body.text)

    return dp


async def run_polling() -> None:
    if _bot is None:
        raise RuntimeError("create_bot() must be called first")
    dp = create_dispatcher()
    try:
        await _bot.get_me()  # populate _bot.me for deep links
    except Exception:
        logger.exception("MAX get_me failed")
    logger.info("Starting MAX bot polling...")
    await dp.start_polling(_bot)
