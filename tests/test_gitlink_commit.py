#!/usr/bin/env python3
"""Auto-commit never stages submodule gitlinks unless opted in (agent-control PR 63): git level."""
import os, subprocess, sys, tempfile, unittest
from unittest.mock import patch
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
        def boom(wd, args, check=True, index=None):
            if args[:2] == ["diff", "--cached"] and "--raw" in args:
                raise RuntimeError("simulated")
            return orig(wd, args, check, index)
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
        def readd(wd, index=None):
            r = orig(wd, index=index)
            subprocess.run(["git", "add", "engine"], cwd=str(self.wd), check=True, env=dict(os.environ, GIT_INDEX_FILE=index))
            return r
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


class IndexSafetyTests(Base):
    def index_snapshot(self):
        return g(self.wd, "ls-files", "-s")

    def test_gitlink_replaced_by_files_fails_closed_and_keeps_index(self):
        # tracked pin "engine" replaced by a plain directory holding a staged file, plus an unrelated staged "keep"
        g(self.wd, "submodule", "deinit", "-q", "-f", "engine")
        g(self.wd, "rm", "-q", "--cached", "engine")
        import shutil; shutil.rmtree(self.wd / "engine")
        (self.wd / "engine").mkdir(); (self.wd / "engine" / "new.txt").write_text("n\n")
        (self.wd / "keep").write_text("k\n")
        g(self.wd, "add", "engine/new.txt", "keep")
        before, head = self.index_snapshot(), g(self.wd, "rev-parse", "HEAD")
        with self.assertRaises(RuntimeError) as cm:
            self.run_commit(allow_gitlink=False)
        self.assertIn("replaced by files", str(cm.exception))
        self.assertEqual(g(self.wd, "rev-parse", "HEAD"), head)
        self.assertEqual(self.index_snapshot(), before, "the caller's index is unchanged: engine/new.txt still staged")
        self.assertIn("engine/new.txt", g(self.wd, "diff", "--cached", "--name-only").split())

    def test_gitlink_replaced_by_ordinary_file_same_path_fails_closed(self):
        # tracked pin "engine" replaced by a regular file at the same path, staged, plus an unrelated staged "keep"
        g(self.wd, "submodule", "deinit", "-q", "-f", "engine")
        g(self.wd, "rm", "-q", "--cached", "engine")
        import shutil; shutil.rmtree(self.wd / "engine")
        (self.wd / "engine").write_text("now a file\n")
        (self.wd / "keep").write_text("k\n")
        g(self.wd, "add", "engine", "keep")
        self.assertIn("100644", g(self.wd, "ls-files", "-s", "engine"))
        before, head = self.index_snapshot(), g(self.wd, "rev-parse", "HEAD")
        with self.assertRaises(RuntimeError) as cm:
            self.run_commit(allow_gitlink=False)
        self.assertIn("share the path engine", str(cm.exception))
        self.assertEqual(g(self.wd, "rev-parse", "HEAD"), head)
        self.assertEqual(self.index_snapshot(), before, "the staged 100644 engine and keep are untouched")
        self.assertFalse([n for n in os.listdir(self.wd / ".git") if n.startswith("index.leftover.")], "temp index removed")

    def test_gitlink_to_symlink_same_path_fails_closed(self):
        g(self.wd, "submodule", "deinit", "-q", "-f", "engine")
        g(self.wd, "rm", "-q", "--cached", "engine")
        import shutil; shutil.rmtree(self.wd / "engine")
        os.symlink("README.md", self.wd / "engine")
        g(self.wd, "add", "engine")
        before, head = self.index_snapshot(), g(self.wd, "rev-parse", "HEAD")
        with self.assertRaises(RuntimeError):
            self.run_commit(allow_gitlink=False)
        self.assertEqual((g(self.wd, "rev-parse", "HEAD"), self.index_snapshot()), (head, before))

    def test_failure_leaves_caller_index_unchanged(self):
        (self.wd / "new.txt").write_text("n\n")
        before = self.index_snapshot()
        orig = gl.unstage_gitlinks
        gl.unstage_gitlinks = lambda wd, index=None: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                self.run_commit(allow_gitlink=False)
        finally:
            gl.unstage_gitlinks = orig
        self.assertEqual(self.index_snapshot(), before)
        self.assertFalse([n for n in os.listdir(self.wd / ".git") if n.startswith("index.leftover.")], "temp index removed")

    def test_success_updates_the_real_index(self):
        (self.wd / "new.txt").write_text("n\n")
        self.assertTrue(self.run_commit(allow_gitlink=False))
        self.assertEqual(g(self.wd, "diff", "--cached", "--name-only"), "", "index matches the new HEAD")
        self.assertIn("new.txt", g(self.wd, "ls-files").split())

    def test_git_timeout_fails_closed(self):
        (self.wd / "new.txt").write_text("n\n")
        head, before = g(self.wd, "rev-parse", "HEAD"), self.index_snapshot()
        real_run = subprocess.run
        def slow(cmd, *a, **k):
            if "add" in cmd[:5]:
                self.assertEqual(k.get("timeout"), gl.GIT_TIMEOUT)
                raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
            return real_run(cmd, *a, **k)
        gl.subprocess.run = slow
        try:
            with self.assertRaises(RuntimeError) as cm:
                self.run_commit(allow_gitlink=False)
        finally:
            gl.subprocess.run = real_run
        self.assertIn("timed out after 60 seconds", str(cm.exception))
        self.assertEqual((g(self.wd, "rev-parse", "HEAD"), self.index_snapshot()), (head, before))


class SourceRedactionTests(unittest.TestCase):
    """A pin whose path holds a secret: skipped list, log line and error text are redacted at the source."""
    FIXTURE = "TOKEN=abcdefgh12345"   # agent-control PR 63's fixture
    GHP = "ghp_" + "A1b2C3d4" * 5

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.wd = t / "wd"; init(self.wd); commit(self.wd, "README.md", "r\n")
        for name in (self.FIXTURE, "v/" + self.GHP):
            d = self.wd / name; d.mkdir(parents=True); init(d); commit(d, "x", "x\n")
        (self.wd / "f.txt").write_text("f\n")

    def test_skipped_and_log_are_redacted(self):
        skipped, logs = [], []
        self.assertTrue(gl.commit_leftover(self.wd, "m", allow_gitlink=False, skipped=skipped, log=logs.append))
        blob = repr(skipped) + "\n".join(logs)
        self.assertNotIn("abcdefgh12345", blob)
        self.assertNotIn("A1b2C3d4", blob)
        self.assertEqual(sorted(s["path"] for s in skipped), ["TOKEN=[redacted]", "v/[redacted]"])

    def test_backend_export_stderr_is_redacted(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sb.git_commit(str(self.wd), "Project log export")
        self.assertIn("2 submodule pin change(s) left uncommitted", err.getvalue())
        self.assertNotIn("abcdefgh12345", err.getvalue())
        self.assertNotIn("A1b2C3d4", err.getvalue())

    def failing_commit_tree(self):
        """Patch git commit-tree to fail with TOKEN=abcdefgh12345 then padding sized so the last 300 characters start
        right after "TOKEN=" (round-2 recheck). Hooks no longer run for exporter commits, so the stderr is injected."""
        real_run = subprocess.run
        def fake(cmd, *a, **k):
            if "commit-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 1, b"", ("TOKEN=abcdefgh12345\n" + "p" * 286).encode())
            return real_run(cmd, *a, **k)
        return patch.object(gl.subprocess, "run", fake)

    def test_failing_git_stderr_is_redacted_before_it_is_cut(self):
        import contextlib, io
        with self.failing_commit_tree(), self.assertRaises(RuntimeError) as cm:
            gl.commit_leftover(self.wd, "m", allow_gitlink=False)
        msg = str(cm.exception)
        self.assertIn("git ", msg)
        for frag in ("abcdefgh12345", "abcdefgh", "12345"):
            self.assertNotIn(frag, msg)
        self.assertLessEqual(len(msg), 400)
        err = io.StringIO()
        with self.failing_commit_tree(), contextlib.redirect_stderr(err):
            sb.git_commit(str(self.wd), "Project log export")
        self.assertIn("git commit failed", err.getvalue())
        self.assertNotIn("abcdefgh", err.getvalue())
        self.assertNotIn("12345", err.getvalue())

    def test_bounded_diagnostic_redacts_full_text_first(self):
        raw = ("TOKEN=abcdefgh12345\n" + "p" * 286).encode()
        self.assertEqual(len(raw.decode()[-300:]), 300)
        self.assertTrue(raw.decode()[-300:].startswith("abcdefgh12345"), "the old cut would have kept the bare value")
        out = gl.bounded_diagnostic(raw)
        self.assertNotIn("abcdefgh", out)
        self.assertLessEqual(len(out), 303)

    def test_error_diagnostics_are_redacted(self):
        orig = gl.unstage_gitlinks
        gl.unstage_gitlinks = lambda wd, index=None: []   # leaves the pins staged -> "still staged" error
        try:
            with self.assertRaises(RuntimeError) as cm:
                gl.commit_leftover(self.wd, "m", allow_gitlink=False)
        finally:
            gl.unstage_gitlinks = orig
        self.assertNotIn("abcdefgh12345", str(cm.exception))
        self.assertNotIn("A1b2C3d4", str(cm.exception))


class SanitizeTests(unittest.TestCase):
    hit = staticmethod(lambda t: sb.Backend.secret_hit(type("B", (), {"secret_patterns": sb.load_secret_patterns()})(), t))
    TOKEN = "ghp_" + "A1b2C3d4" * 5

    def test_redacts_before_truncating_and_counts_all(self):
        # token starts at char 190: a 200-char cut first would leave a fragment too short to match the pattern.
        path = "v" * 190 + self.TOKEN
        out = gl.sanitize_gitlinks([{"path": path, "old": self.TOKEN, "new": "bbbbbbb"}] * 25, self.hit)
        self.assertEqual(out["count"], 25)
        self.assertEqual(len(out["items"]), 10)
        for it in out["items"]:
            self.assertEqual((it["path"], it["old"], it["new"]), ("v" * 190 + "[redacted]", "[redacted]", "bbbbbbb"))
        self.assertNotIn("A1b2C3d4", repr(out))

    def test_assignment_fixture_from_pr63(self):
        out = gl.public_gitlinks([{"path": "TOKEN=abcdefgh12345", "old": "TOKEN=abcdefgh12345", "new": "a"},
                                  {"path": "x/API_KEY: 'zz9zz9zz'/y", "old": "b", "new": "c"}])
        self.assertNotIn("abcdefgh12345", repr(out))
        self.assertNotIn("zz9zz9zz", repr(out))
        self.assertEqual(out["items"][0], {"path": "TOKEN=[redacted]", "old": "[redacted]", "new": "a"})
        self.assertEqual(out["items"][1]["path"], "x/API_KEY=[redacted]/y")

    def test_true_count_is_kept_apart_from_the_list(self):
        out = gl.public_gitlinks([{"path": "p", "old": "", "new": ""}] * 3, limit=2, count=204)
        self.assertEqual((out["count"], len(out["items"])), (204, 2))

    def test_bounded_and_control_stripped(self):
        out = gl.sanitize_gitlinks([{"path": "p\n" + "y" * 500, "old": "a" * 40, "new": "b" * 40}], self.hit)
        it = out["items"][0]
        self.assertTrue(len(it["path"]) <= 200 and len(it["old"]) <= 12 and "\n" not in it["path"])


class CliBase(Base):
    """`swarmctl log export --dir DIR --git`, run through an installed-style symlink, uses the guarded exporter."""
    FIXTURE = "TOKEN=abcdefgh12345"

    def setUp(self):
        super().setUp()
        from helpers import ADMIN, Backend
        self.b = Backend()
        self.addCleanup(self.b.close)
        t = Path(self.tmp.name)
        (t / "tok").write_text(ADMIN)
        (t / "bin").mkdir()
        self.ctl = t / "bin" / "swarmctl"
        os.symlink(ROOT / "server" / "swarmctl", self.ctl)   # like setup-worker.sh: ln -sfn .../server/swarmctl
        self.env = dict(os.environ, SWARM_URL=self.b.base, SWARM_TOKEN_FILE=str(t / "tok"), HOME=str(t))
        # the export folder: a modified tracked pin (engine), a new embedded repo named after the fixture, an edit
        d = self.wd / self.FIXTURE; d.mkdir(); init(d); commit(d, "x", "x\n")
        (self.wd / "README.md").write_text("changed\n")

    def run_ctl(self, *args):
        return subprocess.run([str(self.ctl), *args], env=self.env, capture_output=True, text=True, timeout=60)



class CliExportTests(CliBase):
    def test_cli_export_skips_pins_and_never_prints_the_token(self):
        head = g(self.wd, "rev-parse", "HEAD")
        r = self.run_ctl("log", "export", "--dir", str(self.wd), "--git")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        for frag in ("abcdefgh12345", "abcdefgh", "12345"):
            self.assertNotIn(frag, out)
        self.assertIn("committed in", r.stdout)
        self.assertIn("2 submodule pin change(s) left uncommitted", r.stderr)
        self.assertIn("TOKEN=[redacted]", r.stderr)
        self.assertNotEqual(g(self.wd, "rev-parse", "HEAD"), head)
        self.assertEqual(self.in_head(), ["README.md"], "only the ordinary edit is committed")
        tree = g(self.wd, "ls-tree", "-r", "HEAD")
        self.assertNotIn(self.FIXTURE, tree, "the embedded repo is not committed")
        self.assertIn(f"160000 commit {g(self.wd, 'rev-parse', 'HEAD~1:engine')}\tengine", tree, "the old pin stays in HEAD")
        self.assertTrue(self.pin_modified())
        self.assertEqual(g(self.wd, "diff", "--cached", "--name-only"), "", "the real index matches the new HEAD")

    def test_cli_export_failure_is_redacted_and_exits_nonzero(self):
        (self.wd / ".git" / "refs" / "heads" / "main.lock").write_text("")   # update-ref cannot take the branch lock
        head, before = g(self.wd, "rev-parse", "HEAD"), g(self.wd, "ls-files", "-s")
        r = self.run_ctl("log", "export", "--dir", str(self.wd), "--git")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("git commit failed", r.stderr)
        for frag in ("abcdefgh", "12345"):
            self.assertNotIn(frag, r.stdout + r.stderr)
        self.assertEqual((g(self.wd, "rev-parse", "HEAD"), g(self.wd, "ls-files", "-s")), (head, before))


class ExporterHookTests(CliBase):
    """Exporter commits run no repository hooks (Astra recheck 4): the published tree is the inspected tree."""

    def hook(self, name, body):
        h = self.wd / ".git" / "hooks" / name
        h.write_text("#!/bin/sh\n" + body)
        h.chmod(0o755)

    def test_formatting_pre_commit_hook_cannot_commit_the_skipped_pin(self):
        # P1: a hook that formats README and runs `git add --update` would also stage the modified tracked submodule
        self.hook("pre-commit", 'printf "formatted\\n" > README.md\ngit add --update\n')
        before = g(self.wd, "rev-parse", "HEAD:engine")
        r = self.run_ctl("log", "export", "--dir", str(self.wd), "--git")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left uncommitted", r.stderr)
        self.assertIn("engine", r.stderr)
        self.assertEqual(g(self.wd, "rev-parse", "HEAD:engine"), before, "the skipped pin stays at its old value")
        self.assertEqual(g(self.wd, "show", "HEAD:README.md"), "changed", "the hook did not run")
        self.assertTrue(self.pin_modified())
        self.assertEqual(g(self.wd, "diff", "--cached", "--name-only"), "", "the real index matches the new HEAD")

    def test_post_commit_hook_is_not_run_and_cannot_time_out(self):
        # P2: a slow post-commit hook used to hit the timeout after HEAD moved, leaving the real index stale
        marker = Path(self.tmp.name) / "hook-ran"
        for name in ("pre-commit", "commit-msg", "prepare-commit-msg", "post-commit", "reference-transaction"):
            self.hook(name, f'echo {name} >> "{marker}"\nsleep 5\n')
        head = g(self.wd, "rev-parse", "HEAD")
        r = self.run_ctl("log", "export", "--dir", str(self.wd), "--git")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("committed in", r.stdout)
        self.assertFalse(marker.exists(), "no repository hook runs for an exporter commit")
        self.assertEqual(g(self.wd, "rev-parse", "HEAD~1"), head)
        self.assertEqual(g(self.wd, "show", "HEAD:README.md"), "changed")
        self.assertEqual(g(self.wd, "show", ":README.md"), "changed", "the real index is consistent with HEAD")
        self.assertEqual(g(self.wd, "diff", "--cached", "--name-only"), "")

    def test_index_hook_is_not_run_by_staging(self):
        # recheck 5 P2: a post-index-change hook ran (and could time out) on `git add -A`
        marker = Path(self.tmp.name) / "index-hook-ran"
        self.hook("post-index-change", f'echo ran >> "{marker}"\nsleep 5\n')
        (self.wd / "new.txt").write_text("n\n")
        self.assertTrue(self.run_commit(allow_gitlink=False))
        self.assertFalse(marker.exists(), "no repository hook runs for any exporter git command")

    def test_concurrent_commit_after_staging_is_not_reversed(self):
        # recheck 5 P2: HEAD was read after building the tree, so a concurrent commit became the parent and was reversed
        (self.wd / "new.txt").write_text("n\n")
        real_run, moved = subprocess.run, []
        def racing(cmd, *a, **k):
            r = real_run(cmd, *a, **k)
            if "write-tree" in cmd and not moved:
                (self.wd / "manual.md").write_text("manual\n")
                for c in (["add", "manual.md"], ["commit", "-q", "-m", "manual"]):
                    real_run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *c],
                             cwd=str(self.wd), check=True, capture_output=True)
                moved.append(g(self.wd, "rev-parse", "HEAD"))
            return r
        with patch.object(gl.subprocess, "run", racing):
            try:
                self.run_commit(allow_gitlink=False)
            except RuntimeError:
                pass
        self.assertEqual(g(self.wd, "rev-parse", "HEAD"), moved[0], "the concurrent commit stays HEAD")
        self.assertIn("manual.md", g(self.wd, "ls-tree", "--name-only", "HEAD").splitlines())

    def test_moved_head_is_not_overwritten(self):
        # update-ref is a compare-and-swap: if HEAD moves between inspection and publish, nothing is published
        (self.wd / "new.txt").write_text("n\n")
        before = (self.wd / ".git" / "index").read_bytes()
        real_run, moved = subprocess.run, []
        def racing(cmd, *a, **k):
            if "update-ref" in cmd and not moved:
                moved.append(commit(self.wd / "engine", "c", "c\n"))   # unrelated; then move HEAD out from under us
                real_run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q",
                          "--allow-empty", "-m", "race"], cwd=str(self.wd), check=True, capture_output=True)
                moved.append(g(self.wd, "rev-parse", "HEAD"))
            return real_run(cmd, *a, **k)
        with patch.object(gl.subprocess, "run", racing), self.assertRaises(RuntimeError):
            self.run_commit(allow_gitlink=False)
        self.assertEqual(g(self.wd, "rev-parse", "HEAD"), moved[-1], "the concurrent commit is kept")
        self.assertEqual((self.wd / ".git" / "index").read_bytes(), before, "the real index is unchanged")

    def test_real_timeout_before_publish_keeps_head_and_index(self):
        # replaces the old pre-commit-hook timeout probe: a real git process (write-tree, after the inspection and
        # before publishing) outlives the timeout; HEAD and the real index stay byte-identical
        import shutil
        wrap = Path(self.tmp.name) / "slowgit"; wrap.mkdir()
        (wrap / "git").write_text(f'#!/bin/sh\nfor a in "$@"; do [ "$a" = write-tree ] && sleep 2; done\nexec {shutil.which("git")} "$@"\n')
        (wrap / "git").chmod(0o755)
        before = (g(self.wd, "rev-parse", "HEAD"), (self.wd / ".git" / "index").read_bytes())
        with patch.dict(os.environ, {"PATH": f"{wrap}:{os.environ['PATH']}"}), patch.object(gl, "GIT_TIMEOUT", 0.5), \
                self.assertRaisesRegex(RuntimeError, "git write-tree timed out"):
            self.run_commit(allow_gitlink=False)
        self.assertEqual((g(self.wd, "rev-parse", "HEAD"), (self.wd / ".git" / "index").read_bytes()), before)

    def test_identity_and_message_unchanged(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        with patch.dict(os.environ, clean, clear=True):
            self.assertTrue(gl.commit_leftover(self.wd, "  export msg  \n\n\n", allow_gitlink=False))
        self.assertEqual(g(self.wd, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%B"),
                         "swarm-backend <swarm-backend@localhost>|swarm-backend <swarm-backend@localhost>|  export msg")
        self.assertIn("commit: ", g(self.wd, "reflog", "-1"))


if __name__ == "__main__":
    unittest.main()
