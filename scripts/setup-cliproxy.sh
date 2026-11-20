#!/usr/bin/env bash
# setup-cliproxy.sh: OPTIONAL, ADVANCED. Install CLIProxyAPI on the worker with its web panel reachable only over Tailscale.
# Read docs/optional-services.md first, including the terms-of-service caution. Plain CLIs stay the default.
#
#   sudo bash scripts/setup-cliproxy.sh --management-key-file FILE [--version X.Y.Z] [--serve]
#
# --management-key-file  the panel's password, in a file (mode 600, from the secret request form). It is copied into the
#                        proxy's config, where the proxy turns it into a hash when it starts. It is never printed. The
#                        file is deleted after it is read unless you pass --keep-key-file.
# --version              CLIProxyAPI release to install (default below). The download is checked against the release's
#                        published checksums.txt before anything is installed.
# --serve                also publish the panel over Tailscale: tailscale serve --bg --https=8444 http://127.0.0.1:8318
#
# What it sets up:
#   user cliproxy (no login, no sudo); binary /opt/cliproxyapi/cli-proxy-api; config and logins /var/lib/cliproxyapi
#   cliproxyapi.service   the proxy, bound to 127.0.0.1:8317 only (config: server.host 127.0.0.1)
#   cliproxy-gate.service the gate on 127.0.0.1:8318: forwards the panel and its management API, 404 for everything else
# It does NOT start or restart anything that is already running (never restart the proxy while tasks run: see the docs).
# It does NOT point Claude Code or Codex at the proxy; that is a separate, deliberate step in the docs.
set -euo pipefail

VERSION_DEFAULT=8.0.79
VERSION="$VERSION_DEFAULT"
KEY_FILE=""; SERVE=0; KEEP=0
PREFIX="${CLIPROXY_PREFIX:-}"       # tests install under a temp folder
HOME_DIR="$PREFIX/var/lib/cliproxyapi"
BIN_DIR="$PREFIX/opt/cliproxyapi"
CONF="$HOME_DIR/config.yaml"

die() { printf 'setup-cliproxy: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

check_key() {  # check_key KEYFILE : stops unless the key is plain characters only (so it cannot break the config)
  local key
  [[ "$(grep -c "" "$1")" -le 1 ]] || die "the management key file must hold one line"
  key=$(tr -d '\r\n' < "$1")
  [[ "$key" =~ ^[A-Za-z0-9._~+=-]{16,200}$ ]] || die "the management key must be 16 to 200 plain characters (letters, numbers and . _ ~ + = -)"
}

# render_config KEYFILE : the proxy's config on stdout. The management key is written once, as a quoted string.
render_config() {
  local key client
  key=$(tr -d '\r\n' < "$1")
  client=$(head -c 24 /dev/urandom | base64 | tr -d '/+=\n')
  cat <<YAML
config-version: 8
server:
  host: "127.0.0.1"
  port: 8317
management:
  allow-remote: false
  secret-key: "$key"
access:
  api-keys:
    - "$client"
oauth:
  auth-dir: "$HOME_DIR/auth"
YAML
}

install_binary() {
  local arch tmp base asset
  case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=aarch64;; *) die "unsupported CPU $(uname -m)";; esac
  asset="CLIProxyAPI_${VERSION}_linux_${arch}.tar.gz"
  base="https://github.com/router-for-me/CLIProxyAPI/releases/download/v$VERSION"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/$asset" "$base/$asset" || die "could not download $asset"
  curl -fsSL -o "$tmp/checksums.txt" "$base/checksums.txt" || die "could not download checksums.txt"
  (cd "$tmp" && grep " $asset\$" checksums.txt | sha256sum -c - >/dev/null) || die "the download does not match its published checksum; nothing installed"
  mkdir "$tmp/x" && tar -xzf "$tmp/$asset" -C "$tmp/x"
  install -d -m 755 "$BIN_DIR"
  install -m 755 "$tmp/x/cli-proxy-api" "$BIN_DIR/cli-proxy-api.new" && mv "$BIN_DIR/cli-proxy-api.new" "$BIN_DIR/cli-proxy-api"
  rm -rf "$tmp"
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --management-key-file) KEY_FILE="${2:?missing file}"; shift 2;;
      --version) VERSION="${2:?missing version}"; shift 2;;
      --serve) SERVE=1; shift;;
      --keep-key-file) KEEP=1; shift;;
      -h|--help) sed -n '2,20p' "$0"; exit 0;;
      *) die "unknown option: $1";;
    esac
  done
  [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--version looks like 8.0.79"
  [[ -n "$KEY_FILE" && -r "$KEY_FILE" ]] || die "--management-key-file FILE is required (never put the key on the command line)"
  [[ -n "$PREFIX" || $EUID -eq 0 ]] || die "run as root (sudo)"
  if [[ -z "$PREFIX" && $(stat -c %a "$KEY_FILE") != 600 ]]; then die "$KEY_FILE must be mode 600"; fi
  if [[ -z "$PREFIX" ]]; then id cliproxy >/dev/null 2>&1 || useradd --system --home-dir /var/lib/cliproxyapi --shell /usr/sbin/nologin cliproxy; fi
  check_key "$KEY_FILE"
  say "Config (the proxy listens on 127.0.0.1 only)"
  install -d -m 700 "$HOME_DIR" "$HOME_DIR/auth"
  if [[ -e "$CONF" ]]; then
    echo "    $CONF already exists: keeping it. Delete it to start over (this would drop the panel key and client key)."
  else
    (umask 077; render_config "$KEY_FILE" > "$CONF.new") && mv "$CONF.new" "$CONF"
  fi
  [[ -n "$PREFIX" ]] || chown -R cliproxy:cliproxy "$HOME_DIR"
  [[ $KEEP -eq 1 ]] || shred -u "$KEY_FILE" 2>/dev/null || rm -f "$KEY_FILE"
  if [[ -z "$PREFIX" ]]; then
    say "CLIProxyAPI $VERSION"
    install_binary
    install -m 644 "$(dirname "$0")/../server/optional/cliproxyapi.service" /etc/systemd/system/
    install -m 644 "$(dirname "$0")/../server/optional/cliproxy-gate.service" /etc/systemd/system/
    install -d -m 755 /opt/cliproxyapi/gate
    install -m 755 "$(dirname "$0")/../server/optional/cliproxy-gate.py" /opt/cliproxyapi/gate/cliproxy-gate.py
    systemctl daemon-reload
    systemctl enable cliproxyapi.service cliproxy-gate.service >/dev/null 2>&1
    # A running proxy is never restarted here: that would cut live model streams.
    systemctl is-active --quiet cliproxyapi && echo "    cliproxyapi is already running: not restarted. See docs/optional-services.md to restart it safely." || systemctl start cliproxyapi
    systemctl is-active --quiet cliproxy-gate || systemctl start cliproxy-gate
    if (( SERVE )); then
      command -v tailscale >/dev/null || die "Tailscale is not installed"
      tailscale serve --bg --https=8444 http://127.0.0.1:8318
      echo "    Panel (tailnet only): https://THIS-MACHINE.YOUR-TAILNET.ts.net:8444/management.html"
    fi
  fi
  cat <<MSG

Done. The panel is at /management.html on port 8318 of this machine, and over Tailscale only if you used --serve.
Sign in with the management key you gave. To add accounts: docs/optional-services.md ("Add accounts in the panel").
MSG
}

if [[ "${CLIPROXY_LIB:-0}" != 1 ]]; then main "$@"; fi
