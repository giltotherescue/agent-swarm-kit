"""Done means landed (mirrors agent-control tests/done_landed.test.mjs): a code task whose commits are only on the worker, or whose
requested PR was never confirmed, ends waiting/blocked instead of done. The notice payload carries head_sha and landed."""
import json
import unittest

from helpers import Backend

SHA = "0123456789abcdef0123456789abcdef01234567"
PR = "https://github.com/acme/site/pull/9"


class LandedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.api = cls.b.api
        cls.b.bot("clozy-bot", url="http://127.0.0.1:9/clozy")
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def run_task(self, create=None, status="done", **finish):
        """Queue a repo task owned by clozy-bot, claim and finish it. Returns the stored task row."""
        type(self).n += 1
        tid = f"d{self.n}"
        body = {"id": tid, "prompt": f"Fix the problem {tid}", "repo": "acme/site", "owner": "clozy-bot"}
        body.update(create or {})
        self.b.req("POST", "/tasks", body)
        c = self.b.claim()
        self.assertEqual(c["id"], tid)
        st, out = self.b.finish(c, status, worker="gil-worker-1", **finish)
        self.assertEqual(st, 200, out)
        return self.row(tid)

    def row(self, tid):
        return self.api.s.one("SELECT * FROM tasks WHERE id = ?", tid)

    def payload(self, t):
        return self.api.payload(t, f"{t['id']}:{t['state']}:1", "owner")

    def test_pushed_branch_of_an_earlier_claim_is_not_evidence_for_a_new_unpushed_head(self):
        self.b.req("POST", "/tasks", {"id": "twoclaim", "prompt": "Fix the thing", "repo": "acme/site", "owner": "clozy-bot"})
        c1 = self.b.claim()
        st, _ = self.b.finish(c1, "next", worker="w1", pushed_branch="agent/a", head_sha="a" * 40, unpushed_commits=0)
        self.assertEqual(st, 200)
        c2 = self.b.claim()
        self.assertEqual(c2["id"], "twoclaim")
        st, _ = self.b.finish(c2, "done", worker="w1", head_sha="b" * 40, unpushed_commits=2, summary="more work")
        self.assertEqual(st, 200)
        t = self.row("twoclaim")
        self.assertEqual((t["state"], t["outcome"]), ("waiting", "blocked"))
        self.assertIn("NOT on GitHub: 2 commit(s)", t["question"])
        self.assertIs(self.payload(t)["landed"], False)

    def test_current_claim_push_still_lands_after_an_earlier_claim(self):
        self.b.req("POST", "/tasks", {"id": "twoclaim2", "prompt": "Fix the thing", "repo": "acme/site", "owner": "clozy-bot"})
        c1 = self.b.claim()
        self.b.finish(c1, "next", worker="w1", pushed_branch="agent/a", unpushed_commits=0)
        c2 = self.b.claim()
        self.b.finish(c2, "done", worker="w1", pushed_branch="agent/a", head_sha="b" * 40, unpushed_commits=2)
        t = self.row("twoclaim2")
        self.assertEqual(t["state"], "done")
        self.assertIs(self.payload(t)["landed"], True)

    # ---- unpushed commits ----
    def test_code_task_with_unpushed_commits_and_no_pushed_branch_is_blocked_not_done(self):
        t = self.run_task(unpushed_commits=2, summary="Fixed it")
        self.assertEqual((t["state"], t["outcome"], t["waiting_on"]), ("waiting", "blocked", "owner"))
        self.assertRegex(t["question"], r"committed on gil-worker-1 but NOT on GitHub: 2 commit\(s\) not pushed\. Re-queue with --push or --pr")
        self.assertIn("NOT on GitHub: 2 commit(s) not pushed", t["question"])
        self.assertIn("Agent's report: Fixed it", t["question"])
        self.assertEqual(t["unpushed_commits"], 2)
        p = self.payload(t)
        self.assertIs(p["landed"], False)
        self.assertEqual(p["state"], "waiting")
        self.assertIn("NOT on GitHub", p["question"])

    def test_blocked_task_notifies_the_owner_as_waiting_with_the_question(self):
        self.api.s.run("DELETE FROM notices")
        t = self.run_task(unpushed_commits=1)
        rows = self.api.s.q("SELECT * FROM notices WHERE event_id LIKE ?", t["id"] + ":%")
        self.assertEqual(len(rows), 1)
        body = json.loads(rows[0]["payload"])
        self.assertEqual((body["state"], body["outcome"], body["landed"]), ("waiting", "blocked", False))
        self.assertIn("NOT on GitHub: 1 commit(s)", body["question"])

    def test_blocked_task_with_a_pushed_branch_is_not_blocked_for_commits(self):
        t = self.run_task(create={"push": True}, unpushed_commits=2, pushed_branch="agent/x", head_sha=SHA)
        self.assertEqual(t["state"], "done")
        self.assertIs(self.payload(t)["landed"], True)

    # ---- pushed ----
    def test_pushed_code_task_with_a_pr_is_done_and_the_payload_carries_head_sha_and_landed(self):
        t = self.run_task(create={"open_pr": True}, unpushed_commits=0, head_sha=SHA, pushed_branch="agent/b", pr_url=PR)
        self.assertEqual((t["state"], t["outcome"]), ("done", "done"))
        p = self.payload(t)
        self.assertEqual((p["head_sha"], p["landed"], p["pr_url"]), (SHA, True, PR))

    def test_pushed_without_asking_for_a_pr_is_done_and_landed(self):
        t = self.run_task(create={"push": True}, unpushed_commits=0, head_sha=SHA, pushed_branch="agent/c")
        self.assertEqual(t["state"], "done")
        self.assertIs(self.payload(t)["landed"], True)

    def test_open_pr_with_a_pushed_branch_but_no_pr_url_is_blocked(self):
        t = self.run_task(create={"open_pr": True}, unpushed_commits=0, pushed_branch="agent/c", head_sha=SHA)
        self.assertEqual((t["state"], t["outcome"], t["waiting_on"]), ("waiting", "blocked", "owner"))
        self.assertIn("Pushed agent/c but no PR was confirmed on GitHub.", t["question"])
        self.assertIs(self.payload(t)["landed"], False)

    def test_unpushed_commits_and_no_branch_lead_over_the_missing_pr(self):
        t = self.run_task(create={"open_pr": True}, unpushed_commits=3)
        self.assertEqual(t["state"], "waiting")
        self.assertTrue(t["question"].startswith("Work is committed on gil-worker-1 but NOT on GitHub: 3 commit(s)"))

    # ---- unknown / not applicable ----
    def test_old_worker_without_unpushed_commits_still_finishes_done_and_landed_is_unknown(self):
        t = self.run_task(commits=["abc fix"])
        self.assertEqual(t["state"], "done")
        self.assertIsNone(t["unpushed_commits"])
        p = self.payload(t)
        self.assertIsNone(p["landed"])
        self.assertIsNone(p["head_sha"])

    def test_old_worker_that_pushed_and_opened_a_pr_is_still_landed(self):
        t = self.run_task(create={"open_pr": True}, pushed_branch="agent/o", pr_url=PR)
        self.assertEqual(t["state"], "done")
        self.assertIs(self.payload(t)["landed"], True)

    def test_zero_commits_and_nothing_pushed_is_done_and_unknown(self):
        t = self.run_task(unpushed_commits=0, head_sha=SHA)
        self.assertEqual(t["state"], "done")
        p = self.payload(t)
        self.assertIsNone(p["landed"])
        self.assertEqual(p["head_sha"], SHA)

    def test_ops_style_and_review_and_script_kinds_are_exempt(self):
        for kind in ("ops", "review", "script", "research"):
            with self.subTest(kind=kind):
                t = self.run_task(create={"kind": kind}, unpushed_commits=3)
                self.assertEqual(t["state"], "done", kind)
                self.assertIsNone(self.payload(t)["landed"])

    def test_tasks_without_a_repo_are_exempt(self):
        t = self.run_task(create={"repo": None}, unpushed_commits=3)
        self.assertEqual((t["kind"], t["state"]), ("research", "done"))

    def test_report_only_and_untrusted_tasks_are_never_blocked_for_unpushed_commits(self):
        for name, create in (("report_only", {"report_only": True}), ("untrusted", {"untrusted_input": True}),
                             ("report_only code", {"report_only": True, "kind": "code"}),
                             ("untrusted code", {"untrusted_input": True, "kind": "code"})):
            with self.subTest(name):
                t = self.run_task(create=create, unpushed_commits=2)
                self.assertEqual(t["state"], "done", name)
                self.assertIsNone(self.payload(t)["landed"], name)

    def test_untrusted_code_task_that_claims_a_branch_is_not_blocked_for_a_missing_pr(self):
        t = self.run_task(create={"untrusted_input": True, "kind": "code"}, unpushed_commits=0, pushed_branch="agent/u")
        self.assertEqual(t["state"], "done")

    # ---- the status of the finish body ----
    def test_waiting_and_failed_statuses_are_not_changed_by_the_rule(self):
        w = self.run_task(status="waiting", question="Which color?", unpushed_commits=2)
        self.assertEqual((w["state"], w["outcome"], w["question"]), ("waiting", "waiting", "Which color?"))
        f = self.run_task(status="failed", error="boom", unpushed_commits=2)
        self.assertEqual((f["state"], f["error"]), ("failed", "boom"))
        self.assertIs(self.payload(f)["landed"], False)

    def test_bad_unpushed_commits_values_are_ignored(self):
        for bad in (-1, True, "3", 1.5, None):
            with self.subTest(bad=bad):
                t = self.run_task(unpushed_commits=bad)
                self.assertEqual(t["state"], "done")
                self.assertIsNone(t["unpushed_commits"])

    # ---- head_sha ----
    def test_stored_head_sha_shows_in_the_task(self):
        t = self.run_task(create={"push": True}, unpushed_commits=0, head_sha=SHA, pushed_branch="agent/h")
        got = self.b.req("GET", f"/tasks/{t['id']}")["task"]
        self.assertEqual((got["head_sha"], got["unpushed_commits"]), (SHA, 0))

    def test_head_sha_is_kept_when_a_later_finish_omits_it(self):
        t = self.run_task(status="waiting", question="q?", head_sha=SHA, unpushed_commits=0)
        self.assertEqual(self.row(t["id"])["head_sha"], SHA)
        self.assertEqual(self.b.req("GET", f"/tasks/{t['id']}")["task"]["head_sha"], SHA)

    def test_blocked_task_keeps_its_head_sha_for_the_owner(self):
        t = self.run_task(unpushed_commits=1, head_sha=SHA)
        self.assertEqual((t["state"], t["head_sha"]), ("waiting", SHA))
        self.assertEqual(self.payload(t)["head_sha"], SHA)


class ChainLandedTest(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.b.bot("clozy-bot", url="http://127.0.0.1:9/clozy", projects=("site",), repo="acme/site")

    def test_a_quiet_chain_cycle_is_not_blocked_but_the_last_one_is(self):
        root = self.b.req("POST", "/tasks", {"id": "ch1", "prompt": "Polish", "repo": "acme/site", "owner": "clozy-bot",
                                             "project": "site", "chain": True, "max_cycles": 2})["task"]
        c1 = self.b.claim()
        self.assertEqual(c1["id"], root["id"])
        self.b.finish(c1, "done", unpushed_commits=3, checkpoint="after cycle 1")
        t1 = self.b.api.s.one("SELECT * FROM tasks WHERE id = 'ch1'")
        self.assertEqual((t1["state"], t1["outcome"]), ("done", "done"), "a cycle that hands on a checkpoint is not news")
        self.assertEqual(self.b.api.s.q("SELECT * FROM notices"), [])
        nxt = self.b.api.s.one("SELECT * FROM tasks WHERE chain_root = 'ch1' AND cycle = 2")
        self.assertIsNotNone(nxt)
        c2 = self.b.claim()
        self.assertEqual(c2["id"], nxt["id"])
        self.b.finish(c2, "done", unpushed_commits=3, checkpoint="after cycle 2")
        t2 = self.b.api.s.one("SELECT * FROM tasks WHERE id = ?", nxt["id"])
        self.assertEqual((t2["state"], t2["outcome"]), ("waiting", "blocked"), "the chain's end is real news and is not landed")
        self.assertIn("NOT on GitHub: 3 commit(s) not pushed", t2["question"])


if __name__ == "__main__":
    unittest.main()
