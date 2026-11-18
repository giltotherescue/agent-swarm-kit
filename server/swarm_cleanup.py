#!/usr/bin/env python3
"""swarm-cleanup: daily cleanup of finished task folders. Plain code, no model, no tokens spent.

It runs in two halves so neither user gets more than it needs:
  plan (as swarm-runner, the default): lists the agent's task folders through agent-exec, asks the backend
        for their states with the worker token (states only, no content), writes them to a states file,
        then runs the act half as the agent user through agent-exec.
  act  (as agent, `--act STATES_FILE`): does the git work and the deletes. The runner never runs git in the
        agent's folders.

Rules:
  - queued, running and waiting tasks are never touched. Folders with no task record are left alone,
    except in ~/work/scratch after SWARM_SCRATCH_DAYS (default 14) with no changes.
  - a repo that a live git worktree points into is never deleted.
  - finished tasks: bulky folders that git ignores (node_modules, vendor, .venv, build caches) go after
    SWARM_SLIM_AFTER_DAYS (default 1); the whole folder goes after SWARM_KEEP_DAYS (default 7).
  - before a folder is deleted, everything that cannot be rebuilt is saved to ~/backups/work/<date>/:
    a git bundle of commits not on the remote (made from the repo's common git dir), and a tar.gz of the
    working files (tracked changes, untracked and binary files, ignored files outside bulky folders).
    Both are checked (git bundle verify, tar -t) before anything is deleted. If a check fails, the folder stays.
    Backups are kept SWARM_BACKUP_KEEP_DAYS (default 30).
  - anything changed in the last 48 hours is skipped.
Run with --dry-run to see what it would do.
"""
import json
import os
import pwd
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
WORK = os.path.expanduser(os.environ.get("SWARM_WORK_DIR", "~/work"))
BACKUPS = os.path.expanduser(os.environ.get("SWARM_BACKUP_DIR", "~/backups/work"))
URL = os.environ.get("SWARM_URL", "http://127.0.0.1:8750").rstrip("/")
KEEP = float(os.environ.get("SWARM_KEEP_DAYS", "7"))
SLIM = float(os.environ.get("SWARM_SLIM_AFTER_DAYS", "1"))
SCRATCH = float(os.environ.get("SWARM_SCRATCH_DAYS", "14"))
BACKUP_KEEP = float(os.environ.get("SWARM_BACKUP_KEEP_DAYS", "30"))
BULKY = ("node_modules", "vendor", ".venv", "venv", ".next", "dist", "build", "target", ".cache", ".turbo")
SKIP = ("tmp", ".swarm-pids")
DAY = 86400
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


# ---------------- act half (agent user) ----------------
def git(path, *args):
    return subprocess.run(GIT + ["-C", path] + list(args), capture_output=True, text=True)


def last_change(path):
    newest = os.lstat(path).st_mtime
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in BULKY and d != ".git"]
        for f in files:
            try:
                newest = max(newest, os.lstat(os.path.join(root, f)).st_mtime)
            except OSError:
                pass
    return newest


def gitdir_target(path):
    """For a worktree folder (.git is a file), return the main repo's working folder."""
    gf = os.path.join(path, ".git")
    if not os.path.isfile(gf):
        return None
    try:
        with open(gf) as fh:
            line = fh.read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gd = os.path.normpath(os.path.join(path, line.split(":", 1)[1].strip()))
    # .../repo/.git/worktrees/NAME -> .../repo
    parts = gd.split(os.sep)
    if ".git" in parts:
        return os.sep.join(parts[:parts.index(".git")]) or os.sep
    return None


def protected_repos(root):
    """Repos that a live worktree points into. Deleting one would break the worktree."""
    out = set()
    candidates = []
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if os.path.isdir(p) and not os.path.islink(p):
            candidates.append(p)
            if name == "scratch":
                candidates += [os.path.join(p, s) for s in os.listdir(p) if os.path.isdir(os.path.join(p, s))]
    for p in candidates:
        target = gitdir_target(p)
        if target and os.path.isdir(p):
            out.add(os.path.realpath(target))
        if os.path.isdir(os.path.join(p, ".git")):
            r = git(p, "worktree", "list", "--porcelain")
            others = [ln[9:] for ln in r.stdout.splitlines() if ln.startswith("worktree ")]
            if any(os.path.realpath(o) != os.path.realpath(p) and os.path.isdir(o) for o in others):
                out.add(os.path.realpath(p))
    return out


def archive_members(path, is_git):
    """Paths (relative) to put in the tar: everything except .git and bulky folders git ignores."""
    keep = []
    for root, dirs, files in os.walk(path):
        rel_root = os.path.relpath(root, path)
        for d in list(dirs):
            if d == ".git" or (d in BULKY and (not is_git or git(path, "check-ignore", "-q", os.path.join(root, d)).returncode == 0)):
                dirs.remove(d)
        for f in files:
            if rel_root == "." and f == ".git":
                continue
            keep.append(os.path.normpath(os.path.join(rel_root, f)))
        for d in list(dirs):
            full = os.path.join(root, d)
            if os.path.islink(full):
                keep.append(os.path.normpath(os.path.join(rel_root, d)))
                dirs.remove(d)
    return keep


def backup(path, name, dry):
    """Save everything that cannot be rebuilt. Returns True only when it is safe to delete."""
    is_git = os.path.exists(os.path.join(path, ".git"))
    dest = os.path.join(BACKUPS, time.strftime("%Y-%m-%d"))
    bundle = os.path.join(dest, name + ".bundle")
    tarball = os.path.join(dest, name + ".files.tar.gz")
    unpushed = ""
    if is_git:
        r = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
        if r.returncode != 0 and git(path, "rev-parse", "HEAD").returncode == 0:
            print(f"KEEP {path}: cannot read git history")
            return False
        unpushed = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() not in ("", "0") else ""
        status = git(path, "status", "--porcelain", "--ignored")
        if status.returncode != 0:
            print(f"KEEP {path}: git status failed")
            return False
        if not unpushed and not status.stdout.strip():
            return True  # everything is on the remote and nothing local differs
    members = archive_members(path, is_git)
    if dry:
        print(f"would back up {name} to {dest} ({len(members)} files{', unpushed commits' if unpushed else ''})")
        return True
    os.makedirs(dest, mode=0o700, exist_ok=True)
    if unpushed:
        if git(path, "bundle", "create", "--quiet", bundle, "HEAD", "--not", "--remotes").returncode != 0 or \
                git(path, "bundle", "verify", "--quiet", bundle).returncode != 0:
            print(f"KEEP {path}: git bundle failed")
            return False
    if members:
        tmp = tarball + ".part"
        try:
            with tarfile.open(tmp, "w:gz") as tf:
                for m in members:
                    tf.add(os.path.join(path, m), arcname=os.path.join(name, m), recursive=False)
            with tarfile.open(tmp, "r:gz") as tf:
                if len(tf.getmembers()) != len(members):
                    raise OSError("archive is incomplete")
            os.replace(tmp, tarball)
        except (OSError, tarfile.TarError) as e:
            print(f"KEEP {path}: archive failed ({e})")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
    return True


def remove(path, why, dry):
    print(("would delete " if dry else "deleting ") + f"{path} ({why})")
    if not dry:
        shutil.rmtree(path, ignore_errors=True)


def act(states, dry):
    t0 = time.time()
    if not os.path.isdir(WORK):
        return
    protected = protected_repos(WORK)
    for name in sorted(os.listdir(WORK)):
        path = os.path.join(WORK, name)
        if not os.path.isdir(path) or os.path.islink(path) or name in SKIP:
            continue
        if name == "scratch":
            for s in sorted(os.listdir(path)):
                sp = os.path.join(path, s)
                if not os.path.isdir(sp) or os.path.islink(sp):
                    continue
                if os.path.realpath(sp) in protected:
                    print(f"skip {sp}: a live git worktree points into it")
                    continue
                if t0 - last_change(sp) > SCRATCH * DAY and backup(sp, "scratch-" + s, dry):
                    remove(sp, f"scratch folder unchanged for {SCRATCH:g} days", dry)
            continue
        task = states.get(name)
        if not task or task["state"] not in ("done", "failed", "cancelled"):
            continue
        if os.path.realpath(path) in protected:
            print(f"skip {name}: a live git worktree points into it")
            continue
        if t0 - last_change(path) < 2 * DAY:
            continue
        age = t0 - (task.get("finished_at") or task.get("updated_at") or t0)
        if age > KEEP * DAY:
            if backup(path, name, dry):
                remove(path, f"task {task['state']} {age / DAY:.0f} days ago", dry)
        elif age > SLIM * DAY:
            is_git = os.path.exists(os.path.join(path, ".git"))
            for root, dirs, _ in os.walk(path):
                dirs[:] = [d for d in dirs if d != ".git"]
                for d in list(dirs):
                    full = os.path.join(root, d)
                    if d in BULKY and is_git and git(path, "check-ignore", "-q", full).returncode == 0:
                        remove(full, "bulky folder of a finished task", dry)  # only what git ignores, never tracked files
                        dirs.remove(d)
    if os.path.isdir(BACKUPS):
        for d in os.listdir(BACKUPS):
            p = os.path.join(BACKUPS, d)
            if os.path.isdir(p) and t0 - os.path.getmtime(p) > BACKUP_KEEP * DAY:
                remove(p, "old backup", dry)


def list_folders():
    if not os.path.isdir(WORK):
        return []
    return sorted(n for n in os.listdir(WORK) if os.path.isdir(os.path.join(WORK, n)) and n not in SKIP and n != "scratch")


# ---------------- plan half (swarm-runner) ----------------
def worker_token():
    path = os.path.expanduser(os.environ.get("SWARM_WORKER_TOKEN_FILE", "/var/lib/swarm-runner/worker.token"))
    return open(path).read().strip()


def task_states(ids):
    out = {}
    for i in range(0, len(ids), 200):
        q = urllib.request.quote(",".join(ids[i:i + 200]), safe=",")
        req = urllib.request.Request(f"{URL}/worker/task-states?ids={q}",
                                     headers={"Authorization": "Bearer " + worker_token(), "User-Agent": "swarm-cleanup"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out.update(json.loads(r.read())["states"])
    return out


def agent_cmd():
    sys.path.insert(0, HERE)
    import swarm_worker  # noqa: E402  (same settings for user, folders and agent-exec)
    return swarm_worker.as_agent(), swarm_worker.HANDOFF


def plan(dry):
    prefix, handoff = agent_cmd()
    me = os.path.realpath(__file__)
    env_keys = ("SWARM_KEEP_DAYS", "SWARM_SLIM_AFTER_DAYS", "SWARM_SCRATCH_DAYS", "SWARM_BACKUP_KEEP_DAYS")
    settings = [f"{k}={os.environ[k]}" for k in env_keys if os.environ.get(k)]
    r = subprocess.run(prefix + ["exec", ".", "--", "env"] + settings + ["python3", me, "--list"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"swarm-cleanup: cannot list task folders: {r.stderr.strip()[-300:]}")
    names = json.loads(r.stdout or "[]")
    try:
        states = task_states(names) if names else {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit(f"swarm-cleanup: backend not reachable ({e}); nothing deleted")
    os.makedirs(os.path.join(handoff, "inbox"), exist_ok=True)
    sf = os.path.join(handoff, "inbox", "cleanup-states.json")
    with open(sf, "w") as fh:
        json.dump(states, fh)
    os.chmod(sf, 0o640)
    r = subprocess.run(prefix + ["exec", ".", "--", "env"] + settings + ["python3", me, "--act", sf] + (["--dry-run"] if dry else []))
    sys.exit(r.returncode)


def main():
    dry = "--dry-run" in sys.argv
    if "--list" in sys.argv:
        print(json.dumps(list_folders()))
    elif "--act" in sys.argv:
        sf = sys.argv[sys.argv.index("--act") + 1]
        act(json.load(open(sf)), dry)
    else:
        if os.getuid() == 0:
            sys.exit("swarm-cleanup: run as swarm-runner, not root")
        if pwd.getpwuid(os.getuid()).pw_name == os.environ.get("SWARM_AGENT_USER") and os.environ.get("SWARM_AGENT_USER"):
            sys.exit("swarm-cleanup: the plan half runs as swarm-runner (it needs the worker token)")
        plan(dry)


if __name__ == "__main__":
    main()
