#!/usr/bin/env bash
# backup: nightly copy of the backend database and the swarm config (not the secrets).
# Keeps SWARM_BACKUP_KEEP_DAYS (default 14) days locally. To copy off the box, set SWARM_OFFSITE_CMD,
# for example: SWARM_OFFSITE_CMD='rclone copy {dir} remote:swarm-backups' (the {dir} is replaced).
set -euo pipefail
DB="${SWARM_DB:-$HOME/.local/share/swarm/swarm.db}"
DEST="${SWARM_DB_BACKUP_DIR:-$HOME/backups/db}"
KEEP="${SWARM_BACKUP_KEEP_DAYS:-14}"
mkdir -p "$DEST"; chmod 700 "$DEST"
stamp=$(date +%F)
if [[ -f "$DB" ]]; then
  # Python's sqlite3 backup API makes a consistent copy while the backend keeps running.
  python3 - "$DB" "$DEST/swarm-$stamp.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
  gzip -f "$DEST/swarm-$stamp.db"
fi
find "$DEST" -name 'swarm-*.db.gz' -mtime +"$KEEP" -delete
if [[ -n "${SWARM_OFFSITE_CMD:-}" ]]; then
  eval "${SWARM_OFFSITE_CMD//\{dir\}/$DEST}"
fi
