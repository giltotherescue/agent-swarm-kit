"""with-secrets (CoS 8), check-secrets (Astra 10), swarmctl (Astra 33, 34, 36, CoS 17), review gate (CoS 12), cleanup (Astra 21, CoS 20)."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from helpers import ADMIN, Backend

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SERVER = os.path.join(ROOT, "server")


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


class WithSecretsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.chmod(self.dir, 0o700)
        self.env = dict(os.environ, SWARM_SECRETS_DIR=self.dir)

    def write(self, name, text, mode=0o600):
        p = os.path.join(self.dir, name + ".env")
        with open(p, "w") as fh:
            fh.write(text)
        os.chmod(p, mode)
        return p

    def run_ws(self, *args):
        return subprocess.run([os.path.join(SERVER, "with-secrets")] + list(args), env=self.env, capture_output=True, text=True)

    def test_values_reach_only_the_command(self):
        self.write("voice", "# shared voice key\nVOICE_API_KEY=sv-value-123\nexport VOICE_REGION='eu'\n")
        self.write("other", 'OTHER_KEY="o-456"\n')
        r = self.run_ws("voice", "other", "--", "sh", "-c", 'printf "%s|%s|%s" "$VOICE_API_KEY" "$VOICE_REGION" "$OTHER_KEY"')
        self.assertEqual((r.returncode, r.stdout), (0, "sv-value-123|eu|o-456"))
        self.assertNotIn("VOICE_API_KEY", os.environ)

    def test_check_prints_names_never_values(self):
        self.write("voice", "VOICE_API_KEY=sv-value-123\n")
        r = self.run_ws("--check", "voice")
        self.assertEqual(r.stdout.strip(), "voice: VOICE_API_KEY")
        self.assertNotIn("sv-value-123", r.stdout + r.stderr)

    def test_refusals(self):
        r = self.run_ws("missing", "--", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no secret file", r.stderr)
        self.write("loose", "K=secret-loose-value\n", mode=0o644)
        r = self.run_ws("loose", "--", "true")
        self.assertIn("chmod 600", r.stderr)
        self.assertNotIn("secret-loose-value", r.stderr)
        real = self.write("real", "K=v\n")
        os.symlink(real, os.path.join(self.dir, "linked.env"))
        self.assertIn("link", self.run_ws("linked", "--", "true").stderr)
        self.assertIn("bad secret name", self.run_ws("../etc/passwd", "--", "true").stderr)
        self.write("broken", "this line has no equals sign secret-xyz\n")
        r = self.run_ws("broken", "--", "true")
        self.assertIn("line 1", r.stderr)
        self.assertNotIn("secret-xyz", r.stderr)
        self.assertIn("usage", self.run_ws("real", "true").stderr)


@unittest.skipUnless(shutil.which("sops") and shutil.which("age-keygen"), "SOPS and age required")
class EncryptedSecretsTest(unittest.TestCase):
    def test_runtime_only_and_atomic_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            base = home / ".config" / "swarm-secrets"
            base.mkdir(parents=True, mode=0o700)
            env = {key: value for key, value in os.environ.items() if not key.startswith("SOPS_")}
            env.update(HOME=temp, GIT_CONFIG_NOSYSTEM="1")

            def run(*command, data=None, check=True, extra_env=None):
                return subprocess.run(command, env=dict(env, **(extra_env or {})), input=data,
                                      capture_output=True, check=check)

            sync = home / "swarm-sync-secrets"
            sync.symlink_to(Path(SERVER) / "with-secrets")
            run(sync, "--if-configured")
            worker_key, owner_key = base / "age.key", home / "owner.key"
            run("age-keygen", "-o", worker_key)
            run("age-keygen", "-o", owner_key)
            worker_public = run("age-keygen", "-y", worker_key).stdout.decode().strip()
            owner_public = run("age-keygen", "-y", owner_key).stdout.decode().strip()
            source = home / "source"
            run("git", "init", "-q", "-b", "main", source)
            git = ("git", "-C", source)
            for scope in ("runtime", "admin"):
                (source / scope).mkdir()
            sample = os.urandom(16).hex()

            def encrypt(scope, recipient):
                data = json.dumps({"EXAMPLE_ENV": sample}).encode()
                return run("sops", "encrypt", "--age", recipient, "--input-type", "json", "--output-type", "json",
                           "--filename-override", f"{scope}/tools.json", "/dev/stdin", data=data).stdout

            (source / "runtime" / "tools.json").write_bytes(encrypt("runtime", worker_public))
            admin = encrypt("admin", owner_public)
            (source / "admin" / "tools.json").write_bytes(admin)
            run(*git, "add", ".")
            run(*git, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "encrypted")
            run("git", "clone", "-q", source, base / "repo")
            manifest = base / "runtime-bundles"
            manifest.write_text("tools\n")
            manifest.chmod(0o600)
            result = run(sync)
            self.assertNotIn(sample.encode(), result.stdout + result.stderr)
            cache = base / "cache.json"
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            original = cache.read_bytes()
            wrapper = Path(SERVER) / "with-secrets"
            self.assertEqual(run(wrapper, "--check", "tools").stdout.strip(), b"tools: EXAMPLE_ENV")
            self.assertEqual(run(wrapper, "tools", "--", "printenv", "EXAMPLE_ENV").stdout.strip(), sample.encode())
            self.assertNotEqual(run("sops", "decrypt", "--input-type", "json", "/dev/stdin", data=admin, check=False,
                                    extra_env={"SOPS_AGE_KEY_FILE": str(worker_key)}).returncode, 0)
            (source / "runtime" / "tools.json").write_bytes(encrypt("runtime", owner_public))
            run(*git, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qam", "wrong recipient")
            result = run(sync, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(sample.encode(), result.stdout + result.stderr)
            self.assertEqual(cache.read_bytes(), original)
            manifest.write_text("")
            run(sync)
            self.assertNotEqual(run(wrapper, "--check", "tools", check=False).returncode, 0)


class CodexVerdictTest(unittest.TestCase):
    def run_codex(self, event_types, exit_code):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            codex = folder / "codex"
            codex.write_text('#!/bin/sh\ncat\nexit "$MOCK_EXIT"\n')
            codex.chmod(0o700)
            prompt = folder / "prompt"
            events = [{"type": "thread.started", "thread_id": "test-thread"}]
            events.extend({"type": kind, "item": {"type": "error"}} for kind in event_types)
            prompt.write_text("\n".join(map(json.dumps, events)) + "\n")
            result = subprocess.run([Path(SERVER) / "run-agent", "codex", "default", folder, prompt],
                                    env=dict(os.environ, PATH=f"{folder}:{os.environ['PATH']}", MOCK_EXIT=str(exit_code)),
                                    capture_output=True)
            self.assertEqual((folder / ".task-context" / "session-codex").read_text().strip(), "test-thread")
            return result.returncode

    def test_completed_turn_overrides_warnings_and_cli_exit(self):
        self.assertEqual(self.run_codex(["turn.started", "error", "item.completed", "turn.completed"], 1), 0)

    def test_failed_or_incomplete_turn_is_not_success(self):
        self.assertNotEqual(self.run_codex(["turn.started", "turn.failed"], 0), 0)
        self.assertNotEqual(self.run_codex(["turn.started", "item.completed"], 0), 0)
        self.assertNotEqual(self.run_codex(["turn.completed", "error"], 0), 0)
        self.assertEqual(self.run_codex(["turn.completed"], 124), 124)


class CheckSecretsTest(unittest.TestCase):
    def test_reports_file_and_line_but_never_the_secret(self):
        d = tempfile.mkdtemp()
        fake = "gh" + "p_" + "B" * 36
        with open(os.path.join(d, "notes.txt"), "w") as fh:
            fh.write("hello\nkey " + fake + "\n")
        r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "check-secrets.sh"), d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("notes.txt:2", r.stdout)
        self.assertNotIn(fake, r.stdout + r.stderr)

    def test_scan_errors_are_not_clean(self):
        r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "check-secrets.sh"), "/no/such/folder"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("clean", r.stdout)
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".secret-patterns"), "w") as fh:
            fh.write("broken[regex\n")
        r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "check-secrets.sh"), d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


class SwarmctlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.home = tempfile.mkdtemp()
        tok = os.path.join(cls.home, "admin.token")
        with open(tok, "w") as fh:
            fh.write(ADMIN)
        cls.bin = os.path.join(cls.home, "bin")
        os.makedirs(cls.bin)
        os.symlink(os.path.join(SERVER, "swarmctl"), os.path.join(cls.bin, "swarmctl"))  # like /usr/local/bin/swarmctl
        cls.env = dict(os.environ, HOME=cls.home, SWARM_URL=cls.b.base, SWARM_TOKEN_FILE=tok)
        cls.ctl = load("swarmctl_mod", os.path.join(SERVER, "swarmctl"))

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def sw(self, *args, stdin=None):
        return subprocess.run([os.path.join(self.bin, "swarmctl")] + list(args), env=self.env, capture_output=True, text=True, input=stdin)

    def test_review_works_through_the_installed_link(self):
        r = self.sw("review", "-r", "acme/site", "--pr-number", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = r.stdout.split()[0]
        t = self.b.req("GET", f"/tasks/{tid}")["task"]
        self.assertEqual((t["kind"], t["pr_number"], t["report_only"]), ("review", 7, 1))
        self.assertIn("acme/site", t["prompt"])

    def test_project_set_and_the_older_form(self):
        r = self.sw("project", "set", "website", "--repo", "acme/site", "--bot", "website-bot", "--allow-push-main")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = json.loads(r.stdout)["project"]
        self.assertEqual((p["default_repo"], p["bots"], p["allow_push_main"]), ("acme/site", ["website-bot"], 1))
        r = self.sw("project", "website", "--model", "opus", "--no-push-main")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = json.loads(r.stdout)["project"]
        self.assertEqual((p["default_model"], p["bots"], p["allow_push_main"]), ("opus", ["website-bot"], 0))

    def test_follow_up_and_held_behind_output(self):
        tid = self.sw("enqueue", "-", stdin="first job").stdout.split()[0]
        c = self.b.claim()
        self.b.finish(c, "done")
        out1 = self.sw("enqueue", "--follow-up", tid, "-", stdin="change one").stdout
        out2 = self.sw("enqueue", "--follow-up", tid, "-", stdin="change two").stdout
        self.assertNotIn("held behind", out1)
        self.assertIn("held behind " + out1.split()[0], out2)

    def test_local_path_warning(self):
        warn = self.ctl.local_path_warnings("Use /workspace/brand/logo.png and /home/me/notes.md and https://x.example/home/", ["notes.md"])
        self.assertEqual(warn, ["/workspace/brand/logo.png"])
        r = self.sw("enqueue", "--allow-duplicate", "-", stdin="Read /Users/me/plan.pdf first")
        self.assertIn("Attach the file with --attach", r.stderr)

    def test_token_file_is_private_and_never_through_a_link(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "t.token")
        with open(out, "w") as fh:
            fh.write("old")
        os.chmod(out, 0o644)
        self.ctl.write_secret_file(out, "new-token")
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o600")
        self.assertEqual(open(out).read(), "new-token\n")
        link = os.path.join(d, "link.token")
        os.symlink(os.path.join(d, "elsewhere"), link)
        with self.assertRaises(SystemExit):
            self.ctl.check_writable(link)
        # A bare file name (no folder) works: this used to crash after the old token was already replaced.
        self.b.req("PUT", "/bots/cli-bot", {})
        r = subprocess.run([os.path.join(self.bin, "swarmctl"), "bot", "token", "cli-bot", "--out", "cli.token"], env=self.env,
                           capture_output=True, text=True, cwd=d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("swb_", r.stdout + r.stderr)
        self.assertEqual(oct(os.stat(os.path.join(d, "cli.token")).st_mode & 0o777), "0o600")

    def test_drain_and_undrain(self):
        d = tempfile.mkdtemp()
        env = dict(self.env, SWARM_DRAIN_FILE=os.path.join(d, "drain"), SWARM_STATE_FILE=os.path.join(d, "state.json"))
        r = subprocess.run([os.path.join(self.bin, "swarmctl"), "drain"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(d, "drain")))
        subprocess.run([os.path.join(self.bin, "swarmctl"), "undrain"], env=env, check=True, capture_output=True)
        self.assertFalse(os.path.exists(os.path.join(d, "drain")))


class ReviewGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, SERVER)
        import swarm_worker
        cls.check = staticmethod(swarm_worker.review_findings_missing_refs)

    def test_extensionless_paths_count(self):
        report = ("## Summary\nStatus: done\n\n## Findings\n1. **High**: token leak in server/run-agent:42\n"
                  "2. Medium: Makefile:7 runs as root\n3. Low: retry loop at src/app.ts:10-14\n")
        self.assertEqual(self.check(report), [])

    def test_missing_refs_are_listed(self):
        report = "## Findings\n- High: something is wrong somewhere\n- Low: see https://example.com:443/x\n- None beyond these\n"
        self.assertEqual(len(self.check(report)), 2)

    def test_severity_items_without_findings_heading(self):
        self.assertEqual(len(self.check("## What changed\n- High: no reference\n- added tests\n")), 1)
        self.assertEqual(self.check("## Summary\nStatus: done\n\nNo findings at all.\n"), [])


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        os.environ.update(SWARM_WORK_DIR=self.work, SWARM_BACKUP_DIR=os.path.join(self.tmp, "backups"))
        self.c = load("swarm_cleanup_mod", os.path.join(SERVER, "swarm_cleanup.py"))
        self.old = 1_000_000

    def git(self, path, *args):
        return subprocess.run(["git", "-C", path, "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(args),
                              check=True, capture_output=True, text=True)

    def make_repo(self, name):
        origin = os.path.join(self.tmp, name + "-origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        path = os.path.join(self.work, name)
        subprocess.run(["git", "clone", "-q", origin, path], check=True, capture_output=True)
        open(os.path.join(path, "a.txt"), "w").write("a\n")
        self.git(path, "add", ".")
        self.git(path, "commit", "-qm", "a")
        self.git(path, "push", "-q", "origin", "HEAD:main")
        return path

    def age(self, path):
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                os.utime(os.path.join(root, n), (self.old, self.old), follow_symlinks=False)
        os.utime(path, (self.old, self.old))

    def test_untracked_and_binary_files_are_saved_before_delete(self):
        path = self.make_repo("t1")
        open(os.path.join(path, "new-notes.md"), "w").write("not committed\n")
        with open(os.path.join(path, "logo.bin"), "wb") as fh:
            fh.write(bytes(range(256)))
        self.git(path, "checkout", "-qb", "agent/t1")
        open(os.path.join(path, "b.txt"), "w").write("b\n")
        self.git(path, "add", "b.txt")
        self.git(path, "commit", "-qm", "b")
        self.age(path)
        self.c.act({"t1": {"state": "done", "finished_at": self.old}}, dry=False)
        self.assertFalse(os.path.exists(path))
        (day,) = os.listdir(os.path.join(self.tmp, "backups"))
        dest = os.path.join(self.tmp, "backups", day)
        with tarfile.open(os.path.join(dest, "t1.files.tar.gz")) as tf:
            names = tf.getnames()
            self.assertIn("t1/new-notes.md", names)
            self.assertEqual(tf.extractfile("t1/logo.bin").read(), bytes(range(256)))
        r = subprocess.run(["git", "bundle", "list-heads", os.path.join(dest, "t1.bundle")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)

    def test_repo_with_a_live_worktree_is_kept(self):
        path = self.make_repo("t2")
        wt = os.path.join(self.work, "scratch", "wt")
        os.makedirs(os.path.dirname(wt))
        self.git(path, "worktree", "add", "-q", wt)
        self.age(path)
        self.c.act({"t2": {"state": "done", "finished_at": self.old}}, dry=False)
        self.assertTrue(os.path.exists(path))

    def test_active_and_unknown_folders_are_never_touched(self):
        for name, state in (("q1", "queued"), ("r1", "running"), ("w1", "waiting"), ("x1", None)):
            p = os.path.join(self.work, name)
            os.makedirs(p)
            open(os.path.join(p, "f"), "w").write("x")
            self.age(p)
        self.c.act({"q1": {"state": "queued"}, "r1": {"state": "running"}, "w1": {"state": "waiting"}}, dry=False)
        self.assertEqual(sorted(os.listdir(self.work)), ["q1", "r1", "w1", "x1"])


if __name__ == "__main__":
    unittest.main()
