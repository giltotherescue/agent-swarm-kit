#!/usr/bin/env bash
# check-secrets: look for things that should never be committed (tokens, keys, private hostnames).
# Usage: scripts/check-secrets.sh [path]   Exit 1 if anything suspicious is found.
# Add your own names (bot ids, hostnames, project names) one per line to .secret-patterns to check those too.
set -uo pipefail
ROOT="${1:-.}"
patterns=(
  'gh[pousr]_[A-Za-z0-9]{20,}'           # GitHub tokens
  'github_pat_[A-Za-z0-9_]{20,}'
  'sk-[A-Za-z0-9_-]{20,}'                 # OpenAI and Anthropic style keys
  'xox[abprs]-[A-Za-z0-9-]{10,}'          # Slack
  'AKIA[0-9A-Z]{16}'                      # AWS access key id
  'AIza[0-9A-Za-z_-]{30,}'                # Google API key
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'tskey-[A-Za-z0-9-]{10,}'               # Tailscale auth key
  'swb_[A-Za-z0-9_-]{20,}'                # swarm bot tokens
  '[a-z0-9-]+\.tail[a-z0-9]+\.ts\.net'    # real tailnet hostnames
)
if [[ -f "$ROOT/.secret-patterns" ]]; then
  while IFS= read -r line; do [[ -n "$line" && "$line" != \#* ]] && patterns+=("$line"); done < "$ROOT/.secret-patterns"
fi
found=0
for p in "${patterns[@]}"; do
  if grep -rInE --exclude-dir=.git --exclude=.secret-patterns --exclude=check-secrets.sh -e "$p" "$ROOT"; then found=1; fi
done
if (( found )); then echo "check-secrets: possible secrets found above" >&2; exit 1; fi
echo "check-secrets: clean"
