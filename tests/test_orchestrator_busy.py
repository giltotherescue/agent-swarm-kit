"""0.8.1 (agent-control PR 64 and its review): queued messages reach an orchestrator during a long busy turn, and nothing
is ever typed onto a menu or someone's draft.

The real Supervisor runs its real step(), beat() and loop timing (next_wake) against a fake clock, a fake pane (the
orch-status answer) and a fake backend that keeps the heartbeat contract (receipt, then the oldest undelivered message
with its enqueue time `at`). No tmux, no network. Real Claude Code captures live in tests/fixtures/panes.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from helpers import SERVER

sys.path.insert(0, SERVER)
import swarm_orchestrator as so  # noqa: E402

PANES = Path(__file__).resolve().parent / "fixtures" / "panes"
RULE = "─" * 64
FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"


def cap(name):
    return (PANES / f"cap-{name}.txt").read_text()


def _pane(*input_lines, above="", below=FOOTER):
    return "⏺ Working.\n" + above + RULE + "\n" + "\n".join(input_lines) + "\n" + RULE + "\n" + below + "\n"


BUSY_PANE = _pane("❯ ", above="✻ Thinking… (esc to interrupt)\n")
MENU_PANE = "Which layout?\n ❯ 1. Grid\n   2. List\n"


class RealCapturesTest(unittest.TestCase):
    """Claude Code v2.1 panes captured on gil-worker-1 (the prompt is ❯ plus a no-break space)."""

    def test_captures(self):
        for name, draft, menu in (("menu", True, True), ("idle", False, False), ("bypass", False, False),
                                  ("busy", False, False), ("queued", False, False), ("cleared", False, False),
                                  ("draft", True, False), ("single", True, False)):
            self.assertEqual(so.input_draft(cap(name)), draft, name)
            self.assertEqual(so.pane_menu(cap(name)), menu, name)
        self.assertEqual(so.input_box(cap("draft")), "draft")   # positive: blocks the idle path too
        self.assertIsNone(so.input_box(cap("menu")))


class InputBoxTest(unittest.TestCase):
    def test_not_a_draft(self):
        for name, pane in {
            "empty": _pane("❯ " + " " * 40),
            "nbsp": _pane("❯ "),
            "placeholder": _pane('❯ Try "fix lint errors"'),
            "queued hint": _pane("❯ Press up to edit queued messages"),
            "queued shown above": _pane("❯ ", above="  fix the thing\n  ctrl+x ctrl+s to send now\n"),
            "queued line in box": _pane("❯ ", "Queued · ctrl+x ctrl+s to send now"),
            "boxed empty": "╭" + "─" * 40 + "╮\n│ ❯                │\n╰" + "─" * 40 + "╯\n",
            "boxed hint": "╭" + "─" * 40 + "╮\n│ ❯ Press up to edit queued messages │\n╰" + "─" * 40 + "╯\n",
        }.items():
            self.assertEqual(so.input_box(pane), "empty", name)

    def test_draft(self):
        for name, pane in {
            "single": _pane("❯ half-typed thought"),
            "multiline empty first": _pane("❯ ", "second line draft"),
            "multiline": _pane("❯ first", "  second"),
            "wrapped": _pane("❯ " + "word " * 30, "continued text"),
            "hint plus text": _pane('❯ Try "fix lint errors" and more'),
            "starts with Queued": _pane("❯ Queued the release for Friday"),
            "Queued alone": _pane("❯ Queued"),
            "pipe": _pane("❯ |"),
            "unboxed bars": _pane("❯ │ │"),
            "pasted rule in draft": _pane("❯ ", RULE, "draft text after a pasted rule"),
            "pasted rule only": _pane("❯ ", RULE),
            "quote line in draft": _pane("❯ ", "> quoted line"),
            "boxed text": "╭" + "─" * 40 + "╮\n│ ❯ hello          │\n╰" + "─" * 40 + "╯\n",
            "boxed missing bar": "╭" + "─" * 40 + "╮\n│ ❯                │\n  stray\n╰" + "─" * 40 + "╯\n",
            "text runs on below the rule": _pane("❯ ", below="\n".join(f"draft line {i}" for i in range(12))),
        }.items():
            self.assertEqual(so.input_box(pane), "draft", name)
            self.assertTrue(so.input_draft(pane), name)

    def test_unreadable(self):
        for name, pane in {"menu": MENU_PANE, "no rules": "⏺ x\n❯ \n", "no lower rule": "⏺ x\n" + RULE + "\n❯ \n",
                           "no prompt": "⏺ x\n", "nothing": ""}.items():
            self.assertIsNone(so.input_box(pane), name)
            self.assertTrue(so.input_draft(pane), name)   # the busy path never pastes here

    def test_menu(self):
        self.assertTrue(so.pane_menu(MENU_PANE))
        self.assertTrue(so.pane_menu("Do it?\n Esc to cancel"))
        self.assertFalse(so.pane_menu(BUSY_PANE))

    def test_urgent_is_read_from_the_first_line_only(self):
        self.assertTrue(so.is_urgent({"text": "URGENT bus #1 thread t | reply\n\nbody"}))
        self.assertFalse(so.is_urgent({"text": "bus #1 thread t\n\nURGENT in the body"}))


class Result:
    def __init__(self, rc=0, stdout="{}"):
        self.returncode, self.stdout, self.stderr = rc, stdout, ""


class FakeBackend:
    """The heartbeat contract of swarm_backend: a receipt marks a message delivered once (and records the busy event in
    that same first-delivery branch), the reply carries the oldest undelivered message with its enqueue time."""

    def __init__(self, clock):
        self.clock, self.msgs, self.events, self.beats = clock, [], [], 0
        self.fail = 0           # heartbeats that fail before the backend sees them
        self.lose_reply = 0     # heartbeats the backend processes but whose reply is lost

    def add(self, text, at=None):
        mid = len(self.msgs) + 1
        self.msgs.append({"id": mid, "at": int(self.clock() if at is None else at), "sender": "chief", "text": text % mid
                          if "%d" in text else text, "delivered": False})
        return mid

    def api(self, method, path, body=None, raw=None, timeout=60):
        assert path.endswith("/heartbeat"), path
        self.beats += 1
        if self.fail:
            self.fail -= 1
            raise urllib.error.URLError("down")
        ack = body.get("delivered_message_id")
        for m in self.msgs:
            if m["id"] == ack and not m["delivered"]:
                m["delivered"] = True
                self.events.append(("message-delivered", ack))
                if isinstance(body.get("delivered_busy_seconds"), int):
                    self.events.append(("message-delivered-busy", ack))
        self.events += [(e["type"], None) for e in body.get("events") or []]
        if self.lose_reply:
            self.lose_reply -= 1
            raise urllib.error.URLError("reply lost")
        head = next(({k: m[k] for k in ("id", "at", "sender", "text")} for m in self.msgs if not m["delivered"]), None)
        return {"desired": "running", "deleted": False, "generation": 1, "restart_generation": 0, "message": head,
                "config": {}}

    def delivered(self):
        return [m["id"] for m in self.msgs if m["delivered"]]

    def types(self, typ):
        return [mid for t, mid in self.events if t == typ]


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, attr, val in ((so.sw, "RUNNER_DIR", os.path.join(self.tmp, "runner")),
                                  (so.sw, "HANDOFF", os.path.join(self.tmp, "handoff")),
                                  (so.time, "time", lambda: self.now)):
            p = patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.be = FakeBackend(lambda: self.now)
        p = patch.object(so.sw, "api", self.be.api)
        p.start()
        self.addCleanup(p.stop)
        self.sup = so.Supervisor("steward")
        self.sup.agent = self.fake_agent
        self.sup.beat()
        self.pane, self.spinning = BUSY_PANE, True
        self.pasted, self.enters, self.typed = [], 0, []   # message ids pasted; Enter presses by orch-keys; texts sent
        self.send_rc, self.keys_rc = [], []                 # queued exit codes for orch-send / orch-keys (default 0)

    def fake_agent(self, *args, **kw):
        if args[0] == "orch-status":
            tail = f"✻ Working (esc to interrupt) {self.now}" if self.spinning else "done\n❯ "
            return Result(stdout=json.dumps({"alive": True, "dead": False, "tail": tail, "screen": self.pane, "pid": 7}))
        if args[0] == "orch-send":
            text = Path(self.sup.inbox, "message.md").read_text()
            rc = self.send_rc.pop(0) if self.send_rc else 0
            if rc not in so.NOT_PASTED:
                self.typed.append(text)
                mid = next((m["id"] for m in self.be.msgs if m["text"] in text), None)
                self.pasted.append(mid)
            return Result(rc)
        if args[0] == "orch-keys":
            rc = self.keys_rc.pop(0) if self.keys_rc else 0
            self.enters += rc == 0
            return Result(rc)
        if args[0] == "orch-kill":
            return Result()
        raise AssertionError(args)

    def run_loop(self, seconds):
        """The supervisor's own loop: step(), then sleep next_wake(). Returns the times of every step."""
        end, steps = self.now + seconds, []
        while self.now < end:
            self.sup.step()
            steps.append(self.now)
            self.now += self.sup.next_wake()
        return steps

    def paste_times(self):
        return [t for t, _ in self.paste_log]


class BusyDeliveryTest(LoopCase):
    def track(self):
        """Record when each paste happens (relative to the start)."""
        start, self.paste_log, real = self.now, [], self.fake_agent

        def agent(*args, **kw):
            n = len(self.pasted)
            r = real(*args, **kw)
            if len(self.pasted) > n:
                self.paste_log.append((round(self.now - start, 1), self.pasted[-1]))
            return r
        self.sup.agent = agent
        return start

    def test_busy_pane_delivers_after_max_wait_measured_from_enqueue(self):
        self.run_loop(3)
        self.track()
        self.be.add("bus #%d hello")
        self.run_loop(120)
        self.assertEqual(self.be.delivered(), [1])
        self.assertTrue(89 <= self.paste_times()[0] <= 91, self.paste_log)
        self.assertEqual(self.be.types("message-delivered-busy"), [1])

    def test_urgent_queued_just_after_a_poll_lands_at_about_15_seconds(self):
        self.run_loop(3)
        self.track()
        self.be.add("URGENT bus #%d thread t | reply\n\nstop")   # just after the heartbeat: found ~10 s later
        self.run_loop(40)
        self.assertTrue(14.5 <= self.paste_times()[0] <= 16, self.paste_log)

    def test_old_message_found_busy_is_pasted_at_once(self):
        self.be.add("bus #%d hello", at=self.now - 600)   # queued while nobody looked (enqueue time, not discovery)
        self.track()
        self.run_loop(15)
        self.assertLessEqual(self.paste_times()[0], 11, self.paste_log)

    def test_backlog_drains_at_the_gap_and_successors_skip_their_own_wait(self):
        self.run_loop(3)
        self.track()
        self.be.add("URGENT bus #%d first")
        self.be.add("bus #%d normal successor")
        self.be.add("bus #%d another")
        self.run_loop(60)
        times = self.paste_times()
        self.assertEqual([m for _, m in self.paste_log], [1, 2, 3])
        self.assertTrue(times[0] <= 16, times)
        self.assertTrue(4.5 <= times[1] - times[0] <= 6.5 and 4.5 <= times[2] - times[1] <= 6.5, times)

    def test_empty_receipt_reply_resets_the_backlog(self):
        self.run_loop(3)
        self.track()
        self.be.add("URGENT bus #%d first")
        self.run_loop(20)
        self.assertEqual(self.be.delivered(), [1])
        self.assertFalse(self.sup.draining)   # the receipt heartbeat came back empty
        self.be.add("bus #%d later, normal")
        self.run_loop(60)
        self.assertEqual(self.be.delivered(), [1])   # a new normal message waits its full 90 s, not the gap
        self.run_loop(40)
        self.assertEqual(self.be.delivered(), [1, 2])

    def test_null_reply_clears_the_cached_message(self):
        self.be.add("bus #%d hello")
        self.pane = _pane("❯ a draft")
        self.run_loop(100)
        self.assertIsNotNone(self.sup.message)
        self.be.msgs[0]["delivered"] = True   # expired or removed in the backend while the draft blocked it
        self.run_loop(15)
        self.assertIsNone(self.sup.message)
        self.pane = BUSY_PANE
        self.run_loop(120)
        self.assertEqual(self.pasted, [])

    def test_menu_and_draft_and_unreadable_box_block_busy_paste(self):
        self.be.add("bus #%d hello")
        for pane in (MENU_PANE, _pane("❯ half-typed thought"), _pane("❯ ", "second line draft"), "⏺ working\n❯ \n",
                     cap("draft"), cap("single"), cap("menu")):
            self.pane = pane
            self.run_loop(120)
            self.assertEqual(self.pasted, [], pane[-80:])
        self.pane = cap("queued")
        self.run_loop(15)
        self.assertEqual(self.pasted, [1])

    def test_blocked_message_does_not_spin_the_loop(self):
        self.be.add("bus #%d hello")
        self.pane = _pane("❯ draft")
        steps = self.run_loop(200)
        self.assertLess(len(steps), 200 / so.POLL + 15)

    def test_receipt_retry_without_repaste_and_one_busy_event(self):
        self.be.add("bus #%d hello")
        self.be.lose_reply = 1   # the backend stores the receipt, the reply is lost: the receipt is sent again
        self.run_loop(120)
        self.assertEqual(self.pasted, [1])
        self.assertEqual(self.be.types("message-delivered"), [1])
        self.assertEqual(self.be.types("message-delivered-busy"), [1])

    def test_failed_heartbeat_after_paste_retries_only_the_receipt(self):
        self.be.add("bus #%d hello")
        self.run_loop(85)
        self.be.fail = 3
        self.run_loop(60)
        self.assertEqual(self.pasted, [1])
        self.assertEqual(self.be.delivered(), [1])

    def test_paste_failure_retries_the_paste(self):
        self.be.add("bus #%d hello")
        self.send_rc = [5]   # tmux paste-buffer failed: nothing typed
        self.run_loop(120)
        self.assertEqual(self.pasted, [1])
        self.assertEqual(self.be.delivered(), [1])

    def test_failed_enter_is_retried_alone_and_acked_only_after_submit(self):
        self.be.add("bus #%d hello")
        self.send_rc, self.keys_rc = [6], [1] * 1000   # pasted, Enter failed; Enter retries keep failing
        self.run_loop(130)
        self.assertEqual(self.pasted, [1])
        self.assertEqual(self.be.delivered(), [])     # not acknowledged before the submit is confirmed
        self.assertEqual(self.typed and len(self.typed), 1)
        self.keys_rc = []                             # the next Enter goes through
        self.run_loop(15)
        self.assertEqual(self.pasted, [1])            # never pasted a second time
        self.assertEqual(self.enters, 1)
        self.assertEqual(self.be.delivered(), [1])
        self.assertEqual(self.be.types("message-delivered-busy"), [1])

    def test_uncertain_send_never_repastes(self):
        self.be.add("bus #%d hello")
        real = self.fake_agent

        def agent(*args, **kw):
            if args[0] == "orch-send":
                real(*args, **kw)
                raise so.subprocess.TimeoutExpired("agent-exec", 60)
            return real(*args, **kw)
        self.sup.agent = agent
        self.run_loop(130)
        self.assertEqual(self.pasted, [1])
        self.assertEqual(self.be.delivered(), [1])


class IdleAndStopTest(LoopCase):
    def go_idle(self):
        self.spinning = False

    def test_idle_empty_prompt_delivers_immediately_without_busy_event(self):
        self.go_idle()
        self.pane = cap("idle")
        self.run_loop(25)   # the pane is quiet for IDLE seconds
        self.be.add("bus #%d hello")
        start = self.now
        self.run_loop(15)
        self.assertEqual(self.be.delivered(), [1])
        self.assertLessEqual(self.sup.tail_changed - start, 11)
        self.assertEqual(self.be.types("message-delivered-busy"), [])

    def test_idle_unknown_screen_still_delivers(self):
        self.go_idle()
        self.pane = "fake claude ready\n"   # no Claude Code input box (as in the tmux tests)
        self.be.add("bus #%d hello")
        self.run_loop(40)
        self.assertEqual(self.be.delivered(), [1])

    def test_stationary_menu_or_draft_never_gets_an_idle_paste(self):
        self.go_idle()
        self.be.add("bus #%d hello")
        for pane in (MENU_PANE, cap("menu"), cap("draft"), cap("single"), _pane("❯ ", RULE, "after a pasted rule")):
            self.pane = pane
            self.run_loop(200)
            self.assertEqual((self.pasted, self.enters), ([], 0), pane[-80:])
        self.pane = cap("cleared")
        self.run_loop(30)
        self.assertEqual(self.pasted, [1])

    def test_wrap_up_is_not_sent_onto_a_menu_or_draft(self):
        self.go_idle()
        for pane in (MENU_PANE, cap("draft")):
            self.pane = self.sup.screen = pane
            self.sup.state = "running"
            self.sup.begin_stop("the desired state is stopped")
            self.assertEqual((self.pasted, self.typed, self.enters), ([], [], 0))
            self.assertEqual(self.sup.stop_sent_at, 0)   # falls back to the stop deadline
            self.assertEqual(self.sup.stop_deadline, self.now + so.STOP_GRACE)

    def test_wrap_up_is_sent_on_an_empty_prompt(self):
        self.go_idle()
        self.pane = self.sup.screen = cap("idle")
        self.sup.begin_stop("the desired state is stopped")
        self.assertEqual(len(self.typed), 1)
        self.assertIn("stopping this session", self.typed[0])
        self.assertEqual(self.sup.stop_sent_at, self.now)

    def test_wrap_up_with_failed_enter_retries_enter_once(self):
        self.go_idle()
        self.pane = self.sup.screen = cap("idle")
        self.send_rc = [6]
        self.sup.begin_stop("restart requested by an admin", then="start")
        self.assertEqual((len(self.typed), self.enters), (1, 1))
        self.assertEqual(self.sup.stop_sent_at, self.now)


if __name__ == "__main__":
    unittest.main()
