"""Tests for the zero-touch bootstrap: scripts/render-cloud-init.py (the user-data) and scripts/firstboot.sh (what
runs on the new server), with stand-ins for setup-worker.sh, the smoke test, Tailscale and systemctl."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(KIT, "scripts"))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("render_cloud_init", os.path.join(KIT, "scripts", "render-cloud-init.py"))
rci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rci)

PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake" + " bot"
AUTH = "tskey-" + "auth-kAbCdEfGh1CNTRL-abcdefghijklmnopqrstuv"   # made-up; split so the secret scanner stays quiet


class Render(unittest.TestCase):
    def test_fills_every_placeholder_and_is_valid_yaml_shape(self):
        text = rci.render("ops", PUB, kit_sha256="a" * 64)
        self.assertTrue(text.startswith("#cloud-config"))
        self.assertNotIn("@@", text)
        self.assertIn("- " + PUB, text)
        self.assertIn("ADMIN_USER=ops", text)
        self.assertIn("LOCKDOWN=auto", text)
        self.assertIn('echo "' + "a" * 64 + '  $work/kit.tar.gz" | sha256sum -c -', text)
        with open(os.path.join(KIT, "VERSION")) as fh:
            self.assertIn("archive/refs/tags/v" + fh.read().strip(), text)
        self.assertNotIn("tskey", text)
        self.assertIn("start, --no-block, swarm-firstboot.service", text)

    def test_parses_as_yaml_when_pyyaml_is_there(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        d = yaml.safe_load(rci.render("ops", PUB, AUTH, lockdown="bot", with_claude=False))
        files = {f["path"]: f for f in d["write_files"]}
        self.assertEqual(files["/root/.swarm-tailscale-authkey"]["permissions"], "0600")
        self.assertEqual(files["/root/.swarm-tailscale-authkey"]["content"], AUTH)
        env = files["/etc/agent-swarm-firstboot.env"]
        self.assertEqual(env["permissions"], "0600")
        self.assertIn("AUTH_KEY_FILE=/root/.swarm-tailscale-authkey", env["content"])
        self.assertIn("LOCKDOWN=bot", env["content"])
        self.assertIn("WITH_CLAUDE=0", env["content"])
        self.assertEqual(d["users"][0]["ssh_authorized_keys"], [PUB])
        self.assertEqual(d["users"][0]["sudo"], "ALL=(ALL) NOPASSWD:ALL")

    def test_rejects_anything_that_could_break_out(self):
        for bad in ("Root", "ops; rm -rf /", "root", "agent", "a b", ""):
            with self.assertRaises(rci.Bad, msg=bad):
                rci.render(bad, PUB)
        for bad in ("ssh-ed25519 AAAA\nruncmd: [evil]", "-----BEGIN " + "OPENSSH PRIVATE KEY-----", "ssh-ed25519"):
            with self.assertRaises(rci.Bad, msg=bad):
                rci.render("ops", bad)
        with self.assertRaises(rci.Bad):
            rci.render("ops", PUB, auth_key="tskey-x\n  - path: /etc/evil")
        with self.assertRaises(rci.Bad):
            rci.render("ops", PUB, kit_url="http://example.com/kit.tgz")
        with self.assertRaises(rci.Bad):
            rci.render("ops", PUB, kit_url='https://example.com/kit.tgz"; id; "')
        with self.assertRaises(rci.Bad):
            rci.render("ops", PUB, kit_sha256="zz")
        with self.assertRaises(rci.Bad):
            rci.render("ops", PUB, lockdown="never")

    def test_command_line_never_prints_a_file_with_a_key_and_saves_it_private(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        pub, key, out = (os.path.join(d, n) for n in ("k.pub", "ts", "ud.yaml"))
        open(pub, "w").write(PUB + "\n")
        open(key, "w").write(AUTH + "\n")
        script = os.path.join(KIT, "scripts", "render-cloud-init.py")
        r = subprocess.run([sys.executable, script, "--admin-user", "ops", "--ssh-public-key-file", pub, "--auth-key-file", key],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(AUTH, r.stdout + r.stderr)
        r = subprocess.run([sys.executable, script, "--admin-user", "ops", "--ssh-public-key-file", pub, "--auth-key-file", key, "--out", out],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(AUTH, r.stdout + r.stderr)
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o600")
        self.assertIn(AUTH, open(out).read())


class Firstboot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        os.makedirs(f"{t}/kit/scripts")
        os.makedirs(f"{t}/bin")
        shutil.copy(os.path.join(KIT, "scripts", "firstboot.sh"), f"{t}/kit/scripts/firstboot.sh")
        open(f"{t}/kit/VERSION", "w").write("9.9.9\n")
        self.calls = f"{t}/calls"
        open(self.calls, "w").close()
        self.stub("kit/scripts/setup-worker.sh", 'echo "setup-worker $*" >> "$CALLS"; exit "${SETUP_RC:-0}"')
        self.stub("kit/scripts/setup-secrets.sh", 'echo "setup-secrets $*" >> "$CALLS"')
        self.stub("kit/scripts/smoke-test.sh", 'echo "smoke-test" >> "$CALLS"; echo "smoke-test: 0 failed, 2 warnings"; exit "${SMOKE_RC:-0}"')
        self.stub("bin/systemctl", 'echo "systemctl $*" >> "$CALLS"')
        # Tailscale: "status" and "ip" succeed only when TS_UP=1. "up" prints a login link like the real one.
        self.stub("bin/tailscale", '''case "$1" in
  status) [ "${TS_UP:-0}" = 1 ] ;;
  ip) [ "${TS_UP:-0}" = 1 ] && echo 100.64.0.7 ;;
  up) echo "To authenticate, visit:"; echo; echo "	https://login.tailscale.com/a/abc123XYZ"; sleep 1 ;;
esac''')
        self.env_file = f"{t}/firstboot.env"
        self.state = f"{t}/state"

    def stub(self, rel, body):
        path = os.path.join(self.tmp, rel)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_fb(self, env_text, **extra):
        open(self.env_file, "w").write(env_text)
        env = dict(os.environ, PATH=f"{self.tmp}/bin:" + os.environ["PATH"], CALLS=self.calls, FIRSTBOOT_ENV_FILE=self.env_file,
                   FIRSTBOOT_STATE_DIR=self.state, FIRSTBOOT_LOG=f"{self.tmp}/fb.log", FIRSTBOOT_ALLOW_NON_ROOT="1",
                   FIRSTBOOT_POLL="1", TAILSCALE_WAIT="2")
        env.update(extra)
        r = subprocess.run(["bash", f"{self.tmp}/kit/scripts/firstboot.sh"], capture_output=True, text=True, env=env, timeout=60)
        return r, open(f"{self.state}/status").read(), open(self.calls).read()

    def test_key_join_runs_every_step_and_locks_down(self):
        key = f"{self.tmp}/authkey"
        open(key, "w").write(AUTH)
        r, status, calls = self.run_fb(f"ADMIN_USER=ops\nAUTH_KEY_FILE={key}\nLOCKDOWN=auto\n", TS_UP="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"setup-worker --yes --admin-user ops --auth-key {key}", calls)
        self.assertIn("setup-secrets --prepare", calls)
        self.assertIn("systemctl start swarm-worker", calls)
        self.assertIn("setup-worker --lockdown --yes --admin-user ops", calls)
        order = [calls.index(x) for x in ("setup-worker --yes", "setup-secrets", "systemctl start", "smoke-test", "setup-worker --lockdown")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(status.rstrip().endswith("finished"), status)
        self.assertFalse(os.path.exists(key), "the one-use auth key must be gone from disk")
        self.assertNotIn(AUTH, r.stdout + r.stderr + status)

    def test_bot_mode_leaves_public_ssh_open(self):
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=bot\n", TS_UP="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("--lockdown", calls)
        self.assertIn("left open on purpose", status)
        self.assertIn("setup-worker --yes --admin-user ops --tailscale later", calls)

    def test_without_a_key_it_saves_the_login_link_and_does_not_lock_down_until_joined(self):
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=auto\n", TS_UP="0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(open(f"{self.state}/tailscale-login-url").read().strip(), "https://login.tailscale.com/a/abc123XYZ")
        self.assertIn("waiting for approval", status)
        self.assertNotIn("--lockdown", calls)
        self.assertIn("skipped: Tailscale never connected", status)

    def test_failed_smoke_test_stops_before_lockdown(self):
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=auto\n", TS_UP="1", SMOKE_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("--lockdown", calls)
        self.assertIn("stopped smoke test failed", status)

    def test_setup_exit_3_is_fine_but_exit_1_stops(self):
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=bot\n", TS_UP="1", SETUP_RC="3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        open(self.calls, "w").close()
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=bot\n", TS_UP="1", SETUP_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("smoke-test", calls)
        self.assertIn("stopped setup-worker.sh exited 1", status)

    def test_login_url_parser(self):
        out = subprocess.run(["bash", "-c", 'FIRSTBOOT_LIB=1 source "$1"; login_url "$2"', "_", f"{KIT}/scripts/firstboot.sh", "/dev/stdin"],
                             input="noise\n\thttps://login.tailscale.com/a/1a2b3c4d5e6f\nmore\n", capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "https://login.tailscale.com/a/1a2b3c4d5e6f")


class SetupFlags(unittest.TestCase):
    def test_yes_answers_questions_and_lockdown_still_needs_a_tailnet_address(self):
        setup = os.path.join(KIT, "scripts", "setup-worker.sh")
        r = subprocess.run(["bash", "-c", f'SWARM_SETUP_LIB=1 source "{setup}"; ASSUME_YES=1; confirm "ok?" && echo yes-said'],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertIn("yes-said", r.stdout)
        r = subprocess.run(["bash", "-c", f'SWARM_SETUP_LIB=1 source "{setup}"; confirm "ok?" && echo yes-said'],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertNotIn("yes-said", r.stdout)
        self.assertIn("--yes", subprocess.run(["bash", setup, "--help"], capture_output=True, text=True).stdout)


if __name__ == "__main__":
    unittest.main()
