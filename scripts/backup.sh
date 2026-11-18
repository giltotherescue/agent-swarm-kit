#!/usr/bin/env bash
# backup: nightly copy of the backend database. It holds tasks, briefs, answers, reports and files,
# so treat every backup as private. Bot trigger keys and tokens are NOT in it (they live in files).
# Keeps SWARM_BACKUP_KEEP_DAYS (default 14) days locally. To copy off the box, set SWARM_OFFSITE_CMD,
# for example: SWARM_OFFSITE_CMD='rclone copy {dir} remote:swarm-backups' (the {dir} is replaced).
# Use an encrypted remote (rclone crypt, restic) for off-box copies.
# Each run writes a new file (date and time in the name), checks it, and only then moves it into place.
set -euo pipefail
umask 077
DB="${SWARM_DB:-$HOME/.local/share/swarm/swarm.db}"
DEST="${SWARM_DB_BACKUP_DIR:-$HOME/backups/db}"
KEEP="${SWARM_BACKUP_KEEP_DAYS:-14}"
[[ -f "$DB" ]] || { echo "backup: database $DB not found; nothing backed up" >&2; exit 1; }
mkdir -p "$DEST"; chmod 700 "$DEST"
stamp=$(date -u +%Y-%m-%d-%H%M%S)
tmp="$DEST/.swarm-$stamp.db.part"
final="$DEST/swarm-$stamp.db.gz"
trap 'rm -f "$tmp" "$tmp.gz"' EXIT
# Python's sqlite3 backup API makes a consistent copy while the backend keeps running.
python3 - "$DB" "$tmp" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
ok = sqlite3.connect(sys.argv[2]).execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit("backup: the copy failed its integrity check")
PY
gzip -c "$tmp" > "$tmp.gz"
gzip -t "$tmp.gz"
mv "$tmp.gz" "$final"
chmod 600 "$final"
find "$DEST" -name 'swarm-*.db.gz' -mtime +"$KEEP" -delete
if [[ -n "${SWARM_OFFSITE_CMD:-}" ]]; then
  eval "${SWARM_OFFSITE_CMD//\{dir\}/$DEST}"
fi
echo "backup: wrote $final"
