"""Authorization fixes (must-fix 2): Astra 2, 3, 4, 7, plus the HTTP protections from must-fix 4."""
import os
import re
import socket
import time
import unittest

from helpers import ADMIN, WORKER, Backend, sb

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "backend.md")


class AuthzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.alice = cls.b.bot("alice", projects=["site"], repo="acme/site")
        cls.bob = cls.b.bot("bob", projects=["shop"], repo="acme/shop")
        cls.chief = cls.b.bot("chief", role="chief")

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    # ---- Astra 2: duplicate detection never shows another bot's task ----
    def test_dedupe_is_per_owner(self):
        a = self.b.req("POST", "/tasks", {"prompt": "Private plan for site", "title": "alice secret title",
                                          "idempotency_key": "k1"}, tok=self.alice)
        b = self.b.req("POST", "/tasks", {"prompt": "Private plan for site", "idempotency_key": "k1"}, tok=self.bob)
        self.assertFalse(b["duplicate"])
        self.assertNotEqual(a["task"]["id"], b["task"]["id"])
        self.assertNotIn("alice secret title", str(b))
        # Implicit (same brief) duplicate check is per owner too.
        c = self.b.req("POST", "/tasks", {"prompt": "Private plan for site", "title": "alice secret title"}, tok=self.alice)
        self.assertTrue(c["duplicate"])
        d = self.b.req("POST", "/tasks", {"prompt": "Private plan for site", "title": "alice secret title"}, tok=self.bob)
        self.assertEqual(d["task"]["id"], b["task"]["id"])  # bob's own earlier task, never alice's
        self.assertEqual(d["task"]["owner"], "bob")
        # Same key, different work: refused, and the id shown is the caller's own task.
        st, err = self.b.call("POST", "/tasks", {"prompt": "something else", "idempotency_key": "k1"}, tok=self.bob)
        self.assertEqual(st, 409)
        self.assertEqual(err["task_id"], b["task"]["id"])

    def test_dedupe_covers_work_defining_fields(self):
        a = self.b.req("POST", "/tasks", {"prompt": "Same words", "project": "site", "branch": "x"}, tok=self.alice)
        b = self.b.req("POST", "/tasks", {"prompt": "Same words", "project": "site", "branch": "y"}, tok=self.alice)
        self.assertNotEqual(a["task"]["id"], b["task"]["id"])
        c = self.b.req("POST", "/tasks", {"prompt": "Same words", "project": "site", "branch": "x",
                                          "attachments": [{"name": "a.txt", "content_base64": "aGk="}]}, tok=self.alice)
        self.assertFalse(c["duplicate"])

    # ---- Astra 3: projects and repos are bound per bot ----
    def test_project_and_repo_binding(self):
        self.assertEqual(self.b.call("POST", "/tasks", {"prompt": "x", "project": "shop"}, tok=self.alice)[0], 403)
        self.assertEqual(self.b.call("POST", "/tasks", {"prompt": "x", "repo": "acme/shop"}, tok=self.alice)[0], 403)
        self.assertEqual(self.b.call("POST", "/tasks", {"prompt": "x", "repo": "evil/repo"}, tok=self.alice)[0], 403)
        self.assertEqual(self.b.call("POST", "/tasks", {"prompt": "x", "project": "site", "repo": "acme/shop"}, tok=self.alice)[0], 403)
        ok = self.b.req("POST", "/tasks", {"prompt": "fix header", "repo": "acme/site"}, tok=self.alice)["task"]
        self.assertEqual((ok["project"], ok["repo"]), ("site", "acme/site"))
        ok = self.b.req("POST", "/tasks", {"prompt": "fix footer", "project": "site"}, tok=self.alice)["task"]
        self.assertEqual(ok["repo"], "acme/site")
        # No repo, no project: research is always fine.
        self.b.req("POST", "/tasks", {"prompt": "research competitors"}, tok=self.alice)
        # Bots see only their own projects.
        names = [p["name"] for p in self.b.req("GET", "/projects", tok=self.alice)["projects"]]
        self.assertEqual(names, ["site"])

    def test_push_main_is_opt_in(self):
        st, err = self.b.call("POST", "/tasks", {"prompt": "hotfix", "project": "site", "push_main": True}, tok=self.alice)
        self.assertEqual(st, 400)
        self.assertIn("allow-push-main", err["error"])
        self.b.req("PUT", "/projects/site", {"allow_push_main": True})
        t = self.b.req("POST", "/tasks", {"prompt": "hotfix", "project": "site", "push_main": True}, tok=self.alice)["task"]
        self.assertEqual((t["push"], t["push_main"], t["open_pr"]), (1, 1, 0))
        self.b.req("PUT", "/projects/site", {"allow_push_main": False})

    # ---- Astra 4: waiting items act only on the filer's own tasks ----
    def test_waiting_item_cannot_link_or_act_on_another_bots_task(self):
        bob_task = self.b.req("POST", "/tasks", {"prompt": "bob work"}, tok=self.bob)["task"]["id"]
        st, _ = self.b.call("POST", "/waiting", {"project": "site", "kind": "decision", "ask": "Harmless?", "dedupe_key": "trap",
                                                 "task_id": bob_task}, tok=self.alice)
        self.assertEqual(st, 404)  # alice cannot even see bob's task
        st, _ = self.b.call("POST", "/waiting", {"project": "site", "bot": "alice", "kind": "decision", "ask": "Harmless?",
                                                 "dedupe_key": "trap2", "task_id": bob_task}, tok=self.chief)
        self.assertEqual(st, 403)
        # Even an item that somehow points at bob's task never cancels it.
        item = self.b.req("POST", "/waiting", {"project": "site", "kind": "decision", "ask": "Fine?", "dedupe_key": "trap3"},
                          tok=self.alice)["item"]
        self.b.api.s.run("UPDATE items SET task_id = ? WHERE id = ?", bob_task, item["id"])
        self.b.req("POST", f"/waiting/{item['id']}", {"action": "drop"}, tok=self.chief)
        self.assertEqual(self.b.req("GET", f"/tasks/{bob_task}")["task"]["state"], "queued")
        self.b.req("POST", f"/tasks/{bob_task}/cancel", {}, tok=self.bob)

    def test_bots_cannot_see_each_others_things(self):
        t = self.b.req("POST", "/tasks", {"prompt": "only bob"}, tok=self.bob)["task"]["id"]
        for method, path in (("GET", f"/tasks/{t}"), ("GET", f"/tasks/{t}/report"), ("GET", f"/tasks/{t}/artifacts"),
                             ("POST", f"/tasks/{t}/answer"), ("POST", f"/tasks/{t}/message"), ("POST", f"/tasks/{t}/cancel")):
            self.assertEqual(self.b.call(method, path, {"answer": "x", "text": "x"}, tok=self.alice)[0], 404, path)
        self.assertNotIn(t, [x["id"] for x in self.b.req("GET", "/tasks", tok=self.alice)["tasks"]])
        st = self.b.req("GET", "/status", tok=self.alice)
        self.assertNotIn(t, str(st))
        self.assertEqual(self.b.call("GET", "/bots/bob", tok=self.alice)[0], 403)
        self.assertEqual(self.b.call("POST", "/bots/bob/test", {}, tok=self.alice)[0], 403)
        self.b.req("POST", f"/tasks/{t}/cancel", {}, tok=self.bob)

    def test_notify_url_is_shortened_except_for_admin(self):
        self.b.req("PUT", "/bots/alice", {"notify_url": "http://127.0.0.1:9/hooks/secret-path?key=abc"}, tok=self.alice)
        shown = self.b.req("GET", "/bots/alice", tok=self.alice)["bot"]["notify_url"]
        self.assertNotIn("secret-path", shown)
        self.assertNotIn("secret-path", str(self.b.req("GET", "/bots", tok=self.chief)))
        self.assertIn("secret-path", self.b.req("GET", "/bots/alice")["bot"]["notify_url"])
        self.b.req("PUT", "/bots/alice", {"notify_url": ""}, tok=self.alice)

    # ---- Astra 7: the permission matrix is enforced and published ----
    def test_route_matrix(self):
        self.b.req("POST", "/tasks", {"prompt": "matrix task", "allow_duplicate": True, "owner": "alice"})
        claimed = None
        while True:
            c = self.b.claim()
            if not c:
                break
            if c["prompt"] == "matrix task":
                claimed = c
                break
            self.b.finish(c, "done")
        task_tok = claimed["task_token"]
        tokens = {"admin": ADMIN, "chief": self.chief, "bot": self.alice, "worker": WORKER, "task": task_tok}
        for r in sb.ROUTES:
            path = r["pattern"].replace("([^/]+)", "zz-none").replace("(artifacts|attachments)", "artifacts") \
                .replace("(report|artifact|bundle)", "report").replace("(start|stop|restart|delete)", "start").replace("\\", "")
            for role, tok in tokens.items():
                st, out = self.b.call(r["method"], path, {}, tok=tok)
                generic = st == 403 and isinstance(out, dict) and out.get("error") == "this token cannot do that"
                if r["roles"] is None or role in r["roles"]:
                    # Allowed roles may still get "not yours" (own-only rules), never the role refusal or 401.
                    self.assertFalse(st == 401 or generic, (r["method"], path, role, st, out))
                else:
                    self.assertTrue(generic, (r["method"], path, role, st, out))
            if r["roles"] is not None:
                self.assertEqual(self.b.call(r["method"], path, {}, tok=None)[0], 401, path)
        self.b.finish(claimed, "done")

    def test_docs_publish_the_same_matrix(self):
        with open(DOCS) as fh:
            self.assertIn(sb.permission_table(), fh.read(), "docs/backend.md is out of date: python3 server/swarm_backend.py --permissions")


class HttpHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def raw(self, request, read=True):
        s = socket.create_connection(("127.0.0.1", self.b.srv.server_address[1]), timeout=10)
        s.sendall(request)
        data = s.recv(4096) if read else b""
        s.close()
        return data

    def test_auth_before_body(self):
        # Claims a huge body and sends none: rejected at once, without waiting to read it.
        t0 = time.time()
        out = self.raw(b"POST /tasks HTTP/1.1\r\nHost: x\r\nContent-Length: 50000000\r\n\r\n")
        self.assertIn(b" 401 ", out.split(b"\r\n")[0])
        self.assertLess(time.time() - t0, 5)

    def test_bad_lengths(self):
        h = f"Authorization: Bearer {ADMIN}\r\n".encode()
        self.assertIn(b" 400 ", self.raw(b"POST /tasks HTTP/1.1\r\nHost: x\r\n" + h + b"Content-Length: -5\r\n\r\n"))
        self.assertIn(b" 400 ", self.raw(b"POST /tasks HTTP/1.1\r\nHost: x\r\n" + h + b"Content-Length: abc\r\n\r\n"))
        self.assertIn(b" 413 ", self.raw(b"POST /tasks/x/answer HTTP/1.1\r\nHost: x\r\n" + h + b"Content-Length: 5000000\r\n\r\n"))
        self.assertIn(b" 411 ", self.raw(b"POST /tasks HTTP/1.1\r\nHost: x\r\n" + h + b"Transfer-Encoding: chunked\r\n\r\n"))

    def test_auth_failure_rate_limit(self):
        old = self.b.api.limiter.limit
        self.b.api.limiter.limit = 3
        self.b.api.limiter.hits.clear()
        try:
            codes = [self.b.call("GET", "/status", tok="wrong")[0] for _ in range(5)]
            self.assertEqual(codes[:3], [401, 401, 401])
            self.assertEqual(codes[3:], [429, 429])
            # Even the right token is refused from that address for a while.
            self.assertEqual(self.b.call("GET", "/status")[0], 429)
        finally:
            self.b.api.limiter.limit = old
            self.b.api.limiter.hits.clear()

    def test_connection_cap(self):
        capped = Backend(SWARM_MAX_CONNECTIONS="1")
        os.environ["SWARM_MAX_CONNECTIONS"] = "32"
        try:
            port = capped.srv.server_address[1]
            hold = socket.create_connection(("127.0.0.1", port), timeout=10)
            hold.sendall(b"GET /health HTTP/1.1\r\n")  # never finishes the request
            time.sleep(0.3)
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertIn(b" 503 ", s.recv(4096))
            s.close()
            hold.close()
        finally:
            capped.close()

    def test_notify_url_ssrf_checks(self):
        os.environ["SWARM_ALLOW_HTTP_NOTIFY"] = "0"
        try:
            for url in ("https://[::1]/", "https://10.0.0.1/", "https://127.0.0.1/x", "https://169.254.169.254/latest",
                        "https://100.64.1.2/", "https://localhost/", "https://[::ffff:127.0.0.1]/", "http://example.com/",
                        "https://user:pw@example.com/"):
                self.assertIsNotNone(sb.valid_notify_url(url), url)
                st, _ = self.b.call("PUT", "/bots/ssrf", {"notify_url": url})
                self.assertEqual(st, 400, url)
            with self.assertRaises(ValueError):
                sb.post_json("https://127.0.0.1:9/x", b"{}", {}, timeout=2)
        finally:
            os.environ["SWARM_ALLOW_HTTP_NOTIFY"] = "1"


if __name__ == "__main__":
    unittest.main()
