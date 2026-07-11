#!/usr/bin/env bash
#
# Consistent hot backup of the SQLite database — no downtime.
#
# Uses SQLite "VACUUM INTO", which writes a clean single-file snapshot even
# while the app is running (safe with WAL). The result is copied to
# ./backups/sos-<timestamp>.db and the 30 most recent backups are kept.
#
# Usage:
#   ./scripts/backup.sh
#
# Cron (daily at 03:00):
#   0 3 * * * cd /root/alisa_sos && ./scripts/backup.sh >> /var/log/alisa-backup.log 2>&1
#
set -euo pipefail

cd "$(dirname "$0")/.."          # repo root (compose project dir)
mkdir -p backups

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/sos-${STAMP}.db"

# Produce a consistent snapshot into the mounted ./data dir, then move it out.
docker compose exec -T bot python3 - <<'PY'
import os, sqlite3
tmp = "data/_backup.tmp.db"
if os.path.exists(tmp):
    os.remove(tmp)
con = sqlite3.connect("data/sos.db")
con.execute("VACUUM INTO 'data/_backup.tmp.db'")
con.close()
PY

mv data/_backup.tmp.db "$OUT"
echo "✅ Backup: $OUT ($(du -h "$OUT" | cut -f1))"

# Retention: keep the 30 newest backups.
ls -1t backups/sos-*.db 2>/dev/null | tail -n +31 | xargs -r rm -f
