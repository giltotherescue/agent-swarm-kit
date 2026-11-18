#!/usr/bin/env python3
"""Two-user handoff check. Run as root, in a throwaway sandbox (test_handoff_users.py starts it).

It sets up the inbox and outbox the way setup-worker.sh does (setgid folders, mode 2750, two users who are
NOT in each other's group), then:
  1. as the runner user: Runner.fill_inbox() and write_prompt() from server/swarm_worker.py
  2. as the agent user: agent-exec prepare (reads the inbox), then agent-exec collect (writes the outbox)
  3. as the runner user: Runner.upload() reads the report and artifacts from the outbox
This is the path that failed on a fresh install of 0.2.0 (defects 2 and 3 in the install test).

Exit 0: all good. Exit 1: a check failed. Exit 4: this sandbox does not behave like a real server (setgid
is not cleared by chmod from a non-member), so the result would mean nothing.
Usage: python3 -I -B handoff_users_check.py --kit DIR [--keep-users]
"""
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import traceback

AGENT, RUNNER = "swtest-agent", "swtest-runner"


def ensure_user(name):
    try:
        pwd.getpwnam(name)
        return False
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
                        "--shell", "/usr/sbin/nologin", name], check=True)
        return True


def as_user(name, fn, *args):
    """Run fn(*args) in a child process with the user's uid and gid only (no extra groups)."""
    pw = pwd.getpwnam(name)
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.setgroups([])
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            os.umask(0o027)   # what swarm-worker.service and agent-exec use
            fn(*args)
            code = 0
        except BaseException:
            traceback.print_exc()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def fail(msg):
    print(f"HANDOFF CHECK FAILED: {msg}", flush=True)
    sys.exit(1)


def main():
    argv = sys.argv[1:]
    kit = os.path.abspath(argv[argv.index("--kit") + 1]) if "--kit" in argv else os.path.join(os.path.dirname(__file__), "..")
    keep = "--keep-users" in argv
    if os.geteuid() != 0:
        print("handoff_users_check.py must run as root (in a sandbox)", file=sys.stderr)
        sys.exit(2)
    made = [u for u in (AGENT, RUNNER) if ensure_user(u)]
    base = tempfile.mkdtemp(prefix="swarm-handoff-")
    try:
        os.chmod(base, 0o755)
        a, r = pwd.getpwnam(AGENT), pwd.getpwnam(RUNNER)
        ag, rg = grp.getgrnam(AGENT).gr_gid, grp.getgrnam(RUNNER).gr_gid
        handoff, work = os.path.join(base, "handoff"), os.path.join(base, "work")
        inbox, outbox = os.path.join(handoff, "inbox"), os.path.join(handoff, "outbox")
        os.makedirs(handoff, 0o755)
        # Same owners and modes as setup-worker.sh.
        for d, uid, gid in ((inbox, r.pw_uid, ag), (outbox, a.pw_uid, rg)):
            os.mkdir(d)
            os.chown(d, uid, gid)
            os.chmod(d, 0o2750)
        os.mkdir(work)
        os.chown(work, a.pw_uid, a.pw_gid)
        os.chmod(work, 0o700)

        # Control: does this sandbox clear setgid when a non-member chmods? If not, it cannot catch the bug.
        probe = os.path.join(inbox, "probe")
        def chmod_probe():
            os.mkdir(probe, 0o750)
            os.chmod(probe, 0o2750)
        if not as_user(RUNNER, chmod_probe):
            fail("could not run the control probe as the runner user")
        if os.stat(probe).st_mode & 0o2000:
            print("SANDBOX DOES NOT CLEAR SETGID ON CHMOD BY A NON-MEMBER; the check would prove nothing", flush=True)
            sys.exit(4)
        shutil.rmtree(probe)

        os.environ.update(SWARM_HANDOFF_DIR=handoff, SWARM_WORK_DIR=work, SWARM_RUNNER_DIR=os.path.join(base, "runner"))
        sys.path.insert(0, os.path.join(kit, "server"))
        import swarm_worker as sw

        task_id = "t-handoff-1"
        task = {"id": task_id, "prompt": "Brief for the handoff check", "attachments": ["notes.txt"],
                "parent": {"report": "Earlier report"}}

        def runner_writes_inbox():
            run = sw.Runner.__new__(sw.Runner)
            run.task, run.prompt_n = task, 0
            run.inbox = os.path.join(handoff, "inbox", task_id)
            run.tapi = lambda *a_, **k_: b"attached file\n"
            run.fill_inbox()
            run.write_prompt("prompt text")
            with open(os.path.join(run.inbox, "messages.md"), "a") as fh:   # what beat() does
                fh.write("hello from the bot\n")

        if not as_user(RUNNER, runner_writes_inbox):
            fail("the runner could not write the task inbox (defect 2 area)")
        st = os.stat(os.path.join(inbox, task_id, "attachments", "notes.txt"))
        if st.st_gid != ag:
            fail(f"inbox file has group {grp.getgrgid(st.st_gid).gr_name}, expected {AGENT} (setgid was lost)")

        agent_exec = [sys.executable, "-I", "-B", os.path.join(kit, "server", "agent-exec"), "--work", work, "--handoff", handoff]

        def agent_prepares_and_collects():
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
            p = subprocess.run(agent_exec + ["prepare", task_id, task_id], capture_output=True, text=True, env=env)
            if p.returncode != 0:
                raise RuntimeError(f"agent-exec prepare failed: {p.stderr.strip()}")
            wd = os.path.join(work, task_id)
            ctx = os.path.join(wd, ".task-context")
            for f in ("brief.md", "attachments/notes.txt", "attachments/previous-REPORT.md"):
                if not os.path.isfile(os.path.join(ctx, f)):
                    raise RuntimeError(f"prepare did not copy {f}")
            if "hello from the bot" not in open(os.path.join(ctx, "messages.md")).read():
                raise RuntimeError("the agent cannot read messages.md written by the runner")
            open(os.path.join(wd, "REPORT.md"), "w").write("## Summary\nStatus: done\n")
            open(os.path.join(wd, "AGENT_STATUS"), "w").write("done\n")
            os.makedirs(os.path.join(wd, "artifacts", "sub"))
            open(os.path.join(wd, "artifacts", "sub", "result.txt"), "w").write("artifact body\n")
            c = subprocess.run(agent_exec + ["collect", task_id, task_id], capture_output=True, text=True, env=env)
            if c.returncode != 0:
                raise RuntimeError(f"agent-exec collect failed: {c.stderr.strip()}")

        if not as_user(AGENT, agent_prepares_and_collects):
            fail("the agent could not read the inbox or write the outbox")
        for rel in ("summary.json", "REPORT.md", "artifacts", "artifacts/sub__result.txt"):
            st = os.stat(os.path.join(outbox, task_id, rel))
            if st.st_gid != rg:
                fail(f"outbox/{rel} has group {grp.getgrgid(st.st_gid).gr_name}, expected {RUNNER} (defect 3 area)")

        rdir = os.path.join(base, "runner-result")   # somewhere the runner child may write its findings
        os.mkdir(rdir)
        os.chown(rdir, r.pw_uid, r.pw_gid)
        result = os.path.join(rdir, "result.json")

        def runner_reads_outbox():
            run = sw.Runner.__new__(sw.Runner)
            run.task, run.claim_id, run.warnings = task, "claim-1", []
            run.outbox = os.path.join(handoff, "outbox", task_id)
            got = []
            run.tapi = lambda method, path, raw=None, **k_: got.append((path, raw.decode()))
            summary = json.loads(sw.safe_read(run.outbox, "summary.json"))
            run.upload(summary)
            status = sw.safe_read(run.outbox, "AGENT_STATUS")
            with open(result, "w") as fh:
                json.dump({"uploads": got, "warnings": run.warnings, "status": status.decode() if status else None}, fh)

        if not as_user(RUNNER, runner_reads_outbox):
            fail("the runner could not read the outbox (defect 3)")
        res = json.load(open(result))
        paths = dict(res["uploads"])
        if res["warnings"]:
            fail(f"runner warnings: {res['warnings']}")
        if "Status: done" not in paths.get("/files/report/REPORT.md", ""):
            fail(f"REPORT.md did not reach the upload: {res['uploads']}")
        if paths.get("/files/artifact/sub__result.txt") != "artifact body\n":
            fail(f"the artifact did not reach the upload: {res['uploads']}")
        if (res["status"] or "").strip() != "done":
            fail("the runner could not read AGENT_STATUS")
        print("HANDOFF CHECK OK: runner wrote the inbox, agent read it and wrote the outbox, runner read it back", flush=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        if not keep:
            for u in made:
                subprocess.run(["userdel", u], capture_output=True)


if __name__ == "__main__":
    main()
