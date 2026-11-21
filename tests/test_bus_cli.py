"""swarmctl bus against a small in-process fake backend (no network, fake credentials)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # lets `unittest tests.test_tools` find helpers
CTL = Path(__file__).resolve().parents[1] / "server" / "swarmctl"
SECRET = "ab_polisher.1." + "f" * 40
ADMIN = "admin-token-xyz"


class Stub(BaseHTTPRequestHandler):
    seen, send_failures, ack_first = [], 0, True

    def _reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        n = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(n) or b"null") if n else None
        Stub.seen.append((self.command, self.path, self.headers.get("authorization"), payload))
        p = self.path
        if p == "/bus/send":
            if Stub.send_failures > 0:
                Stub.send_failures -= 1
                return self._reply({"error": "boom"}, 503)
            return self._reply({"id": 7, "to": payload["to"], "thread": "dm:chief+polisher", "state": "queued",
                                "warning": "chief is unreachable: orchestrator not running"}, 201)
        if p.startswith("/bus/messages/") and p.endswith("/ack"):
            return self._reply({"first": Stub.ack_first, "id": 7, "newer_in_thread": 2, "thread": "dm:chief+polisher"})
        if p.startswith("/bus/agents/polisher/token"):
            return self._reply({"token": SECRET})
        if p.startswith("/bus/agents/") and self.command in ("PUT", "DELETE"):
            return self._reply({"ok": True})
        if p.startswith("/bus/inbox"):
            return self._reply({"messages": [{"id": 7, "from": "chief", "thread": "t1", "body": "hello\nsecond",
                                              "created_at": 1, "newer_in_thread": 1}]})
        if p.startswith("/bus/messages/99"):
            return self._reply({"error": "no such message", "did_you_mean": ["98", "97"]}, 404)
        return self._reply({"error": "unexpected"}, 404)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, *a):
        pass


class BusCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "agent.token").write_text(SECRET + "\n")
        (t / "admin.token").write_text(ADMIN + "\n")
        self.env = {"PATH": os.environ["PATH"], "HOME": str(t), "SWARM_URL": f"http://127.0.0.1:{self.srv.server_port}",
                    "BUS_AGENT": "polisher", "BUS_TOKEN_FILE": str(t / "agent.token"), "SWARM_TOKEN_FILE": str(t / "admin.token")}
        Stub.seen.clear()
        Stub.send_failures = 0
        Stub.ack_first = True

    def tearDown(self):
        self.tmp.cleanup()

    def bus(self, *args, env=None, stdin=None):
        return subprocess.run([sys.executable, str(CTL), "bus", *args], capture_output=True, text=True,
                              env=env or self.env, input=stdin, timeout=60)

    def sends(self):
        return [s for s in Stub.seen if s[1] == "/bus/send"]

    def test_send_retries_reuse_client_msg_id_and_warning_goes_to_stderr(self):
        Stub.send_failures = 2
        r = self.bus("send", "chief", "hi there")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sent #7 to chief (thread dm:chief+polisher)", r.stdout)
        self.assertIn("warning: chief is unreachable", r.stderr)
        self.assertNotIn("warning", r.stdout)
        sends = self.sends()
        self.assertEqual(len(sends), 3)
        self.assertEqual(len({s[3]["client_msg_id"] for s in sends}), 1)
        self.assertEqual(sends[0][2], "Bearer " + SECRET)
        self.assertNotIn("from", sends[0][3])

    def test_send_explicit_id_stdin_and_thread(self):
        r = self.bus("send", "chief", "-", "--thread", "task:1", "--urgent", "--id", "abc", stdin="from stdin\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.sends()[0][3]
        self.assertEqual((p["client_msg_id"], p["thread"], p["urgent"], p["body"]), ("abc", "task:1", True, "from stdin"))

    def test_send_wake_policy_flags(self):
        r = self.bus("send", "chief", "ping", "--noop", "--no-reply", "--id", "n1")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.sends()[-1][3]
        self.assertEqual((p["noop"], p["reply_expected"]), (True, False))
        self.assertNotIn("urgent", p)
        self.bus("send", "chief", "question", "--urgent", "--reply", "--id", "n2")
        p = self.sends()[-1][3]
        self.assertEqual((p["urgent"], p["reply_expected"]), (True, True))
        self.assertNotIn("noop", p)
        self.bus("send", "chief", "status update", "--fyi")
        self.assertIs(self.sends()[-1][3]["actionable"], False)
        r = self.bus("send", "chief", "x", "--reply", "--no-reply")
        self.assertNotEqual(r.returncode, 0)

    def test_send_json_output(self):
        r = self.bus("send", "chief", "hi", "--json")
        self.assertEqual(json.loads(r.stdout)["id"], 7)

    def test_admin_fallback_sends_from(self):
        env = {k: v for k, v in self.env.items() if k != "BUS_TOKEN_FILE"}
        r = self.bus("send", "chief", "yo", env=env)  # no agent token file under HOME: admin token, from in body
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.sends()[0]
        self.assertEqual(s[3]["from"], "polisher")
        self.assertEqual(s[2], "Bearer " + ADMIN)

    def test_default_agent_token_file_used(self):
        agents = Path(self.tmp.name) / ".config/swarm/agents"
        agents.mkdir(parents=True)
        (agents / "polisher.token").write_text(SECRET + "\n")
        env = {k: v for k, v in self.env.items() if k != "BUS_TOKEN_FILE"}
        self.bus("send", "chief", "yo", env=env)
        self.assertEqual(self.sends()[0][2], "Bearer " + SECRET)

    def test_no_identity_fails_without_leaking(self):
        env = {k: v for k, v in self.env.items() if k not in ("BUS_TOKEN_FILE", "BUS_AGENT")}
        r = self.bus("send", "chief", "x", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(ADMIN, r.stdout + r.stderr)

    def test_ack_first_and_already_acked_exit_3(self):
        r = self.bus("ack", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("acked #7 (first)", r.stdout)
        self.assertIn("2 newer message(s) in thread dm:chief+polisher: read it first (swarmctl bus thread dm:chief+polisher)", r.stdout)
        Stub.ack_first = False
        r = self.bus("ack", "7")
        self.assertEqual(r.returncode, 3)
        self.assertIn("#7 was already acked by an earlier copy", r.stdout)

    def test_token_written_0600_and_never_printed(self):
        out = Path(self.tmp.name) / "deep" / "dir" / "polisher.token"
        r = self.bus("token", "polisher", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(out))
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertEqual(out.read_text(), SECRET)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(out.parent.stat().st_mode), 0o700)
        self.assertEqual(Stub.seen[-1][:3], ("GET", "/bus/agents/polisher/token", "Bearer " + ADMIN))
        self.assertEqual([p.name for p in out.parent.iterdir()], ["polisher.token"])  # no temp file left
        self.bus("token", "polisher", "--rotate", "--out", str(out))  # overwrites an existing file
        self.assertEqual(Stub.seen[-1][:2], ("POST", "/bus/agents/polisher/token/rotate"))

    def test_404_shows_did_you_mean(self):
        r = self.bus("status", "99")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 404", r.stderr)
        self.assertIn("did you mean: 98, 97", r.stderr)
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_unreachable_backend(self):
        env = dict(self.env, SWARM_URL="http://127.0.0.1:1")
        r = self.bus("inbox", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("network error", r.stderr)
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_register_body_and_digest_validation(self):
        r = self.bus("register", "polisher", "--kind", "bot", "--wake", "webhook", "--bot", "b1", "--orchestrator", "o1",
                     "--alias", "pol", "--alias", "p2", "--description", "d", "--notices", "bus")
        self.assertEqual(r.returncode, 0, r.stderr)
        m, path, auth, body = Stub.seen[-1]
        self.assertEqual((m, path, auth), ("PUT", "/bus/agents/polisher", "Bearer " + ADMIN))
        self.assertEqual(body, {"kind": "bot", "wake": "webhook", "digest_minutes": 60, "bot_id": "b1", "orchestrator": "o1",
                                "aliases": ["pol", "p2"], "description": "d", "notices": "bus"})
        self.bus("register", "polisher", "--kind", "bot", "--wake", "poll", "--digest-minutes", "5", "--notices", "legacy")
        self.assertEqual((Stub.seen[-1][3]["digest_minutes"], Stub.seen[-1][3]["notices"]), (5, "legacy"))
        n = len(Stub.seen)
        r = self.bus("register", "polisher", "--kind", "bot", "--wake", "poll", "--digest-minutes", "4")
        self.assertEqual(r.returncode, 2)
        self.assertIn("at least 5", r.stderr)
        r = self.bus("register", "polisher", "--kind", "bot", "--wake", "poll", "--notices", "other")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(Stub.seen), n)

    def test_retire_inbox_and_whoami(self):
        self.assertEqual(self.bus("retire", "polisher").returncode, 0)
        self.assertEqual(Stub.seen[-1][:2], ("DELETE", "/bus/agents/polisher"))
        r = self.bus("inbox", "--all", "--after", "3")
        self.assertIn("#7", r.stdout)
        self.assertIn("hello", r.stdout)
        self.assertNotIn("second", r.stdout)
        self.assertIn("1 newer in thread", r.stdout)
        self.assertEqual(Stub.seen[-1][1], "/bus/inbox?all=1&after=3")
        r = self.bus("whoami")
        self.assertEqual(r.returncode, 0)
        self.assertIn("identity: polisher", r.stdout)
        self.assertNotIn(SECRET, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
