"""Primary/fallback claim routing (port of agent-control PR 68)."""
import os
import time
import unittest

from helpers import ADMIN, WORKER, Backend


class PrimaryWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        for name in ("SWARM_PRIMARY_WORKER", "SWARM_PRIMARY_STALE_SECONDS", "SWARM_FALLBACK_AFTER_SECONDS"):
            os.environ.pop(name, None)

    def setUp(self):
        self.b.api.s.run("UPDATE tasks SET state = 'cancelled' WHERE state NOT IN ('done','failed','cancelled')")
        self.b.api.s.run("DELETE FROM workers")
        os.environ["SWARM_PRIMARY_WORKER"] = "main"
        os.environ.pop("SWARM_PRIMARY_STALE_SECONDS", None)
        os.environ.pop("SWARM_FALLBACK_AFTER_SECONDS", None)

    def queue(self, prompt, **kw):
        return self.b.req("POST", "/tasks", dict(kw, prompt=prompt, allow_duplicate=True), tok=ADMIN)["task"]["id"]

    def claim(self, worker, projects=None):
        body = {"worker": worker}
        if projects is not None:
            body["projects"] = projects
        return self.b.req("POST", "/worker/claim", body, tok=WORKER)["task"]

    def seen(self, worker, at, projects=None, draining=0):
        self.b.api.s.run("INSERT OR REPLACE INTO workers(name, last_seen, draining, projects) VALUES(?, ?, ?, ?)",
                         worker, at, draining, projects)

    def age(self, tid, seconds):
        at = int(time.time()) - seconds
        self.b.api.s.run("UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?", at, at, tid)

    def test_primary_claims_fresh_task_and_records_last_seen(self):
        tid = self.queue("fresh")
        self.assertEqual(self.claim("main")["id"], tid)
        row = self.b.api.s.one("SELECT last_seen FROM workers WHERE name = 'main'")
        self.assertLess(time.time() - row["last_seen"], 5)

    def test_fallback_waits_while_primary_is_fresh_then_claims_after_fallback_after(self):
        self.seen("main", int(time.time()))
        tid = self.queue("fresh")
        self.assertIsNone(self.claim("spare"))
        self.age(tid, 121)
        self.assertEqual(self.claim("spare")["id"], tid)

    def test_requeued_task_counts_from_updated_at_not_created_at(self):
        self.seen("main", int(time.time()))
        tid = self.queue("requeued")
        self.b.api.s.run("UPDATE tasks SET created_at = 1, attempts = 1 WHERE id = ?", tid)
        self.assertIsNone(self.claim("spare"))
        self.b.api.s.run("UPDATE tasks SET updated_at = ? WHERE id = ?", int(time.time()) - 121, tid)
        self.assertEqual(self.claim("spare")["id"], tid)

    def test_fallback_after_is_read_from_env(self):
        os.environ["SWARM_FALLBACK_AFTER_SECONDS"] = "10"
        self.seen("main", int(time.time()))
        tid = self.queue("short wait")
        self.age(tid, 11)
        self.assertEqual(self.claim("spare")["id"], tid)

    def test_fallback_claims_at_once_when_primary_stale_never_seen_or_draining(self):
        never = self.queue("never")
        self.assertEqual(self.claim("spare")["id"], never, "primary never seen")
        self.seen("main", int(time.time()) - 91)
        stale = self.queue("stale")
        self.assertEqual(self.claim("spare")["id"], stale, "primary older than SWARM_PRIMARY_STALE_SECONDS")
        self.seen("main", int(time.time()), draining=1)
        drain = self.queue("draining")
        self.assertEqual(self.claim("spare")["id"], drain, "a draining primary takes no work")
        self.seen("main", int(time.time()) - 30)
        self.queue("fresh primary")
        self.assertIsNone(self.claim("spare"))
        os.environ["SWARM_PRIMARY_STALE_SECONDS"] = "20"
        self.assertIsNotNone(self.claim("spare"), "SWARM_PRIMARY_STALE_SECONDS is read from env")

    def test_fallback_claims_at_once_when_project_is_off_primary_allowlist(self):
        self.seen("main", int(time.time()), projects='["site"]')
        allowed = self.queue("on main's list", project="site")
        elsewhere = self.queue("not on main's list", project="store")
        self.assertEqual(self.claim("spare")["id"], elsewhere)
        self.assertIsNone(self.claim("spare"))
        self.assertEqual(self.claim("main", ["site"])["id"], allowed)

    def test_pins_override_routing_both_ways(self):
        self.seen("main", int(time.time()))
        spare_pin = self.queue("pinned to spare", target_worker="spare")
        self.assertIsNone(self.claim("main"), "the primary never takes another worker's pin")
        self.assertEqual(self.claim("spare")["id"], spare_pin)
        main_pin = self.queue("pinned to main", target_worker="main")
        self.age(main_pin, 10_000)
        self.seen("main", 1)
        self.assertIsNone(self.claim("spare"), "a fallback never takes the primary's pin, even when the primary is stale")
        self.assertEqual(self.claim("main")["id"], main_pin)

    def test_unset_or_empty_primary_keeps_first_come(self):
        for value in (None, ""):
            if value is None:
                os.environ.pop("SWARM_PRIMARY_WORKER", None)
            else:
                os.environ["SWARM_PRIMARY_WORKER"] = value
            self.seen("main", int(time.time()))
            tid = self.queue("first come")
            self.assertEqual(self.claim("spare")["id"], tid)

    def test_task_whose_workspace_is_on_the_fallback_stays_there(self):
        self.seen("main", int(time.time()))
        self.seen("spare", int(time.time()))
        tid = self.queue("resume")
        self.b.api.s.run("UPDATE tasks SET workspace_host = 'spare', attempts = 1 WHERE id = ?", tid)
        self.assertIsNone(self.claim("main"), "the saved workspace stays on spare")
        self.assertEqual(self.claim("spare")["id"], tid)

    def test_heartbeat_refreshes_last_seen(self):
        tid = self.queue("run")
        c = self.claim("spare")
        self.assertEqual(c["id"], tid)
        self.b.api.s.run("UPDATE workers SET last_seen = 1 WHERE name = 'spare'")
        self.b.req("POST", f"/tasks/{tid}/heartbeat", {"claim_id": c["claim_id"]}, tok=c["task_token"])
        self.assertGreater(self.b.api.s.one("SELECT last_seen FROM workers WHERE name = 'spare'")["last_seen"], 1)


if __name__ == "__main__":
    unittest.main()
