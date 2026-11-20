"""Tests for the zero-touch bootstrap: scripts/render-cloud-init.py (the user-data) and scripts/firstboot.sh (what
runs on the new server), with stand-ins for setup-worker.sh, the smoke test, Tailscale and systemctl."""
import hashlib
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
        self.assertIn('"' + rci.default_kit_url()[0] + '"', text)
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


def load_render(path):
    spec = importlib.util.spec_from_file_location("render_copy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DefaultKitUrl(unittest.TestCase):
    """Install test 5, defect 4: the default download was the v0.6.0 tag, which was never made, so first boot got a
    404. The tag is used only when the checkout has it; otherwise this commit, or the main branch. No network."""
    SHA = "7f0b34cff21bb45d8776b74fcbb6335dc6fbc688"

    def fake_git(self, tags=(), head=SHA):
        def run(*args):
            if args[:3] == ("rev-parse", "-q", "--verify"):
                name = args[3][len("refs/tags/"):-len("^{commit}")]
                return self.SHA if name in tags else None
            if args == ("rev-parse", "HEAD"):
                return head
            raise AssertionError(f"unexpected git call {args}")
        return run

    def check_url(self, url):
        self.assertTrue(rci.URL_RE.match(url), url)
        self.assertTrue(url.startswith("https://github.com/giltotherescue/agent-swarm-kit/archive/"), url)
        self.assertTrue(url.endswith(".tar.gz"), url)

    def test_release_tag_when_the_checkout_has_it(self):
        url, why = rci.default_kit_url("0.6.0", self.fake_git(tags=("v0.6.0",)))
        self.assertEqual(url, "https://github.com/giltotherescue/agent-swarm-kit/archive/refs/tags/v0.6.0.tar.gz")
        self.assertIn("release tag v0.6.0", why)
        self.check_url(url)

    def test_missing_tag_falls_back_to_this_commit(self):
        url, why = rci.default_kit_url("0.6.0", self.fake_git(tags=("v0.5.0",)))
        self.assertEqual(url, f"https://github.com/giltotherescue/agent-swarm-kit/archive/{self.SHA}.tar.gz")
        self.assertNotIn("tags/v0.6.0", url)
        self.assertIn("no v0.6.0 tag", why)
        self.check_url(url)

    def test_no_git_falls_back_to_the_main_branch(self):
        for head in (None, "", "not-a-sha"):
            url, why = rci.default_kit_url("0.6.0", self.fake_git(head=head))
            self.assertEqual(url, "https://github.com/giltotherescue/agent-swarm-kit/archive/refs/heads/main.tar.gz")
            self.check_url(url)
        url, _ = rci.default_kit_url("0.6.0", lambda *a: None)
        self.check_url(url)

    def test_default_in_this_checkout_is_well_formed(self):
        url, why = rci.default_kit_url()
        self.check_url(url)
        self.assertIn(url, rci.render("ops", PUB))

    def test_a_copy_outside_git_or_inside_another_repo_uses_the_branch(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        kit = os.path.join(d, "vendor", "kit")
        shutil.copytree(os.path.join(KIT, "scripts"), os.path.join(kit, "scripts"))
        shutil.copy(os.path.join(KIT, "VERSION"), kit)
        mod = load_render(os.path.join(kit, "scripts", "render-cloud-init.py"))
        self.assertEqual(mod.default_kit_url()[0], mod.BRANCH_URL)
        if not shutil.which("git"):
            self.skipTest("git not installed")
        # The kit unpacked inside someone else's repository: that repository's commit is not a kit commit.
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", d, *cmd], check=True, capture_output=True, env=env)
        self.assertIsNone(mod.git("rev-parse", "HEAD"))
        self.assertEqual(mod.default_kit_url()[0], mod.BRANCH_URL)

    def test_command_line_says_which_default_it_used(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        pub = os.path.join(d, "k.pub")
        with open(pub, "w") as fh:
            fh.write(PUB + "\n")
        script = os.path.join(KIT, "scripts", "render-cloud-init.py")
        r = subprocess.run([sys.executable, script, "--admin-user", "ops", "--ssh-public-key-file", pub],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kit download https://", r.stderr)
        self.assertNotIn("kit download", r.stdout)
        r = subprocess.run([sys.executable, script, "--admin-user", "ops", "--ssh-public-key-file", pub,
                            "--kit-url", "https://example.com/kit.tar.gz"], capture_output=True, text=True)
        self.assertEqual(r.stderr, "")
        self.assertIn('"https://example.com/kit.tar.gz"', r.stdout)


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
        self.units = f"{t}/units"
        os.makedirs(self.units)

    def stub(self, rel, body):
        path = os.path.join(self.tmp, rel)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_fb(self, env_text, *args, **extra):
        open(self.env_file, "w").write(env_text)
        env = dict(os.environ, PATH=f"{self.tmp}/bin:" + os.environ["PATH"], CALLS=self.calls, FIRSTBOOT_ENV_FILE=self.env_file,
                   FIRSTBOOT_STATE_DIR=self.state, FIRSTBOOT_LOG=f"{self.tmp}/fb.log", FIRSTBOOT_ALLOW_NON_ROOT="1",
                   FIRSTBOOT_POLL="1", TAILSCALE_WAIT="2", FIRSTBOOT_UNIT_DIR=self.units)
        env.update(extra)
        r = subprocess.run(["bash", f"{self.tmp}/kit/scripts/firstboot.sh", *args], capture_output=True, text=True, env=env, timeout=60)
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

    def unit(self):
        return os.path.join(self.units, "swarm-firstboot-resume.service")

    def test_resume_unit_is_removed_when_firstboot_ends(self):
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=bot\n", TS_UP="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("systemctl enable swarm-firstboot-resume.service", calls)
        self.assertIn("systemctl disable swarm-firstboot-resume.service", calls)
        self.assertFalse(os.path.exists(self.unit()))
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=bot\n", TS_UP="1", SETUP_RC="1")
        self.assertFalse(os.path.exists(self.unit()), "a stopped run removes the unit too")

    def interrupted(self, phase, status_text):
        """Leave the state a reboot leaves: unit installed, status without an end, the phase it was in."""
        os.makedirs(self.state, exist_ok=True)
        open(f"{self.state}/status", "w").write(status_text)
        open(f"{self.state}/phase", "w").write(phase + "\n")
        open(self.unit(), "w").write("[Unit]\n")

    def test_reboot_during_the_tailscale_wait_resumes_the_wait_and_locks_down(self):
        self.interrupted("tailscale-wait", "started kit 9.9.9\nsetup ok\ntailscale waiting for approval\nsmoke ok\n")
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=auto\n", "--resume", TS_UP="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("resumed after a reboot during the Tailscale wait", status)
        self.assertIn("setup-worker --lockdown --yes --admin-user ops", calls)
        self.assertNotIn("setup-worker --yes", calls, "a resume does not run setup again")
        self.assertTrue(status.startswith("started kit 9.9.9"), "a resume keeps the earlier lines")
        self.assertTrue(status.rstrip().endswith("finished"), status)
        self.assertFalse(os.path.exists(self.unit()))

    def test_resume_still_not_joined_asks_for_a_new_link_and_ends(self):
        self.interrupted("tailscale-wait", "setup ok\n")
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=auto\n", "--resume", TS_UP="0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(open(f"{self.state}/tailscale-login-url").read().strip(), "https://login.tailscale.com/a/abc123XYZ")
        self.assertIn("skipped: Tailscale never connected", status)
        self.assertTrue(status.rstrip().endswith("finished"), status)

    def test_reboot_during_another_step_writes_stopped(self):
        self.interrupted("smoke", "setup ok\nworker started\n")
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=auto\n", "--resume", TS_UP="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("stopped a reboot interrupted first boot during the smoke step", status.splitlines()[-1])
        self.assertNotIn("setup-worker", calls)
        self.assertFalse(os.path.exists(self.unit()))

    def test_resume_after_a_finished_run_does_nothing(self):
        self.interrupted("finished", "setup ok\nfinished\n")
        r, status, calls = self.run_fb("ADMIN_USER=ops\nLOCKDOWN=auto\n", "--resume", TS_UP="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(status, "setup ok\nfinished\n")
        self.assertNotIn("setup-worker", calls)

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


class SopsChecksum(unittest.TestCase):
    """Install test 5, defect 2: the SOPS checksum line was rewritten as "HASH   sops" (three spaces), so
    sha256sum -c looked for a file named " sops" and every fresh install failed to get SOPS."""
    SECRETS = os.path.join(KIT, "scripts", "setup-secrets.sh")
    VERSION = "3.10.2"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dl = os.path.join(self.tmp, "dl")
        os.makedirs(self.dl)
        self.binary = b"#!/bin/sh\necho 'sops stand-in'\n"
        with open(os.path.join(self.dl, "sops"), "wb") as fh:
            fh.write(self.binary)
        good = hashlib.sha256(self.binary).hexdigest()
        other = hashlib.sha256(b"something else").hexdigest()
        v = self.VERSION
        # Same shape as the published sops-vX.checksums.txt: GNU coreutils output, two spaces before the name.
        self.sums = os.path.join(self.dl, "sums")
        with open(self.sums, "w") as fh:
            fh.write(f"{other}  sops-v{v}.darwin.amd64\n{other}  sops-v{v}.darwin.arm64\n"
                     f"{other}  sops-v{v}.linux.arm64\n{good}  sops-v{v}.linux.amd64\n"
                     f"{other}  sops-v{v}.linux.amd64.sbom.json\n{other}  sops-v{v}.windows.amd64.exe\n")

    def lib(self, code, cwd=None, path=None):
        env = dict(os.environ, SECRETS_LIB="1")
        if path:
            env["PATH"] = path
        return subprocess.run(["bash", "-c", f'set -euo pipefail\nsource "{self.SECRETS}"\n' + code], cwd=cwd or self.dl,
                              capture_output=True, text=True, env=env, timeout=60)

    def test_rewritten_line_verifies_a_file_named_sops(self):
        r = self.lib(f'sops_sum_line sums sops-v{self.VERSION}.linux.amd64')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, hashlib.sha256(self.binary).hexdigest() + "  sops\n")
        r = self.lib(f'sops_sum_line sums sops-v{self.VERSION}.linux.amd64 | sha256sum -c -')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "sops: OK")

    def test_the_0_6_0_sed_did_not_verify(self):
        # Keeps the bug on record: the old pipeline fails on the same realistic file.
        r = self.lib(f'grep " sops-v{self.VERSION}.linux.amd64$" sums | sed "s/ sops-v.*/  sops/" | sha256sum -c -')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("' sops'", r.stderr)

    def test_binary_mode_marker_and_arm64_pick_the_right_line(self):
        arm = hashlib.sha256(b"arm").hexdigest()
        with open(self.sums, "a") as fh:
            fh.write(f"{arm} *sops-v9.9.9.linux.arm64\n")
        r = self.lib('sops_sum_line sums sops-v9.9.9.linux.arm64')
        self.assertEqual(r.stdout, arm + "  sops\n")

    def test_tampered_download_or_missing_line_fails(self):
        with open(os.path.join(self.dl, "sops"), "ab") as fh:
            fh.write(b"evil\n")
        r = self.lib(f'sops_sum_line sums sops-v{self.VERSION}.linux.amd64 | sha256sum -c -')
        self.assertNotEqual(r.returncode, 0)
        r = self.lib('sops_sum_line sums sops-v0.0.0.linux.amd64')
        self.assertEqual(r.stdout, "")
        r = self.lib('sops_sum_line sums sops-v0.0.0.linux.amd64 | sha256sum -c -')
        self.assertNotEqual(r.returncode, 0)

    def install_tools(self, binary):
        """Run install_tools with stand-ins for curl and dpkg; install is recorded instead of writing /usr/local/bin."""
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(self.tmp, "sops.download"), "wb") as fh:
            fh.write(binary)
        scripts = {
            "curl": f'out=""; url=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; -*) ;; *) url="$1";; esac; shift; done\n'
                    f'case "$url" in *checksums.txt) cp "{self.sums}" "$out";; *) cp "{self.tmp}/sops.download" "$out";; esac',
            "dpkg": "echo amd64",
            "age-keygen": "exit 0",
        }
        for name, body in scripts.items():
            with open(os.path.join(bin_dir, name), "w") as fh:
                fh.write("#!/bin/sh\n" + body + "\n")
            os.chmod(os.path.join(bin_dir, name), 0o755)
        code = f'install() {{ echo "install $*" >> "{self.tmp}/installed"; }}\nSOPS_VERSION={self.VERSION}\ninstall_tools'
        return self.lib(code, cwd=self.tmp, path=f"{bin_dir}:/usr/bin:/bin")

    def test_install_tools_installs_a_matching_download(self):
        if shutil.which("sops", path="/usr/bin:/bin"):
            self.skipTest("sops is in /usr/bin, so install_tools would skip the download")
        r = self.install_tools(self.binary)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.tmp, "installed")) as fh:
            self.assertRegex(fh.read(), r"^install -m 755 \S+/sops /usr/local/bin/sops\n$")

    def test_install_tools_refuses_a_download_that_does_not_match(self):
        if shutil.which("sops", path="/usr/bin:/bin"):
            self.skipTest("sops is in /usr/bin, so install_tools would skip the download")
        r = self.install_tools(b"not the release\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match its published checksum; nothing installed", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "installed")))


if __name__ == "__main__":
    unittest.main()
