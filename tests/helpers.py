"""Shared test helpers. Standard library only."""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server")
sys.path.insert(0, SERVER)
import swarm_backend as sb  # noqa: E402

ADMIN = "admin-tok-for-tests"
WORKER = "worker-tok-for-tests"


class Backend:
    """A backend on a random local port, in its own temp folder."""

    def __init__(self, **env):
        self.tmp = tempfile.mkdtemp()
        for name, val in (("admin", ADMIN), ("worker", WORKER)):
            with open(os.path.join(self.tmp, name), "w") as fh:
                fh.write(val)
        os.environ.update(SWARM_ADMIN_TOKEN_FILE=os.path.join(self.tmp, "admin"),
                          SWARM_BACKEND_WORKER_TOKEN_FILE=os.path.join(self.tmp, "worker"),
                          SWARM_ALLOW_HTTP_NOTIFY="1", SWARM_QUIET="1", SWARM_AUTH_FAIL_LIMIT="100000",
                          SWARM_BOT_KEYS_DIR=os.path.join(self.tmp, "bot-keys"))
        os.environ.update(env)
        self.db = os.path.join(self.tmp, "t.db")
        self.srv, self.api = sb.make_server(self.db, "127.0.0.1:0", "https://swarm.example")
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, tok=ADMIN, raw=None, headers=None, bare=False):
        """Returns (status, parsed body). The admin token queueing a task gets owner "tester" unless bare=True (enqueue needs an owner)."""
        if (not bare and method == "POST" and path == "/tasks" and tok == ADMIN and isinstance(body, dict)
                and "owner" not in body and not body.get("follow_up")):
            body = dict(body, owner="tester")
        h = {"Content-Type": "application/json"}
        if tok is not None:
            h["Authorization"] = "Bearer " + tok
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                status, out = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, out = e.code, e.read()
        try:
            return status, json.loads(out)
        except ValueError:
            return status, out

    def req(self, method, path, body=None, tok=ADMIN, expect=200, **kw):
        status, out = self.call(method, path, body, tok, **kw)
        assert status == expect, (method, path, status, out)
        return out

    def bot(self, bot_id, role="project", url=None, projects=(), repo=None):
        body = {"role": role}
        if url:
            body["notify_url"] = url
        self.req("PUT", f"/bots/{bot_id}", body)
        for p in projects:
            self.req("PUT", f"/projects/{p}", {"default_repo": repo or f"acme/{p}", "add_bots": [bot_id]})
        return self.req("POST", f"/bots/{bot_id}/token")["token"]

    def claim(self, worker="w1"):
        return self.req("POST", "/worker/claim", {"worker": worker}, tok=WORKER)["task"]

    def finish(self, claimed, status="done", **extra):
        return self.call("POST", f"/tasks/{claimed['id']}/finish", dict(extra, status=status, claim_id=claimed["claim_id"]),
                         tok=claimed["task_token"])
