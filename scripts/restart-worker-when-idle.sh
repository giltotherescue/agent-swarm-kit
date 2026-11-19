#!/usr/bin/env bash
# restart-worker-when-idle: stop taking new tasks, wait until no task is running, restart the worker,
# then take tasks again. Restarting the worker while tasks run would kill them.
# Usage: sudo bash scripts/restart-worker-when-idle.sh [--max-wait SECONDS] [--also UNIT]
#   --also UNIT  also restart this unit while idle, for example a multi-subscription proxy that tasks use.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
exec "$(dirname "$(readlink -f "$0")")/../server/swarmctl" restart-when-idle "$@"
