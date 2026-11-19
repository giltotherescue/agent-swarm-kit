#!/usr/bin/env bash
# restore-backup: put a nightly backup back as the live database, safely.
# Usage (as root on the worker): sudo bash scripts/restore-backup.sh /var/lib/agent-swarm/backups/swarm-DATE.db.gz
# Steps: unpack into a temporary file, check it, stop the services, keep the current database as
# swarm.db.before-restore-TIME, move the checked copy into place, start the services.
# Nothing is touched until the backup has been unpacked and checked.
set -euo pipefail
SRC="${1:?usage: restore-backup.sh BACKUP.db.gz}"
DATA_DIR="${SWARM_DATA_DIR:-/var/lib/agent-swarm}"
DB="$DATA_DIR/swarm.db"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f "$SRC" ]] || { echo "no such file: $SRC" >&2; exit 1; }
tmp="$DATA_DIR/.restore-$$.db"
trap 'rm -f "$tmp"' EXIT
gunzip -c "$SRC" > "$tmp"
python3 - "$tmp" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("restore: the backup failed its integrity check; nothing changed")
c.execute("SELECT COUNT(*) FROM tasks").fetchone()
PY
chown swarm:swarm "$tmp"; chmod 600 "$tmp"
echo "Backup checked. Stopping the worker and backend."
systemctl stop swarm-worker swarm-backend
keep="$DB.before-restore-$(date -u +%Y%m%d%H%M%S)"
if [[ -f "$DB" ]]; then
  # A consistent copy of the current database, including anything still in the WAL file.
  python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" "$DB" "$keep"
  chown swarm:swarm "$keep"; chmod 600 "$keep"
fi
rm -f "$DB-wal" "$DB-shm"
mv "$tmp" "$DB"
trap - EXIT
systemctl start swarm-backend swarm-worker
echo "Restored. The previous database is kept at $keep."
echo "Running tasks from before the restore come back as queued after their lease runs out."
