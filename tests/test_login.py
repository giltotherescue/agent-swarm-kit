"""Tests for swarmctl login (server/swarm_login.py): parsing what the real CLIs print, saving the Claude token,
and the background flow with stand-in programs. The parsing samples are real output from codex 0.167.0, gh and
claude 2.1.321 with the codes and links replaced by made-up values."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server")
sys.path.insert(0, SERVER)
import swarm_login as sl  # noqa: E402

CODEX = (
    "\x1b[90mOpenAI's command-line coding agent\x1b[0m\n\nFollow these steps to sign in with ChatGPT using device code authorization:\n\n"
    "1. Open this link in your browser and sign in to your account\n   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n"
    "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n   \x1b[94mABCD-EF123\x1b[0m\n\n"
    "Continue only if you started this login in Codex. If a website or another person gave you this code, cancel.\n")
GITHUB = ("! Failed to copy one-time code to clipboard\n  No clipboard utilities available.\n"
          "! First copy your one-time code: ABCD-1234\nOpen this URL to continue in your web browser: https://github.com/login/device\n")
URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=00000000-0000-0000-0000-000000000000&response_type=code&scope=user%3Ainference&state=fake"
CLAUDE = ("Browser didn't open? Use the url below to sign in (c to copy)\n"
          f"\x1b]8;id=x1;{URL}\x1b\\{URL[:60]}\x1b]8;;\x1b\\\nPaste code here if prompted > ")
TOKEN = "sk-ant-oat01-" + "A1b2C3d4" * 6


class Parsing(unittest.TestCase):
    def test_codex_link_code_and_expiry(self):
        self.assertEqual(sl.parse_codex(CODEX), {"url": "https://auth.openai.com/codex/device", "code": "ABCD-EF123", "expires_minutes": 15})

    def test_codex_partial_output_is_not_complete(self):
        part = CODEX.split("2. Enter")[0]
        info = sl.parse_codex(part)
        self.assertEqual(info, {"url": "https://auth.openai.com/codex/device"})
        self.assertFalse(sl.needs("codex", info))

    def test_github_code_and_link(self):
        self.assertEqual(sl.parse_github(GITHUB), {"code": "ABCD-1234", "url": "https://github.com/login/device"})

    def test_claude_prefers_the_hyperlink_target_over_cut_off_text(self):
        self.assertEqual(sl.parse_claude(CLAUDE), {"url": URL})

    def test_claude_falls_back_to_plain_text(self):
        self.assertEqual(sl.parse_claude("open " + URL + "\nmore"), {"url": URL})

    def test_claude_token_found_and_never_mistaken_for_the_link(self):
        self.assertIsNone(sl.find_claude_token(CLAUDE))
        out = f"\x1b[32m✓ Long-lived authentication token created\x1b[0m\nYour OAuth token (valid for 1 year):\n{TOKEN}\nStore this token securely.\n"
        self.assertEqual(sl.find_claude_token(out), TOKEN)

    def test_strip_ansi(self):
        self.assertEqual(sl.strip_ansi("\x1b[1mhi\x1b[0m\r\nthere"), "hi\nthere")

    def test_last_line_hides_tokens(self):
        self.assertEqual(sl.last_line(f"oops\nfailed with {TOKEN}\n"), "failed with [token]")


class EnvFile(unittest.TestCase):
    def test_sets_replaces_and_locks_down(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "agent.env")
        with open(p, "w") as fh:
            fh.write("# note\nSOME_URL=http://127.0.0.1:1\nCLAUDE_CODE_OAUTH_TOKEN=old\n")
        sl.set_env_value(p, "CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
        text = open(p).read()
        self.assertIn("SOME_URL=http://127.0.0.1:1", text)
        self.assertEqual(text.count("CLAUDE_CODE_OAUTH_TOKEN="), 1)
        self.assertIn(TOKEN, text)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)

    def test_refuses_newlines(self):
        with self.assertRaises(ValueError):
            sl.set_env_value("/nonexistent/x", "A", "b\nC=d")


class Flow(unittest.TestCase):
    """Run `swarmctl login` for real, with stand-in programs instead of the CLIs, as a normal user."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, SWARM_LOGIN_DIR=os.path.join(self.tmp, "login"), SWARM_AGENT_ENV_FILE=os.path.join(self.tmp, "agent.env"),
                        SWARM_LOGIN_ALLOW_NON_ROOT="1", SWARM_LOGIN_PROMPT_WAIT="15", HOME=self.tmp, SWARM_LOGIN_SECONDS="60")
        self.addCleanup(self.cancel_all)

    def cancel_all(self):
        for name in sl.PROVIDERS:
            self.ctl(name, "--cancel", check=False)

    def script(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def ctl(self, provider, *args, stdin="", check=True, **extra):
        env = dict(self.env, **extra)
        r = subprocess.run([sys.executable, os.path.join(SERVER, "swarmctl"), "login", provider, *args], input=stdin,
                           capture_output=True, text=True, env=env, timeout=90)
        if check:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_codex_prints_only_link_code_and_expiry(self):
        self.script("codex.txt", CODEX)
        prog = self.script("fake-codex", f"#!/bin/sh\ncat {self.tmp}/codex.txt\nsleep 1\nexit 0\n")
        r = self.ctl("codex", SWARM_LOGIN_FAKE_CODEX=json.dumps([prog]))
        self.assertEqual(r.stdout.splitlines()[:3], ["URL: https://auth.openai.com/codex/device", "CODE: ABCD-EF123", "EXPIRES: 15 minutes"])
        self.assertNotIn("Continue only if", r.stdout)
        st = self.ctl("codex", "--status", "--json")
        for _ in range(20):
            if json.loads(st.stdout)["state"] == "done":
                break
            import time
            time.sleep(0.5)
            st = self.ctl("codex", "--status", "--json")
        self.assertEqual(json.loads(st.stdout)["state"], "done")

    def test_failed_signin_reports_the_reason(self):
        prog = self.script("fake-codex", "#!/bin/sh\necho 'device code login is not enabled for this account'\nexit 3\n")
        r = self.ctl("codex", check=False, SWARM_LOGIN_FAKE_CODEX=json.dumps([prog]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("RESULT: failed", r.stdout)
        self.assertIn("device code login is not enabled", r.stdout)

    def test_claude_code_goes_in_from_stdin_and_token_is_saved_not_printed(self):
        self.script("claude.txt", CLAUDE)
        prog = self.script("fake-claude", f"""#!/bin/sh
cat {self.tmp}/claude.txt
read code
if [ "$code" = "good-code#123" ]; then printf '\\nYour OAuth token:\\n{TOKEN}\\n'; exit 0; fi
echo 'bad code'; exit 1
""")
        fake = {"SWARM_LOGIN_FAKE_CLAUDE": json.dumps([prog])}
        r = self.ctl("claude", **fake)
        self.assertIn("URL: " + URL, r.stdout)
        self.assertIn("--finish", r.stdout)
        r2 = self.ctl("claude", "--finish", stdin="good-code#123\n", **fake)
        self.assertIn("RESULT: done", r2.stdout)
        for out in (r.stdout, r.stderr, r2.stdout, r2.stderr):
            self.assertNotIn(TOKEN, out)
        env_file = os.path.join(self.tmp, "agent.env")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN, open(env_file).read())
        self.assertEqual(stat.S_IMODE(os.stat(env_file).st_mode), 0o640)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "login", "claude", "raw.log")))

    def test_token_from_stdin_is_validated_and_saved(self):
        bad = self.ctl("claude", "--token-stdin", stdin="not-a-token\n", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "agent.env")))
        ok = self.ctl("claude", "--token-stdin", stdin=TOKEN + "\n")
        self.assertNotIn(TOKEN, ok.stdout + ok.stderr)
        self.assertIn(TOKEN, open(os.path.join(self.tmp, "agent.env")).read())

    def test_needs_root_without_the_test_switch(self):
        env = {k: v for k, v in self.env.items() if k != "SWARM_LOGIN_ALLOW_NON_ROOT"}
        if os.getuid() != 0:
            r = subprocess.run([sys.executable, os.path.join(SERVER, "swarmctl"), "login", "codex"], capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("sudo", r.stderr)


if __name__ == "__main__":
    unittest.main()
