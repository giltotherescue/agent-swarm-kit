"""Task-scoped tokens and claim fencing (must-fix 1, Astra 11, 12, 14, CoS 1)."""
import time
import unittest

from helpers import ADMIN, WORKER, Backend


class TaskTokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.web = cls.b.bot("web", projects=["site"])
        cls.b.bot("shop", projects=["store"])

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def setUp(self):
        # Every test starts with an empty queue.
        self.b.api.s.run("UPDATE tasks SET state = 'cancelled' WHERE state NOT IN ('done','failed','cancelled')")
        self.b.api.s.run("UPDATE claims SET ended_at = 1 WHERE ended_at IS NULL")

    def queue(self, prompt, tok=ADMIN, **kw):
        return self.b.req("POST", "/tasks", dict(kw, prompt=prompt, allow_duplicate=True), tok=tok)["task"]["id"]

    def two_running(self):
        a = self.queue("task A", owner="web")
        b = self.queue("task B", owner="shop")
        ca, cb = self.b.claim(), self.b.claim()
        self.assertEqual((ca["id"], cb["id"]), (a, b))
        return ca, cb

    def test_claim_returns_token_stored_only_as_hash(self):
        tid = self.queue("hash check")
        c = self.b.claim()
        self.assertEqual(c["id"], tid)
        self.assertTrue(c["task_token"].startswith("swt_"))
        row = self.b.api.s.one("SELECT * FROM claims WHERE claim_id = ?", c["claim_id"])
        self.assertNotIn(c["task_token"], str(row))
        self.assertEqual(len(row["token_hash"]), 64)

    def test_task_token_cannot_read_list_or_finish_another_task(self):
        ca, cb = self.two_running()
        ta = ca["task_token"]
        self.assertEqual(self.b.call("GET", f"/tasks/{cb['id']}", tok=ta)[0], 403)
        self.assertEqual(self.b.call("GET", f"/tasks/{cb['id']}/attachments", tok=ta)[0], 403)
        self.assertEqual(self.b.call("GET", f"/tasks/{cb['id']}/report", tok=ta)[0], 403)
        self.assertEqual(self.b.call("GET", "/tasks", tok=ta)[0], 403)
        self.assertEqual(self.b.call("GET", "/status", tok=ta)[0], 403)
        st, _ = self.b.call("POST", f"/tasks/{cb['id']}/finish", {"status": "done", "claim_id": cb["claim_id"]}, tok=ta)
        self.assertEqual(st, 403)
        st, _ = self.b.call("POST", f"/tasks/{cb['id']}/heartbeat", {"claim_id": cb["claim_id"]}, tok=ta)
        self.assertEqual(st, 403)
        st, _ = self.b.call("PUT", f"/tasks/{cb['id']}/files/report/REPORT.md", raw=b"x", tok=ta, headers={"X-Claim-Id": cb["claim_id"]})
        self.assertEqual(st, 403)
        # Its own task works.
        self.assertEqual(self.b.call("GET", f"/tasks/{ca['id']}", tok=ta)[0], 200)
        self.assertEqual(self.b.call("POST", f"/tasks/{ca['id']}/heartbeat", {"claim_id": ca["claim_id"]}, tok=ta)[0], 200)

    def test_task_token_cannot_claim_or_read_bots_items_projects(self):
        ca, _ = self.two_running()
        ta = ca["task_token"]
        for method, path, body in (("POST", "/worker/claim", {"worker": "x"}), ("GET", "/bots", None), ("GET", "/bots/web", None),
                                   ("PUT", "/bots/web", {"notify_url": ""}), ("GET", "/waiting", None),
                                   ("POST", "/waiting", {"project": "p"}), ("GET", "/projects", None),
                                   ("PUT", "/projects/site", {}), ("POST", "/tasks", {"prompt": "x"}), ("GET", "/notices", None),
                                   ("POST", "/alerts", {"key": "k", "text": "t"}), ("GET", "/worker/task-states?ids=" + ca["id"], None)):
            self.assertEqual(self.b.call(method, path, body, tok=ta)[0], 403, path)

    def assert_dead(self, c):
        tok, tid = c["task_token"], c["id"]
        self.assertEqual(self.b.call("GET", f"/tasks/{tid}", tok=tok)[0], 409)
        st, out = self.b.call("POST", f"/tasks/{tid}/heartbeat", {"claim_id": c["claim_id"]}, tok=tok)
        self.assertEqual(st, 409)
        self.assertTrue(out["claim_ended"])
        st, _ = self.b.call("PUT", f"/tasks/{tid}/files/artifact/x.txt", raw=b"x", tok=tok, headers={"X-Claim-Id": c["claim_id"]})
        self.assertEqual(st, 409)
        self.assertEqual(self.b.call("GET", f"/tasks/{tid}/attachments", tok=tok)[0], 409)

    def test_token_dies_after_finish_done(self):
        self.queue("finish me")
        c = self.b.claim()
        self.assertEqual(self.b.finish(c, "done")[0], 200)
        self.assert_dead(c)

    def test_token_dies_after_cancel(self):
        self.queue("cancel me", owner="web")
        c = self.b.claim()
        out = self.b.req("POST", f"/tasks/{c['id']}/cancel", {}, tok=self.web)
        self.assertEqual(out["task"]["state"], "cancelled")
        self.assert_dead(c)
        # A finish after the cancel cannot bring it back or publish anything.
        st, out = self.b.finish(c, "done")
        self.assertEqual(st, 409)
        self.assertEqual(self.b.req("GET", f"/tasks/{c['id']}")["task"]["state"], "cancelled")

    def test_token_dies_after_wait(self):
        self.queue("ask me")
        c = self.b.claim()
        self.assertEqual(self.b.finish(c, "waiting", question="Which one?")[0], 200)
        self.assert_dead(c)

    def test_token_dies_after_reap(self):
        tid = self.queue("lose me")
        c = self.b.claim()
        self.b.api.s.run("UPDATE tasks SET lease_until = 1 WHERE id = ?", tid)
        self.b.api.reap()
        self.assertEqual(self.b.req("GET", f"/tasks/{tid}")["task"]["state"], "queued")
        self.assert_dead(c)

    def test_token_dies_when_lease_runs_out_even_before_reap(self):
        tid = self.queue("slow me")
        c = self.b.claim()
        self.b.api.s.run("UPDATE tasks SET lease_until = ? WHERE id = ?", int(time.time()) - 5, tid)
        self.assert_dead(c)

    def test_token_dies_after_max_age(self):
        self.queue("old token")
        c = self.b.claim()
        self.b.api.s.run("UPDATE claims SET expires_at = 1 WHERE claim_id = ?", c["claim_id"])
        self.assert_dead(c)

    def test_worker_token_cannot_read_reports_files_or_tasks(self):
        tid = self.queue("has a report", attachments=[{"name": "a.txt", "content_base64": "aGk="}])
        c = self.b.claim()
        self.b.req("PUT", f"/tasks/{tid}/files/report/REPORT.md", raw=b"secret report", tok=c["task_token"],
                   headers={"X-Claim-Id": c["claim_id"]})
        self.b.finish(c, "done")
        for path in (f"/tasks/{tid}/report", f"/tasks/{tid}", "/tasks", f"/tasks/{tid}/artifacts", f"/tasks/{tid}/attachments",
                     f"/tasks/{tid}/attachments/a.txt", "/bots", "/bots/web", "/waiting", "/projects", "/status", "/notices"):
            self.assertEqual(self.b.call("GET", path, tok=WORKER)[0], 403, path)
        for method, path in (("POST", f"/tasks/{tid}/finish"), ("POST", f"/tasks/{tid}/heartbeat"),
                             ("PUT", f"/tasks/{tid}/files/report/REPORT.md"), ("POST", f"/tasks/{tid}/answer"),
                             ("POST", f"/tasks/{tid}/cancel"), ("POST", "/tasks")):
            self.assertEqual(self.b.call(method, path, {}, tok=WORKER)[0], 403, path)
        # What it may still do: task states only (no content).
        states = self.b.req("GET", f"/worker/task-states?ids={tid}", tok=WORKER)["states"]
        self.assertEqual(states[tid]["state"], "done")
        self.assertNotIn("prompt", states[tid])

    def test_stale_claim_cannot_finish_or_heartbeat(self):
        tid = self.queue("claimed twice")
        old = self.b.claim("w-old")
        self.b.api.s.run("UPDATE tasks SET lease_until = 1 WHERE id = ?", tid)
        self.b.api.reap()
        self.b.api.s.run("UPDATE tasks SET not_before = 0 WHERE id = ?", tid)
        new = self.b.claim("w-new")
        self.assertEqual(new["id"], tid)
        self.assertNotEqual(old["claim_id"], new["claim_id"])
        st, out = self.b.finish(old, "done", summary="stale")
        self.assertEqual(st, 409)
        self.assertTrue(out["claim_ended"])
        self.assertEqual(self.b.call("POST", f"/tasks/{tid}/heartbeat", {"claim_id": old["claim_id"]}, tok=old["task_token"])[0], 409)
        # A good token with the wrong claim id is refused too.
        self.assertEqual(self.b.call("POST", f"/tasks/{tid}/heartbeat", {"claim_id": old["claim_id"]}, tok=new["task_token"])[0], 409)
        self.assertEqual(self.b.finish(new, "done", summary="fresh")[0], 200)
        self.assertEqual(self.b.req("GET", f"/tasks/{tid}")["task"]["summary"], "fresh")

    def test_finish_retry_replays_and_never_reverts_an_answer(self):
        tid = self.queue("question then retry", owner="web")
        c = self.b.claim()
        st, first = self.b.finish(c, "waiting", question="Blue or green?")
        self.assertEqual(st, 200)
        self.b.req("POST", f"/tasks/{tid}/answer", {"answer": "Blue"}, tok=self.web)
        # The worker never saw the first response and sends the same finish again.
        st, again = self.b.finish(c, "waiting", question="Blue or green?")
        self.assertEqual(st, 200)
        self.assertTrue(again["replayed"])
        t = self.b.req("GET", f"/tasks/{tid}")["task"]
        self.assertEqual((t["state"], t["pending_answer"]), ("queued", "Blue"))

    def test_publish_check_follows_current_project_rules(self):
        self.b.req("PUT", "/projects/site", {"allow_push": True})
        tid = self.queue("publish me", tok=self.web, project="site", push=True)
        c = self.b.claim()
        chk = self.b.req("POST", f"/tasks/{tid}/publish-check", {"claim_id": c["claim_id"]}, tok=c["task_token"])
        self.assertTrue(chk["allowed"])
        self.b.req("PUT", "/projects/site", {"allow_push": False})
        chk = self.b.req("POST", f"/tasks/{tid}/publish-check", {"claim_id": c["claim_id"]}, tok=c["task_token"])
        self.assertFalse(chk["allowed"])
        self.b.req("PUT", "/projects/site", {"allow_push": True})

    def test_answer_after_lost_work_starts_fresh(self):
        tid = self.queue("lost work", owner="web")
        c = self.b.claim()
        self.b.finish(c, "waiting", question="Unpushed commits are gone. Continue fresh?", outcome="work_unavailable")
        self.b.api.s.run("UPDATE tasks SET unpushed = 1, outcome = 'work_unavailable' WHERE id = ?", tid)
        self.b.req("POST", f"/tasks/{tid}/answer", {"answer": "continue fresh"}, tok=self.web)
        again = self.b.claim()
        self.assertEqual(again["id"], tid)
        # Without this the runner would ask the same question forever.
        self.assertEqual((again["unpushed"], again["start_fresh"]), (0, 1))

    def test_messages_need_acknowledgement(self):
        tid = self.queue("listen", owner="web")
        c = self.b.claim()
        self.b.req("POST", f"/tasks/{tid}/message", {"text": "also the footer"}, tok=self.web)
        hb = self.b.req("POST", f"/tasks/{tid}/heartbeat", {"claim_id": c["claim_id"]}, tok=c["task_token"])
        ids = [m["id"] for m in hb["messages"]]
        self.assertEqual(len(ids), 1)
        # Response lost: the next heartbeat (no ack) gets it again.
        hb = self.b.req("POST", f"/tasks/{tid}/heartbeat", {"claim_id": c["claim_id"]}, tok=c["task_token"])
        self.assertEqual([m["id"] for m in hb["messages"]], ids)
        hb = self.b.req("POST", f"/tasks/{tid}/heartbeat", {"claim_id": c["claim_id"], "ack": ids}, tok=c["task_token"])
        hb = self.b.req("POST", f"/tasks/{tid}/heartbeat", {"claim_id": c["claim_id"]}, tok=c["task_token"])
        self.assertEqual(hb["messages"], [])


if __name__ == "__main__":
    unittest.main()
