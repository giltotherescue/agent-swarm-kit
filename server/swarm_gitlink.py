"""Auto-commits never stage submodule gitlinks (agent-control PR 63, ported).

Every place the kit commits leftover work for someone (today: the project-log export folder, and any future
leftover-work commit) goes through commit_leftover(). After `git add -A` it unstages every staged entry whose
index mode is 160000 on either side (added, modified, deleted), found from `git diff --cached --raw -z`, so it
does not depend on .gitmodules. Opt in for an intentional pin bump with allow_gitlink=True (task field
allow_gitlink, `swarmctl enqueue --allow-gitlink`) or AGENT_ALLOW_GITLINK=1 in the environment.
Standard library only.
"""
import os
import subprocess

GITLINK_MODE = "160000"
MAX_ITEMS = 10           # entries carried in a notice; the count always covers all of them
MAX_STORED = 200
REDACTED = "[redacted]"


def gitlink_allowed(task=None, env=None):
    env = os.environ if env is None else env
    if str(env.get("AGENT_ALLOW_GITLINK", "")).strip().lower() in ("1", "true", "yes"):
        return True
    v = (task or {}).get("allow_gitlink")
    return v is True or str(v).strip().lower() in ("1", "true", "yes")


def _git_raw(wd, args, check=True):
    """git with raw bytes and literal pathspecs. Fails closed on error."""
    r = subprocess.run(["git", *args], cwd=str(wd), capture_output=True, env=dict(os.environ, GIT_LITERAL_PATHSPECS="1"))
    if check and r.returncode:
        raise RuntimeError("git " + " ".join(a for a in args[:3] if isinstance(a, str)) + " failed: "
                           + r.stderr.decode("utf-8", "replace").strip()[-300:])
    return r


def _display_path(raw):
    text = raw.decode("utf-8", "backslashreplace")
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)


def staged_gitlinks(wd):
    """Staged gitlink changes from index modes: [{"path","old","new","raw"}], shas short, "" when absent.
    --ignore-submodules=none overrides diff.ignoreSubmodules / submodule.<name>.ignore. Raises on git errors."""
    r = _git_raw(wd, ["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", "--ignore-submodules=none"])
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
        om, nm, osha, nsha = f[:4]
        if GITLINK_MODE not in (om, nm):
            continue
        zero = lambda x: not x.strip("0")  # noqa: E731
        out.append({"path": _display_path(path), "raw": path,
                    "old": "" if om != GITLINK_MODE or zero(osha) else osha[:7],
                    "new": "" if nm != GITLINK_MODE or zero(nsha) else nsha[:7]})
    return out


def public_gitlink(g):
    return {k: g[k] for k in ("path", "old", "new")}


def format_note(items):
    return "submodule pin change(s) left uncommitted: " + ", ".join(
        f"{g['path']} ({g.get('old') or 'none'} -> {g.get('new') or 'none'})" for g in items)


def unstage_gitlinks(wd):
    """Unstage staged gitlink changes, leaving them in the worktree. Returns those unstaged."""
    items = staged_gitlinks(wd)
    if not items:
        return []
    has_head = _git_raw(wd, ["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0
    for g in items:
        _git_raw(wd, ["reset", "-q", "--", g["raw"]] if has_head else ["rm", "-q", "--cached", "-f", "--", g["raw"]])
    return items


def commit_leftover(wd, message, allow_gitlink=None, skipped=None, log=None, identity=("swarm-backend", "swarm-backend@localhost")):
    """`git add -A` then commit, never staging gitlinks unless allowed. Returns True if a commit was made.

    Skipped pins are appended to `skipped` ({"path","old","new"}) and reported through `log`. If git fails or a
    gitlink is still staged after unstaging, this raises instead of committing (fail closed)."""
    if allow_gitlink is None:
        allow_gitlink = gitlink_allowed()
    if not os.path.isdir(os.path.join(str(wd), ".git")):
        _git_raw(wd, ["init", "-q"])
    _git_raw(wd, ["add", "-A"])
    if not allow_gitlink:
        dropped = unstage_gitlinks(wd)
        if dropped:
            pub = [public_gitlink(g) for g in dropped]
            if skipped is not None:
                skipped.extend(pub)
            if log:
                log(f"{message}: " + format_note(pub))
        left = staged_gitlinks(wd)  # re-scan right before committing
        if left:
            raise RuntimeError("refusing to commit: gitlink(s) still staged after unstage: " + ", ".join(g["path"] for g in left))
    if not _git_raw(wd, ["diff", "--cached", "--name-only"]).stdout.strip():
        return False
    _git_raw(wd, ["-c", "user.name=" + identity[0], "-c", "user.email=" + identity[1], "commit", "-q", "-m", message])
    return True


def sanitize_gitlinks(items, secret_hit, limit=MAX_ITEMS):
    """Notice-safe copy of a gitlink_skipped list: {"count", "items"}.

    Each field is control-stripped, then checked with the backend's secret patterns BEFORE truncation (a value that
    looks like a secret is replaced whole by "[redacted]"), truncated, and checked again (a cut can leave a fragment).
    The count covers every entry, shown or not."""
    raw = [g for g in (items if isinstance(items, list) else []) if isinstance(g, dict)]

    def clip(v, n):
        s = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in str(v if v is not None else ""))
        if secret_hit(s):
            return REDACTED
        s = s[:n]
        return REDACTED if secret_hit(s) else s
    return {"count": len(raw),
            "items": [{"path": clip(g.get("path"), 200), "old": clip(g.get("old"), 12), "new": clip(g.get("new"), 12)}
                      for g in raw[:limit]]}


def warning_line(items, count):
    if not items:
        return ""
    lst = ", ".join(f"{g['path']} ({g.get('old') or 'none'} -> {g.get('new') or 'none'})" for g in items)
    more = f", +{count - len(items)} more" if count > len(items) else ""
    return (f"WARNING: {count} submodule pin change(s) were NOT committed by the auto-commit: {lst}{more}. "
            "Re-queue with allow_gitlink to commit a pin bump.")[:900]
