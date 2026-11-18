"""Long-running jobs (0.3.0): per-task timeout with an admin maximum, the long-running kind, and chains."""
import os
import unittest

from helpers import ADMIN, Backend


class LongRunningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["SWARM_TASK_TIMEOUT_MAX"] = str(48 * 3600)
        cls.b = Backend()
        cls.bot = cls.b.bot("app-bot", projects=["app"])
        cls.other = cls.b.bot("other-bot", projects=["other"])

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SWARM_TASK_TIMEOUT_MAX", None)
        cls.b.close()

    def claim(self, task_id):
        c = self.b.claim()
        while c and c["id"] != task_id:
            self.b.finish(c, "done")
            c = self.b.claim()
        self.assertIsNotNone(c, f"{task_id} was not claimable")
        return c

    def entries(self, project):
        return self.b.req("GET", f"/log?project={project}")["entries"]

    def test_timeout_override_is_bounded_by_the_admin_maximum(self):
        st, err = self.b.call("POST", "/tasks", {"prompt": "too long", "timeout_minutes": 49 * 60}, tok=self.bot)
        self.assertEqual(st, 400, err)
        self.assertIn("SWARM_TASK_TIMEOUT_MAX", err["error"])
        t = self.b.req("POST", "/tasks", {"prompt": "eight hours", "timeout_minutes": 480}, tok=self.bot)["task"]
        self.assertEqual(t["timeout"], 480 * 60)
        # The task token outlives the run's time limit.
        c = self.claim(t["id"])
        claim = self.b.api.s.one("SELECT created_at, expires_at FROM claims WHERE claim_id = ?", c["claim_id"])
        self.assertGreaterEqual(claim["expires_at"] - claim["created_at"], 480 * 60 + 3600)
        self.assertEqual(c["timeout"], 480 * 60)
        self.b.finish(c, "done")

    def test_long_running_kind_defaults_to_the_maximum(self):
        t = self.b.req("POST", "/tasks", {"prompt": "walk the app for two days", "kind": "long-running"}, tok=self.bot)["task"]
        self.assertEqual(t["timeout"], 48 * 3600)
        st, _ = self.b.call("POST", "/tasks", {"prompt": "both", "kind": "long-running", "chain": True}, tok=self.bot)
        self.assertEqual(st, 400)
        self.assertEqual(self.b.call("POST", "/tasks", {"prompt": "cycles without chain", "max_cycles": 3}, tok=self.bot)[0], 400)
        self.b.req("POST", f"/tasks/{t['id']}/cancel", tok=self.bot)

    def test_chain_queues_each_cycle_with_its_checkpoint_and_stops_at_the_limit(self):
        root = self.b.req("POST", "/tasks", {"prompt": "Polish the app: walk it, fix one bug, retest", "project": "app",
                                             "chain": True, "max_cycles": 3, "timeout_minutes": 60, "title": "Polisher"},
                          tok=self.bot)["task"]
        self.assertEqual((root["chain_root"], root["cycle"], root["max_cycles"]), (root["id"], 1, 3))
        tid = root["id"]
        for n in (1, 2, 3):
            c = self.claim(tid)
            self.assertEqual(c["cycle"], n)
            if n > 1:
                self.assertEqual(c["attachments"], ["checkpoint.md"])
                cp = self.b.call("GET", f"/tasks/{tid}/attachments/checkpoint.md")[1]
                self.assertEqual(cp, f"after cycle {n - 1}".encode())
            self.b.finish(c, "done", summary=f"Fixed bug {n}", checkpoint=f"after cycle {n}")
            nxt = self.b.req("GET", "/tasks?project=app&state=queued")["tasks"]
            nxt = [t for t in nxt if t.get("chain_root") == root["id"]]
            if n < 3:
                self.assertEqual(len(nxt), 1)
                self.assertEqual((nxt[0]["parent_id"], nxt[0]["follow_up"], nxt[0]["timeout"]), (tid, 1, 3600))
                tid = nxt[0]["id"]
            else:
                self.assertEqual(nxt, [])
        logs = [e for e in self.entries("app") if e["kind"] == "result" and "Polisher" in e["summary"]]
        self.assertEqual([e["summary"] for e in logs], [f"Cycle {n} done: Polisher" for n in (1, 2, 3)])
        self.assertIn("Next: cycle 2 queued", logs[0]["detail"])
        self.assertIn("reached its limit of 3 cycles", logs[2]["detail"])
        # The checkpoint is kept on each cycle for a restart.
        self.assertIn("checkpoint.md", self.b.req("GET", f"/tasks/{tid}")["task"]["artifacts"])

    def test_no_checkpoint_ends_the_chain_and_a_failure_stops_it(self):
        a = self.b.req("POST", "/tasks", {"prompt": "Chain that finishes", "project": "app", "chain": True}, tok=self.bot)["task"]
        self.b.finish(self.claim(a["id"]), "done", summary="All bugs fixed")
        self.assertIn("left no checkpoint", [e for e in self.entries("app") if e["task_id"] == a["id"]][-1]["detail"])
        f = self.b.req("POST", "/tasks", {"prompt": "Chain that fails", "project": "app", "chain": True}, tok=self.bot)["task"]
        self.b.finish(self.claim(f["id"]), "failed", error="tests would not start", checkpoint="ignored")
        self.assertIn("Chain stopped: cycle 1 failed", [e for e in self.entries("app") if e["task_id"] == f["id"]][-1]["detail"])
        queued = self.b.req("GET", "/tasks?state=queued")["tasks"]
        self.assertFalse([t for t in queued if t.get("chain_root") in (a["id"], f["id"])])

    def test_cancel_on_any_cycle_stops_the_chain(self):
        root = self.b.req("POST", "/tasks", {"prompt": "Chain to cancel", "project": "app", "chain": True}, tok=self.bot)["task"]
        self.b.finish(self.claim(root["id"]), "done", checkpoint="cycle 1 notes")
        nxt = [t for t in self.b.req("GET", "/tasks?state=queued")["tasks"] if t.get("chain_root") == root["id"]]
        self.assertEqual(len(nxt), 1)
        # Cancelling the finished first cycle cancels the queued next one.
        r = self.b.req("POST", f"/tasks/{root['id']}/cancel", tok=self.bot)
        self.assertEqual(r["chain_cancelled"], [nxt[0]["id"]])
        self.assertEqual(self.b.req("GET", f"/tasks/{nxt[0]['id']}")["task"]["state"], "cancelled")
        self.assertTrue([e for e in self.entries("app") if e["summary"].startswith(f"Chain {root['id']} cancelled")])
        # A running cycle that is cancelled cannot queue another one: its finish is refused.
        r2 = self.b.req("POST", "/tasks", {"prompt": "Chain cancelled mid-run", "project": "app", "chain": True}, tok=self.bot)["task"]
        c = self.claim(r2["id"])
        self.b.req("POST", f"/tasks/{r2['id']}/cancel", tok=self.bot)
        st, _ = self.b.finish(c, "done", checkpoint="too late")
        self.assertEqual(st, 409)
        self.assertFalse([t for t in self.b.req("GET", "/tasks?state=queued")["tasks"] if t.get("chain_root") == r2["id"]])

    def test_a_request_cannot_join_another_chain(self):
        victim = self.b.req("POST", "/tasks", {"prompt": "Other bot's chain", "project": "other", "chain": True}, tok=self.other)["task"]
        mine = self.b.req("POST", "/tasks", {"prompt": "Sneaky", "project": "app", "chain": True, "_chain_root": victim["id"],
                                             "_cycle": 7}, tok=self.bot)["task"]
        self.assertEqual((mine["chain_root"], mine["cycle"]), (mine["id"], 1))
        self.b.req("POST", f"/tasks/{mine['id']}/cancel", tok=self.bot)
        self.assertEqual(self.b.req("GET", f"/tasks/{victim['id']}")["task"]["state"], "queued")
        self.b.req("POST", f"/tasks/{victim['id']}/cancel", tok=ADMIN)


if __name__ == "__main__":
    unittest.main()
