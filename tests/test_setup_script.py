"""Tests for scripts/setup-worker.sh and scripts/smoke-test.sh (0.2.1 install-test fixes).

Local tests load the script's functions (SWARM_SETUP_LIB=1) with stub commands and need no root:
release pruning, the Tailscale modes, and the Claude Code install quoting and failure path.

The root tests (lockdown refusal on a root-only box, --admin-user, --harden) make users and write /etc/ssh, so
they run tests/setup_root_check.sh in a throwaway Ubuntu container (Docker), or as root only when
SWARM_TEST_DESTRUCTIVE_OK=1 says this machine is throwaway. Otherwise they skip with a loud message
(SWARM_REQUIRE_USER_TESTS=1 turns the skip into a failure).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.abspath(os.path.join(HERE, ".."))
SETUP = os.path.join(KIT, "scripts", "setup-worker.sh")
SMOKE = os.path.join(KIT, "scripts", "smoke-test.sh")
UBUNTU = os.environ.get("SWARM_TEST_UBUNTU_IMAGE", "ubuntu:24.04")


def bash(script, tmp, stdin="", path=None):
    """Run a bash snippet with the setup script's functions loaded. Returns (exit code, output).
    PATH is only the stubs plus system folders, so tools installed for the person running the tests
    (a real claude in ~/.local/bin, for example) cannot leak in."""
    full = f'set -euo pipefail\nSWARM_SETUP_LIB=1 source "{SETUP}"\nLOG="{tmp}/setup.log"\n' + script
    r = subprocess.run(["bash", "-c", full], input=stdin, capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, PATH=path or f"{tmp}/bin:/usr/sbin:/usr/bin:/sbin:/bin"))
    return r.returncode, r.stdout + r.stderr


def read(path):
    with open(path) as fh:
        return fh.read()


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def stub(tmp, name, body):
    os.makedirs(os.path.join(tmp, "bin"), exist_ok=True)
    path = os.path.join(tmp, "bin", name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)


class PruneReleasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_keeps_current_and_previous_only(self):
        rel = os.path.join(self.tmp, "kit", "releases")
        for name in ("0.1.0-legacy", "0.2.0-20261118165710", "0.2.0-20261118165728", "0.2.1-20261119100000"):
            os.makedirs(os.path.join(rel, name, "server"))
        os.symlink(os.path.join(rel, "0.2.1-20261119100000"), os.path.join(self.tmp, "kit", "current"))
        os.symlink(os.path.join(rel, "0.1.0-legacy"), os.path.join(self.tmp, "kit", "previous"))
        code, out = bash(f'KIT_ROOT="{self.tmp}/kit"\nprune_releases\n', self.tmp)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(os.listdir(rel)), ["0.1.0-legacy", "0.2.1-20261119100000"])
        self.assertIn("Removed old release 0.2.0-20261118165710", out)

    def test_three_reruns_leave_two_releases(self):
        kit = os.path.join(self.tmp, "kit")
        script = f'KIT_ROOT="{kit}"\n'
        for i in range(3):   # what three runs of setup do to the links, then prune
            script += (f'mkdir -p "$KIT_ROOT/releases/0.2.1-{i}"\n'
                       f'[[ -L "$KIT_ROOT/current" ]] && ln -sfn "$(readlink -f "$KIT_ROOT/current")" "$KIT_ROOT/previous"\n'
                       f'ln -sfn "$KIT_ROOT/releases/0.2.1-{i}" "$KIT_ROOT/current"\nprune_releases\n')
        code, out = bash(script, self.tmp)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(os.listdir(os.path.join(kit, "releases"))), ["0.2.1-1", "0.2.1-2"])

    def test_no_current_link_removes_nothing(self):
        rel = os.path.join(self.tmp, "kit", "releases")
        os.makedirs(os.path.join(rel, "0.2.0-a"))
        code, out = bash(f'KIT_ROOT="{self.tmp}/kit"\nprune_releases\n', self.tmp)
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(rel), ["0.2.0-a"])


class TailscaleModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls")
        # Installed, not joined: "status" fails until "up" runs.
        stub(self.tmp, "tailscale", f'echo "$*" >> {self.calls}\n'
                                    f'[ "$1" = status ] && [ ! -e {self.tmp}/joined ] && exit 1\n'
                                    f'[ "$1" = up ] && touch {self.tmp}/joined\nexit 0')

    def calls_made(self):
        return read(self.calls) if os.path.exists(self.calls) else ""

    def test_later_never_waits_for_a_browser(self):
        code, out = bash('parse_args --tailscale later\ntailscale_step\necho "STATE=$TS_STATE TODO=${#INCOMPLETE[@]}"\n', self.tmp)
        self.assertEqual(code, 0, out)
        self.assertNotIn("up", self.calls_made().split())
        self.assertIn("sudo tailscale up --ssh", out)
        self.assertIn("STATE=installed, not joined yet", out)
        self.assertIn("TODO=0", out)   # a choice, not a failure

    def test_now_without_a_terminal_or_key_defers_instead_of_hanging(self):
        code, out = bash("tailscale_step\necho \"STATE=$TS_STATE\"\n", self.tmp, stdin="")
        self.assertEqual(code, 0, out)
        self.assertNotIn("up", self.calls_made().split())
        self.assertIn("joining later", out)

    def test_auth_key_joins_without_a_browser(self):
        key = os.path.join(self.tmp, "ts.key")
        write(key, "not-a-real-key\n")
        code, out = bash(f'parse_args --auth-key "{key}"\ntailscale_step\necho "STATE=$TS_STATE"\n', self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn(f"up --ssh --timeout=15m --auth-key file:{key}", self.calls_made())
        self.assertIn("STATE=joined", out)
        self.assertNotIn("not-a-real-key", out)

    def test_no_tailscale_skips_it_and_says_what_it_costs(self):
        for flags in ("--no-tailscale", "--tailscale off"):
            code, out = bash(f"parse_args {flags}\ntailscale_step\necho \"STATE=$TS_STATE\"\n", self.tmp)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.calls_made(), "")
            self.assertIn("--lockdown will refuse", out)

    def test_bad_mode_is_refused(self):
        code, out = bash("parse_args --tailscale sometime\n", self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("now, later or off", out)

    def test_already_joined_turns_on_ssh(self):
        write(os.path.join(self.tmp, "joined"), "")
        code, out = bash("tailscale_step\necho \"STATE=$TS_STATE\"\n", self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn("set --ssh", self.calls_made())
        self.assertIn("STATE=joined", out)


class ClaudeInstallTest(unittest.TestCase):
    """Defect 1: the installer line must reach the agent's shell unmangled, and a failed install must say so."""
    TOOLS = ("bash", "sh", "env", "mkdir", "chmod", "chown", "cat", "rm", "mktemp", "grep", "sed", "tail", "date",
             "git", "id", "head", "tr", "cut", "awk", "ls", "touch", "dirname", "basename", "readlink")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A PATH with only the basic tools, so a claude installed on the test machine is not found.
        self.sys = os.path.join(self.tmp, "sys")
        os.makedirs(self.sys)
        for t in self.TOOLS:
            if shutil.which(t):
                os.symlink(shutil.which(t), os.path.join(self.sys, t))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        write(os.path.join(self.home, ".profile"), "# test profile\n")
        stub(self.tmp, "gh", "exit 1")   # not logged in

    def run_install(self):
        # Our stand-in for sudo checks the exact form "-u USER -H bash -lc CODE" and runs CODE as this user.
        script = (f'AGENT_HOME="{self.home}"\nWITH_CODEX=0\n'
                  'sudo() { [[ "$1" == -u && "$3" == -H && "$4" == bash && "$5" == -lc ]] || { echo "bad sudo: $*"; return 99; }\n'
                  f'  HOME="{self.home}" bash -lc "$6"; }}\n'
                  'install_model_clis\nprintf "TODO:%s\\n" "${INCOMPLETE[@]}"\n')
        return bash(script, self.tmp, path=f"{self.tmp}/bin:{self.sys}")

    def test_installer_runs_with_its_variables_intact(self):
        # The fake installer: curl writes a script that puts a claude command in ~/.local/bin.
        stub(self.tmp, "curl", 'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                               '[ -n "$out" ] || { echo "curl: option -o: is badly used here" >&2; exit 2; }\n'
                               'cat > "$out" <<\'EOS\'\nmkdir -p "$HOME/.local/bin"\n'
                               'printf \'#!/bin/sh\\necho 9.9.9\\n\' > "$HOME/.local/bin/claude"\n'
                               'chmod 755 "$HOME/.local/bin/claude"\nEOS')
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".local", "bin", "claude")), out)
        self.assertNotIn("TODO:Claude", out)
        self.assertIn('.local/bin', read(os.path.join(self.home, ".profile")))

    def test_failed_installer_is_reported_as_a_failed_install(self):
        stub(self.tmp, "curl", "echo 'curl: (22) The requested URL returned error: 503' >&2; exit 22")
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertIn("TODO:Claude Code did not install: the installer failed", out)
        self.assertNotIn("does not run as", out)   # the 0.2.0 message hid the real cause
        self.assertIn("curl: (22)", read(os.path.join(self.tmp, "setup.log")))

    def test_installed_but_broken_says_so(self):
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        stub(self.tmp, "curl", "exit 0")
        claude = os.path.join(self.home, ".local", "bin", "claude")
        write(claude, "#!/bin/sh\nexit 1\n")
        os.chmod(claude, 0o755)
        code, out = self.run_install()
        self.assertIn("TODO:Claude Code was installed but 'claude --version' does not run", out)


class ScriptTextTest(unittest.TestCase):
    def test_no_sudo_i_with_a_command_line(self):
        # sudo -i escapes the command line; any "sudo -iu USER bash -c" with $ in it breaks (defect 1).
        for path in (SETUP, SMOKE):
            for n, line in enumerate(read(path).splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                self.assertNotRegex(line, r"sudo -iu \"?\$\w+\"? bash", f"{path}:{n}")

    def test_apt_waits_for_the_lock(self):
        # Every apt-get call goes through "${APT[@]}", which carries the lock timeout (defect 7).
        import re
        text = read(SETUP)
        self.assertIn("APT=(apt-get -o DPkg::Lock::Timeout=300", text)
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"^\s*(logged\s+)?apt-get\b|(\|\||&&|;)\s*(logged\s+)?apt-get\b", line):
                self.fail(f"setup-worker.sh:{n} calls apt-get without the lock timeout: {line.strip()}")

    def test_shellcheck_if_available(self):
        files = ["scripts/setup-worker.sh", "scripts/smoke-test.sh", "tests/setup_root_check.sh"]
        if shutil.which("shellcheck"):
            cmd = ["shellcheck"] + [os.path.join(KIT, f) for f in files]
        elif shutil.which("docker") and subprocess.run(["docker", "image", "inspect", "koalaman/shellcheck:stable"],
                                                       capture_output=True).returncode == 0:
            cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{KIT}:/mnt:ro", "koalaman/shellcheck:stable"] + files
        else:
            self.skipTest("shellcheck is not installed (nor the koalaman/shellcheck Docker image)")
        r = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def root_sandbox():
    if os.geteuid() == 0 and os.environ.get("SWARM_TEST_DESTRUCTIVE_OK") == "1":
        return ["bash", os.path.join(HERE, "setup_root_check.sh"), KIT], ""
    if not shutil.which("docker"):
        return None, "docker is not installed"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0:
            return None, "docker is not usable by this user"
        if subprocess.run(["docker", "image", "inspect", UBUNTU], capture_output=True, timeout=30).returncode != 0 and \
                subprocess.run(["docker", "pull", "-q", UBUNTU], capture_output=True, timeout=300).returncode != 0:
            return None, f"could not pull {UBUNTU}"
    except subprocess.TimeoutExpired:
        return None, "docker timed out"
    return ["docker", "run", "--rm", "--network", "none", "-v", f"{KIT}:/kit:ro", UBUNTU,
            "bash", "/kit/tests/setup_root_check.sh", "/kit"], ""


class SetupAsRootTest(unittest.TestCase):
    def test_lockdown_refusal_admin_user_and_harden(self):
        cmd, why = root_sandbox()
        if cmd is None:
            msg = ("\n" + "!" * 78 + "\nSKIPPED: setup-worker.sh root checks (lockdown refusal, --admin-user, --harden):\n"
                   f"  {why}\nRun where Docker works, or as root in a throwaway box with SWARM_TEST_DESTRUCTIVE_OK=1.\n" + "!" * 78)
            print(msg, file=sys.stderr)
            if os.environ.get("SWARM_REQUIRE_USER_TESTS") == "1":
                self.fail(msg)
            self.skipTest(why)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("SETUP ROOT CHECK OK", out)


if __name__ == "__main__":
    unittest.main()
