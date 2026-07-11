"""Telegram bot — multi-tenant version."""

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import checkin as checkin_module
import db
import guide
import messaging
import notifier
from config import settings

logger = logging.getLogger(__name__)
router = Router()

# chat_id → "set_name" | "set_message" | "set_tz" | "set_checkin_time"
_pending_state: dict[int, str] = {}


async def _subscribe_links_text(tg_bot, owner: db.Owner) -> str:
    """Subscribe links for every available platform."""
    bot_info = await tg_bot.get_me()
    tg_link = f"https://t.me/{bot_info.username}?start=sub_{owner.webhook_token}"
    lines = [f"📱 Telegram:\n{tg_link}"]
    if messaging.max_enabled():
        try:
            import max_bot
            max_link = await max_bot.build_subscribe_link(owner.webhook_token)
            if max_link:
                lines.append(f"🅼 MAX:\n{max_link}")
        except Exception:
            logger.exception("failed to build MAX subscribe link")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _owner_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Контакты"), KeyboardButton(text="🆘 Тест SOS")],
            [KeyboardButton(text="📬 Ответы"), KeyboardButton(text="🚨 Отправить SOS")],
            [KeyboardButton(text="🔗 Ссылка для друзей"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


def _subscribe_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Мои подписки"), KeyboardButton(text="❌ Отписаться")],
        ],
        resize_keyboard=True,
    )


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="cfg_name")],
        [InlineKeyboardButton(text="📝 Изменить текст SOS", callback_data="cfg_message")],
        [InlineKeyboardButton(text="🕐 Изменить часовой пояс", callback_data="cfg_tz")],
        [InlineKeyboardButton(text="⏰ Авточек", callback_data="cfg_checkin")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data="cfg_delete")],
    ])


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="cfg_cancel"),
    ]])


def _approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{chat_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{chat_id}"),
    ]])


def _admin_delete_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"admin_delete:{chat_id}"),
    ]])


def _admin_delete_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_delete_confirm:{chat_id}"),
        InlineKeyboardButton(text="Отмена", callback_data="admin_cancel"),
    ]])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_active_owner(chat_id: int, reply_target: Message) -> db.Owner | None:
    """Return owner only if registered and approved. Sends error message otherwise."""
    owner = await db.get_owner(chat_id)
    if not owner:
        await reply_target.answer("Вы не зарегистрированы. Используйте /register")
        return None
    if owner.status != "active":
        await reply_target.answer(
            "⏳ Ваш аккаунт ожидает одобрения администратора.\n"
            "Вы получите уведомление, как только заявка будет рассмотрена."
        )
        return None
    return owner


async def _get_active_owner_cb(callback: CallbackQuery) -> db.Owner | None:
    """Return owner only if registered and approved. Answers callback with error otherwise."""
    owner = await db.get_owner(callback.from_user.id)
    if not owner or owner.status != "active":
        await callback.answer("Нет доступа")
        return None
    return owner


def _is_admin(chat_id: int) -> bool:
    return bool(settings.admin_chat_id and chat_id == settings.admin_chat_id)


# ---------------------------------------------------------------------------
# /start  — entry point for both owners and subscribers
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    payload = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ""

    # Deep-link subscription: /start sub_<webhook_token>
    if payload.startswith("sub_"):
        token = payload[4:]
        owner = await db.get_owner_by_token(token)
        if owner is None:
            await message.answer("Ссылка недействительна. Попросите отправителя поделиться новой ссылкой.")
            return

        if owner.chat_id == message.from_user.id:
            await message.answer("Нельзя подписаться на самого себя.")
            return

        name = message.from_user.full_name
        added = await db.add_contact(owner.chat_id, message.from_user.id, name, db.TELEGRAM)
        if added:
            await message.answer(
                f"✅ Вы подписались на оповещения от {owner.name}.\n"
                "Если владелец активирует SOS через Алису — вы получите сообщение.\n\n"
                "Напишите боту что угодно — и ваш ответ будет передан владельцу.",
                reply_markup=_subscribe_keyboard(),
            )
            await message.bot.send_message(
                owner.chat_id,
                f"👤 Новый подписчик: {name} (id: {message.from_user.id})",
            )
        else:
            await message.answer(
                f"Вы уже подписаны на оповещения от {owner.name}.",
                reply_markup=_subscribe_keyboard(),
            )
        return

    # Regular /start — check if user is already an owner
    owner = await db.get_owner(message.from_user.id)
    if owner:
        if owner.status == "pending":
            await message.answer(
                f"👋 Привет, {owner.name}!\n\n"
                "⏳ Ваша заявка на регистрацию отправлена администратору.\n"
                "Мы уведомим вас, как только она будет рассмотрена."
            )
        else:
            await message.answer(
                f"👋 С возвращением, {owner.name}!\n\n"
                "Управляйте контактами и настройками через кнопки ниже.",
                reply_markup=_owner_keyboard(),
            )
    else:
        await message.answer(
            f"👋 Привет, {message.from_user.first_name}!\n\n"
            "Это сервис экстренного оповещения через Яндекс Алису.\n\n"
            "Если вы хотите настроить бота для себя — нажмите:\n"
            "/register\n\n"
            "Если вы получили ссылку от друга — перейдите по ней, чтобы подписаться.",
            reply_markup=ReplyKeyboardRemove(),
        )


# ---------------------------------------------------------------------------
# Owner registration
# ---------------------------------------------------------------------------

@router.message(Command("register"))
async def cmd_register(message: Message) -> None:
    existing = await db.get_owner(message.from_user.id)
    if existing:
        if existing.status == "pending":
            await message.answer(
                "⏳ Ваша заявка уже отправлена и ожидает одобрения администратора.\n"
                "Вы получите уведомление, как только она будет рассмотрена."
            )
        else:
            await message.answer(
                f"Вы уже зарегистрированы как {existing.name}.\n\n"
                "Используйте кнопку «📊 Статус» чтобы посмотреть все настройки.",
                reply_markup=_owner_keyboard(),
            )
        return

    chat_id = message.from_user.id
    name = message.from_user.full_name

    # If no admin configured, or registrant IS the admin — approve immediately
    if not settings.admin_chat_id or chat_id == settings.admin_chat_id:
        owner = await db.create_owner(chat_id, name, status="active", platform=db.TELEGRAM)
        webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}"
        links = await _subscribe_links_text(message.bot, owner)
        await message.answer(
            f"✅ Вы зарегистрированы!\n\n"
            f"<b>Webhook URL для Яндекс Диалогов:</b>\n"
            f"<code>{webhook_url}</code>\n\n"
            f"<b>Ссылки для друзей:</b>\n"
            f"{links}\n\n"
            "Поделитесь ссылкой с друзьями — они подпишутся одним нажатием.",
            parse_mode="HTML",
            reply_markup=_owner_keyboard(),
        )
        await message.answer(guide.alice_skill_setup(webhook_url))
    else:
        await db.create_owner(chat_id, name, status="pending", platform=db.TELEGRAM)
        await message.answer(
            "⏳ Заявка на регистрацию отправлена администратору.\n"
            "Вы получите уведомление, когда она будет рассмотрена."
        )
        try:
            await message.bot.send_message(
                settings.admin_chat_id,
                f"📩 <b>Новая заявка на регистрацию</b>\n\n"
                f"👤 Имя: {name}\n"
                f"🆔 ID: <code>{chat_id}</code>",
                parse_mode="HTML",
                reply_markup=_approval_keyboard(chat_id),
            )
        except Exception:
            logger.exception("Failed to notify admin about registration from %d", chat_id)


# ---------------------------------------------------------------------------
# Approval / rejection (admin only)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("approve:"))
async def callback_approve(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    chat_id = int(callback.data.split(":")[1])
    owner = await db.get_owner(chat_id)
    if not owner:
        await callback.message.edit_text("❌ Пользователь не найден (возможно, удалил аккаунт).")
        await callback.answer()
        return
    await db.set_owner_status(chat_id, "active")
    webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}"
    await callback.message.edit_text(f"✅ Одобрено: {owner.name} (id: {chat_id})")
    # Notify the approved owner on their own platform.
    try:
        if owner.platform == db.MAX:
            await messaging.send(
                db.MAX, chat_id,
                "✅ Ваша регистрация одобрена!\n\n"
                f"Webhook URL для Яндекс Диалогов:\n{webhook_url}\n\n"
                "Откройте бота и используйте кнопки для управления.",
            )
            await messaging.send(db.MAX, chat_id, guide.alice_skill_setup(webhook_url))
        else:
            links = await _subscribe_links_text(callback.message.bot, owner)
            await callback.message.bot.send_message(
                chat_id,
                f"✅ Ваша регистрация одобрена!\n\n"
                f"<b>Webhook URL для Яндекс Диалогов:</b>\n"
                f"<code>{webhook_url}</code>\n\n"
                f"<b>Ссылки для друзей:</b>\n"
                f"{links}\n\n"
                "Используйте кнопки ниже для управления ботом.",
                parse_mode="HTML",
                reply_markup=_owner_keyboard(),
            )
            await callback.message.bot.send_message(chat_id, guide.alice_skill_setup(webhook_url))
    except Exception:
        logger.exception("Failed to notify user %d about approval", chat_id)
    await callback.answer("✅ Одобрено")


@router.callback_query(F.data.startswith("reject:"))
async def callback_reject(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    chat_id = int(callback.data.split(":")[1])
    owner = await db.get_owner(chat_id)
    name = owner.name if owner else str(chat_id)
    platform = owner.platform if owner else db.TELEGRAM
    await db.delete_owner(chat_id)
    await callback.message.edit_text(f"❌ Отклонено: {name} (id: {chat_id})")
    try:
        await messaging.send(
            platform, chat_id,
            "❌ Ваша заявка на регистрацию отклонена администратором.\n"
            "Если считаете это ошибкой — свяжитесь с администратором."
        )
    except Exception:
        logger.exception("Failed to notify user %d about rejection", chat_id)
    await callback.answer("❌ Отклонено")


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    owners = await db.get_all_owners()
    pending = [o for o in owners if o.status == "pending"]
    active = [o for o in owners if o.status == "active"]

    kb_rows = []
    if pending:
        kb_rows.append([InlineKeyboardButton(
            text=f"⏳ Заявки ({len(pending)})", callback_data="admin_list_pending"
        )])
    kb_rows.append([InlineKeyboardButton(
        text=f"📋 Все владельцы ({len(active)})", callback_data="admin_list_active"
    )])

    await message.answer(
        f"👑 <b>Панель администратора</b>\n\n"
        f"✅ Активных владельцев: {len(active)}\n"
        f"⏳ Ожидают одобрения: {len(pending)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.callback_query(F.data == "admin_list_active")
async def callback_admin_list_active(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    owners = [o for o in await db.get_all_owners() if o.status == "active"]
    if not owners:
        await callback.answer("Нет активных владельцев")
        return
    await callback.answer()
    for owner in owners:
        contacts = await db.get_contacts(owner.chat_id)
        await callback.message.answer(
            f"👤 <b>{owner.name}</b>\n"
            f"🆔 ID: <code>{owner.chat_id}</code>\n"
            f"👥 Контактов: {len(contacts)}\n"
            f"🕐 Часовой пояс: UTC{owner.tz_offset:+d}",
            parse_mode="HTML",
            reply_markup=_admin_delete_keyboard(owner.chat_id),
        )


@router.callback_query(F.data == "admin_list_pending")
async def callback_admin_list_pending(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    owners = [o for o in await db.get_all_owners() if o.status == "pending"]
    if not owners:
        await callback.answer("Нет заявок")
        return
    await callback.answer()
    for owner in owners:
        await callback.message.answer(
            f"📩 <b>{owner.name}</b>\n"
            f"🆔 ID: <code>{owner.chat_id}</code>",
            parse_mode="HTML",
            reply_markup=_approval_keyboard(owner.chat_id),
        )


@router.callback_query(F.data.startswith("admin_delete:"))
async def callback_admin_delete(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    chat_id = int(callback.data.split(":")[1])
    owner = await db.get_owner(chat_id)
    if not owner:
        await callback.message.edit_text("Пользователь не найден.")
        await callback.answer()
        return
    contacts = await db.get_contacts(owner.chat_id)
    await callback.message.edit_text(
        f"⚠️ Удалить аккаунт <b>{owner.name}</b> (id: {chat_id})?\n\n"
        f"Будут удалены: {len(contacts)} контактов, все ответы и данные авточека.",
        parse_mode="HTML",
        reply_markup=_admin_delete_confirm_keyboard(chat_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_delete_confirm:"))
async def callback_admin_delete_confirm(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    chat_id = int(callback.data.split(":")[1])
    owner = await db.get_owner(chat_id)
    name = owner.name if owner else str(chat_id)
    platform = owner.platform if owner else db.TELEGRAM
    await db.delete_owner(chat_id)
    await callback.message.edit_text(f"✅ Аккаунт {name} (id: {chat_id}) удалён вместе со всеми данными.")
    try:
        await messaging.send(
            platform, chat_id,
            "❌ Ваш аккаунт был удалён администратором.\n"
            "Для повторной регистрации откройте бота заново."
        )
    except Exception:
        logger.exception("Failed to notify deleted user %d", chat_id)
    await callback.answer("✅ Удалено")


@router.callback_query(F.data == "admin_cancel")
async def callback_admin_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.message(Command("settings"))
@router.message(F.text == "📊 Статус")
async def cmd_status(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    contacts = await db.get_contacts(owner.chat_id)
    webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}"
    links = await _subscribe_links_text(message.bot, owner)
    await message.answer(
        f"📊 <b>Ваши настройки</b>\n\n"
        f"👤 Имя: {owner.name}\n"
        f"🕐 Часовой пояс: UTC{owner.tz_offset:+d}\n"
        f"👥 Подписчиков: {len(contacts)}\n\n"
        f"📢 Текст SOS:\n{owner.sos_message}\n\n"
        f"🔗 Webhook: <code>{webhook_url}</code>\n"
        f"👫 Ссылки для друзей:\n{links}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Settings menu (edit)
# ---------------------------------------------------------------------------

@router.message(F.text == "⚙️ Настройки")
async def cmd_settings_menu(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    await message.answer(
        f"⚙️ <b>Редактирование настроек</b>\n\n"
        f"👤 Имя: {owner.name}\n"
        f"🕐 Часовой пояс: UTC{owner.tz_offset:+d}\n"
        f"📢 Текст SOS:\n{owner.sos_message}",
        parse_mode="HTML",
        reply_markup=_settings_keyboard(),
    )


@router.callback_query(F.data == "cfg_name")
async def callback_cfg_name(callback: CallbackQuery) -> None:
    if not await _get_active_owner_cb(callback):
        return
    _pending_state[callback.from_user.id] = "set_name"
    await callback.message.answer("✏️ Введите новое имя:", reply_markup=_cancel_keyboard())
    await callback.answer()


@router.callback_query(F.data == "cfg_message")
async def callback_cfg_message(callback: CallbackQuery) -> None:
    if not await _get_active_owner_cb(callback):
        return
    _pending_state[callback.from_user.id] = "set_message"
    await callback.message.answer(
        "📝 Введите новый текст SOS-сообщения:",
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "cfg_tz")
async def callback_cfg_tz(callback: CallbackQuery) -> None:
    if not await _get_active_owner_cb(callback):
        return
    _pending_state[callback.from_user.id] = "set_tz"
    await callback.message.answer(
        "🕐 Введите часовой пояс — число от −12 до +14.\n"
        "Примеры: Москва = 3, Екатеринбург = 5, Калининград = 2",
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "cfg_cancel")
async def callback_cfg_cancel(callback: CallbackQuery) -> None:
    _pending_state.pop(callback.from_user.id, None)
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.callback_query(F.data == "cfg_checkin")
async def callback_cfg_checkin(callback: CallbackQuery) -> None:
    if not await _get_active_owner_cb(callback):
        return
    row = await db.get_checkin(callback.from_user.id)
    if row and row["enabled"]:
        t = checkin_module.fmt_time(row["time_minutes"])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Изменить время", callback_data="cfg_checkin_time")],
            [InlineKeyboardButton(text="🔴 Выключить", callback_data="cfg_checkin_off")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cfg_cancel")],
        ])
        await callback.message.answer(
            f"⏰ Авточек включён: ежедневно в {t}.\n\n"
            "Бот будет спрашивать «Всё в порядке?» и, если не получит ответа, "
            "автоматически разошлёт SOS.",
            reply_markup=kb,
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Включить", callback_data="cfg_checkin_on")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cfg_cancel")],
        ])
        await callback.message.answer(
            "⏰ Авточек выключен.\n\n"
            "Если включить — бот будет ежедневно спрашивать «Всё в порядке?».\n"
            "Нет ответа 1 ч → повтор. Ещё 30 мин → последнее предупреждение. "
            "Ещё 10 мин → автоматический SOS.",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.in_({"cfg_checkin_on", "cfg_checkin_time"}))
async def callback_cfg_checkin_set_time(callback: CallbackQuery) -> None:
    if not await _get_active_owner_cb(callback):
        return
    _pending_state[callback.from_user.id] = "set_checkin_time"
    await callback.message.answer(
        "Введите время ежедневной проверки в формате ЧЧ:ММ\n"
        "Например: <code>09:00</code> или <code>21:30</code>\n\n"
        "Время указывается в вашем часовом поясе (UTC{tz}).",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "cfg_checkin_off")
async def callback_cfg_checkin_off(callback: CallbackQuery) -> None:
    if not await _get_active_owner_cb(callback):
        return
    await db.ensure_checkin(callback.from_user.id)
    await db.update_checkin(callback.from_user.id, enabled=0, state="idle", attempts=0)
    await callback.message.answer("🔴 Авточек выключен.")
    await callback.answer()


@router.callback_query(F.data == "checkin_ok")
async def callback_checkin_ok(callback: CallbackQuery) -> None:
    was_active = await checkin_module.confirm(callback.from_user.id)
    if was_active:
        await callback.message.edit_text("✅ Отметка принята. Всё хорошо!")
    else:
        await callback.message.edit_text("✅ Принято.")
    await callback.answer()


@router.callback_query(F.data == "checkin_sos")
async def callback_checkin_sos(callback: CallbackQuery) -> None:
    owner = await _get_active_owner_cb(callback)
    if not owner:
        return
    await callback.message.edit_text("🆘 Отправляю SOS вашим контактам...")
    sent, failed = await notifier.send_sos(owner)
    await callback.message.answer(
        f"🆘 SOS отправлен!\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}"
    )
    await db.update_checkin(callback.from_user.id, state="idle", attempts=0)
    await callback.answer()


@router.callback_query(F.data == "cfg_delete")
async def callback_cfg_delete(callback: CallbackQuery) -> None:
    owner = await _get_active_owner_cb(callback)
    if not owner:
        return
    contacts = await db.get_contacts(owner.chat_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Да, удалить всё", callback_data="confirm_delete"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_delete"),
    ]])
    await callback.message.answer(
        f"⚠️ Удалить аккаунт?\n\n"
        f"Будут удалены ваш профиль и {len(contacts)} подписчиков.\n"
        "Это действие необратимо.",
        reply_markup=keyboard,
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Legacy text commands (still work)
# ---------------------------------------------------------------------------

@router.message(Command("setname"))
async def cmd_setname(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /setname Ваше Имя")
        return
    name = parts[1].strip()
    await db.update_owner(message.from_user.id, name=name)
    await message.answer(f"✅ Имя изменено на «{name}»")


@router.message(Command("setmessage"))
async def cmd_setmessage(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /setmessage Текст вашего SOS-сообщения")
        return
    text = parts[1].strip()
    await db.update_owner(message.from_user.id, sos_message=text)
    await message.answer(f"✅ Текст SOS изменён:\n{text}")


@router.message(Command("settz"))
async def cmd_settz(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    parts = message.text.split(maxsplit=1)
    try:
        offset = int(parts[1].strip().lstrip("+"))
        if not -12 <= offset <= 14:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("Использование: /settz 3  (число от -12 до +14, разница с UTC)")
        return
    await db.update_owner(message.from_user.id, tz_offset=offset)
    await message.answer(f"✅ Часовой пояс: UTC{offset:+d}")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if _pending_state.pop(message.from_user.id, None):
        await message.answer("Ввод отменён.", reply_markup=_owner_keyboard())
    else:
        await message.answer("Нечего отменять.")


@router.message(Command("deleteaccount"))
async def cmd_deleteaccount(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы.")
        return
    contacts = await db.get_contacts(owner.chat_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Да, удалить всё", callback_data="confirm_delete"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_delete"),
    ]])
    await message.answer(
        f"⚠️ Удалить аккаунт?\n\n"
        f"Будут удалены ваш профиль и {len(contacts)} подписчиков.\n"
        "Это действие необратимо.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "confirm_delete")
async def callback_confirm_delete(callback: CallbackQuery) -> None:
    owner = await db.get_owner(callback.from_user.id)
    if not owner:
        await callback.answer("Аккаунт не найден")
        return
    await db.delete_owner(callback.from_user.id)
    await callback.message.edit_text("✅ Аккаунт и все контакты удалены.")
    await callback.answer()


@router.callback_query(F.data == "cancel_delete")
async def callback_cancel_delete(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Contact management (owner)
# ---------------------------------------------------------------------------

@router.message(Command("contacts"))
@router.message(F.text == "👥 Контакты")
async def cmd_contacts(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    contacts = await db.get_contacts(owner.chat_id)
    if not contacts:
        links = await _subscribe_links_text(message.bot, owner)
        await message.answer(
            f"Список контактов пуст.\n\nПоделитесь ссылками с друзьями:\n{links}"
        )
        return

    def _tag(platform: str) -> str:
        return "🅼" if platform == db.MAX else "📱"

    lines = [f"👥 Подписчики ({len(contacts)}):\n"]
    for i, c in enumerate(contacts, 1):
        lines.append(f"{i}. {_tag(c.platform)} {c.name}")
    lines.append("\n✏️ — переименовать (удобно для Алисы), ❌ — удалить")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✏️ {_tag(c.platform)} {c.name}",
                callback_data=f"rename:{c.platform}:{c.chat_id}",
            ),
            InlineKeyboardButton(
                text="❌",
                callback_data=f"remove:{c.platform}:{c.chat_id}",
            ),
        ]
        for c in contacts
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("remove:"))
async def callback_remove_contact(callback: CallbackQuery) -> None:
    owner = await _get_active_owner_cb(callback)
    if not owner:
        return
    _, platform, chat_id_str = callback.data.split(":")
    chat_id = int(chat_id_str)
    contacts = await db.get_contacts(owner.chat_id)
    name = next((c.name for c in contacts if c.chat_id == chat_id and c.platform == platform), str(chat_id))
    await db.remove_contact(owner.chat_id, chat_id, platform)
    await callback.answer(f"❌ {name} удалён")
    await callback.message.delete()


@router.callback_query(F.data.startswith("rename:"))
async def callback_rename_contact(callback: CallbackQuery) -> None:
    owner = await _get_active_owner_cb(callback)
    if not owner:
        return
    _, platform, chat_id_str = callback.data.split(":")
    contacts = await db.get_contacts(owner.chat_id)
    cur_name = next(
        (c.name for c in contacts if c.chat_id == int(chat_id_str) and c.platform == platform),
        chat_id_str,
    )
    _pending_state[callback.from_user.id] = f"rename:{platform}:{chat_id_str}"
    await callback.message.answer(
        f"✏️ Введите новое имя для «{cur_name}»\n"
        "(как Алисе удобнее его произносить):",
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.message(Command("mylink"))
@router.message(F.text == "🔗 Ссылка для друзей")
async def cmd_mylink(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    links = await _subscribe_links_text(message.bot, owner)
    await message.answer(
        f"Ссылки для подписки на ваши оповещения:\n\n{links}\n\n"
        "Отправьте друзьям ссылку их мессенджера — они подпишутся одним нажатием."
    )


# ---------------------------------------------------------------------------
# SOS (owner)
# ---------------------------------------------------------------------------

@router.message(Command("test"))
@router.message(F.text == "🆘 Тест SOS")
async def cmd_test(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    await message.answer("Отправляю тестовый SOS...")
    sent, failed = await notifier.send_sos(
        owner, extra_message="[ТЕСТ — не паникуйте!]"
    )
    await message.answer(f"✅ Тест завершён: {sent} доставлено, {failed} ошибок")


@router.message(Command("sos"))
@router.message(F.text == "🚨 Отправить SOS")
async def cmd_sos(message: Message) -> None:
    owner = await _get_active_owner(message.from_user.id, message)
    if not owner:
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆘 ДА, ОТПРАВИТЬ SOS", callback_data="confirm_sos"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_sos"),
    ]])
    await message.answer("⚠️ Отправить экстренный SOS всем контактам?", reply_markup=keyboard)


@router.callback_query(F.data == "confirm_sos")
async def callback_confirm_sos(callback: CallbackQuery) -> None:
    owner = await _get_active_owner_cb(callback)
    if not owner:
        return
    await callback.message.edit_text("🆘 Отправляю SOS...")
    sent, failed = await notifier.send_sos(owner)
    await callback.message.edit_text(
        f"🆘 SOS отправлен!\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}"
    )


@router.callback_query(F.data == "cancel_sos")
async def callback_cancel_sos(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Replies (owner)
# ---------------------------------------------------------------------------

@router.message(Command("replies"))
@router.message(F.text == "📬 Ответы")
async def cmd_replies(message: Message) -> None:
    try:
        owner = await _get_active_owner(message.from_user.id, message)
        if not owner:
            return
        replies = await db.get_unread_replies(owner.chat_id)
        if not replies:
            await message.answer("Нет новых ответов от контактов.")
            return
        lines = [f"📬 Новые ответы ({len(replies)}):\n"]
        for r in replies:
            lines.append(f"👤 {r['contact_name']}:\n{r['text']}\n")
        await message.answer("\n".join(lines))
        await db.mark_replies_read(owner.chat_id)
    except Exception:
        logger.exception("cmd_replies error")
        await message.answer("Ошибка при получении ответов. Проверьте логи.")


# ---------------------------------------------------------------------------
# Subscriber commands
# ---------------------------------------------------------------------------

@router.message(Command("unsubscribe"))
@router.message(F.text == "❌ Отписаться")
async def cmd_unsubscribe(message: Message) -> None:
    removed = await db.remove_subscriber(message.from_user.id, db.TELEGRAM)
    if removed:
        await message.answer(
            f"❌ Вы отписались от {removed} оповещений.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer("Вы не были подписаны ни на одного владельца.")


@router.message(Command("subscribe"))
async def cmd_subscribe_hint(message: Message) -> None:
    await message.answer(
        "Для подписки используйте персональную ссылку от владельца бота.\n"
        "Она выглядит так:\n"
        "https://t.me/botname?start=sub_..."
    )


@router.message(F.text == "📋 Мои подписки")
async def cmd_my_subscriptions(message: Message) -> None:
    owners = await db.get_owners_for_contact(message.from_user.id, db.TELEGRAM)
    if not owners:
        await message.answer("Вы не подписаны ни на кого.")
        return
    lines = ["📋 Ваши подписки:\n"]
    for i, owner in enumerate(owners, 1):
        lines.append(f"{i}. {owner.name}")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Catch-all: settings input + subscriber reply forwarding
# ---------------------------------------------------------------------------

@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.text or message.text.startswith("/"):
        return

    chat_id = message.from_user.id
    state = _pending_state.pop(chat_id, None)

    if state and state.startswith("rename:"):
        owner = await _get_active_owner(chat_id, message)
        if not owner:
            return
        _, platform, cid_str = state.split(":")
        new_name = message.text.strip()
        if not new_name:
            _pending_state[chat_id] = state
            await message.answer("Имя не может быть пустым. Введите ещё раз:", reply_markup=_cancel_keyboard())
            return
        ok = await db.rename_contact(owner.chat_id, int(cid_str), platform, new_name)
        if ok:
            await message.answer(f"✅ Контакт переименован в «{new_name}»", reply_markup=_owner_keyboard())
        else:
            await message.answer("Контакт не найден (возможно, удалён).", reply_markup=_owner_keyboard())
        return

    if state == "set_name":
        owner = await _get_active_owner(chat_id, message)
        if not owner:
            return
        name = message.text.strip()
        await db.update_owner(chat_id, name=name)
        await message.answer(f"✅ Имя изменено на «{name}»", reply_markup=_owner_keyboard())
        return

    if state == "set_message":
        owner = await _get_active_owner(chat_id, message)
        if not owner:
            return
        text = message.text.strip()
        await db.update_owner(chat_id, sos_message=text)
        await message.answer(f"✅ Текст SOS изменён:\n{text}", reply_markup=_owner_keyboard())
        return

    if state == "set_tz":
        owner = await _get_active_owner(chat_id, message)
        if not owner:
            return
        try:
            offset = int(message.text.strip().lstrip("+"))
            if not -12 <= offset <= 14:
                raise ValueError
            await db.update_owner(chat_id, tz_offset=offset)
            await message.answer(f"✅ Часовой пояс: UTC{offset:+d}", reply_markup=_owner_keyboard())
        except ValueError:
            _pending_state[chat_id] = "set_tz"
            await message.answer(
                "Введите число от -12 до +14, например: 3",
                reply_markup=_cancel_keyboard(),
            )
        return

    if state == "set_checkin_time":
        owner = await _get_active_owner(chat_id, message)
        if not owner:
            return
        minutes = checkin_module.parse_time(message.text)
        if minutes is None:
            _pending_state[chat_id] = "set_checkin_time"
            await message.answer(
                "Неверный формат. Введите время как <code>09:00</code>:",
                parse_mode="HTML",
                reply_markup=_cancel_keyboard(),
            )
            return
        await db.ensure_checkin(chat_id)
        await db.update_checkin(
            chat_id, enabled=1, time_minutes=minutes, state="idle", attempts=0, last_asked_at=0
        )
        t = checkin_module.fmt_time(minutes)
        await message.answer(
            f"✅ Авточек включён. Каждый день в {t} бот будет спрашивать «Всё в порядке?».",
            reply_markup=_owner_keyboard(),
        )
        return

    # Silent checkin confirmation — any text from owner proves they're alive
    await checkin_module.confirm(chat_id)

    # Forward as subscriber reply to owner(s)
    try:
        owners = await db.get_owners_for_contact(chat_id, db.TELEGRAM)
        if not owners:
            return
        fallback_name = message.from_user.full_name
        text = message.text.strip()
        for owner in owners:
            # Use the name THIS owner gave the contact (may be renamed for Alice).
            display = await db.get_contact_name(owner.chat_id, chat_id, db.TELEGRAM) or fallback_name
            await db.add_reply(owner.chat_id, chat_id, display, text, db.TELEGRAM)
            try:
                await message.bot.send_message(
                    owner.chat_id,
                    f"💬 Ответ от {display}:\n{text}",
                )
            except Exception:
                logger.exception("Failed to forward reply to owner %d", owner.chat_id)
        await message.answer("✅ Ваш ответ отправлен.")
    except Exception:
        logger.exception("handle_text error")
        await message.answer("Ошибка при отправке ответа.")


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
