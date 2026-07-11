"""Database backup & restore.

- Consistent snapshot of the live SQLite DB (via VACUUM INTO), packed into a
  ``.tar.gz`` of all ``*.db`` files.
- Optional encryption with ``BACKUP_PASSPHRASE`` → ``.tar.gz.enc``
  (AES-256-GCM, key derived from the passphrase via scrypt).
- Restore: unpack the archive, replace the live DB files, reopen the DB.

Transport-agnostic: producing/consuming bytes only. The Telegram commands
and the auto-backup loop live in ``bot.py``.
"""

import asyncio
import glob
import hashlib
import io
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

import db
from config import settings

# Encrypted-file header: magic + version.
_MAGIC = b"ASOSbk1\n"
_SALT_LEN = 16
_NONCE_LEN = 12


# ---------------------------------------------------------------------------
# Encryption (only used when BACKUP_PASSPHRASE is set)
# ---------------------------------------------------------------------------

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt,
                          n=2 ** 14, r=8, p=1, dklen=32)


def _encrypt(data: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, data, None)
    return _MAGIC + salt + nonce + ct


def _decrypt(blob: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:len(_MAGIC)] != _MAGIC:
        raise ValueError("Файл не является зашифрованным бэкапом этого бота")
    off = len(_MAGIC)
    salt = blob[off:off + _SALT_LEN]
    nonce = blob[off + _SALT_LEN:off + _SALT_LEN + _NONCE_LEN]
    ct = blob[off + _SALT_LEN + _NONCE_LEN:]
    key = _derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception as exc:  # wrong passphrase or corrupt file
        raise ValueError("Не удалось расшифровать — неверный BACKUP_PASSPHRASE или повреждён файл") from exc


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def _db_dir() -> str:
    return os.path.dirname(settings.db_path) or "."


def _snapshot_live_db(dest: str) -> None:
    """Consistent snapshot of the running DB into `dest` (must not exist)."""
    con = sqlite3.connect(settings.db_path)
    try:
        safe = dest.replace("'", "''")
        con.execute(f"VACUUM INTO '{safe}'")
    finally:
        con.close()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


async def create_archive(prefix: str = "alisa-sos-backup") -> tuple[bytes, str]:
    """Return (archive_bytes, filename) for a fresh backup of all .db files."""
    main_base = os.path.basename(settings.db_path)
    tmpdir = tempfile.mkdtemp(prefix="asosbk-")
    try:
        # Consistent snapshot of the live DB (off the event loop).
        await asyncio.to_thread(_snapshot_live_db, os.path.join(tmpdir, main_base))
        # Any other *.db files in the data dir, copied as-is.
        for p in glob.glob(os.path.join(_db_dir(), "*.db")):
            if os.path.basename(p) != main_base:
                shutil.copy2(p, os.path.join(tmpdir, os.path.basename(p)))

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for fname in sorted(os.listdir(tmpdir)):
                tar.add(os.path.join(tmpdir, fname), arcname=fname)
        data = buf.getvalue()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    filename = f"{prefix}-{_stamp()}.tar.gz"
    if settings.backup_passphrase:
        data = _encrypt(data, settings.backup_passphrase)
        filename += ".enc"
    return data, filename


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _extract_db_files(archive: bytes, filename: str) -> dict[str, bytes]:
    if filename.endswith(".enc"):
        if not settings.backup_passphrase:
            raise ValueError("Архив зашифрован (.enc), но BACKUP_PASSPHRASE не задан")
        archive = _decrypt(archive, settings.backup_passphrase)

    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for m in tar.getmembers():
            # Ignore anything unsafe or non-.db.
            if not m.isfile() or "/" in m.name or ".." in m.name:
                continue
            if m.name.endswith(".db"):
                f = tar.extractfile(m)
                if f is not None:
                    members[os.path.basename(m.name)] = f.read()
    if not members:
        raise ValueError("В архиве нет .db файлов")
    return members


async def _write_safety_backup() -> str:
    data, fname = await create_archive(prefix="pre-restore")
    os.makedirs("backups", exist_ok=True)
    path = os.path.join("backups", fname)
    with open(path, "wb") as f:
        f.write(data)
    return path


async def restore_from(archive: bytes, filename: str) -> dict:
    """Replace the live DB with the archive's contents. Returns a report."""
    members = _extract_db_files(archive, filename)   # validate/decrypt first

    # 1) safety backup of the CURRENT state (DB still live)
    safety = await _write_safety_backup()

    main_base = os.path.basename(settings.db_path)
    # A single-file backup with a different name → map it onto our DB path.
    if main_base not in members and len(members) == 1:
        members = {main_base: next(iter(members.values()))}

    # 2) stop all DB access, 3) swap files, 4) reopen — all under the gate
    db.set_restoring(True)
    try:
        await db.close()
        os.makedirs(_db_dir(), exist_ok=True)
        for base, blob in members.items():
            with open(os.path.join(_db_dir(), base), "wb") as f:
                f.write(blob)
        # Drop stale WAL/SHM of the main DB so they don't clobber the restore.
        for suffix in ("-wal", "-shm"):
            p = settings.db_path + suffix
            if os.path.exists(p):
                os.remove(p)
        await db.init(settings.db_path)
    finally:
        db.set_restoring(False)

    return {
        "counts": await db.counts(),
        "files": sorted(members),
        "safety": safety,
    }
