"""
Yandex Alice (Яндекс Алиса) skill webhook handler — multi-tenant version.

Each owner gets a personal webhook URL:
    POST /alice/{webhook_token}

The webhook_token is generated at /register and must be pasted into the
owner's Yandex Dialogs skill settings as the Webhook URL.
"""

import logging

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Request

import db
import notifier

logger = logging.getLogger(__name__)
router = APIRouter()

_CONFIRM_WORDS = {"да", "конечно", "yes", "ок", "ok", "давай", "подтверждаю", "отправляй"}
_CANCEL_WORDS = {"нет", "отмена", "cancel", "стоп", "не надо", "отменить"}
_DONE_WORDS = {"всё", "все", "готово", "достаточно", "хватит", "ладно", "закончить"}
_ALL_WORDS = {"всем", "всё", "все", "всем контактам"}
_SPECIFIC_WORDS = {"конкретному", "одному", "выбрать", "определённому"}

_sessions: dict[str, dict] = {}

_LAT_TO_CYR: dict[str, str] = {
    'a': 'а', 'b': 'б', 'c': 'с', 'd': 'д', 'e': 'е', 'f': 'ф',
    'g': 'г', 'h': 'х', 'i': 'и', 'j': 'й', 'k': 'к', 'l': 'л',
    'm': 'м', 'n': 'н', 'o': 'о', 'p': 'п', 'q': 'к', 'r': 'р',
    's': 'с', 't': 'т', 'u': 'у', 'v': 'в', 'w': 'в', 'x': 'х',
    'y': 'й', 'z': 'з',
}


def _norm(s: str) -> str:
    return ''.join(_LAT_TO_CYR.get(ch, ch) for ch in s.lower())


def _find_contact(query: str, contacts: dict[int, str]) -> tuple[int, str] | None:
    q = _norm(query.strip())
    for chat_id, name in contacts.items():
        parts = [_norm(p) for p in name.split()]
        if q == _norm(name) or q in parts or any(p.startswith(q) for p in parts):
            return chat_id, name
    return None


def _contact_first_names(contacts: dict[int, str]) -> str:
    return ", ".join(name.split()[0] for name in contacts.values())


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
        response["response"]["buttons"] = [{"title": b, "hide": True} for b in buttons]
    return response


@router.post("/alice/{webhook_token}")
async def alice_webhook(webhook_token: str, request: Request):
    owner = await db.get_owner_by_token(webhook_token)
    if owner is None:
        raise HTTPException(status_code=404, detail="Unknown webhook token")

    bot: Bot = request.app.state.bot
    body: dict = await request.json()

    session = body.get("session", {})
    req = body.get("request", {})
    session_id: str = session.get("session_id", "")
    is_new_session: bool = session.get("new", False)
    command: str = req.get("command", "").strip().lower()
    utterance: str = req.get("original_utterance", "").strip().lower()

    def _has(words: set) -> bool:
        return any(w in command or w in utterance for w in words)

    if is_new_session:
        contacts = await db.get_contacts(owner.chat_id)
        if not contacts:
            return _alice_response(
                "Список контактов пуст. Добавьте контакты через Telegram бота.",
                end_session=True,
            )
        count = len(contacts)
        _sessions[session_id] = {"state": "awaiting_recipient", "owner_id": owner.chat_id}
        buttons = ["Всем", "Одному"] if count > 1 else ["Да"]
        return _alice_response(
            f"Навык экстренного оповещения. "
            f"Отправить SOS всем {count} контактам или одному?",
            buttons=buttons,
        )

    state_data = _sessions.get(session_id, {"state": "awaiting_recipient", "owner_id": owner.chat_id})
    state = state_data["state"]

    if state == "awaiting_recipient":
        if _has(_CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено. Будьте в безопасности.", end_session=True)

        if _has(_ALL_WORDS) or _has(_CONFIRM_WORDS):
            _sessions[session_id] = {"state": "awaiting_message", "owner_id": owner.chat_id, "recipient_ids": None}
            return _alice_response(
                "Отправляю всем. Хотите добавить сообщение? Скажите что передать или 'всё'.",
                buttons=["Всё"],
            )

        if _has(_SPECIFIC_WORDS):
            contacts = await db.get_contacts(owner.chat_id)
            _sessions[session_id] = {"state": "awaiting_name", "owner_id": owner.chat_id}
            return _alice_response(
                f"Кому отправить? Назовите имя. Доступные контакты: {_contact_first_names(contacts)}.",
            )

        contacts = await db.get_contacts(owner.chat_id)
        match = _find_contact(utterance or command, contacts)
        if match:
            chat_id, name = match
            _sessions[session_id] = {
                "state": "awaiting_specific_confirmation",
                "owner_id": owner.chat_id,
                "recipient_id": chat_id,
                "recipient_name": name,
            }
            return _alice_response(f"Отправить SOS контакту {name}?", buttons=["Да", "Нет"])

        return _alice_response(
            "Скажите 'всем' чтобы оповестить всех, или 'одному' чтобы выбрать человека.",
            buttons=["Всем", "Одному"],
        )

    if state == "awaiting_name":
        if _has(_CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено.", end_session=True)

        contacts = await db.get_contacts(owner.chat_id)
        match = _find_contact(utterance or command, contacts)
        if match:
            chat_id, name = match
            _sessions[session_id] = {
                "state": "awaiting_specific_confirmation",
                "owner_id": owner.chat_id,
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

        if _has(_CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено.", end_session=True)

        if _has(_CONFIRM_WORDS):
            _sessions[session_id] = {
                "state": "awaiting_message",
                "owner_id": owner.chat_id,
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
        if not _has(_DONE_WORDS):
            extra = utterance or command

        recipient_ids: list[int] | None = state_data.get("recipient_ids")
        sent, failed = await notifier.send_sos(bot, owner, extra_message=extra, contact_ids=recipient_ids)
        _sessions.pop(session_id, None)

        if sent == 0:
            reply = "Не удалось отправить. Проверьте список контактов."
        else:
            recipient_name = state_data.get("recipient_name")
            reply = (
                f"SOS отправлен контакту {recipient_name}. Держитесь!"
                if recipient_name
                else f"SOS отправлен {sent} контактам. Помощь в пути. Держитесь!"
            )
            if failed:
                reply += f" Не удалось доставить {failed}."

        logger.info("Alice SOS owner=%d sent=%d failed=%d extra=%r", owner.chat_id, sent, failed, extra)
        return _alice_response(reply, end_session=True)

    _sessions.pop(session_id, None)
    return _alice_response("Что-то пошло не так. Попробуйте снова.", end_session=True)
