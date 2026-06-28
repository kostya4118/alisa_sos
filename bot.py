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

import db
import notifier
from config import settings

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _owner_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Контакты"), KeyboardButton(text="🆘 Тест SOS")],
            [KeyboardButton(text="🔗 Ссылка для друзей"), KeyboardButton(text="📊 Статус")],
        ],
        resize_keyboard=True,
    )


def _subscribe_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Мои подписки")]],
        resize_keyboard=True,
    )


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
        added = await db.add_contact(owner.chat_id, message.from_user.id, name)
        if added:
            await message.answer(
                f"✅ Вы подписались на оповещения от {owner.name}.\n"
                "Если владелец активирует SOS через Алису — вы получите сообщение.\n\n"
                "Для отписки: /unsubscribe",
                reply_markup=_subscribe_keyboard(),
            )
            await message.bot.send_message(
                owner.chat_id,
                f"👤 Новый подписчик: {name} (id: {message.from_user.id})",
            )
        else:
            await message.answer(f"Вы уже подписаны на оповещения от {owner.name}.\nДля отписки: /unsubscribe")
        return

    # Regular /start — check if user is already an owner
    owner = await db.get_owner(message.from_user.id)
    if owner:
        await message.answer(
            f"👋 С возвращением, {owner.name}!\n\n"
            "Управляйте контактами и настройками через кнопки ниже.",
            reply_markup=_owner_keyboard(),
        )
    else:
        await message.answer(
            f"👋 Привет, {message.from_user.first_name}!\n\n"
            "Это сервис экстренного оповещения через Яндекс Алису.\n\n"
            "Если вы хотите настроить бота для себя — зарегистрируйтесь:\n"
            "/register\n\n"
            "Если вы получили ссылку от друга — перейдите по ней, чтобы подписаться.",
            reply_markup=ReplyKeyboardRemove(),
        )


# ---------------------------------------------------------------------------
# Owner registration & settings
# ---------------------------------------------------------------------------

@router.message(Command("register"))
async def cmd_register(message: Message) -> None:
    existing = await db.get_owner(message.from_user.id)
    if existing:
        await message.answer(
            f"Вы уже зарегистрированы как {existing.name}.\n"
            f"Ваш webhook: {settings.base_url}/alice/{existing.webhook_token}\n\n"
            "Используйте /settings чтобы посмотреть все настройки.",
            reply_markup=_owner_keyboard(),
        )
        return

    owner = await db.create_owner(message.from_user.id, message.from_user.full_name)
    bot_info = await message.bot.get_me()
    subscribe_link = f"https://t.me/{bot_info.username}?start=sub_{owner.webhook_token}"
    webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}"

    await message.answer(
        f"✅ Вы зарегистрированы!\n\n"
        f"<b>Webhook URL для Яндекс Диалогов:</b>\n"
        f"<code>{webhook_url}</code>\n\n"
        f"<b>Ссылка для друзей:</b>\n"
        f"{subscribe_link}\n\n"
        "Скопируйте Webhook URL и вставьте в настройки своего навыка Алисы.\n"
        "Поделитесь ссылкой с друзьями — они подпишутся одним нажатием.\n\n"
        "Настройки: /setname, /setmessage, /settz\n"
        "Удалить аккаунт: /deleteaccount",
        parse_mode="HTML",
        reply_markup=_owner_keyboard(),
    )


@router.message(Command("settings"))
@router.message(F.text == "📊 Статус")
async def cmd_settings(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
        return
    contacts = await db.get_contacts(owner.chat_id)
    bot_info = await message.bot.get_me()
    subscribe_link = f"https://t.me/{bot_info.username}?start=sub_{owner.webhook_token}"
    webhook_url = f"{settings.base_url}/alice/{owner.webhook_token}"
    await message.answer(
        f"📊 <b>Ваши настройки</b>\n\n"
        f"👤 Имя: {owner.name}\n"
        f"🕐 Часовой пояс: UTC{owner.tz_offset:+d}\n"
        f"👥 Подписчиков: {len(contacts)}\n\n"
        f"📢 Текст SOS:\n{owner.sos_message}\n\n"
        f"🔗 Webhook: <code>{webhook_url}</code>\n"
        f"👫 Ссылка для друзей:\n{subscribe_link}",
        parse_mode="HTML",
    )


@router.message(Command("setname"))
async def cmd_setname(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
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
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
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
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
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


@router.callback_query(F.data == "cancel_delete")
async def callback_cancel_delete(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")


# ---------------------------------------------------------------------------
# Contact management (owner)
# ---------------------------------------------------------------------------

@router.message(Command("contacts"))
@router.message(F.text == "👥 Контакты")
async def cmd_contacts(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
        return
    contacts = await db.get_contacts(owner.chat_id)
    if not contacts:
        bot_info = await message.bot.get_me()
        subscribe_link = f"https://t.me/{bot_info.username}?start=sub_{owner.webhook_token}"
        await message.answer(
            f"Список контактов пуст.\n\nПоделитесь ссылкой с друзьями:\n{subscribe_link}"
        )
        return

    lines = [f"👥 Подписчики ({len(contacts)}):\n"]
    for i, (chat_id, name) in enumerate(contacts.items(), 1):
        lines.append(f"{i}. {name}")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {name}", callback_data=f"remove:{chat_id}")]
        for chat_id, name in contacts.items()
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("remove:"))
async def callback_remove_contact(callback: CallbackQuery) -> None:
    owner = await db.get_owner(callback.from_user.id)
    if not owner:
        await callback.answer("Нет доступа")
        return
    chat_id = int(callback.data.split(":")[1])
    contacts = await db.get_contacts(owner.chat_id)
    name = contacts.get(chat_id, str(chat_id))
    await db.remove_contact(owner.chat_id, chat_id)
    await callback.answer(f"❌ {name} удалён")
    await callback.message.delete()


@router.message(Command("mylink"))
@router.message(F.text == "🔗 Ссылка для друзей")
async def cmd_mylink(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
        return
    bot_info = await message.bot.get_me()
    subscribe_link = f"https://t.me/{bot_info.username}?start=sub_{owner.webhook_token}"
    await message.answer(
        f"Ссылка для подписки на ваши оповещения:\n\n{subscribe_link}\n\n"
        "Отправьте её друзьям — они подпишутся одним нажатием."
    )


# ---------------------------------------------------------------------------
# SOS commands (owner)
# ---------------------------------------------------------------------------

@router.message(Command("test"))
@router.message(F.text == "🆘 Тест SOS")
async def cmd_test(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
        return
    await message.answer("Отправляю тестовый SOS...")
    sent, failed = await notifier.send_sos(
        message.bot, owner, extra_message="[ТЕСТ — не паникуйте!]"
    )
    await message.answer(f"✅ Тест завершён: {sent} доставлено, {failed} ошибок")


@router.message(Command("sos"))
async def cmd_sos(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆘 ДА, ОТПРАВИТЬ SOS", callback_data="confirm_sos"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_sos"),
    ]])
    await message.answer("⚠️ Отправить экстренный SOS всем контактам?", reply_markup=keyboard)


@router.callback_query(F.data == "confirm_sos")
async def callback_confirm_sos(callback: CallbackQuery) -> None:
    owner = await db.get_owner(callback.from_user.id)
    if not owner:
        await callback.answer("Нет доступа")
        return
    await callback.message.edit_text("🆘 Отправляю SOS...")
    sent, failed = await notifier.send_sos(callback.message.bot, owner)
    await callback.message.edit_text(
        f"🆘 SOS отправлен!\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}"
    )


@router.callback_query(F.data == "cancel_sos")
async def callback_cancel_sos(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")


# ---------------------------------------------------------------------------
# Subscriber commands
# ---------------------------------------------------------------------------

@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    removed = await db.remove_subscriber(message.from_user.id)
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
        f"https://t.me/botname?start=sub_..."
    )


@router.message(F.text == "📋 Мои подписки")
async def cmd_my_subscriptions(message: Message) -> None:
    owners = await db.get_owners_for_contact(message.from_user.id)
    if not owners:
        await message.answer("Вы не подписаны ни на кого.\nДля отписки от всех: /unsubscribe")
        return
    lines = ["📋 Ваши подписки:\n"]
    for i, owner in enumerate(owners, 1):
        lines.append(f"{i}. {owner.name}")
    lines.append("\nДля отписки от всех: /unsubscribe")
    await message.answer("\n".join(lines))


@router.message(Command("replies"))
async def cmd_replies(message: Message) -> None:
    owner = await db.get_owner(message.from_user.id)
    if not owner:
        await message.answer("Вы не зарегистрированы. Используйте /register")
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


@router.message(F.text & ~F.text.startswith("/"))
async def handle_subscriber_reply(message: Message) -> None:
    """Forward any plain text message from a subscriber to their owner(s)."""
    owners = await db.get_owners_for_contact(message.from_user.id)
    if not owners:
        return
    sender_name = message.from_user.full_name
    text = message.text.strip()
    for owner in owners:
        await db.add_reply(owner.chat_id, message.from_user.id, sender_name, text)
        try:
            await message.bot.send_message(
                owner.chat_id,
                f"💬 Ответ от {sender_name}:\n{text}",
            )
        except Exception:
            logger.exception("Failed to forward reply to owner %d", owner.chat_id)
    await message.answer("✅ Ваш ответ отправлен.")


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
