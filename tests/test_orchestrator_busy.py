"""0.8.1 (agent-control PR 64): queued messages reach an orchestrator during a long busy turn.

The supervisor object is driven directly with a fake clock, a fake pane and a fake backend; no tmux, no network.
"""
import sys
import unittest
from unittest.mock import patch

from helpers import SERVER

sys.path.insert(0, SERVER)
import swarm_orchestrator as so  # noqa: E402

RULE = "─" * 64


def _pane(*input_lines, above="", below="  ⏵⏵ bypass permissions on (shift+tab to cycle)"):
    return "⏺ Working.\n" + above + RULE + "\n" + "\n".join(input_lines) + "\n" + RULE + "\n" + below + "\n"


BUSY_PANE = _pane("❯ ", above="✻ Thinking… (esc to interrupt)\n")


class InputDraftTest(unittest.TestCase):
    def test_not_a_draft(self):
        for name, pane in {
            "empty": _pane("❯ " + " " * 40),
            "placeholder": _pane('❯ Try "fix lint errors"'),
            "queued hint": _pane("❯ Press up to edit queued messages"),
            "queued shown": _pane("❯ ", above="  fix the thing\n  Queued · ctrl+x ctrl+s to send now\n"),
            "queued line in box": _pane("❯ ", "  Queued · ctrl+x ctrl+s to send now"),
            "boxed empty": "╭" + "─" * 40 + "╮\n│ ❯  │\n╰" + "─" * 40 + "╯\n",
        }.items():
            self.assertFalse(so.input_draft(pane), name)

    def test_draft(self):
        for name, pane in {
            "single": _pane("❯ half-typed thought"),
            "multiline empty first": _pane("❯ ", "second line draft"),
            "multiline": _pane("❯ first", "  second"),
            "wrapped": _pane("❯ " + "word " * 30, "continued text"),
            "hint plus text": _pane('❯ Try "fix lint errors" and more'),
            "menu": "Which layout?\n ❯ 1. Grid\n   2. List\n",
            "no rules": "⏺ x\n❯ \n",
            "no lower rule": "⏺ x\n" + RULE + "\n❯ \n",
            "no prompt": "⏺ x\n",
        }.items():
            self.assertTrue(so.input_draft(pane), name)

    def test_menu(self):
        self.assertTrue(so.pane_menu("Which layout?\n ❯ 1. Grid\n   2. List\n"))
        self.assertTrue(so.pane_menu("Do it?\n Esc to cancel"))
        self.assertFalse(so.pane_menu(BUSY_PANE))

    def test_urgent_is_read_from_the_first_line_only(self):
        self.assertTrue(so.is_urgent({"text": "URGENT bus #1 thread t | reply\n\nbody"}))
        self.assertFalse(so.is_urgent({"text": "bus #1 thread t\n\nURGENT in the body"}))


class BusyDeliveryTest(unittest.TestCase):
    """Mid-turn delivery: the spinner timer keeps the pane changing, so a message that waited long enough is pasted."""

    def setUp(self):
        self.now = 1000.0
        p = patch.object(so.time, "time", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        s = self.s = so.Supervisor.__new__(so.Supervisor)
        s.name = "steward"
        s.message, s.delivered, s.events = None, None, []
        s.pending_since, s.last_busy_paste, s.pasted_ids = None, 0, set()
        s.last_beat, s.beat_ok = 0, True
        s.tail, s.screen, s.tail_changed = "", BUSY_PANE, self.now
        s.queue, s.pasted, s.receipts, s.receipt_ok = [], [], [], True
        s.agent = lambda *a, **k: (s.pasted.append(s.message["id"]) if a[0] == "orch-send" else None) or type("R", (), {"returncode": 0})()
        s.write_inbox = lambda *a: None
        s.beat = self.fake_beat

    def fake_beat(self):
        s = self.s
        s.last_beat = self.now
        if s.delivered is not None:
            if not s.receipt_ok:
                return False
            s.receipts.append(s.delivered)
            s.queue = [m for m in s.queue if m["id"] != s.delivered]
            s.delivered = None
        if s.queue and (not s.message or s.message["id"] != s.queue[0]["id"]):
            s.message = s.queue[0]
        return True

    def msg(self, mid, text="bus #%d hello"):
        self.s.queue.append({"id": mid, "sender": "chief", "text": text % mid if "%d" in text else text})

    def spin(self, n, pane=None):
        """n seconds of a pane that changes every second (a ticking spinner)."""
        s = self.s
        for _ in range(n):
            self.now += 1
            s.tail, s.tail_changed = f"tick {self.now}", self.now
            s.screen = (pane or BUSY_PANE) + f"timer {self.now}\n"
            s.handle_messages()

    def types(self):
        return [e["type"] for e in self.s.events]

    def test_busy_pane_delivers_after_max_wait(self):
        self.msg(1)
        self.spin(80)
        self.assertEqual(self.s.pasted, [])
        self.spin(20)
        self.assertEqual(self.s.pasted, [1])
        self.assertEqual(self.s.receipts, [1])
        self.assertIn("message-delivered-busy", self.types())

    def test_urgent_delivers_sooner(self):
        self.msg(2, "URGENT bus #2 thread t | reply\n\nbody")
        self.spin(10)
        self.assertEqual(self.s.pasted, [])
        self.spin(20)
        self.assertEqual(self.s.pasted, [2])

    def test_menu_blocks(self):
        self.msg(3)
        self.spin(120, "Which layout?\n ❯ 1. Grid\n   2. List\n")
        self.assertEqual(self.s.pasted, [])

    def test_typed_input_blocks_then_clears(self):
        self.msg(4)
        self.spin(120, _pane("❯ half-typed thought"))
        self.assertEqual(self.s.pasted, [])
        self.spin(10)  # input cleared
        self.assertEqual(self.s.pasted, [4])

    def test_multiline_draft_with_empty_first_line_blocks_and_hints_do_not(self):
        self.msg(9)
        self.spin(120, _pane("❯ ", "draft on line two"))
        self.assertEqual(self.s.pasted, [])
        self.spin(10, _pane("❯ Press up to edit queued messages"))
        self.assertEqual(self.s.pasted, [9])

    def test_ambiguous_layout_blocks(self):
        self.msg(11)
        self.spin(120, "⏺ working\n❯ \n")
        self.assertEqual(self.s.pasted, [])

    def test_failed_receipt_is_retried_without_repaste(self):
        self.msg(10)
        self.s.receipt_ok = False
        self.spin(100)
        self.assertEqual(self.s.pasted, [10])
        self.assertEqual(self.s.receipts, [])
        self.s.receipt_ok = True
        self.spin(20)
        self.assertEqual(self.s.pasted, [10])
        self.assertEqual(self.s.receipts, [10])

    def test_backend_returning_a_pasted_message_again_is_not_repasted(self):
        self.msg(12)
        self.spin(100)
        self.assertEqual(self.s.pasted, [12])
        self.s.message = {"id": 12, "sender": "chief", "text": "bus #12 hello"}   # backend had not stored the receipt
        self.s.queue = [self.s.message]
        self.spin(10)
        self.assertEqual(self.s.pasted, [12])
        self.assertEqual(self.s.receipts, [12, 12])

    def test_idle_path_unchanged(self):
        self.s.tail_changed = self.now - 100
        self.s.tail = "> \n"
        self.msg(5)
        self.s.beat()
        self.s.handle_messages()
        self.assertEqual(self.s.pasted, [5])
        self.assertNotIn("message-delivered-busy", self.types())
        self.assertEqual(self.s.tail_changed, self.now)

    def test_backlog_drains_in_busy_mode(self):
        for i in (6, 7, 8):
            self.msg(i)
        self.s.beat()
        self.spin(89)
        self.assertEqual(self.s.pasted, [])
        self.spin(30)  # first at ~90 s, the rest each busy gap later, not one per idle window or max wait
        self.assertEqual(self.s.pasted, [6, 7, 8])
        gaps = [e for e in self.types() if e == "message-delivered-busy"]
        self.assertEqual(len(gaps), 3)

    def test_gap_between_busy_pastes(self):
        for i in (6, 7):
            self.msg(i)
        self.s.beat()
        self.spin(91)
        self.assertEqual(self.s.pasted, [6])
        self.spin(3)
        self.assertEqual(self.s.pasted, [6])
        self.spin(3)
        self.assertEqual(self.s.pasted, [6, 7])


if __name__ == "__main__":
    unittest.main()
