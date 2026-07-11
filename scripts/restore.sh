#!/usr/bin/env bash
#
# Restore the SQLite database from a backup produced by scripts/backup.sh.
#
# Stops the service, replaces data/sos.db with the backup (removing stale
# WAL/SHM sidecar files), and starts the service again.
#
# Usage:
#   ./scripts/restore.sh backups/sos-20260711-030000.db
#
set -euo pipefail

cd "$(dirname "$0")/.."          # repo root (compose project dir)

BACKUP="${1:-}"
if [ -z "$BACKUP" ] || [ ! -f "$BACKUP" ]; then
    echo "Usage: $0 <backup-file>"
    echo "Available backups:"
    ls -1t backups/sos-*.db 2>/dev/null || echo "  (none in ./backups)"
    exit 1
fi

echo "⚠️  This will REPLACE the current database with:"
echo "    $BACKUP"
read -r -p "Continue? [y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "Aborted."; exit 1; }

echo "→ Stopping service..."
docker compose down

echo "→ Restoring database..."
cp "$BACKUP" data/sos.db
# Drop stale WAL/SHM so they don't clobber the restored file on next start.
rm -f data/sos.db-wal data/sos.db-shm

echo "→ Starting service..."
docker compose up -d

echo "✅ Restored from $BACKUP"
echo "   Check logs: docker compose logs --tail=40 bot"
