"""Per-project test-base env var (agent-control PR 69, ported): CLOZY_TEST_BASE=origin/<PR base>, exported for the whole task."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import swarm_worker as sw  # noqa: E402

M = {"clozy": "CLOZY_TEST_BASE"}


def env(task, environ=None, mapping=M):
    return sw.task_base_env(task, environ or {}, mapping)


class TaskBaseEnv(unittest.TestCase):
    def test_default_map_has_clozy(self):
        self.assertEqual(sw.task_base_env_map(sw.DEFAULT_TASK_BASE_ENV), M)

    def test_pr_base_branch(self):
        self.assertEqual(env({"project": "clozy", "base": "feat/x"}), {"CLOZY_TEST_BASE": "origin/feat/x"})

    def test_parent_base_used_when_task_has_none(self):
        self.assertEqual(env({"project": "clozy", "parent": {"base": "dev"}}), {"CLOZY_TEST_BASE": "origin/dev"})

    def test_no_base_falls_back_to_main(self):
        self.assertEqual(env({"project": "clozy"}), {"CLOZY_TEST_BASE": "origin/main"})

    def test_already_set_is_left_alone(self):
        self.assertEqual(env({"project": "clozy", "base": "b"}, {"CLOZY_TEST_BASE": "origin/keep"}), {})

    def test_unmapped_project_gets_nothing(self):
        self.assertEqual(env({"project": "other", "base": "b"}), {})
        self.assertEqual(env({"base": "b"}), {})

    def test_unsafe_base_falls_back(self):
        for bad in ("a b", "x;rm -rf /", "../x", "a..b", "a\nb", "$(id)", "-rf", "x.lock", "a//b", "/abs", ".hid", "a`id`"):
            self.assertEqual(env({"project": "clozy", "base": bad}), {"CLOZY_TEST_BASE": "origin/main"}, bad)

    def test_origin_prefix_not_doubled(self):
        self.assertEqual(env({"project": "clozy", "base": "origin/dev"}), {"CLOZY_TEST_BASE": "origin/dev"})

    def test_map_override(self):
        m = sw.task_base_env_map("clozy=CLOZY_TEST_BASE, Foo=FOO_BASE,bad=lower,x=SWARM_URL,=A")
        self.assertEqual(m, {"clozy": "CLOZY_TEST_BASE", "foo": "FOO_BASE"})
        self.assertEqual(env({"project": "foo", "base": "b"}, mapping=m), {"FOO_BASE": "origin/b"})
        self.assertEqual(sw.task_base_env_map(""), {})


if __name__ == "__main__":
    unittest.main()
