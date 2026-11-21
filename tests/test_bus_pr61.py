"""PR 61 fix round ("stored means accepted"); mirrors agent-control tests/bus_pr61_fixes.test.mjs plus the extra case in
bus_review_fixes.test.mjs. Faults are injected into the Store's run/one/q (the kit's database layer)."""
import contextlib
import io
import re
import unittest

import test_bus
import test_bus_notices as tn


@contextlib.contextmanager
def fault(store, pattern, message, after=False, methods=("run", "one", "q")):
    """Make statements matching `pattern` raise (after = run them first, then raise). Restores on exit."""
    rx = re.compile(pattern)
    saved = {m: getattr(store, m) for m in methods}

    def wrap(name):
        def f(sql, *args):
            if rx.search(sql):
                if after:
                    saved[name](sql, *args)
                raise RuntimeError(message)
            return saved[name](sql, *args)
        return f
    for m in methods:
        setattr(store, m, wrap(m))
    try:
        yield
    finally:
        for m, fn in saved.items():
            setattr(store, m, fn)


@contextlib.contextmanager
def quiet():
    """Capture stderr so expected failure logs stay out of the test output, and expose them."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


class FinishNoticeStoredMeansAccepted(tn.NoticeBase):
    def test_pre_store_insert_failure_still_uses_the_legacy_fallback(self):
        with fault(self.api.s, r"^INSERT INTO bus_messages", "injected pre-store failure"), quiet():
            self.finished("clozy-bot", "tp1")
        self.assertEqual(self.bus_msgs(), [])
        self.assertEqual([n["target"] for n in self.notice_rows()], ["clozy-bot"], "normal path ran once")

    def test_read_failure_after_storage_does_not_fall_back(self):
        with fault(self.api.s, r"^SELECT m\.\* FROM bus_messages m WHERE m\.id = \?$", "injected read failure", methods=("one",)), quiet():
            self.finished("clozy-bot", "tp2")
        self.assertEqual(len(self.bus_msgs()), 1)
        self.assertEqual(self.notice_rows(), [], "no legacy delivery after a stored bus notice")

    def test_clear_unreachable_failure_after_storage_does_not_fall_back(self):
        self.api.s.run("UPDATE agents SET unreachable_since = 1 WHERE name = 'clozy'")
        with fault(self.api.s, r"^UPDATE agents SET unreachable_since = NULL", "injected cleanup failure"), quiet():
            self.finished("clozy-bot", "tp3")
        self.assertEqual(len(self.bus_msgs()), 1)
        self.assertEqual(self.notice_rows(), [])

    def test_committed_tmux_enqueue_whose_response_fails_does_not_send_the_legacy_notice(self):
        b = self.b
        b.bot("app-bot", projects=("app",))
        b.req("POST", "/worker/claim", {"worker": "w1"}, tok=tn.__dict__.get("WORKER") or __import__("helpers").WORKER)
        b.req("POST", "/orchestrators", {"name": "polisher", "worker_id": "w1", "project": "app", "owner_bot_id": "app-bot",
                                         "brief": "Keep improving.", "start": True})
        test_bus.beat(b, "polisher", state="running")
        self.reg("polisher", {"kind": "orchestrator", "wake": "tmux", "orchestrator": "polisher", "notices": "bus",
                              "aliases": ["clozy-bot-orch"]})
        self.api.s.run("UPDATE agents SET bot_id = 'clozy-bot' WHERE name = 'polisher'")
        self.api.s.run("UPDATE agents SET notices = 'legacy' WHERE name = 'clozy'")
        with fault(self.api.s, r"^INSERT INTO orchestrator_messages", "injected response failure after enqueue", after=True), quiet():
            self.finished("clozy-bot", "tp4")
        self.assertEqual(len(self.bus_msgs("recipient = 'polisher'")), 1)
        self.assertEqual(len(self.api.s.q("SELECT * FROM orchestrator_messages WHERE bus_message_id IS NOT NULL")), 1)
        self.assertEqual(self.notice_rows(), [])
        self.api.bus.flush(now=int(__import__("time").time()) + 180)
        self.assertEqual(len(self.api.s.q("SELECT * FROM orchestrator_messages WHERE bus_message_id IS NOT NULL")), 1)


class TmuxDispatch(test_bus.BusCase):
    def send_failing(self, **kw):
        with fault(self.api.s, r"^INSERT INTO orchestrator_messages", "injected tmux failure"), quiet():
            return self.bus.send(dict({"from": "chief", "to": "polisher", "body": "hello"}, **kw))

    def om(self):
        return self.rows("SELECT * FROM orchestrator_messages WHERE bus_message_id IS NOT NULL")

    def test_enqueue_failure_after_storage_is_accepted_and_repaired_once(self):
        st, d = self.send_failing(client_msg_id="review-tmux")
        self.assertEqual(st, 201)
        self.assertEqual((self.count(), self.om()), (1, []))
        again = {"from": "chief", "to": "polisher", "body": "hello", "client_msg_id": "review-tmux"}
        self.assertEqual(self.bus.send(again)[0], 200)
        self.assertEqual(len(self.om()), 1, "duplicate retry repaired it")
        self.assertEqual(self.bus.send(again)[0], 200)
        self.flush(180)
        self.assertEqual(len(self.om()), 1, "no double enqueue")

    def test_duplicate_to_another_recipient_repairs_the_stored_recipient_only(self):
        self.add_orch("beta")
        self.b.req("PUT", "/bus/agents/beta", {"kind": "orchestrator", "wake": "tmux", "orchestrator": "beta"}, expect=201)
        st, _ = self.send_failing(client_msg_id="x-recip")
        self.assertEqual(st, 201)
        st, d = self.bus.send({"from": "chief", "to": "beta", "body": "other", "client_msg_id": "x-recip"})
        self.assertEqual((st, d["to"]), (200, "polisher"))
        om = self.om()
        self.assertEqual([(o["name"], "hello" in o["text"]) for o in om], [("polisher", True)])

    def test_flush_repairs_a_stored_message_with_no_orchestrator_row(self):
        self.send_failing()
        self.flush(10)
        self.assertEqual(self.om(), [], "inside the 30 s grace")
        self.flush(180)
        om = self.om()
        self.assertEqual(len(om), 1)
        self.assertEqual(om[0]["bus_message_id"], self.rows("SELECT id FROM bus_messages")[0]["id"])
        self.flush(240)
        self.assertEqual(len(self.om()), 1)

    def test_enqueue_is_idempotent_per_bus_message_id(self):
        self.bus.send({"from": "chief", "to": "polisher", "body": "once"})
        m = self.rows("SELECT * FROM bus_messages")[0]
        a = self.agent("polisher")
        self.bus.tmux_enqueue(m, a)
        self.bus.tmux_enqueue(m, a)
        self.assertEqual(len(self.om()), 1)

    def test_http_send_and_alerts_to_a_tmux_agent_survive_a_failed_enqueue(self):
        with fault(self.api.s, r"^INSERT INTO orchestrator_messages", "injected tmux failure"), quiet():
            st, out = self.b.call("POST", "/bus/send", {"to": "polisher", "body": "via http", "from": "chief"})
            self.assertEqual(st, 201, out)
            self.bus.alert_agent = "polisher"
            self.assertIs(self.bus.alert_to_agent("k1", "alert"), True)
        self.assertEqual(len(self.rows("SELECT * FROM bus_messages WHERE recipient = 'polisher'")), 2)


class UnreachableBookkeeping(test_bus.BusCase):
    def test_final_read_failure_still_records_the_episode_and_flush_alerts_at_15_minutes(self):
        with fault(self.api.s, r"^SELECT m\.\* FROM bus_messages m WHERE m\.id = \?$", "injected read failure", methods=("one",)), quiet():
            st, _ = self.bus.send({"from": "gil", "to": "quiet", "body": "hello"}, now=self.t0)
        self.assertEqual(st, 201)
        self.assertTrue(self.agent("quiet")["unreachable_since"], "episode started despite the failed read")
        self.flush(20 * 60)
        self.assertGreaterEqual(len(self.rows("SELECT * FROM bus_messages WHERE recipient = 'chief' AND sender = 'agent-control'")), 1)

    def test_a_failed_episode_write_is_discovered_by_the_flush_sweep(self):
        self.api.s.run("UPDATE agents SET last_seen_at = NULL WHERE name = 'quiet'")
        with fault(self.api.s, r"^UPDATE agents SET unreachable_since = COALESCE", "injected episode write failure"), quiet():
            st, _ = self.bus.send({"from": "gil", "to": "quiet", "body": "hello"}, now=self.t0)
        self.assertEqual(st, 201)
        self.assertIsNone(self.agent("quiet")["unreachable_since"])
        self.flush(0)
        self.assertTrue(self.agent("quiet")["unreachable_since"], "sweep started the episode")
        self.flush(20 * 60)
        self.assertGreaterEqual(len(self.rows("SELECT * FROM bus_messages WHERE recipient = 'chief' AND sender = 'agent-control'")), 1)

    def test_sweep_never_starts_an_episode_for_humans_or_system_mail(self):
        self.bus.send({"from": "chief", "to": "gil", "body": "for the human"}, now=self.t0)
        self.api.s.run("UPDATE agents SET last_seen_at = NULL WHERE name IN ('gil', 'quiet')")
        self.bus.send({"from": "agent-control", "to": "quiet", "body": "system mail"}, now=self.t0)
        self.api.s.run("UPDATE agents SET unreachable_since = NULL")
        self.flush(0)
        self.assertIsNone(self.agent("gil")["unreachable_since"])
        self.assertIsNone(self.agent("quiet")["unreachable_since"])


class SafeLogs(test_bus.BusCase):
    def test_post_store_logs_never_contain_the_exception_text_of_a_secret(self):
        marker = "Bearer REVIEW_SYNTHETIC_SECRET_ONLY"
        with fault(self.api.s, r"^SELECT m\.\* FROM bus_messages m WHERE m\.id = \?$", marker, methods=("one",)), quiet() as buf:
            st, _ = self.bus.send({"from": "chief", "to": "gil", "body": "hello"})
        self.assertEqual(st, 201)
        log = buf.getvalue()
        self.assertTrue(log, "something was logged")
        self.assertNotIn("REVIEW_SYNTHETIC_SECRET_ONLY", log)
        self.assertNotIn("Bearer", log)
        self.assertIn("RuntimeError", log)

    def test_safe_err_redacts_secrets_and_bounds_length(self):
        e = self.bus.safe_err
        self.assertNotIn("ghp_", e(ValueError("boom " + tn.FAKE_SECRET)))
        self.assertLess(len(e(ValueError("x" * 5000))), 250)
        self.assertEqual(e(TypeError("plain failure")), "TypeError: plain failure")
        self.assertNotIn("ab_chief", e(ValueError("bad ab_chief.1.abc")))
        self.assertNotIn("\n", e(ValueError("a\nb")))


if __name__ == "__main__":
    unittest.main()
