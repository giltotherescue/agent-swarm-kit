#!/usr/bin/env bash
# disk-alert: warn the Chief of Staff once a day when the disk is fuller than SWARM_DISK_ALERT_PERCENT (default 80).
# Silent when everything is fine. Run from a systemd timer as the agent user.
set -euo pipefail
LIMIT="${SWARM_DISK_ALERT_PERCENT:-80}"
used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
if (( used >= LIMIT )); then
  host=$(hostname -s)
  "$(dirname "$0")/../server/swarmctl" alert "disk-$host-$(date +%F)" \
    "Disk on $host is ${used}% full (limit ${LIMIT}%). Run swarm-cleanup --dry-run, then remove old folders or grow the disk."
fi
