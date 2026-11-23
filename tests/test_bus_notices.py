"""Finish notices over the bus (agents.notices = 'bus'); mirrors agent-control tests/bus_notices.test.mjs and the PR 60 default-copy rule.
The kit's "default copy" is the Chief of Staff bot's copy (waiting on a human, ownerless, owner not registered)."""
import json
import time
import unittest
from unittest.mock import patch

from helpers import Backend

FAKE_SECRET = "ghp_" + "A1b2C3d4" * 5   # matches the GitHub token pattern; not a real token
CLOZY_HOOK = "http://127.0.0.1:9/clozy"
OTHER_HOOK = "http://127.0.0.1:9/other"
CHIEF_HOOK = "http://127.0.0.1:9/chief"
PR = "https://github.com/acme/site/pull/1"
SHA = "0123456789abcdef0123456789abcdef01234567"


class Hook:
    def __init__(self):
        self.calls = []
        self.status = 200

    def __call__(self, url, data, headers):
        self.calls.append({"url": url, "body": json.loads(data), "headers": headers})
        return self.status, b"{}"


class NoticeBase(unittest.TestCase):
    notices = "bus"

    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.api = self.b.api
        self.hook = Hook()
        self.api.bus.poster = self.hook
        self.b.bot("clozy-bot", url=CLOZY_HOOK)
        self.b.bot("other-bot", url=OTHER_HOOK)
        self.reg("clozy", {"kind": "bot", "wake": "webhook", "bot_id": "clozy-bot", "notices": self.notices, "aliases": ["clozy-old"]})
        self.reg("other", {"kind": "bot", "wake": "webhook", "bot_id": "other-bot"})
        self.n = 0

    def reg(self, name, body):
        st, out = self.b.call("PUT", f"/bus/agents/{name}", body)
        self.assertLess(st, 300, (name, st, out))

    def add_chief(self):
        self.b.bot("chief-bot", role="chief", url=CHIEF_HOOK)

    def finished(self, owner, tid=None, status="done", report=None, create=None, **extra):
        """Queue a code task for `owner`, claim it, optionally save a report, finish it. Returns the task row."""
        self.n += 1
        tid = tid or f"t{self.n}"
        body = {"id": tid, "prompt": "Ship the thing\nmore", "repo": "acme/site", "push": True, "open_pr": True}
        body.update(create or {})
        if owner is not None:
            body["owner"] = owner
        self.b.req("POST", "/tasks", body)
        if owner is None:  # enqueue now needs an owner; an ownerless row is a legacy one
            self.b.api.s.run("UPDATE tasks SET owner = NULL WHERE id = ?", tid)
        c = self.b.claim()
        self.assertEqual(c["id"], tid)
        if report is not None:
            self.b.req("PUT", f"/tasks/{tid}/files/report/REPORT.md", None, tok=c["task_token"], raw=report.encode(),
                       headers={"X-Claim-Id": c["claim_id"]})
        if status == "done":
            extra = dict({"pr_url": PR, "pushed_branch": f"agent/{tid}", "head_sha": SHA, "unpushed_commits": 0}, **extra)
        st, out = self.b.finish(c, status, **extra)
        self.assertEqual(st, 200, out)
        return self.row(tid)

    def row(self, tid):
        return self.api.s.one("SELECT * FROM tasks WHERE id = ?", tid)

    def bus_msgs(self, where="sender = 'agent-control'", *args):
        return self.api.s.q(f"SELECT * FROM bus_messages WHERE {where} ORDER BY id", *args)

    def notice_rows(self):
        return self.api.s.q("SELECT * FROM notices ORDER BY id")

    def token(self, name):
        return self.b.req("GET", f"/bus/agents/{name}/token")["token"]


class BusNoticeTest(NoticeBase):
    def test_bus_agent_gets_the_finish_notice_on_the_bus_only(self):
        self.add_chief()
        t = self.finished("clozy-bot", "tb1")
        self.assertEqual(t["state"], "done")
        self.assertEqual(self.notice_rows(), [], "no bot webhook notice row, no Chief copy")
        m = self.bus_msgs()
        self.assertEqual(len(m), 1)
        m = m[0]
        self.assertEqual((m["recipient"], m["sender"], m["thread"]), ("clozy", "agent-control", "task:tb1"))
        self.assertEqual((m["urgent"], m["reply_expected"], m["held"], m["wake"]), (1, 0, 0, "now"))
        meta = json.loads(m["meta"])
        self.assertEqual(m["client_msg_id"], meta["idempotency_key"])
        self.assertTrue(m["client_msg_id"].startswith("tb1:done:"), m["client_msg_id"])
        self.assertEqual(meta["notify_target"], "bus")
        self.assertEqual((meta["event"], meta["task_id"], meta["pr_url"], meta["head_sha"], meta["landed"]), ("task", "tb1", PR, SHA, True))
        lines = m["body"].split("\n")
        self.assertEqual(lines[0], "[swarm] Task tb1 done/done: Ship the thing")
        self.assertIn(f"PR: {PR}", m["body"])
        self.assertIn(f"Head: {SHA}", m["body"])
        self.assertIn("Landed on GitHub: yes", m["body"])
        self.assertIn("Report: GET /tasks/tb1/report (your bus token can read it)", m["body"])

    def test_the_delivery_is_a_bus_message_not_a_task_notice(self):
        self.finished("clozy-bot", "tb2")
        self.api.bus.flush()
        self.assertEqual([(c["url"], c["body"]["kind"]) for c in self.hook.calls], [(CLOZY_HOOK, "bus_message")])
        p = self.hook.calls[0]["body"]
        self.assertEqual((p["from"], p["to"], p["thread"], p["urgent"], p["reply_expected"]), ("agent-control", "clozy", "task:tb2", True, False))
        self.assertEqual(p["meta"]["notify_target"], "bus")
        self.assertEqual(self.bus_msgs()[0]["state"], "delivered")
        self.assertEqual(self.notice_rows(), [])

    def test_waiting_on_a_human_gets_no_chief_copy_when_the_bus_took_it(self):
        self.add_chief()
        t = self.finished("clozy-bot", "tw1", status="waiting", question="Blue or green?", waiting_on="human")
        self.assertEqual((t["state"], t["waiting_on"]), ("waiting", "human"))
        self.assertEqual(self.notice_rows(), [])
        m = self.bus_msgs()
        self.assertEqual(len(m), 1)
        self.assertIn("Question: Blue or green?", m[0]["body"])
        self.assertEqual(json.loads(m[0]["meta"])["waiting_on"], "human")

    def test_owner_resolves_by_name_alias_and_bot_id(self):
        for owner in ("clozy", "clozy-old", "clozy-bot", "CLOZY-Bot"):
            self.finished(owner)
        m = self.bus_msgs()
        self.assertEqual([x["recipient"] for x in m], ["clozy"] * 4)
        self.assertEqual(self.notice_rows(), [])

    def test_healthcheck_alias_still_gets_notices_but_plain_healthcheck_is_quiet(self):
        self.reg("clozy", {"aliases": ["clozy-old", "healthcheck"]})
        self.finished("healthcheck", "th1")
        self.assertEqual([m["recipient"] for m in self.bus_msgs()], ["clozy"])
        self.reg("clozy", {"aliases": ["clozy-old"]})
        before = len(self.bus_msgs())
        self.finished("healthcheck", "th2")
        self.assertEqual(len(self.bus_msgs()), before)

    def test_duplicate_notice_does_not_create_a_second_message(self):
        t = self.finished("clozy-bot", "td1")
        self.api.notify_task(t)
        self.api.notify_task(self.row("td1"))
        self.assertEqual(len(self.bus_msgs()), 1)

    def test_a_task_of_another_bus_agent_goes_to_that_agent(self):
        self.reg("other", {"notices": "bus"})
        self.finished("other-bot", "to1")
        self.assertEqual([m["recipient"] for m in self.bus_msgs()], ["other"])


class InformationalNoticeTest(NoticeBase):
    def cancel(self, tid):
        self.b.req("POST", "/tasks", {"id": tid, "prompt": "x", "owner": "clozy-bot"})
        self.api.set_state(self.row(tid), "cancelled", "test")

    def test_cancellation_is_informational_and_never_falls_back(self):
        self.add_chief()
        self.cancel("tc1")
        self.cancel("tc1b")
        m = self.bus_msgs()
        self.assertEqual(len(m), 2)
        self.assertTrue(all((x["urgent"], x["actionable"], x["held"], x["wake"]) == (0, 0, 1, "digest") for x in m))
        self.api.bus.flush()
        self.assertEqual(self.hook.calls, [])
        self.assertEqual(self.notice_rows(), [], "no legacy or Chief fallback for a cancellation")

    def test_cancellation_rides_the_next_done_wake_and_done_stays_deduplicated(self):
        self.cancel("tc2")
        self.finished("clozy-bot", "td2")
        self.assertEqual([c["body"]["kind"] for c in self.hook.calls], [])
        self.api.bus.flush()
        self.assertEqual([c["body"]["kind"] for c in self.hook.calls], ["bus_message"])
        self.assertEqual([x["meta"]["task_id"] for x in self.hook.calls[0]["body"]["pending"]], ["tc2"])
        done = self.bus_msgs("actionable = 1")
        self.assertEqual(len(done), 1)
        self.assertEqual((done[0]["urgent"], done[0]["held"]), (1, 0))
        self.api.notify_task(self.row("td2"))
        self.assertEqual(len(self.bus_msgs("actionable = 1")), 1)


class LegacyOwnerTest(NoticeBase):
    notices = "legacy"

    def test_legacy_agent_keeps_the_old_path(self):
        self.finished("clozy-bot", "tl1")
        self.assertEqual(self.bus_msgs(), [])
        rows = self.notice_rows()
        self.assertEqual([(r["target"], r["reason"]) for r in rows], [("clozy-bot", "owner")])


class NoAgentOwnerTest(NoticeBase):
    def test_owner_without_a_bus_agent_keeps_the_old_path(self):
        self.add_chief()
        self.b.bot("ghost-bot", url="http://127.0.0.1:9/ghost")
        self.finished("ghost-bot", "tg1")
        self.assertEqual(self.bus_msgs(), [])
        self.assertEqual([(r["target"], r["reason"]) for r in self.notice_rows()], [("ghost-bot", "owner")])

    def test_unregistered_owner_and_ownerless_tasks_go_to_chief(self):
        self.add_chief()
        self.finished("nobody-bot", "tg2")
        self.finished(None, "tg3")
        self.assertEqual(self.bus_msgs(), [])
        self.assertEqual(sorted((r["target"], r["reason"]) for r in self.notice_rows()),
                         [("chief-bot", "no_owner"), ("chief-bot", "owner_not_registered")])

    def test_retired_agent_no_longer_takes_notices(self):
        self.b.req("DELETE", "/bus/agents/clozy")
        self.finished("clozy-bot", "tr1")
        self.assertEqual(self.bus_msgs(), [])
        self.assertEqual([r["target"] for r in self.notice_rows()], ["clozy-bot"])


class BusFailureFallbackTest(NoticeBase):
    def test_failed_bus_send_runs_the_normal_path_including_the_chief_copy(self):
        self.add_chief()
        with patch.object(self.api.bus, "send_notice", return_value=False) as sn:
            self.finished("clozy-bot", "tf1", status="waiting", question="Which one?", waiting_on="human")
        self.assertEqual(sn.call_count, 1)
        self.assertEqual(self.bus_msgs(), [])
        self.assertEqual(sorted((r["target"], r["reason"]) for r in self.notice_rows()),
                         [("chief-bot", "waiting_on_human"), ("clozy-bot", "owner")])

    def test_failed_bus_send_for_a_done_task_reaches_the_owner_bot(self):
        with patch.object(self.api.bus, "send_notice", return_value=False):
            self.finished("clozy-bot", "tf2")
        self.assertEqual([(r["target"], r["reason"]) for r in self.notice_rows()], [("clozy-bot", "owner")])

    def test_missing_bus_table_falls_back_without_raising(self):
        self.add_chief()
        self.api.s.run("DROP TABLE bus_attempts")
        self.api.s.run("DROP TABLE bus_messages")
        self.finished("clozy-bot", "tf3", status="waiting", question="Which one?", waiting_on="human")
        self.assertEqual(sorted((r["target"], r["reason"]) for r in self.notice_rows()),
                         [("chief-bot", "waiting_on_human"), ("clozy-bot", "owner")])

    def test_no_bus_flag_skips_the_bus(self):
        t = self.finished("clozy-bot", "tf4")
        self.assertEqual(len(self.bus_msgs()), 1)
        self.api.notify_task(t, no_bus=True)
        self.assertEqual([(r["target"], r["reason"]) for r in self.notice_rows()], [("clozy-bot", "owner")])
        self.assertEqual(len(self.bus_msgs()), 1)

    def test_failed_notice_message_falls_back_once_and_alerts_the_alert_agent(self):
        self.add_chief()
        self.reg("chief", {"kind": "bot", "wake": "poll"})
        self.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = 'chief'", int(time.time()))
        self.hook.status = 503
        self.finished("clozy-bot", "tn1", status="waiting", question="Which one?", waiting_on="human")
        self.assertEqual(self.notice_rows(), [])
        calls = []
        real = self.api.notify_task

        def spy(task, no_bus=False):
            calls.append((task["id"], no_bus))
            return real(task, no_bus=no_bus)
        self.api.notify_task = spy
        later = int(time.time()) + 3 * 3600
        self.api.bus.flush(now=later)  # the post fails and the message is older than 2 hours: failed
        self.assertEqual(self.bus_msgs()[0]["state"], "failed")
        self.assertEqual(calls, [("tn1", True)])
        # the message was 3 hours old, so the escalation to the Chief bot ('alert') rides along with the two legacy notices
        self.assertEqual(sorted((r["target"], r["reason"]) for r in self.notice_rows()),
                         [("chief-bot", "alert"), ("chief-bot", "waiting_on_human"), ("clozy-bot", "owner")])
        a = self.bus_msgs("sender = 'agent-control' AND recipient = 'chief'")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["thread"], "alerts")
        self.assertIn("clozy is unreachable", a[0]["body"])
        # a second flush does not send it again
        self.api.bus.flush(now=later + 60)
        self.assertEqual(calls, [("tn1", True)])
        self.assertEqual(len(self.bus_msgs("sender = 'agent-control' AND recipient = 'chief'")), 1)
        self.assertEqual(len(self.notice_rows()), 3)


class ReportExcerptTest(NoticeBase):
    def test_report_excerpt_is_included(self):
        self.finished("clozy-bot", "ts1", report="# Summary\nExcerpt line")
        b = self.bus_msgs()[0]["body"]
        self.assertIn("--- REPORT.md (excerpt) ---", b)
        self.assertIn("Excerpt line", b)

    def test_report_excerpt_is_dropped_when_it_looks_secret(self):
        self.finished("clozy-bot", "ts2", report="leaked " + FAKE_SECRET)
        b = self.bus_msgs()[0]["body"]
        self.assertNotIn("leaked", b)
        self.assertNotIn(FAKE_SECRET, b)
        self.assertNotIn("excerpt", b)
        self.assertIn("Report: GET /tasks/ts2/report", b)

    def test_no_report_means_no_excerpt(self):
        self.finished("clozy-bot", "ts3")
        self.assertNotIn("excerpt", self.bus_msgs()[0]["body"])

    def test_excerpt_is_cut_to_1500_characters(self):
        self.finished("clozy-bot", "ts4", report="A" * 5000)
        b = self.bus_msgs()[0]["body"]
        self.assertEqual(b.count("A"), 1500)


class AgentTokenTaskAccessTest(NoticeBase):
    def setUp(self):
        super().setUp()
        self.finished("clozy-bot", "tr1", report="# Report\nAll good")
        self.finished("other-bot", "tr2", report="# Other\nPrivate")
        self.tok = self.token("clozy")

    def test_reads_its_own_bots_task_and_report(self):
        st, out = self.b.call("GET", "/tasks/tr1", tok=self.tok)
        self.assertEqual(st, 200)
        self.assertEqual(out["task"]["id"], "tr1")
        st, out = self.b.call("GET", "/tasks/tr1/report", tok=self.tok)
        self.assertEqual(st, 200)
        self.assertIn(b"All good", out)

    def test_reads_tasks_it_owns_by_alias_and_by_name(self):
        self.finished("clozy-old", "tr3")
        self.finished("clozy", "tr4")
        for tid in ("tr3", "tr4"):
            self.assertEqual(self.b.call("GET", f"/tasks/{tid}", tok=self.tok)[0], 200, tid)

    def test_another_bots_task_is_404(self):
        self.assertEqual(self.b.call("GET", "/tasks/tr2", tok=self.tok)[0], 404)
        self.assertEqual(self.b.call("GET", "/tasks/tr2/report", tok=self.tok)[0], 404)
        self.assertEqual(self.b.call("GET", "/tasks/missing", tok=self.tok)[0], 404)
        # and the other agent sees only its own
        other = self.token("other")
        self.assertEqual(self.b.call("GET", "/tasks/tr2/report", tok=other)[0], 200)
        self.assertEqual(self.b.call("GET", "/tasks/tr1/report", tok=other)[0], 404)

    def test_cannot_write_anything_on_tasks(self):
        for method, path, body in (("POST", "/tasks", {"prompt": "x"}), ("POST", "/tasks/tr1/answer", {"answer": "x"}),
                                   ("POST", "/tasks/tr1/message", {"text": "x"}), ("POST", "/tasks/tr1/cancel", {}),
                                   ("POST", "/tasks/tr1/finish", {"status": "done"}), ("POST", "/tasks/tr1/heartbeat", {}),
                                   ("PUT", "/tasks/tr1/files/report/REPORT.md", {}), ("GET", "/tasks", None),
                                   ("GET", "/tasks/tr1/artifacts", None)):
            with self.subTest(path=path):
                self.assertEqual(self.b.call(method, path, body, tok=self.tok)[0], 403)
        self.assertEqual(self.row("tr1")["state"], "done")

    def test_bad_agent_token_is_401(self):
        self.assertEqual(self.b.call("GET", "/tasks/tr1/report", tok="ab_clozy.1.bad")[0], 401)
        self.assertEqual(self.b.call("GET", "/tasks/tr1/report", tok=None)[0], 401)

    def test_rotated_token_loses_access(self):
        self.b.req("POST", "/bus/agents/clozy/token/rotate", {})
        self.assertEqual(self.b.call("GET", "/tasks/tr1/report", tok=self.tok)[0], 401)


if __name__ == "__main__":
    unittest.main()
