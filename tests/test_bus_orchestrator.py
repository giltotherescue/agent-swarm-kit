"""Agent bus parts of the supervisor: idle message polling, bus token file, session environment, message cleaning.
No tmux, no backend: the backend call and agent-exec are replaced."""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import swarm_orchestrator as so  # noqa: E402
import swarm_worker as sw  # noqa: E402

SECRET = "ab_steward.1." + "e" * 40   # stand-in, not a real token
# A real empty Claude Code input box: the guard fails closed, so an empty screen would get nothing.
IDLE_SCREEN = open(os.path.join(HERE, "fixtures", "panes", "cap-idle.txt")).read()


class Done:
    returncode = 0
    stdout = "{}"
    stderr = ""


class BusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for attr, val in (("RUNNER_DIR", os.path.join(self.tmp, "runner")), ("HANDOFF", os.path.join(self.tmp, "handoff"))):
            p = patch.object(so.sw, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.sup = so.Supervisor("steward")
        self.sup.config = {"project": "app"}
        self.api_calls = []
        self.api_result = {"token": SECRET}
        p = patch.object(so.sw, "api", self.fake_api)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        self.sup.agent = self.fake_agent

    def fake_api(self, method, path, body=None, raw=None, timeout=60):
        self.api_calls.append((method, path))
        if isinstance(self.api_result, Exception):
            raise self.api_result
        return self.api_result

    def fake_agent(self, *args, **kw):
        if args[0] == "orch-status":
            r = Done()
            r.stdout = json.dumps({"alive": True, "dead": False, "tail": "", "screen": IDLE_SCREEN, "pid": 1})
            return r
        if args[0] == "orch-capture":
            r = Done()
            r.stdout = json.dumps({"pane": "%1", "screen": IDLE_SCREEN})
            return r
        if args[0] == "orch-type":   # what reaches the session: the message file when there is one, else the line
            name = "message.md" if "--id" in args else "line.txt"
            with open(os.path.join(self.sup.inbox, name)) as fh:
                self.sent.append(fh.read())
        if args[0] == "orch-enter":
            r = Done()
            r.stdout = json.dumps({"state": "submitted"})
            return r
        return Done()

    def ready(self, message=None):
        self.sup.state, self.sup.desired = "running", "running"
        self.sup.tail_changed = time.time() - 100     # idle
        self.sup.message = message
        os.makedirs(self.sup.inbox, exist_ok=True)

    def step_idle(self, beats):
        """Run the idle branch of step() with a fake status and a fake heartbeat."""
        st = {"alive": True, "dead": False, "tail": "", "screen": IDLE_SCREEN, "pid": 1}
        self.sup.status = lambda: st

        def beat(release=False):
            beats.append(time.time())
            self.sup.last_beat = time.time()
            return True
        self.sup.beat = beat
        self.sup.step()

    def test_default_poll_is_10_seconds(self):
        self.assertEqual(so.MESSAGE_POLL, 10)

    def test_idle_poll_between_heartbeats(self):
        self.ready()
        self.sup.last_beat = time.time() - 12
        beats = []
        self.step_idle(beats)
        self.assertEqual(len(beats), 1)       # well before the 30 s heartbeat
        self.step_idle(beats)
        self.assertEqual(len(beats), 1)       # interval respected

    def test_busy_poll_only_times_the_wait(self):
        """0.8.1: a busy pane is polled too (to time how long a message waited), but nothing is typed."""
        self.ready()
        self.sup.tail_changed = time.time()
        self.sup.last_beat = time.time() - 12
        beats = []
        self.step_idle(beats)
        self.assertEqual(len(beats), 1)
        self.assertEqual(self.sent, [])

    def test_polled_message_is_delivered(self):
        self.ready()
        self.sup.last_beat = time.time() - 12
        beats = []

        def beat(release=False):
            beats.append(1)
            self.sup.last_beat = time.time()
            if len(beats) == 1:
                self.sup.message = {"id": 5, "sender": "chief", "text": "[bus #5 from=chief] hello"}
            return True
        self.sup.status = lambda: {"alive": True, "dead": False, "tail": "", "screen": IDLE_SCREEN, "pid": 1}
        self.sup.beat = beat
        self.sup.step()
        self.assertEqual(self.sup.delivered, 5)
        self.assertIn("[bus #5 from=chief] hello", self.sent[0])

    def test_token_written_0600_atomically_and_not_printed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            path = self.sup.fetch_bus_token()
        self.assertEqual(self.api_calls, [("GET", "/bus/agents/steward/token?worker=" + sw.NAME)])
        self.assertEqual(path, os.path.join(self.sup.dir, "steward.bus-token"))
        self.assertEqual(open(path).read(), SECRET)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(self.sup.dir) if f.endswith(".tmp") and "bus" in f], [])
        self.assertNotIn(SECRET, out.getvalue())

    def test_404_skips_silently_and_removes_stale_file(self):
        stale = os.path.join(self.sup.dir, "steward.bus-token")
        open(stale, "w").write("old")
        self.api_result = urllib.error.HTTPError("u", 404, "nf", {}, None)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertIsNone(self.sup.fetch_bus_token())
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.exists(stale))

    def test_other_errors_log_without_the_token(self):
        self.api_result = urllib.error.HTTPError("u", 500, "boom", {}, None)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertIsNone(self.sup.fetch_bus_token())
        self.assertIn("no bus token (HTTP 500)", out.getvalue())

    def test_environment_exports_and_prompt_note(self):
        self.assertEqual(self.sup.bus_wrap(["claude", "x"]), ["claude", "x"])
        self.assertNotIn("swarmctl bus", self.sup.prompt("start"))
        self.sup.bus_token_file = "/state/steward.bus-token"
        cmd = self.sup.bus_wrap(["claude", "x"])
        for want in ("BUS_AGENT=steward", "BUS_TOKEN_FILE=/state/steward.bus-token", f"SWARM_URL={so.sw.URL}", so.BIN_DIR):
            self.assertIn(want, cmd)
        self.assertEqual(cmd[-2:], ["claude", "x"])
        self.assertTrue(os.path.exists(os.path.join(so.BIN_DIR, "swarmctl")))
        note = self.sup.prompt("start")
        for want in ("[bus #ID", "swarmctl bus send AGENT", "swarmctl bus ack ID", "exit 3", "reply_expected"):
            self.assertIn(want, note)

    def test_control_characters_never_reach_the_pane(self):
        evil = "ok\x1b[201~rm -rf /\r\nline2\ttab\x1b\x07\x00\x85\x9b\x7f end"
        self.ready({"id": 6, "sender": "chief\x1b[201~", "text": evil})
        self.sup.deliver()
        text = self.sent[0]
        for bad in ("\x1b", "\r", "\x07", "\x00", "\x85", "\x9b", "\x7f", " "):
            self.assertNotIn(bad, text)
        self.assertIn("ok[201~rm -rf /\nline2\ttab", text)

    def test_clean_text_unit(self):
        self.assertEqual(so.clean_text("a\x1b[201~b\rc\x1bd\x80e\x9ff\n\tg"), "a[201~bcdef\n\tg")

    def test_long_message_goes_whole_into_the_file(self):
        self.ready({"id": 7, "sender": "chief", "text": "bus #9 " + "a" * 20000})
        out = io.StringIO()
        with redirect_stdout(out):
            self.sup.deliver()
        self.assertIn("a" * 20000 + "\n", self.sent[0])   # nothing cut: the pointer line stays short
        self.assertEqual(len(self.sent), 1)

if __name__ == "__main__":
    unittest.main()
