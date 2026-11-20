#!/usr/bin/env bash
# setup-secrets.sh: the encrypted-secrets setup from docs/secrets.md, as two steps a bot can run on the worker.
#
#   sudo bash scripts/setup-secrets.sh --prepare
#       Installs SOPS and age, makes this worker's own age key and a read-only Git deploy key for the agent user,
#       and writes only the PUBLIC halves to /var/lib/agent-swarm/firstboot/ for the owner's bot to read:
#         worker-age-public-key.txt    add it as a recipient in the secrets repo's .sops.yaml (docs/secrets.md)
#         worker-deploy-key.pub.txt    add it to the secrets repo as a deploy key with write access OFF
#       Safe to run again: it keeps keys that exist. Private keys never leave the agent user's folder.
#   sudo bash scripts/setup-secrets.sh --link SECRETS_REPO_URL [--bundles tools,other]
#       After the recipient and the deploy key are in place: clones the secrets repo for the agent user, writes
#       the runtime bundle names, syncs, and checks the names (never the values) with with-secrets --check.
#
# Environment for tests: SECRETS_AGENT_HOME, SECRETS_PUBLIC_DIR, SECRETS_SKIP_INSTALL=1.
set -euo pipefail

AGENT_USER="${SECRETS_AGENT_USER:-agent}"
AGENT_HOME="${SECRETS_AGENT_HOME:-/home/$AGENT_USER}"
PUBLIC_DIR="${SECRETS_PUBLIC_DIR:-/var/lib/agent-swarm/firstboot}"
SOPS_VERSION="${SOPS_VERSION:-3.10.2}"
CONF="$AGENT_HOME/.config/swarm-secrets"

die() { printf 'setup-secrets: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

# as_agent CMD...: run a command as the agent user (or directly when tests say so)
as_agent() {
  if [[ -n "${SECRETS_AS_SELF:-}" ]]; then "$@"; else sudo -u "$AGENT_USER" -H "$@"; fi
}

install_tools() {
  [[ -n "${SECRETS_SKIP_INSTALL:-}" ]] && return 0
  if ! command -v age-keygen >/dev/null 2>&1; then
    say "Installing age"
    apt-get -o DPkg::Lock::Timeout=120 -qq install -y age >/dev/null || die "could not install age"
  fi
  if ! command -v sops >/dev/null 2>&1; then
    say "Installing SOPS $SOPS_VERSION (checked against the release's published checksums)"
    local arch tmp base
    case "$(dpkg --print-architecture)" in amd64) arch=amd64;; arm64) arch=arm64;; *) die "unsupported CPU for the SOPS download";; esac
    tmp=$(mktemp -d); base="https://github.com/getsops/sops/releases/download/v$SOPS_VERSION"
    curl -fsSL -o "$tmp/sops" "$base/sops-v$SOPS_VERSION.linux.$arch" || die "could not download SOPS"
    curl -fsSL -o "$tmp/sums" "$base/sops-v$SOPS_VERSION.checksums.txt" || die "could not download the SOPS checksums"
    (cd "$tmp" && grep " sops-v$SOPS_VERSION.linux.$arch\$" sums | sed "s/ sops-v.*/  sops/" | sha256sum -c - >/dev/null) \
      || die "the SOPS download does not match its published checksum; nothing installed"
    install -m 755 "$tmp/sops" /usr/local/bin/sops
    rm -rf "$tmp"
  fi
}

prepare() {
  install_tools
  say "Making this worker's keys (private halves stay under $CONF)"
  install -d -m 755 "$PUBLIC_DIR"
  as_agent bash -c '
    set -euo pipefail
    umask 077
    conf="$1"
    install -d -m 700 "$conf"
    [ -f "$conf/age.key" ] || age-keygen -o "$conf/age.key" >/dev/null 2>&1
    [ -f "$conf/deploy_key" ] || ssh-keygen -q -t ed25519 -N "" -C "swarm-worker-secrets-readonly" -f "$conf/deploy_key"
    chmod 600 "$conf/age.key" "$conf/deploy_key"
  ' _ "$CONF"
  as_agent age-keygen -y "$CONF/age.key" > "$PUBLIC_DIR/worker-age-public-key.txt"
  cp "$CONF/deploy_key.pub" "$PUBLIC_DIR/worker-deploy-key.pub.txt"
  chmod 644 "$PUBLIC_DIR/worker-age-public-key.txt" "$PUBLIC_DIR/worker-deploy-key.pub.txt"
  echo "Public age key:   $PUBLIC_DIR/worker-age-public-key.txt"
  echo "Public deploy key: $PUBLIC_DIR/worker-deploy-key.pub.txt"
}

link() {
  local url="$1" bundles="$2"
  [[ -f "$CONF/age.key" ]] || die "no worker key yet; run --prepare first"
  [[ "$url" =~ ^(git@|ssh://|https://)[A-Za-z0-9._@:/~-]+$ ]] || die "that does not look like a Git address"
  local repo="$CONF/repo"
  if [[ ! -d "$repo/.git" ]]; then
    say "Cloning the secrets repo with the read-only deploy key"
    as_agent env GIT_SSH_COMMAND="ssh -i $CONF/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
      git clone --quiet "$url" "$repo" || die "clone failed. Is the deploy key added to the secrets repo?"
  fi
  # The worker service runs the sync without your login, so the repo itself carries the key to use.
  as_agent git -C "$repo" config core.sshCommand "ssh -i $CONF/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
  as_agent bash -c 'umask 077; printf "%s\n" "$@" > "$0/runtime-bundles"' "$CONF" ${bundles//,/ }
  say "Syncing"
  as_agent env PATH="/usr/local/bin:/usr/bin:/bin" swarm-sync-secrets || die "sync failed. Is this worker's age key listed as a recipient (sops updatekeys)?"
  as_agent env PATH="/usr/local/bin:/usr/bin:/bin" with-secrets --check ${bundles//,/ }
}

main() {
  case "${1:-}" in
    --prepare) prepare;;
    --link)
      [[ -n "${2:-}" ]] || die "--link needs the secrets repo address"
      bundles=tools
      if [[ "${3:-}" == --bundles ]]; then bundles="${4:?--bundles needs names}"; fi
      [[ "$bundles" =~ ^[A-Za-z0-9._,-]+$ ]] || die "bad bundle names"
      link "$2" "$bundles";;
    *) sed -n '2,16p' "$0"; exit 2;;
  esac
}

if [[ "${SECRETS_LIB:-0}" != 1 ]]; then main "$@"; fi
