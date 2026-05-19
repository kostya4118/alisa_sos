"""Telegram bot for managing emergency contacts."""

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    CallbackQuery,
)

import notifier
import storage
from config import settings

logger = logging.getLogger(__name__)

router = Router()


def _admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Контакты"), KeyboardButton(text="🆘 Тест SOS")],
            [KeyboardButton(text="📊 Статус")],
        ],
        resize_keyboard=True,
    )


def _subscribe_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Подписаться на оповещения")]],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    is_admin = message.from_user.id == settings.admin_chat_id
    if is_admin:
        await message.answer(
            f"👋 Привет, {settings.owner_name}!\n\n"
            "Это ваш бот экстренного оповещения.\n\n"
            "Когда Алиса получит команду «SOS», бот разошлёт сообщение всем подписчикам.\n\n"
            "Команды:\n"
            "/contacts — список подписчиков\n"
            "/test — тестовый SOS\n"
            "/sos — немедленный SOS без Алисы",
            reply_markup=_admin_keyboard(),
        )
    else:
        in_list = await storage.contact_exists(message.from_user.id)
        status = "✅ Вы уже подписаны на оповещения." if in_list else "Подпишитесь, чтобы получать сигналы тревоги."
        await message.answer(
            f"👋 Привет, {message.from_user.first_name}!\n\n"
            "Этот бот отправляет экстренные оповещения.\n\n"
            f"{status}",
            reply_markup=None if in_list else _subscribe_keyboard(),
        )


@router.message(Command("subscribe"))
@router.message(F.text == "✅ Подписаться на оповещения")
async def cmd_subscribe(message: Message) -> None:
    name = message.from_user.full_name
    added = await storage.add_contact(message.from_user.id, name)
    if added:
        await message.answer(
            "✅ Вы подписались на экстренные оповещения!\n"
            "Вы будете получать сообщения, если владелец активирует SOS через Алису.\n\n"
            "Для отписки используйте /unsubscribe",
            reply_markup=None,
        )
        await message.bot.send_message(
            settings.admin_chat_id,
            f"👤 Новый подписчик: {name} (id: {message.from_user.id})",
        )
    else:
        await message.answer("Вы уже подписаны. Для отписки: /unsubscribe")


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    removed = await storage.remove_contact(message.from_user.id)
    if removed:
        await message.answer("❌ Вы отписались от оповещений.")
        await message.bot.send_message(
            settings.admin_chat_id,
            f"👤 Отписался: {message.from_user.full_name} (id: {message.from_user.id})",
        )
    else:
        await message.answer("Вы не были подписаны. /subscribe — чтобы подписаться.")


@router.message(Command("contacts"))
@router.message(F.text == "👥 Контакты")
async def cmd_contacts(message: Message) -> None:
    if message.from_user.id != settings.admin_chat_id:
        return
    contacts = await storage.get_contacts()
    if not contacts:
        await message.answer("Список контактов пуст. Попросите друзей написать боту /subscribe")
        return

    lines = [f"👥 Подписчики ({len(contacts)}):\n"]
    for i, (chat_id, name) in enumerate(contacts.items(), 1):
        lines.append(f"{i}. {name} (id: {chat_id})")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {name}", callback_data=f"remove:{chat_id}")]
        for chat_id, name in contacts.items()
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("remove:"))
async def callback_remove_contact(callback: CallbackQuery) -> None:
    if callback.from_user.id != settings.admin_chat_id:
        await callback.answer("Нет доступа")
        return
    chat_id = int(callback.data.split(":")[1])
    contacts = await storage.get_contacts()
    name = contacts.get(chat_id, str(chat_id))
    await storage.remove_contact(chat_id)
    await callback.answer(f"❌ {name} удалён из списка")
    await callback.message.delete()


@router.message(Command("test"))
@router.message(F.text == "🆘 Тест SOS")
async def cmd_test(message: Message) -> None:
    if message.from_user.id != settings.admin_chat_id:
        return
    await message.answer("Отправляю тестовый SOS...")
    sent, failed = await notifier.send_sos(message.bot, extra_message="[ТЕСТ — не паникуйте!]")
    await message.answer(f"✅ Тест завершён: {sent} доставлено, {failed} ошибок")


@router.message(Command("sos"))
@router.message(F.text == "🆘 Тест SOS")
async def cmd_sos(message: Message) -> None:
    if message.from_user.id != settings.admin_chat_id:
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆘 ДА, ОТПРАВИТЬ SOS", callback_data="confirm_sos"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_sos"),
    ]])
    await message.answer("⚠️ Отправить экстренный SOS всем контактам?", reply_markup=keyboard)


@router.callback_query(F.data == "confirm_sos")
async def callback_confirm_sos(callback: CallbackQuery) -> None:
    if callback.from_user.id != settings.admin_chat_id:
        await callback.answer("Нет доступа")
        return
    await callback.message.edit_text("🆘 Отправляю SOS...")
    sent, failed = await notifier.send_sos(callback.message.bot)
    await callback.message.edit_text(
        f"🆘 SOS отправлен!\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}"
    )


@router.callback_query(F.data == "cancel_sos")
async def callback_cancel_sos(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")


@router.message(Command("status"))
@router.message(F.text == "📊 Статус")
async def cmd_status(message: Message) -> None:
    if message.from_user.id != settings.admin_chat_id:
        return
    contacts = await storage.get_contacts()
    await message.answer(
        f"📊 Статус бота\n\n"
        f"👤 Владелец: {settings.owner_name}\n"
        f"👥 Подписчиков: {len(contacts)}\n"
        f"🤖 Бот работает\n"
        f"🔗 Alice webhook: /alice\n\n"
        f"Для добавления контактов попросите их написать боту: /subscribe"
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
