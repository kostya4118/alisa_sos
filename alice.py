"""
Yandex Alice (Яндекс Алиса) skill webhook handler — multi-tenant version.

Each owner gets a personal webhook URL:
    POST /alice/{webhook_token}

The webhook_token is generated at /register and must be pasted into the
owner's Yandex Dialogs skill settings as the Webhook URL.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

import db
import email_out
import messaging
import notifier

logger = logging.getLogger(__name__)
router = APIRouter()

_CONFIRM_WORDS = {"да", "конечно", "yes", "ок", "ok", "давай", "подтверждаю", "отправляй"}
_CANCEL_WORDS = {"нет", "отмена", "cancel", "стоп", "не надо", "отменить"}
_DONE_WORDS = {"всё", "все", "готово", "достаточно", "хватит", "ладно", "закончить"}
_ALL_WORDS = {"всем", "всё", "все", "всем контактам"}
_SPECIFIC_WORDS = {"конкретному", "одному", "выбрать", "определённому"}
_SKIP_WORDS = {"пропустить", "пропусти", "дальше", "пропускаю", "skip"}
_PHONE_WORDS = {"телефон", "через телефон", "по телефону", "звонок", "позвони", "позвонить", "приложение"}
_NOMSG_WORDS = {"без сообщения", "не надо сообщения", "без текста", "пусто"}

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


def _find_contact(query: str, contacts: list[db.Contact]) -> db.Contact | None:
    q = _norm(query.strip())
    for c in contacts:
        parts = [_norm(p) for p in c.name.split()]
        if q == _norm(c.name) or q in parts or any(p.startswith(q) for p in parts):
            return c
    return None


def _contact_first_names(contacts: list[db.Contact]) -> str:
    return ", ".join(c.first_name() for c in contacts)


def _has_phone_bridge(owner: db.Owner) -> bool:
    """True if the owner has a way to trigger a phone call/SMS (webhook or e-mail)."""
    return bool(owner.sos_webhook_url) or (bool(owner.sos_email) and email_out.enabled())


def _sos_prompt(count: int, has_phone: bool, prefix: str = "") -> tuple[str, list[str]]:
    """Recipient question text + buttons; adds the 'Телефон' option when the
    owner has a phone bridge (webhook or e-mail) configured."""
    if count > 1:
        if has_phone:
            return (f"{prefix}Отправить SOS всем {count} контактам, одному или на телефон?",
                    ["Всем", "Одному", "Телефон"])
        return (f"{prefix}Отправить SOS всем {count} контактам или одному?",
                ["Всем", "Одному"])
    if has_phone:
        return f"{prefix}Отправить SOS или на телефон?", ["Да", "Телефон"]
    return f"{prefix}Отправить SOS?", ["Да"]


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
    if owner.status != "active":
        return _alice_response(
            "Ваш аккаунт ещё не подтверждён администратором. Попробуйте позже.",
            end_session=True,
        )

    body: dict = await request.json()

    session = body.get("session", {})
    req = body.get("request", {})
    session_id: str = session.get("session_id", "")
    is_new_session: bool = session.get("new", False)
    command: str = req.get("command", "").strip().lower()
    utterance: str = req.get("original_utterance", "").strip().lower()

    def _has(words: set) -> bool:
        return any(w in command or w in utterance for w in words)

    async def _go_to_next(prefix: str = "") -> dict:
        """Check for unread replies; announce them or fall through to SOS prompt."""
        fresh = await db.get_unread_replies(owner.chat_id)
        if fresh:
            _sessions[session_id] = {
                "state": "awaiting_read_replies",
                "owner_id": owner.chat_id,
                "replies": fresh,
            }
            n = len(fresh)
            word = "ответ" if n == 1 else ("ответа" if n < 5 else "ответов")
            return _alice_response(
                f"{prefix}Есть {n} новых {word}. Зачитать?",
                buttons=["Да", "Нет"],
            )
        cnt = len(await db.get_contacts(owner.chat_id))
        _sessions[session_id] = {"state": "awaiting_recipient", "owner_id": owner.chat_id}
        text, sos_btns = _sos_prompt(cnt, _has_phone_bridge(owner), prefix)
        return _alice_response(text, buttons=sos_btns)

    if is_new_session:
        contacts = await db.get_contacts(owner.chat_id)
        if not contacts:
            return _alice_response(
                "Список контактов пуст. Добавьте контакты через Telegram бота.",
                end_session=True,
            )
        try:
            replies = await db.get_unread_replies(owner.chat_id)
        except Exception:
            logger.exception("get_unread_replies failed for owner %d", owner.chat_id)
            replies = []
        if replies:
            _sessions[session_id] = {
                "state": "awaiting_read_replies",
                "owner_id": owner.chat_id,
                "replies": replies,
            }
            word = "ответ" if len(replies) == 1 else ("ответа" if len(replies) < 5 else "ответов")
            return _alice_response(
                f"Есть {len(replies)} новых {word} от ваших контактов. Зачитать?",
                buttons=["Да", "Нет"],
            )
        count = len(contacts)
        _sessions[session_id] = {"state": "awaiting_recipient", "owner_id": owner.chat_id}
        text, buttons = _sos_prompt(count, _has_phone_bridge(owner),
                                    "Навык экстренного оповещения. ")
        return _alice_response(text, buttons=buttons)

    state_data = _sessions.get(session_id, {"state": "awaiting_recipient", "owner_id": owner.chat_id})
    state = state_data["state"]

    if state == "awaiting_read_replies":
        replies = state_data.get("replies", [])

        if _has(_CONFIRM_WORDS):
            await db.mark_replies_read(owner.chat_id)
            parts = [f"{r['contact_name']} написал: {r['text']}" for r in replies]
            replies_text = ". ".join(parts)

            # Unique senders (preserving order), keyed by (contact_id, platform)
            seen: set = set()
            unique_senders: list[dict] = []
            for r in replies:
                key = (r["contact_id"], r["platform"])
                if key not in seen:
                    seen.add(key)
                    unique_senders.append({
                        "contact_id": r["contact_id"],
                        "name": r["contact_name"],
                        "platform": r["platform"],
                    })

            if len(unique_senders) == 1:
                s = unique_senders[0]
                _sessions[session_id] = {
                    "state": "awaiting_reply_text",
                    "owner_id": owner.chat_id,
                    "reply_contact_id": s["contact_id"],
                    "reply_contact_name": s["name"],
                    "reply_platform": s["platform"],
                }
                return _alice_response(
                    f"{replies_text}. Хотите ответить {s['name']}? Скажите что передать или 'пропустить'.",
                    buttons=["Пропустить"],
                )
            else:
                names = ", ".join(s["name"] for s in unique_senders)
                _sessions[session_id] = {
                    "state": "awaiting_reply_name",
                    "owner_id": owner.chat_id,
                    "senders": unique_senders,
                }
                return _alice_response(
                    f"{replies_text}. Хотите ответить? Назовите имя или скажите 'пропустить'. Писали: {names}.",
                    buttons=["Пропустить"],
                )

        # "нет" or anything else — mark as read, check for fresh ones, then SOS
        await db.mark_replies_read(owner.chat_id)
        return await _go_to_next()

    if state == "awaiting_reply_name":
        senders: list[dict] = state_data.get("senders", [])

        if _has(_SKIP_WORDS) or _has(_CANCEL_WORDS):
            return await _go_to_next()

        sender_contacts = [
            db.Contact(chat_id=s["contact_id"], name=s["name"], platform=s["platform"])
            for s in senders
        ]
        match = _find_contact(utterance or command, sender_contacts)
        if match:
            _sessions[session_id] = {
                "state": "awaiting_reply_text",
                "owner_id": owner.chat_id,
                "reply_contact_id": match.chat_id,
                "reply_contact_name": match.name,
                "reply_platform": match.platform,
            }
            return _alice_response(f"Что передать {match.name}?")

        names = ", ".join(s["name"] for s in senders)
        return _alice_response(
            f"Не нашла такого имени. Скажите кому ответить или 'пропустить'. Писали: {names}.",
            buttons=["Пропустить"],
        )

    if state == "awaiting_reply_text":
        cid: int = state_data["reply_contact_id"]
        cname: str = state_data["reply_contact_name"]
        cplatform: str = state_data.get("reply_platform", db.TELEGRAM)

        if _has(_SKIP_WORDS) or _has(_CANCEL_WORDS):
            return await _go_to_next()

        reply_text = utterance or command
        sent_ok = False
        try:
            await messaging.send(cplatform, cid, f"💬 {owner.name}: {reply_text}")
            sent_ok = True
        except Exception:
            logger.exception("Failed to send reply to contact %d", cid)

        prefix = f"Ответ отправлен {cname}. " if sent_ok else "Не удалось отправить ответ. "
        return await _go_to_next(prefix)

    if state == "awaiting_recipient":
        if _has(_CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено. Будьте в безопасности.", end_session=True)

        # "Телефон" — trigger the phone bridge (webhook/e-mail) for a chosen contact.
        if _has_phone_bridge(owner) and _has(_PHONE_WORDS):
            contacts = await db.get_contacts(owner.chat_id)
            if not contacts:
                _sessions.pop(session_id, None)
                return _alice_response("Список контактов пуст.", end_session=True)
            if len(contacts) == 1:
                c = contacts[0]
                _sessions[session_id] = {
                    "state": "awaiting_webhook_message",
                    "owner_id": owner.chat_id,
                    "target_name": c.name,
                }
                return _alice_response(
                    f"Что передать {c.first_name()}? Скажите сообщение или 'без сообщения'.",
                    buttons=["Без сообщения"],
                )
            _sessions[session_id] = {"state": "awaiting_webhook_name", "owner_id": owner.chat_id}
            return _alice_response(
                f"Кому позвонить? Назовите имя. Доступные: {_contact_first_names(contacts)}.",
            )

        if _has(_ALL_WORDS) or _has(_CONFIRM_WORDS):
            _sessions[session_id] = {"state": "awaiting_message", "owner_id": owner.chat_id, "recipient": None}
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
            _sessions[session_id] = {
                "state": "awaiting_specific_confirmation",
                "owner_id": owner.chat_id,
                "recipient_id": match.chat_id,
                "recipient_name": match.name,
                "recipient_platform": match.platform,
            }
            return _alice_response(f"Отправить SOS контакту {match.name}?", buttons=["Да", "Нет"])

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
            _sessions[session_id] = {
                "state": "awaiting_specific_confirmation",
                "owner_id": owner.chat_id,
                "recipient_id": match.chat_id,
                "recipient_name": match.name,
                "recipient_platform": match.platform,
            }
            return _alice_response(f"Отправить SOS контакту {match.name}?", buttons=["Да", "Нет"])

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
                "recipient": {
                    "chat_id": state_data["recipient_id"],
                    "name": name,
                    "platform": state_data.get("recipient_platform", db.TELEGRAM),
                },
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

        recipient: dict | None = state_data.get("recipient")
        contacts_subset = None
        if recipient is not None:
            contacts_subset = [db.Contact(
                chat_id=recipient["chat_id"],
                name=recipient["name"],
                platform=recipient["platform"],
            )]
        sent, failed = await notifier.send_sos(owner, extra_message=extra, contacts=contacts_subset)
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

    if state == "awaiting_webhook_name":
        if _has(_CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено.", end_session=True)
        contacts = await db.get_contacts(owner.chat_id)
        match = _find_contact(utterance or command, contacts)
        if match:
            _sessions[session_id] = {
                "state": "awaiting_webhook_message",
                "owner_id": owner.chat_id,
                "target_name": match.name,
            }
            return _alice_response(
                f"Что передать {match.first_name()}? Скажите сообщение или 'без сообщения'.",
                buttons=["Без сообщения"],
            )
        return _alice_response(
            f"Не нашла такой контакт. Назовите имя. Доступные: {_contact_first_names(contacts)}.",
        )

    if state == "awaiting_webhook_message":
        if _has(_CANCEL_WORDS):
            _sessions.pop(session_id, None)
            return _alice_response("Отменено.", end_session=True)
        target = state_data.get("target_name")
        extra = ""
        if not _has(_DONE_WORDS) and not _has(_NOMSG_WORDS):
            extra = utterance or command
        fired = await notifier.notify_integrations(owner, extra_message=extra, target=target, kind="sos")
        _sessions.pop(session_id, None)
        logger.info("Alice phone-bridge owner=%d target=%r fired=%s extra=%r",
                    owner.chat_id, target, fired, extra)
        if fired:
            return _alice_response(
                f"Отправляю на телефон контакту {target}. Держитесь!",
                end_session=True,
            )
        return _alice_response(
            "Не удалось: телефон-мост не настроен. Задайте webhook или e-mail в настройках.",
            end_session=True,
        )

    _sessions.pop(session_id, None)
    return _alice_response("Что-то пошло не так. Попробуйте снова.", end_session=True)
