#!/usr/bin/env bash
# setup-worker.sh: turn a fresh Ubuntu LTS server (22.04 or 24.04) into a swarm worker.
#
# Run as root from a copy of this repo:
#   sudo bash scripts/setup-worker.sh                 # install everything, keep public SSH open for now
#   sudo bash scripts/setup-worker.sh --lockdown      # later, after Tailscale SSH works: close public SSH
#
# Options:
#   --tailscale-authkey-file FILE  join Tailscale without a browser (the key is read from the file, never printed)
#   --no-tailscale                 skip Tailscale (not recommended)
#   --no-codex | --no-claude       skip installing one of the model CLIs
#   --swap-gb N                    swap file size if the box has none (default 4, 0 to skip)
#   --lockdown                     only run the lockdown step: firewall allows SSH on Tailscale only
#
# Safe to run more than once. It never prints a token.
set -euo pipefail

KIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIT_DIR=/opt/agent-swarm-kit
CONF_DIR=/etc/agent-swarm
DATA_DIR=/var/lib/agent-swarm
AGENT_USER=agent
TS_KEY_FILE=""
WITH_TS=1
WITH_CODEX=1
WITH_CLAUDE=1
SWAP_GB=4
LOCKDOWN_ONLY=0

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'setup-worker: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tailscale-authkey-file) TS_KEY_FILE="$2"; shift 2;;
    --no-tailscale) WITH_TS=0; shift;;
    --no-codex) WITH_CODEX=0; shift;;
    --no-claude) WITH_CLAUDE=0; shift;;
    --swap-gb) SWAP_GB="$2"; shift 2;;
    --lockdown) LOCKDOWN_ONLY=1; shift;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) die "unknown option: $1";;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo bash scripts/setup-worker.sh)"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || die "this script supports Ubuntu LTS only (found ${ID:-unknown})"

lockdown() {
  say "Lockdown: SSH only over Tailscale"
  command -v tailscale >/dev/null || die "Tailscale is not installed"
  tailscale status >/dev/null 2>&1 || die "Tailscale is not connected. Run: tailscale up --ssh"
  cat <<'MSG'
Before continuing, prove you can reach this box over Tailscale from another device, for example:
  ssh <admin-user>@<this-machine's-tailscale-name>
If that does not work, answer "no" and fix it first, or you may lock yourself out.
Your provider's web console (serial console) is the way back in if something goes wrong.
MSG
  read -r -p "Can you log in over Tailscale right now? (yes/no) " ok
  [[ "$ok" == yes ]] || die "stopped; nothing changed"
  apt-get install -y -qq ufw >/dev/null
  ufw --force reset >/dev/null
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow in on tailscale0 >/dev/null
  ufw allow 41641/udp >/dev/null   # Tailscale direct connections
  ufw --force enable >/dev/null
  install -d -m 755 /etc/ssh/sshd_config.d
  cat > /etc/ssh/sshd_config.d/60-swarm-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  say "Done. Public SSH is closed; the firewall only accepts traffic from your tailnet."
  echo "Also close port 22 in your provider's cloud firewall if it has one."
}

if [[ $LOCKDOWN_ONLY -eq 1 ]]; then lockdown; exit 0; fi

say "System packages and automatic security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq ca-certificates curl git jq tmux python3 sqlite3 unattended-upgrades ufw \
  build-essential unzip >/dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

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

say "Node.js LTS (needed by Codex CLI and most web repos)"
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi

if [[ $WITH_TS -eq 1 ]]; then
  say "Tailscale"
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
  if ! tailscale status >/dev/null 2>&1; then
    if [[ -n "$TS_KEY_FILE" ]]; then
      tailscale up --ssh --authkey "file:$TS_KEY_FILE"
    else
      echo "Open the link Tailscale prints below and approve this machine."
      tailscale up --ssh
    fi
  fi
fi

say "Users: '$AGENT_USER' runs the agents (no sudo); 'swarm' runs the backend"
id "$AGENT_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$AGENT_USER"
id swarm >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin swarm
install -d -o swarm -g swarm -m 750 "$DATA_DIR" "$DATA_DIR/backups"
loginctl enable-linger "$AGENT_USER"   # lets per-task systemd scopes run without a login session
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "/home/$AGENT_USER/.config/swarm" "/home/$AGENT_USER/work" \
  "/home/$AGENT_USER/backups"

say "Kit files in $KIT_DIR"
if [[ "$KIT_SRC" != "$KIT_DIR" ]]; then
  install -d -m 755 "$KIT_DIR"
  cp -a "$KIT_SRC/." "$KIT_DIR/"
fi
chown -R root:root "$KIT_DIR"
chmod -R go-w "$KIT_DIR"
chmod 755 "$KIT_DIR"/server/swarmctl "$KIT_DIR"/server/run-agent "$KIT_DIR"/server/*.py "$KIT_DIR"/scripts/*.sh
ln -sf "$KIT_DIR/server/swarmctl" /usr/local/bin/swarmctl

say "Settings and tokens in $CONF_DIR"
install -d -m 755 "$CONF_DIR"
[[ -f "$CONF_DIR/swarm.env" ]] || install -m 644 "$KIT_DIR/config/swarm.env.example" "$CONF_DIR/swarm.env"
make_token() {  # make_token FILE OWNER:GROUP MODE ; keeps an existing token
  [[ -s "$1" ]] || (umask 077; head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$1")
  chown "$2" "$1"; chmod "$3" "$1"
}
make_token "$CONF_DIR/admin.token" root:swarm 640
make_token "$CONF_DIR/worker.token" root:swarm 640
install -o "$AGENT_USER" -g "$AGENT_USER" -m 600 "$CONF_DIR/worker.token" "/home/$AGENT_USER/.config/swarm/worker.token"
install -d -m 700 /root/.config/swarm
printf 'SWARM_URL=http://127.0.0.1:8750\nSWARM_TOKEN_FILE=%s/admin.token\n' "$CONF_DIR" > /root/.config/swarm/client.env

say "Model CLIs for '$AGENT_USER' (you log in to them after this script)"
if [[ $WITH_CLAUDE -eq 1 ]]; then
  sudo -iu "$AGENT_USER" bash -c 'command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash' || \
    echo "Claude Code install failed; see docs/worker-setup.md"
fi
if [[ $WITH_CODEX -eq 1 ]]; then
  sudo -iu "$AGENT_USER" bash -c 'mkdir -p ~/.local && npm config set prefix ~/.local && (command -v codex >/dev/null || npm install -g @openai/codex)' || \
    echo "Codex CLI install failed; see docs/worker-setup.md"
fi
# shellcheck disable=SC2016  # $HOME and $PATH must stay literal in .profile
grep -q '.local/bin' "/home/$AGENT_USER/.profile" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "/home/$AGENT_USER/.profile"
sudo -iu "$AGENT_USER" git config --global user.name >/dev/null 2>&1 || {
  sudo -iu "$AGENT_USER" git config --global user.name "Swarm Worker"
  sudo -iu "$AGENT_USER" git config --global user.email "swarm-worker@users.noreply.github.com"
}

say "Services (restart on crash and on reboot)"
install -m 644 "$KIT_DIR"/server/systemd/*.service "$KIT_DIR"/server/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now swarm-backend.service
systemctl enable swarm-worker.service
systemctl enable --now swarm-cleanup.timer swarm-backup.timer swarm-disk-alert.timer

for _ in 1 2 3 4 5; do curl -fsS http://127.0.0.1:8750/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8750/health >/dev/null || die "backend did not start: journalctl -u swarm-backend -n 50"

cat <<EOF

Setup finished. The backend is running. The worker service is enabled but waits for you.

Next steps (details in docs/worker-setup.md):
  1. Log in the model CLIs as the agent user, with your own subscriptions:
       sudo -iu $AGENT_USER claude        (then type /login)
       sudo -iu $AGENT_USER codex login --device-auth
  2. Log in GitHub for repo work:  sudo -iu $AGENT_USER gh auth login
  3. Start the worker:  systemctl start swarm-worker && swarmctl status
  4. Let your bots reach the backend (docs/backend.md, "Reaching the backend").
  5. Add your bots (docs/results-routing.md) and run the first-week walkthrough.
  6. When Tailscale SSH works from your laptop, close public SSH:
       sudo bash $KIT_DIR/scripts/setup-worker.sh --lockdown

Tokens are in $CONF_DIR (admin.token, worker.token). Never paste them into chat.
EOF
