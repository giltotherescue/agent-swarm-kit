#!/usr/bin/env bash
# setup-worker.sh: turn a fresh Ubuntu LTS server (22.04 or 24.04) into a swarm worker, or upgrade one.
#
# Tested on: not yet run end to end on a fresh server. Read docs/worker-setup.md, run it on a server you
# can rebuild, and run scripts/smoke-test.sh afterwards.
#
# Run as root from a copy of this repo:
#   sudo bash scripts/setup-worker.sh                 # install or upgrade; keeps public SSH open for now
#   sudo bash scripts/setup-worker.sh --lockdown      # later, after SSH over Tailscale works: close public SSH
#
# Options:
#   --auth-key FILE        join Tailscale without a browser; the key is read from FILE and never printed
#                          (--tailscale-authkey-file FILE is the old name and still works)
#   --tags TAGS            Tailscale tags to advertise, for example tag:swarm-worker (needs tagOwners in your ACL)
#   --no-tailscale         skip Tailscale (not recommended)
#   --no-codex | --no-claude   skip installing one of the model CLIs
#   --swap-gb N            swap file size if the box has none (default 4, 0 to skip)
#   --max-wait SECONDS     on upgrade, how long to wait for running tasks to finish (default 3600)
#   --force-os             run on an Ubuntu version other than 22.04 or 24.04
#   --lockdown             only run the lockdown step: SSH reachable over Tailscale only
#
# Safe to run again: a second run is an upgrade. It never prints a token.
# Exit codes: 0 done, 1 stopped with an error, 3 finished but something needs your attention (listed at the end).
set -euo pipefail

KIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIT_ROOT=/opt/agent-swarm-kit
CONF_DIR=/etc/agent-swarm
DATA_DIR=/var/lib/agent-swarm
RUNNER_HOME=/var/lib/swarm-runner
HANDOFF=/srv/agent-swarm
LIBEXEC=/usr/local/lib/agent-swarm
AGENT_USER=agent
RUNNER_USER=swarm-runner
TS_KEY_FILE=""
TS_TAGS=""
WITH_TS=1
WITH_CODEX=1
WITH_CLAUDE=1
SWAP_GB=4
MAX_WAIT=3600
FORCE_OS=0
LOCKDOWN_ONLY=0
INCOMPLETE=()

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'setup-worker: %s\n' "$*" >&2; exit 1; }
todo() { INCOMPLETE+=("$*"); printf '    NOT DONE: %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auth-key|--tailscale-authkey-file) TS_KEY_FILE="${2:?missing file}"; shift 2;;
    --tags) TS_TAGS="${2:?missing tags}"; shift 2;;
    --no-tailscale) WITH_TS=0; shift;;
    --no-codex) WITH_CODEX=0; shift;;
    --no-claude) WITH_CLAUDE=0; shift;;
    --swap-gb) SWAP_GB="${2:?}"; shift 2;;
    --max-wait) MAX_WAIT="${2:?}"; shift 2;;
    --force-os) FORCE_OS=1; shift;;
    --lockdown) LOCKDOWN_ONLY=1; shift;;
    -h|--help) sed -n '2,26p' "$0"; exit 0;;
    *) die "unknown option: $1";;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo bash scripts/setup-worker.sh)"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || die "this script supports Ubuntu LTS only (found ${ID:-unknown})"
if [[ "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" && $FORCE_OS -eq 0 ]]; then
  die "tested for Ubuntu 22.04 and 24.04 only (found ${VERSION_ID:-unknown}). Use --force-os to try anyway."
fi
[[ -z "$TS_KEY_FILE" || -r "$TS_KEY_FILE" ]] || die "cannot read the Tailscale auth key file $TS_KEY_FILE"
[[ "$SWAP_GB" =~ ^[0-9]+$ ]] || die "--swap-gb needs a whole number"
[[ "$MAX_WAIT" =~ ^[0-9]+$ ]] || die "--max-wait needs a number of seconds"

ssh_hardening() {
  # sshd uses the FIRST value it reads for most settings, and cloud images ship files like 50-cloud-init.conf.
  # A 00- prefix makes ours win. Then we check the effective settings, not just the file.
  install -d -m 755 /etc/ssh/sshd_config.d
  grep -qE '^\s*Include\s+/etc/ssh/sshd_config.d/\*\.conf' /etc/ssh/sshd_config \
    || die "/etc/ssh/sshd_config does not include sshd_config.d/*.conf; add the hardening lines there by hand"
  cat > /etc/ssh/sshd_config.d/00-swarm-hardening.conf <<'EOF'
# agent-swarm-kit: key-only SSH, no root login. Loaded first so it wins over later files.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
  rm -f /etc/ssh/sshd_config.d/60-swarm-hardening.conf   # 0.1.0 name, loaded too late to win
  sshd -t || die "sshd configuration test failed; nothing reloaded. Fix the error above."
  local eff
  eff="$(sshd -T 2>/dev/null)"
  grep -qx 'passwordauthentication no' <<<"$eff" || die "sshd still allows passwords after the change (check Match blocks)"
  grep -qx 'permitrootlogin no' <<<"$eff" || die "sshd still allows root login after the change (check Match blocks)"
  if grep -qiE '^\s*Match\s' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
    echo "    Note: your sshd config has Match blocks. Check they do not turn passwords or root login back on."
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd || die "could not reload sshd"
}

lockdown() {
  say "Lockdown: SSH only over Tailscale"
  command -v tailscale >/dev/null || die "Tailscale is not installed"
  tailscale status >/dev/null 2>&1 || die "Tailscale is not connected. Run: tailscale up --ssh"
  cat <<'MSG'
Before continuing, prove you can reach this box over Tailscale from another device, for example:
  ssh <admin-user>@<this-machine's-tailscale-name>
If that does not work, answer "no" and fix it first, or you may lock yourself out.
Your provider's web console (serial console) is the way back in if something goes wrong.
Note: Tailscale SSH is its own SSH server inside Tailscale. It follows your tailnet policy (the "ssh" rules
in the ACL), not the OpenSSH settings below. Use the sample policy in docs/worker-setup.md so that only
your admin group can log in, and never as root.
MSG
  read -r -p "Can you log in over Tailscale right now? (yes/no) " ok
  [[ "$ok" == yes ]] || die "stopped; nothing changed"
  apt-get install -y -qq ufw >/dev/null
  # Keep your other rules. Only remove rules that open SSH to the whole internet.
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  for r in "OpenSSH" "22/tcp" "22" "ssh"; do ufw delete allow "$r" >/dev/null 2>&1 || true; done
  ufw delete allow in on tailscale0 >/dev/null 2>&1 || true   # 0.1.0 opened every port to the tailnet
  ufw allow in on tailscale0 to any port 22 proto tcp >/dev/null
  ufw allow 41641/udp >/dev/null   # Tailscale direct connections
  ufw --force enable >/dev/null
  ssh_hardening
  if ufw status | grep -E '(^| )22(/tcp)? ' | grep -v tailscale0 | grep -q ALLOW; then
    die "a public SSH rule is still in ufw; see: sudo ufw status numbered"
  fi
  say "Done. Public SSH is closed in ufw; SSH works over Tailscale only."
  echo "Also close port 22 in your provider's cloud firewall if it has one."
  ufw status verbose
}

if [[ $LOCKDOWN_ONLY -eq 1 ]]; then lockdown; exit 0; fi

VERSION_NEW="$(tr -d '[:space:]' < "$KIT_SRC/VERSION")"
UPGRADE=0
[[ -e "$KIT_ROOT/current" || -d "$KIT_ROOT/server" ]] && UPGRADE=1

say "Checking the '$AGENT_USER' account before anything else"
if id "$AGENT_USER" >/dev/null 2>&1; then
  uid=$(id -u "$AGENT_USER")
  home=$(getent passwd "$AGENT_USER" | cut -d: -f6)
  (( uid >= 1000 )) || die "user '$AGENT_USER' exists with uid $uid (a system account). Remove it or pick another server."
  [[ "$home" == "/home/$AGENT_USER" ]] || die "user '$AGENT_USER' has home $home; this kit needs /home/$AGENT_USER"
  for g in sudo admin wheel adm docker lxd libvirt disk root; do
    if id -nG "$AGENT_USER" | tr ' ' '\n' | grep -qx "$g"; then
      die "user '$AGENT_USER' is in the '$g' group, which gives admin power. Remove it: gpasswd -d $AGENT_USER $g"
    fi
  done
  if sudo -l -U "$AGENT_USER" 2>/dev/null | grep -q "may run the following"; then
    die "user '$AGENT_USER' has sudo rights (sudo -l -U $AGENT_USER). Remove them before installing."
  fi
  if [[ -s "/home/$AGENT_USER/.ssh/authorized_keys" ]]; then
    echo "    Note: /home/$AGENT_USER/.ssh/authorized_keys is not empty. Nobody needs to log in as $AGENT_USER; consider emptying it."
  fi
fi

say "System packages and automatic security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq ca-certificates curl git jq tmux python3 sqlite3 unattended-upgrades ufw sudo acl \
  build-essential unzip >/dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
systemctl is-enabled unattended-upgrades >/dev/null 2>&1 || todo "automatic security updates are not enabled (systemctl enable --now unattended-upgrades)"

say "Logs: cap the system journal at 1 GB"
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=1G\n' > /etc/systemd/journald.conf.d/swarm.conf
systemctl restart systemd-journald

if [[ "$SWAP_GB" != 0 ]] && ! swapon --show | grep -q .; then
  say "Swap: ${SWAP_GB} GB swap file (gives memory spikes somewhere to go)"
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

say "GitHub CLI"
if ! command -v gh >/dev/null; then
  install -d -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update -qq && apt-get install -y -qq gh >/dev/null
fi
gh --version >/dev/null || die "GitHub CLI did not install"

say "Node.js LTS (needed by Codex CLI and most web repos)"
node_ok() { command -v node >/dev/null && command -v npm >/dev/null && (( $(node -p 'process.versions.node.split(".")[0]') >= 18 )); }
if ! node_ok; then
  tmp_ns=$(mktemp)
  curl -fsSL https://deb.nodesource.com/setup_lts.x -o "$tmp_ns"
  bash "$tmp_ns" >/dev/null
  rm -f "$tmp_ns"
  apt-get install -y -qq nodejs >/dev/null
fi
node_ok || die "Node.js 18 or newer with npm is required (found: $(node --version 2>/dev/null || echo none))"

if [[ $WITH_TS -eq 1 ]]; then
  say "Tailscale"
  if ! command -v tailscale >/dev/null; then
    tmp_ts=$(mktemp)
    curl -fsSL https://tailscale.com/install.sh -o "$tmp_ts"
    sh "$tmp_ts"
    rm -f "$tmp_ts"
  fi
  ts_args=(--ssh)
  [[ -n "$TS_TAGS" ]] && ts_args+=(--advertise-tags "$TS_TAGS")
  if ! tailscale status >/dev/null 2>&1; then
    if [[ -n "$TS_KEY_FILE" ]]; then
      tailscale up "${ts_args[@]}" --auth-key "file:$TS_KEY_FILE"
    else
      echo "Open the link Tailscale prints below and approve this machine."
      tailscale up "${ts_args[@]}"
    fi
  else
    tailscale set --ssh || todo "could not turn on Tailscale SSH (tailscale set --ssh)"
  fi
  tailscale status >/dev/null 2>&1 || todo "Tailscale is not connected (tailscale up --ssh)"
fi

say "Users: '$AGENT_USER' runs agents (no sudo, no tokens), '$RUNNER_USER' runs the runner, 'swarm' runs the backend"
id "$AGENT_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$AGENT_USER"
id swarm >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin swarm
id "$RUNNER_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$RUNNER_HOME" --shell /usr/sbin/nologin "$RUNNER_USER"
chmod 750 "/home/$AGENT_USER"
install -d -o swarm -g swarm -m 750 "$DATA_DIR" "$DATA_DIR/backups"
install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 700 "$RUNNER_HOME" "$RUNNER_HOME/repos" "$RUNNER_HOME/logs"
install -d -o root -g root -m 755 "$HANDOFF"
# inbox: the runner writes, the agent reads. outbox: the agent writes, the runner reads. Nobody else.
install -d -o "$RUNNER_USER" -g "$AGENT_USER" -m 2750 "$HANDOFF/inbox"
install -d -o "$AGENT_USER" -g "$RUNNER_USER" -m 2750 "$HANDOFF/outbox"
loginctl enable-linger "$AGENT_USER"   # gives agent a systemd user manager, so each task gets its own scope with limits
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "/home/$AGENT_USER/work" "/home/$AGENT_USER/backups" \
  "/home/$AGENT_USER/.config" "/home/$AGENT_USER/.config/agent-secrets"

say "Limits for all tasks together (memory, processes)"
agent_uid=$(id -u "$AGENT_USER")
install -d "/etc/systemd/system/user-${agent_uid}.slice.d"
install -m 644 "$KIT_SRC/server/systemd/user-slice-limits.conf.template" "/etc/systemd/system/user-${agent_uid}.slice.d/50-swarm-limits.conf"

say "Kit version $VERSION_NEW into $KIT_ROOT/releases"
install -d -m 755 "$KIT_ROOT" "$KIT_ROOT/releases"
REL="$KIT_ROOT/releases/$VERSION_NEW-$(date -u +%Y%m%d%H%M%S)"
install -d -m 755 "$REL"
(cd "$KIT_SRC" && tar --exclude=.git --exclude=.task-context --exclude=artifacts -cf - .) | (cd "$REL" && tar -xf -)
chown -R root:root "$REL"
chmod -R go-w,a+rX "$REL"
chmod 755 "$REL"/server/swarmctl "$REL"/server/run-agent "$REL"/server/agent-exec "$REL"/server/with-secrets \
  "$REL"/server/*.py "$REL"/scripts/*.sh

say "Settings and tokens in $CONF_DIR"
install -d -m 755 "$CONF_DIR"
[[ -f "$CONF_DIR/swarm.env" ]] || install -m 644 "$REL/config/swarm.env.example" "$CONF_DIR/swarm.env"
[[ -f "$CONF_DIR/agent.env" ]] || install -m 644 /dev/null "$CONF_DIR/agent.env"
install -d -o swarm -g swarm -m 700 "$CONF_DIR/bot-keys"
make_token() {  # make_token FILE OWNER:GROUP MODE [rotate] ; keeps an existing token unless asked to rotate
  if [[ ! -s "$1" || "${4:-}" == rotate ]]; then
    (umask 077; head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$1.new"); mv "$1.new" "$1"
  fi
  chown "$2" "$1"; chmod "$3" "$1"
}
make_token "$CONF_DIR/admin.token" root:swarm 640
ROTATE=""
if [[ -e "/home/$AGENT_USER/.config/swarm/worker.token" ]]; then
  # 0.1.0 gave the agent user a copy of the worker token. Agents could read it, so replace it.
  echo "    The old worker token was readable by agents. Making a new one."
  rm -f "/home/$AGENT_USER/.config/swarm/worker.token"
  ROTATE=rotate
fi
make_token "$CONF_DIR/worker.token" root:swarm 640 "$ROTATE"
install -o "$RUNNER_USER" -g "$RUNNER_USER" -m 600 "$CONF_DIR/worker.token" "$RUNNER_HOME/worker.token"
install -d -m 700 /root/.config/swarm
printf 'SWARM_URL=http://127.0.0.1:8750\nSWARM_TOKEN_FILE=%s/admin.token\n' "$CONF_DIR" > /root/.config/swarm/client.env
chmod 600 /root/.config/swarm/client.env

say "Model CLIs for '$AGENT_USER' (you log in to them after this script)"
if [[ $WITH_CLAUDE -eq 1 ]]; then
  if ! sudo -iu "$AGENT_USER" bash -c 'command -v claude >/dev/null'; then
    sudo -iu "$AGENT_USER" bash -o pipefail -c 'f=$(mktemp) && curl -fsSL https://claude.ai/install.sh -o "$f" && bash "$f"; rc=$?; rm -f "$f"; exit $rc' \
      || todo "Claude Code install failed (see docs/worker-setup.md, step 4)"
  fi
  sudo -iu "$AGENT_USER" bash -c 'claude --version' >/dev/null 2>&1 || todo "claude does not run as $AGENT_USER"
fi
if [[ $WITH_CODEX -eq 1 ]]; then
  sudo -iu "$AGENT_USER" bash -c 'mkdir -p ~/.local && npm config set prefix ~/.local && (command -v codex >/dev/null || npm install -g @openai/codex)' \
    >/dev/null || todo "Codex CLI install failed (see docs/worker-setup.md, step 4)"
  sudo -iu "$AGENT_USER" bash -c 'codex --version' >/dev/null 2>&1 || todo "codex does not run as $AGENT_USER"
fi
# shellcheck disable=SC2016  # $HOME and $PATH must stay literal in .profile
grep -q '.local/bin' "/home/$AGENT_USER/.profile" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "/home/$AGENT_USER/.profile"
sudo -iu "$AGENT_USER" git config --global user.name >/dev/null 2>&1 || {
  sudo -iu "$AGENT_USER" git config --global user.name "Swarm Worker"
  sudo -iu "$AGENT_USER" git config --global user.email "swarm-worker@users.noreply.github.com"
}
if sudo -iu "$AGENT_USER" bash -c 'gh auth status' >/dev/null 2>&1; then
  todo "the $AGENT_USER user is logged in to GitHub. Agents must not hold a GitHub login: run 'sudo -iu $AGENT_USER gh auth logout' after logging in $RUNNER_USER"
fi

# Wait for running tasks before switching versions: a restart would kill them.
if [[ $UPGRADE -eq 1 ]] && systemctl is-active --quiet swarm-worker; then
  say "Upgrade: draining the worker and waiting up to $MAX_WAIT seconds for running tasks"
  touch "$RUNNER_HOME/drain"
  waited=0
  hdr=$(mktemp); chmod 600 "$hdr"
  printf 'Authorization: Bearer %s\n' "$(cat "$CONF_DIR/admin.token")" > "$hdr"
  while :; do
    # Ask the backend (works for a 0.1.0 worker too, which has no drain switch), and the runner's own state file.
    running=$(curl -fsS -H @"$hdr" http://127.0.0.1:8750/status 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("running") or []))' 2>/dev/null || echo 0)
    local_running=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("running") or []))' "$RUNNER_HOME/runner-state.json" 2>/dev/null || echo 0)
    [[ "$running" == 0 && "$local_running" == 0 ]] && break
    (( waited >= MAX_WAIT )) && { rm -f "$RUNNER_HOME/drain"; die "tasks are still running after $MAX_WAIT seconds; nothing switched. Run again later."; }
    sleep 15; waited=$((waited + 15))
  done
  rm -f "$hdr"
  # A 0.1.0 worker does not know about draining; stop it now so it cannot pick up new work mid-switch.
  [[ -d "$KIT_ROOT/server" ]] && systemctl stop swarm-worker
fi

say "Helpers: agent-exec (the runner's only way to run things as $AGENT_USER), with-secrets, swarmctl"
install -d -m 755 "$LIBEXEC"
install -o root -g root -m 755 "$REL/server/agent-exec" "$LIBEXEC/agent-exec"
install -o root -g root -m 755 "$REL/server/with-secrets" /usr/local/bin/with-secrets
sudoers=$(mktemp)
cat > "$sudoers" <<EOF
# agent-swarm-kit: the runner may start agent-exec as the agent user, and nothing else.
Defaults:$RUNNER_USER !requiretty
$RUNNER_USER ALL=($AGENT_USER) NOPASSWD: $LIBEXEC/agent-exec
EOF
visudo -cf "$sudoers" >/dev/null || die "the sudoers rule failed its check; nothing installed"
install -o root -g root -m 440 "$sudoers" /etc/sudoers.d/swarm-runner
rm -f "$sudoers"

say "Switching to the new version"
if [[ -d "$KIT_ROOT/server" && ! -L "$KIT_ROOT/current" ]]; then
  # 0.1.0 layout: files straight in /opt/agent-swarm-kit. Keep them as a release for rollback.
  legacy="$KIT_ROOT/releases/0.1.0-legacy"
  install -d -m 755 "$legacy"
  for f in "$KIT_ROOT"/* "$KIT_ROOT"/.[!.]*; do
    [[ -e "$f" ]] || continue
    case "$(basename "$f")" in releases|current|previous) continue;; esac
    mv "$f" "$legacy/"
  done
  ln -sfn "$legacy" "$KIT_ROOT/current"
fi
[[ -L "$KIT_ROOT/current" ]] && ln -sfn "$(readlink -f "$KIT_ROOT/current")" "$KIT_ROOT/previous"
ln -sfn "$REL" "$KIT_ROOT/current.new" && mv -T "$KIT_ROOT/current.new" "$KIT_ROOT/current"
ln -sfn "$KIT_ROOT/current/server/swarmctl" /usr/local/bin/swarmctl

say "Services (restart on crash and on reboot)"
install -m 644 "$REL"/server/systemd/*.service "$REL"/server/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable swarm-backend.service swarm-worker.service >/dev/null
systemctl restart swarm-backend.service
systemctl enable --now swarm-cleanup.timer swarm-backup.timer swarm-disk-alert.timer >/dev/null

for _ in $(seq 1 10); do curl -fsS http://127.0.0.1:8750/health >/dev/null 2>&1 && break; sleep 1; done
if ! curl -fsS http://127.0.0.1:8750/health | grep -q "\"$VERSION_NEW\""; then
  if [[ -L "$KIT_ROOT/previous" ]]; then
    echo "The new backend did not start. Rolling back to $(readlink -f "$KIT_ROOT/previous")."
    ln -sfn "$(readlink -f "$KIT_ROOT/previous")" "$KIT_ROOT/current"
    systemctl restart swarm-backend.service
  fi
  rm -f "$RUNNER_HOME/drain"
  die "backend did not start: journalctl -u swarm-backend -n 50"
fi
if [[ $UPGRADE -eq 1 ]] && systemctl is-enabled --quiet swarm-worker; then
  systemctl restart swarm-worker
fi
rm -f "$RUNNER_HOME/drain"

cat <<EOF

Setup finished (kit $VERSION_NEW). The backend is running.$( [[ $UPGRADE -eq 1 ]] && echo " This was an upgrade; see CHANGELOG.md." )

Next steps (details in docs/worker-setup.md):
  1. Log in the model CLIs as the agent user, with your own subscriptions:
       sudo -iu $AGENT_USER claude        (then type /login)
       sudo -iu $AGENT_USER codex login --device-auth
  2. Log in GitHub for the runner (it pushes and opens PRs; agents never get this login).
     Use a fine-grained token limited to the swarm's repos:
       sudo -u $RUNNER_USER -H gh auth login
       sudo -u $RUNNER_USER -H gh auth setup-git
  3. Start the worker and check everything:
       sudo systemctl start swarm-worker
       sudo bash $KIT_ROOT/current/scripts/smoke-test.sh
  4. Let your bots reach the backend over Tailscale Serve (docs/backend.md, "Reaching the backend").
  5. Add your bots and bind them to projects (templates/bot-setup-checklist.md).
  6. When SSH over Tailscale works from your laptop, close public SSH:
       sudo bash $KIT_ROOT/current/scripts/setup-worker.sh --lockdown

Tokens are in $CONF_DIR (admin.token, worker.token) and $RUNNER_HOME/worker.token. Never paste them into chat.
EOF
if (( ${#INCOMPLETE[@]} )); then
  printf '\nSETUP NEEDS YOUR ATTENTION:\n'
  printf '  - %s\n' "${INCOMPLETE[@]}"
  exit 3
fi
