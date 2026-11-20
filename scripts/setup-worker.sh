#!/usr/bin/env bash
# setup-worker.sh: turn a fresh Ubuntu LTS server (22.04 or 24.04) into a swarm worker, or upgrade one.
#
# Tested on: 0.3.0 passed a clean install on a fresh Ubuntu 24.04 server on 2026-11-18 (setup, smoke test,
# admin user, firewall and SSH hardening, lockdown refusal checks, upgrade by re-running this script, the
# project log, timeouts and chains with a stand-in for the model, task and cleanup flow). 0.3.1 fixes the
# small defects that run found. Not yet tested on a real server: joining Tailscale (real lockdown, Serve,
# Tailscale SSH), real Claude and Codex logins and model tasks, GitHub login and repo pushes, bot notify
# delivery. Read docs/worker-setup.md, run it on a server you can rebuild, and run scripts/smoke-test.sh.
#
# Run as root from a copy of this repo:
#   sudo bash scripts/setup-worker.sh --admin-user NAME   # install or upgrade; see "Tailscale" below
#   sudo bash scripts/setup-worker.sh --lockdown          # later, after SSH over Tailscale works: close public SSH
#
# Tailscale: by default the script joins Tailscale and WAITS (up to 15 minutes) until you open the login link
# it prints and approve the machine. To not wait, use --auth-key FILE, or --tailscale later (join any time
# after with: sudo tailscale up --ssh). --no-tailscale skips it fully, and then --lockdown cannot run.
#
# Options:
#   --admin-user NAME      your normal login with sudo. Made if missing (root's SSH keys are copied to it).
#                          With a working admin, setup turns off root and password login for SSH.
#   --tailscale MODE       now (default: join and wait for approval), later (install, join yourself later),
#                          off (same as --no-tailscale)
#   --auth-key FILE        join Tailscale without a browser; the key is read from FILE and never printed
#                          (--tailscale-authkey-file FILE is the old name and still works)
#   --tags TAGS            Tailscale tags to advertise, for example tag:swarm-worker (needs tagOwners in your ACL)
#   --no-tailscale         skip Tailscale. Costs you: no private SSH, and --lockdown refuses to run
#   --no-codex | --no-claude   skip installing one of the model CLIs
#   --swap-gb N            swap file size if the box has none (default 4, 0 to skip)
#   --max-wait SECONDS     on upgrade, how long to wait for running tasks to finish (default 3600)
#   --force-os             run on an Ubuntu version other than 22.04 or 24.04
#   --harden               only run the SSH and firewall hardening step (public SSH stays open)
#   --lockdown             only run the lockdown step: SSH reachable over Tailscale only
#   --yes                  never ask a question (for cloud-init and bots). Skips the "did you test SSH" prompts.
#                          Lockdown with --yes still refuses unless Tailscale is connected and has an address.
#
# Safe to run again: a second run is an upgrade. It never prints a token. Long installer output goes to
# /var/log/agent-swarm-setup.log; the screen shows the "==>" steps.
# Package installs first wait (up to 10 minutes, with a progress line) for first-boot setup (cloud-init) and any
# other apt or dpkg run to finish, then retry apt up to 4 times if it still hits a lock.
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
AGENT_HOME=/home/agent
RUNNER_USER=swarm-runner
LOG=/var/log/agent-swarm-setup.log
TS_KEY_FILE=""
TS_TAGS=""
TS_MODE=now
TS_STATE="not set up"
ADMIN_USER=""
WITH_CODEX=1
WITH_CLAUDE=1
SWAP_GB=4
MAX_WAIT=3600
FORCE_OS=0
ONLY=""
ASSUME_YES=0
INCOMPLETE=()
HARDENED="no"
# apt and the package locks. A brand new cloud server often installs packages at first boot (cloud-init,
# unattended-upgrades), so every apt call first waits for that (wait_for_apt), and retries if it still hits
# a lock (apt_run). DPkg::Lock::Timeout only covers the dpkg lock, not the one "apt-get update" takes.
APT=(apt-get -o DPkg::Lock::Timeout=120 -o Dpkg::Use-Pty=0 -qq)
APT_LOCKS=(/var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock)
APT_WAIT_SECONDS="${SWARM_APT_WAIT_SECONDS:-600}"   # how long to wait for first-boot setup and other updates
APT_TRIES="${SWARM_APT_TRIES:-4}"                  # apt runs per call when it fails on a lock
APT_RETRY_PAUSE="${SWARM_APT_RETRY_PAUSE:-20}"     # seconds between those runs
APT_POLL="${SWARM_APT_POLL:-5}"                    # seconds between checks while waiting
APT_PROGRESS="${SWARM_APT_PROGRESS:-30}"           # seconds between progress lines while waiting
APT_CI_TIMEOUT="${SWARM_APT_CI_TIMEOUT:-10}"       # how long one "cloud-init status --wait" check may take

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'setup-worker: %s\n' "$*" >&2; exit 1; }
todo() { INCOMPLETE+=("$*"); printf '    NOT DONE: %s\n' "$*"; }

logged() {  # logged COMMAND... : run a noisy command with its output in $LOG; on failure show the last lines
  printf '\n### %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"
  "$@" >>"$LOG" 2>&1 </dev/null && return 0
  local rc=$?
  printf '    failed (exit %s): %s\n    Last lines of %s:\n' "$rc" "$*" "$LOG"
  tail -n 15 "$LOG" | sed 's/^/      /'
  return "$rc"
}

as_agent() {  # as_agent 'shell code' : run code as the agent user in a login shell (so ~/.local/bin is on PATH)
  # Not "sudo -i": sudo -i backslash-escapes the command line, so $vars and $(...) inside it break (0.2.0 bug).
  (cd "$AGENT_HOME" && sudo -u "$AGENT_USER" -H bash -lc "$1")
}

# ---------- apt: wait for first-boot updates, retry on a lock ----------

lock_holders() {  # prints the process ids that have an apt or dpkg lock file open
  local f pid fd
  if command -v fuser >/dev/null 2>&1; then
    fuser "${APT_LOCKS[@]}" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -t "${APT_LOCKS[@]}" 2>/dev/null | sort -u
  else   # no fuser or lsof: look through /proc (works as root)
    for fd in /proc/[0-9]*/fd/*; do
      f=$(readlink "$fd" 2>/dev/null) || continue
      for l in "${APT_LOCKS[@]}"; do
        if [[ "$f" == "$l" ]]; then pid=${fd#/proc/}; echo "${pid%%/*}"; fi
      done
    done | sort -u
  fi
}

apt_busy() {  # prints why apt is not free now; prints nothing when it is free
  local pids names
  if command -v cloud-init >/dev/null 2>&1 && [[ "$(timeout "$APT_CI_TIMEOUT" cloud-init status --wait >/dev/null 2>&1; echo $?)" == 124 ]]; then
    echo "the server's first-boot setup (cloud-init)"; return 0
  fi
  pids=$(lock_holders | tr '\n' ' ')
  if [[ -n "${pids// }" ]]; then
    # shellcheck disable=SC2086  # one argument per process id
    names=$(ps -o comm= -p ${pids// /,} 2>/dev/null | sort -u | tr '\n' ' ')
    echo "another package install (${names:-process} ${pids% }) holding the apt or dpkg lock"; return 0
  fi
  if pgrep -x 'apt-get|apt|dpkg' >/dev/null 2>&1 || pgrep -f '^/usr/bin/python3 /usr/bin/unattended-upgrade( |$)' >/dev/null 2>&1; then
    echo "another apt, dpkg or unattended-upgrade run"; return 0
  fi
}

wait_for_apt() {  # waits up to APT_WAIT_SECONDS for apt to be free; returns 1 (with a message) if it never is
  local start=$SECONDS last=-100000 why
  while why=$(apt_busy) && [[ -n "$why" ]]; do
    if (( SECONDS - start >= APT_WAIT_SECONDS )); then
      echo "    Gave up after $(( SECONDS - start )) seconds: still waiting for $why."
      return 1
    fi
    if (( SECONDS - last >= APT_PROGRESS )); then
      echo "    Waiting for $why to finish ($(( SECONDS - start ))s so far, up to ${APT_WAIT_SECONDS}s)"
      last=$SECONDS
    fi
    sleep "$APT_POLL"
  done
  return 0
}

apt_run() {  # apt_run ARGS... : apt-get ARGS with output in $LOG; waits for the locks first and retries on a lock error
  local try rc out
  out=$(mktemp)
  for (( try = 1; try <= APT_TRIES; try++ )); do
    if ! wait_for_apt; then rm -f "$out"; return 1; fi
    printf '\n### %s: apt-get %s (try %s)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" "$try" >>"$LOG"
    rc=0
    "${APT[@]}" "$@" >"$out" 2>&1 </dev/null || rc=$?
    cat "$out" >>"$LOG"
    if (( rc == 0 )); then rm -f "$out"; return 0; fi
    # Retry only when another process had a lock ("update" also retries network errors); anything else is real.
    if ! grep -qE 'Could not get lock|Unable to lock|Unable to acquire the dpkg frontend lock|is another process using it' "$out" \
        && [[ "$1" != update ]]; then
      break
    fi
    if (( try < APT_TRIES )); then
      echo "    apt-get $1 failed (another process had a lock, or a network error); trying again in ${APT_RETRY_PAUSE}s (try $try of $APT_TRIES)"
      sleep "$APT_RETRY_PAUSE"
    fi
  done
  printf '    apt-get %s failed (exit %s). Last lines of %s:\n' "$1" "$rc" "$LOG"
  tail -n 15 "$out" | sed 's/^/      /'
  rm -f "$out"
  return "$rc"
}

# ---------- admin account checks (so hardening can never lock you out) ----------

admin_problems() {  # admin_problems NAME : prints one line per problem; no output means NAME is a usable admin
  local u="$1" ent uid home shell keys rules mode status sudo_ok=0 pw_ok=0
  if ! ent=$(getent passwd "$u"); then echo "user '$u' does not exist"; return 0; fi
  IFS=: read -r _ _ uid _ _ home shell <<<"$ent"
  if [[ "$u" == root ]] || (( uid < 1000 )); then echo "'$u' is root or a system account"; fi
  case "$u" in "$AGENT_USER"|"$RUNNER_USER"|swarm) echo "'$u' is a swarm service account, not an admin";; esac
  case "$shell" in */nologin|*/false|"") echo "'$u' has no login shell";; esac
  keys="$home/.ssh/authorized_keys"
  if [[ ! -s "$keys" ]] || ! grep -qE '^[^#[:space:]]' "$keys"; then
    echo "$keys is missing or empty, so '$u' cannot log in with a key"
  else
    [[ "$(stat -c %U "$keys")" == "$u" ]] || echo "$keys is not owned by '$u'"
    for f in "$home" "$home/.ssh" "$keys"; do
      mode=$(stat -c %a "$f")
      (( 8#$mode & 8#022 )) && echo "$f can be written by others, so SSH ignores the keys (chmod go-w $f)"
    done
  fi
  if id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qxE 'sudo|admin'; then sudo_ok=1; fi
  rules=""
  command -v sudo >/dev/null && rules=$(sudo -l -U "$u" 2>/dev/null || true)
  grep -q "may run the following" <<<"$rules" && sudo_ok=1
  grep -q 'NOPASSWD: ALL' <<<"$rules" && pw_ok=1
  (( sudo_ok )) || echo "'$u' cannot use sudo (usermod -aG sudo $u)"
  status=$(passwd -S "$u" 2>/dev/null | awk '{print $2}')
  [[ "$status" == P ]] && pw_ok=1
  (( pw_ok )) || echo "'$u' has no password, so sudo cannot work for it (set one: passwd $u)"
  return 0
}

find_admin() {  # sets ADMIN_USER to a usable admin: the sudo caller first, then the sudo group
  local u
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root && -z "$(admin_problems "$SUDO_USER")" ]]; then
    ADMIN_USER="$SUDO_USER"; return 0
  fi
  for u in $(getent group sudo admin | cut -d: -f4 | tr ',' ' '); do
    if [[ -z "$(admin_problems "$u")" ]]; then ADMIN_USER="$u"; return 0; fi
  done
  return 1
}

admin_howto() {  # admin_howto FLAG
  cat <<EOF
No usable admin account. This step turns off root login over SSH, so first you need a normal user with sudo
and your SSH key, or you would be locked out of the server. Either let the script make one:
  sudo bash $0 $1 --admin-user NAME
or make it yourself as root (replace NAME):
  adduser NAME                                     # asks for a password; sudo uses it, SSH does not
  usermod -aG sudo NAME
  rsync --archive --chown=NAME:NAME /root/.ssh /home/NAME    # copies root's SSH keys to NAME
Then check from your laptop that "ssh NAME@THIS-SERVER" works and "sudo -v" takes the password, and run this again.
EOF
}

create_admin() {  # create_admin NAME : make NAME a key-login admin with sudo (keeps anything already there)
  local u="$1" home grp root_keys
  [[ "$u" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "--admin-user: '$u' is not a valid user name"
  case "$u" in root|"$AGENT_USER"|"$RUNNER_USER"|swarm) die "--admin-user cannot be '$u'";; esac
  if ! id "$u" >/dev/null 2>&1; then
    echo "    Making the admin user '$u'"
    useradd --create-home --shell /bin/bash "$u"
  fi
  usermod -aG sudo "$u"
  home=$(getent passwd "$u" | cut -d: -f6)
  grp=$(id -gn "$u")
  install -d -m 700 -o "$u" -g "$grp" "$home/.ssh"
  if [[ ! -s "$home/.ssh/authorized_keys" ]]; then
    root_keys="$(getent passwd root | cut -d: -f6)/.ssh/authorized_keys"
    # Only plain key lines: some images put a "please log in as USER" command on root's keys.
    if [[ -s "$root_keys" ]] && grep -E '^(ssh-|ecdsa-|sk-)' "$root_keys" > "$home/.ssh/authorized_keys.new"; then
      chown "$u:$grp" "$home/.ssh/authorized_keys.new"; chmod 600 "$home/.ssh/authorized_keys.new"
      mv "$home/.ssh/authorized_keys.new" "$home/.ssh/authorized_keys"
      echo "    Copied root's SSH keys to $home/.ssh/authorized_keys, so the same key logs in as $u."
    else
      rm -f "$home/.ssh/authorized_keys.new"
      die "root has no plain SSH keys in $root_keys to copy. Put your public key in $home/.ssh/authorized_keys yourself."
    fi
  fi
  if [[ "$(passwd -S "$u" 2>/dev/null | awk '{print $2}')" != P ]] \
      && ! { command -v sudo >/dev/null && sudo -l -U "$u" 2>/dev/null | grep -q 'NOPASSWD: ALL'; }; then
    if [[ -t 0 ]]; then
      echo "    Choose a password for $u. sudo asks for it. SSH never does (keys only)."
      passwd "$u" || true
    fi
  fi
}

require_admin() {  # require_admin FLAG : make (if --admin-user) and check the admin, or stop with the steps
  local probs
  if [[ -n "$ADMIN_USER" ]]; then
    create_admin "$ADMIN_USER"
    probs=$(admin_problems "$ADMIN_USER")
    # shellcheck disable=SC2001  # indent every line
    [[ -z "$probs" ]] || die "$(printf 'the admin user %s is not ready:\n%s\nFix that and run this again. Nothing was hardened.' \
      "$ADMIN_USER" "$(sed 's/^/  - /' <<<"$probs")")"
  else
    find_admin || die "$(admin_howto "$1")"
  fi
  echo "    Admin account: $ADMIN_USER (sudo, SSH key)"
}

confirm() {  # confirm QUESTION : true only for an exact "yes" (reads stdin, so it also works when piped); --yes says yes
  local ok=""
  if (( ASSUME_YES )); then echo "    $1 yes (--yes)"; return 0; fi
  read -r -p "$1 (yes/no) " ok || true
  [[ "$ok" == yes ]]
}

admin_login_proven() {  # admin_login_proven NAME
  if [[ "${SUDO_USER:-}" == "$1" ]]; then
    echo "    You ran this with sudo as $1, so $1 can log in and use sudo."
    return 0
  fi
  echo "    Before root login is turned off, check that $1 works. In a second window on your laptop run:"
  echo "      ssh $1@THIS-SERVER   and then   sudo -v"
  confirm "    Did both work?"
}

# ---------- SSH and firewall ----------

ssh_ports() { local p; p=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | sort -u); echo "${p:-22}"; }

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
  if ! sshd -t; then
    rm -f /etc/ssh/sshd_config.d/00-swarm-hardening.conf
    die "sshd configuration test failed; the change was removed and nothing reloaded. Fix the error above."
  fi
  local eff
  eff="$(sshd -T 2>/dev/null)"
  grep -qx 'passwordauthentication no' <<<"$eff" || die "sshd still allows passwords after the change (check Match blocks)"
  grep -qx 'permitrootlogin no' <<<"$eff" || die "sshd still allows root login after the change (check Match blocks)"
  if grep -qiE '^\s*Match\s' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
    echo "    Note: your sshd config has Match blocks. Check they do not turn passwords or root login back on."
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd || die "could not reload sshd"
}

ufw_base() {  # firewall: deny incoming, allow outgoing, SSH over Tailscale, Tailscale direct connections
  local p
  apt_run install -y ufw || die "could not install ufw"
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw delete allow in on tailscale0 >/dev/null 2>&1 || true   # 0.1.0 opened every port to the tailnet
  for p in $(ssh_ports); do ufw allow in on tailscale0 to any port "$p" proto tcp >/dev/null; done
  ufw allow 41641/udp >/dev/null
}

public_ssh_rules() {  # prints ufw rules that let anyone on the internet reach SSH
  local p
  for p in $(ssh_ports); do
    ufw status numbered | grep -E "(^| )$p(/tcp)? " | grep -v tailscale0 | grep ALLOW || true
  done
}

harden() {  # install-time hardening: firewall on with public SSH still open; root login off once an admin works
  say "SSH and firewall hardening (public SSH stays open until --lockdown)"
  local p ok=0 probs
  ufw_base
  for p in $(ssh_ports); do ufw allow "$p/tcp" comment 'public SSH until setup-worker.sh --lockdown' >/dev/null; done
  ufw --force enable >/dev/null
  HARDENED="firewall"
  if [[ -n "$ADMIN_USER" ]]; then
    create_admin "$ADMIN_USER"
    probs=$(admin_problems "$ADMIN_USER")
    if [[ -z "$probs" ]]; then ok=1; else
      # shellcheck disable=SC2001  # indent every line
      printf '    Admin user %s is not ready:\n%s\n' "$ADMIN_USER" "$(sed 's/^/      - /' <<<"$probs")"
      todo "admin user $ADMIN_USER is not ready, so root login over SSH is still on. Fix the lines above, then: sudo bash $0 --harden --admin-user $ADMIN_USER"
    fi
  elif find_admin; then
    ok=1
  fi
  if (( ok )) && admin_login_proven "$ADMIN_USER"; then
    ssh_hardening
    HARDENED="full"
    echo "    Root login and password login over SSH are off. Log in as $ADMIN_USER from now on."
  else
    echo "    Root login over SSH is still ON: no admin account was confirmed. To fix, see docs/worker-setup.md"
    echo "    (\"Make your admin user\"), then run: sudo bash $0 --harden --admin-user NAME"
  fi
}

lockdown() {
  say "Lockdown: SSH only over Tailscale"
  # Check the admin first: closing public SSH and root login with no other account locks everyone out.
  require_admin --lockdown
  command -v tailscale >/dev/null \
    || die "Tailscale is not installed. Install it with: sudo bash $0 --tailscale later   (then join, then lock down)"
  tailscale status >/dev/null 2>&1 || die "Tailscale is not connected. Join first: sudo tailscale up --ssh"
  # With --yes nobody proves SSH works, so at least insist that this machine really has a tailnet address.
  if (( ASSUME_YES )) && [[ -z "$(tailscale ip -4 2>/dev/null | head -n 1)" ]]; then
    die "Tailscale has no address for this machine yet, so lockdown with --yes refuses. Wait for it to join, then run again."
  fi
  cat <<MSG
Before continuing, prove you can reach this box over Tailscale from another device, as $ADMIN_USER:
  ssh $ADMIN_USER@<this-machine's-tailscale-name>     and then   sudo -v
If that does not work, answer "no" and fix it first, or you may lock yourself out.
Your provider's web console (serial console) is the way back in if something goes wrong.
Note: Tailscale SSH is its own SSH server inside Tailscale. It follows your tailnet policy (the "ssh" rules
in the ACL), not the OpenSSH settings below. Use the sample policy in docs/worker-setup.md with
"users": ["$ADMIN_USER"], so that only your admin group can log in, and never as root.
MSG
  confirm "Can you log in as $ADMIN_USER over Tailscale and use sudo right now?" || die "stopped; nothing changed"
  ufw_base
  # Keep your other rules. Only remove rules that open SSH to the whole internet.
  local p n tries=0
  for p in $(ssh_ports); do
    for r in "$p/tcp" "$p"; do ufw delete allow "$r" >/dev/null 2>&1 || true; done
  done
  for r in "OpenSSH" "ssh"; do ufw delete allow "$r" >/dev/null 2>&1 || true; done
  # Anything left (for example a rule with a comment), one at a time by its number.
  while n=$(public_ssh_rules | head -n 1 | sed -nE 's/^\[ *([0-9]+)\].*/\1/p') && [[ -n "$n" ]] && (( tries++ < 20 )); do
    ufw --force delete "$n" >/dev/null || break
  done
  ufw --force enable >/dev/null
  ssh_hardening
  [[ -z "$(public_ssh_rules)" ]] || die "a public SSH rule is still in ufw; see: sudo ufw status numbered"
  say "Done. Public SSH is closed in ufw; SSH works over Tailscale only, as $ADMIN_USER."
  echo "Also close port 22 in your provider's cloud firewall if it has one."
  ufw status verbose
}

# ---------- install steps ----------

tailscale_step() {
  if [[ "$TS_MODE" == off ]]; then
    TS_STATE="skipped (--no-tailscale): no private SSH, and --lockdown will refuse until Tailscale is joined"
    return 0
  fi
  say "Tailscale"
  if ! command -v tailscale >/dev/null; then
    local tmp_ts; tmp_ts=$(mktemp)
    wait_for_apt || true
    if ! curl -fsSL https://tailscale.com/install.sh -o "$tmp_ts" || ! logged sh "$tmp_ts"; then
      rm -f "$tmp_ts"
      todo "Tailscale did not install (see $LOG)"
      TS_STATE="not installed"
      return 0
    fi
    rm -f "$tmp_ts"
  fi
  if tailscale status >/dev/null 2>&1; then
    tailscale set --ssh || todo "could not turn on Tailscale SSH (tailscale set --ssh)"
    TS_STATE="joined"
    return 0
  fi
  local mode="$TS_MODE" ts_args=(--ssh --timeout=15m)
  [[ -n "$TS_TAGS" ]] && ts_args+=(--advertise-tags "$TS_TAGS")
  if [[ "$mode" == now && -z "$TS_KEY_FILE" && ! -t 0 ]]; then
    echo "    No terminal to show the Tailscale login link and no --auth-key, so joining later (like --tailscale later)."
    mode=later
  fi
  if [[ "$mode" == later ]]; then
    echo "    Tailscale is installed but not joined. Nothing waits for a browser."
    echo "    Join when you are ready (it prints a link to approve): sudo tailscale up --ssh${TS_TAGS:+ --advertise-tags $TS_TAGS}"
    TS_STATE="installed, not joined yet (join: sudo tailscale up --ssh)"
    return 0
  fi
  if [[ -n "$TS_KEY_FILE" ]]; then
    tailscale up "${ts_args[@]}" --auth-key "file:$TS_KEY_FILE" || true
  else
    echo "    The script now WAITS (up to 15 minutes) until you open the link below and approve this machine."
    echo "    To not wait: press Ctrl-C and run the script again with --tailscale later."
    tailscale up "${ts_args[@]}" || true
  fi
  if tailscale status >/dev/null 2>&1; then
    TS_STATE="joined"
  else
    todo "Tailscale is not connected. Join: sudo tailscale up --ssh"
    TS_STATE="installed, not joined"
  fi
}

install_model_clis() {
  say "Model CLIs for '$AGENT_USER' (you log in to them after this script)"
  # PATH first, so the checks below find what the installers put in ~/.local/bin.
  # shellcheck disable=SC2016  # $HOME and $PATH must stay literal in .profile
  if ! grep -q '.local/bin' "$AGENT_HOME/.profile" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$AGENT_HOME/.profile"
    chown "$AGENT_USER:$AGENT_USER" "$AGENT_HOME/.profile"
  fi
  if [[ $WITH_CLAUDE -eq 1 ]]; then
    local claude_failed=0
    if ! as_agent 'command -v claude' >/dev/null 2>&1; then
      # shellcheck disable=SC2016  # expanded by the agent's shell, not here
      if ! logged as_agent 'f=$(mktemp) || exit 1; curl -fsSL https://claude.ai/install.sh -o "$f" && bash "$f"; rc=$?; rm -f "$f"; exit $rc'; then
        claude_failed=1
        todo "Claude Code did not install: the installer failed (see $LOG). Run this script again to retry."
      fi
    fi
    if (( ! claude_failed )) && ! as_agent 'claude --version' >/dev/null 2>&1; then
      todo "Claude Code was installed but 'claude --version' does not run as $AGENT_USER (see $LOG)"
    fi
  fi
  if [[ $WITH_CODEX -eq 1 ]]; then
    if ! as_agent 'command -v codex' >/dev/null 2>&1; then
      logged as_agent 'mkdir -p ~/.local && npm config set prefix ~/.local && npm install -g @openai/codex' \
        || todo "Codex CLI did not install (see $LOG)"
    fi
    as_agent 'codex --version' >/dev/null 2>&1 || todo "codex does not run as $AGENT_USER (see $LOG)"
  fi
  as_agent 'git config --global user.name' >/dev/null 2>&1 \
    || as_agent 'git config --global user.name "Swarm Worker" && git config --global user.email "swarm-worker@users.noreply.github.com"'
  if as_agent 'gh auth status' >/dev/null 2>&1; then
    todo "the $AGENT_USER user is logged in to GitHub. Agents must not hold a GitHub login: run 'sudo -iu $AGENT_USER gh auth logout' after logging in $RUNNER_USER"
  fi
}

prune_releases() {  # keep only the releases that current and previous point at
  local cur prev d real
  cur=$(readlink -f "$KIT_ROOT/current" 2>/dev/null || true)
  prev=$(readlink -f "$KIT_ROOT/previous" 2>/dev/null || true)
  [[ -n "$cur" && -d "$cur" ]] || return 0
  for d in "$KIT_ROOT"/releases/*; do
    [[ -d "$d" && ! -L "$d" ]] || continue
    real=$(readlink -f "$d")
    [[ "$real" == "$cur" || "$real" == "$prev" ]] && continue
    rm -rf -- "$d"
    echo "    Removed old release $(basename "$d")"
  done
}

first_backup() {  # a new install has no backup until 03:10; make one now so status is not stuck warning
  if [[ -z "$(find "$DATA_DIR/backups" -name '*.db.gz' 2>/dev/null | head -n 1)" ]]; then
    logged systemctl start swarm-backup.service || todo "the first database backup failed (systemctl status swarm-backup)"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --auth-key|--tailscale-authkey-file) TS_KEY_FILE="${2:?missing file}"; shift 2;;
      --tags) TS_TAGS="${2:?missing tags}"; shift 2;;
      --tailscale)
        case "${2:-}" in now|later|off) TS_MODE="$2";; *) die "--tailscale needs now, later or off";; esac
        shift 2;;
      --no-tailscale) TS_MODE=off; shift;;
      --admin-user) ADMIN_USER="${2:?missing user name}"; shift 2;;
      --no-codex) WITH_CODEX=0; shift;;
      --no-claude) WITH_CLAUDE=0; shift;;
      --swap-gb) SWAP_GB="${2:?}"; shift 2;;
      --max-wait) MAX_WAIT="${2:?}"; shift 2;;
      --force-os) FORCE_OS=1; shift;;
      --harden) ONLY=harden; shift;;
      --lockdown) ONLY=lockdown; shift;;
      --yes|--non-interactive) ASSUME_YES=1; shift;;
      -h|--help) sed -n '2,38p' "$0"; exit 0;;
      *) die "unknown option: $1";;
    esac
  done
}

main() {
  parse_args "$@"
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
  touch "$LOG"; chmod 600 "$LOG"
  export DEBIAN_FRONTEND=noninteractive
  # Make the apt calls inside the Node.js and Tailscale installers wait for the dpkg lock too.
  printf 'DPkg::Lock::Timeout "120";\n' > /etc/apt/apt.conf.d/90-agent-swarm-lock-wait

  if [[ "$ONLY" == lockdown ]]; then lockdown; exit 0; fi
  if [[ "$ONLY" == harden ]]; then harden; exit 0; fi
  echo "Installer output goes to $LOG (sudo tail -f $LOG to watch it)."
  if [[ -n "$ADMIN_USER" ]]; then
    say "Admin user '$ADMIN_USER' (checked now, so problems show before the long installs)"
    create_admin "$ADMIN_USER"
  fi

  VERSION_NEW="$(tr -d '[:space:]' < "$KIT_SRC/VERSION")"
  UPGRADE=0
  [[ -e "$KIT_ROOT/current" || -d "$KIT_ROOT/server" ]] && UPGRADE=1

  say "Checking the '$AGENT_USER' account before anything else"
  if id "$AGENT_USER" >/dev/null 2>&1; then
    uid=$(id -u "$AGENT_USER")
    home=$(getent passwd "$AGENT_USER" | cut -d: -f6)
    (( uid >= 1000 )) || die "user '$AGENT_USER' exists with uid $uid (a system account). Remove it or pick another server."
    [[ "$home" == "$AGENT_HOME" ]] || die "user '$AGENT_USER' has home $home; this kit needs $AGENT_HOME"
    for g in sudo admin wheel adm docker lxd libvirt disk root; do
      if id -nG "$AGENT_USER" | tr ' ' '\n' | grep -qx "$g"; then
        die "user '$AGENT_USER' is in the '$g' group, which gives admin power. Remove it: gpasswd -d $AGENT_USER $g"
      fi
    done
    if sudo -l -U "$AGENT_USER" 2>/dev/null | grep -q "may run the following"; then
      die "user '$AGENT_USER' has sudo rights (sudo -l -U $AGENT_USER). Remove them before installing."
    fi
    if [[ -s "$AGENT_HOME/.ssh/authorized_keys" ]]; then
      echo "    Note: $AGENT_HOME/.ssh/authorized_keys is not empty. Nobody needs to log in as $AGENT_USER; consider emptying it."
    fi
  fi

  say "System packages and automatic security updates (can take a few minutes)"
  echo "    First, wait for any first-boot setup or other update to finish (up to $(( APT_WAIT_SECONDS / 60 )) minutes)."
  apt_run update || die "apt-get update failed after waiting and $APT_TRIES tries (see $LOG). If another update is still running, wait for it and run this script again."
  apt_run upgrade -y || die "apt-get upgrade failed (see $LOG)"
  apt_run install -y ca-certificates curl git jq tmux python3 sqlite3 unattended-upgrades ufw sudo acl \
    build-essential unzip rsync || die "package install failed (see $LOG)"
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
    apt_run update && apt_run install -y gh || true
  fi
  gh --version >/dev/null 2>&1 || die "GitHub CLI did not install (see $LOG)"

  say "Node.js LTS (needed by Codex CLI and most web repos)"
  node_ok() { command -v node >/dev/null && command -v npm >/dev/null && (( $(node -p 'process.versions.node.split(".")[0]') >= 18 )); }
  if ! node_ok; then
    tmp_ns=$(mktemp)
    curl -fsSL https://deb.nodesource.com/setup_lts.x -o "$tmp_ns"
    wait_for_apt || true
    logged bash "$tmp_ns" || true
    rm -f "$tmp_ns"
    apt_run install -y nodejs || true
  fi
  node_ok || die "Node.js 18 or newer with npm is required (found: $(node --version 2>/dev/null || echo none); see $LOG)"

  tailscale_step

  say "Users: '$AGENT_USER' runs agents (no sudo, no tokens), '$RUNNER_USER' runs the runner, 'swarm' runs the backend"
  id "$AGENT_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$AGENT_USER"
  id swarm >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin swarm
  id "$RUNNER_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$RUNNER_HOME" --shell /usr/sbin/nologin "$RUNNER_USER"
  chmod 750 "$AGENT_HOME"
  install -d -o swarm -g swarm -m 750 "$DATA_DIR" "$DATA_DIR/backups"
  install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 700 "$RUNNER_HOME" "$RUNNER_HOME/repos" "$RUNNER_HOME/logs"
  install -d -o root -g root -m 755 "$HANDOFF"
  # inbox: the runner writes, the agent reads. outbox: the agent writes, the runner reads. Nobody else.
  # The setgid bit (the 2 in 2750) gives new entries the other user's group. Code must never chmod these folders.
  install -d -o "$RUNNER_USER" -g "$AGENT_USER" -m 2750 "$HANDOFF/inbox"
  install -d -o "$AGENT_USER" -g "$RUNNER_USER" -m 2750 "$HANDOFF/outbox"
  loginctl enable-linger "$AGENT_USER"   # gives agent a systemd user manager, so each task gets its own scope with limits
  install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 700 "$AGENT_HOME/work" "$AGENT_HOME/backups" \
    "$AGENT_HOME/.config" "$AGENT_HOME/.config/agent-secrets"

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
  # agent.env holds extras for tasks (a proxy address, the Claude token from "swarmctl login claude"). Root owns it,
  # the agent group reads it, nobody else does (0.5.0: it was world-readable before).
  [[ -f "$CONF_DIR/agent.env" ]] || install -m 640 /dev/null "$CONF_DIR/agent.env"
  chown root:"$AGENT_USER" "$CONF_DIR/agent.env"; chmod 640 "$CONF_DIR/agent.env"
  install -d -o swarm -g swarm -m 700 "$CONF_DIR/bot-keys"
  make_token() {  # make_token FILE OWNER:GROUP MODE [rotate] ; keeps an existing token unless asked to rotate
    if [[ ! -s "$1" || "${4:-}" == rotate ]]; then
      (umask 077; head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$1.new"); mv "$1.new" "$1"
    fi
    chown "$2" "$1"; chmod "$3" "$1"
  }
  make_token "$CONF_DIR/admin.token" root:swarm 640
  ROTATE=""
  if [[ -e "$AGENT_HOME/.config/swarm/worker.token" ]]; then
    # 0.1.0 gave the agent user a copy of the worker token. Agents could read it, so replace it.
    echo "    The old worker token was readable by agents. Making a new one."
    rm -f "$AGENT_HOME/.config/swarm/worker.token"
    ROTATE=rotate
  fi
  make_token "$CONF_DIR/worker.token" root:swarm 640 "$ROTATE"
  install -o "$RUNNER_USER" -g "$RUNNER_USER" -m 600 "$CONF_DIR/worker.token" "$RUNNER_HOME/worker.token"
  install -d -m 700 /root/.config/swarm
  printf 'SWARM_URL=http://127.0.0.1:8750\nSWARM_TOKEN_FILE=%s/admin.token\n' "$CONF_DIR" > /root/.config/swarm/client.env
  chmod 600 /root/.config/swarm/client.env

  install_model_clis

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
      (( waited >= MAX_WAIT )) && { rm -f "$RUNNER_HOME/drain" "$hdr"; die "tasks are still running after $MAX_WAIT seconds; nothing switched. Run again later."; }
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
  ln -sfn /usr/local/bin/with-secrets /usr/local/bin/swarm-sync-secrets
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
  systemctl enable swarm-backend.service swarm-worker.service >/dev/null 2>&1
  systemctl restart swarm-backend.service
  systemctl enable --now swarm-cleanup.timer swarm-backup.timer swarm-disk-alert.timer >/dev/null 2>&1

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

  say "Old releases: keeping current and previous only"
  prune_releases
  say "First database backup (if there is none yet)"
  first_backup

  harden

  cat <<EOF

Setup finished (kit $VERSION_NEW). The backend is running.$( [[ $UPGRADE -eq 1 ]] && echo " This was an upgrade; see CHANGELOG.md." )
Installer output: $LOG

Security right now:
  - Firewall (ufw): on. SSH is still open to the internet until you run --lockdown.
  - Root login and password login over SSH: $( [[ $HARDENED == full ]] && echo "off. Log in as $ADMIN_USER." || echo "STILL ON. No admin user was confirmed; see docs/worker-setup.md, \"Make your admin user\"." )
  - Tailscale: $TS_STATE

Next steps (details in docs/worker-setup.md):
  1. Sign in the model CLIs and GitHub. Each command starts the sign-in and prints only a link and a code to open in a browser:
       sudo swarmctl login codex
       sudo swarmctl login claude        (the browser then shows a code; send it with: swarmctl login claude --finish)
       sudo swarmctl login github        (the runner's login: it pushes and opens PRs; agents never get it)
     Details and the by-hand way: docs/worker-setup.md, step 7.
  2. Codex needs device-code login turned on in ChatGPT Security Settings first (docs/troubleshooting.md).
  3. Start the worker and check everything (on a new box expect 2 WARN: GitHub and Tailscale, until you do them):
       sudo systemctl start swarm-worker
       sudo bash $KIT_ROOT/current/scripts/smoke-test.sh
  4. Join Tailscale if you have not (sudo tailscale up --ssh), then let your bots reach the backend over
     Tailscale Serve (docs/backend.md, "Reaching the backend").
  5. Add your bots and bind them to projects (templates/bot-setup-checklist.md).
  6. When SSH over Tailscale works from your laptop, close public SSH:
       sudo bash $KIT_ROOT/current/scripts/setup-worker.sh --lockdown${ADMIN_USER:+ --admin-user $ADMIN_USER}

Tokens are in $CONF_DIR (admin.token, worker.token) and $RUNNER_HOME/worker.token. Never paste them into chat.
EOF
  if (( ${#INCOMPLETE[@]} )); then
    printf '\nSETUP NEEDS YOUR ATTENTION:\n'
    printf '  - %s\n' "${INCOMPLETE[@]}"
    exit 3
  fi
}

# Tests load the functions without running anything: SWARM_SETUP_LIB=1 source scripts/setup-worker.sh
if [[ "${SWARM_SETUP_LIB:-0}" != 1 ]]; then
  main "$@"
fi
