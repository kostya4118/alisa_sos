import asyncio
import json
from pathlib import Path

from config import settings

_lock = asyncio.Lock()


def _load_raw() -> dict:
    path = Path(settings.contacts_file)
    if not path.exists():
        return {"contacts": {}}
    with open(path) as f:
        return json.load(f)


def _save_raw(data: dict) -> None:
    with open(settings.contacts_file, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def get_contacts() -> dict[int, str]:
    """Returns {chat_id: name} mapping of all subscribers."""
    async with _lock:
        data = _load_raw()
        return {int(k): v for k, v in data.get("contacts", {}).items()}


async def add_contact(chat_id: int, name: str) -> bool:
    """Returns True if newly added, False if already existed."""
    async with _lock:
        data = _load_raw()
        contacts = data.setdefault("contacts", {})
        key = str(chat_id)
        if key in contacts:
            return False
        contacts[key] = name
        _save_raw(data)
        return True


async def remove_contact(chat_id: int) -> bool:
    """Returns True if removed, False if wasn't in list."""
    async with _lock:
        data = _load_raw()
        contacts = data.setdefault("contacts", {})
        key = str(chat_id)
        if key not in contacts:
            return False
        del contacts[key]
        _save_raw(data)
        return True


async def contact_exists(chat_id: int) -> bool:
    contacts = await get_contacts()
    return chat_id in contacts
