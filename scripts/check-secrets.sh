#!/usr/bin/env bash
# check-secrets: look for things that should never be committed (tokens, keys, private hostnames).
# Usage: scripts/check-secrets.sh [path]
# Exit 0 clean, 1 if anything suspicious is found, 2 if the scan itself failed (never reports "clean" then).
# It prints only file:line and which pattern matched, never the matching text, so it is safe in logs.
# Patterns live in config/secret-patterns.txt. Add your own names (bot ids, hostnames, project names)
# one per line to .secret-patterns at the scanned folder's root to check those too (that file is git-ignored).
# Limits: it finds known token shapes and your own names. It cannot find random-looking secrets with no prefix.
set -uo pipefail
ROOT="${1:-.}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATTERN_FILE="${SWARM_SECRET_PATTERNS_FILE:-$HERE/../config/secret-patterns.txt}"
[[ -d "$ROOT" ]] || { echo "check-secrets: $ROOT is not a folder" >&2; exit 2; }
[[ -r "$PATTERN_FILE" ]] || { echo "check-secrets: cannot read $PATTERN_FILE" >&2; exit 2; }

patterns=()
while IFS= read -r line; do
  line="${line%%  #*}"; line="${line%"${line##*[![:space:]]}"}"
  [[ -n "$line" && "$line" != \#* ]] && patterns+=("$line")
done < "$PATTERN_FILE"
if [[ -f "$ROOT/.secret-patterns" ]]; then
  while IFS= read -r line; do [[ -n "$line" && "$line" != \#* ]] && patterns+=("$line"); done < "$ROOT/.secret-patterns"
fi

found=0; failed=0; n=0
for p in "${patterns[@]}"; do
  n=$((n + 1))
  # Check the pattern itself first: a broken pattern must fail, not pass silently.
  grep -E -e "$p" /dev/null >/dev/null 2>&1; rc=$?
  if (( rc == 2 )); then echo "check-secrets: pattern $n is not a valid regular expression" >&2; failed=1; continue; fi
  out=$(grep -rInE --exclude-dir=.git --exclude=.secret-patterns --exclude=secret-patterns.txt -e "$p" "$ROOT" 2>/tmp/check-secrets.err.$$)
  rc=$?
  if (( rc == 0 )); then
    found=1
    # Only file:line, never the matched text.
    printf '%s\n' "$out" | cut -d: -f1,2 | sed "s/\$/  (pattern $n)/"
  elif (( rc == 2 )); then
    echo "check-secrets: grep failed for pattern $n: $(head -c 300 /tmp/check-secrets.err.$$)" >&2
    failed=1
  fi
  rm -f /tmp/check-secrets.err.$$
done
if (( failed )); then echo "check-secrets: the scan did not complete; fix the errors above" >&2; exit 2; fi
if (( found )); then echo "check-secrets: possible secrets found at the places above" >&2; exit 1; fi
echo "check-secrets: clean"
