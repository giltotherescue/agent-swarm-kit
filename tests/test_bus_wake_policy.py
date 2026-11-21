"""Bus wake policy and digests, unreachable alerts (mirrors agent-control tests/bus_wake_policy.test.mjs).
Kit differences: timestamps are whole seconds, send() only stores and sets bus.wake, and bus.flush(now=) is both the delivery loop
and the cron. Webhooks go through a fake poster, so nothing touches the network."""
import contextlib
import io
import json
import threading
import time
import unittest

from helpers import Backend

HOOK = "http://127.0.0.1:9/clozy"
MIN = 60


class Hook:
    """Fake webhook transport: poster(url, data_bytes, headers) -> (status, body_bytes)."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.lock = threading.Lock()

    def __call__(self, url, data, headers):
        with self.lock:
            self.calls.append({"url": url, "body": json.loads(data), "headers": headers})
        return self.status, b"{}"

    def kinds(self):
        return [c["body"]["kind"] for c in self.calls]


class WakeBase(unittest.TestCase):
    digest_minutes = None

    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.api = self.b.api
        self.bus = self.api.bus
        self.hook = Hook()
        self.bus.poster = self.hook
        self.t0 = int(time.time())
        self.b.bot("clozy-bot", url=HOOK)
        body = {"kind": "bot", "wake": "webhook", "bot_id": "clozy-bot", "notices": "bus"}
        if self.digest_minutes is not None:
            body["digest_minutes"] = self.digest_minutes
        self.reg("clozy", body)
        self.reg("polisher", {"kind": "service", "wake": "poll"})
        self.reg("chief", {"kind": "bot", "wake": "poll"})
        self.seen()

    def seen(self, *names):
        """Poll agents are reachable when they checked in recently (the real clock; flush(now=) does not move it)."""
        for n in names or ("polisher", "chief"):
            self.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = ?", int(time.time()), n)

    def reg(self, name, body):
        status, out = self.b.call("PUT", f"/bus/agents/{name}", body)
        self.assertLess(status, 300, (name, status, out))
        return status, out

    def send(self, body, now=None, frm="polisher", to="clozy"):
        st, data = self.bus.send({"from": frm, "to": to, **body}, now=self.t0 if now is None else now)
        return dict(data, status=st)

    def msgs(self, where="1=1", *args):
        return self.api.s.q(f"SELECT * FROM bus_messages WHERE {where} ORDER BY id", *args)

    def agent(self, name="clozy"):
        return self.api.s.one("SELECT * FROM agents WHERE name = ?", name)

    def system_to(self, name):
        return self.msgs("sender = 'agent-control' AND recipient = ?", name)

    def flush(self, at):
        return self.bus.flush(now=self.t0 + at)


class WakeNowTest(WakeBase):
    def test_urgent_wakes_now_and_the_response_says_so(self):
        r = self.send({"body": "blocked on X", "urgent": True})
        self.assertEqual((r["status"], r["wake"], r["state"]), (201, "now", "queued"))
        self.assertTrue(self.bus.wake.is_set())
        self.assertEqual(self.hook.calls, [], "send only stores; delivery is the flush")
        self.flush(0)
        self.assertEqual(self.msgs()[0]["state"], "delivered")
        self.assertEqual(self.hook.kinds(), ["bus_message"])
        p = self.hook.calls[0]["body"]
        self.assertIs(p["reply_expected"], True)  # urgent defaults to reply expected
        self.assertIn("reply_expected", p["etiquette"])
        self.assertEqual(self.hook.calls[0]["headers"]["Idempotency-Key"], f"bus:{r['id']}")

    def test_non_urgent_is_held_for_the_digest(self):
        r = self.send({"body": "progress 40%"})
        self.assertEqual((r["wake"], r["state"]), ("digest", "queued"))
        self.assertFalse(self.bus.wake.is_set(), "a held message does not wake the delivery loop")
        self.flush(1)
        self.assertEqual(self.hook.calls, [])
        m = self.msgs()[0]
        self.assertEqual((m["held"], m["reply_expected"], m["wake"], m["urgent"]), (1, 0, "digest", 0))
        r2 = self.send({"body": "question?", "reply_expected": True})
        self.assertEqual(r2["wake"], "digest")
        self.assertEqual(self.msgs()[1]["reply_expected"], 1)

    def test_poll_recipient_waits_in_the_inbox(self):
        r = self.send({"body": "fyi"}, frm="clozy", to="chief")
        self.assertEqual(r["wake"], "inbox")
        self.assertFalse(self.msgs()[0]["held"])

    def test_orchestrator_question_and_blocker_meta_are_urgent(self):
        self.reg("orch", {"kind": "orchestrator", "wake": "poll"})
        q = self.send({"body": "which db?", "reply_expected": True}, frm="orch")
        self.assertEqual(q["wake"], "now")
        self.assertEqual(self.msgs("id = ?", q["id"])[0]["urgent"], 1)
        # an orchestrator progress ping (no question) is still a digest
        self.assertEqual(self.send({"body": "step 3 done"}, frm="orch")["wake"], "digest")
        # reply_expected from a non-orchestrator is not urgent by itself
        self.assertEqual(self.send({"body": "q?", "reply_expected": True})["wake"], "digest")
        # the blocker flag forces urgent for anyone
        b = self.send({"body": "stuck", "meta": {"blocker": True}})
        self.assertEqual(b["wake"], "now")
        row = self.msgs("id = ?", b["id"])[0]
        self.assertEqual((row["urgent"], row["reply_expected"]), (1, 1))
        self.assertEqual(self.send({"body": "x", "meta": {"blocker": False}})["wake"], "digest")
        self.assertEqual(self.send({"body": "x", "noop": True, "meta": {"blocker": True}})["status"], 400)

    def test_tmux_always_wakes_now(self):
        self.b.bot("app-bot", url=HOOK, projects=("app",))
        self.b.req("POST", "/worker/claim", {"worker": "w1"}, tok="worker-tok-for-tests")
        self.b.req("POST", "/orchestrators", {"name": "steward", "worker_id": "w1", "project": "app", "brief": "Keep going."})
        self.api.s.run("UPDATE orchestrators SET state = 'running', desired = 'running', last_heartbeat_at = ? WHERE name = 'steward'",
                       int(time.time()))
        self.reg("steward", {"kind": "orchestrator", "wake": "tmux", "orchestrator": "steward"})
        r = self.send({"body": "plain ping"}, to="steward")
        self.assertEqual((r["status"], r["wake"]), (201, "now"))
        row = self.api.s.one("SELECT * FROM orchestrator_messages WHERE bus_message_id = ?", r["id"])
        self.assertIsNotNone(row)
        self.assertFalse(self.msgs("id = ?", r["id"])[0]["held"])
        self.assertEqual(self.hook.calls, [])


class WakeNoopTest(WakeBase):
    def test_noop_never_wakes_and_is_delivered(self):
        r = self.send({"body": "still working", "noop": True})
        self.assertEqual((r["status"], r["wake"], r["state"]), (201, "never", "delivered"))
        m = self.msgs()[0]
        self.assertEqual((m["noop"], m["held"], m["state"]), (1, 0, "delivered"))
        self.assertIsNotNone(m["delivered_at"])
        self.flush(500 * MIN)
        self.assertEqual(self.hook.calls, [])

    def test_noop_cannot_be_urgent_or_a_blocker(self):
        r = self.send({"body": "x", "noop": True, "urgent": True})
        self.assertEqual(r["status"], 400)
        self.assertNotIn("wake", r)
        self.assertEqual(self.send({"body": "x", "noop": True, "meta": {"blocker": True}})["status"], 400)
        self.assertEqual(self.msgs(), [])

    def test_noop_from_an_orchestrator_with_a_question_is_not_urgent(self):
        self.reg("orch", {"kind": "orchestrator", "wake": "poll"})
        r = self.send({"body": "fyi", "noop": True, "reply_expected": True}, frm="orch")
        self.assertEqual((r["status"], r["wake"]), (201, "never"))
        self.assertEqual(self.msgs()[0]["urgent"], 0)

    def test_idempotent_retries_repeat_the_wake_field(self):
        a = self.send({"body": "ping", "client_msg_id": "k1"})
        b = self.send({"body": "ping", "client_msg_id": "k1"})
        self.assertEqual((a["wake"], b["status"], b["duplicate"], b["wake"], b["id"]), ("digest", 200, True, "digest", a["id"]))
        u = self.send({"body": "now", "urgent": True, "client_msg_id": "k2"})
        v = self.send({"body": "now", "urgent": True, "client_msg_id": "k2"})
        self.assertEqual((u["wake"], v["wake"], v["duplicate"]), ("now", "now", True))
        n = self.send({"body": "zz", "noop": True, "client_msg_id": "k3"})
        n2 = self.send({"body": "zz", "noop": True, "client_msg_id": "k3"})
        self.assertEqual((n["wake"], n2["wake"], n2["duplicate"]), ("never", "never", True))
        self.assertEqual(len(self.msgs()), 3)


class DigestTest(WakeBase):
    def test_digest_batches_into_one_post_and_each_message_acks_alone(self):
        ids = [self.send({"body": f"ping {i}"})["id"] for i in range(5)]
        self.flush(30 * MIN)
        self.assertEqual(self.hook.calls, [], "interval not reached")
        s = self.flush(61 * MIN)
        self.assertEqual(s["digests"], 1)
        self.assertEqual(len(self.hook.calls), 1)
        p = self.hook.calls[0]["body"]
        self.assertEqual((p["kind"], p["count"], p["to"]), ("bus_digest", 5, "clozy"))
        self.assertEqual([m["id"] for m in p["messages"]], ids)
        self.assertEqual(p["messages"][0]["ack"], {"method": "POST", "path": f"/bus/messages/{ids[0]}/ack"})
        self.assertEqual([m["ack"]["path"] for m in p["messages"]], [f"/bus/messages/{i}/ack" for i in ids])
        self.assertIs(p["messages"][0]["reply_expected"], False)
        self.assertIn("reply_expected", p["etiquette"])
        self.assertTrue(all(m["state"] == "delivered" and m["held"] == 0 for m in self.msgs()))
        # nothing pending: no second digest
        self.flush(200 * MIN)
        self.assertEqual(len(self.hook.calls), 1)
        # each message acks on its own, once
        tok = self.b.req("GET", "/bus/agents/clozy/token")["token"]
        a1 = self.b.req("POST", f"/bus/messages/{ids[0]}/ack", {}, tok=tok)
        a2 = self.b.req("POST", f"/bus/messages/{ids[0]}/ack", {}, tok=tok)
        self.assertEqual((a1["first"], a2["first"]), (True, False))
        self.assertEqual(len(self.msgs("read_at IS NOT NULL")), 1)

    def test_default_interval_is_60_minutes(self):
        self.assertEqual(self.agent()["digest_minutes"], 60)
        self.send({"body": "a"})
        self.flush(59 * MIN)
        self.assertEqual(self.hook.calls, [])
        self.flush(60 * MIN)
        self.assertEqual(self.hook.kinds(), ["bus_digest"])

    def test_digest_not_when_last_digest_was_recent(self):
        self.send({"body": "a"})
        self.api.s.run("UPDATE agents SET last_digest_at = ? WHERE name = 'clozy'", self.t0 + 50 * MIN)
        self.flush(61 * MIN)
        self.assertEqual(self.hook.calls, [], "a digest went out 11 minutes ago")
        self.flush(111 * MIN)
        self.assertEqual(self.hook.kinds(), ["bus_digest"])

    def test_digest_minutes_validation(self):
        for bad in (0, 4, -1, 10081, "60", 60.5, True):
            with self.subTest(bad=bad):
                st, out = self.b.call("PUT", "/bus/agents/clozy", {"digest_minutes": bad})
                self.assertEqual(st, 400, out)
        for ok in (5, 10, 10080):
            with self.subTest(ok=ok):
                st, out = self.b.call("PUT", "/bus/agents/clozy", {"digest_minutes": ok})
                self.assertEqual((st, out["digest_minutes"]), (200, ok))
        self.assertEqual(self.agent()["digest_minutes"], 10080)
        self.assertEqual(self.b.call("PUT", "/bus/agents/clozy", {"digest_minutes": 60})[1]["digest_minutes"], 60)

    def test_digest_minutes_zero_and_four_are_rejected_explicitly(self):
        self.assertEqual(self.b.call("PUT", "/bus/agents/clozy", {"digest_minutes": 0})[0], 400)
        self.assertEqual(self.b.call("PUT", "/bus/agents/clozy", {"digest_minutes": 4})[0], 400)
        self.assertEqual(self.agent()["digest_minutes"], 60)


class DigestIntervalTest(WakeBase):
    digest_minutes = 10

    def test_interval_is_per_agent_and_counts_from_the_last_digest(self):
        self.assertEqual(self.agent()["digest_minutes"], 10)
        self.send({"body": "a"})
        self.flush(5 * MIN)
        self.assertEqual(len(self.hook.calls), 0)
        self.flush(11 * MIN)
        self.assertEqual(len(self.hook.calls), 1)
        # a new message right after a digest waits for the interval since that digest
        self.send({"body": "b"}, now=self.t0 + 11 * MIN)
        self.flush(15 * MIN)
        self.assertEqual(len(self.hook.calls), 1)
        self.flush(22 * MIN)
        self.assertEqual(len(self.hook.calls), 2)
        self.assertEqual(self.agent()["last_digest_at"], self.t0 + 22 * MIN)


class DigestThirtyTest(WakeBase):
    digest_minutes = 30

    def test_thirty_minute_digest(self):
        self.reg("orch", {"kind": "orchestrator", "wake": "poll"})
        self.send({"body": "step 3 done"}, frm="orch")
        self.flush(20 * MIN)
        self.assertEqual(self.hook.calls, [])
        self.flush(31 * MIN)
        self.assertEqual(self.hook.kinds(), ["bus_digest"])


class UrgentWakeTest(WakeBase):
    def test_urgent_wake_carries_pending_items_and_sets_last_digest(self):
        a = self.send({"body": "ping a"})
        b = self.send({"body": "ping b"})
        u = self.send({"body": "blocker", "urgent": True})
        self.flush(0)
        self.assertEqual(len(self.hook.calls), 1)
        p = self.hook.calls[0]["body"]
        self.assertEqual(p["kind"], "bus_message")
        self.assertEqual([m["id"] for m in p["pending"]], [a["id"], b["id"]])
        self.assertEqual(p["pending_count"], 2)
        self.assertEqual(p["pending"][0]["ack"]["path"], f"/bus/messages/{a['id']}/ack")
        self.assertEqual(p["id"], u["id"])
        self.assertTrue(all(m["state"] == "delivered" and m["held"] == 0 for m in self.msgs()))
        self.assertEqual(self.agent()["last_digest_at"], self.t0)

    def test_failed_urgent_post_leaves_pending_items_held(self):
        self.hook.status = 503
        c = self.send({"body": "ping c"})
        self.send({"body": "blocker 2", "urgent": True})
        self.flush(0)
        self.assertEqual(len(self.hook.calls), 1)
        m = self.msgs("id = ?", c["id"])[0]
        self.assertEqual((m["state"], m["held"], m["attempts"], m["lease_until"]), ("queued", 1, 0, None))
        self.assertIsNone(self.agent()["last_digest_at"])

    def test_urgent_wake_does_not_make_the_next_digest_early(self):
        self.send({"body": "blocker", "urgent": True})
        self.flush(0)
        self.send({"body": "later"}, now=self.t0 + 1 * MIN)
        self.flush(30 * MIN)
        self.assertEqual(self.hook.kinds(), ["bus_message"])
        self.flush(61 * MIN)
        self.assertEqual(self.hook.kinds(), ["bus_message", "bus_digest"])


class AnswerWakeTest(WakeBase):
    def test_answer_to_a_waiting_question_wakes_once_without_urgent(self):
        q = self.send({"body": "which db?", "thread": "q1", "reply_expected": True}, frm="clozy", to="polisher")
        self.assertEqual(q["status"], 201)
        r = self.send({"body": "postgres", "thread": "q1"})
        self.assertEqual(r["wake"], "now")
        self.flush(0)
        self.assertEqual(self.hook.kinds(), ["bus_message"])
        self.assertEqual(self.msgs("id = ?", r["id"])[0]["urgent"], 0)
        # already answered: more chatter on the thread is a digest again
        self.assertEqual(self.send({"body": "and also sqlite", "thread": "q1"})["wake"], "digest")
        # a different thread is unaffected
        self.assertEqual(self.send({"body": "unrelated", "thread": "other"})["wake"], "digest")

    def test_fyi_on_a_question_thread_joins_the_digest_and_is_not_the_answer(self):
        self.send({"body": "which branch?", "thread": "q3", "reply_expected": True}, frm="clozy", to="polisher")
        fyi = self.send({"body": "FYI: checks running", "thread": "q3", "actionable": False, "urgent": True})
        self.assertEqual(fyi["wake"], "digest")
        m = self.msgs("id = ?", fyi["id"])[0]
        self.assertEqual((m["actionable"], m["urgent"], m["held"], m["reply_expected"]), (0, 0, 1, 0))
        self.flush(0)
        self.assertEqual(self.hook.calls, [])
        self.flush(61 * MIN)
        self.assertEqual(self.hook.kinds(), ["bus_digest"])
        item = self.hook.calls[0]["body"]["messages"][0]
        self.assertEqual((item["id"], item["actionable"]), (fyi["id"], False))
        # the real answer still wakes at once
        self.assertEqual(self.send({"body": "use main", "thread": "q3"}, now=self.t0 + 62 * MIN)["wake"], "now")

    def test_messages_default_to_actionable(self):
        r = self.send({"body": "normal"})
        self.assertEqual(self.msgs("id = ?", r["id"])[0]["actionable"], 1)
        self.assertIs(self.bus.shape_msg(self.msgs("id = ?", r["id"])[0])["actionable"], True)

    def test_actionable_column_migrates_with_default_one(self):
        cols = {c["name"]: c for c in self.api.s.q("PRAGMA table_info(bus_messages)")}
        self.assertEqual(cols["actionable"]["dflt_value"], "1")

    def test_without_a_question_a_reply_is_a_digest(self):
        self.send({"body": "just saying", "thread": "q2"}, frm="clozy", to="polisher")
        self.assertEqual(self.send({"body": "ok", "thread": "q2"})["wake"], "digest")


class ExpiryTest(WakeBase):
    def test_held_messages_older_than_24h_expire_before_delivery(self):
        old = self.send({"body": "ancient ping"}, now=self.t0 - 25 * 3600)
        fresh = self.send({"body": "fresh ping"}, now=self.t0 - 5 * MIN)
        self.send({"body": "blocker", "urgent": True})
        self.flush(0)
        self.assertEqual(len(self.hook.calls), 1)
        self.assertEqual([m["id"] for m in self.hook.calls[0]["body"]["pending"]], [fresh["id"]])
        o = self.msgs("id = ?", old["id"])[0]
        self.assertEqual(o["state"], "failed")
        self.assertIn("expired", o["last_error"])
        # the digest path too
        old2 = self.send({"body": "ancient 2"}, now=self.t0 - 30 * 3600)
        self.flush(2 * 3600)
        self.assertEqual(len(self.hook.calls), 1)
        self.assertEqual(self.msgs("id = ?", old2["id"])[0]["state"], "failed")

    def test_expired_urgent_message_alerts_its_sender_again(self):
        u = self.send({"body": "urgent q", "urgent": True}, frm="clozy", to="chief", now=self.t0 - 25 * 3600)
        self.flush(-24 * 3600)  # the 5 minute stuck alert
        self.flush(0)
        ids = [m["client_msg_id"] for m in self.system_to("clozy")]
        self.assertIn(f"urgent:{u['id']}", ids)
        self.assertIn(f"failed:{u['id']}", ids)
        self.assertEqual(self.msgs("id = ?", u["id"])[0]["state"], "failed")


class FailedDigestTest(WakeBase):
    def test_failed_digest_does_not_move_last_digest_and_settles_failure(self):
        self.hook.status = 503
        m = self.send({"body": "c"})
        t1 = 200 * MIN
        self.flush(t1)
        self.assertEqual(len(self.hook.calls), 1)
        self.assertIsNone(self.agent()["last_digest_at"])
        row = self.msgs("id = ?", m["id"])[0]
        self.assertEqual((row["state"], row["held"], row["attempts"], row["next_attempt_at"], row["last_error"]),
                         ("queued", 1, 1, self.t0 + t1 + 60, "HTTP 503"))
        self.assertIsNone(row["lease_until"])
        att = self.api.s.q("SELECT * FROM bus_attempts WHERE message_id = ?", m["id"])
        self.assertEqual([(a["transport"], a["ok"], a["http_status"]) for a in att], [("webhook", 0, 503)])
        # before the backoff is over nothing is sent
        self.flush(t1 + 30)
        self.assertEqual(len(self.hook.calls), 1)
        # the backoff retry still goes out, and now the digest counts
        self.hook.status = 200
        self.flush(t1 + 2 * MIN)
        self.assertEqual(len(self.hook.calls), 2)
        self.assertEqual(self.hook.kinds(), ["bus_digest", "bus_digest"])
        self.assertEqual(self.agent()["last_digest_at"], self.t0 + t1 + 2 * MIN)
        self.assertEqual(self.msgs("id = ?", m["id"])[0]["state"], "delivered")

    def test_five_failed_digests_fail_the_message(self):
        self.hook.status = 503
        m = self.send({"body": "never arrives"})
        for i in range(5):
            self.flush(2 * 3600 * (i + 1))
        self.assertEqual(len(self.hook.calls), 5)
        row = self.msgs("id = ?", m["id"])[0]
        self.assertEqual((row["state"], row["attempts"]), ("failed", 5))
        self.assertIsNone(self.agent()["last_digest_at"])


class AtomicClaimTest(WakeBase):
    def test_two_digest_rounds_send_one_post(self):
        self.send({"body": "a"})
        self.send({"body": "b"})
        at = self.t0 + 61 * MIN
        first = self.bus.digest_round("clozy", now=at)
        second = self.bus.digest_round("clozy", now=at)
        self.assertTrue(first["ok"])
        self.assertIsNone(second)
        self.assertEqual(self.hook.kinds(), ["bus_digest"])
        self.assertEqual(self.hook.calls[0]["body"]["count"], 2)

    def test_overlapping_threads_send_one_digest(self):
        for i in range(3):
            self.send({"body": f"m{i}"})
        at = self.t0 + 61 * MIN
        barrier = threading.Barrier(3)

        def go():
            barrier.wait()
            self.bus.digest_round("clozy", now=at)
        ts = [threading.Thread(target=go) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.hook.kinds().count("bus_digest"), 1)
        self.assertTrue(all(m["state"] == "delivered" for m in self.msgs()))

    def test_nothing_pending_sends_nothing_and_keeps_last_digest(self):
        self.assertIsNone(self.bus.digest_round("clozy", now=self.t0))
        self.assertEqual(self.hook.calls, [])
        self.assertIsNone(self.agent()["last_digest_at"])


class UnreachableAlertsTest(WakeBase):
    def setUp(self):
        super().setUp()
        self.bus.alert_agent = "ops"
        self.reg("ops", {"kind": "human", "wake": "poll"})
        self.reg("gil", {"kind": "human", "wake": "poll"})
        self.seen("ops", "gil")
        self.api.s.run("UPDATE bots SET notify_url = NULL WHERE id = 'clozy-bot'")  # clozy is unreachable: no valid notify_url

    def test_alert_default_agent_is_chief_and_configurable_by_env(self):
        import os
        from unittest.mock import patch
        import swarm_bus
        with patch.dict(os.environ, {"SWARM_BUS_ALERT_AGENT": "ops-x"}):
            self.assertEqual(swarm_bus.Bus(self.api, {}).alert_agent, "ops-x")
        with patch.dict(os.environ):
            os.environ.pop("SWARM_BUS_ALERT_AGENT", None)
            self.assertEqual(swarm_bus.Bus(self.api, {}).alert_agent, "chief")

    def test_after_15_minutes_once_an_hour_then_gil_after_3_hours(self):
        chief_bot = self.b.bot("chief-bot", role="chief", url="http://127.0.0.1:9/chief")
        self.assertTrue(chief_bot)
        r = self.send({"body": "x", "urgent": True})
        self.assertIn("unreachable", r["warning"])
        self.assertEqual(self.agent()["unreachable_since"], self.t0)
        self.flush(5 * MIN)
        self.assertEqual(len(self.system_to("ops")), 0)
        self.flush(20 * MIN)
        a = self.system_to("ops")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["client_msg_id"], f"unreachable:clozy:{(self.t0 + 20 * MIN) // 3600}")
        self.assertEqual((a[0]["thread"], a[0]["urgent"], a[0]["reply_expected"]), ("alerts", 1, 0))
        self.assertIn("clozy is unreachable", a[0]["body"])
        self.assertEqual(len(self.system_to("chief")), 0)
        self.flush(40 * MIN)
        self.assertEqual(len(self.system_to("ops")), 1, "not again within the hour")
        self.flush(90 * MIN)
        self.assertEqual(len(self.system_to("ops")), 2)
        self.assertEqual(len(self.system_to("gil")), 0)
        self.assertEqual(self.api.s.q("SELECT * FROM notices WHERE event_id LIKE 'alert:bus-escalate:%'"), [])
        self.flush(190 * MIN)
        g = self.system_to("gil")
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["client_msg_id"], f"escalate:clozy:{self.t0}")
        self.assertIn("clozy has been unreachable for 3 hours", g[0]["body"])
        esc = self.api.s.q("SELECT * FROM notices WHERE event_id LIKE 'alert:bus-escalate:%'")
        self.assertEqual(len(esc), 1)
        self.assertIn("clozy has been unreachable for 3 hours", json.loads(esc[0]["payload"])["text"])
        self.assertIsNotNone(self.agent()["escalated_at"])
        self.flush(200 * MIN)
        self.assertEqual(len(self.system_to("gil")), 1, "once per episode")

    def test_no_alert_before_15_minutes_even_with_repeated_sends(self):
        for i in range(3):
            self.send({"body": f"m{i}"}, now=self.t0 + i * MIN)
        self.flush(14 * MIN)
        self.assertEqual(self.system_to("ops"), [])

    def test_recovery_ends_the_episode(self):
        self.send({"body": "x"})
        self.assertIsNotNone(self.agent()["unreachable_since"])
        self.api.s.run("UPDATE bots SET notify_url = ? WHERE id = 'clozy-bot'", HOOK)
        self.flush(20 * MIN)
        self.assertIsNone(self.agent()["unreachable_since"])
        self.assertEqual(self.system_to("ops"), [])

    def test_never_for_human_agents(self):
        self.api.s.run("UPDATE agents SET last_seen_at = NULL WHERE name = 'gil'")  # nobody read the inbox: unreachable
        r = self.send({"body": "hello", "urgent": True}, to="gil")
        self.assertIn("unreachable", r["warning"])
        self.flush(20 * MIN)
        self.flush(4 * 3600)
        self.assertEqual(self.system_to("ops"), [])
        self.assertIsNone(self.agent("gil")["unreachable_since"])
        self.assertIsNone(self.agent("gil")["alerted_at"])

    def test_rejected_alert_enqueue_releases_the_hourly_claim(self):
        self.send({"body": "x", "urgent": True})
        real = self.bus.send

        def refuse(m, now=None):
            if m.get("from") == "agent-control" and m.get("to") == "ops":
                return 429, {"error": "queue is full"}
            return real(m, now=now)
        self.bus.send = refuse
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.flush(20 * MIN)
        self.assertIn("BUS ALERT ENQUEUE REJECTED", err.getvalue())
        self.assertEqual(self.system_to("ops"), [])
        self.assertIsNone(self.agent()["alerted_at"], "the hourly claim was put back")
        self.bus.send = real
        self.flush(21 * MIN)
        self.assertEqual(len(self.system_to("ops")), 1)
        self.assertEqual(self.agent()["alerted_at"], self.t0 + 21 * MIN)

    def test_missing_alert_agent_is_logged_and_falls_back_to_the_chief_bot_notice(self):
        self.bus.alert_agent = "nobody"
        self.b.bot("chief-bot", role="chief", url="http://127.0.0.1:9/chief")
        self.send({"body": "x", "urgent": True})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.flush(20 * MIN)
        self.assertRegex(err.getvalue(), r"BUS ALERT NOT DELIVERED.*nobody")
        rows = self.api.s.q("SELECT * FROM notices WHERE reason = 'alert' AND target = 'chief-bot'")
        self.assertEqual(len(rows), 1)
        body = json.loads(rows[0]["payload"])
        self.assertEqual(body["from"], "agent-bus")
        self.assertRegex(body["text"], r"clozy is unreachable.*alert agent \"nobody\" is not registered")

    def test_missing_alert_agent_without_a_chief_bot_still_logs(self):
        self.bus.alert_agent = "nobody"
        self.send({"body": "x", "urgent": True})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.flush(20 * MIN)
        self.assertIn("BUS ALERT NOT DELIVERED", err.getvalue())
        self.assertEqual(self.api.s.q("SELECT * FROM notices WHERE reason = 'alert'"), [])

    def test_running_orchestrator_escalates_at_once(self):
        self.b.bot("chief-bot", role="chief", url="http://127.0.0.1:9/chief")
        self.b.bot("app-bot", url=HOOK, projects=("app",))
        self.b.req("POST", "/worker/claim", {"worker": "w1"}, tok="worker-tok-for-tests")
        self.b.req("POST", "/orchestrators", {"name": "steward", "worker_id": "w1", "project": "app", "brief": "Keep going."})
        # a run in progress whose heartbeat is long gone
        self.api.s.run("UPDATE orchestrators SET state = 'running', desired = 'running', last_heartbeat_at = 1 WHERE name = 'steward'")
        self.reg("orch", {"kind": "orchestrator", "wake": "tmux", "orchestrator": "steward"})
        r = self.send({"body": "hello"}, to="orch")
        self.assertIn("heartbeat", r["warning"])
        esc = self.api.s.q("SELECT * FROM notices WHERE event_id LIKE 'alert:bus-escalate:orch:%'")
        self.assertEqual(len(esc), 1)
        self.assertIn("orchestrator with a run in progress", json.loads(esc[0]["payload"])["text"])
        g = self.system_to("gil")
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["client_msg_id"], f"escalate:orch:{self.t0}")
        # no second escalation in the same episode
        self.flush(10 * MIN)
        self.assertEqual(len(self.system_to("gil")), 1)


if __name__ == "__main__":
    unittest.main()
