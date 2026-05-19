"""
Yandex Alice (Яндекс Алиса) skill webhook handler.

Skill setup in Yandex Dialogs (https://dialogs.yandex.ru/developer):
  - Activation phrase: "СОС" / "тревога" / "помощь"
  - Webhook URL: https://your-domain.com/alice
  - Set the ALICE_SECRET env var and add it to the skill's header config
    (Header: X-Alice-Secret: <value>) for request verification.
"""

import logging

from aiogram import Bot
from fastapi import APIRouter, Header, HTTPException, Request

import notifier
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

_CONFIRM_WORDS = {"да", "конечно", "yes", "ок", "ok", "давай", "подтверждаю"}
_CANCEL_WORDS = {"нет", "отмена", "cancel", "стоп", "не надо", "отменить"}
_DONE_WORDS = {"всё", "все", "готово", "достаточно", "хватит", "ладно", "закончить"}

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
        _sessions[session_id] = {"state": "awaiting_confirmation"}
        return _alice_response(
            "Навык экстренного оповещения активирован. "
            "Отправить SOS всем контактам?",
            buttons=["Да", "Нет"],
        )

    state_data = _sessions.get(session_id, {"state": "awaiting_confirmation"})
    state = state_data["state"]

    if state == "awaiting_confirmation":
        if any(w in command for w in _CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено. Будьте в безопасности.", end_session=True)

        if any(w in command for w in _CONFIRM_WORDS) or command in ("sos", "с о с"):
            _sessions[session_id] = {"state": "awaiting_message"}
            return _alice_response(
                "SOS отправляю. Хотите добавить сообщение? "
                "Скажите что передать или 'всё' чтобы завершить.",
                buttons=["Всё"],
            )

        return _alice_response(
            "Не расслышала. Скажите 'да' чтобы отправить сигнал тревоги, "
            "или 'нет' для отмены.",
            buttons=["Да", "Нет"],
        )

    if state == "awaiting_message":
        extra = ""
        end = False

        if any(w in command for w in _DONE_WORDS):
            end = True
        else:
            extra = req.get("original_utterance", command)

        sent, failed = await notifier.send_sos(bot, extra_message=extra)
        _sessions.pop(session_id, None)

        if sent == 0:
            reply = "Список контактов пуст. Добавьте контакты через Telegram бота."
        else:
            reply = f"SOS отправлен {sent} контактам. Помощь в пути. Держитесь!"
            if failed:
                reply += f" Не удалось доставить {failed}."

        logger.info("Alice SOS: sent=%d failed=%d extra=%r", sent, failed, extra)
        return _alice_response(reply, end_session=True)

    _sessions.pop(session_id, None)
    return _alice_response("Что-то пошло не так. Попробуйте снова.", end_session=True)
