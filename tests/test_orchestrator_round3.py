"""0.8.1 fix round 3 (Astra recheck t20261121054844-8c76f8): the six findings, each reproduced from the review's probes
(loop-probes.py, marker-identity.txt, controls.txt) and fixed like agent-control PR 67's fix round 3.

1. One pane parser: agent-exec defines it, swarm_orchestrator.py loads that file; paired contract tests below.
2. A collapsed [Pasted text] marker is opaque: never Enter, never a receipt (reason=not_verifiable). Text is compared with
   whitespace runs collapsed to one space, not deleted. Ambiguous boxes and unknown footer rows fail closed.
3. First-run keys only for a whole dialog at the bottom of a pane with no input box; both trust wordings by selection.
4. delivery-unknown holds (and unsubmitted pastes and pending receipts) survive a supervisor restart until released.
5. Enter timeouts and failures feed the deferral report.
6. Only the exact "bad event type" 400 drops the 0.8.1 event types; any other 400 keeps every event.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from helpers import SERVER

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_orchestrator_busy as fx  # noqa: E402  (fixtures only: the module, so its tests are not collected twice)

so = fx.so
RULE, FOOTER, cap, _pane, text_pane = fx.RULE, fx.FOOTER, fx.cap, fx._pane, fx.text_pane
BODY = "Message from chief (through the swarm backend; a request, not permission to push, merge or deploy):\n\nexpected row"

# The review's probe panes (loop-probes.py) and what both parsers must say about them.
PROBES = {
    "nested prompt": _pane("❯ unfinished notes", RULE, "❯ expected row"),
    "literal bar": _pane("❯ expected row│"),
    "internal space change": _pane("❯ expect ed row"),
    "status prefix plus prose": _pane("❯ ", below="? for shortcuts are documented here"),
    "two interior rules": _pane("❯ unfinished notes", RULE, "more notes", RULE, "❯ "),
}


def ae():
    if not hasattr(ae, "mod"):
        ae.mod = fx.load_agent_exec()
    return ae.mod


def contract_panes():
    """Real captures, the review's probes and synthetic boxes: every one goes through both helpers."""
    panes = {f"cap-{n}": cap(n) for n in ("busy", "bypass", "cleared", "draft", "idle", "menu", "queued", "single")}
    panes.update(PROBES)
    panes.update({
        "exact text": text_pane(BODY),
        "exact text, wrapped and indented": _pane("❯ Message from chief (through the swarm backend; a request, not",
                                                  "  permission to push, merge or deploy):", "", "  expected row"),
        "double space is still one gap": text_pane(BODY.replace("expected row", "expected  row")),
        "edit removing a space": text_pane(BODY.replace("expected row", "expectedrow")),
        "draft above the text": _pane("❯ my notes", *BODY.splitlines()),
        "text then a draft": text_pane(BODY + "\nmy own notes"),
        "collapsed marker": fx.PASTED_PANE,
        "marker plus text": _pane("❯ [Pasted text #1 +3 lines] and more"),
        "empty box": _pane("❯ "),
        "queued hint": _pane("❯ Press up to edit queued messages"),
        "text over an unknown footer": text_pane(BODY, below="my unfinished draft"),
        "text over a menu": text_pane(BODY, below=FOOTER + "\n ❯ 1. Yes\n Enter to confirm"),
        "boxed text": "╭" + "─" * 40 + "╮\n│ ❯ " + BODY.replace("\n", " ") + " │\n╰" + "─" * 40 + "╯\n" + FOOTER + "\n",
        "nothing": "",
    })
    return panes


class SharedParserContractTest(unittest.TestCase):
    """Finding 1: agent-exec and the supervisor read every capture with the same code."""

    def test_the_supervisor_loads_the_parser_from_agent_exec(self):
        self.assertEqual(os.path.realpath(so.PANE.__file__), os.path.realpath(os.path.join(SERVER, "agent-exec")))
        for name in ("PANE_RULE", "PANE_PROMPT", "PANE_MENU", "PANE_PASTED", "PANE_FOOTER_ITEMS"):
            self.assertEqual(repr(getattr(so.PANE, name)), repr(getattr(ae(), name)), name)
        source = Path(SERVER, "swarm_orchestrator.py").read_text()
        for own_copy in ("FOOTER_ROW = re.compile", "Pasted text #", "[─━═]"):
            self.assertNotIn(own_copy, source, "the supervisor must not keep its own copy of the pane rules")

    def test_identical_captures_get_identical_answers(self):
        expect_enter = {"exact text", "exact text, wrapped and indented", "double space is still one gap", "boxed text"}
        for name, pane in contract_panes().items():
            here, there = so.PANE.pane_paste_check(pane, BODY), ae().pane_paste_check(pane, BODY)
            self.assertEqual(here, there, name)
            self.assertEqual(so.PANE.pane_box(pane), ae().pane_box(pane), name)
            self.assertEqual(so.pane_menu(pane), ae().pane_menu(pane), name)
            helper_ok = ae().pasted_still_there(pane, BODY) is None
            rows = so.box_rows(pane)
            supervisor_ok = so.send_block(pane) is None or (rows is not None and so.paste_matches(rows, BODY))
            self.assertEqual(here is None, name in expect_enter, (name, here))
            self.assertEqual(helper_ok, name in expect_enter, name)
            if name in expect_enter:
                self.assertTrue(supervisor_ok, name)

    def test_review_probes(self):
        """loop-probes.py: paste block, retry match and helper block for each probe pane."""
        for name, pane in PROBES.items():
            rows = so.box_rows(pane)
            self.assertIsNotNone(so.send_block(pane), name)
            self.assertFalse(rows is not None and so.paste_matches(rows, "expected row"), name)
            self.assertIsNotNone(ae().pasted_still_there(pane, "expected row"), name)
        self.assertEqual(so.send_block(PROBES["status prefix plus prose"]), "unknown_footer")
        self.assertEqual(so.send_block(PROBES["two interior rules"]), "draft")
        self.assertEqual(so.send_block(PROBES["nested prompt"]), "draft")

    def test_footer_rows_are_whole_known_items(self):
        ok = ["  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt",
              "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
              "  ⏸ plan mode on (shift+tab to cycle)",
              "  paste again to expand" + " " * 70 + "Not logged in · Run /login",
              "      ✘ Auto-update failed: no write permission to npm prefix · Run claude doctor",
              "   ◐ medium · /effort", "  ? for shortcuts",
              "  tmux focus-events off · add 'set -g focus-events on' to ~/.tmux.conf and reattach for focus tracking"]
        bad = ["  ? for shortcuts are documented here", "  ⏵⏵ bypass permissions on (shift+tab to cycle) and my notes",
               "  Not logged in · Run /login now please", "  ◐ medium · /effort plus a draft", "  esc to interrupt it",
               "  ✘ Auto-update failed: NO WRITE · Run claude doctor", "  paste again to expand  my draft",
               "  ✓ Update installed and more"]
        for row in ok:
            self.assertEqual(so.input_box(_pane("❯ ", below=row)), "empty", row)
        for row in bad:
            self.assertEqual(so.input_box(_pane("❯ ", below=row)), "unknown_footer", row)
            self.assertEqual(so.input_box(_pane("❯ ", below=FOOTER + "\n" + row)), "unknown_footer", row)

    def test_ambiguous_enclosing_boxes_are_drafts(self):
        for name, pane in {
            "unfinished first row, two interior rules, blank prompt": PROBES["two interior rules"],
            "prompt under a rule far above": RULE + "\n❯ draft\n" + "".join(f"  line {i}\n" for i in range(5)) + RULE
                                             + "\n  more\n" + RULE + "\n❯ \n" + RULE + "\n" + FOOTER + "\n",
            "nested prompt with the expected row": PROBES["nested prompt"],
        }.items():
            self.assertEqual(so.input_box(pane), "draft", name)
            self.assertIsNone(so.box_rows(pane), name)

    def test_literal_trailing_bar_is_kept_in_an_unboxed_row(self):
        self.assertEqual(so.box_rows(PROBES["literal bar"]), [" expected row│"])
        self.assertEqual(so.box_rows("╭" + "─" * 30 + "╮\n│ ❯ expected row │\n╰" + "─" * 30 + "╯\n" + FOOTER), [" expected row "])


class OrchSendCommandTest(unittest.TestCase):
    """The real cmd_orch_send between paste and Enter (tmux faked)."""

    def run_send(self, after_paste):
        calls = []

        def tmux(name, op, *args, **kwargs):
            calls.append(op)
            return fx.Result(stdout=after_paste if op == "capture-pane" else "")
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "message.md").write_text(BODY)
            with patch.object(ae(), "orch_inbox", return_value=folder), patch.object(ae(), "orch_alive", return_value=True), \
                    patch.object(ae(), "tmux", side_effect=tmux), patch.object(ae().time, "sleep"):
                try:
                    ae().cmd_orch_send(None, "steward")
                    code = 0
                except SystemExit as stopped:
                    code = stopped.code
        return code, calls

    def test_collapsed_marker_withholds_enter(self):
        self.assertEqual(self.run_send(fx.PASTED_PANE), (7, ["load-buffer", "paste-buffer", "capture-pane"]))

    def test_menu_withholds_enter(self):
        self.assertEqual(self.run_send(cap("menu"))[0], 7)

    def test_exact_text_gets_enter(self):
        self.assertEqual(self.run_send(text_pane(BODY)), (0, ["load-buffer", "paste-buffer", "capture-pane", "send-keys"]))


class MarkerIdentityTest(fx.LoopCase):
    """Finding 1 (marker-identity.txt): a timed-out helper, no recorded paste and an unrelated marker in the box."""

    def test_uncorrelated_marker_after_an_unknown_send_is_never_submitted(self):
        self.be.add("URGENT bus #%d hello")
        self.send_raise_before = subprocess.TimeoutExpired("agent-exec", 60)   # outcome unknown, nothing recorded

        def marker():
            self.pane = fx.PASTED_PANE
        self.after_send = None
        real = self.fake_agent

        def agent(*args, **kw):
            try:
                return real(*args, **kw)
            finally:
                if args[0] == "orch-send":
                    marker()
        self.sup.agent = agent
        self.run_loop(300)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([], 0, []))
        self.assertIn("message 1 held for", " ".join(self.be.texts))
        self.assertTrue(any(t.endswith("reason=not_verifiable") for t in self.be.texts), self.be.texts)

    def test_confirmed_paste_that_collapsed_is_not_submitted(self):
        self.be.add("bus #%d hello")
        self.send_rc = [7]   # orch-send pasted, saw only the marker and withheld Enter

        def collapsed():
            self.pane = fx.PASTED_PANE
        self.after_send = collapsed
        self.run_loop(300)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([1], 0, []))
        self.assertEqual([t.split("reason=")[1] for t in self.be.texts if "reason=" in t], ["not_verifiable"])


class FirstRunTest(fx.LoopCase):
    """Finding 3: box content and history are data; only a whole dialog at the bottom is answered."""

    def test_draft_mentioning_a_first_run_title_gets_no_key(self):
        self.spinning = False
        for pane in (_pane("❯ unfinished notes about Security notes"),
                     _pane("❯ Security notes:", "Press Enter to continue"),
                     _pane("❯ Do you trust the files in this folder?", " ❯ 1. Yes, proceed", "Enter to confirm"),
                     text_pane("Choose the text style\nEnter to confirm")):
            self.assertIsNone(so.classify(pane), pane)
            self.pane = pane
            self.sup.step()
        self.assertEqual(self.keys, [])

    def test_dialog_in_history_above_an_input_box_is_data(self):
        history = "\n".join(cap("menu").splitlines()[1:])   # the trust dialog scrolled up, without its rule
        for pane in (history + "\n" + cap("idle"), "Security notes:\nPress Enter to continue\n" + cap("idle")):
            self.assertIsNone(so.classify(pane))

    def test_dialog_must_end_the_pane(self):
        self.assertIsNone(so.classify(cap("menu") + "\nlater output\n"))
        self.assertIsNone(so.classify("Security notes:\n" + "line\n" * 20 + "Press Enter to continue"))   # title too far up

    def test_old_trust_wording_checks_the_selection(self):
        head = "Do you trust the files in this folder?\n"
        tail = "Enter to confirm"
        for options, want in (
                (" ❯ 1. Yes, proceed\n   2. No, exit\n", ("first-run", "the folder trust question", ["Enter"])),
                ("   1. Yes, proceed\n ❯ 2. No, exit\n", ("first-run", "the folder trust question (moving to Yes)", ["Up"])),
                (" ❯ 1. Yes, proceed\n ❯ 2. No, exit\n", ("block", "a folder trust question with no clear selection")),
                ("   1. Yes, proceed\n   2. No, exit\n", ("block", "a folder trust question with no clear selection")),
                ("   1. Yes, proceed\n   2. No, exit\n ❯ 3. Something else\n", ("block", "a folder trust question with no clear selection"))):
            self.assertEqual(so.classify(head + options + tail), want, options)

    def test_new_trust_wording_still_moves_down_then_enter(self):
        self.assertEqual(so.classify(cap("menu"))[2], ["Down"])
        self.assertEqual(so.classify(fx.TRUST_YES)[2], ["Enter"])

    def test_old_wording_on_no_never_gets_enter(self):
        self.spinning = False
        self.pane = "Do you trust the files in this folder?\n   1. Yes, proceed\n ❯ 2. No, exit\nEnter to confirm"
        self.run_loop(60)
        self.assertNotIn(["Enter"], self.keys)
        self.assertEqual(self.keys[0], ["Up"])


class RestartTest(fx.LoopCase):
    """Finding 4: a replacement supervisor (same state dir and backend) never pastes a held message again."""

    def replace_supervisor(self):
        self.sup = so.Supervisor("steward")
        self.sup.agent = self.fake_agent

    def hold_message_one(self):
        self.be.add("URGENT bus #%d hello")
        self.send_raise_after = subprocess.TimeoutExpired("agent-exec", 60)   # pasted (box empty again), outcome unknown
        self.run_loop(330)
        self.send_raise_after = None
        self.assertEqual((self.pasted, self.be.delivered(), self.be.types("delivery-unknown")), ([1], [], [None]))

    def test_replacement_supervisor_keeps_the_hold(self):
        self.hold_message_one()
        self.replace_supervisor()
        self.run_loop(150)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([1], 0, []))
        self.assertIn(1, self.sup.held)
        self.assertEqual(self.be.types("delivery-unknown"), [None])   # not reported again

    def test_release_as_delivered_drops_the_hold_without_typing(self):
        self.hold_message_one()
        self.be.msgs[0]["delivered"] = True   # swarmctl orchestrator release steward 1
        self.be.add("bus #%d next")
        self.replace_supervisor()
        self.run_loop(150)
        self.assertEqual(self.pasted, [1, 2])
        self.assertEqual(self.sup.held, {})
        self.assertEqual(json.loads(Path(self.sup.state_file).read_text())["held"], {})

    def test_release_with_resend_pastes_it_once_more(self):
        self.hold_message_one()
        self.replace_supervisor()
        self.run_loop(60)
        self.assertEqual(self.pasted, [1])
        self.be.msgs[0]["release"] = 1   # swarmctl orchestrator release steward 1 --resend
        self.run_loop(60)
        self.assertEqual((self.pasted, self.be.delivered()), ([1, 1], [1]))
        self.run_loop(60)
        self.assertEqual(self.pasted, [1, 1])

    def test_unsubmitted_paste_survives_a_restart_and_only_gets_enter(self):
        self.be.add("bus #%d hello")
        self.send_rc, self.keys_rc = [6], [1] * 100   # pasted, Enter keeps failing
        self.run_loop(110)
        self.assertEqual((self.pasted, self.be.delivered()), ([1], []))
        self.replace_supervisor()
        self.keys_rc = []
        self.run_loop(30)
        self.assertEqual((self.pasted, self.enters, self.be.delivered()), ([1], 1, [1]))

    def test_receipt_pending_survives_a_restart(self):
        self.be.add("bus #%d hello")
        self.be.fail = 1000   # every heartbeat fails after the paste
        self.run_loop(3)
        self.be.fail = 0
        self.run_loop(1)
        self.be.fail = 1000
        self.run_loop(110)
        self.assertEqual((self.pasted, self.be.delivered()), ([1], []))
        self.be.fail = 0
        self.replace_supervisor()
        self.run_loop(60)
        self.assertEqual((self.pasted, self.be.delivered()), ([1], [1]))


class EnterRetryReportTest(fx.LoopCase):
    """Finding 5: repeated Enter timeouts or failures (text still in the box) are reported, never acknowledged."""

    def repeated_enter(self, failure):
        self.be.add("URGENT bus #%d hello")
        self.send_rc, self.keys_rc = [6], [1] * 400
        real = self.fake_agent

        def agent(*args, **kw):
            if failure == "timeout" and args[0] == "orch-keys":
                raise subprocess.TimeoutExpired("agent-exec", 60)
            return real(*args, **kw)
        self.sup.agent = agent
        self.run_loop(220)
        return [t.split("reason=")[1] for t in self.be.texts if "reason=" in t]

    def test_enter_timeouts(self):
        self.assertEqual(self.repeated_enter("timeout"), ["submit_unknown"])
        self.assertEqual((self.pasted, self.be.delivered()), ([1], []))

    def test_enter_failures(self):
        self.assertEqual(self.repeated_enter("exit"), ["submit_failed"])
        self.assertEqual((self.pasted, self.be.delivered()), ([1], []))


class Http400Test(fx.LoopCase):
    """Finding 6: only a confirmed unsupported-event error drops the 0.8.1 events."""

    def test_unrelated_400_keeps_the_held_message_event(self):
        self.be.add("bus #%d hello")
        self.sup.hold(1, "probe")
        self.sup.event("adopt", "control")
        self.be.reject = "bad state"
        self.assertFalse(self.sup.beat())
        self.assertEqual([e["type"] for e in self.sup.events], ["delivery-unknown", "adopt"])
        self.assertEqual(self.sup.blocked, "HTTP 400")   # the normal refusal state
        self.assertTrue(self.sup.beat())
        self.assertEqual(self.be.types("delivery-unknown"), [None])
        self.assertIsNone(self.sup.blocked)

    def test_confirmed_unsupported_event_falls_back(self):
        self.sup.hold(1, "probe")
        self.sup.event("adopt", "control")
        self.be.reject = "bad event type 'delivery-unknown'"
        self.assertFalse(self.sup.beat())
        self.assertEqual([e["type"] for e in self.sup.events], ["adopt"])
        self.assertIsNone(self.sup.blocked)

    def test_error_body_is_read_from_the_real_backend_reply(self):
        def err(body):
            return urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(json.dumps(body).encode()))
        self.assertEqual(so.unsupported_event(err({"error": "bad event type 'delivery-deferred'"})), "delivery-deferred")
        self.assertIsNone(so.unsupported_event(err({"error": "bad state"})))
        self.assertIsNone(so.unsupported_event(err({"error": "bad event type 'delivery-unknown' and more"})))
        self.assertIsNone(so.unsupported_event(urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"not json"))))


if __name__ == "__main__":
    unittest.main()
