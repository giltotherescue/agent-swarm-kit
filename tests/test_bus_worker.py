"""Finish body fields for the agent bus: unpushed_commits and head_sha, from the git summary of agent-exec collect."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import swarm_worker as sw  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"


class Stub:
    lost = False
    workname = "w"
    timeout = 60
    outbox = "/nonexistent"

    def session_id(self, summary, provider):
        return None

    def upload(self, summary):
        return None


class DecideExtraTest(unittest.TestCase):
    def extra(self, git):
        summary = {"files": {}, "git": git} if git is not None else {"files": {}}
        # exit code 75 (limit) returns right after the common fields are set
        state, extra = sw.Runner.decide(Stub(), 75, 0, "claude", "m", summary)
        self.assertEqual(state, "limit")
        return extra

    def test_count_and_head(self):
        e = self.extra({"head": SHA, "unpushed": 3})
        self.assertEqual((e["unpushed"], e["unpushed_commits"], e["head_sha"]), (True, 3, SHA))

    def test_zero_commits_ahead(self):
        e = self.extra({"head": SHA, "unpushed": 0})
        self.assertEqual((e["unpushed"], e["unpushed_commits"], e["head_sha"]), (False, 0, SHA))

    def test_no_git_leaves_keys_absent(self):
        e = self.extra(None)
        self.assertNotIn("unpushed_commits", e)
        self.assertNotIn("head_sha", e)
        self.assertIs(e["unpushed"], False)

    def test_no_commits_no_head(self):
        e = self.extra({"head": None, "unpushed": "x"})
        self.assertNotIn("head_sha", e)
        self.assertNotIn("unpushed_commits", e)


if __name__ == "__main__":
    unittest.main()
