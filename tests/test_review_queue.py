"""The review queue (server/swarm_review.py, docs/review-queue.md): file, list, decide, stale hash refused, delivery and ack,
and the poll window for poll-only agents. Only fake values are used."""
import time
import unittest

from helpers import ADMIN, Backend, sb

HOOK = "http://127.0.0.1:9/clozy"


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.api = self.b.api
        self.posts = []
        self.api.bus.poster = lambda bot, payload: (self.posts.append(payload) or (200, None))
        self.clozy_bot = self.b.bot("clozy-bot", url=HOOK, projects=("clozy",))
        self.other_bot = self.b.bot("other-bot", projects=("other",))
        self.chief_bot = self.b.bot("chief-bot", role="chief")
        self.b.req("PUT", "/bus/agents/clozy", {"kind": "bot", "wake": "webhook", "bot_id": "clozy-bot"}, expect=201)
        self.b.req("PUT", "/bus/agents/cgc", {"kind": "bot", "wake": "poll"}, expect=201)
        self.cgc = self.b.req("GET", "/bus/agents/cgc/token")["token"]

    def file(self, tok, expect=201, **body):
        body.setdefault("project", "clozy")
        body.setdefault("key", "pricing-2026-11")
        body.setdefault("title", "Price the starter plan at $49 or $79?")
        return self.b.req("PUT", "/review/items", body, tok=tok, expect=expect)

    def decide(self, item, expect=200, tok=ADMIN, **body):
        body.setdefault("content_hash", item["content_hash"])
        return self.b.req("POST", f"/review/items/{item['id']}/decision", body, tok=tok, expect=expect)

    # ---------------- file ----------------
    def test_upsert_by_project_bot_key(self):
        opts = [{"label": "$49", "reason": "More signups", "suggested": True}, "$79"]
        out = self.file(self.clozy_bot, options=opts, recommendation="$49", links=["https://example.com/draft"])
        self.assertEqual(out["result"], "created")
        it = out["item"]
        self.assertEqual((it["bot"], it["status"], it["revision"], it["type"]), ("clozy-bot", "open", 1, "decision"))
        self.assertEqual(it["options"][1], {"label": "$79", "reason": "", "suggested": False})
        self.assertEqual(it["ask"], it["title"])
        self.assertEqual(len(it["content_hash"]), 64)
        # Identical re-send: unchanged, same revision.
        again = self.file(self.clozy_bot, expect=200, options=opts, recommendation="$49", links=["https://example.com/draft"])
        self.assertEqual((again["result"], again["item"]["id"], again["item"]["revision"]), ("unchanged", it["id"], 1))
        # A change bumps the revision in place and changes the hash; left-out fields keep their value.
        upd = self.file(self.clozy_bot, expect=200, recommendation="$79")
        self.assertEqual((upd["result"], upd["item"]["id"], upd["item"]["revision"]), ("updated", it["id"], 2))
        self.assertEqual(len(upd["item"]["options"]), 2)
        self.assertNotEqual(upd["item"]["content_hash"], it["content_hash"])
        # Another key or project is another item.
        self.assertEqual(self.file(self.clozy_bot, key="other")["result"], "created")

    def test_filer_identity_and_validation(self):
        self.b.req("PUT", "/review/items", {"project": "clozy", "key": "k", "title": "t", "bot": "other-bot"}, tok=self.clozy_bot, expect=403)
        self.b.req("PUT", "/review/items", {"project": "clozy", "key": "k", "title": "t"}, expect=400)   # admin must name bot
        st, out = self.b.call("PUT", "/review/items", {"project": "", "key": "bad key", "options": "x"}, tok=self.clozy_bot)
        self.assertEqual(st, 400)
        self.assertGreaterEqual(len(out["errors"]), 2)
        self.b.req("PUT", "/review/items", {"project": "clozy", "key": "k", "title": "t", "type": "poem"}, tok=self.clozy_bot, expect=400)
        self.b.req("PUT", "/review/items", {"project": "clozy", "key": "k", "title": "t", "links": ["javascript:alert(1)"]},
                   tok=self.clozy_bot, expect=400)
        # A bus agent files as itself.
        it = self.file(self.cgc, project="cgc")["item"]
        self.assertEqual(it["bot"], "cgc")
        # Bots never decide.
        self.decide(it, tok=self.cgc, expect=403)
        self.decide(it, tok=self.clozy_bot, expect=403)

    # ---------------- list / detail ----------------
    def test_list_and_detail_are_scoped(self):
        mine = self.file(self.clozy_bot)["item"]
        theirs = self.file(self.other_bot, project="other")["item"]
        self.assertEqual([i["id"] for i in self.b.req("GET", "/review/items", tok=self.clozy_bot)["items"]], [mine["id"]])
        # The clozy bus agent (bot_id clozy-bot) sees the items filed with the bot token, and vice versa.
        ctok = self.b.req("GET", "/bus/agents/clozy/token")["token"]
        self.assertEqual([i["id"] for i in self.b.req("GET", "/review/items", tok=ctok)["items"]], [mine["id"]])
        self.b.req("GET", f"/review/items/{theirs['id']}", tok=self.clozy_bot, expect=404)
        self.assertEqual(self.b.req("GET", f"/review/items/{mine['id']}", tok=self.clozy_bot)["item"]["content_hash"], mine["content_hash"])
        allr = self.b.req("GET", "/review/items")
        self.assertEqual(allr["total"], 2)
        self.assertEqual(self.b.req("GET", "/review/items?bot=other-bot", tok=self.chief_bot)["total"], 1)
        self.assertEqual(self.b.req("GET", "/review/items?status=all&project=other")["items"][0]["id"], theirs["id"])
        self.b.req("GET", "/review/items?status=nope", expect=400)

    # ---------------- decide ----------------
    def test_stale_hash_is_refused_and_records_nothing(self):
        it = self.file(self.clozy_bot, options=["$49", "$79"])["item"]
        self.file(self.clozy_bot, expect=200, options=["$49", "$99"])            # the bot changes the options after Gil opened it
        st, out = self.b.call("POST", f"/review/items/{it['id']}/decision", {"action": "approve", "content_hash": it["content_hash"],
                                                                             "option": "$49"})
        self.assertEqual((st, out["code"]), (409, "stale"))
        self.assertNotEqual(out["item"]["content_hash"], it["content_hash"])   # the refreshed item comes back
        st, out = self.b.call("POST", f"/review/items/{it['id']}/decision", {"action": "approve"})
        self.assertEqual((st, out["code"]), (409, "stale"))                    # no hash at all
        cur = self.b.req("GET", f"/review/items/{it['id']}")["item"]
        self.assertEqual((cur["status"], cur["decision"]), ("open", None))
        self.assertEqual(self.api.s.one("SELECT COUNT(*) AS n FROM bus_messages")["n"], 0)
        # A wrong revision with the right hash is also stale.
        self.decide(cur, action="approve", revision=1, expect=409)
        # The current hash works.
        self.assertEqual(self.decide(cur, action="approve", option="$99")["item"]["status"], "approved")
        # A second click on a decided item records nothing new.
        st, out = self.b.call("POST", f"/review/items/{it['id']}/decision", {"action": "reject", "content_hash": cur["content_hash"]})
        self.assertEqual((st, out["code"]), (409, "not_open"))

    def test_hash_is_canonical(self):
        h = sb.swarm_review.content_hash(" Ask ", [{"label": "A", "reason": "r"}], " rec ")
        self.assertEqual(h, sb.swarm_review.content_hash("Ask", [{"label": "A", "reason": "r", "suggested": True}], "rec"))
        self.assertNotEqual(h, sb.swarm_review.content_hash("Ask", [{"label": "A", "reason": "other"}], "rec"))
        self.assertNotEqual(h, sb.swarm_review.content_hash("Ask", [{"label": "A", "reason": "r"}], None))

    def test_decide_validation(self):
        it = self.file(self.clozy_bot, options=["$49", "$79"])["item"]
        self.decide(it, action="maybe", expect=400)
        self.decide(it, action="approve", option="$12", expect=400)
        self.b.req("POST", "/review/items/rnope/decision", {"action": "approve", "content_hash": "x"}, expect=404)

    # ---------------- delivery and ack ----------------
    def test_decision_goes_to_the_bus_agent_and_ack(self):
        it = self.file(self.clozy_bot, options=["$49", "$79"])["item"]
        out = self.decide(it, tok=self.chief_bot, action="approve", option="$49", note="Go.", edited_text="Starter: $49")["item"]
        self.assertEqual(out["decision"]["action"], "approve")
        self.assertEqual(out["decision"]["by"], "chief:chief-bot")
        self.assertEqual(out["delivery"]["state"], "bus")
        m = self.api.s.one("SELECT * FROM bus_messages WHERE id = ?", out["delivery"]["bus_message_id"])
        self.assertEqual((m["recipient"], m["sender"], m["thread"], m["urgent"], m["wake"]),
                         ("clozy", "agent-control", f"review:{it['id']}", 1, "now"))
        for s in ("[review-decision]", "decision=approve", "Chosen option: $49", "Final text:\nStarter: $49", "/ack"):
            self.assertIn(s, m["body"])
        # Unacked until the bot acks; the ack also marks the bus message read.
        self.assertEqual(self.b.req("GET", "/review/items?status=answered&acked=0", tok=self.clozy_bot)["total"], 1)
        self.assertEqual(self.b.req("POST", f"/review/items/{it['id']}/ack", {"revision": 99}, tok=self.clozy_bot)["stale"], True)
        a = self.b.req("POST", f"/review/items/{it['id']}/ack", {"revision": 1}, tok=self.clozy_bot)
        self.assertEqual((a["first"], a["decided_revision"]), (True, 1))
        self.assertFalse(self.b.req("POST", f"/review/items/{it['id']}/ack", {}, tok=self.clozy_bot)["first"])
        self.assertEqual(self.b.req("GET", "/review/items?status=answered&acked=0", tok=self.clozy_bot)["total"], 0)
        self.assertEqual(self.api.s.one("SELECT state FROM bus_messages WHERE id = ?", m["id"])["state"], "read")
        self.b.req("POST", f"/review/items/{it['id']}/ack", {}, tok=self.other_bot, expect=404)

    def test_poll_agent_reads_decision_from_inbox(self):
        it = self.file(self.cgc, project="cgc", title="Hook A or B?")["item"]
        self.decide(it, action="reject", note="Neither.")
        inbox = self.b.req("GET", "/bus/inbox", tok=self.cgc)["messages"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["meta"]["kind"], "review_decision")
        self.assertEqual(inbox[0]["meta"]["decision"], "reject")

    def test_bot_without_agent_reads_answered_unacked_list(self):
        it = self.file(self.other_bot, project="other")["item"]
        out = self.decide(it, action="approve")["item"]
        self.assertEqual(out["delivery"]["state"], "no_agent")
        lst = self.b.req("GET", "/review/items?status=answered&acked=0", tok=self.other_bot)["items"]
        self.assertEqual([i["id"] for i in lst], [it["id"]])
        self.b.req("POST", f"/review/items/{it['id']}/ack", {}, tok=self.other_bot)
        self.assertEqual(self.b.req("GET", "/review/items?status=answered&acked=0", tok=self.other_bot)["items"], [])

    def test_refused_message_falls_back_to_pointer(self):
        it = self.file(self.clozy_bot)["item"]
        out = self.decide(it, action="approve", note="key sk-ant-" + "x" * 30)["item"]   # fake secret-shaped note
        self.assertEqual(out["delivery"]["state"], "bus")
        m = self.api.s.one("SELECT body FROM bus_messages WHERE id = ?", out["delivery"]["bus_message_id"])
        self.assertIn("read the full record", m["body"])
        self.assertNotIn("sk-ant-", m["body"])

    def test_changes_then_revise_reopens_same_item(self):
        it = self.file(self.clozy_bot)["item"]
        self.decide(it, action="changes", note="Add the annual price.")
        cur = self.b.req("GET", f"/review/items/{it['id']}", tok=self.clozy_bot)["item"]
        self.assertEqual((cur["status"], cur["decision"]["action"]), ("changes_requested", "request_changes"))
        self.decide(cur, action="approve", expect=409)   # not open until the bot revises
        re = self.file(self.clozy_bot, expect=200, ask="Starter at $49/mo or $490/yr?")
        self.assertEqual((re["result"], re["item"]["id"], re["item"]["status"], re["item"]["revision"]), ("reopened", it["id"], "open", 2))
        out = self.decide(re["item"], action="approve")["item"]
        self.assertEqual((out["decision"]["revision"], out["delivery"]["acked_at"]), (2, None))
        self.assertEqual(self.api.s.one("SELECT COUNT(*) AS n FROM bus_messages WHERE thread = ?", f"review:{it['id']}")["n"], 2)

    def test_after_approve_or_reject_a_new_item_is_created(self):
        it = self.file(self.clozy_bot)["item"]
        self.decide(it, action="reject")
        out = self.file(self.clozy_bot)
        self.assertEqual(out["result"], "created")
        self.assertNotEqual(out["item"]["id"], it["id"])
        self.assertEqual(out["item"]["previous_item_id"], it["id"])
        self.assertIn("rejected", out["warning"])

    def test_close(self):
        it = self.file(self.clozy_bot)["item"]
        self.b.req("POST", f"/review/items/{it['id']}/close", {"reason": "resolved elsewhere"}, tok=self.other_bot, expect=404)
        out = self.b.req("POST", f"/review/items/{it['id']}/close", {"reason": "resolved elsewhere"}, tok=self.clozy_bot)["item"]
        self.assertEqual((out["status"], out["close_reason"]), ("closed", "resolved elsewhere"))
        self.decide(out, action="approve", expect=409)
        self.assertEqual(self.file(self.clozy_bot)["result"], "created")


class PollWindowCase(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.bus = self.b.api.bus
        self.b.bot("clozy-bot", url=HOOK)

    def test_poll_window(self):
        a = self.b.req("PUT", "/bus/agents/cgc", {"kind": "bot", "wake": "poll", "poll_window_minutes": 1500}, expect=201)
        self.assertEqual(a["poll_window_minutes"], 1500)
        tok = self.b.req("GET", "/bus/agents/cgc/token")["token"]
        self.b.req("GET", "/bus/inbox", tok=tok)        # checks in
        t = int(time.time())
        self.b.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = 'cgc'", t - 20 * 3600)
        row = self.b.api.s.one("SELECT * FROM agents WHERE name = 'cgc'")
        self.assertTrue(self.bus.reachability(row)["reachable"])
        self.b.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = 'cgc'", t - 26 * 3600)
        r = self.bus.reachability(self.b.api.s.one("SELECT * FROM agents WHERE name = 'cgc'"))
        self.assertEqual((r["reachable"], r["reason"]), (False, "has not checked in for 1500 minutes"))
        # Left out keeps it; null clears it (back to 10 minutes).
        self.assertEqual(self.b.req("PUT", "/bus/agents/cgc", {"description": "daily"})["poll_window_minutes"], 1500)
        self.assertIsNone(self.b.req("PUT", "/bus/agents/cgc", {"poll_window_minutes": None})["poll_window_minutes"])
        self.b.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = 'cgc'", t - 20 * 60)
        self.assertFalse(self.bus.reachability(self.b.api.s.one("SELECT * FROM agents WHERE name = 'cgc'"))["reachable"])

    def test_poll_window_validation(self):
        for v in (5, 10081, "60", True):
            self.b.req("PUT", "/bus/agents/cgc", {"kind": "bot", "wake": "poll", "poll_window_minutes": v}, expect=400)
        self.b.req("PUT", "/bus/agents/clozy", {"kind": "bot", "wake": "webhook", "bot_id": "clozy-bot", "poll_window_minutes": 60}, expect=400)
        # Switching a poll agent to webhook drops its window.
        self.b.req("PUT", "/bus/agents/c2", {"kind": "bot", "wake": "poll", "poll_window_minutes": 60}, expect=201)
        self.assertIsNone(self.b.req("PUT", "/bus/agents/c2", {"wake": "webhook", "bot_id": "clozy-bot"})["poll_window_minutes"])


if __name__ == "__main__":
    unittest.main()
