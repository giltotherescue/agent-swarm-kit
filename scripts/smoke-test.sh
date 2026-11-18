#!/usr/bin/env bash
# smoke-test: after setup-worker.sh, check that the worker is set up the way the docs say, then run one
# script task (no model, no tokens spent) from queued to done.
# Usage: sudo bash scripts/smoke-test.sh [--no-task]
# Prints PASS, WARN or FAIL per check. Exit 1 if anything failed. It never prints a token.
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
RUN_TASK=1; [[ "${1:-}" == "--no-task" ]] && RUN_TASK=0
AGENT=agent; RUNNER=swarm-runner; CONF=/etc/agent-swarm
fails=0; warns=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }
warn() { printf 'WARN  %s\n' "$*"; warns=$((warns + 1)); }
check() { local msg="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$msg"; else fail "$msg"; fi; }
mode_owner() { stat -c '%a %U:%G' "$1" 2>/dev/null; }
expect_mode() {  # expect_mode FILE "640 root:swarm"
  local got; got=$(mode_owner "$1")
  if [[ "$got" == "$2" ]]; then pass "$1 is $2"; else fail "$1 is '${got:-missing}', expected $2"; fi
}
agent_cannot_read() {
  if sudo -u "$AGENT" test -r "$1" 2>/dev/null; then fail "agent can read $1"; else pass "agent cannot read $1"; fi
}

echo "== Users"
for u in "$AGENT" swarm "$RUNNER"; do check "user $u exists" id "$u"; done
if id -nG "$AGENT" | tr ' ' '\n' | grep -qxE 'sudo|admin|wheel|adm|docker|lxd'; then fail "agent is in an admin group"; else pass "agent is in no admin group"; fi
if sudo -l -U "$AGENT" 2>/dev/null | grep -q "may run the following"; then fail "agent has sudo rights"; else pass "agent has no sudo rights"; fi
rules=$(sudo -l -U "$RUNNER" 2>/dev/null | sed -n '/may run the following/,$p' | grep -c NOPASSWD || true)
if [[ "$rules" == 1 ]] && sudo -l -U "$RUNNER" 2>/dev/null | grep -q "($AGENT) NOPASSWD: /usr/local/lib/agent-swarm/agent-exec"; then
  pass "swarm-runner may run only agent-exec as agent"
else fail "swarm-runner sudo rights are not exactly the one agent-exec rule (sudo -l -U $RUNNER)"; fi
check "linger is on for agent (per-task limits need it)" test -e "/var/lib/systemd/linger/$AGENT"
check "limits for all tasks together are installed" test -f "/etc/systemd/system/user-$(id -u "$AGENT").slice.d/50-swarm-limits.conf"

echo "== Files and permissions"
expect_mode "$CONF/admin.token" "640 root:swarm"
expect_mode "$CONF/worker.token" "640 root:swarm"
expect_mode /var/lib/swarm-runner/worker.token "600 $RUNNER:$RUNNER"
expect_mode "$CONF/bot-keys" "700 swarm:swarm"
expect_mode /usr/local/lib/agent-swarm/agent-exec "755 root:root"
expect_mode /etc/sudoers.d/swarm-runner "440 root:root"
expect_mode /srv/agent-swarm/inbox "2750 $RUNNER:$AGENT"
expect_mode /srv/agent-swarm/outbox "2750 $AGENT:$RUNNER"
for f in "$CONF/admin.token" "$CONF/worker.token" /var/lib/swarm-runner/worker.token /var/lib/agent-swarm/swarm.db; do agent_cannot_read "$f"; done
if [[ -e "/home/$AGENT/.config/swarm/worker.token" ]]; then fail "the agent user still has a worker token copy"; else pass "the agent user has no worker token"; fi
check "sudoers rule passes visudo" visudo -cf /etc/sudoers.d/swarm-runner
if [[ -n "$(find /var/lib/agent-swarm/backups -name '*.db.gz' -perm /077 2>/dev/null)" ]]; then fail "a backup file is readable by others"; else pass "backup files are private"; fi

echo "== Services and timers"
for s in swarm-backend swarm-worker; do
  check "$s is enabled" systemctl is-enabled "$s"
  check "$s is running" systemctl is-active "$s"
done
for t in swarm-cleanup swarm-backup swarm-disk-alert; do check "$t.timer is active" systemctl is-active "$t.timer"; done
if systemctl --failed --no-legend | grep -q 'swarm-'; then fail "a swarm unit has failed: systemctl --failed"; else pass "no failed swarm units"; fi
check "automatic security updates are enabled" systemctl is-enabled unattended-upgrades
if swapon --show | grep -q .; then pass "swap is on"; else warn "no swap"; fi

echo "== Backend"
health=$(curl -fsS http://127.0.0.1:8750/health 2>/dev/null)
if [[ "$health" == *'"ok": true'* ]]; then pass "health endpoint answers ($health)"; else fail "health endpoint does not answer"; fi
if [[ "$health" == *"$(tr -d '[:space:]' < /opt/agent-swarm-kit/current/VERSION)"* ]]; then pass "backend runs the installed version"; else fail "backend version differs from /opt/agent-swarm-kit/current/VERSION"; fi
bind=$(grep -E '^SWARM_BIND=' "$CONF/swarm.env" | cut -d= -f2)
if [[ -z "$bind" || "$bind" == 127.0.0.1:* ]]; then pass "backend listens on localhost only"; else warn "SWARM_BIND is $bind, not localhost"; fi

echo "== Logins (checked, never printed)"
if sudo -u "$RUNNER" -H gh auth status >/dev/null 2>&1; then pass "swarm-runner is logged in to GitHub"; else warn "swarm-runner is not logged in to GitHub (repo tasks cannot clone or push): sudo -u $RUNNER -H gh auth login"; fi
if sudo -iu "$AGENT" bash -c 'gh auth status' >/dev/null 2>&1; then fail "agent is logged in to GitHub; agents must not hold it: sudo -iu $AGENT gh auth logout"; else pass "agent holds no GitHub login"; fi
for c in claude codex; do
  if sudo -iu "$AGENT" bash -c "command -v $c" >/dev/null 2>&1; then pass "$c is installed for agent"; else warn "$c is not installed for agent"; fi
done
if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then pass "Tailscale is connected"; else warn "Tailscale is not connected"; fi
if command -v ufw >/dev/null && ufw status | grep -E '(^| )22(/tcp)? ' | grep -v tailscale0 | grep -q ALLOW; then
  warn "public SSH is still open; run setup-worker.sh --lockdown once SSH over Tailscale works"
fi

if [[ $RUN_TASK -eq 1 ]]; then
  echo "== One script task, end to end (no model)"
  key="smoke-$(date -u +%Y%m%d%H%M%S)"
  out=$(echo "Smoke test: print ok" | swarmctl enqueue --command 'echo ok' --key "$key" --title "smoke test" - 2>&1)
  tid=$(awk '{print $1}' <<<"$out")
  if [[ -z "$tid" || "$tid" == swarmctl:* ]]; then fail "could not queue a task: $out"
  else
    state=""
    for _ in $(seq 1 60); do
      state=$(swarmctl show "$tid" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["state"])' 2>/dev/null)
      [[ "$state" == "done" || "$state" == failed || "$state" == cancelled ]] && break
      sleep 3
    done
    if [[ "$state" == "done" ]]; then
      log=$(swarmctl get-artifact "$tid" script.log -o /dev/stdout 2>/dev/null)
      if [[ "$log" == *ok* ]]; then pass "task $tid finished done and its log says ok"; else fail "task $tid is done but script.log is missing or wrong"; fi
    else
      fail "task $tid ended as '${state:-unknown}' (swarmctl show $tid; journalctl -u swarm-worker -n 50)"
    fi
  fi
fi

echo
echo "smoke-test: $fails failed, $warns warnings"
(( fails == 0 ))
