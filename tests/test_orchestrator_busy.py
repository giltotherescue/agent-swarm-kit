"""0.8.1 (agent-control PR 64 and its review): queued messages reach an orchestrator during a long busy turn, and nothing
is ever typed onto a menu or someone's draft.

The real Supervisor runs its real step(), beat() and loop timing (next_wake) against a fake clock, a fake pane (the
orch-status answer) and a fake backend that keeps the heartbeat contract (receipt, then the oldest undelivered message
with its enqueue time `at`). No tmux, no network. Real Claude Code captures live in tests/fixtures/panes.
"""
import json
import os
import textwrap
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
PASTED_PANE = _pane("❯ [Pasted text #1 +3 lines]")   # Claude Code's collapsed paste: opaque, never submitted


def text_pane(text, **kw):
    """The input box holding this text, expanded (the first line on the prompt row)."""
    return _pane(*("❯ " + text).splitlines(), **kw)


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
            "boxed empty": "╭" + "─" * 40 + "╮\n│ ❯                │\n╰" + "─" * 40 + "╯\n" + FOOTER + "\n",
            "boxed hint": "╭" + "─" * 40 + "╮\n│ ❯ Press up to edit queued messages │\n╰" + "─" * 40 + "╯\n" + FOOTER + "\n",
            "every real footer row": _pane("❯ ", below=FOOTER + "\n  ? for shortcuts\n   Not logged in · Run /login\n"
                                           "  ✘ Auto-update failed: no write permission to npm prefix · Run claude doctor\n"
                                           "   ◐ medium · /effort\n  paste again to expand" + " " * 40 + "Not logged in · Run /login"),
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
                ("no footer (no evidence of a live box)", _pane("❯ ", below=""), "unknown_footer"),
                ("shell prompt after a genuine footer", _pane("❯ ", below="  ? for shortcuts\nuser@host:~$ "), "unknown_footer"),
                ("prose after a genuine footer", _pane("❯ ", below=FOOTER + "\nmy unfinished draft"), "unknown_footer"),
                ("bottom 22 rows below the prompt", "⏺ x\n" + RULE + "\n❯ \n" + "\n" * 22 + RULE + "\n" + FOOTER + "\n", None)):
            self.assertEqual(so.input_box(pane), want, name)
            for busy in (False, True):
                self.assertIsNotNone(so.send_block(pane, busy=busy), name)
                self.assertIsNotNone(so.send_block(pane, busy=busy, plain=True), name)

    def test_tall_draft_with_an_interior_rule_is_not_empty(self):
        """PR 67 review: a real prompt with unfinished text, 21 continuation lines, an interior rule and "❯ " before the
        real bottom was read as an empty box there."""
        pane = ("⏺ x\n" + RULE + "\n❯ unfinished text\n" + "".join(f"  continuation {i}\n" for i in range(21))
                + RULE + "\n❯ \n" + RULE + "\n" + FOOTER + "\n")
        self.assertNotEqual(so.input_box(pane), "empty")
        self.assertIsNotNone(so.send_block(pane))
        # clipped: the real top rule scrolled off the screen, continuation lines remain above the interior rule
        clipped = "".join(f"  continuation {i}\n" for i in range(21)) + RULE + "\n❯ \n" + RULE + "\n" + FOOTER + "\n"
        self.assertEqual(so.input_box(clipped), "empty")   # documented limit: nothing on screen shows the clipped top

    def test_quoted_menu_words_inside_the_box_are_data(self):
        line = "[Message from chief via swarm] The installer says Enter to confirm."
        pane = _pane("❯ " + line)
        self.assertFalse(so.pane_menu(pane))
        self.assertIsNone(so.PANE.editor_check(pane, line))
        self.assertTrue(so.pane_menu(_pane("❯ ", below=FOOTER + "\n Enter to confirm · Esc to cancel")))

    def test_pointer_identity_in_the_editor(self):
        line = "New bus message #7 from chief: read /w/orchestrators/x/.orchestrator/inbox/msg-7.md"
        ok = {"exact": _pane("❯ " + line), "trailing spaces": _pane("❯ " + line + "   ")}
        bad = {"prefix only": _pane("❯ " + line[:30]), "someone's text after it": _pane("❯ " + line, "  my own notes"),
               "draft before it": _pane("❯ my notes " + line), "collapsed marker": PASTED_PANE,
               "extra space inside": _pane("❯ " + line.replace("from chief", "from  chief").replace("from  chief", "fromchief")),
               "menu under the box": _pane("❯ " + line, below=FOOTER + "\n ❯ 1. Yes\n Enter to confirm"),
               "unknown footer": _pane("❯ " + line, below=FOOTER + "\nuser@host:~$ "),
               "no box": line + "\n"}
        for name, pane in ok.items():
            self.assertIsNone(so.PANE.editor_check(pane, line), name)
        for name, pane in bad.items():
            self.assertIsNotNone(so.PANE.editor_check(pane, line), name)

    def test_wrapped_pointer_at_80_columns(self):
        line = "New bus message #82 from chief (URGENT): read /home/swarm-runner/work/orchestrators/steward/.orchestrator/inbox/msg-82.md"
        rule = "─" * 80
        rows = textwrap.wrap(line, 78, break_on_hyphens=False)
        self.assertGreater(len(rows), 1)
        pane = "\n".join(["⏺ Done."] * 10 + [rule, "❯ " + rows[0]] + ["  " + r for r in rows[1:]] +
                         [rule, "  ? for shortcuts", "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]) + "\n"
        self.assertIsNone(so.PANE.editor_check(pane, line))
        self.assertIsNotNone(so.PANE.editor_check(pane, line.replace(" read ", " read  x")))
        hist = pane.replace(rule + "\n❯ " + rows[0], "❯ " + line + "\n" + rule + "\n❯ " + rows[0], 1)
        self.assertEqual(so.PANE.shown_above_editor(hist, line), 1)

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

    def test_only_the_active_dialog_counts(self):
        security = "Security notes:\n 1. Claude can make mistakes\nPress Enter to continue\n"
        self.assertEqual(so.classify(security + cap("menu"))[2], ["Down"])        # older screen scrolled up above
        self.assertEqual(so.classify(cap("menu") + security)[1], "the security notes")   # the trust screen is history
        both = cap("menu").replace("   Yes, I trust this folder", " ❯ Yes, I trust this folder")
        self.assertEqual(so.classify(both)[0], "block")   # two selections: conflicting evidence, nothing is sent
        stale_yes = TRUST_YES + "\n" + cap("menu")       # an earlier trust dialog with Yes selected, the live one on No
        self.assertEqual(so.classify(stale_yes)[2], ["Down"])

    def test_unrecognized_cursor_is_not_answered(self):
        pane = cap("menu").replace(" ❯ No, exit", "   No, exit")
        self.assertEqual(so.classify(pane)[0], "block")

    def test_login_screen_is_never_answered(self):
        for pane in (cap("menu") + "\nSelect login method:\n 1. Claude account\n", "Invalid API key · Please run /login\n"):
            self.assertEqual(so.classify(pane)[0], "login")




class Result:
    def __init__(self, rc=0, stdout="{}"):
        self.returncode, self.stdout, self.stderr = rc, stdout, ""


class Crash(BaseException):
    """The supervisor process dies here (not an Exception: step()'s error handler must not catch it)."""


class FakeBackend:
    """The heartbeat contract of swarm_backend: a receipt marks a message delivered once (and records the busy event in
    that same first-delivery branch), the reply carries the oldest undelivered message with its enqueue time."""

    def __init__(self, clock):
        self.clock, self.msgs, self.events, self.texts, self.beats = clock, [], [], [], 0
        self.on_beat = None     # called before each heartbeat is answered (the pane may change during network work)
        self.fail = 0           # heartbeats that fail before the backend sees them
        self.lose_reply = 0     # heartbeats the backend processes but whose reply is lost
        self.restart_generation = 0
        self.reject = None      # the next heartbeat carrying events is refused with HTTP 400 and this error

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
        if self.reject and body.get("events"):
            why, self.reject = self.reject, None
            raise urllib.error.HTTPError(path, 400, why, {}, None)
        for e in body.get("events") or []:
            if e["type"] not in ORCH_EVENT_TYPES:
                raise urllib.error.HTTPError(path, 400, f"bad event type {e['type']!r}", {}, None)
            self.events.append((e["type"], None))
            self.texts.append(e["text"])
        if self.lose_reply:
            self.lose_reply -= 1
            raise urllib.error.URLError("reply lost")
        head = next(({k: m[k] for k in ("id", "at", "sender", "text", "release") if k in m} for m in self.msgs
                     if not m["delivered"]), None)
        return {"desired": "running", "deleted": False, "generation": 1, "restart_generation": self.restart_generation,
                "message": head, "config": {}}

    def delivered(self):
        return [m["id"] for m in self.msgs if m["delivered"]]

    def types(self, typ):
        return [mid for t, mid in self.events if t == typ]


class Driver:
    """agent-exec's PaneDriver for the simulated pane (no sleeping: time advances on its own clock)."""

    def __init__(self, t):
        self.t, self.clock = t, 0.0

    def capture(self):
        return self.t.render()

    def type(self, text):
        self.t.typed.append(text)
        if self.t.on_type:
            self.t.on_type(text)
        if self.t.can_edit():
            self.t.editor += text
        return True

    def keys(self, *keys):
        for k in keys:
            if k == "BSpace":
                self.t.editor = self.t.editor[:-1]
            elif k == "Enter":
                self.t.enters += 1
                if self.t.on_enter:
                    self.t.on_enter()
                if self.t.enter_works and self.t.editor and self.t.can_edit():
                    self.t.history.append(self.t.editor)
                    self.t.submitted.append(self.t.editor)
                    self.t.editor = ""
        return True

    def sleep(self, s):
        self.clock += s

    def now(self):
        return self.clock


class LoopCase(unittest.TestCase):
    """The real Supervisor on a fake clock. agent-exec is simulated around a Claude Code editor drawn into self.pane (a
    template with an empty box, or a box-less stand-in screen): typed text shows in the box (wrapped at the rule width,
    later rows indented, like Claude Code) and Enter moves it into the conversation above the box. orch-type and
    orch-enter run agent-exec's real type_line and enter_line against that pane."""

    def setUp(self):
        self.now = 1_800_000_000.0
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, attr, val in ((so.sw, "RUNNER_DIR", os.path.join(self.tmp, "runner")),
                                  (so.sw, "HANDOFF", os.path.join(self.tmp, "handoff")),
                                  (so.sw, "WORK", os.path.join(self.tmp, "work")),
                                  (so.time, "time", lambda: self.now)):
            p = patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.be = FakeBackend(lambda: self.now)
        p = patch.object(so.sw, "api", self.be.api)
        p.start()
        self.addCleanup(p.stop)
        self.pane, self.spinning = BUSY_PANE, True
        self.editor, self.history = "", []
        self.typed, self.submitted, self.pasted, self.enters = [], [], [], 0   # pasted: message ids whose line was typed
        self.files = {}                       # message id -> text of the message file
        self.enter_works, self.on_type, self.on_enter, self.after_type = True, None, None, None
        self.type_rc, self.enter_raise, self.type_raise = [], None, None
        self.keys, self.keys_rc, self.killed, self.on_keys, self.status_fail = [], [], 0, None, 0
        self.sup = self.new_supervisor()
        self.sup.beat()

    def new_supervisor(self):
        sup = so.Supervisor("steward")
        sup.agent = self.fake_agent
        return sup

    def restart_supervisor(self):
        """A replacement supervisor process: only the state file survives."""
        self.sup = self.new_supervisor()
        self.sup.beat()

    def can_edit(self):
        box = so.PANE.pane_box(self.pane or "")
        return box["state"] == "empty" or self.box_less()

    def box_less(self):
        lines = so.PANE.pane_lines(self.pane or "")
        return bool(lines) and not any(so.INPUT_RULE.match(ln) or so.PROMPT_LINE.match(ln) for ln in lines)

    def render(self):
        if not self.pane:
            return self.pane or ""
        lines = self.pane.rstrip("\n").split("\n")
        box = so.PANE.pane_box(self.pane)
        if box["state"] == "empty" and box["top"] is not None:
            top, bot = box["top"], box["bottom"]
            if self.editor:
                width = so.PANE.pane_cells(lines[bot].strip()) - 2
                rows = textwrap.wrap(self.editor, width, break_on_hyphens=False) or [""]
                lines[top + 1:bot] = ["❯ " + rows[0]] + ["  " + r for r in rows[1:]]
            lines[top:top] = ["❯ " + h for h in self.history]
        elif self.box_less():
            lines += [ln for h in self.history for ln in (h, "got: " + h)] + ([self.editor] if self.editor else [])
        return "\n".join(lines) + "\n"

    def line(self):
        return Path(self.sup.inbox, "line.txt").read_text().rstrip("\n")

    def fake_agent(self, *args, **kw):
        cmd, opts = args[0], dict(zip(args[2::2], args[3::2]))
        plain = "--plain" in args
        if cmd == "orch-status":
            if self.status_fail:
                self.status_fail -= 1
                raise RuntimeError("agent-exec orch-status failed (1)")   # as Supervisor.agent(check=True) raises
            tail = f"✻ Working (esc to interrupt) {self.now}" if self.spinning else "done\n❯ "
            return Result(stdout=json.dumps({"alive": True, "dead": False, "tail": tail, "screen": self.render(), "pid": 7}))
        if cmd == "orch-capture":
            if self.status_fail or self.pane is None:
                return Result(stdout=json.dumps({"pane": None, "screen": ""}))
            ok = opts.get("--pane") in (None, "%1")
            return Result(stdout=json.dumps({"pane": "%1" if ok else None, "screen": self.render() if ok else ""}))
        if cmd == "orch-type":
            line = self.line()
            if "--id" in opts:
                self.files[int(opts["--id"])] = Path(self.sup.inbox, "message.md").read_text()
            self.pasted.append((self.sup.message or {}).get("id"))
            if self.type_rc:
                return Result(self.type_rc.pop(0))
            out = so.PANE.type_line(Driver(self), line, plain)
            if self.type_raise:
                raise self.type_raise
            if self.after_type:
                self.after_type()
            return Result({"ok": 0, "nothing": 5, "erased": 8}.get(out, 9), json.dumps({"typed": out}))
        if cmd == "orch-enter":
            st = so.PANE.enter_line(Driver(self), self.line(), int(opts.get("--before") or 0), plain)
            if self.enter_raise:
                raise self.enter_raise
            return Result(stdout=json.dumps({"state": st}))
        if cmd == "orch-keys":
            rc = self.keys_rc.pop(0) if self.keys_rc else 0
            if rc == 0:
                self.keys.append(list(args[2:]))
                self.enters += "Enter" in args[2:]
                if self.on_keys:
                    self.on_keys(list(args[2:]))
            return Result(rc)
        if cmd == "orch-kill":
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

    def idle_at(self, pane):
        self.spinning, self.pane = False, pane
        self.run_loop(25)

    def track(self):
        """Record when each message's line is typed (relative to now)."""
        start, self.paste_log, real = self.now, [], self.fake_agent

        def agent(*args, **kw):
            n = len(self.pasted)
            r = real(*args, **kw)
            if len(self.pasted) > n:
                self.paste_log.append((round(self.now - start, 1), self.pasted[-1]))
            return r
        self.sup.agent = agent
        return start

    def paste_times(self):
        return [t for t, _ in self.paste_log]

    def pointer(self, mid, bus=None, sender="chief", urgent=False):
        path = os.path.join(self.tmp, "work", "orchestrators", "steward", ".orchestrator", "inbox", f"msg-{mid}.md")
        return f"New bus message #{bus or mid} from {sender}{' (URGENT)' if urgent else ''}: read {path}"


class BusyDeliveryTest(LoopCase):
    def test_busy_pane_delivers_after_max_wait_measured_from_enqueue(self):
        self.run_loop(3)
        self.track()
        self.be.add("bus #%d hello")
        self.run_loop(120)
        self.assertEqual(self.be.delivered(), [1])
        self.assertTrue(89 <= self.paste_times()[0] <= 91, self.paste_log)
        self.assertEqual(self.be.types("message-delivered-busy"), [1])
        self.assertEqual(self.submitted, ["[Message from chief via swarm] bus #1 hello"])   # one short line: as is

    def test_urgent_queued_just_after_a_poll_lands_at_about_15_seconds(self):
        self.run_loop(3)
        self.track()
        self.be.add("URGENT bus #%d thread t | reply\n\nstop")   # just after the heartbeat: found ~10 s later
        self.run_loop(40)
        self.assertTrue(14.5 <= self.paste_times()[0] <= 16, self.paste_log)
        self.assertEqual(self.submitted, [self.pointer(1, urgent=True)])

    def test_old_message_found_busy_is_typed_at_once(self):
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
        self.assertEqual(self.be.delivered(), [1, 2, 3])

    def test_empty_receipt_reply_resets_the_backlog(self):
        self.run_loop(3)
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
        self.assertEqual(self.typed, [])

    def test_menu_and_draft_and_unreadable_box_block_busy_delivery(self):
        self.be.add("bus #%d hello")
        for pane in (MENU_PANE, _pane("❯ half-typed thought"), _pane("❯ ", "second line draft"), "⏺ working\n❯ \n",
                     cap("draft"), cap("single"), cap("menu"), PASTED_PANE):
            self.pane = pane
            self.run_loop(120)
            self.assertEqual((self.typed, self.enters), ([], 0), pane[-80:])
        self.pane = cap("queued")
        self.run_loop(15)
        self.assertEqual(self.be.delivered(), [1])

    def test_blocked_message_does_not_spin_the_loop(self):
        self.be.add("bus #%d hello")
        self.pane = _pane("❯ draft")
        steps = self.run_loop(200)
        self.assertLess(len(steps), 200 / so.POLL + 15)

    def test_receipt_retry_without_retyping_and_one_busy_event(self):
        self.be.add("bus #%d hello")
        self.be.lose_reply = 1   # the backend stores the receipt, the reply is lost: the receipt is sent again
        self.run_loop(120)
        self.assertEqual((len(self.typed), self.enters), (1, 1))
        self.assertEqual(self.be.types("message-delivered"), [1])
        self.assertEqual(self.be.types("message-delivered-busy"), [1])

    def test_failed_heartbeats_after_submission_retry_only_the_receipt(self):
        self.be.add("bus #%d hello")
        self.run_loop(85)
        self.be.fail = 3
        self.run_loop(80)
        self.assertEqual((len(self.typed), self.enters, self.be.delivered()), (1, 1, [1]))

    def test_pane_changes_during_the_message_poll(self):
        """The pane turned into the trust menu inside the message-poll heartbeat: the send-time capture sees it."""
        self.idle_at(cap("idle"))
        self.be.add("bus #%d hello")

        def flip():
            self.pane = MENU_PANE
        self.be.on_beat = flip
        self.run_loop(30)
        self.assertEqual((self.typed, self.enters, self.be.delivered()), ([], 0, []))

    def test_older_backend_refusing_the_new_event_types_does_not_stop_the_session(self):
        self.run_loop(3)
        self.be.add("URGENT bus #%d stop")
        self.pane = _pane("❯ draft")
        with patch(__name__ + ".ORCH_EVENT_TYPES", tuple(t for t in ORCH_EVENT_TYPES if t not in so.NEW_EVENT_TYPES)):
            self.run_loop(200)
        self.assertEqual(self.sup.state, "running")
        self.assertIsNone(self.sup.blocked)
        self.assertEqual(self.be.types("delivery-deferred"), [])

    def test_activity_seen_on_the_fresh_capture_applies_the_busy_wait(self):
        """Idle at observation, busy on the post-poll capture: the new normal message waits the busy time."""
        self.idle_at(cap("idle"))
        self.be.add("bus #%d normal")
        start = self.now

        def go_busy():
            self.pane, self.spinning, self.be.on_beat = cap("busy"), True, None
        self.be.on_beat = go_busy
        self.run_loop(60)
        self.assertEqual(self.typed, [])
        self.run_loop(45)
        self.assertEqual(self.be.delivered(), [1])
        self.assertGreaterEqual(self.sup.last_busy_paste - start, 89)

    def test_failed_heartbeat_keeps_deferral_tracking(self):
        self.be.add("bus #%d hello")
        self.pane = _pane("❯ draft")
        self.run_loop(100)
        self.be.fail = 2
        self.run_loop(100)
        self.be.fail = 1
        self.run_loop(300)
        self.assertEqual(self.be.types("delivery-deferred"), [None])

    def test_sustained_deferral_is_reported_once_per_reason(self):
        self.run_loop(3)
        self.be.add("URGENT bus #%d stop")
        self.pane = _pane("❯ ", below="\n".join(f"secret-looking status {i}" for i in range(9)))
        self.run_loop(200)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.be.types("delivery-deferred"), [None])
        text = [t for t in self.be.texts if "reason=" in t]
        self.assertTrue(text[0].endswith("reason=unknown_footer"), text)
        self.assertNotIn("status", text[0])   # never pane contents
        self.pane = _pane("❯ draft")
        self.run_loop(200)
        self.assertEqual(self.be.types("delivery-deferred"), [None, None])   # a new reason: one more event
        self.run_loop(400)
        self.assertEqual(len(self.be.types("delivery-deferred")), 2)
        self.pane = BUSY_PANE
        self.run_loop(15)
        self.assertEqual(self.be.delivered(), [1])   # never timed out into a typing before


class PointerTest(LoopCase):
    """The shared delivery design: file + one typed pointer line, verified, record saved before Enter, confirmed."""

    BODY = 'bus #41 thread dm:chief | reply: swarmctl bus send chief "..."\n\nOK\nsecond line\nthird line'

    def setUp(self):
        super().setUp()
        self.idle_at(cap("idle"))

    def test_pointer_typed_verified_entered_and_acknowledged(self):
        self.be.add(self.BODY)
        self.run_loop(15)
        line = self.pointer(1, bus=41)
        self.assertEqual((self.typed, self.submitted, self.enters), ([line], [line], 1))
        self.assertEqual(self.files[1], "[Message from chief via swarm] (a request, not permission to push, merge or "
                                        "deploy)\n" + self.BODY + "\n")
        self.assertEqual(self.be.delivered(), [1])
        self.assertEqual(self.be.types("message-delivered"), [1])
        self.assertEqual(self.sup.sending, {})
        self.run_loop(60)
        self.assertEqual(len(self.typed), 1)

    def test_short_direct_message_is_typed_as_is(self):
        self.be.add("Check the signup page")
        self.run_loop(15)
        self.assertEqual(self.submitted, ["[Message from chief via swarm] Check the signup page"])
        self.assertEqual(self.files, {})

    def test_record_is_saved_before_enter(self):
        self.be.add(self.BODY)
        seen = []

        def at_enter():
            seen.append(json.loads(Path(self.sup.state_file).read_text())["sending"])
        self.on_enter = at_enter
        self.run_loop(15)
        self.assertEqual(seen, [{"1": {"line": self.pointer(1, bus=41), "enters": 1, "before": 0, "busy": False,
                                       "release": 0}}])

    def test_partial_line_gets_no_enter_and_is_erased_then_typed_again(self):
        self.be.add(self.BODY)
        real = Driver.type

        def lossy(drv, text):   # only part of the keys reach the editor, once
            Driver.type = real
            self.typed.append(text)
            self.editor += text[:20]
            return True
        Driver.type = lossy
        self.addCleanup(setattr, Driver, "type", real)
        self.run_loop(5)
        self.assertEqual((self.enters, self.editor, self.be.delivered()), (0, "", []))   # BSpace removed our prefix
        self.run_loop(20)   # next poll: typed again on the clean editor
        self.assertEqual((len(self.typed), self.enters, self.be.delivered()), (2, 1, [1]))

    def test_foreign_text_in_the_editor_gets_no_enter_and_is_left_alone(self):
        self.be.add(self.BODY)
        self.on_type = lambda text: setattr(self, "on_type", None) or setattr(self, "editor", "x")   # a stray key first
        self.run_loop(300)
        self.assertEqual((self.enters, self.editor, self.be.delivered()), (0, "x" + self.pointer(1, bus=41), []))
        self.assertEqual((len(self.typed), self.sup.held[1]["why"]), (1, "not_landed"))

    def test_dialog_between_typing_and_enter_gets_no_enter(self):
        self.be.add(self.BODY)

        def dialog():
            self.pane = MENU_PANE
        self.after_type = dialog
        self.run_loop(15)
        self.after_type = None
        self.assertEqual((len(self.typed), self.enters, self.be.delivered()), (1, 0, []))
        self.assertEqual(self.sup.held[1]["why"], "not_landed")
        self.pane = cap("idle")
        self.run_loop(300)
        self.assertEqual((len(self.typed), self.enters, self.be.delivered()), (1, 0, []))   # held: never typed again
        self.assertEqual(self.be.types("delivery-unknown"), [None])

    def test_crash_after_typing_before_the_record_types_nothing_again(self):
        self.be.add(self.BODY)
        self.type_raise = Crash()
        with self.assertRaises(Crash):
            self.run_loop(15)
        self.type_raise = None
        self.assertEqual(self.sup.sending, {})
        self.restart_supervisor()
        self.run_loop(120)   # the restarted supervisor sees a recent change (busy wait), then submits what is there
        self.assertEqual((len(self.typed), self.submitted, self.be.delivered()), (1, [self.pointer(1, bus=41)], [1]))

    def test_crash_before_enter_presses_enter_once_without_retyping(self):
        self.be.add(self.BODY)
        real = self.on_enter

        def die():
            raise Crash()
        self.on_enter = die   # the process dies with the record saved, before the key reaches the pane
        with self.assertRaises(Crash):
            self.run_loop(15)
        self.on_enter = real
        self.assertEqual((self.enters, self.submitted), (1, []))   # counted, but nothing reached the editor
        self.restart_supervisor()
        self.run_loop(15)
        self.assertEqual((len(self.typed), len(self.submitted), self.be.delivered()), (1, 1, [1]))

    def test_crash_after_enter_never_submits_twice(self):
        self.be.add(self.BODY)
        self.enter_raise = Crash()   # Enter went in; the supervisor died before it recorded anything else
        with self.assertRaises(Crash):
            self.run_loop(15)
        self.enter_raise = None
        self.assertEqual(len(self.submitted), 1)
        self.restart_supervisor()
        self.run_loop(60)
        self.assertEqual((len(self.typed), len(self.submitted), self.enters, self.be.delivered()), (1, 1, 1, [1]))

    def test_unknown_after_enter_without_evidence_is_held(self):
        self.be.add(self.BODY)
        self.enter_raise = Crash()
        with self.assertRaises(Crash):
            self.run_loop(15)
        self.enter_raise = None
        self.history.clear()   # the conversation scrolled away: no evidence either way
        self.restart_supervisor()
        self.run_loop(300)
        self.assertEqual((len(self.typed), self.enters, self.be.delivered()), (1, 1, []))
        self.assertEqual(self.sup.held[1]["why"], "submit_uncertain")

    def test_enter_that_does_not_take_is_retried_then_held(self):
        self.be.add(self.BODY)
        self.enter_works = False
        self.run_loop(120)
        self.assertEqual((len(self.typed), self.enters, self.be.delivered()), (1, so.SUBMIT_TRIES, []))
        self.assertEqual(self.sup.held[1]["why"], "submit_uncertain")

    def test_queued_hint_alone_is_not_a_receipt(self):
        self.be.add(self.BODY)

        def swallow():
            self.editor, self.pane, self.enter_works = "", cap("queued"), False   # the box shows only the generic hint
        self.on_enter = swallow
        self.run_loop(60)
        self.assertEqual(self.be.delivered(), [])
        self.assertIn(self.sup.held.get(1, {}).get("why"), ("submit_uncertain", "not_landed"))

    def dialog_takes_the_line(self):
        self.pane, self.editor = MENU_PANE, ""   # a dialog opened right after the line verified, and took it

    def test_release_types_once_then_holds_again(self):
        """Recheck 3 finding 2: one admin --resend allowed two typings. Now one release, one typing."""
        self.be.add(self.BODY)
        self.after_type = self.dialog_takes_the_line
        self.run_loop(15)
        self.assertEqual(self.sup.held[1]["why"], "not_landed")
        self.pane = cap("idle")
        self.be.msgs[0]["release"] = 1    # swarmctl orchestrator release steward 1 --resend
        self.run_loop(so.HEARTBEAT + 15)  # the next heartbeat brings it; the resend's typing does not verify either
        self.assertEqual(len(self.typed), 2)
        self.assertEqual(self.sup.held[1], {"why": "not_landed", "release": 1})
        self.pane = cap("idle")
        self.after_type = None
        self.run_loop(300)
        self.assertEqual((len(self.typed), self.be.delivered()), (2, []))   # no second typing for the same release
        self.be.msgs[0]["release"] = 2
        self.run_loop(so.HEARTBEAT + 15)
        self.assertEqual((len(self.typed), len(self.submitted), self.be.delivered()), (3, 1, [1]))

    def test_release_survives_a_crash_after_it_was_consumed(self):
        self.be.add(self.BODY)
        self.after_type = self.dialog_takes_the_line
        self.run_loop(15)
        self.pane, self.after_type = cap("idle"), None
        self.be.msgs[0]["release"] = 1
        self.type_raise = Crash()
        with self.assertRaises(Crash):
            self.run_loop(so.HEARTBEAT + 15)
        self.type_raise = None
        self.assertEqual(self.sup.held[1], {"why": "resend_typing", "release": 1})   # used up before the keys
        self.restart_supervisor()
        self.run_loop(300)   # the release is spent: no second typing and no Enter on the half-known state
        self.assertEqual((len(self.typed), self.enters, self.be.delivered()), (2, 0, []))
        self.be.msgs[0]["release"] = 2   # the admin releases again: the line already in the editor is submitted, not retyped
        self.run_loop(so.HEARTBEAT + 15)
        self.assertEqual((len(self.typed), len(self.submitted), self.be.delivered()), (2, 1, [1]))

    def test_80x24_pane_delivers(self):
        rule = "─" * 80
        self.idle_at("\n".join(["⏺ Done."] * 18 + [rule, "❯ ", rule, "  ? for shortcuts",
                                                  "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]) + "\n")
        self.be.add(self.BODY)
        rows = []
        self.on_type = lambda text: None
        self.after_type = lambda: rows.append(len(so.PANE.pane_box(self.render())["rows"]))
        self.run_loop(15)
        self.assertGreater(rows[0], 1)   # the pointer wrapped over several editor rows and still verified
        self.assertEqual(self.submitted, [self.pointer(1, bus=41)])
        self.assertEqual(self.be.delivered(), [1])

    def test_invalid_api_key_text_above_an_empty_editor_is_not_a_login_screen(self):
        """Recheck 3 finding 5: a tool diagnostic above a live editor killed the session as needs-login."""
        lines = cap("idle").rstrip("\n").splitlines()
        top = next(i for i, ln in enumerate(lines) if so.INPUT_RULE.match(ln))
        pane = "\n".join(lines[:top] + ["⏺ Bash(curl api)", "  ⎿  Error: Invalid API key · Please run /login"] + lines[top:]) + "\n"
        self.assertIsNone(so.classify(pane))
        self.idle_at(pane)
        self.be.add(self.BODY)
        self.run_loop(60)
        self.assertEqual((self.sup.state, self.killed), ("running", 0))
        self.assertEqual(self.be.delivered(), [1])
        # A real sign-in screen (no live editor) still stops the session and gets no keys.
        self.idle_at("Select login method:\n ❯ 1. Claude account with subscription\n   2. Anthropic Console account\n")
        self.run_loop(30)
        self.assertEqual((self.sup.state, self.keys), ("needs-login", []))


class ThemePickerTest(LoopCase):
    """Recheck 3 finding 4: the real 2.1.321 theme picker at 220x50 has no Enter hint."""

    def test_capture_is_recognized(self):
        self.assertEqual(so.classify(cap_file("pane-theme-picker.txt")), ("first-run", "the theme picker", ["Enter"]))
        draft = _pane("❯ Choose the text style that looks best with your terminal")
        self.assertIsNone(so.classify(draft))
        cut = cap_file("pane-theme-picker.txt").replace("  Syntax theme: Monokai Extended (ctrl+t to disable)", "")
        self.assertIsNone(so.classify(cut))
        self.assertIsNone(so.classify(cap_file("pane-theme-picker.txt").replace("❯", " ")))   # no selection marker

    def test_enter_inside_the_start_up_window_only(self):
        self.spinning = False
        self.sup.started_at = int(self.now)
        self.pane = cap_file("pane-theme-picker.txt")

        def answered(k):
            self.pane = cap("idle")
        self.on_keys = answered
        self.run_loop(20)
        self.assertEqual(self.keys, [["Enter"]])
        self.assertEqual(self.sup.state, "running")

    def test_no_keys_after_the_start_up_window(self):
        self.spinning = False
        self.sup.started_at = int(self.now) - so.FIRST_RUN_WINDOW - 10
        self.pane = cap_file("pane-theme-picker.txt")
        self.run_loop(30)
        self.assertEqual(self.keys, [])


class CollapsedPasteCaptureTest(LoopCase):
    """Recheck 3 finding 1: the real 2.1.321 captures of a collapsed multi-line paste (idle and busy). The marker is a
    draft: no typing onto it and never Enter; the pointer design never produces one."""

    def test_marker_is_a_draft_and_blocks(self):
        for name in ("pane-idle-multiline-paste.txt", "pane-busy-urgent-paste.txt"):
            pane = cap_file(name)
            self.assertEqual((so.send_block(pane), so.send_block(pane, busy=True), so.classify(pane)), ("draft", "draft", None))
            self.assertIsNotNone(so.PANE.editor_check(pane, "New bus message #1 from chief: read /x/msg-1.md"), name)

    def test_no_keys_onto_a_collapsed_paste(self):
        self.idle_at(cap_file("pane-idle-multiline-paste.txt"))
        self.be.add("bus #%d hello\nmore")
        self.run_loop(200)
        self.assertEqual((self.typed, self.enters, self.be.delivered()), ([], 0, []))
        self.assertTrue(self.be.texts[-1].endswith("reason=draft"), self.be.texts)


def cap_file(name):
    return (PANES / name).read_text()


class IdleAndStopTest(LoopCase):
    def go_idle(self):
        self.spinning = False

    def test_idle_empty_prompt_delivers_immediately_without_busy_event(self):
        self.idle_at(cap("idle"))
        self.be.add("bus #%d hello")
        start = self.now
        self.run_loop(15)
        self.assertEqual(self.be.delivered(), [1])
        self.assertLessEqual(self.sup.tail_changed - start, 11)
        self.assertEqual(self.be.types("message-delivered-busy"), [])

    def test_idle_box_less_screen_is_refused_and_reported(self):
        self.go_idle()
        self.pane = "fake claude ready\n"
        self.be.add("bus #%d hello")
        self.run_loop(200)
        self.assertEqual((self.typed, self.be.delivered()), ([], []))
        self.assertTrue(self.be.texts[-1].endswith("reason=unreadable"), self.be.texts)

    def test_idle_box_less_stand_in_delivers_only_with_the_plain_adapter(self):
        self.go_idle()
        self.pane = "fake claude ready\n"
        self.be.add("bus #%d hello")
        with patch.object(so, "PLAIN", True):
            self.run_loop(40)
        self.assertEqual(self.be.delivered(), [1])
        self.assertEqual(len(self.submitted), 1)

    def test_stationary_menu_or_draft_never_gets_an_idle_typing(self):
        self.go_idle()
        self.be.add("bus #%d hello")
        for pane in (MENU_PANE, cap("draft"), cap("single"), _pane("❯ ", RULE, "after a pasted rule"),
                     _pane('❯ Try "restarting the service"')):
            self.pane = pane
            self.run_loop(200)
            self.assertEqual((self.typed, self.enters), ([], 0), pane[-80:])
        self.pane = cap("cleared")
        self.run_loop(30)
        self.assertEqual(self.be.delivered(), [1])

    def test_trust_screen_gets_down_then_enter_on_yes(self):
        self.go_idle()
        self.sup.started_at = int(self.now)
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
        self.assertEqual(self.be.delivered(), [1])
        self.assertEqual(self.killed, 0)

    def test_trust_screen_that_ignores_down_never_gets_enter(self):
        self.go_idle()
        self.sup.started_at = int(self.now)
        self.pane = cap("menu")
        self.run_loop(60)
        self.assertNotIn(["Enter"], self.keys)
        self.assertEqual(self.keys, [["Down"]] * so.FIRST_RUN_TRIES)
        self.assertEqual(self.sup.state, "needs-login")

    def test_login_screen_is_never_answered(self):
        self.go_idle()
        self.be.add("bus #%d hello")
        self.pane = cap("menu").rstrip() + "\n Select login method:\n ❯ 1. Claude account\n"
        self.run_loop(60)
        self.assertEqual((self.keys, self.typed), ([], []))
        self.assertEqual(self.sup.state, "needs-login")

    def test_wrap_up_is_not_sent_onto_a_menu_or_draft(self):
        self.go_idle()
        for pane in (MENU_PANE, cap("draft")):
            self.pane = pane
            self.sup.state = "running"
            self.sup.begin_stop("the desired state is stopped")
            self.assertEqual((self.typed, self.enters), ([], 0))
            self.assertEqual(self.sup.stop_sent_at, 0)   # falls back to the stop deadline
            self.assertEqual(self.sup.stop_deadline, self.now + so.STOP_GRACE)

    def test_wrap_up_is_typed_and_entered_on_an_empty_prompt(self):
        self.go_idle()
        self.pane = cap("idle")
        self.sup.begin_stop("the desired state is stopped")
        self.assertEqual(len(self.submitted), 1)
        self.assertIn("stopping this session", self.submitted[0])
        self.assertEqual(self.sup.stop_sent_at, self.now)

    def test_wrap_up_skipped_when_the_capture_fails(self):
        self.go_idle()
        self.pane = cap("idle")
        self.status_fail = 2
        self.sup.begin_stop("the desired state is stopped")
        self.assertEqual((self.typed, self.enters, self.sup.stop_sent_at), ([], 0, 0))
        self.assertIn("reason=unreadable", self.sup.events[-1]["text"])

    def test_wrap_up_dialog_after_typing_gets_no_enter(self):
        self.go_idle()
        self.pane = cap("idle")
        self.after_type = lambda: setattr(self, "pane", MENU_PANE)
        self.sup.begin_stop("restart requested by an admin", then="start")
        self.assertEqual((len(self.typed), self.enters, self.sup.stop_sent_at), (1, 0, 0))

    def test_restart_heartbeat_captures_the_pane_again(self):
        self.idle_at(cap("idle"))
        self.pane = MENU_PANE
        self.be.restart_generation = 1
        self.now += so.HEARTBEAT
        self.sup.step()
        self.assertEqual(self.sup.state, "stopping")
        self.assertEqual((self.typed, self.enters, self.sup.stop_sent_at), ([], 0, 0))


class AgentExecCommandsTest(unittest.TestCase):
    """agent-exec's own commands on a real (private) tmux socket are covered by test_orchestrator.SupervisorTest; here the
    message file is checked directly."""

    def test_message_file_is_private_and_atomic(self):
        ae = so.PANE
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = ae.Paths(str(tmp / "work"), str(tmp / "handoff"))
        inbox = Path(ae.orch_inbox(p, "steward"))
        inbox.mkdir(parents=True)
        (inbox / "message.md").write_text("[Message from chief via swarm]\nhello\n")
        path = ae.orch_message_file(p, "steward", 12)
        self.assertEqual(path, str(tmp / "work" / "orchestrators" / "steward" / ".orchestrator" / "inbox" / "msg-12.md"))
        self.assertEqual(Path(path).read_text(), "[Message from chief via swarm]\nhello\n")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["msg-12.md"])


if __name__ == "__main__":
    unittest.main()
