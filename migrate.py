"""
Migration from single-tenant (contacts.json + env vars) to multi-tenant SQLite.

Runs automatically on startup when ADMIN_CHAT_ID env var is present
and the owner doesn't exist in the DB yet. Safe to call multiple times.

Can also be run manually:
    python migrate.py
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import db

logger = logging.getLogger(__name__)


async def run() -> None:
    """Migrate old single-tenant data. No-op if already done or not applicable."""
    admin_chat_id_str = os.environ.get("ADMIN_CHAT_ID", "").strip()
    if not admin_chat_id_str:
        return

    try:
        admin_chat_id = int(admin_chat_id_str)
    except ValueError:
        logger.warning("migrate: ADMIN_CHAT_ID=%r is not an integer, skipping", admin_chat_id_str)
        return

    if await db.get_owner(admin_chat_id):
        return  # already migrated

    owner_name = os.environ.get("OWNER_NAME", "Владелец").strip() or "Владелец"
    sos_message = (
        os.environ.get("SOS_MESSAGE", "").strip()
        or "🆘 ТРЕВОГА! Мне нужна помощь!"
    )
    try:
        tz_offset = int(os.environ.get("TZ_OFFSET", "0"))
    except ValueError:
        tz_offset = 0

    contacts_file = os.environ.get("CONTACTS_FILE", "contacts.json")
    base_url = os.environ.get("BASE_URL", "").rstrip("/")

    token = str(uuid.uuid4())
    conn = db._conn_or_error()
    await conn.execute(
        "INSERT INTO owners(chat_id, name, sos_message, tz_offset, webhook_token) "
        "VALUES (?, ?, ?, ?, ?)",
        (admin_chat_id, owner_name, sos_message, tz_offset, token),
    )
    await conn.commit()
    logger.info("migrate: created owner %d (%s)", admin_chat_id, owner_name)

    contacts_imported = 0
    if Path(contacts_file).exists():
        try:
            with open(contacts_file, encoding="utf-8") as f:
                data = json.load(f)
            for chat_id_str, name in data.get("contacts", {}).items():
                await db.add_contact(admin_chat_id, int(chat_id_str), name)
                contacts_imported += 1
                logger.info("migrate:   contact %s (%s)", name, chat_id_str)
        except Exception as exc:
            logger.error("migrate: failed to read %s: %s", contacts_file, exc)

    webhook_url = f"{base_url}/alice/{token}" if base_url else f"/alice/{token}"
    logger.info(
        "migrate: done — owner=%d contacts=%d",
        admin_chat_id, contacts_imported,
    )
    logger.info("migrate: NEW WEBHOOK URL → %s", webhook_url)
    logger.info("migrate: Update this URL in your Yandex Dialogs skill settings!")


if __name__ == "__main__":
    # Standalone usage: python migrate.py
    # Reads .env from the current directory automatically.
    env_file = Path(".env")
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    db_path = os.environ.get("DB_PATH", "data/sos.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def _main() -> None:
        await db.init(db_path)
        await run()
        await db.close()

    asyncio.run(_main())
