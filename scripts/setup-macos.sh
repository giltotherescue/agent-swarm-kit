#!/usr/bin/env bash
# setup-macos.sh: run a swarm worker on a Mac. One user (you), Homebrew, launchd, no sudo.
#
# Less isolated than the Ubuntu worker, on purpose: there are no separate agent and runner users and no memory or
# CPU caps. Tasks run as you and can read everything you can, including your GitHub and model logins. Use a spare
# Mac or a home machine, not your everyday laptop. docs/macos-worker.md has the details.
#
# Run as your normal user (NOT with sudo), from a copy of this repo, on a Mac that already has Homebrew:
#   bash scripts/setup-macos.sh                       a second worker for a backend that runs somewhere else
#       --backend-url URL --worker-token-file FILE        (required in that case; the token file is copied, never printed)
#   bash scripts/setup-macos.sh --with-backend        this Mac runs the task backend too (one machine does it all)
#   bash scripts/setup-macos.sh --uninstall           stop and remove the launchd agents (your data stays)
# Options: --worker-name NAME  --no-claude  --no-codex  --max-parallel N (default 1)
#
# What it makes (all under your home folder):
#   ~/.local/share/agent-swarm-kit/releases/VERSION-TIME, and current pointing at it
#   ~/.config/agent-swarm/swarm.env, agent.env (mode 600), worker.token (mode 600)
#   ~/Library/LaunchAgents/com.agent-swarm-kit.worker.plist   the runner (always on)
#   ~/Library/LaunchAgents/com.agent-swarm-kit.sync.plist     secrets sync, every 10 minutes
#   ~/Library/LaunchAgents/com.agent-swarm-kit.cleanup.plist  old task folders, daily
#   ~/Library/LaunchAgents/com.agent-swarm-kit.backend.plist  only with --with-backend
# Safe to run again: a second run is an upgrade. Exit codes: 0 done, 1 stopped on an error.
set -euo pipefail

KIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL_PREFIX=com.agent-swarm-kit
WITH_BACKEND=0; WITH_CLAUDE=1; WITH_CODEX=1; MAX_PARALLEL=1; UNINSTALL=0
BACKEND_URL=""; TOKEN_FILE=""; WORKER_NAME=""

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'setup-macos: %s\n' "$*" >&2; exit 1; }

paths() {  # sets every path from $HOME (and lets tests move them)
  [[ "$HOME" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "your home folder path has spaces or odd characters ($HOME); not supported"
  KIT_ROOT="${SWARM_MAC_KIT_ROOT:-$HOME/.local/share/agent-swarm-kit}"
  CONF="${SWARM_MAC_CONF:-$HOME/.config/agent-swarm}"
  DATA="${SWARM_MAC_DATA:-$HOME/.local/share/agent-swarm}"
  BIN="$HOME/.local/bin"
  LIBDIR="$HOME/.local/lib/agent-swarm"
  AGENTS="${SWARM_MAC_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
  LOGS="${SWARM_MAC_LOGS:-$HOME/Library/Logs}"
}

rand_token() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n'; }

# env_file: the settings the launchd wrapper loads. Plain KEY=VALUE, absolute paths, no secrets.
write_env() {
  local url="$BACKEND_URL"
  [[ -n "$url" ]] || url="http://127.0.0.1:8750"
  [[ -f "$CONF/swarm.env" ]] && { echo "    Keeping your existing $CONF/swarm.env (delete it to regenerate)"; return 0; }
  {
    echo "# agent-swarm-kit on macOS. Made by scripts/setup-macos.sh. No secrets in this file."
    echo "# Single-user mode: tasks run as you, there are no systemd scopes, so no per-task memory or CPU caps."
    echo "SWARM_URL=$url"
    [[ -n "$WORKER_NAME" ]] && echo "SWARM_WORKER_NAME=$WORKER_NAME"
    echo "SWARM_WORKER_TOKEN_FILE=$CONF/worker.token"
    echo "SWARM_AGENT_ENV_FILE=$CONF/agent.env"
    echo "SWARM_WORK_DIR=$DATA/work"
    echo "SWARM_HANDOFF_DIR=$DATA/handoff"
    echo "SWARM_RUNNER_DIR=$DATA/runner"
    echo "SWARM_DRAIN_FILE=$DATA/runner/drain"
    echo "SWARM_BACKUP_DIR=$DATA/backups/work"
    echo "SWARM_TASK_SCOPE=0"
    echo "SWARM_REQUIRE_SCOPE=0"
    echo "SWARM_MAX_PARALLEL=$MAX_PARALLEL"
    echo "SWARM_FALLBACK_PROVIDER=codex"
    echo "SWARM_FALLBACK_MODEL=default"
    echo "SWARM_TASK_TIMEOUT=14400"
    echo "SWARM_MAX_LOAD_PER_CPU=1.5"
    echo "SWARM_MIN_FREE_MEM_MB=1500"
    echo "SWARM_KEEP_DAYS=7"
    echo "SWARM_SLIM_AFTER_DAYS=1"
    if (( WITH_BACKEND )); then
      echo "# Backend (this Mac keeps the queue). Keep it on localhost; Tailscale Serve publishes it (docs/backend.md)."
      echo "SWARM_BIND=127.0.0.1:8750"
      echo "SWARM_DB=$DATA/swarm.db"
      echo "SWARM_PUBLIC_URL=https://YOUR-MAC.YOUR-TAILNET.ts.net"
      echo "SWARM_ADMIN_TOKEN_FILE=$CONF/admin.token"
      echo "SWARM_BACKEND_WORKER_TOKEN_FILE=$CONF/worker.token"
      echo "SWARM_BOT_KEYS_DIR=$CONF/bot-keys"
      echo "SWARM_DB_BACKUP_DIR=$DATA/backups"
      echo "SWARM_LOG_EXPORT_DIR=$DATA/log-exports"
    fi
  } > "$CONF/swarm.env"
  chmod 600 "$CONF/swarm.env"
}

plist() {  # plist LABEL_SUFFIX ROLE EXTRA_XML : write one LaunchAgent file
  local suffix="$1" role="$2" extra="${3:-}"
  cat > "$AGENTS/$LABEL_PREFIX.$suffix.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_PREFIX.$suffix</string>
  <key>ProgramArguments</key>
  <array><string>$LIBDIR/swarm-launchd</string><string>$role</string></array>
$extra
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$LOGS/agent-swarm-$suffix.log</string>
  <key>StandardErrorPath</key><string>$LOGS/agent-swarm-$suffix.log</string>
</dict>
</plist>
EOF
}

write_plists() {
  install -d "$AGENTS" "$LOGS"
  # The runner and the backend stay up; launchd restarts them if they stop. The wait stops a crash loop.
  plist worker worker '  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>'
  # Secrets: right at login, then every 10 minutes.
  plist sync sync '  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>600</integer>'
  # Cleanup of old task folders: every day at 03:30.
  plist cleanup cleanup '  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>'
  if (( WITH_BACKEND )); then
    plist backend backend '  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>'
  fi
}

launch() {  # launch ACTION LABEL_SUFFIX : bootstrap or bootout one agent (launchctl is stubbed in tests)
  local uid; uid=$(id -u)
  case "$1" in
    start) launchctl bootout "gui/$uid/$LABEL_PREFIX.$2" 2>/dev/null || true
           launchctl bootstrap "gui/$uid" "$AGENTS/$LABEL_PREFIX.$2.plist";;
    stop)  launchctl bootout "gui/$uid/$LABEL_PREFIX.$2" 2>/dev/null || true;;
  esac
}

install_kit() {
  local ver rel
  ver="$(tr -d '[:space:]' < "$KIT_SRC/VERSION")"
  rel="$KIT_ROOT/releases/$ver-$(date -u +%Y%m%d%H%M%S)"
  install -d "$rel"
  (cd "$KIT_SRC" && tar --exclude=.git --exclude=.task-context --exclude=artifacts -cf - .) | (cd "$rel" && tar -xf -)
  chmod 755 "$rel"/server/swarmctl "$rel"/server/run-agent "$rel"/server/agent-exec "$rel"/server/with-secrets \
    "$rel"/server/*.py "$rel"/server/macos/swarm-launchd "$rel"/scripts/*.sh
  ln -sfn "$rel" "$KIT_ROOT/current"
  # Keep this release and the one before it.
  local keep; keep=$(ls -1dt "$KIT_ROOT"/releases/* | head -n 2)
  for d in "$KIT_ROOT"/releases/*; do grep -qxF "$d" <<<"$keep" || rm -rf "$d"; done
  install -d "$BIN" "$LIBDIR"
  install -m 755 "$rel/server/macos/swarm-launchd" "$LIBDIR/swarm-launchd"
  ln -sfn "$KIT_ROOT/current/server/swarmctl" "$BIN/swarmctl"
  ln -sfn "$KIT_ROOT/current/server/with-secrets" "$BIN/with-secrets"
  ln -sfn "$KIT_ROOT/current/server/with-secrets" "$BIN/swarm-sync-secrets"
  VERSION_NEW="$ver"
}

make_config() {
  install -d -m 700 "$CONF" "$CONF/bot-keys"
  install -d -m 700 "$DATA" "$DATA/work" "$DATA/handoff" "$DATA/handoff/inbox" "$DATA/handoff/outbox" "$DATA/runner" "$DATA/runner/logs"
  [[ -f "$CONF/agent.env" ]] || install -m 600 /dev/null "$CONF/agent.env"
  chmod 600 "$CONF/agent.env"
  if (( WITH_BACKEND )); then
    [[ -s "$CONF/admin.token" ]] || (umask 077; rand_token > "$CONF/admin.token")
    [[ -s "$CONF/worker.token" ]] || (umask 077; rand_token > "$CONF/worker.token")
    chmod 600 "$CONF/admin.token" "$CONF/worker.token"
    install -d -m 700 "$HOME/.config/swarm"
    printf 'SWARM_URL=http://127.0.0.1:8750\nSWARM_TOKEN_FILE=%s/admin.token\nSWARM_DRAIN_FILE=%s/runner/drain\nSWARM_STATE_FILE=%s/runner/runner-state.json\n' \
      "$CONF" "$DATA" "$DATA" > "$HOME/.config/swarm/client.env"
    chmod 600 "$HOME/.config/swarm/client.env"
  else
    install -m 600 "$TOKEN_FILE" "$CONF/worker.token"
  fi
  write_env
}

make_age_key() {
  local d="$HOME/.config/swarm-secrets"
  install -d -m 700 "$d"
  if [[ ! -f "$d/age.key" ]]; then (umask 077; age-keygen -o "$d/age.key" >/dev/null 2>&1); fi
  chmod 600 "$d/age.key"
  AGE_PUBLIC="$(age-keygen -y "$d/age.key")"
}

install_cli_tools() {
  say "Homebrew packages (no sudo)"
  command -v brew >/dev/null || die "Homebrew is not installed. Install it first from https://brew.sh (its own installer asks for your Mac password once), then run this again."
  local p
  for p in coreutils git gh node jq sops age; do
    brew list --formula "$p" >/dev/null 2>&1 || brew install "$p"
  done
  if (( WITH_CLAUDE )) && ! command -v claude >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/claude" ]]; then
    say "Claude Code"
    local f; f=$(mktemp); curl -fsSL https://claude.ai/install.sh -o "$f" && bash "$f" || echo "    Claude Code did not install; run this script again to retry."
    rm -f "$f"
  fi
  if (( WITH_CODEX )) && ! command -v codex >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/codex" ]]; then
    say "Codex CLI"
    npm install -g --prefix "$HOME/.local" @openai/codex || echo "    Codex did not install; run this script again to retry."
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --with-backend) WITH_BACKEND=1; shift;;
      --backend-url) BACKEND_URL="${2:?missing URL}"; shift 2;;
      --worker-token-file) TOKEN_FILE="${2:?missing file}"; shift 2;;
      --worker-name) WORKER_NAME="${2:?missing name}"; shift 2;;
      --max-parallel) MAX_PARALLEL="${2:?missing number}"; shift 2;;
      --no-claude) WITH_CLAUDE=0; shift;;
      --no-codex) WITH_CODEX=0; shift;;
      --uninstall) UNINSTALL=1; shift;;
      -h|--help) sed -n '2,26p' "$0"; exit 0;;
      *) die "unknown option: $1";;
    esac
  done
  [[ "$MAX_PARALLEL" =~ ^[1-9][0-9]*$ ]] || die "--max-parallel needs a whole number"
  [[ -z "$WORKER_NAME" || "$WORKER_NAME" =~ ^[A-Za-z0-9._-]{1,60}$ ]] || die "--worker-name: letters, numbers, dot, dash, underscore only"
  if (( ! UNINSTALL )) && (( ! WITH_BACKEND )); then
    [[ -n "$BACKEND_URL" && -n "$TOKEN_FILE" ]] || die "give --backend-url and --worker-token-file (a second worker for a backend elsewhere), or --with-backend"
    [[ "$BACKEND_URL" =~ ^https://[A-Za-z0-9._:/-]+$ || "$BACKEND_URL" =~ ^http://127\.0\.0\.1(:[0-9]+)?$ ]] || die "--backend-url must be https (your private Tailscale address)"
    [[ -r "$TOKEN_FILE" ]] || die "cannot read $TOKEN_FILE"
  fi
}

main() {
  parse_args "$@"
  [[ "${SWARM_MAC_FORCE:-0}" == 1 || "$(uname -s)" == Darwin ]] || die "this script is for macOS. On Ubuntu use scripts/setup-worker.sh"
  [[ $EUID -ne 0 ]] || die "do not run this with sudo. It needs no admin rights and installs only into your home folder"
  paths
  if (( UNINSTALL )); then
    say "Stopping the launchd agents"
    for s in worker sync cleanup backend; do launch stop "$s"; rm -f "$AGENTS/$LABEL_PREFIX.$s.plist"; done
    echo "Removed. Your settings ($CONF), tasks and secrets are still there; delete them by hand if you want them gone."
    return 0
  fi
  if [[ "${SWARM_MAC_FORCE:-0}" != 1 ]]; then
    [[ -x /usr/bin/python3 ]] || die "/usr/bin/python3 is missing. Run: xcode-select --install"
    install_cli_tools
  fi
  say "Kit into $KIT_ROOT"
  paths; install_kit
  say "Settings and folders in $CONF"
  make_config
  say "This Mac's secrets key (the private half never leaves $HOME/.config/swarm-secrets)"
  make_age_key
  say "launchd agents"
  write_plists
  launch start sync; launch start worker; launch start cleanup
  (( WITH_BACKEND )) && launch start backend
  cat <<EOF

Kit $VERSION_NEW is installed and the agents are loaded.
  Runner log:    $LOGS/agent-swarm-worker.log
  Public age key (safe to share, add it as a secrets recipient, docs/secrets.md): $AGE_PUBLIC

Next steps (docs/macos-worker.md):
  1. Sign in, once, in Terminal on this Mac (it has a browser):
       claude            then /login          (or: claude setup-token, then put the token in $CONF/agent.env)
       codex login --device-auth             (turn on device-code login in ChatGPT Security Settings first)
       gh auth login && gh auth setup-git
  2. Keep the Mac awake and set to log in automatically: a LaunchAgent runs only while you are logged in.
  3. $( ((WITH_BACKEND)) && echo "Check the backend: curl -fsS http://127.0.0.1:8750/health. Then follow docs/backend.md to publish it over Tailscale." || echo "Check the runner shows up: swarmctl status (from a machine that reaches the backend)." )
EOF
}

# Tests load the functions without running anything: SWARM_MAC_LIB=1 source scripts/setup-macos.sh
if [[ "${SWARM_MAC_LIB:-0}" != 1 ]]; then main "$@"; fi
