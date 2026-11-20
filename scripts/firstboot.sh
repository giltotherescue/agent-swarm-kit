#!/usr/bin/env bash
# firstboot.sh: build a swarm worker with nobody typing. cloud-init (scripts/cloud-init.yaml, filled in by
# scripts/render-cloud-init.py) downloads the kit and starts this once, as root, after the server's own
# first-boot setup is finished.
#
# Steps, each written to the status file so a bot can read progress over SSH:
#   setup      scripts/setup-worker.sh --yes --admin-user NAME (and --auth-key FILE when there is one)
#   tailscale  without an auth key: start "tailscale up --ssh" and save the login link for the bot
#   secrets    scripts/setup-secrets.sh --prepare (tools, a worker age key, a read-only deploy key; no repo yet)
#   worker     start swarm-worker
#   smoke      scripts/smoke-test.sh (0 failed is required; the GitHub and login warnings are expected)
#   lockdown   close public SSH, once Tailscale is connected (LOCKDOWN=auto), or leave it to the bot (LOCKDOWN=bot)
#
# Settings come from /etc/agent-swarm-firstboot.env (KEY=VALUE, no secrets in it). The Tailscale auth key, if any,
# is a file the renderer wrote with mode 600; this script deletes it as soon as Tailscale has joined.
#   ADMIN_USER           required
#   AUTH_KEY_FILE        optional path to a Tailscale auth key file
#   LOCKDOWN             auto (default) or bot
#   WITH_CLAUDE, WITH_CODEX   1 (default) or 0
#   TAILSCALE_WAIT       seconds to wait for Tailscale to connect before giving up on lockdown (default 1800)
# Status: /var/lib/agent-swarm/firstboot/status holds one line per step ("setup ok", "smoke failed ...") and ends
# with "finished" or "stopped". The Tailscale login link, when needed, is in .../tailscale-login-url.
# Exit codes: 0 finished, 1 stopped on an error.
#
# A reboot before the end: while it runs, swarm-firstboot-resume.service is enabled and runs
# "firstboot.sh --resume" at the next boot. If the reboot came during the wait for Tailscale, that picks up the wait
# (with a fresh login link) and the lockdown. A reboot during any other step writes "stopped" with what to do. Either
# way the status file always ends with "finished" or "stopped", and the resume unit removes itself.
set -uo pipefail

ENV_FILE="${FIRSTBOOT_ENV_FILE:-/etc/agent-swarm-firstboot.env}"
STATE_DIR="${FIRSTBOOT_STATE_DIR:-/var/lib/agent-swarm/firstboot}"
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${FIRSTBOOT_LOG:-/var/log/agent-swarm-firstboot.log}"
POLL="${FIRSTBOOT_POLL:-10}"
UNIT_DIR="${FIRSTBOOT_UNIT_DIR:-/etc/systemd/system}"
RESUME_UNIT=swarm-firstboot-resume.service

status() { printf '%s %s\n' "$1" "${2:-}" | sed 's/ *$//' >> "$STATE_DIR/status"; printf '==> %s %s\n' "$1" "${2:-}"; }
phase() { printf '%s\n' "$1" > "$STATE_DIR/phase"; }
stop() { status stopped "$*"; remove_resume_unit; exit 1; }

# install_resume_unit: run "firstboot.sh --resume" at the next boot, until firstboot ends one way or the other
install_resume_unit() {
  cat > "$UNIT_DIR/$RESUME_UNIT" <<EOF
[Unit]
Description=agent-swarm-kit first-boot setup, after a reboot that interrupted it
After=network-online.target
Wants=network-online.target
ConditionPathExists=!$STATE_DIR/done
[Service]
# simple, not oneshot: the boot must not wait on the Tailscale wait
Type=simple
ExecStart=/bin/bash $KIT_DIR/scripts/firstboot.sh --resume
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload >/dev/null 2>&1
  systemctl enable "$RESUME_UNIT" >/dev/null 2>&1 || true
}

remove_resume_unit() {
  : > "$STATE_DIR/done"
  [[ -e "$UNIT_DIR/$RESUME_UNIT" ]] || return 0
  systemctl disable "$RESUME_UNIT" >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/$RESUME_UNIT"
  systemctl daemon-reload >/dev/null 2>&1 || true
}

# login_url FILE: print the first Tailscale login link in FILE (tailscale up prints one when it needs a browser)
login_url() { grep -Eo 'https://login\.tailscale\.com/[A-Za-z0-9/_.?=&%-]+' "$1" 2>/dev/null | head -n 1; }

# tailscale_ready: true when Tailscale is connected and this machine has a tailnet address
tailscale_ready() {
  command -v tailscale >/dev/null 2>&1 || return 1
  tailscale status >/dev/null 2>&1 || return 1
  [[ -n "$(tailscale ip -4 2>/dev/null | head -n 1)" ]]
}

wait_for_tailscale() {  # wait_for_tailscale SECONDS : returns 0 once ready
  local end=$(( SECONDS + $1 ))
  until tailscale_ready; do
    (( SECONDS >= end )) && return 1
    sleep "$POLL"
  done
}

# ask_tailscale_login: start "tailscale up --ssh" in the background and save its login link for the bot
ask_tailscale_login() {
  local tsout="$STATE_DIR/tailscale-up.out"
  : > "$tsout"; chmod 600 "$tsout"
  nohup tailscale up --ssh --timeout=60m > "$tsout" 2>&1 < /dev/null &
  for _ in $(seq 1 30); do [[ -n "$(login_url "$tsout")" ]] && break; sleep 2; done
  login_url "$tsout" > "$STATE_DIR/tailscale-login-url"
  if [[ -s "$STATE_DIR/tailscale-login-url" ]]; then status tailscale "waiting for approval, link in $STATE_DIR/tailscale-login-url"
  else status tailscale "no login link yet (see $tsout)"; fi
}

# lockdown_when_joined: the last step, also the one a resume after a reboot picks up again
lockdown_when_joined() {
  phase tailscale-wait
  if wait_for_tailscale "${TAILSCALE_WAIT:-1800}"; then
    phase lockdown
    if bash "$KIT_DIR/scripts/setup-worker.sh" --lockdown --yes --admin-user "$ADMIN_USER"; then status lockdown "ok: public SSH closed"
    else stop "lockdown failed; public SSH is still open"; fi
  else
    status lockdown "skipped: Tailscale never connected. Public SSH is still open. Approve the machine, then run setup-worker.sh --lockdown --yes"
  fi
  phase finished
  status finished
  remove_resume_unit
}

load_env() {
  [[ -r "$ENV_FILE" ]] || stop "missing $ENV_FILE"
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  : "${ADMIN_USER:?}"
  LOCKDOWN="${LOCKDOWN:-auto}"
  case "$LOCKDOWN" in auto|bot) ;; *) stop "LOCKDOWN must be auto or bot";; esac
}

# resume: run at boot by swarm-firstboot-resume.service when an earlier run never wrote finished or stopped
resume() {
  [[ $EUID -eq 0 || -n "${FIRSTBOOT_ALLOW_NON_ROOT:-}" ]] || { echo "firstboot: run as root" >&2; exit 1; }
  install -d -m 755 "$STATE_DIR"
  exec > >(tee -a "$LOG") 2>&1
  if [[ -f "$STATE_DIR/status" ]] && tail -n 1 "$STATE_DIR/status" | grep -Eq '^(finished|stopped)( |$)'; then
    remove_resume_unit
    return 0
  fi
  local was
  was=$(cat "$STATE_DIR/phase" 2>/dev/null || echo unknown)
  if [[ "$was" != tailscale-wait ]]; then
    stop "a reboot interrupted first boot during the ${was} step. Run it again: sudo bash $KIT_DIR/scripts/firstboot.sh"
  fi
  load_env
  status resumed "after a reboot during the Tailscale wait"
  if ! tailscale_ready; then ask_tailscale_login; fi
  lockdown_when_joined
}

main() {
  [[ $EUID -eq 0 || -n "${FIRSTBOOT_ALLOW_NON_ROOT:-}" ]] || { echo "firstboot: run as root" >&2; exit 1; }
  install -d -m 755 "$STATE_DIR"
  : > "$STATE_DIR/status"
  rm -f "$STATE_DIR/done"
  exec > >(tee -a "$LOG") 2>&1
  phase setup
  install_resume_unit
  load_env
  status started "kit $(tr -d '[:space:]' < "$KIT_DIR/VERSION")"

  local args=(--yes --admin-user "$ADMIN_USER")
  [[ "${WITH_CLAUDE:-1}" == 0 ]] && args+=(--no-claude)
  [[ "${WITH_CODEX:-1}" == 0 ]] && args+=(--no-codex)
  if [[ -n "${AUTH_KEY_FILE:-}" && -r "$AUTH_KEY_FILE" ]]; then args+=(--auth-key "$AUTH_KEY_FILE"); else args+=(--tailscale later); fi

  bash "$KIT_DIR/scripts/setup-worker.sh" "${args[@]}"
  local rc=$?
  # Exit 3 means "finished, with things listed for attention". Those are the logins and the like that a bot does next.
  if (( rc != 0 && rc != 3 )); then stop "setup-worker.sh exited $rc (see /var/log/agent-swarm-setup.log)"; fi
  status setup ok

  if [[ -n "${AUTH_KEY_FILE:-}" ]]; then
    if wait_for_tailscale 300; then status tailscale joined; else status tailscale "not connected yet"; fi
    shred -u "$AUTH_KEY_FILE" 2>/dev/null || rm -f "$AUTH_KEY_FILE"   # one-use key, gone from disk now
  elif ! tailscale_ready; then
    # No key: ask Tailscale for a login link and keep waiting in the background. The bot reads the link over SSH
    # and gives it to the owner, who approves the machine in a browser.
    phase tailscale
    ask_tailscale_login
  fi

  phase secrets

  if bash "$KIT_DIR/scripts/setup-secrets.sh" --prepare; then status secrets "prepared, no repo linked yet"
  else status secrets "failed (the bot can run setup-secrets.sh --prepare again)"; fi

  phase worker
  systemctl start swarm-worker || stop "swarm-worker did not start (journalctl -u swarm-worker -n 50)"
  status worker started

  phase smoke
  if bash "$KIT_DIR/scripts/smoke-test.sh" > "$STATE_DIR/smoke.out" 2>&1; then
    status smoke "ok: $(tail -n 1 "$STATE_DIR/smoke.out")"
  else
    stop "smoke test failed (see $STATE_DIR/smoke.out); public SSH is still open and nothing was locked"
  fi

  if [[ "$LOCKDOWN" == bot ]]; then
    status lockdown "left open on purpose: the bot closes it after it reaches this machine over Tailscale"
    phase finished
    status finished
    remove_resume_unit
  else
    lockdown_when_joined
  fi
}

# Tests load the functions without running anything: FIRSTBOOT_LIB=1 source scripts/firstboot.sh
if [[ "${FIRSTBOOT_LIB:-0}" != 1 ]]; then
  if [[ "${1:-}" == --resume ]]; then resume; else main "$@"; fi
fi
