#!/usr/bin/env bash
# Root-only checks for scripts/setup-worker.sh: lockdown refusal on a root-only box, --admin-user, --harden.
# Run ONLY in a throwaway Ubuntu container or VM (test_setup_script.py starts it in Docker): it makes users,
# writes /etc/ssh and replaces tailscale, ufw, sshd, systemctl and apt-get with stubs. It puts all of that back
# when it exits, pass or fail, and refuses (exit 2, nothing changed) on a box that already has a usable admin.
# Usage: bash setup_root_check.sh KIT_DIR     Prints "SETUP ROOT CHECK OK" at the end, exit 1 on a failed check.
set -uo pipefail
KIT="${1:?kit dir}"
SETUP="$KIT/scripts/setup-worker.sh"
[[ $EUID -eq 0 ]] || { echo "must run as root in a throwaway container" >&2; exit 2; }
grep -q '^ID=ubuntu' /etc/os-release || { echo "needs an Ubuntu image" >&2; exit 2; }

STUB="${SWARM_ROOT_CHECK_STUB_DIR:-/usr/local/sbin}" CALLS=/tmp/stub-calls
fail() { echo "SETUP ROOT CHECK FAILED: $*"; echo "--- last output:"; cat /tmp/out 2>/dev/null; exit 1; }

# These checks need a root-only box: they expect lockdown to find no admin, and they make "alice" the admin.
# On a box that already has a sudo user they would fail for the wrong reason (install test 4, caveat B).
# "Usable admin" is decided by the setup script's own check, so a stock sudo user with no keys does not count.
if admin=$(SWARM_SETUP_LIB=1 SUDO_USER="" bash -c 'source "$1"; find_admin && echo "$ADMIN_USER"' _ "$SETUP"); then
  echo "this box already has a usable admin account ($admin), so it is not a fresh root-only box." >&2
  echo "Run these checks in Docker (the test does that) or on a brand new throwaway server." >&2
  exit 2
fi

# Everything below that changes the box is put back on exit, pass or fail: the stubs that shadow the real
# ufw, sshd, systemctl, tailscale and apt-get, root's authorized_keys, sshd_config, and the test user.
SAVE=$(mktemp -d /tmp/setup-root-check.XXXXXX)
SAVED_FILES=("$STUB/ufw" "$STUB/systemctl" "$STUB/apt-get" "$STUB/tailscale" "$STUB/sshd"
             /etc/ssh/sshd_config /etc/ssh/sshd_config.d/00-swarm-hardening.conf /root/.ssh/authorized_keys)
for f in "${SAVED_FILES[@]}"; do
  if [[ -e "$f" || -L "$f" ]]; then mkdir -p "$SAVE$(dirname "$f")"; cp -a "$f" "$SAVE$f"; fi
done
ALICE_EXISTED=0; id alice >/dev/null 2>&1 && ALICE_EXISTED=1
restore() {
  for f in "${SAVED_FILES[@]}"; do
    rm -f "$f"
    if [[ -e "$SAVE$f" || -L "$SAVE$f" ]]; then cp -a "$SAVE$f" "$f"; fi
  done
  (( ALICE_EXISTED )) || userdel -r alice >/dev/null 2>&1 || true
  rm -rf "$SAVE" /tmp/ts-joined "$CALLS" /tmp/out
}
trap restore EXIT

# Stubs. Each logs its arguments so we can see what would have changed.
for c in ufw systemctl apt-get; do
  printf '#!/bin/sh\necho "%s $*" >> %s\nexit 0\n' "$c" "$CALLS" > "$STUB/$c"
done
cat > "$STUB/tailscale" <<EOF
#!/bin/sh
echo "tailscale \$*" >> $CALLS
[ "\$1" = status ] && [ ! -e /tmp/ts-joined ] && exit 1
exit 0
EOF
cat > "$STUB/sshd" <<'EOF'
#!/bin/sh
case "$1" in
  -t) exit 0;;
  -T) echo "port 22"
      if [ -e /etc/ssh/sshd_config.d/00-swarm-hardening.conf ]; then
        echo "passwordauthentication no"; echo "permitrootlogin no"
      else
        echo "passwordauthentication yes"; echo "permitrootlogin yes"
      fi;;
esac
exit 0
EOF
chmod 755 "$STUB"/*
mkdir -p /etc/ssh/sshd_config.d /root/.ssh
echo 'Include /etc/ssh/sshd_config.d/*.conf' > /etc/ssh/sshd_config
chmod 700 /root/.ssh
# A made-up public key line for the test. Not a real key.
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINOTAREALKEYJUSTFORTHESETUPTEST000000000000 test@example' > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

run() { : > "$CALLS"; rm -f /etc/ssh/sshd_config.d/00-swarm-hardening.conf; bash "$SETUP" "$@" > /tmp/out 2>&1; echo $?; }
changed() { grep -qE '^ufw (allow|delete|--force enable|default)' "$CALLS" || [[ -e /etc/ssh/sshd_config.d/00-swarm-hardening.conf ]]; }

echo "== 1. Lockdown on a root-only box is refused before anything changes"
touch /tmp/ts-joined
rc=$(echo yes | run --lockdown)
[[ "$rc" == 1 ]] || fail "lockdown on a root-only box exited $rc, expected 1"
grep -q 'No usable admin account' /tmp/out || fail "no clear refusal message"
for want in 'adduser NAME' 'usermod -aG sudo NAME' 'rsync --archive --chown=NAME:NAME /root/.ssh /home/NAME'; do
  grep -qF "$want" /tmp/out || fail "the refusal does not list: $want"
done
changed && fail "lockdown changed ufw or sshd on a root-only box"
echo "ok"

echo "== 2. --harden on a root-only box: firewall on, public SSH kept, root login left on"
rc=$(run --harden </dev/null)
[[ "$rc" == 0 ]] || fail "--harden exited $rc"
grep -q '^ufw allow 22/tcp comment' "$CALLS" || fail "public SSH was not kept open in ufw"
grep -q '^ufw --force enable' "$CALLS" || fail "ufw was not turned on"
[[ -e /etc/ssh/sshd_config.d/00-swarm-hardening.conf ]] && fail "root login was turned off with no admin user"
grep -q 'Root login over SSH is still ON' /tmp/out || fail "--harden did not say root login is still on"
echo "ok"

echo "== 3. --admin-user makes the user, copies root's keys, adds sudo; refuses while it has no password"
rc=$(echo yes | run --lockdown --admin-user alice)
[[ "$rc" == 1 ]] || fail "lockdown with a password-less new admin exited $rc, expected 1"
id alice >/dev/null 2>&1 || fail "alice was not made"
id -nG alice | tr ' ' '\n' | grep -qx sudo || fail "alice is not in the sudo group"
cmp -s /root/.ssh/authorized_keys /home/alice/.ssh/authorized_keys || fail "root's keys were not copied"
[[ "$(stat -c '%a %U' /home/alice/.ssh/authorized_keys)" == "600 alice" ]] || fail "alice's authorized_keys has the wrong owner or mode"
[[ "$(stat -c '%a %U' /home/alice/.ssh)" == "700 alice" ]] || fail "alice's .ssh has the wrong owner or mode"
grep -q "has no password" /tmp/out || fail "the missing password was not reported"
changed && fail "lockdown changed ufw or sshd while the admin was not ready"
echo "ok"

echo "== 4. Lockdown with a ready admin but Tailscale not joined is refused"
echo 'alice:not-a-real-password-test-only' | chpasswd
rm -f /tmp/ts-joined
rc=$(echo yes | run --lockdown --admin-user alice)
[[ "$rc" == 1 ]] || fail "lockdown without Tailscale exited $rc"
grep -q 'tailscale up --ssh' /tmp/out || fail "no join hint"
changed && fail "lockdown changed ufw or sshd without Tailscale"
echo "ok"

echo "== 5. Answering no stops lockdown with nothing changed"
touch /tmp/ts-joined
rc=$(echo no | run --lockdown --admin-user alice)
[[ "$rc" == 1 ]] || fail "lockdown after 'no' exited $rc"
changed && fail "lockdown changed something after 'no'"
echo "ok"

echo "== 6. Lockdown with a ready admin and Tailscale joined (found without --admin-user)"
rc=$(echo yes | run --lockdown)
[[ "$rc" == 0 ]] || fail "lockdown exited $rc"
grep -q 'Admin account: alice' /tmp/out || fail "alice was not found as the admin"
grep -q '"users": \["alice"\]' /tmp/out || fail "the ACL hint does not name alice"
grep -q '^PermitRootLogin no' /etc/ssh/sshd_config.d/00-swarm-hardening.conf || fail "root login was not turned off"
grep -q '^ufw allow in on tailscale0 to any port 22 proto tcp' "$CALLS" || fail "no SSH rule on tailscale0"
grep -q '^ufw delete allow 22/tcp' "$CALLS" || fail "public SSH rule was not removed"
echo "ok"

echo "== 7. --harden with a ready admin who ran it through sudo: root login off, public SSH still open"
rc=$(SUDO_USER=alice run --harden </dev/null)
[[ "$rc" == 0 ]] || fail "--harden exited $rc"
grep -q '^PermitRootLogin no' /etc/ssh/sshd_config.d/00-swarm-hardening.conf 2>/dev/null || fail "root login was not turned off"
grep -q '^ufw allow 22/tcp comment' "$CALLS" || fail "public SSH was not kept open"
grep -q 'Log in as alice' /tmp/out || fail "no 'log in as alice' message"
echo "ok"

echo "== 8. --harden with a ready admin but no proof of login (no sudo caller, answer no): root login left on"
rc=$(echo no | run --harden)
[[ "$rc" == 0 ]] || fail "--harden exited $rc"
[[ -e /etc/ssh/sshd_config.d/00-swarm-hardening.conf ]] && fail "root login was turned off without proof the admin can log in"
echo "ok"

echo "SETUP ROOT CHECK OK"
