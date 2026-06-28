import time
import uuid
from dataclasses import dataclass

import aiosqlite

_conn: aiosqlite.Connection | None = None


@dataclass
class Owner:
    chat_id: int
    name: str
    sos_message: str
    tz_offset: int
    webhook_token: str


async def init(path: str) -> None:
    global _conn
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.executescript("""
        CREATE TABLE IF NOT EXISTS owners (
            chat_id       INTEGER PRIMARY KEY,
            name          TEXT    NOT NULL,
            sos_message   TEXT    NOT NULL DEFAULT '🆘 ТРЕВОГА! Мне нужна помощь!',
            tz_offset     INTEGER NOT NULL DEFAULT 0,
            webhook_token TEXT    NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id  INTEGER NOT NULL REFERENCES owners(chat_id) ON DELETE CASCADE,
            chat_id   INTEGER NOT NULL,
            name      TEXT    NOT NULL,
            UNIQUE(owner_id, chat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_id);
        CREATE TABLE IF NOT EXISTS replies (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id     INTEGER NOT NULL REFERENCES owners(chat_id) ON DELETE CASCADE,
            contact_id   INTEGER NOT NULL,
            contact_name TEXT    NOT NULL,
            text         TEXT    NOT NULL,
            created_at   INTEGER NOT NULL,
            read         INTEGER NOT NULL DEFAULT 0
        );
    """)
    await _conn.commit()


async def close() -> None:
    if _conn:
        await _conn.close()


def _conn_or_error() -> aiosqlite.Connection:
    assert _conn is not None, "DB not initialised — call db.init() first"
    return _conn


def _row_to_owner(row) -> Owner:
    return Owner(
        chat_id=row["chat_id"],
        name=row["name"],
        sos_message=row["sos_message"],
        tz_offset=row["tz_offset"],
        webhook_token=row["webhook_token"],
    )


async def get_owner(chat_id: int) -> Owner | None:
    async with _conn_or_error().execute(
        "SELECT * FROM owners WHERE chat_id = ?", (chat_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_owner(row) if row else None


async def get_owner_by_token(token: str) -> Owner | None:
    async with _conn_or_error().execute(
        "SELECT * FROM owners WHERE webhook_token = ?", (token,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_owner(row) if row else None


async def create_owner(chat_id: int, name: str) -> Owner:
    token = str(uuid.uuid4())
    conn = _conn_or_error()
    await conn.execute(
        "INSERT INTO owners(chat_id, name, webhook_token) VALUES (?, ?, ?)",
        (chat_id, name, token),
    )
    await conn.commit()
    return Owner(chat_id=chat_id, name=name,
                 sos_message="🆘 ТРЕВОГА! Мне нужна помощь!",
                 tz_offset=0, webhook_token=token)


async def update_owner(chat_id: int, **fields) -> None:
    allowed = {"name", "sos_message", "tz_offset"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    conn = _conn_or_error()
    await conn.execute(
        f"UPDATE owners SET {cols} WHERE chat_id = ?",
        (*updates.values(), chat_id),
    )
    await conn.commit()


async def delete_owner(chat_id: int) -> None:
    conn = _conn_or_error()
    await conn.execute("DELETE FROM owners WHERE chat_id = ?", (chat_id,))
    await conn.commit()


async def get_contacts(owner_id: int) -> dict[int, str]:
    async with _conn_or_error().execute(
        "SELECT chat_id, name FROM contacts WHERE owner_id = ?", (owner_id,)
    ) as cur:
        rows = await cur.fetchall()
    return {row["chat_id"]: row["name"] for row in rows}


async def add_contact(owner_id: int, chat_id: int, name: str) -> bool:
    try:
        conn = _conn_or_error()
        await conn.execute(
            "INSERT INTO contacts(owner_id, chat_id, name) VALUES (?, ?, ?)",
            (owner_id, chat_id, name),
        )
        await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def remove_contact(owner_id: int, chat_id: int) -> bool:
    conn = _conn_or_error()
    cur = await conn.execute(
        "DELETE FROM contacts WHERE owner_id = ? AND chat_id = ?",
        (owner_id, chat_id),
    )
    await conn.commit()
    return cur.rowcount > 0


async def remove_subscriber(chat_id: int) -> int:
    """Remove this chat_id from ALL owners' contact lists."""
    conn = _conn_or_error()
    cur = await conn.execute(
        "DELETE FROM contacts WHERE chat_id = ?", (chat_id,)
    )
    await conn.commit()
    return cur.rowcount


async def contact_exists(owner_id: int, chat_id: int) -> bool:
    async with _conn_or_error().execute(
        "SELECT 1 FROM contacts WHERE owner_id = ? AND chat_id = ?",
        (owner_id, chat_id),
    ) as cur:
        return await cur.fetchone() is not None


async def get_owners_for_contact(contact_id: int) -> list[Owner]:
    """Return all owners who have this chat_id in their contacts list."""
    async with _conn_or_error().execute(
        "SELECT o.* FROM owners o JOIN contacts c ON c.owner_id = o.chat_id WHERE c.chat_id = ?",
        (contact_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_owner(row) for row in rows]


async def add_reply(owner_id: int, contact_id: int, contact_name: str, text: str) -> None:
    conn = _conn_or_error()
    await conn.execute(
        "INSERT INTO replies(owner_id, contact_id, contact_name, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (owner_id, contact_id, contact_name, text, int(time.time())),
    )
    await conn.commit()


async def get_unread_replies(owner_id: int) -> list[dict]:
    async with _conn_or_error().execute(
        "SELECT contact_id, contact_name, text FROM replies WHERE owner_id = ? AND read = 0 ORDER BY created_at",
        (owner_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"contact_id": row["contact_id"], "contact_name": row["contact_name"], "text": row["text"]} for row in rows]


async def mark_replies_read(owner_id: int) -> None:
    conn = _conn_or_error()
    await conn.execute("UPDATE replies SET read = 1 WHERE owner_id = ? AND read = 0", (owner_id,))
    await conn.commit()
