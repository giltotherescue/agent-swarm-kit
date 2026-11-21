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
from swarm_backend import ORCH_EVENT_TYPES  # noqa: E402

PANES = Path(__file__).resolve().parent / "fixtures" / "panes"
RULE = "─" * 64
FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"


def cap(name):
    return (PANES / f"cap-{name}.txt").read_text()


def _pane(*input_lines, above="", below=FOOTER):
    return "⏺ Working.\n" + above + RULE + "\n" + "\n".join(input_lines) + "\n" + RULE + "\n" + below + "\n"


BUSY_PANE = _pane("❯ ", above="✻ Thinking… (esc to interrupt)\n")
MENU_PANE = "Which layout?\n ❯ 1. Grid\n   2. List\n"
PASTED_PANE = _pane("❯ [Pasted text #1 +3 lines]")


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
            "nbsp": _pane("❯\u00a0"),
            "queued hint": _pane("❯ Press up to edit queued messages"),
            "queued shown above": _pane("❯ ", above="  fix the thing\n  ctrl+x ctrl+s to send now\n"),
            "no footer (box at the pane end)": _pane("❯ ", below=""),
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
            "Try placeholder or typed text": _pane('❯ Try "restarting the service"'),
            "Try fix lint": _pane('❯ Try "fix lint errors"'),
            "hint plus text": _pane('❯ Press up to edit queued messages and more'),
            "unverified Queued hint": _pane("❯ Queued · ctrl+x ctrl+s to send now"),
            "queued line in box": _pane("❯ ", "Queued · ctrl+x ctrl+s to send now"),
            "starts with Queued": _pane("❯ Queued the release for Friday"),
            "Queued alone": _pane("❯ Queued"),
            "pipe": _pane("❯ |"),
            "unboxed bars": _pane("❯ │ │"),
            "pasted rule in draft": _pane("❯ ", RULE, "draft text after a pasted rule"),
            "pasted rule only": _pane("❯ ", RULE),
            "quote line in draft": _pane("❯ ", "> quoted line"),
            # the last prompt and nearest rule would redefine the box: an indented rule and ">" inside a draft
            "indented rule and > inside a draft": RULE + "\n❯ unfinished draft\n  " + "─" * 40 + "\n>\n" + RULE + "\n" + FOOTER,
            # a full-width rule pasted inside a draft: two candidate boxes
            "identical rule inside a draft": RULE + "\n❯ unfinished draft\n" + RULE + "\n❯ \n" + RULE + "\n" + FOOTER,
            "boxed text": "╭" + "─" * 40 + "╮\n│ ❯ hello          │\n╰" + "─" * 40 + "╯\n",
            "boxed missing bar": "╭" + "─" * 40 + "╮\n│ ❯                │\n  stray\n╰" + "─" * 40 + "╯\n",
        }.items():
            self.assertEqual(so.input_box(pane), "draft", name)
            self.assertTrue(so.input_draft(pane), name)
            self.assertEqual(so.send_block(pane), "draft", name)

    def test_bottom_must_close_the_pane_or_a_known_footer(self):
        """Probes: one unknown line under the single rule (the real bottom missing), too many footer lines, or the
        bottom far below the prompt are not an empty box."""
        for name, pane, want in (
                ("first footer line is prose", _pane("❯ ", below="my unfinished draft"), "unknown_footer"),
                ("nine unknown footer lines", _pane("❯ ", below="\n".join(f"status {i}" for i in range(9))), "unknown_footer"),
                ("text runs on below the rule", _pane("❯ ", below="\n".join(f"draft line {i}" for i in range(12))), "unknown_footer"),
                ("too many known footer lines", _pane("❯ ", below="\n".join([FOOTER] * 9)), "unknown_footer"),
                ("bottom 22 rows below the prompt", "⏺ x\n" + RULE + "\n❯ \n" + "\n" * 22 + RULE + "\n" + FOOTER + "\n", None)):
            self.assertEqual(so.input_box(pane), want, name)
            for busy in (False, True):
                self.assertIsNotNone(so.send_block(pane, busy=busy), name)
                self.assertIsNotNone(so.send_block(pane, busy=busy, plain=True), name)

    def test_cap_draft_minus_bottom_rule_still_shows_the_draft(self):
        lines = cap("draft").rstrip("\n").splitlines()
        bottom = max(i for i, ln in enumerate(lines) if so.INPUT_RULE.match(ln))
        pane = "\n".join(lines[:bottom] + lines[bottom + 1:])
        self.assertIn("draft line two", pane)
        self.assertNotEqual(so.input_box(pane), "empty")
        self.assertIsNotNone(so.send_block(pane))

    def test_unreadable(self):
        for name, pane in {"menu": MENU_PANE, "no rules": "⏺ x\n❯ \n", "no lower rule": "⏺ x\n" + RULE + "\n❯ \n",
                           "no prompt": "⏺ x\n", "nothing": "", "fake claude ready": "fake claude ready\n"}.items():
            self.assertIsNone(so.input_box(pane), name)
            self.assertTrue(so.input_draft(pane), name)   # the busy path never pastes here

    def test_fail_closed_unless_an_explicit_stand_in(self):
        """Idle and wrap-up need a confirmed empty box too. Only the explicit plain adapter lets a readable box-less
        screen through (idle only); an empty or failed capture is never a stand-in's screen."""
        stand_in = "fake claude ready\n"
        self.assertEqual(so.send_block(stand_in), "unreadable")
        self.assertEqual(so.send_block(stand_in, busy=True), "unreadable")
        self.assertIsNone(so.send_block(stand_in, plain=True))
        self.assertEqual(so.send_block(stand_in, busy=True, plain=True), "unreadable")
        for nothing in ("", "\n\n  \n", None):
            self.assertEqual(so.send_block(nothing, plain=True), "unreadable")
        self.assertEqual(so.send_block(MENU_PANE, plain=True), "menu")
        self.assertEqual(so.send_block(_pane("❯ draft"), plain=True), "draft")
        self.assertIsNone(so.send_block(cap("idle")))

    def test_menu(self):
        self.assertTrue(so.pane_menu(MENU_PANE))
        self.assertTrue(so.pane_menu("Do it?\n Esc to cancel"))
        self.assertFalse(so.pane_menu(BUSY_PANE))

    def test_historical_menu_words_above_an_empty_box_do_not_block(self):
        lines = cap("idle").splitlines()
        top = next(i for i, ln in enumerate(lines) if so.INPUT_RULE.match(ln))
        pane = "\n".join(lines[:top] + ["⏺ The installer says Enter to confirm.", "  ❯ 1. an old numbered list"] + lines[top:])
        self.assertFalse(so.pane_menu(pane))
        self.assertIsNone(so.send_block(pane))
        # without a readable box below it, the same words still count as a menu
        self.assertTrue(so.pane_menu("⏺ The installer says Enter to confirm.\n"))

    def test_urgent_is_read_from_the_first_line_only(self):
        self.assertTrue(so.is_urgent({"text": "URGENT bus #1 thread t | reply\n\nbody"}))
        self.assertFalse(so.is_urgent({"text": "bus #1 thread t\n\nURGENT in the body"}))


TRUST_YES = cap("menu").replace(" ❯ No, exit", "   No, exit").replace("   Yes, I trust this folder", " ❯ Yes, I trust this folder")


class TrustScreenTest(unittest.TestCase):
    """Claude Code's current first-run folder trust screen (real capture cap-menu): "❯ No, exit" is preselected."""

    def test_new_wording_moves_down_first(self):
        self.assertIn("Quick safety check", cap("menu"))
        self.assertEqual(so.classify(cap("menu")), ("first-run", "the folder trust question (moving to Yes)", ["Down"]))

    def test_enter_only_with_the_cursor_on_yes(self):
        self.assertIn(" ❯ Yes, I trust this folder", TRUST_YES)
        self.assertEqual(so.classify(TRUST_YES), ("first-run", "the folder trust question", ["Enter"]))

    def test_old_wording_still_answered(self):
        self.assertEqual(so.classify("Do you trust the files in this folder?\n ❯ 1. Yes, proceed\n   2. No, exit\nEnter to confirm"),
                         ("first-run", "the folder trust question", ["Enter"]))

    def test_unrecognized_cursor_is_not_answered(self):
        pane = cap("menu").replace(" ❯ No, exit", "   No, exit")
        self.assertEqual(so.classify(pane)[0], "block")

    def test_login_screen_is_never_answered(self):
        for pane in (cap("menu") + "\nSelect login method:\n 1. Claude account\n", "Invalid API key · Please run /login\n"):
            self.assertEqual(so.classify(pane)[0], "login")


class Result:
    def __init__(self, rc=0, stdout="{}"):
        self.returncode, self.stdout, self.stderr = rc, stdout, ""


class FakeBackend:
    """The heartbeat contract of swarm_backend: a receipt marks a message delivered once (and records the busy event in
    that same first-delivery branch), the reply carries the oldest undelivered message with its enqueue time."""

    def __init__(self, clock):
        self.clock, self.msgs, self.events, self.texts, self.beats = clock, [], [], [], 0
        self.on_beat = None     # called before each heartbeat is answered (the pane may change during network work)
        self.fail = 0           # heartbeats that fail before the backend sees them
        self.lose_reply = 0     # heartbeats the backend processes but whose reply is lost
        self.restart_generation = 0

    def add(self, text, at=None):
        mid = len(self.msgs) + 1
        self.msgs.append({"id": mid, "at": int(self.clock() if at is None else at), "sender": "chief", "text": text % mid
                          if "%d" in text else text, "delivered": False})
        return mid

    def api(self, method, path, body=None, raw=None, timeout=60):
        assert path.endswith("/heartbeat"), path
        self.beats += 1
        if self.on_beat:
            self.on_beat()
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
        for e in body.get("events") or []:
            if e["type"] not in ORCH_EVENT_TYPES:
                raise urllib.error.HTTPError(path, 400, f"bad event type {e['type']!r}", {}, None)
            self.events.append((e["type"], None))
            self.texts.append(e["text"])
        if self.lose_reply:
            self.lose_reply -= 1
            raise urllib.error.URLError("reply lost")
        head = next(({k: m[k] for k in ("id", "at", "sender", "text")} for m in self.msgs if not m["delivered"]), None)
        return {"desired": "running", "deleted": False, "generation": 1, "restart_generation": self.restart_generation, "message": head,
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
        self.keys, self.killed, self.on_keys, self.status_fail = [], 0, None, 0   # status_fail: next N orch-status fail
        self.send_raise_before = self.send_raise_after = None
        self.pane_before, self.after_send = None, None   # after_send(): the pane changes right after orch-send returns

    def fake_agent(self, *args, **kw):
        """agent-exec as the supervisor sees it. A paste that is not submitted (exit 6 or 7) leaves the text in the box,
        like Claude Code; a successful Enter on it clears the box."""
        if args[0] == "orch-status":
            if self.status_fail:
                self.status_fail -= 1
                raise RuntimeError("agent-exec orch-status failed (1)")   # as Supervisor.agent(check=True) raises
            tail = f"✻ Working (esc to interrupt) {self.now}" if self.spinning else "done\n❯ "
            return Result(stdout=json.dumps({"alive": True, "dead": False, "tail": tail, "screen": self.pane, "pid": 7}))
        if args[0] == "orch-send":
            if self.send_raise_before:
                raise self.send_raise_before
            text = Path(self.sup.inbox, "message.md").read_text()
            rc = self.send_rc.pop(0) if self.send_rc else 0
            if rc not in so.NOT_PASTED:
                self.typed.append(text)
                mid = next((m["id"] for m in self.be.msgs if m["text"] in text), None)
                self.pasted.append(mid)
                if rc != 0:
                    self.pane_before, self.pane = self.pane, PASTED_PANE
            if self.after_send:
                self.after_send()
            if self.send_raise_after:
                raise self.send_raise_after
            return Result(rc)
        if args[0] == "orch-keys":
            rc = self.keys_rc.pop(0) if self.keys_rc else 0
            if rc == 0:
                self.keys.append(list(args[2:]))
                self.enters += "Enter" in args[2:]
                if "Enter" in args[2:] and self.pane is PASTED_PANE:
                    self.pane = self.pane_before
                if self.on_keys:
                    self.on_keys(list(args[2:]))
            return Result(rc)
        if args[0] == "orch-kill":
            self.killed += 1
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

    def test_unknown_outcome_with_empty_box_is_held_not_acknowledged(self):
        """orch-send timed out after Claude Code took the text (box empty again): maybe submitted, maybe not. No receipt,
        no Enter, never pasted again; one delivery-unknown event."""
        self.be.add("bus #%d hello")
        self.send_raise_after = so.subprocess.TimeoutExpired("agent-exec", 60)
        self.run_loop(130)
        self.send_raise_after = None
        self.run_loop(200)
        self.assertEqual(self.pasted, [1])
        self.assertEqual(self.enters, 0)
        self.assertEqual(self.be.delivered(), [])
        self.assertEqual(self.be.types("delivery-unknown"), [None])
        self.assertIn(1, self.sup.held)

    def test_unknown_outcome_with_the_text_still_in_the_box_gets_enter(self):
        self.be.add("bus #%d hello")
        self.send_rc = [6]   # the fake leaves the text in the box ...
        self.send_raise_after = so.subprocess.TimeoutExpired("agent-exec", 60)   # ... and the outcome is unknown
        self.run_loop(110)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([1], 1, [1]))
        self.assertEqual(self.be.types("delivery-unknown"), [])

    def test_pre_launch_failure_is_not_acknowledged_and_is_pasted_later(self):
        """Probe: OSError before orch-send started used to give 0 pastes, 1 Enter and a receipt."""
        self.be.add("bus #%d hello")
        self.send_raise_before = OSError("agent-exec: no such file")
        self.run_loop(130)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([], 0, []))
        self.send_raise_before = None
        self.run_loop(15)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([1], 0, [1]))

    def test_enter_withheld_by_orch_send_is_not_pressed_onto_a_menu(self):
        self.be.add("bus #%d hello")
        self.send_rc = [7]   # pasted, then orch-send saw the pane change and withheld Enter

        def to_menu():
            self.pane, self.after_send = MENU_PANE, None
        self.after_send = to_menu
        self.run_loop(300)
        self.assertEqual(self.pasted, [1])
        self.assertEqual((self.enters, self.be.delivered()), (0, []))
        self.assertEqual(self.be.types("delivery-deferred"), [None])
        self.pane = PASTED_PANE
        self.run_loop(15)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([1], 1, [1]))

    def test_confirmed_paste_whose_text_is_gone_is_held(self):
        self.be.add("bus #%d hello")
        self.send_rc = [6]

        def emptied():
            self.pane = BUSY_PANE   # the box is empty again: the text went somewhere, unknown where
        self.after_send = emptied
        self.run_loop(160)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([1], 0, []))
        self.assertEqual(self.be.types("delivery-unknown"), [None])

    def test_pane_changes_during_the_message_poll(self):
        """Probe: the pane turned into the trust menu inside the message-poll heartbeat; the paste used the old capture."""
        self.spinning = False
        self.pane = cap("idle")
        self.run_loop(25)
        self.be.add("bus #%d hello")

        def flip():
            self.pane = MENU_PANE
        self.be.on_beat = flip
        self.run_loop(30)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([], 0, []))

    def test_older_backend_refusing_the_new_event_types_does_not_stop_the_session(self):
        self.run_loop(3)
        self.be.add("URGENT bus #%d stop")
        self.pane = _pane("❯ draft")
        with patch(__name__ + ".ORCH_EVENT_TYPES", tuple(t for t in ORCH_EVENT_TYPES if t not in so.NEW_EVENT_TYPES)):
            self.run_loop(200)
        self.assertEqual(self.sup.state, "running")
        self.assertIsNone(self.sup.blocked)
        self.assertEqual(self.be.types("delivery-deferred"), [])
        self.assertFalse(any(e["type"] == "delivery-deferred" for e in self.sup.events))

    def test_sustained_deferral_is_reported_once_per_reason(self):
        """Probe: empty box, nine unknown footer lines, urgent message, 200 s: used to stall with no event."""
        self.run_loop(3)
        self.be.add("URGENT bus #%d stop")
        self.pane = _pane("❯ ", below="\n".join(f"secret-looking status {i}" for i in range(9)))
        self.run_loop(200)
        self.assertEqual(self.pasted, [])
        self.assertEqual(self.be.types("delivery-deferred"), [None])
        text = [t for t in self.be.texts if "reason=" in t]
        self.assertEqual(text, ["message 1 held for %s s: reason=unknown_footer" % text[0].split()[4]])
        self.assertNotIn("status", text[0])   # never pane contents
        self.pane = _pane("❯ draft")
        self.run_loop(200)
        self.assertEqual(self.be.types("delivery-deferred"), [None, None])   # a new reason: one more event
        self.assertTrue(self.be.texts[-1].endswith("reason=draft"))
        self.run_loop(400)
        self.assertEqual(len(self.be.types("delivery-deferred")), 2)
        self.pane = BUSY_PANE
        self.run_loop(15)
        self.assertEqual(self.pasted, [1])   # never timed out into a paste before


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

    def test_idle_box_less_screen_is_refused_and_reported(self):
        """Fail closed like agent-control PR 67: a screen with no Claude Code input box gets nothing."""
        self.go_idle()
        self.pane = "fake claude ready\n"
        self.be.add("bus #%d hello")
        self.run_loop(200)
        self.assertEqual((self.pasted, self.be.delivered()), ([], []))
        self.assertTrue(self.be.texts[-1].endswith("reason=unreadable"), self.be.texts)

    def test_idle_box_less_stand_in_delivers_only_with_the_plain_adapter(self):
        self.go_idle()
        self.pane = "fake claude ready\n"
        self.be.add("bus #%d hello")
        with patch.object(so, "PLAIN", True):
            self.run_loop(40)
        self.assertEqual(self.be.delivered(), [1])

    def test_plain_adapter_never_pastes_onto_a_failed_or_empty_capture(self):
        self.go_idle()
        with patch.object(so, "PLAIN", True):
            self.pane = "fake claude ready\n"
            self.run_loop(25)
            self.be.add("bus #%d hello")
            for pane in ("", "\n\n", "fake claude ready\n" + RULE + "\n❯ \n"):
                self.pane = pane
                self.run_loop(60)
                self.assertEqual(self.pasted, [], repr(pane))
            self.pane = "fake claude ready\n"

            def fail_capture():
                self.status_fail = 1   # step()'s status read worked; the send-time capture fails
            self.be.lose_reply, self.be.on_beat = 0, fail_capture
            self.sup.message = None
            self.now += so.MESSAGE_POLL
            self.sup.step()
            self.assertEqual((self.sup.message or {}).get("id"), 1)
            self.assertEqual(self.pasted, [])
            self.be.on_beat = None
            self.run_loop(15)
        self.assertEqual(self.pasted, [1])

    def test_stationary_menu_or_draft_never_gets_an_idle_paste(self):
        self.go_idle()
        self.be.add("bus #%d hello")
        for pane in (MENU_PANE, cap("draft"), cap("single"), _pane("❯ ", RULE, "after a pasted rule"),
                     _pane('❯ Try "restarting the service"')):
            self.pane = pane
            self.run_loop(200)
            self.assertEqual((self.pasted, self.enters), ([], 0), pane[-80:])
        self.pane = cap("cleared")
        self.run_loop(30)
        self.assertEqual(self.pasted, [1])

    def test_trust_screen_gets_down_then_enter_on_yes(self):
        """Real capture cap-menu: "❯ No, exit" preselected. Down, then Enter only once the ❯ is on the Yes line."""
        self.go_idle()
        self.be.add("bus #%d hello")
        self.pane = cap("menu")

        def keys(k):
            if k == ["Down"] and self.pane == cap("menu"):
                self.pane = TRUST_YES
            elif k == ["Enter"] and self.pane == TRUST_YES:
                self.pane = cap("idle")
        self.on_keys = keys
        self.run_loop(60)
        self.assertEqual(self.keys, [["Down"], ["Enter"]])
        self.assertEqual(self.be.delivered(), [1])   # the message waited behind the screen, then went in
        self.assertEqual(self.killed, 0)

    def test_trust_screen_that_ignores_down_never_gets_enter(self):
        self.go_idle()
        self.pane = cap("menu")
        self.run_loop(60)
        self.assertNotIn(["Enter"], self.keys)
        self.assertEqual(self.keys, [["Down"]] * so.FIRST_RUN_TRIES)
        self.assertEqual(self.sup.state, "needs-login")   # stays at the screen: stopped for an admin

    def test_login_screen_is_never_answered(self):
        self.go_idle()
        self.be.add("bus #%d hello")
        self.pane = cap("menu").rstrip() + "\n Select login method:\n ❯ 1. Claude account\n"
        self.run_loop(60)
        self.assertEqual((self.keys, self.pasted), ([], []))
        self.assertEqual(self.sup.state, "needs-login")

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


class WrapUpTest(LoopCase):
    def setUp(self):
        super().setUp()
        self.spinning = False

    def test_restart_heartbeat_captures_the_pane_again(self):
        """Probe: cached cap-idle, live trust menu, restart_generation incremented on the next heartbeat: the wrap-up was
        pasted using the stale capture."""
        self.pane = cap("idle")
        self.run_loop(25)
        self.pane = MENU_PANE
        self.be.restart_generation = 1
        self.now += so.HEARTBEAT
        self.sup.step()
        self.assertEqual(self.sup.state, "stopping")
        self.assertEqual((self.typed, self.enters), ([], 0))
        self.assertEqual(self.sup.stop_sent_at, 0)
        self.assertEqual(self.sup.stop_deadline, self.now + so.STOP_GRACE)

    def test_wrap_up_skipped_when_the_capture_fails(self):
        self.pane = self.sup.screen = cap("idle")
        self.status_fail = True
        self.sup.begin_stop("the desired state is stopped")
        self.assertEqual((self.typed, self.enters, self.sup.stop_sent_at), ([], 0, 0))
        self.assertEqual(self.sup.stop_deadline, self.now + so.STOP_GRACE)
        self.assertIn("reason=unreadable", self.sup.events[-1]["text"])

    def test_wrap_up_onto_a_box_less_screen_is_skipped(self):
        self.pane = self.sup.screen = "fake claude ready\n"
        self.sup.begin_stop("the desired state is stopped")
        self.assertEqual((self.typed, self.sup.stop_sent_at), ([], 0))
        with patch.object(so, "PLAIN", True):
            self.sup.begin_stop("the desired state is stopped")
        self.assertEqual(len(self.typed), 1)

    def test_wrap_up_unknown_outcome_with_empty_box_gets_no_enter(self):
        self.pane = self.sup.screen = cap("idle")
        self.send_raise_after = so.subprocess.TimeoutExpired("agent-exec", 60)
        self.sup.begin_stop("restart requested by an admin", then="start")
        self.assertEqual((len(self.typed), self.enters, self.sup.stop_sent_at), (1, 0, 0))

    def test_wrap_up_enter_withheld_onto_a_menu(self):
        self.pane = self.sup.screen = cap("idle")
        self.send_rc = [7]
        real = self.fake_agent

        def agent(*args, **kw):
            r = real(*args, **kw)
            if args[0] == "orch-send":
                self.pane = MENU_PANE   # the pane became a menu after the paste
            return r
        self.sup.agent = agent
        self.sup.begin_stop("restart requested by an admin", then="start")
        self.assertEqual((len(self.typed), self.enters, self.sup.stop_sent_at), (1, 0, 0))


def load_agent_exec():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("agent_exec_busy", os.path.join(SERVER, "agent-exec"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_exec_busy", loader))
    loader.exec_module(mod)
    return mod


class OrchSendEnterCheckTest(unittest.TestCase):
    """agent-exec orch-send looks at the pane between the paste and Enter (exit 7 withholds Enter)."""

    @classmethod
    def setUpClass(cls):
        cls.ae = load_agent_exec()

    def check(self, screen, plain=False):
        return self.ae.pasted_still_there(screen, "Message from chief (through the swarm backend)\n\nhello", plain)

    def test_pasted_text_in_the_box_gets_enter(self):
        for pane in (cap("draft"), cap("single"), PASTED_PANE, _pane("❯ Message from chief (through the swarm backend)", "", "  hello")):
            self.assertIsNone(self.check(pane), pane[-120:])

    def test_menu_or_gone_text_withholds_enter(self):
        for pane in (cap("menu"), MENU_PANE, cap("idle"), cap("busy"), cap("queued"), "", "\n\n",
                     _pane("❯ hello", below=FOOTER + "\n ❯ 1. Yes\n Enter to confirm")):
            self.assertIsNotNone(self.check(pane), pane[-120:])

    def test_plain_stand_in(self):
        self.assertIsNone(self.check("fake claude ready\nMessage from chief (through the swarm backend)\n", plain=True))
        self.assertIsNotNone(self.check("fake claude ready\n", plain=True))
        self.assertIsNotNone(self.check("Message from chief (through the swarm backend)\n ❯ 1. Grid\n", plain=True))


if __name__ == "__main__":
    unittest.main()
