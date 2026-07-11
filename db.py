import time
import uuid
from dataclasses import dataclass, field

import aiosqlite

_conn: aiosqlite.Connection | None = None

# Supported messaging platforms
TELEGRAM = "telegram"
MAX = "max"


@dataclass
class Owner:
    chat_id: int
    name: str
    sos_message: str
    tz_offset: int
    webhook_token: str
    status: str = field(default="active")      # "pending" | "active"
    platform: str = field(default=TELEGRAM)    # "telegram" | "max"


@dataclass
class Contact:
    chat_id: int
    name: str
    platform: str = TELEGRAM

    def first_name(self) -> str:
        return self.name.split()[0] if self.name else self.name


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
            webhook_token TEXT    NOT NULL UNIQUE,
            status        TEXT    NOT NULL DEFAULT 'active',
            platform      TEXT    NOT NULL DEFAULT 'telegram'
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id  INTEGER NOT NULL REFERENCES owners(chat_id) ON DELETE CASCADE,
            chat_id   INTEGER NOT NULL,
            name      TEXT    NOT NULL,
            platform  TEXT    NOT NULL DEFAULT 'telegram',
            UNIQUE(owner_id, chat_id, platform)
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_id);
        CREATE TABLE IF NOT EXISTS checkins (
            owner_id      INTEGER PRIMARY KEY REFERENCES owners(chat_id) ON DELETE CASCADE,
            enabled       INTEGER NOT NULL DEFAULT 0,
            time_minutes  INTEGER NOT NULL DEFAULT 540,
            state         TEXT    NOT NULL DEFAULT 'idle',
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_asked_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS replies (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id     INTEGER NOT NULL REFERENCES owners(chat_id) ON DELETE CASCADE,
            contact_id   INTEGER NOT NULL,
            contact_name TEXT    NOT NULL,
            text         TEXT    NOT NULL,
            created_at   INTEGER NOT NULL,
            read         INTEGER NOT NULL DEFAULT 0,
            platform     TEXT    NOT NULL DEFAULT 'telegram'
        );
        CREATE TABLE IF NOT EXISTS sos_log (
            contact_id INTEGER NOT NULL,
            platform   TEXT    NOT NULL,
            owner_id   INTEGER NOT NULL,
            sent_at    INTEGER NOT NULL,
            PRIMARY KEY (contact_id, platform, owner_id)
        );
    """)
    await _conn.commit()
    # Idempotent column additions for installations created before these columns existed.
    for table, col, ddl in (
        ("owners", "status", "ALTER TABLE owners ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
        ("owners", "platform", "ALTER TABLE owners ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'"),
        ("contacts", "platform", "ALTER TABLE contacts ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'"),
        ("replies", "platform", "ALTER TABLE replies ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'"),
    ):
        try:
            await _conn.execute(ddl)
            await _conn.commit()
        except aiosqlite.OperationalError:
            pass  # column already exists

    # Index on the (now-guaranteed) platform column — created after migrations
    # so it also works on databases upgraded from the pre-platform schema.
    await _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contacts_chat ON contacts(chat_id, platform)"
    )
    await _conn.commit()


async def close() -> None:
    if _conn:
        await _conn.close()


def _conn_or_error() -> aiosqlite.Connection:
    assert _conn is not None, "DB not initialised — call db.init() first"
    return _conn


def _row_to_owner(row) -> Owner:
    d = dict(row)
    return Owner(
        chat_id=d["chat_id"],
        name=d["name"],
        sos_message=d["sos_message"],
        tz_offset=d["tz_offset"],
        webhook_token=d["webhook_token"],
        status=d.get("status", "active"),
        platform=d.get("platform", TELEGRAM),
    )


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------

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


async def get_all_owners() -> list[Owner]:
    """All owners ordered by status (pending first) then name."""
    async with _conn_or_error().execute(
        "SELECT * FROM owners ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, name"
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_owner(row) for row in rows]


async def create_owner(chat_id: int, name: str, status: str = "active",
                       platform: str = TELEGRAM) -> Owner:
    token = str(uuid.uuid4())
    conn = _conn_or_error()
    await conn.execute(
        "INSERT INTO owners(chat_id, name, webhook_token, status, platform) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, name, token, status, platform),
    )
    await conn.commit()
    return Owner(chat_id=chat_id, name=name,
                 sos_message="🆘 ТРЕВОГА! Мне нужна помощь!",
                 tz_offset=0, webhook_token=token, status=status, platform=platform)


async def set_owner_status(chat_id: int, status: str) -> None:
    conn = _conn_or_error()
    await conn.execute("UPDATE owners SET status = ? WHERE chat_id = ?", (status, chat_id))
    await conn.commit()


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


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

async def get_contacts(owner_id: int) -> list[Contact]:
    async with _conn_or_error().execute(
        "SELECT chat_id, name, platform FROM contacts WHERE owner_id = ? ORDER BY id",
        (owner_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [Contact(chat_id=r["chat_id"], name=r["name"], platform=r["platform"]) for r in rows]


async def add_contact(owner_id: int, chat_id: int, name: str,
                      platform: str = TELEGRAM) -> bool:
    try:
        conn = _conn_or_error()
        await conn.execute(
            "INSERT INTO contacts(owner_id, chat_id, name, platform) VALUES (?, ?, ?, ?)",
            (owner_id, chat_id, name, platform),
        )
        await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def remove_contact(owner_id: int, chat_id: int, platform: str = TELEGRAM) -> bool:
    conn = _conn_or_error()
    cur = await conn.execute(
        "DELETE FROM contacts WHERE owner_id = ? AND chat_id = ? AND platform = ?",
        (owner_id, chat_id, platform),
    )
    await conn.commit()
    return cur.rowcount > 0


async def rename_contact(owner_id: int, chat_id: int, platform: str, new_name: str) -> bool:
    """Rename one of the owner's contacts. Returns True if it existed.

    Also relabels this contact's already-stored (unread) replies so Alice
    reads out the new name too.
    """
    conn = _conn_or_error()
    cur = await conn.execute(
        "UPDATE contacts SET name = ? WHERE owner_id = ? AND chat_id = ? AND platform = ?",
        (new_name, owner_id, chat_id, platform),
    )
    await conn.execute(
        "UPDATE replies SET contact_name = ? "
        "WHERE owner_id = ? AND contact_id = ? AND platform = ?",
        (new_name, owner_id, chat_id, platform),
    )
    await conn.commit()
    return cur.rowcount > 0


async def get_contact_name(owner_id: int, chat_id: int, platform: str = TELEGRAM) -> str | None:
    """The name this owner uses for the given contact (may be renamed)."""
    async with _conn_or_error().execute(
        "SELECT name FROM contacts WHERE owner_id = ? AND chat_id = ? AND platform = ?",
        (owner_id, chat_id, platform),
    ) as cur:
        row = await cur.fetchone()
    return row["name"] if row else None


async def remove_subscriber(chat_id: int, platform: str = TELEGRAM) -> int:
    """Remove this (chat_id, platform) from ALL owners' contact lists."""
    conn = _conn_or_error()
    cur = await conn.execute(
        "DELETE FROM contacts WHERE chat_id = ? AND platform = ?", (chat_id, platform)
    )
    await conn.commit()
    return cur.rowcount


async def get_owners_for_contact(contact_id: int, platform: str = TELEGRAM) -> list[Owner]:
    """Return all owners who have this (chat_id, platform) in their contacts list."""
    async with _conn_or_error().execute(
        "SELECT o.* FROM owners o JOIN contacts c ON c.owner_id = o.chat_id "
        "WHERE c.chat_id = ? AND c.platform = ?",
        (contact_id, platform),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_owner(row) for row in rows]


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------

async def add_reply(owner_id: int, contact_id: int, contact_name: str, text: str,
                    platform: str = TELEGRAM) -> None:
    conn = _conn_or_error()
    await conn.execute(
        "INSERT INTO replies(owner_id, contact_id, contact_name, text, created_at, platform) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (owner_id, contact_id, contact_name, text, int(time.time()), platform),
    )
    await conn.commit()


async def get_unread_replies(owner_id: int) -> list[dict]:
    async with _conn_or_error().execute(
        "SELECT contact_id, contact_name, text, platform FROM replies "
        "WHERE owner_id = ? AND read = 0 ORDER BY created_at",
        (owner_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "contact_id": r["contact_id"],
            "contact_name": r["contact_name"],
            "text": r["text"],
            "platform": r["platform"],
        }
        for r in rows
    ]


async def mark_replies_read(owner_id: int) -> None:
    conn = _conn_or_error()
    await conn.execute("UPDATE replies SET read = 1 WHERE owner_id = ? AND read = 0", (owner_id,))
    await conn.commit()


# ---------------------------------------------------------------------------
# Last SOS sender (for routing a subscriber's reply back to the right owner)
# ---------------------------------------------------------------------------

async def record_sos_recipient(contact_id: int, platform: str, owner_id: int) -> None:
    """Log that ``owner_id`` just sent an SOS to this contact (one row per owner)."""
    conn = _conn_or_error()
    await conn.execute(
        "INSERT OR REPLACE INTO sos_log(contact_id, platform, owner_id, sent_at) "
        "VALUES (?, ?, ?, ?)",
        (contact_id, platform, owner_id, int(time.time())),
    )
    await conn.commit()


async def get_last_sos_owner(contact_id: int, platform: str = TELEGRAM) -> int | None:
    """Owner whose SOS reached this contact most recently."""
    async with _conn_or_error().execute(
        "SELECT owner_id FROM sos_log WHERE contact_id = ? AND platform = ? "
        "ORDER BY sent_at DESC LIMIT 1",
        (contact_id, platform),
    ) as cur:
        row = await cur.fetchone()
    return row["owner_id"] if row else None


async def get_recent_sos_owners(contact_id: int, platform: str = TELEGRAM,
                                within_seconds: int = 3600) -> list[int]:
    """Distinct owners who sent an SOS to this contact within the window,
    newest first."""
    since = int(time.time()) - within_seconds
    async with _conn_or_error().execute(
        "SELECT owner_id FROM sos_log WHERE contact_id = ? AND platform = ? AND sent_at >= ? "
        "ORDER BY sent_at DESC",
        (contact_id, platform, since),
    ) as cur:
        rows = await cur.fetchall()
    return [r["owner_id"] for r in rows]


async def route_reply(contact_id: int, owners: list["Owner"],
                      platform: str = TELEGRAM) -> tuple["Owner | None", list["Owner"]]:
    """Decide which owner should receive a subscriber's reply.

    Returns ``(target, ask_among)``. If ``target`` is None the caller should
    ask the subscriber to choose among ``ask_among`` (buttons). Rules:
      - one subscription → that owner;
      - exactly one owner alarmed within the last hour → that owner;
      - several owners alarmed within the last hour → ask (among them);
      - none in the last hour → the most recent SOS sender ever, else ask.
    """
    if len(owners) <= 1:
        return (owners[0] if owners else None), owners

    recent_ids = await get_recent_sos_owners(contact_id, platform)
    recent = [o for o in owners if o.chat_id in recent_ids]
    if len(recent) == 1:
        return recent[0], owners
    if len(recent) > 1:
        return None, recent

    last_id = await get_last_sos_owner(contact_id, platform)
    target = next((o for o in owners if o.chat_id == last_id), None)
    return target, owners


# ---------------------------------------------------------------------------
# Checkins (daily check-in / dead-man switch)
# ---------------------------------------------------------------------------

async def get_checkin(owner_id: int) -> dict | None:
    async with _conn_or_error().execute(
        "SELECT * FROM checkins WHERE owner_id = ?", (owner_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_pending_checkins() -> list[dict]:
    """All enabled check-ins joined with owner tz_offset and platform."""
    async with _conn_or_error().execute(
        """SELECT c.owner_id, c.enabled, c.time_minutes, c.state, c.attempts,
                  c.last_asked_at, o.tz_offset, o.platform
           FROM checkins c JOIN owners o ON o.chat_id = c.owner_id
           WHERE c.enabled = 1 AND o.status = 'active'"""
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def ensure_checkin(owner_id: int) -> None:
    conn = _conn_or_error()
    await conn.execute("INSERT OR IGNORE INTO checkins(owner_id) VALUES (?)", (owner_id,))
    await conn.commit()


async def update_checkin(owner_id: int, **fields) -> None:
    allowed = {"enabled", "time_minutes", "state", "attempts", "last_asked_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    conn = _conn_or_error()
    await conn.execute(
        f"UPDATE checkins SET {cols} WHERE owner_id = ?",
        (*updates.values(), owner_id),
    )
    await conn.commit()
