"""Enqueue requires an owner (port of agent-control PR 72): HTTP, follow-ups, chains, and the swarmctl CLI."""
import os
import runpy
import subprocess
import sys
import unittest
from types import SimpleNamespace

from helpers import Backend

CTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "swarmctl")


class EnqueueOwnerHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.alice = cls.b.bot("alice", projects=["site"], repo="acme/site")

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def test_missing_or_empty_owner_is_rejected_with_hint(self):
        for body in ({"prompt": "no owner one"}, {"prompt": "no owner two", "owner": ""}, {"prompt": "no owner three", "owner": "  "}):
            st, err = self.b.call("POST", "/tasks", body)
            self.assertEqual(st, 400)
            self.assertIn("--owner <bot id or alias>", err["error"])

    def test_explicit_owner_and_bot_token_work(self):
        self.b.req("POST", "/tasks", {"prompt": "admin with owner", "owner": "alice"})
        t = self.b.req("POST", "/tasks", {"prompt": "bot own task"}, tok=self.alice)["task"]
        self.assertEqual(t["owner"], "alice")

    def test_follow_up_and_chain_inherit_owner(self):
        t = self.b.req("POST", "/tasks", {"prompt": "parent task", "owner": "alice"})["task"]
        self.b.api.s.run("UPDATE tasks SET state = 'done' WHERE id = ?", t["id"])
        f = self.b.req("POST", "/tasks", {"prompt": "continue it", "follow_up": t["id"]})["task"]
        self.assertEqual(f["owner"], "alice")

    def test_follow_up_of_ownerless_parent_needs_owner_but_internal_works(self):
        t = self.b.req("POST", "/tasks", {"prompt": "legacy parent", "owner": "alice"})["task"]
        self.b.api.s.run("UPDATE tasks SET state = 'done', owner = NULL WHERE id = ?", t["id"])
        st, err = self.b.call("POST", "/tasks", {"prompt": "public follow", "follow_up": t["id"]})
        self.assertEqual(st, 400)
        self.assertIn("--owner", err["error"])
        who = {"role": "admin", "bot": None}
        r = self.b.api.follow_up(who, t["id"], {"prompt": "chain cycle"})  # server-created: not public
        self.assertIsNone(r["task"]["owner"])


class EnqueueOwnerCliTest(unittest.TestCase):
    def run_ctl(self, *args, env=None):
        e = {k: v for k, v in os.environ.items() if k != "ENQUEUE_OWNER"}
        e.update(env or {}, SWARM_URL="http://127.0.0.1:1")
        return subprocess.run([sys.executable, CTL, *args], input="brief", capture_output=True, text=True, env=e, timeout=30)

    def test_fails_fast_without_owner(self):
        r = self.run_ctl("enqueue", "-")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--owner", r.stderr)
        self.assertIn("ENQUEUE_OWNER", r.stderr)

    def test_env_supplies_owner_and_explicit_wins(self):
        need = runpy.run_path(CTL)["need_owner"]
        os.environ["ENQUEUE_OWNER"] = "env-bot"
        try:
            a = SimpleNamespace(owner=None, follow_up=None)
            need(a)
            self.assertEqual(a.owner, "env-bot")
            a = SimpleNamespace(owner="cli-bot", follow_up=None)
            need(a)
            self.assertEqual(a.owner, "cli-bot")
        finally:
            del os.environ["ENQUEUE_OWNER"]


if __name__ == "__main__":
    unittest.main()
