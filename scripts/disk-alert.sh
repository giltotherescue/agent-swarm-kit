#!/usr/bin/env bash
# disk-alert: warn the Chief of Staff once a day when the disk is fuller than SWARM_DISK_ALERT_PERCENT (default 80).
# Silent when everything is fine. Run from a systemd timer as swarm-runner (it uses the worker token,
# which may only send alerts, claim tasks and read task states).
# If the alert cannot be queued (backend down, disk completely full), it writes a critical line to the
# system log and exits with an error, so `systemctl --failed` and scripts/smoke-test.sh show it.
set -uo pipefail
LIMIT="${SWARM_DISK_ALERT_PERCENT:-80}"
used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
if (( used >= LIMIT )); then
  host=$(hostname -s)
  msg="Disk on $host is ${used}% full (limit ${LIMIT}%). Run: sudo systemctl start swarm-cleanup, then remove old folders or grow the disk."
  if ! "$(dirname "$(readlink -f "$0")")/../server/swarmctl" alert "disk-$host-$(date +%F)" "$msg" >/dev/null; then
    logger -p user.crit -t swarm-disk-alert "$msg (could not tell the Chief of Staff)"
    exit 1
  fi
fi
