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
import time
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
        # chown of the temp .profile needs root unless it names the caller's own user and group. Record it instead,
        # so these tests pass for any user (install test 4, defect 5: they failed for users other than agent).
        os.remove(os.path.join(self.sys, "chown"))
        write(os.path.join(self.sys, "chown"), f'#!/bin/sh\necho "$@" >> "{self.tmp}/chown.calls"\n')
        os.chmod(os.path.join(self.sys, "chown"), 0o755)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        write(os.path.join(self.home, ".profile"), "# test profile\n")
        stub(self.tmp, "gh", "exit 1")   # not logged in

    def run_install(self):
        # Our stand-in for sudo checks the exact form "-u USER -H bash -lc CODE" and runs CODE as this user.
        # AGENT_USER is the user running the tests, so the installer's chown of the temp .profile works for any
        # user, not only for "agent" or root (install test 4, defect 5).
        script = (f'AGENT_HOME="{self.home}"\nAGENT_USER="$(id -un)"\nWITH_CODEX=0\n'
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
        user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(read(os.path.join(self.tmp, "chown.calls")).split()[0], f"{user}:{user}")

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


class RootPasswordTest(unittest.TestCase):
    """Install test 5, defect 1: stock DigitalOcean Ubuntu 24.04 marks root's password "must be changed", so every
    sudo -u agent failed during first boot. Setup clears that flag (and only that flag) before anything uses sudo."""
    LOCKED = "root:*:{lastchg}:0:99999:7:::\ndaemon:*:19000:0:99999:7:::\n"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls")
        write(self.calls, "")
        for name in ("chage", "passwd", "usermod", "chpasswd"):
            stub(self.tmp, name, f'echo "{name} $*" >> "{self.calls}"')

    def run_clear(self, lastchg, yes=True, stdin=""):
        shadow = os.path.join(self.tmp, "shadow")
        write(shadow, self.LOCKED.format(lastchg=lastchg))
        script = (f'SHADOW_FILE="{shadow}"\nASSUME_YES={1 if yes else 0}\n'
                  'clear_root_forced_password_change\nprintf "TODO:%s\\n" "${INCOMPLETE[@]}"\n')
        return bash(script, self.tmp, stdin=stdin)

    def test_forced_change_is_cleared_with_yes_and_no_password_is_set(self):
        code, out = self.run_clear(0)
        self.assertEqual(code, 0, out)
        calls = read(self.calls).splitlines()
        self.assertEqual(len(calls), 1, calls)
        self.assertRegex(calls[0], r"^chage -d \d{4}-\d{2}-\d{2} root$")
        self.assertIn("no password is set", out)
        self.assertNotIn("TODO:root", out)

    def test_normal_root_account_is_left_alone(self):
        for lastchg in ("19500", ""):
            code, out = self.run_clear(lastchg)
            self.assertEqual(code, 0, out)
            self.assertEqual(read(self.calls), "", lastchg)
            self.assertNotIn("must be changed", out)

    def test_missing_shadow_file_is_left_alone(self):
        code, out = bash(f'SHADOW_FILE="{self.tmp}/none"\nclear_root_forced_password_change\necho done', self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn("done", out)
        self.assertEqual(read(self.calls), "")

    def test_interactive_no_leaves_it_and_says_how_to_fix(self):
        code, out = self.run_clear(0, yes=False, stdin="no\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(read(self.calls), "")
        self.assertIn("TODO:root's password must be changed", out)
        self.assertIn("chage -d $(date +%F) root", out)

    def test_runs_before_anything_uses_sudo(self):
        text = read(SETUP)
        main = text[text.index("\nmain() {"):]
        self.assertLess(main.index("clear_root_forced_password_change"), main.index("create_admin"))
        self.assertLess(main.index("clear_root_forced_password_change"), main.index("install_model_clis"))
        # Not on the --harden or --lockdown only paths: they never run sudo -u.
        self.assertLess(main.index('"$ONLY" == harden'), main.index("clear_root_forced_password_change"))


class AptWaitTest(unittest.TestCase):
    """Round 3, defect A: wait for first-boot setup and other apt runs, retry on a lock, and never hang."""
    FAST = ('APT_POLL=0.2\nAPT_PROGRESS=0\nAPT_RETRY_PAUSE=0.1\nAPT_CI_TIMEOUT=0.5\n')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.busy = os.path.join(self.tmp, "busy")
        self.calls = os.path.join(self.tmp, "apt-calls")
        stub(self.tmp, "pgrep", "exit 1")   # no other apt process on this machine, as far as the test is concerned
        # A fake fuser: a lock is held while the "busy" file exists.
        stub(self.tmp, "fuser", f'[ -e {self.busy} ] && echo " 4242" && exit 0\nexit 1')
        stub(self.tmp, "cloud-init", "exit 0")   # first-boot setup finished (this machine may have a real one)

    def run_sh(self, body, timeout=60):
        script = f'APT_LOCKS=("{self.tmp}/lock")\n' + self.FAST + body
        full = f'set -euo pipefail\nSWARM_SETUP_LIB=1 source "{SETUP}"\nLOG="{self.tmp}/setup.log"\n' + script
        r = subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, PATH=f"{self.tmp}/bin:/usr/sbin:/usr/bin:/sbin:/bin"))
        return r.returncode, r.stdout + r.stderr

    def apt_stub(self, fail_times, message="E: Could not get lock /var/lib/apt/lists/lock. It is held by process 4242 (apt-get)"):
        counter = os.path.join(self.tmp, "n")
        stub(self.tmp, "apt-get", f'echo "$*" >> {self.calls}\nn=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n'
                                  f'if [ $n -le {fail_times} ]; then echo "{message}" >&2; exit 100; fi\nexit 0')

    def apt_calls(self):
        return read(self.calls).splitlines() if os.path.exists(self.calls) else []

    def test_waits_while_a_lock_is_held_then_goes_on(self):
        write(self.busy, "")
        code, out = self.run_sh(f'(sleep 1.5; rm -f {self.busy}) &\nAPT_WAIT_SECONDS=30\nwait_for_apt\necho FREE\n')
        self.assertEqual(code, 0, out)
        self.assertIn("Waiting for another package install", out)
        self.assertIn("4242", out)
        self.assertIn("FREE", out)

    def test_stuck_lock_fails_clearly_and_does_not_hang(self):
        write(self.busy, "")
        self.apt_stub(0)
        code, out = self.run_sh('APT_WAIT_SECONDS=2\nif apt_run update; then echo RAN; else echo "FAILED rc=$?"; fi\n', timeout=30)
        self.assertEqual(code, 0, out)
        self.assertIn("Gave up after", out)
        self.assertIn("FAILED rc=1", out)
        self.assertEqual(self.apt_calls(), [])   # never ran apt while the lock was held

    def test_retries_when_apt_hits_a_lock(self):
        self.apt_stub(2)
        code, out = self.run_sh('APT_WAIT_SECONDS=5\napt_run update\necho OK\n')
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.apt_calls()), 3)
        self.assertIn("trying again", out)
        self.assertIn("OK", out)
        self.assertIn("Could not get lock", read(os.path.join(self.tmp, "setup.log")))

    def test_gives_up_after_the_tries_with_a_clear_message(self):
        self.apt_stub(99)
        code, out = self.run_sh('APT_WAIT_SECONDS=5\nAPT_TRIES=3\nif apt_run install -y ufw; then echo RAN; else echo "FAILED rc=$?"; fi\n')
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.apt_calls()), 3)
        self.assertIn("apt-get install failed (exit 100)", out)
        self.assertIn("FAILED rc=100", out)

    def test_a_real_error_is_not_retried(self):
        self.apt_stub(99, message="E: Unable to locate package nosuchpackage")
        code, out = self.run_sh('if apt_run install -y nosuchpackage; then echo RAN; else echo "FAILED rc=$?"; fi\n')
        self.assertEqual(len(self.apt_calls()), 1)
        self.assertIn("FAILED rc=100", out)

    def test_waits_for_cloud_init(self):
        running = os.path.join(self.tmp, "ci-running")
        write(running, "")
        stub(self.tmp, "cloud-init", f'while [ -e {running} ]; do sleep 0.1; done\nexit 0')
        self.apt_stub(0)
        code, out = self.run_sh(f'(sleep 1.5; rm -f {running}) &\nAPT_WAIT_SECONDS=30\napt_run update\necho OK\n')
        self.assertEqual(code, 0, out)
        self.assertIn("first-boot setup (cloud-init)", out)
        self.assertEqual(len(self.apt_calls()), 1)

    def test_real_open_lock_file_is_seen(self):
        if not shutil.which("fuser") and not shutil.which("lsof"):
            self.skipTest("neither fuser nor lsof here (the /proc fallback is used on such servers)")
        os.remove(os.path.join(self.tmp, "bin", "fuser"))   # use the real fuser (or lsof) on a real open file
        lock = os.path.join(self.tmp, "lock")
        holder = subprocess.Popen(["bash", "-c", f'exec 9>"{lock}"; sleep 2'])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        time.sleep(0.3)
        code, out = self.run_sh('APT_WAIT_SECONDS=20\nwait_for_apt\necho FREE\n')
        holder.wait()
        self.assertEqual(code, 0, out)
        self.assertIn(str(holder.pid), out)
        self.assertIn("FREE", out)

    def test_proc_fallback_finds_the_holder(self):
        lock = os.path.join(self.tmp, "lock")
        holder = subprocess.Popen(["bash", "-c", f'exec 9>"{lock}"; sleep 5'])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        time.sleep(0.3)
        # Hide fuser and lsof by giving a PATH without them.
        sysbin = os.path.join(self.tmp, "sys")
        os.makedirs(sysbin)
        for t in ("bash", "readlink", "sort", "tr", "grep", "cat", "sed", "date", "mktemp", "rm", "dirname"):
            if shutil.which(t):
                os.symlink(shutil.which(t), os.path.join(sysbin, t))
        full = (f'set -euo pipefail\nSWARM_SETUP_LIB=1 source "{SETUP}"\nAPT_LOCKS=("{lock}")\nlock_holders\n')
        r = subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=30, env=dict(os.environ, PATH=sysbin))
        self.assertEqual(r.stdout.split(), [str(holder.pid)], r.stderr)


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
        self.assertIn("APT=(apt-get -o DPkg::Lock::Timeout=", text)
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"^\s*(logged\s+)?apt-get\b|(\|\||&&|;)\s*(logged\s+)?apt-get\b", line):
                self.fail(f"setup-worker.sh:{n} calls apt-get without the lock timeout: {line.strip()}")

    def test_env_example_shows_worker_name_and_projects_without_setting_them(self):
        # Install test 5, defect 3: both are needed to pin work to a worker, and were only in docs/multi-worker.md.
        # Commented out, so a new install still uses its hostname and every project, and old installs are unchanged.
        lines = read(os.path.join(KIT, "config", "swarm.env.example")).splitlines()
        for name in ("SWARM_WORKER_NAME", "SWARM_WORKER_PROJECTS"):
            self.assertTrue(any(ln.startswith(f"# {name}=") for ln in lines), name)
            self.assertFalse(any(ln.startswith(f"{name}=") for ln in lines), f"{name} must stay commented out")
            i = next(n for n, ln in enumerate(lines) if ln.startswith(f"# {name}="))
            self.assertIn("docs/multi-worker.md", lines[i - 1] + lines[i - 2], name)

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
        if r.returncode == 2 and "not a fresh root-only box" in out:
            # Destructive mode on a box that already has an admin: the checks cannot mean anything there, and the
            # script changed nothing (install test 4, caveat B).
            if os.environ.get("SWARM_REQUIRE_USER_TESTS") == "1":
                self.fail(out)
            self.skipTest(out.strip().splitlines()[0])
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("SETUP ROOT CHECK OK", out)


if __name__ == "__main__":
    unittest.main()
