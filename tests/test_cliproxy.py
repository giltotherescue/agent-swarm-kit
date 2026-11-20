"""Tests for the optional CLIProxyAPI pieces: the panel gate (server/optional/cliproxy-gate.py) and the config the installer writes
(scripts/setup-cliproxy.sh). Not tested here: the real proxy binary under systemd, `tailscale serve`, and a browser OAuth
sign-in through the panel. See docs/optional-services.md."""
import http.client
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

KIT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
loader = importlib.machinery.SourceFileLoader("gate", os.path.join(KIT, "server", "optional", "cliproxy-gate.py"))
spec = importlib.util.spec_from_loader("gate", loader)
gate = importlib.util.module_from_spec(spec)
loader.exec_module(gate)
SETUP = os.path.join(KIT, "scripts", "setup-cliproxy.sh")
KEY = "test-management-key-1234567890"


class Upstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        Upstream.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        out = b"upstream ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_any


class Gate(unittest.TestCase):
    def setUp(self):
        Upstream.seen = []
        self.up = HTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.gate = gate.make_server("127.0.0.1:0", f"127.0.0.1:{self.up.server_address[1]}")
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.addCleanup(lambda: [self.gate.shutdown(), self.gate.server_close(), self.up.shutdown(), self.up.server_close()])

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.gate.server_address[1], timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read().decode()

    def test_panel_and_management_api_pass_through_with_their_key(self):
        self.assertEqual(self.req("GET", "/management.html"), (200, "upstream ok"))
        s, _ = self.req("GET", "/v0/management/codex-auth-url?is_webui=true", headers={"Authorization": "Bearer " + KEY})
        self.assertEqual(s, 200)
        s, _ = self.req("POST", "/v0/management/oauth-callback", body='{"provider":"codex"}', headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
        self.assertEqual(s, 200)
        self.assertEqual(Upstream.seen[1][:3], ("GET", "/v0/management/codex-auth-url?is_webui=true", "Bearer " + KEY))
        self.assertEqual(Upstream.seen[2][3], '{"provider":"codex"}')

    def test_everything_else_is_404_and_never_reaches_the_proxy(self):
        for path in ("/v1/models", "/v1/chat/completions", "/", "/v0/management", "/v0/other", "/management.html/x",
                     "/v0/management/../v1/models", "/v8/management", "/v8/management/%2e%2e/v1/models", "/v0/management/%2e%2e/v1/models", "//v1/models", "/v0/management//x"):
            self.assertEqual(self.req("GET", path)[0], 404, path)
        self.assertEqual(self.req("POST", "/v1/chat/completions", body="{}")[0], 404)
        self.assertEqual(Upstream.seen, [])

    def test_a_dead_proxy_is_a_502_not_a_hang(self):
        g = gate.make_server("127.0.0.1:0", "127.0.0.1:1")
        threading.Thread(target=g.serve_forever, daemon=True).start()
        self.addCleanup(lambda: [g.shutdown(), g.server_close()])
        c = http.client.HTTPConnection("127.0.0.1", g.server_address[1], timeout=10)
        c.request("GET", "/management.html")
        self.assertEqual(c.getresponse().status, 502)

    def test_refuses_to_listen_beyond_localhost(self):
        with self.assertRaises(SystemExit):
            gate.make_server("0.0.0.0:0", "127.0.0.1:1")


class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.keyfile = os.path.join(self.tmp, "key")
        with open(self.keyfile, "w") as fh:
            fh.write(KEY + "\n")
        os.chmod(self.keyfile, 0o600)

    def run_setup(self, *args):
        return subprocess.run(["bash", SETUP, *args], capture_output=True, text=True, env=dict(os.environ, CLIPROXY_PREFIX=self.tmp + "/root"))

    def test_config_binds_localhost_has_the_key_once_and_the_key_file_is_removed(self):
        r = self.run_setup("--management-key-file", self.keyfile)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(KEY, r.stdout + r.stderr)
        conf = os.path.join(self.tmp, "root/var/lib/cliproxyapi/config.yaml")
        with open(conf) as fh:
            text = fh.read()
        self.assertIn('host: "127.0.0.1"', text)
        self.assertIn("allow-remote: false", text)
        self.assertEqual(text.count(KEY), 1)
        self.assertEqual(oct(os.stat(conf).st_mode & 0o777), "0o600")
        self.assertFalse(os.path.exists(self.keyfile))

    def test_existing_config_is_kept(self):
        self.run_setup("--management-key-file", self.keyfile)
        conf = os.path.join(self.tmp, "root/var/lib/cliproxyapi/config.yaml")
        with open(conf, "a") as fh:
            fh.write("# mine\n")
        with open(self.keyfile, "w") as fh:
            fh.write("another-management-key-0987654321\n")
        self.run_setup("--management-key-file", self.keyfile)
        with open(conf) as fh:
            text = fh.read()
        self.assertIn("# mine", text)
        self.assertNotIn("another-management-key", text)

    def test_odd_keys_are_refused_before_anything_is_written(self):
        for bad in ('short', 'has"quote-0123456789abcdef', "line\nbreak-0123456789abcdef", "has space 0123456789abcdef"):
            with open(self.keyfile, "w") as fh:
                fh.write(bad)
            r = self.run_setup("--management-key-file", self.keyfile)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "root/var/lib/cliproxyapi/config.yaml")))

    def test_the_key_cannot_come_from_the_command_line(self):
        self.assertNotEqual(self.run_setup("--management-key", KEY).returncode, 0)
        self.assertNotEqual(self.run_setup().returncode, 0)


if __name__ == "__main__":
    unittest.main()
