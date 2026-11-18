#!/usr/bin/env python3
"""swarm-cleanup: daily cleanup of finished task folders. Plain code, no model, no tokens spent.

Rules:
  - queued, running and waiting tasks are never touched.
  - finished tasks (done, failed, cancelled): bulky folders (node_modules, vendor, .venv, build caches)
    go after SWARM_SLIM_AFTER_DAYS (default 1); the whole folder goes after SWARM_KEEP_DAYS (default 7).
  - before a folder is deleted, unpushed commits and uncommitted changes are saved to
    ~/backups/work/<date>/ (kept SWARM_BACKUP_KEEP_DAYS, default 30). If the backup fails, the folder stays.
  - folders that are not tasks (no record in the backend) are left alone unless they sit in ~/work/scratch
    and have not changed for SWARM_SCRATCH_DAYS (default 14).
  - anything changed in the last 48 hours is skipped.
Run with --dry-run to see what it would do.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

WORK = os.path.expanduser(os.environ.get("SWARM_WORK_DIR", "~/work"))
BACKUPS = os.path.expanduser(os.environ.get("SWARM_BACKUP_DIR", "~/backups/work"))
URL = os.environ.get("SWARM_URL", "http://127.0.0.1:8750").rstrip("/")
KEEP = float(os.environ.get("SWARM_KEEP_DAYS", "7"))
SLIM = float(os.environ.get("SWARM_SLIM_AFTER_DAYS", "1"))
SCRATCH = float(os.environ.get("SWARM_SCRATCH_DAYS", "14"))
BACKUP_KEEP = float(os.environ.get("SWARM_BACKUP_KEEP_DAYS", "30"))
BULKY = ("node_modules", "vendor", ".venv", "venv", ".next", "dist", "build", "target", ".cache", ".turbo")
DAY = 86400


def token():
    path = os.path.expanduser(os.environ.get("SWARM_WORKER_TOKEN_FILE", "~/.config/swarm/worker.token"))
    return open(path).read().strip()


def task_state(task_id):
    req = urllib.request.Request(f"{URL}/tasks/{task_id}", headers={"Authorization": "Bearer " + token(), "User-Agent": "swarm-cleanup"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["task"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def last_change(path):
    newest = os.path.getmtime(path)
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in BULKY and d != ".git"]
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return newest


def backup(path, name, dry):
    """Save unpushed commits and uncommitted changes. Returns True when it is safe to delete."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return True
    git = ["git", "-C", path]
    unpushed = subprocess.run(git + ["log", "--branches", "--not", "--remotes", "--oneline"], capture_output=True, text=True)
    dirty = subprocess.run(git + ["status", "--porcelain"], capture_output=True, text=True)
    if unpushed.returncode != 0 or dirty.returncode != 0:
        return False
    if not unpushed.stdout.strip() and not dirty.stdout.strip():
        return True
    dest = os.path.join(BACKUPS, time.strftime("%Y-%m-%d"))
    if dry:
        print(f"would back up {name} to {dest}")
        return True
    os.makedirs(dest, exist_ok=True)
    ok = True
    if unpushed.stdout.strip():
        ok = subprocess.run(git + ["bundle", "create", os.path.join(dest, name + ".bundle"), "--branches", "--not", "--remotes"],
                            capture_output=True).returncode == 0
    if dirty.stdout.strip():
        with open(os.path.join(dest, name + ".uncommitted.diff"), "w") as fh:
            ok = subprocess.run(git + ["diff", "HEAD"], stdout=fh).returncode == 0 and ok
    return ok


def remove(path, why, dry):
    print(("would delete " if dry else "deleting ") + f"{path} ({why})")
    if not dry:
        shutil.rmtree(path, ignore_errors=True)


def main():
    dry = "--dry-run" in sys.argv
    t0 = time.time()
    if not os.path.isdir(WORK):
        return
    for name in sorted(os.listdir(WORK)):
        path = os.path.join(WORK, name)
        if not os.path.isdir(path) or name in ("tmp",):
            continue
        if name == "scratch":
            for s in os.listdir(path):
                sp = os.path.join(path, s)
                if os.path.isdir(sp) and t0 - last_change(sp) > SCRATCH * DAY and backup(sp, "scratch-" + s, dry):
                    remove(sp, f"scratch folder unchanged for {SCRATCH:g} days", dry)
            continue
        try:
            task = task_state(name)
        except Exception as e:
            print(f"skip {name}: backend not reachable ({e})")
            continue
        if not task or task["state"] not in ("done", "failed", "cancelled"):
            continue
        if t0 - last_change(path) < 2 * DAY:
            continue
        age = t0 - (task.get("finished_at") or task["updated_at"])
        if age > KEEP * DAY:
            if backup(path, name, dry):
                remove(path, f"task {task['state']} {age / DAY:.0f} days ago", dry)
            else:
                print(f"KEEP {path}: backup of unpushed work failed")
        elif age > SLIM * DAY:
            for root, dirs, _ in os.walk(path):
                for d in list(dirs):
                    full = os.path.join(root, d)
                    ignored = subprocess.run(["git", "-C", path, "check-ignore", "-q", full], capture_output=True).returncode == 0
                    if d in BULKY and ignored:  # only remove what git ignores, never tracked files
                        remove(os.path.join(root, d), "bulky folder of a finished task", dry)
                        dirs.remove(d)
    if os.path.isdir(BACKUPS):
        for d in os.listdir(BACKUPS):
            p = os.path.join(BACKUPS, d)
            if os.path.isdir(p) and t0 - os.path.getmtime(p) > BACKUP_KEEP * DAY:
                remove(p, "old backup", dry)


if __name__ == "__main__":
    main()
