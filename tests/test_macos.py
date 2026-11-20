"""Tests for the macOS worker path that can run on Linux: the setup script (with a stand-in launchctl and no Homebrew),
the launchd plists, the launchd wrapper, the vm_stat and PATH helpers, and a real single-user run (backend, runner,
one script task) started through the same wrapper launchd uses. What cannot be tested here: launchctl itself,
Homebrew, the real Claude and Codex installers, vm_stat on a real Mac. See docs/macos-worker.md."""
import importlib.machinery
import importlib.util
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.abspath(os.path.join(HERE, ".."))
SETUP = os.path.join(KIT, "scripts", "setup-macos.sh")
sys.path.insert(0, os.path.join(KIT, "server"))
import swarm_worker as sw  # noqa: E402

loader = importlib.machinery.SourceFileLoader("agent_exec", os.path.join(KIT, "server", "agent-exec"))
spec = importlib.util.spec_from_loader("agent_exec", loader)
ax = importlib.util.module_from_spec(spec)
loader.exec_module(ax)

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           50000.
Pages speculative:                         2000.
Pages throttled:                              0.
Pages wired down:                         90000.
"""


class Helpers(unittest.TestCase):
    def test_vm_stat_available_memory(self):
        self.assertEqual(sw.parse_vm_stat(VM_STAT), (10000 + 50000 + 2000) * 16384 // (1024 * 1024))

    def test_vm_stat_garbage_is_unknown_not_zero(self):
        self.assertIsNone(sw.parse_vm_stat("nothing useful"))
        self.assertIsNone(sw.parse_vm_stat("page size of 4096 bytes\nPages free: 5.\n"))

    def test_linux_path_is_unchanged_and_mac_path_gets_homebrew_first(self):
        base = "/h/.local/bin:/h/bin:/usr/local/bin:/usr/bin:/bin"
        self.assertEqual(ax.task_path("/h", "linux"), base)
        self.assertEqual(ax.task_path("/h", "darwin", lambda d: True).split(":")[:2],
                         ["/opt/homebrew/opt/coreutils/libexec/gnubin", "/opt/homebrew/bin"])
        self.assertEqual(ax.task_path("/h", "darwin", lambda d: False), base)

    def test_timeout_falls_back_to_gtimeout_only_when_needed(self):
        self.assertEqual(ax.timeout_command("p", lambda n, path=None: "/x/" + n), "timeout")
        self.assertEqual(ax.timeout_command("p", lambda n, path=None: "/x/gtimeout" if n == "gtimeout" else None), "gtimeout")
        self.assertEqual(ax.timeout_command("p", lambda n, path=None: None), "timeout")


class SetupScript(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        os.makedirs(f"{self.home}/bin")
        self.calls = f"{self.home}/launchctl.calls"
        with open(f"{self.home}/bin/launchctl", "w") as fh:
            fh.write(f'#!/bin/sh\necho "$*" >> {self.calls}\n')
        os.chmod(f"{self.home}/bin/launchctl", 0o755)
        self.env = dict(os.environ, HOME=self.home, SWARM_MAC_FORCE="1", PATH=f"{self.home}/bin:" + os.environ["PATH"])

    def run_setup(self, *args, check=True):
        r = subprocess.run(["bash", SETUP, *args], capture_output=True, text=True, env=self.env, timeout=120)
        if check:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def mode(self, rel):
        return oct(os.stat(os.path.join(self.home, rel)).st_mode & 0o777)

    def test_with_backend_makes_private_files_and_four_valid_agents(self):
        self.run_setup("--with-backend")
        for rel in (".config/agent-swarm/swarm.env", ".config/agent-swarm/agent.env", ".config/agent-swarm/worker.token",
                    ".config/agent-swarm/admin.token", ".config/swarm-secrets/age.key"):
            self.assertEqual(self.mode(rel), "0o600", rel)
        agents = f"{self.home}/Library/LaunchAgents"
        wrapper = f"{self.home}/.local/lib/agent-swarm/swarm-launchd"
        plists = {}
        for name in ("worker", "sync", "cleanup", "backend"):
            with open(f"{agents}/com.agent-swarm-kit.{name}.plist", "rb") as fh:
                plists[name] = plistlib.load(fh)
            self.assertEqual(plists[name]["Label"], f"com.agent-swarm-kit.{name}")
            self.assertEqual(plists[name]["ProgramArguments"], [wrapper, name])
            self.assertTrue(plists[name]["AbandonProcessGroup"])
        self.assertTrue(plists["worker"]["KeepAlive"])
        self.assertEqual(plists["sync"]["StartInterval"], 600)
        self.assertEqual(plists["cleanup"]["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        with open(f"{self.home}/.config/agent-swarm/swarm.env") as fh:
            env = fh.read()
        self.assertIn("SWARM_TASK_SCOPE=0", env)
        self.assertIn("SWARM_BIND=127.0.0.1:8750", env)
        self.assertNotIn("SWARM_AGENT_USER", env)   # single-user mode
        self.assertTrue(os.path.islink(f"{self.home}/.local/bin/swarm-sync-secrets"))
        self.assertEqual(os.path.basename(os.readlink(f"{self.home}/.local/bin/swarm-sync-secrets")), "with-secrets")
        with open(self.calls) as fh:
            calls = fh.read()
        self.assertEqual(calls.count("bootstrap"), 4)

    def test_running_again_keeps_tokens_and_settings(self):
        self.run_setup("--with-backend")
        path = f"{self.home}/.config/agent-swarm"
        with open(f"{path}/worker.token") as fh:
            before = fh.read()
        with open(f"{path}/swarm.env", "a") as fh:
            fh.write("SWARM_MAX_PARALLEL=3\n")
        time.sleep(1.1)   # release folders are named by the second
        self.run_setup("--with-backend")
        with open(f"{path}/worker.token") as fh:
            self.assertEqual(fh.read(), before)
        with open(f"{path}/swarm.env") as fh:
            self.assertIn("SWARM_MAX_PARALLEL=3", fh.read())
        self.assertEqual(len(os.listdir(f"{self.home}/.local/share/agent-swarm-kit/releases")), 2)

    def test_second_worker_needs_url_and_token_and_copies_the_token_privately(self):
        bad = self.run_setup(check=False)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("--backend-url", bad.stderr)
        tok = f"{self.home}/given.token"
        with open(tok, "w") as fh:
            fh.write("SECRET-TOKEN-VALUE")
        r = self.run_setup("--backend-url", "https://box.example.ts.net", "--worker-token-file", tok, "--worker-name", "spare-mac")
        self.assertNotIn("SECRET-TOKEN-VALUE", r.stdout + r.stderr)
        self.assertEqual(self.mode(".config/agent-swarm/worker.token"), "0o600")
        with open(f"{self.home}/.config/agent-swarm/swarm.env") as fh:
            env = fh.read()
        self.assertIn("SWARM_URL=https://box.example.ts.net", env)
        self.assertIn("SWARM_WORKER_NAME=spare-mac", env)
        self.assertNotIn("SWARM_BIND", env)
        self.assertFalse(os.path.exists(f"{self.home}/Library/LaunchAgents/com.agent-swarm-kit.backend.plist"))

    def test_refuses_a_plain_http_backend_address_and_odd_names(self):
        tok = f"{self.home}/t"
        open(tok, "w").close()
        self.assertEqual(self.run_setup("--backend-url", "http://box.example.com", "--worker-token-file", tok, check=False).returncode, 1)
        self.assertEqual(self.run_setup("--with-backend", "--worker-name", "a b;c", check=False).returncode, 1)

    def test_uninstall_removes_agents_and_keeps_data(self):
        self.run_setup("--with-backend")
        self.run_setup("--uninstall")
        self.assertEqual(os.listdir(f"{self.home}/Library/LaunchAgents"), [])
        self.assertTrue(os.path.exists(f"{self.home}/.config/agent-swarm/worker.token"))

    def test_refuses_on_linux_without_the_test_switch(self):
        env = {k: v for k, v in self.env.items() if k != "SWARM_MAC_FORCE"}
        r = subprocess.run(["bash", SETUP, "--with-backend"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("macOS", r.stderr)


class SingleUserRun(unittest.TestCase):
    """The Mac path for real, on Linux: backend and runner started through swarm-launchd, one script task to done."""

    def test_backend_runner_and_a_script_task(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(f"{home}/bin")
        with open(f"{home}/bin/launchctl", "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(f"{home}/bin/launchctl", 0o755)
        env = dict(os.environ, HOME=home, SWARM_MAC_FORCE="1", PATH=f"{home}/bin:" + os.environ["PATH"])
        for k in [k for k in env if k.startswith("SWARM_") and k != "SWARM_MAC_FORCE"]:
            del env[k]
        subprocess.run(["bash", SETUP, "--with-backend"], env=env, check=True, capture_output=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        conf = f"{home}/.config/agent-swarm/swarm.env"
        with open(conf) as fh:
            text = fh.read().replace("127.0.0.1:8750", f"127.0.0.1:{port}")
        with open(conf, "w") as fh:
            fh.write(text)
        with open(f"{home}/.config/swarm/client.env") as fh:
            client = fh.read().replace("8750", str(port))
        with open(f"{home}/.config/swarm/client.env", "w") as fh:
            fh.write(client)
        wrapper = f"{home}/.local/lib/agent-swarm/swarm-launchd"
        env["SWARM_POLL_SECONDS"] = "1"
        procs = [subprocess.Popen([wrapper, role], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for role in ("backend", "worker")]
        self.addCleanup(lambda: [(p.terminate(), p.wait(timeout=20)) for p in procs])
        ctl = f"{home}/.local/bin/swarmctl"
        r = subprocess.run([ctl, "enqueue", "--command", "echo ok-from-single-user", "--key", "mac-test", "--title", "t", "-"],
                           input="x", capture_output=True, text=True, env=env)
        for _ in range(30):
            if r.returncode == 0:
                break
            time.sleep(1)
            r = subprocess.run([ctl, "enqueue", "--command", "echo ok-from-single-user", "--key", "mac-test", "--title", "t", "-"],
                               input="x", capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tid = r.stdout.split()[0]
        state = ""
        for _ in range(60):
            out = subprocess.run([ctl, "list"], capture_output=True, text=True, env=env).stdout
            line = [ln for ln in out.splitlines() if ln.startswith(tid)]
            state = line[0].split()[1] if line else ""
            if state in ("done", "failed"):
                break
            time.sleep(1)
        self.assertEqual(state, "done", out)


class Wrapper(unittest.TestCase):
    def test_wrapper_loads_settings_puts_gnu_tools_first_and_syncs_before_the_runner(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        for d in (".config/agent-swarm", ".local/bin", "kit/server"):
            os.makedirs(f"{home}/{d}")
        with open(f"{home}/.config/agent-swarm/swarm.env", "w") as fh:
            fh.write("SWARM_MARKER=from-env-file\n")
        log = f"{home}/log"
        with open(f"{home}/.local/bin/swarm-sync-secrets", "w") as fh:
            fh.write(f'#!/bin/sh\necho "sync $*" >> {log}\n')
        with open(f"{home}/fakepy", "w") as fh:
            fh.write(f'#!/bin/sh\necho "py $* marker=$SWARM_MARKER path=$PATH" >> {log}\n')
        for f in (f"{home}/.local/bin/swarm-sync-secrets", f"{home}/fakepy"):
            os.chmod(f, 0o755)
        env = dict(os.environ, HOME=home, SWARM_MAC_KIT=f"{home}/kit", SWARM_PYTHON=f"{home}/fakepy")
        subprocess.run([os.path.join(KIT, "server", "macos", "swarm-launchd"), "worker"], env=env, check=True)
        with open(log) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0], "sync --if-configured")
        self.assertIn("swarm_worker.py marker=from-env-file", lines[1])
        self.assertIn("path=" + home + "/.local/bin:/opt/homebrew/opt/coreutils/libexec/gnubin:/opt/homebrew/bin", lines[1])

    def test_wrapper_stops_when_the_secrets_sync_fails(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        for d in (".config/agent-swarm", ".local/bin"):
            os.makedirs(f"{home}/{d}")
        open(f"{home}/.config/agent-swarm/swarm.env", "w").close()
        with open(f"{home}/.local/bin/swarm-sync-secrets", "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(f"{home}/.local/bin/swarm-sync-secrets", 0o755)
        r = subprocess.run([os.path.join(KIT, "server", "macos", "swarm-launchd"), "worker"],
                           env=dict(os.environ, HOME=home, SWARM_PYTHON="/bin/false"))
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
