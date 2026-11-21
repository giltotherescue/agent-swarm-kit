#!/usr/bin/env python3
"""Auto-commit never stages submodule gitlinks unless opted in (agent-control PR 63): git level."""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import swarm_gitlink as gl  # noqa: E402
import swarm_backend as sb  # noqa: E402


def g(wd, *a):
    r = subprocess.run(["git", "-c", "protocol.file.allow=always", "-c", "user.name=T", "-c", "user.email=t@example.com", *a],
                       cwd=str(wd), capture_output=True, text=True)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


def init(wd):
    Path(wd).mkdir(parents=True, exist_ok=True)
    g(wd, "init", "-q", "-b", "main")


def commit(wd, name="f.txt", text="x\n"):
    (Path(wd) / name).write_text(text)
    g(wd, "add", "-A")
    g(wd, "commit", "-q", "-m", "c " + text)
    return g(wd, "rev-parse", "HEAD")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.sub = t / "sub"; init(self.sub); self.sub_a = commit(self.sub, "a", "a\n")
        self.wd = t / "wd"; init(self.wd); commit(self.wd, "README.md", "r\n")
        g(self.wd, "submodule", "add", "-q", str(self.sub), "engine")
        g(self.wd, "commit", "-q", "-m", "add sub")
        self.sub_b = commit(self.wd / "engine", "b", "b\n")  # moves the submodule HEAD: pin modified
        self.skipped, self.logs = [], []
        self.addCleanup(self.tmp.cleanup)

    def run_commit(self, **kw):
        return gl.commit_leftover(self.wd, "agent task tid: uncommitted changes", skipped=self.skipped, log=self.logs.append, **kw)

    def in_head(self):
        return sorted(g(self.wd, "show", "--name-only", "--format=", "HEAD").split())

    def pin_modified(self):
        return "engine" in g(self.wd, "status", "--porcelain")


class GitlinkTests(Base):
    def test_modified_pin_not_committed_and_logged(self):
        (self.wd / "new.txt").write_text("n\n")
        self.assertTrue(self.run_commit(allow_gitlink=False))
        self.assertEqual(self.in_head(), ["new.txt"])
        self.assertTrue(self.pin_modified())
        self.assertEqual([(s["path"], s["old"], s["new"]) for s in self.skipped], [("engine", self.sub_a[:7], self.sub_b[:7])])
        self.assertTrue(any("engine" in l and self.sub_b[:7] in l for l in self.logs))

    def test_only_pin_makes_no_commit(self):
        head = g(self.wd, "rev-parse", "HEAD")
        self.assertFalse(self.run_commit(allow_gitlink=False))
        self.assertEqual(g(self.wd, "rev-parse", "HEAD"), head)
        self.assertEqual(len(self.skipped), 1)

    def test_opt_in_param_and_env_and_task_field(self):
        self.assertTrue(self.run_commit(allow_gitlink=True))
        self.assertEqual(self.in_head(), ["engine"])
        self.assertEqual(self.skipped, [])
        self.assertTrue(gl.gitlink_allowed({"allow_gitlink": 1}, env={}))
        self.assertTrue(gl.gitlink_allowed({}, env={"AGENT_ALLOW_GITLINK": "1"}))
        self.assertFalse(gl.gitlink_allowed({"allow_gitlink": 0}, env={}))

    def test_opt_in_env(self):
        os.environ["AGENT_ALLOW_GITLINK"] = "1"
        try:
            self.assertTrue(self.run_commit())
        finally:
            del os.environ["AGENT_ALLOW_GITLINK"]
        self.assertEqual(self.in_head(), ["engine"])

    def test_added_gitlink(self):
        g(self.wd, "clone", "-q", str(self.sub), "vendor")
        g(self.wd, "-C", str(self.wd / "engine"), "checkout", "-q", self.sub_a)
        (self.wd / "n.txt").write_text("n\n")
        self.assertTrue(self.run_commit(allow_gitlink=False))
        self.assertEqual(self.in_head(), ["n.txt"])
        self.assertEqual([(s["path"], s["old"]) for s in self.skipped], [("vendor", "")])
        self.assertTrue((self.wd / "vendor").exists())

    def test_added_gitlink_committed_with_opt_in(self):
        g(self.wd, "clone", "-q", str(self.sub), "vendor")
        g(self.wd, "-C", str(self.wd / "engine"), "checkout", "-q", self.sub_a)
        self.assertTrue(self.run_commit(allow_gitlink=True))
        self.assertEqual(self.in_head(), ["vendor"])

    def test_deleted_gitlink(self):
        g(self.wd, "-C", str(self.wd / "engine"), "checkout", "-q", self.sub_a)
        g(self.wd, "submodule", "deinit", "-q", "-f", "engine")
        g(self.wd, "rm", "-q", "-f", "engine")
        (self.wd / "n.txt").write_text("n\n")
        self.assertTrue(self.run_commit(allow_gitlink=False))
        self.assertEqual(self.in_head(), [".gitmodules", "n.txt"])
        self.assertEqual([(s["path"], s["new"]) for s in self.skipped], [("engine", "")])
        self.assertIn("engine", g(self.wd, "ls-tree", "HEAD"))

    def test_ignore_all_config_cannot_hide_a_pin(self):
        g(self.wd, "config", "submodule.engine.ignore", "all")
        (self.wd / "n.txt").write_text("n\n")
        self.assertTrue(self.run_commit(allow_gitlink=False))
        self.assertEqual(self.in_head(), ["n.txt"])
        self.assertEqual([s["path"] for s in self.skipped], ["engine"])

    def test_no_head_initial_commit(self):
        t = Path(self.tmp.name) / "fresh"; init(t)
        g(t, "clone", "-q", str(self.sub), "engine")
        (t / "f.txt").write_text("f\n")
        skipped = []
        self.assertTrue(gl.commit_leftover(t, "m", allow_gitlink=False, skipped=skipped))
        self.assertEqual(g(t, "show", "--name-only", "--format=", "HEAD").split(), ["f.txt"])
        self.assertEqual([s["path"] for s in skipped], ["engine"])

    def test_scan_error_fails_closed(self):
        (self.wd / "new.txt").write_text("n\n")
        orig, head = gl._git_raw, g(self.wd, "rev-parse", "HEAD")
        def boom(wd, args, check=True):
            if args[:2] == ["diff", "--cached"] and "--raw" in args:
                raise RuntimeError("simulated")
            return orig(wd, args, check)
        gl._git_raw = boom
        try:
            with self.assertRaises(RuntimeError):
                self.run_commit(allow_gitlink=False)
        finally:
            gl._git_raw = orig
        self.assertEqual(g(self.wd, "rev-parse", "HEAD"), head)

    def test_still_staged_refuses_commit(self):
        (self.wd / "new.txt").write_text("n\n")
        orig, head = gl.unstage_gitlinks, g(self.wd, "rev-parse", "HEAD")
        def readd(wd):
            r = orig(wd); g(self.wd, "add", "engine"); return r
        gl.unstage_gitlinks = readd
        try:
            with self.assertRaises(RuntimeError):
                self.run_commit(allow_gitlink=False)
        finally:
            gl.unstage_gitlinks = orig
        self.assertEqual(g(self.wd, "rev-parse", "HEAD"), head)

    def test_odd_names_do_not_hit_other_paths(self):
        g(self.wd, "-C", str(self.wd / "engine"), "checkout", "-q", self.sub_a)
        for name in (":(literal)engine", ":(exclude)engine", "lf\nname", "sp ace"):
            with self.subTest(name=name):
                self.skipped.clear()
                d = self.wd / name; d.mkdir(); init(d); commit(d, "x", "x\n")
                (self.wd / "code.txt").write_text(repr(name))
                self.assertTrue(self.run_commit(allow_gitlink=False))
                self.assertEqual(self.in_head(), ["code.txt"])
                self.assertEqual(len(self.skipped), 1)
                self.assertNotIn("\n", self.skipped[0]["path"])
                self.assertIn("engine", g(self.wd, "ls-tree", "-r", "HEAD", "--name-only", "-z").split("\0"))
                import shutil; shutil.rmtree(d)

    def test_plain_repo_unchanged(self):
        t = Path(self.tmp.name) / "plain"; init(t); commit(t, "README.md", "r\n")
        (t / "a.txt").write_text("a\n")
        skipped = []
        self.assertTrue(gl.commit_leftover(t, "m", skipped=skipped))
        self.assertEqual(skipped, [])
        self.assertFalse(gl.commit_leftover(t, "m"))

    def test_backend_log_export_commit_skips_pins(self):
        """sb.git_commit (the project-log export commit) is the kit's auto-commit site: it must skip pins too."""
        (self.wd / "export.md").write_text("week\n")
        sb.git_commit(str(self.wd), "Project log export 2026-W47")
        self.assertEqual(self.in_head(), ["export.md"])
        self.assertTrue(self.pin_modified())


class SanitizeTests(unittest.TestCase):
    hit = staticmethod(lambda t: sb.Backend.secret_hit(type("B", (), {"secret_patterns": sb.load_secret_patterns()})(), t))
    TOKEN = "ghp_" + "A1b2C3d4" * 5

    def test_redacts_before_truncating_and_counts_all(self):
        # token starts at char 190: a 200-char cut would leave a fragment too short to match the pattern.
        path = "v" * 190 + self.TOKEN
        out = gl.sanitize_gitlinks([{"path": path, "old": self.TOKEN, "new": "bbbbbbb"}] * 25, self.hit)
        self.assertEqual(out["count"], 25)
        self.assertEqual(len(out["items"]), 10)
        for it in out["items"]:
            self.assertEqual((it["path"], it["old"], it["new"]), ("[redacted]", "[redacted]", "bbbbbbb"))
        self.assertNotIn("A1b2C3d4", repr(out))

    def test_bounded_and_control_stripped(self):
        out = gl.sanitize_gitlinks([{"path": "p\n" + "y" * 500, "old": "a" * 40, "new": "b" * 40}], self.hit)
        it = out["items"][0]
        self.assertTrue(len(it["path"]) <= 200 and len(it["old"]) <= 12 and "\n" not in it["path"])


if __name__ == "__main__":
    unittest.main()
