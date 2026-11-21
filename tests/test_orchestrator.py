"""Orchestrators (0.7.0): one long-lived agent session per name, pinned to one worker, supervised outside the runner.

The backend tests use the real HTTP backend. The supervisor tests run the real supervisor, agent-exec and tmux in
single-user mode with a stand-in for Claude Code, a stand-in for with-secrets and a stand-in Relay command, so no
model, no real secret and no real Relay is used.
"""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ADMIN, SERVER, WORKER, Backend, sb

sys.path.insert(0, SERVER)
import swarm_orchestrator as so  # noqa: E402

FAKE_TOKEN = "ghp_" + "A1b2C3d4" * 5   # matches the GitHub token pattern; not a real token
HAS_TMUX = shutil.which("tmux") is not None


def checked_in(backend, worker="w1", projects=None):
    body = {"worker": worker}
    if projects is not None:
        body["projects"] = projects
    backend.req("POST", "/worker/claim", body, tok=WORKER)


def beat(backend, name, expect=200, **body):
    body.setdefault("worker", "w1")
    body.setdefault("supervisor_id", "sup-aaaaaaaa")
    return backend.req("POST", f"/worker/orchestrators/{name}/heartbeat", body, tok=WORKER, expect=expect)


class OrchestratorApiTest(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.chief = self.b.bot("chief-bot", role="chief", url="http://127.0.0.1:9/chief")
        self.owner = self.b.bot("app-bot", url="http://127.0.0.1:9/app", projects=("app",))
        self.other = self.b.bot("other-bot", projects=("other",))
        checked_in(self.b)

    def create(self, expect=200, **fields):
        body = dict(name="steward", worker_id="w1", project="app", owner_bot_id="app-bot",
                    brief="Keep improving the app.", secret_bundles=["app-runtime"], relay_agent_name="steward")
        body.update(fields)
        return self.b.req("POST", "/orchestrators", body, expect=expect)

    def notices(self, kind):
        return [json.loads(r["payload"]) for r in self.b.api.s.q("SELECT payload FROM notices ORDER BY id")
                if json.loads(r["payload"]).get("kind") == kind]

    def test_create_validates_and_never_takes_secret_values(self):
        o = self.create()["orchestrator"]
        self.assertEqual((o["state"], o["desired"], o["tmux_session"], o["secret_bundles"]),
                         ("stopped", "stopped", "orch-steward", ["app-runtime"]))
        cases = [({"name": "Steward"}, 400), ({"name": "p"}, 400), ({"name": "x" * 33}, 400), ({"name": "steward"}, 409),
                 ({"name": "p2", "api_key": "abc"}, 400), ({"name": "p2", "env": {"A": "b"}}, 400),
                 ({"name": "p2", "brief": f"use {FAKE_TOKEN}"}, 400), ({"name": "p2", "worker_id": "never-seen"}, 400),
                 ({"name": "p2", "project": "nope"}, 404), ({"name": "p2", "provider": "codex"}, 400),
                 ({"name": "p2", "relay_agent_name": "steward"}, 409), ({"name": "p2", "owner_bot_id": "other-bot"}, 400),
                 ({"name": "p2", "secret_bundles": ["../admin/x"]}, 400), ({"name": "p2", "repo": "someone/else"}, 400),
                 ({"name": "p2", "brief": ""}, 400)]
        for fields, status in cases:
            with self.subTest(fields=fields):
                self.create(expect=status, **dict({"relay_agent_name": None}, **fields))
        for tok in (self.chief, self.owner, WORKER):
            self.b.req("POST", "/orchestrators", {"name": "p3", "worker_id": "w1", "project": "app", "brief": "x"}, tok=tok, expect=403)

    def test_project_allowlist_of_the_worker_applies(self):
        checked_in(self.b, "w2", projects=["other"])
        self.create(expect=403, name="p2", worker_id="w2", relay_agent_name=None)
        checked_in(self.b, "w2", projects=["other", "app"])
        self.create(name="p2", worker_id="w2", relay_agent_name=None)

    def test_visibility_and_admin_only_control(self):
        self.create()
        self.assertEqual([o["name"] for o in self.b.req("GET", "/orchestrators", tok=self.owner)["orchestrators"]], ["steward"])
        self.assertEqual(self.b.req("GET", "/orchestrators", tok=self.other)["orchestrators"], [])
        self.b.req("GET", "/orchestrators/steward", tok=self.other, expect=404)
        self.assertIn("brief", self.b.req("GET", "/orchestrators/steward", tok=self.chief)["orchestrator"])
        for action in ("start", "stop", "restart", "delete"):
            self.b.req("POST", f"/orchestrators/steward/{action}", tok=self.owner, expect=403)
            self.b.req("POST", f"/orchestrators/steward/{action}", tok=self.chief, expect=403)
        self.b.req("PUT", "/orchestrators/steward", {"model": "opus"}, tok=self.chief, expect=403)
        self.b.req("GET", "/orchestrators", tok=WORKER, expect=403)
        out = self.b.req("POST", "/orchestrators/steward/start")["orchestrator"]
        self.assertEqual((out["desired"], out["generation"]), ("running", 1))
        out = self.b.req("POST", "/orchestrators/steward/restart")["orchestrator"]
        self.assertEqual((out["generation"], out["restart_generation"]), (2, 2))
        self.b.req("PUT", "/orchestrators/steward", {"worker_id": "w1", "model": "opus"})
        self.b.req("PUT", "/orchestrators/steward", {"name": "other"}, expect=400)
        self.assertEqual(self.b.req("GET", "/orchestrators/steward")["orchestrator"]["model"], "opus")

    def test_heartbeat_is_for_the_pinned_worker_and_one_supervisor(self):
        self.create(start=True)
        self.b.req("POST", "/worker/orchestrators/steward/heartbeat", {"worker": "w1", "supervisor_id": "sup-aaaaaaaa"},
                   tok=self.owner, expect=403)
        checked_in(self.b, "w2")
        beat(self.b, "steward", 403, worker="w2")
        beat(self.b, "nope", 404)
        r = beat(self.b, "steward", state="running", pid=4242, session_id="sess-1", restart_count=0,
                 started_at=sb.now(), log_tail=f"working\nexport TOKEN={FAKE_TOKEN}\nstill working",
                 events=[{"type": "start", "text": "fresh session"}])
        self.assertEqual((r["desired"], r["deleted"], r["config"]["secret_bundles"], r["config"]["brief"]),
                         ("running", False, ["app-runtime"], "Keep improving the app."))
        beat(self.b, "steward", 409, supervisor_id="sup-bbbbbbbb")
        beat(self.b, "steward", 400, events=[{"type": "made-up"}])
        beat(self.b, "steward", 400, state="unreachable")
        o = self.b.req("GET", "/orchestrators/steward")
        self.assertEqual((o["orchestrator"]["state"], o["orchestrator"]["pid"], o["orchestrator"]["session_id"]), ("running", 4242, "sess-1"))
        self.assertIn("start", [e["type"] for e in o["events"]])
        logs = self.b.req("GET", "/orchestrators/steward/logs?lines=2", tok=self.owner)["lines"]
        self.assertEqual(logs, ["[line hidden: it looks like a secret]", "still working"])
        self.b.req("GET", "/orchestrators/steward/logs?lines=0", expect=400)
        dump = "\n".join(str(r) for r in self.b.api.s.q("SELECT * FROM orchestrators"))
        self.assertNotIn(FAKE_TOKEN, dump)
        # Once the first supervisor has been quiet past the lease, a new one may take over.
        self.b.api.s.run("UPDATE orchestrators SET last_heartbeat_at = ? WHERE name = 'steward'", sb.now() - 120)
        beat(self.b, "steward", supervisor_id="sup-bbbbbbbb")

    def test_crash_loop_and_stale_supervisor_tell_the_owner_once(self):
        self.create(start=True)
        beat(self.b, "steward", state="running")
        beat(self.b, "steward", state="crash-loop", last_exit="Claude exited with code 1",
             events=[{"type": "crash-loop", "text": "5 crashes"}])
        beat(self.b, "steward", state="crash-loop")
        sent = self.notices("crash-loop")
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["notify_target"], sent[0]["orchestrator"]), ("app-bot", "steward"))
        self.assertIn("swarmctl orchestrator restart steward", sent[0]["text"])
        beat(self.b, "steward", state="running")
        self.b.api.s.run("UPDATE orchestrators SET last_heartbeat_at = ?, desired_at = ? WHERE name = 'steward'",
                         sb.now() - 700, sb.now() - 900)
        self.b.api.orchestrator_watch()
        self.b.api.orchestrator_watch()
        self.assertEqual(self.b.req("GET", "/orchestrators/steward")["orchestrator"]["state"], "unreachable")
        self.assertEqual(len(self.notices("unreachable")), 1)
        self.assertTrue(any("unreachable" in w for w in self.b.req("GET", "/status")["warnings"]))
        beat(self.b, "steward", state="running")
        types = [e["type"] for e in self.b.req("GET", "/orchestrators/steward")["events"]]
        self.assertIn("recovered", types)
        # A stopped orchestrator is never marked unreachable.
        self.b.req("POST", "/orchestrators/steward/stop")
        self.b.api.s.run("UPDATE orchestrators SET last_heartbeat_at = ?, desired_at = ? WHERE name = 'steward'",
                         sb.now() - 7000, sb.now() - 7000)
        self.b.api.orchestrator_watch()
        self.assertEqual(len(self.notices("unreachable")), 1)
        # Without an owner bot, the Chief of Staff hears about it.
        self.create(name="lonely", owner_bot_id=None, relay_agent_name=None, start=True)
        self.b.api.s.run("UPDATE orchestrators SET desired_at = ? WHERE name = 'lonely'", sb.now() - 900)
        self.b.api.orchestrator_watch()
        self.assertEqual([n["notify_target"] for n in self.notices("unreachable")], ["app-bot", "chief-bot"])

    def test_messages_one_at_a_time_and_size_limited(self):
        self.create()
        self.b.req("POST", "/orchestrators/steward/messages", {"text": "hello"}, tok=self.owner, expect=409)  # stopped
        self.b.req("POST", "/orchestrators/steward/start")
        self.b.req("POST", "/orchestrators/steward/messages", {"text": "x" * 4001}, expect=400)
        self.b.req("POST", "/orchestrators/steward/messages", {"text": f"key {FAKE_TOKEN}"}, expect=400)
        self.b.req("POST", "/orchestrators/steward/messages", {"text": "hi"}, tok=self.other, expect=404)
        mid = self.b.req("POST", "/orchestrators/steward/messages", {"text": "Check the signup page"}, tok=self.owner)["id"]
        self.b.req("POST", "/orchestrators/steward/messages", {"text": "and another"}, tok=self.chief, expect=409)
        r = beat(self.b, "steward", state="running")
        self.assertEqual((r["message"]["id"], r["message"]["text"], r["message"]["sender"]), (mid, "Check the signup page", "app-bot"))
        r = beat(self.b, "steward", state="running", delivered_message_id=mid)
        self.assertIsNone(r["message"])
        self.b.req("POST", "/orchestrators/steward/messages", {"text": "next one"}, tok=self.chief)

    def test_delete_needs_stop_and_a_deleted_record_never_runs(self):
        self.create(start=True)
        beat(self.b, "steward", state="running")
        self.b.req("POST", "/orchestrators/steward/delete", expect=409)
        self.b.req("POST", "/orchestrators/steward/stop")
        # Still stopping: the session is not down yet, so the record stays (0.7.1).
        beat(self.b, "steward", state="stopping")
        self.assertIn("still stopping", self.b.req("POST", "/orchestrators/steward/delete", expect=409)["error"])
        beat(self.b, "steward", state="stopped")
        self.b.req("POST", "/orchestrators/steward/delete")
        self.b.req("GET", "/orchestrators/steward", expect=404)
        r = beat(self.b, "steward", state="running")
        self.assertEqual((r["desired"], r["deleted"], r["message"]), ("stopped", True, None))
        self.create()  # the name can be used again
        self.assertEqual(self.b.req("GET", "/orchestrators/steward")["orchestrator"]["desired"], "stopped")

    def test_needs_login_tells_the_owner_once_warns_and_hides_sign_in_links(self):
        self.create(start=True)
        link = "https://claude.ai/oauth/authorize?code=true&client_id=abc&response_type=code&code_challenge=XYZ&state=QQ"
        beat(self.b, "steward", state="running", log_tail=f"Select login method:\n{link}\nsee https://example.com/x/oauth/cb ok")
        logs = self.b.req("GET", "/orchestrators/steward/logs")["lines"]
        self.assertNotIn("code_challenge", "\n".join(logs))
        self.assertIn("see [sign-in link hidden] ok", logs)
        for _ in range(2):
            beat(self.b, "steward", state="needs-login", last_exit="Claude is waiting at a sign-in screen. Run: sudo swarmctl login claude",
                 events=[{"type": "needs-login", "text": "sign-in screen"}, {"type": "first-run", "text": "theme"},
                         {"type": "message-delivered-busy", "text": "pasted while busy"}])
        sent = self.notices("needs-login")
        self.assertEqual([n["notify_target"] for n in sent], ["app-bot"])
        self.assertIn("sudo swarmctl login claude", sent[0]["text"])
        self.assertTrue(any("needs-login" in w for w in self.b.req("GET", "/status")["warnings"]))
        # Messages can still be queued; they wait.
        self.b.req("POST", "/orchestrators/steward/messages", {"text": "later"}, tok=self.owner)

    def test_old_database_gains_the_tables(self):
        with tempfile.TemporaryDirectory() as target:
            path = str(Path(target, "old.db"))
            sb.Store(path).db().close()
            with sqlite3.connect(path) as db:
                for table in ("orchestrators", "orchestrator_events", "orchestrator_messages"):
                    db.execute(f"DROP TABLE {table}")
                db.execute("CREATE TABLE workers_old AS SELECT name, last_seen, draining FROM workers")
                db.execute("DROP TABLE workers")
                db.execute("ALTER TABLE workers_old RENAME TO workers")
            store = sb.Store(path)
            self.assertEqual(store.one("SELECT COUNT(*) AS n FROM orchestrators")["n"], 0)
            self.assertIn("projects", {r["name"] for r in store.q("PRAGMA table_info(workers)")})
            store.db().close()


class OrchestratorCliTest(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.owner = self.b.bot("app-bot", projects=("app",))
        checked_in(self.b)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "admin").write_text(ADMIN)
        (self.tmp / "brief.md").write_text("Keep improving.\n")

    def ctl(self, *args, token="admin", check=True):
        r = subprocess.run([str(Path(SERVER, "swarmctl")), "orchestrator", *args], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, SWARM_URL=self.b.base, SWARM_TOKEN_FILE=str(self.tmp / token)))
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_create_status_message_logs_stop(self):
        out = self.ctl("create", "steward", "--worker", "w1", "-p", "app", "--owner", "app-bot", "--brief", str(self.tmp / "brief.md"),
                       "--secret-bundle", "app-runtime", "--relay-name", "steward", "--start").stdout
        self.assertIn("swarm-orchestrator@steward", out)
        self.assertIn("steward", self.ctl("list").stdout)
        self.ctl("message", "steward", "Look at signup next")
        beat(self.b, "steward", state="running", started_at=sb.now() - 90061, log_tail="line one\nline two")
        status = self.ctl("status", "steward").stdout
        for text in ("running (wants running)", "app-runtime", "orch-steward", "1d 1h 1m", "message queued"):
            self.assertIn(text, status)
        self.assertEqual(self.ctl("logs", "steward", "--lines", "1").stdout.splitlines()[-1], "line two")
        (self.tmp / "bot").write_text(self.owner)
        refused = self.ctl("stop", "steward", token="bot", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("403", refused.stderr)
        self.ctl("set", "steward", "--no-secret-bundles", "-m", "opus")
        self.assertEqual(json.loads(self.ctl("status", "steward", "--json").stdout)["orchestrator"]["secret_bundles"], [])
        self.ctl("stop", "steward")
        self.assertIn("wants stopped", self.ctl("list").stdout)
        self.assertNotEqual(self.ctl("status", check=False).returncode, 0)


class ScreenTest(unittest.TestCase):
    def test_classify_reads_only_the_bottom_of_the_screen(self):
        self.assertEqual(so.classify("Welcome\n\n Select login method:\n 1. Claude account")[0], "login")
        self.assertEqual(so.classify("Paste code here if prompted >")[0], "login")
        self.assertEqual(so.classify("Choose the text style that looks best\n 1. Dark mode")[:3:2], ("first-run", ["Enter"]))
        self.assertEqual(so.classify("Security notes:\n Press Enter to continue")[1], "the security notes")
        self.assertEqual(so.classify("WARNING: Claude Code running in Bypass Permissions mode\n 1. No, exit")[0], "block")
        self.assertIsNone(so.classify("> working on it\n  esc to interrupt"))
        self.assertIsNone(so.classify("Select login method\n" + "work\n" * 40))

    def test_scrub_hides_sign_in_links(self):
        out = so.scrub("open https://claude.ai/oauth/authorize?client_id=a&code_challenge=b\n"
                       "go to https://github.com/login/device now\nplain line", [])
        self.assertEqual(out.splitlines(), ["[line hidden: part of a sign-in link]", "go to [sign-in link hidden] now", "plain line"])


class RestartPolicyTest(unittest.TestCase):
    def test_backoff_doubles_to_the_cap_and_resets_after_a_healthy_run(self):
        p = so.RestartPolicy(start=5, cap=300, healthy=600, limit=100, window=1800)
        self.assertEqual([p.crashed(t * 1000, 1) for t in range(8)], [5, 10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(p.crashed(9000, 601), 5)
        self.assertEqual(p.crashed(9100, 3), 10)

    def test_five_crashes_in_thirty_minutes_is_a_crash_loop(self):
        p = so.RestartPolicy(start=5, cap=300, healthy=600, limit=5, window=1800)
        self.assertEqual([p.crashed(t, 1) is None for t in (0, 100, 200, 300)], [False] * 4)
        self.assertIsNone(p.crashed(400, 1))
        p = so.RestartPolicy(start=5, cap=300, healthy=600, limit=5, window=1800)
        self.assertEqual([p.crashed(t, 1) is None for t in (0, 500, 1000, 1500, 2000, 2500)], [False] * 6)


def write_exe(path, text):
    Path(path).write_text(text)
    os.chmod(path, 0o755)
    return str(path)


FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Stand-in for Claude Code in a terminal: logs its arguments and every line typed into it."""
import json, os, re, sys, time, uuid
base = os.environ["FAKE_DIR"]
args = sys.argv[1:]
sid = args[args.index("--resume") + 1] if "--resume" in args else "s-" + uuid.uuid4().hex[:12]
sessions = os.path.join(base, "sessions", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
os.makedirs(sessions, exist_ok=True)
open(os.path.join(sessions, sid + ".jsonl"), "a").close()
def note(**kw):
    with open(os.path.join(base, "claude.log"), "a") as fh:
        fh.write(json.dumps(dict(kw, sid=sid, bundles=os.environ.get("FAKE_BUNDLES"),
                                 relay=os.environ.get("SWARM_RELAY_AGENT_NAME"))) + "\n")
note(event="start", args=args)
mode = open(os.path.join(base, "mode")).read().strip() if os.path.exists(os.path.join(base, "mode")) else ""
if mode == "crash":
    time.sleep(0.2)
    sys.exit(3)
if mode == "login":
    # Like Claude Code with no sign-in: two first-run screens that take Enter, then the sign-in menu.
    print("Choose the text style that looks best with your terminal\n 1. Dark mode\n 2. Light mode", flush=True)
    note(event="input", text=sys.stdin.readline().rstrip("\n"))
    print("\n" * 60 + "Security notes:\n 1. Claude can make mistakes\nPress Enter to continue", flush=True)
    note(event="input", text=sys.stdin.readline().rstrip("\n"))
    print("\n" * 60 + "Select login method:\n 1. Claude account with subscription\n 2. Anthropic Console account", flush=True)
    print("https://claude.ai/oauth/authorize?code=true&client_id=abc&response_type=code&code_challenge=XYZ&state=QQ", flush=True)
    for line in sys.stdin:
        note(event="input", text=line.rstrip("\n"))
    sys.exit(0)
print("fake claude ready", flush=True)
for line in sys.stdin:
    note(event="input", text=line.rstrip("\n"))
    print("got:", line.strip()[:60], flush=True)
    if line.strip() == "exit now":
        sys.exit(1)
'''

FAKE_WITH_SECRETS = r'''#!/bin/bash
# Stand-in for with-secrets: knows bundle names only, never has values.
if [[ "$1" == --check ]]; then shift; for n in "$@"; do [[ "$n" == missing ]] && exit 2; done; exit 0; fi
names=(); while [[ "$1" != -- ]]; do names+=("$1"); shift; done; shift
FAKE_BUNDLES="${names[*]}" exec "$@"
'''

FAKE_RELAY = r'''#!/bin/bash
# Stand-in for a Relay registration command. It prints a token, which must never be kept anywhere.
echo "registered $1 with token %s"
echo "$1" >> "$FAKE_DIR/relay.log"
''' % FAKE_TOKEN


@unittest.skipUnless(HAS_TMUX, "tmux is not installed")
class SupervisorTest(unittest.TestCase):
    """The real supervisor, agent-exec and tmux, single-user mode, stand-ins for Claude, with-secrets and Relay."""

    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.owner = self.b.bot("app-bot", url="http://127.0.0.1:9/app", projects=("app",))
        checked_in(self.b)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        self.name = "t" + os.urandom(4).hex()
        (self.tmp / "worker.token").write_text(WORKER)
        (self.tmp / "agent.env").write_text(f"FAKE_DIR={self.fake}\nCLAUDE_CODE_OAUTH_TOKEN=stand-in-not-a-token\n")
        (self.tmp / "claude-home").mkdir()
        self.env = dict(os.environ, SWARM_URL=self.b.base, SWARM_WORKER_NAME="w1", SWARM_AGENT_USER="",
                        SWARM_WORKER_TOKEN_FILE=str(self.tmp / "worker.token"), SWARM_WORK_DIR=str(self.tmp / "work"),
                        SWARM_HANDOFF_DIR=str(self.tmp / "handoff"), SWARM_RUNNER_DIR=str(self.tmp / "runner"),
                        SWARM_AGENT_ENV_FILE=str(self.tmp / "agent.env"), SWARM_AGENT_EXEC=str(Path(SERVER, "agent-exec")),
                        SWARM_ORCH_CLAUDE=write_exe(self.tmp / "fake-claude", FAKE_CLAUDE),
                        SWARM_ORCH_WITH_SECRETS=write_exe(self.tmp / "fake-with-secrets", FAKE_WITH_SECRETS),
                        SWARM_RELAY_REGISTER_CMD=write_exe(self.tmp / "fake-relay", FAKE_RELAY) + " {name}",
                        SWARM_ORCH_SESSIONS_DIR=str(self.fake / "sessions"), SWARM_ORCH_SCOPE="0",
                        SWARM_ORCH_CLAUDE_DIR=str(self.tmp / "claude-home"), SWARM_ORCH_SCREEN_SECONDS="1",
                        SWARM_ORCH_HEARTBEAT_SECONDS="1", SWARM_ORCH_POLL_SECONDS="0.3", SWARM_ORCH_IDLE_SECONDS="1",
                        SWARM_ORCH_STOP_GRACE="6", SWARM_ORCH_BACKOFF_START="0.5", SWARM_ORCH_BACKOFF_MAX="2",
                        SWARM_ORCH_CRASH_LIMIT="5", SWARM_ORCH_CRASH_WINDOW="1800")
        self.env.pop("SWARM_WORKER_PROJECTS", None)
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        subprocess.run(["tmux", "-L", f"swarm-orch-{self.name}", "kill-server"], capture_output=True)
        sock = Path(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", f"swarm-orch-{self.name}")
        if sock.is_socket():
            sock.unlink()

    def supervise(self):
        with open(self.tmp / f"supervisor-{len(self.procs)}.log", "w") as out:
            p = subprocess.Popen([sys.executable, str(Path(SERVER, "swarm_orchestrator.py")), self.name], env=self.env,
                                 stdout=out, stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    def wait(self, what, check, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            v = check()
            if v:
                return v
            time.sleep(0.2)
        logs = "\n".join(p.read_text() for p in sorted(self.tmp.glob("supervisor-*.log")))
        self.fail(f"timed out waiting for {what}\n{self.record()}\n{logs[-3000:]}")

    def record(self):
        return self.b.req("GET", f"/orchestrators/{self.name}")

    def lines(self):
        path = self.fake / "claude.log"
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []

    def alive(self):
        return subprocess.run(["tmux", "-L", f"swarm-orch-{self.name}", "has-session", "-t", f"orch-{self.name}"],
                              capture_output=True).returncode == 0

    def test_start_message_crash_resume_adopt_stop(self):
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", owner_bot_id="app-bot",
                                                  brief="Improve the app.", secret_bundles=["app-runtime"],
                                                  relay_agent_name=self.name, start=True))
        sup = self.supervise()
        rec = self.wait("running", lambda: (r := self.record())["orchestrator"]["state"] == "running" and r["orchestrator"]["session_id"] and r)
        o = rec["orchestrator"]
        self.assertEqual((o["tmux_session"], o["restart_count"]), (f"orch-{self.name}", 0))
        self.assertTrue(o["pid"])
        first = self.lines()[0]
        self.assertEqual((first["args"][-1], first["bundles"], first["relay"]), ("Read .orchestrator/start.md and follow it.",
                                                                                 "app-runtime", self.name))
        folder = self.tmp / "work" / "orchestrators" / self.name
        self.assertEqual((folder / ".orchestrator" / "brief.md").read_text().strip(), "Improve the app.")
        self.assertIn("CHECKPOINT.md", (folder / ".orchestrator" / "start.md").read_text())
        self.assertEqual((self.fake / "relay.log").read_text().split(), [self.name])

        # A message is typed in when the session is idle, and acknowledged.
        self.b.req("POST", f"/orchestrators/{self.name}/messages", {"text": "Check the signup page"}, tok=self.owner)
        self.wait("message typed", lambda: any(ln.get("text") == "Check the signup page" for ln in self.lines()))
        self.wait("message acknowledged", lambda: self.record()["pending_message"] is None)
        self.wait("tail uploaded", lambda: any("got: Check the signup page" in ln for ln in
                                               self.b.req("GET", f"/orchestrators/{self.name}/logs")["lines"]))

        # The runner restarting (stop-all) leaves the session alone.
        subprocess.run([self.env["SWARM_AGENT_EXEC"], "--work", self.env["SWARM_WORK_DIR"], "--handoff",
                        self.env["SWARM_HANDOFF_DIR"], "stop-all"], env=self.env, check=True)
        self.assertTrue(self.alive())

        # Claude exits: the supervisor restarts it, resuming the same session.
        sid = o["session_id"]
        subprocess.run(["tmux", "-L", f"swarm-orch-{self.name}", "send-keys", "-t", f"orch-{self.name}", "exit now", "Enter"], check=True)
        self.wait("restart", lambda: self.record()["orchestrator"]["restart_count"] == 1 and
                  self.record()["orchestrator"]["state"] == "running")
        starts = [ln for ln in self.lines() if ln["event"] == "start"]
        self.assertEqual(starts[-1]["args"][-3:], ["--resume", sid, "Read .orchestrator/resume.md and follow it."])
        self.assertEqual((self.fake / "relay.log").read_text().split(), [self.name, self.name])
        types = [e["type"] for e in self.record()["events"]]
        for t in ("crash", "restart", "start", "relay"):
            self.assertIn(t, types)

        # The supervisor itself restarts: the session keeps running and is adopted, not started again.
        sup.send_signal(signal.SIGTERM)
        sup.wait(10)
        self.assertTrue(self.alive())
        n_starts = len(starts)
        self.supervise()
        self.wait("adopted", lambda: "adopt" in [e["type"] for e in self.record()["events"]])
        self.assertEqual(len([ln for ln in self.lines() if ln["event"] == "start"]), n_starts)

        # Stop: a wrap-up message, then the session ends and stays down.
        self.b.req("POST", f"/orchestrators/{self.name}/stop")
        self.wait("stopped", lambda: self.record()["orchestrator"]["state"] == "stopped" and not self.alive())
        self.assertTrue(any("The supervisor is stopping this session" in (ln.get("text") or "") for ln in self.lines()))
        time.sleep(2)
        self.assertFalse(self.alive())

        # Nothing the stand-ins printed as a token reached the backend.
        dump = json.dumps([self.b.api.s.q(f"SELECT * FROM {t}") for t in ("orchestrators", "orchestrator_events")])
        self.assertNotIn(FAKE_TOKEN, dump)

    def test_login_screen_is_never_typed_into(self):
        """0.7.1, install test 6: first-run screens get Enter; the sign-in menu stops the session and holds messages."""
        (self.fake / "mode").write_text("login")
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", owner_bot_id="app-bot",
                                                  brief="Improve.", start=True))
        self.supervise()
        self.wait("started", lambda: self.lines())
        self.b.req("POST", f"/orchestrators/{self.name}/messages", {"text": "Check the signup page"}, tok=self.owner)
        rec = self.wait("needs-login", lambda: (r := self.record())["orchestrator"]["state"] == "needs-login" and r)
        self.assertIn("sudo swarmctl login claude on worker w1", rec["orchestrator"]["last_exit"])
        self.assertIn("first-run", [e["type"] for e in rec["events"]])
        self.assertFalse(self.alive())
        typed = [ln["text"] for ln in self.lines() if ln["event"] == "input"]
        self.assertEqual(typed, ["", ""], "only the two Enter presses for the theme and security screens")
        self.assertIsNotNone(rec["pending_message"], "the message waits in the backend")
        payloads = [json.loads(r["payload"]) for r in self.b.api.s.q("SELECT payload FROM notices")]
        self.assertEqual([p["notify_target"] for p in payloads if p.get("kind") == "needs-login"], ["app-bot"])
        dump = json.dumps(self.b.api.s.q("SELECT * FROM orchestrators"))
        self.assertNotIn("code_challenge", dump)
        # It stays down, even across a supervisor restart, until an admin starts it again.
        self.procs[0].send_signal(signal.SIGTERM)
        self.procs[0].wait(10)
        self.supervise()
        time.sleep(3)
        self.assertEqual(len([ln for ln in self.lines() if ln["event"] == "start"]), 1)
        self.assertEqual(self.record()["orchestrator"]["state"], "needs-login")
        (self.fake / "mode").write_text("")
        self.b.req("POST", f"/orchestrators/{self.name}/start")
        self.wait("running again", lambda: self.record()["orchestrator"]["state"] == "running")
        self.wait("message typed after the sign-in", lambda: any(ln.get("text") == "Check the signup page" for ln in self.lines()))

    def test_no_sign_in_refuses_before_the_first_start(self):
        (self.tmp / "agent.env").write_text(f"FAKE_DIR={self.fake}\n")
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", owner_bot_id="app-bot",
                                                  brief="Improve.", start=True))
        self.supervise()
        rec = self.wait("needs-login", lambda: (r := self.record())["orchestrator"]["state"] == "needs-login" and r)
        self.assertIn("no sign-in", rec["orchestrator"]["last_exit"])
        time.sleep(2)
        self.assertEqual(self.lines(), [])
        (self.tmp / "claude-home" / ".credentials.json").write_text('{"claudeAiOauth": {}}')
        self.b.req("POST", f"/orchestrators/{self.name}/start")
        self.wait("running after the sign-in", lambda: self.record()["orchestrator"]["state"] == "running")

    def test_supervisor_shutdown_ends_a_session_that_should_stop(self):
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", brief="Improve.", start=True))
        self.env["SWARM_ORCH_STOP_GRACE"] = "600"   # the graceful stop stays in "stopping" for this test
        sup = self.supervise()
        self.wait("running", lambda: self.record()["orchestrator"]["state"] == "running")
        self.b.req("POST", f"/orchestrators/{self.name}/stop")
        self.wait("stopping", lambda: self.record()["orchestrator"]["state"] == "stopping")
        self.b.req("POST", f"/orchestrators/{self.name}/delete", expect=409)
        sup.send_signal(signal.SIGTERM)
        sup.wait(20)
        self.assertFalse(self.alive(), "the session must not outlive its supervisor once it should stop")
        self.b.req("POST", f"/orchestrators/{self.name}/delete")

    def test_crash_loop_stops_restarting_until_an_admin_restarts(self):
        (self.fake / "mode").write_text("crash")
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", owner_bot_id="app-bot",
                                                  brief="Improve.", start=True))
        self.supervise()
        self.wait("crash-loop", lambda: self.record()["orchestrator"]["state"] == "crash-loop", timeout=60)
        n = len(self.lines())
        self.assertEqual(n, 5)
        time.sleep(3)
        self.assertEqual(len(self.lines()), n, "restarted during a crash loop")
        payloads = [json.loads(r["payload"]) for r in self.b.api.s.q("SELECT payload FROM notices")]
        self.assertEqual([p["notify_target"] for p in payloads if p.get("kind") == "crash-loop"], ["app-bot"])
        (self.fake / "mode").write_text("")
        self.b.req("POST", f"/orchestrators/{self.name}/restart")
        self.wait("running again", lambda: self.record()["orchestrator"]["state"] == "running")

    def test_missing_secret_bundle_and_wrong_worker_never_start(self):
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", brief="Improve.",
                                                  secret_bundles=["missing"], start=True))
        self.supervise()
        self.wait("start refused", lambda: "secret bundle is not available" in (self.record()["orchestrator"]["last_exit"] or ""))
        self.assertEqual(self.lines(), [])
        self.procs[0].kill()
        self.procs[0].wait()
        self.b.req("POST", f"/orchestrators/{self.name}/stop")
        checked_in(self.b, "w2")
        self.b.req("PUT", f"/orchestrators/{self.name}", {"worker_id": "w2", "secret_bundles": []})
        self.b.req("POST", f"/orchestrators/{self.name}/start")
        self.supervise()   # still named w1
        time.sleep(3)
        self.assertEqual(self.lines(), [])
        self.assertFalse(self.alive())

    def test_project_outside_the_local_allowlist_is_blocked_once(self):
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", brief="Improve.", start=True))
        self.env["SWARM_WORKER_PROJECTS"] = "other"
        self.supervise()
        self.wait("blocked", lambda: self.record()["orchestrator"]["state"] == "blocked")
        time.sleep(2)
        self.assertEqual([e["type"] for e in self.record()["events"]].count("blocked"), 1)
        self.assertEqual(self.lines(), [])

    def test_a_second_supervisor_for_the_same_name_refuses(self):
        self.b.req("POST", "/orchestrators", dict(name=self.name, worker_id="w1", project="app", brief="Improve."))
        self.supervise()
        time.sleep(1)
        second = subprocess.run([sys.executable, str(Path(SERVER, "swarm_orchestrator.py")), self.name], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("another supervisor", second.stderr)


class CleanupSkipsOrchestratorsTest(unittest.TestCase):
    def test_cleanup_never_looks_at_the_orchestrators_folder(self):
        import swarm_cleanup
        self.assertIn("orchestrators", swarm_cleanup.SKIP)


if __name__ == "__main__":
    unittest.main()
