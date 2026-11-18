"""Regression test for the 0.2.0 install-test defects 2 and 3: the runner and the agent are two different OS users,
and the handoff folders only work if the setgid bit survives. Running as one user (like the other tests) cannot
catch this, so this test runs tests/handoff_users_check.py as root in a sandbox where it can make two users:

  1. already root (for example a CI container): run it directly (it makes two system users and removes them)
  2. Docker works: run it in a throwaway container (image from SWARM_TEST_IMAGE, default python:3.12-slim)
  3. passwordless sudo AND (CI is set or SWARM_TEST_USE_SUDO=1): run it with sudo on this machine
  4. none of these: skip, with a loud message. Set SWARM_REQUIRE_USER_TESTS=1 to fail instead of skip.
"""
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.abspath(os.path.join(HERE, ".."))
CHECK = os.path.join(HERE, "handoff_users_check.py")
IMAGE = os.environ.get("SWARM_TEST_IMAGE", "python:3.12-slim")


def docker_ok():
    if not shutil.which("docker"):
        return False, "docker is not installed"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0:
            return False, "docker is installed but not usable by this user"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            if subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True, timeout=300).returncode != 0:
                return False, f"could not pull {IMAGE}"
    except subprocess.TimeoutExpired:
        return False, "docker timed out"
    return True, ""


def sandbox_command():
    """Returns (command, why-not)."""
    if os.geteuid() == 0:
        return [sys.executable, "-I", "-B", CHECK, "--kit", KIT], ""
    ok, why = docker_ok()
    if ok:
        return ["docker", "run", "--rm", "--network", "none", "-v", f"{KIT}:/kit:ro", IMAGE,
                "python3", "-I", "-B", "/kit/tests/handoff_users_check.py", "--kit", "/kit"], ""
    allow_sudo = os.environ.get("CI") or os.environ.get("SWARM_TEST_USE_SUDO") == "1"
    if allow_sudo and shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return ["sudo", "-n", sys.executable, "-I", "-B", CHECK, "--kit", KIT], ""
    return None, why + "; not root; no passwordless sudo allowed (set SWARM_TEST_USE_SUDO=1 to allow it)"


class HandoffTwoUsersTest(unittest.TestCase):
    def test_runner_and_agent_as_separate_users(self):
        cmd, why = sandbox_command()
        if cmd is None:
            msg = ("\n" + "!" * 78 + "\n"
                   "SKIPPED: the two-user handoff test (install-test defects 2 and 3) could not run here:\n"
                   f"  {why}\n"
                   "This is the only test that catches a broken inbox/outbox on a real server.\n"
                   "Run it where Docker works, as root, or with SWARM_TEST_USE_SUDO=1.\n" + "!" * 78)
            print(msg, file=sys.stderr)
            if os.environ.get("SWARM_REQUIRE_USER_TESTS") == "1":
                self.fail(msg)
            self.skipTest(why)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 4:
            self.fail("the sandbox does not clear setgid on chmod like a real server, so the check proves nothing:\n" + out)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("HANDOFF CHECK OK", out)


if __name__ == "__main__":
    unittest.main()
