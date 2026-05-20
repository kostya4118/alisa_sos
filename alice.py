"""
Yandex Alice (Яндекс Алиса) skill webhook handler.

Skill setup in Yandex Dialogs (https://dialogs.yandex.ru/developer):
  - Activation phrase: "СОС" / "тревога" / "помощь"
  - Webhook URL: https://your-domain.com/alice
"""

import logging

from aiogram import Bot
from fastapi import APIRouter, Header, HTTPException, Request

import notifier
import storage
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

_CONFIRM_WORDS = {"да", "конечно", "yes", "ок", "ok", "давай", "подтверждаю", "отправляй"}
_CANCEL_WORDS = {"нет", "отмена", "cancel", "стоп", "не надо", "отменить"}
_DONE_WORDS = {"всё", "все", "готово", "достаточно", "хватит", "ладно", "закончить"}
_ALL_WORDS = {"всем", "всё", "все", "всем контактам"}
_SPECIFIC_WORDS = {"конкретному", "одному", "выбрать", "определённому"}

_sessions: dict[str, dict] = {}


def _alice_response(text: str, *, end_session: bool = False, buttons: list[str] | None = None) -> dict:
    response: dict = {
        "version": "1.0",
        "response": {
            "text": text,
            "tts": text,
            "end_session": end_session,
        },
    }
    if buttons:
        response["response"]["buttons"] = [
            {"title": b, "hide": True} for b in buttons
        ]
    return response


def _find_contact(query: str, contacts: dict[int, str]) -> tuple[int, str] | None:
    query = query.lower().strip()
    for chat_id, name in contacts.items():
        name_lower = name.lower()
        parts = name_lower.split()
        if query == name_lower or query in parts or any(p.startswith(query) for p in parts):
            return chat_id, name
    return None


def _contact_first_names(contacts: dict[int, str]) -> str:
    return ", ".join(name.split()[0] for name in contacts.values())


@router.post("/alice")
async def alice_webhook(
    request: Request,
    x_alice_secret: str | None = Header(default=None, alias="X-Alice-Secret"),
):
    if settings.alice_secret and x_alice_secret != settings.alice_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    body: dict = await request.json()
    bot: Bot = request.app.state.bot

    session = body.get("session", {})
    req = body.get("request", {})
    session_id: str = session.get("session_id", "")
    is_new_session: bool = session.get("new", False)
    command: str = req.get("command", "").strip().lower()

    if is_new_session:
        contacts = await storage.get_contacts()
        if not contacts:
            return _alice_response(
                "Список контактов пуст. Добавьте контакты через Telegram бота.",
                end_session=True,
            )
        count = len(contacts)
        _sessions[session_id] = {"state": "awaiting_recipient"}
        buttons = ["Всем", "Конкретному"] if count > 1 else ["Да"]
        return _alice_response(
            f"Навык экстренного оповещения. "
            f"Отправить SOS всем {count} контактам или конкретному человеку?",
            buttons=buttons,
        )

    state_data = _sessions.get(session_id, {"state": "awaiting_recipient"})
    state = state_data["state"]

    if state == "awaiting_recipient":
        if any(w in command for w in _CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено. Будьте в безопасности.", end_session=True)

        if any(w in command for w in _ALL_WORDS) or any(w in command for w in _CONFIRM_WORDS):
            _sessions[session_id] = {"state": "awaiting_message", "recipient_ids": None}
            return _alice_response(
                "Отправляю всем. Хотите добавить сообщение? Скажите что передать или 'всё'.",
                buttons=["Всё"],
            )

        if any(w in command for w in _SPECIFIC_WORDS):
            contacts = await storage.get_contacts()
            _sessions[session_id] = {"state": "awaiting_name"}
            return _alice_response(
                f"Кому отправить? Назовите имя. Доступные контакты: {_contact_first_names(contacts)}.",
            )

        contacts = await storage.get_contacts()
        match = _find_contact(command, contacts)
        if match:
            chat_id, name = match
            _sessions[session_id] = {
                "state": "awaiting_specific_confirmation",
                "recipient_id": chat_id,
                "recipient_name": name,
            }
            return _alice_response(f"Отправить SOS контакту {name}?", buttons=["Да", "Нет"])

        return _alice_response(
            "Скажите 'всем' чтобы оповестить всех, или 'конкретному' чтобы выбрать человека.",
            buttons=["Всем", "Конкретному"],
        )

    if state == "awaiting_name":
        if any(w in command for w in _CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено.", end_session=True)

        contacts = await storage.get_contacts()
        match = _find_contact(command, contacts)
        if match:
            chat_id, name = match
            _sessions[session_id] = {
                "state": "awaiting_specific_confirmation",
                "recipient_id": chat_id,
                "recipient_name": name,
            }
            return _alice_response(f"Отправить SOS контакту {name}?", buttons=["Да", "Нет"])

        return _alice_response(
            f"Не нашла такого контакта. Попробуйте ещё раз. "
            f"Доступные: {_contact_first_names(contacts)}.",
        )

    if state == "awaiting_specific_confirmation":
        name = state_data["recipient_name"]

        if any(w in command for w in _CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено.", end_session=True)

        if any(w in command for w in _CONFIRM_WORDS):
            _sessions[session_id] = {
                "state": "awaiting_message",
                "recipient_ids": [state_data["recipient_id"]],
                "recipient_name": name,
            }
            return _alice_response(
                f"Хорошо. Хотите добавить сообщение для {name}? Скажите что передать или 'всё'.",
                buttons=["Всё"],
            )

        return _alice_response(
            f"Отправить SOS контакту {name}? Скажите да или нет.",
            buttons=["Да", "Нет"],
        )

    if state == "awaiting_message":
        extra = ""
        if not any(w in command for w in _DONE_WORDS):
            extra = req.get("original_utterance", command)

        recipient_ids: list[int] | None = state_data.get("recipient_ids")
        sent, failed = await notifier.send_sos(bot, extra_message=extra, contact_ids=recipient_ids)
        _sessions.pop(session_id, None)

        if sent == 0:
            reply = "Не удалось отправить. Проверьте список контактов."
        else:
            recipient_name = state_data.get("recipient_name")
            if recipient_name:
                reply = f"SOS отправлен контакту {recipient_name}. Держитесь!"
            else:
                reply = f"SOS отправлен {sent} контактам. Помощь в пути. Держитесь!"
            if failed:
                reply += f" Не удалось доставить {failed}."

        logger.info("Alice SOS: sent=%d failed=%d extra=%r recipients=%r", sent, failed, extra, recipient_ids)
        return _alice_response(reply, end_session=True)

    _sessions.pop(session_id, None)
    return _alice_response("Что-то пошло не так. Попробуйте снова.", end_session=True)
