"""Auto-commits never stage submodule gitlinks (agent-control PR 63, ported).

The stock kit worker never commits leftover work; it publishes only the agent's own commits. The one place the kit
commits for someone is the backend's project-log export folder, and it goes through commit_leftover(). That runs in a
separate index (the caller's index is untouched unless the commit succeeds): `git add -A`, then every staged entry whose
index mode is 160000 on either side (added, modified, deleted) is unstaged, found from `git diff --cached --raw -z`, so
.gitmodules is not needed. A gitlink replaced by a directory of files or by an ordinary file at the same path, any git
error, a timeout, or a gitlink still staged after unstaging aborts the commit (fails closed). The commit is built with
plumbing (write-tree, commit-tree, update-ref compare-and-swap), so repository hooks never run for it: the tree published
is exactly the tree inspected.

`allow_gitlink` (task field, `swarmctl enqueue --allow-gitlink`) and AGENT_ALLOW_GITLINK=1 are metadata for a caller
that auto-commits on a task's behalf; the stock export always passes allow_gitlink=False.

Every gitlink field leaving this module (logs, errors, finish requests, task records, notices, reports) goes through
redact() first, on the full value, and only then is cut. Standard library only.
"""
import os
import re
import shutil
import subprocess
import tempfile

GITLINK_MODE = "160000"
MAX_ITEMS = 10           # entries carried in a notice; the count always covers all of them
MAX_STORED = 200         # entries kept on the task record; the count is stored separately
GIT_TIMEOUT = 60         # seconds per git command, like the export's old limits
REDACTED = "[redacted]"
NOTE_MARK = "gitlink pin not committed"
# Hidden marker on the generated REPORT.md note; the backend detects the note by this, never by prose.
NOTE_TAG = "<!-- swarm:gitlink-skip-note -->"
NOTE_END = "<!-- /swarm:gitlink-skip-note -->"
_NOTE_LINE = r"> Note \(" + re.escape(NOTE_MARK) + r"\): the auto-commit [^\n]*\. Commit an intentional pin bump explicitly\."
# Only an identifiable generated block is ever removed: the start marker alone on its line, then lines holding neither
# marker, then the end marker alone on its line; or the exact legacy footer (start marker line + one generated note line,
# written before the end marker existed). A marker quoted inline or in backticks is prose, and nothing runs to EOF.
_NOTE_RE = re.compile(
    r"(?:\A|\n)\n*(?:"
    + re.escape(NOTE_TAG) + r"[ \t]*\n(?:(?![^\n]*(?:" + re.escape(NOTE_TAG) + "|" + re.escape(NOTE_END) + r"))[^\n]*\n)*?"
    + re.escape(NOTE_END) + r"[ \t]*"
    + r"|" + re.escape(NOTE_TAG) + r"[ \t]*\n" + _NOTE_LINE + r"[ \t]*"
    + r")(?=\n|\Z)")

# The same key=value rule as agent-control's redact() (src/orchestrators.js ASSIGNMENT_RE).
ASSIGNMENT_RE = re.compile(r"\b([A-Za-z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Za-z0-9_]*)\s*[=:]\s*(\"[^\"]*\"|'[^']*'|\S+)", re.I)
_patterns_cache = None


def load_patterns():
    """config/secret-patterns.txt (or SWARM_SECRET_PATTERNS_FILE): the list the runner and backend use."""
    global _patterns_cache
    if _patterns_cache is None:
        path = os.environ.get("SWARM_SECRET_PATTERNS_FILE") or os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "..", "config", "secret-patterns.txt")
        pats = []
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.split("  #", 1)[0].strip()
                    if line and not line.startswith("#"):
                        pats.append(re.compile(line))
        except (OSError, re.error):
            pass
        _patterns_cache = pats
    return _patterns_cache


def redact(text, patterns=None):
    """Replace secret-pattern matches with [redacted] and key=value secrets with KEY=[redacted]."""
    s = str(text if text is not None else "")
    for p in (load_patterns() if patterns is None else patterns):
        s = p.sub(REDACTED, s)
    return ASSIGNMENT_RE.sub(lambda m: m.group(1) + "=" + REDACTED, s)


def _hit(text, patterns):
    return any(p.search(text) for p in patterns) or bool(
        [m for m in ASSIGNMENT_RE.finditer(text) if not m.group(2).strip("\"'").startswith(REDACTED)])


def gitlink_allowed(task=None, env=None):
    env = os.environ if env is None else env
    if str(env.get("AGENT_ALLOW_GITLINK", "")).strip().lower() in ("1", "true", "yes"):
        return True
    v = (task or {}).get("allow_gitlink")
    return v is True or str(v).strip().lower() in ("1", "true", "yes")


def _git_raw(wd, args, check=True, index=None, input=None):
    """git with raw bytes, literal pathspecs and a timeout. Fails closed; error text is redacted."""
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    if index:
        env["GIT_INDEX_FILE"] = index
    shown = list(args)
    while shown[:1] == ["-c"]:
        shown = shown[2:]            # "-c key=value" pairs are not the command
    name = "git " + " ".join(a for a in shown[:3] if isinstance(a, str))
    try:
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=str(wd), capture_output=True, env=env, timeout=GIT_TIMEOUT, input=input)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{name} timed out after {GIT_TIMEOUT} seconds")
    if check and r.returncode:
        raise RuntimeError(name + " failed: " + bounded_diagnostic(r.stderr))
    return r


def bounded_diagnostic(raw, n=300):
    """Git stderr for an error message: control characters flattened, redacted on the FULL text, and only then cut to
    the last n characters, so a cut can never leave the tail of a secret whose key was cut off."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    text = "".join(" " if (ord(c) < 32 and c not in "\n\t") or ord(c) == 127 else c for c in text)
    r = redact(text.strip())
    return r if len(r) <= n else "..." + r[-n:]


def _display_path(raw):
    text = raw.decode("utf-8", "backslashreplace")
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)


def _staged(wd, index=None):
    """All staged entries: [(old_mode, new_mode, old_sha, new_sha, raw_path)]."""
    r = _git_raw(wd, ["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", "--ignore-submodules=none"], index=index)
    parts = r.stdout.split(b"\0")
    out, i = [], 0
    while i + 1 < len(parts):
        head, path = parts[i], parts[i + 1]
        i += 2
        if not head.startswith(b":"):
            continue
        f = head[1:].decode().split()
        if len(f) < 4:
            raise RuntimeError("unparseable git diff --raw entry")
        out.append((f[0], f[1], f[2], f[3], path))
    return out


def staged_gitlinks(wd, index=None):
    """Staged gitlink changes from index modes: [{"path","old","new","raw"}], shas short, "" when absent. Raw, unredacted:
    callers must pass the result through public_gitlinks() before it leaves this module."""
    zero = lambda x: not x.strip("0")  # noqa: E731
    return [{"path": _display_path(p), "raw": p,
             "old": "" if om != GITLINK_MODE or zero(osha) else osha[:7],
             "new": "" if nm != GITLINK_MODE or zero(nsha) else nsha[:7]}
            for om, nm, osha, nsha, p in _staged(wd, index) if GITLINK_MODE in (om, nm)]


def unstage_gitlinks(wd, index=None):
    """Unstage staged gitlink changes, leaving them in the worktree. Returns those unstaged (raw).

    Never touches any other staged path: a gitlink replaced by files (engine -> engine/x) cannot be unstaged without
    unstaging the files too, so that raises; and the set of other staged paths is checked unchanged afterwards."""
    entries = _staged(wd, index)
    items = staged_gitlinks(wd, index)
    if not items:
        return []
    links = {g["raw"] for g in items}
    for om, nm, _o, _n, p in entries:
        if p in links and {om, nm} - {GITLINK_MODE, "000000"}:
            # same path, gitlink on one side and an ordinary file/symlink on the other: resetting it would silently
            # unstage (or resurrect) the ordinary entry, so stop before touching the index
            raise RuntimeError("refusing to auto-commit: a submodule pin and an ordinary file share the path "
                               + sanitize_field(_display_path(p), 200) + "; commit it explicitly")
    others = sorted(p for om, nm, _o, _n, p in entries if p not in links)
    for g in items:
        if any(p.startswith(g["raw"] + b"/") for p in others):
            raise RuntimeError("refusing to auto-commit: a submodule pin was replaced by files at "
                               + sanitize_field(g["path"], 200) + "; commit it explicitly")
    has_head = _git_raw(wd, ["rev-parse", "--verify", "-q", "HEAD"], check=False, index=index).returncode == 0
    for g in items:
        _git_raw(wd, ["reset", "-q", "--", g["raw"]] if has_head else ["rm", "-q", "--cached", "-f", "--", g["raw"]], index=index)
    if sorted(p for *_x, p in _staged(wd, index) if p not in links) != others:
        raise RuntimeError("refusing to auto-commit: unstaging a submodule pin changed other staged paths")
    return items


def commit_leftover(wd, message, allow_gitlink=None, skipped=None, log=None, identity=("swarm-backend", "swarm-backend@localhost")):
    """`git add -A` then commit, never staging gitlinks unless allowed. Returns True if a commit was made.

    Works in a copy of the index; the caller's index is replaced only after a successful commit, so a failure leaves
    it exactly as it was. Skipped pins are appended to `skipped` already sanitized ({"path","old","new"}) and reported
    through `log` (sanitized). Raises RuntimeError (redacted text) instead of committing on any problem."""
    if allow_gitlink is None:
        allow_gitlink = gitlink_allowed()
    if not os.path.isdir(os.path.join(str(wd), ".git")):
        _git_raw(wd, ["init", "-q"])
    git_dir = _git_raw(wd, ["rev-parse", "--absolute-git-dir"]).stdout.decode().strip()
    real = os.path.join(git_dir, "index")
    # Expected HEAD, read ONCE before the index is copied or staged: it is both the commit parent and the update-ref
    # compare-and-swap value, so a concurrent ordinary commit makes update-ref fail instead of being reversed.
    old = _git_raw(wd, ["rev-parse", "--verify", "-q", "HEAD"], check=False).stdout.decode().strip()
    fd, tmp = tempfile.mkstemp(prefix="index.leftover.", dir=git_dir)
    os.close(fd)
    try:
        if os.path.exists(real):
            shutil.copy2(real, tmp)
        else:
            os.remove(tmp)
        _git_raw(wd, ["add", "-A"], index=tmp)
        if not allow_gitlink:
            dropped = unstage_gitlinks(wd, index=tmp)
            if dropped:
                pub = public_gitlinks(dropped, limit=MAX_STORED)
                if skipped is not None:
                    skipped.extend(pub["items"])
                if log:
                    log(f"{message}: " + format_note(pub["items"][:MAX_ITEMS], pub["count"]))
            left = staged_gitlinks(wd, index=tmp)  # re-scan right before committing
            if left:
                raise RuntimeError("refusing to commit: gitlink(s) still staged after unstage: "
                                   + ", ".join(sanitize_field(g["path"], 200) for g in left[:MAX_ITEMS]))
        if not _git_raw(wd, ["diff", "--cached", "--name-only"], index=tmp).stdout.strip():
            return False
        # Plumbing, not `git commit`: exporter commits intentionally run NO repository hooks. A pre-commit hook could
        # stage the very pin skipped above (`git add -u`), and a slow post-commit hook could time out after HEAD moved.
        # write-tree publishes exactly the inspected index; update-ref (every exporter git command runs with hooks disabled, see _git_raw) is a compare-and-swap on
        # HEAD, so on any failure HEAD and the real index are both unchanged.
        tree = _git_raw(wd, ["write-tree"], index=tmp).stdout.decode().strip()
        ident = ["-c", "user.name=" + identity[0], "-c", "user.email=" + identity[1]]
        msg = _git_raw(wd, ["stripspace"], input=message.encode()).stdout  # what `git commit -m` does to the message
        new = _git_raw(wd, [*ident, "commit-tree", tree, *(["-p", old] if old else [])], input=msg).stdout.decode().strip()
        subject = (msg.decode("utf-8", "replace").splitlines() or [""])[0]
        _git_raw(wd, ["update-ref", "-m",
                      ("commit: " if old else "commit (initial): ") + subject, "HEAD", new, old])
        os.replace(tmp, real)
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def sanitize_field(v, n, patterns=None):
    """One field, safe to show: control characters stripped, redacted on the FULL value, then cut to n. A value whose
    redacted form does not fit, or whose cut still looks secret, becomes [redacted] whole."""
    pats = load_patterns() if patterns is None else patterns
    s = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in str(v if v is not None else ""))
    r = redact(s, pats)
    if r != s and len(r) > n:
        return REDACTED
    r = r[:n]
    return REDACTED if _hit(r, pats) else r


def public_gitlinks(items, limit=MAX_ITEMS, patterns=None, count=None):
    """Sanitized copy of a gitlink list: {"count", "items"}. count is the true number of skipped pins (all entries, or
    `count` when the caller knows a larger true total); items holds at most `limit` sanitized entries."""
    raw = [g for g in (items if isinstance(items, list) else []) if isinstance(g, dict)]
    total = max(len(raw), count) if isinstance(count, int) and not isinstance(count, bool) else len(raw)
    return {"count": total,
            "items": [{"path": sanitize_field(g.get("path"), 200, patterns), "old": sanitize_field(g.get("old"), 12, patterns),
                       "new": sanitize_field(g.get("new"), 12, patterns)} for g in raw[:limit]]}


def sanitize_gitlinks(items, secret_hit=None, limit=MAX_ITEMS, patterns=None, count=None):
    """Backward-compatible name for public_gitlinks (secret_hit is ignored; patterns carry the rules)."""
    return public_gitlinks(items, limit=limit, patterns=patterns, count=count)


def format_note(items, count=None):
    """Note text from already sanitized items."""
    count = len(items) if count is None else count
    more = f", +{count - len(items)} more" if count > len(items) else ""
    return f"{count} submodule pin change(s) left uncommitted: " + ", ".join(
        f"{g['path']} ({g.get('old') or 'none'} -> {g.get('new') or 'none'})" for g in items) + more


def warning_line(items, count):
    if not items:
        return ""
    return ("WARNING: " + format_note(items, count).replace("left uncommitted", "were NOT committed by the auto-commit")
            + ". Commit an intentional pin bump explicitly.")[:900]


def report_note(items, count):
    return (f"\n\n{NOTE_TAG}\n> Note ({NOTE_MARK}): the auto-commit " + format_note(items, count)
            + f". Commit an intentional pin bump explicitly.\n{NOTE_END}\n")


def strip_report_note(text):
    """REPORT.md text with every generated skip note block removed (see _NOTE_RE); all other text is kept as is."""
    return _NOTE_RE.sub(lambda m: "\n" if m.group(0).startswith("\n") else "", text or "")
