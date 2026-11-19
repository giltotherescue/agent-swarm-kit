"""Backend tests. Standard library only: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import swarm_backend as sb  # noqa: E402


class Receiver(BaseHTTPRequestHandler):
    """Fake bot inbox. Path /fail answers 404 (permanent), /flaky answers 500."""
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Receiver.got.append((self.path, self.headers.get("Idempotency-Key"), self.headers.get("Authorization"), body))
        code = 404 if self.path == "/fail" else 500 if self.path == "/flaky" else 200
        self.send_response(code)
        self.end_headers()


class BackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        for name, val in (("admin", "admin-test-token"), ("worker", "worker-test-token")):
            with open(os.path.join(cls.tmp, name), "w") as fh:
                fh.write(val)
        os.environ["SWARM_ADMIN_TOKEN_FILE"] = os.path.join(cls.tmp, "admin")
        os.environ["SWARM_BACKEND_WORKER_TOKEN_FILE"] = os.path.join(cls.tmp, "worker")
        os.environ["SWARM_ALLOW_HTTP_NOTIFY"] = "1"
        os.environ["SWARM_QUIET"] = "1"
        os.environ["SWARM_BOT_KEYS_DIR"] = os.path.join(cls.tmp, "bot-keys")
        os.environ["SWARM_AUTH_FAIL_LIMIT"] = "100000"
        cls.srv, cls.api = sb.make_server(os.path.join(cls.tmp, "t.db"), "127.0.0.1:0", "https://swarm.example")
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.rx = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        cls.rx_url = f"http://127.0.0.1:{cls.rx.server_address[1]}"
        threading.Thread(target=cls.rx.serve_forever, daemon=True).start()
        # Bots: a chief and two project bots.
        cls.req("PUT", "/bots/chief", {"role": "chief", "notify_url": cls.rx_url + "/chief", "notify_auth": "hook-secret"})
        cls.req("PUT", "/bots/web", {"notify_url": cls.rx_url + "/web"})
        cls.req("PUT", "/bots/shop", {"notify_url": cls.rx_url + "/fail"})
        cls.web = cls.req("POST", "/bots/web/token")["token"]
        cls.shop = cls.req("POST", "/bots/shop/token")["token"]
        cls.chief = cls.req("POST", "/bots/chief/token")["token"]
        # 0.2.0: a project bot works only on repos of projects it is bound to.
        cls.req("PUT", "/projects/site", {"default_repo": "acme/site", "add_repos": ["a/b"], "add_bots": ["web"]})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.rx.shutdown()

    @classmethod
    def req(cls, method, path, body=None, tok="admin-test-token", expect=200, raw=None, headers=None):
        h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(cls.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                status, data = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        assert status == expect, (status, data)
        try:
            return json.loads(data)
        except ValueError:
            return data

    def claim(self, tid=None):
        claimed = self.req("POST", "/worker/claim", {"worker": "w1"}, tok="worker-test-token")["task"]
        if tid:
            self.assertEqual(claimed["id"], tid)
        return claimed

    def work(self, tid, status, **extra):
        c = self.claim(tid)
        return self.req("POST", f"/tasks/{tid}/finish", dict(extra, status=status, claim_id=c["claim_id"]),
                        tok=c["task_token"])["task"]

    def deliver(self):
        Receiver.got.clear()
        self.api.s.run("UPDATE notices SET next_at = 0 WHERE state = 'pending'")
        sb.deliver_once(self.api)
        return list(Receiver.got)

    def test_auth_and_health(self):
        self.req("GET", "/health", tok="nope")
        self.req("GET", "/status", tok="wrong", expect=401)
        self.req("POST", "/worker/claim", {}, tok=self.web, expect=403)

    def test_idempotency(self):
        a = self.req("POST", "/tasks", {"prompt": "Write the FAQ", "idempotency_key": "faq-1"}, tok=self.web)
        b = self.req("POST", "/tasks", {"prompt": "Write the FAQ", "idempotency_key": "faq-1"}, tok=self.web)
        self.assertEqual(a["task"]["id"], b["task"]["id"])
        self.assertTrue(b["duplicate"])
        err = self.req("POST", "/tasks", {"prompt": "Something else", "idempotency_key": "faq-1"}, tok=self.web, expect=409)
        self.assertIn("different task", err["error"])
        self.req("POST", f"/tasks/{a['task']['id']}/cancel", {}, tok=self.web)

    def test_bot_isolation(self):
        t = self.req("POST", "/tasks", {"prompt": "shop only"}, tok=self.shop)["task"]
        self.assertEqual(t["owner"], "shop")
        self.req("GET", f"/tasks/{t['id']}", tok=self.web, expect=404)
        self.req("POST", "/tasks", {"prompt": "x", "owner": "shop"}, tok=self.web, expect=403)
        self.req("POST", f"/tasks/{t['id']}/cancel", {}, tok=self.shop)

    def test_lifecycle_owner_routing_and_no_double_delivery(self):
        tid = self.req("POST", "/tasks", {"prompt": "Fix the header", "repo": "acme/site"}, tok=self.web)["task"]["id"]
        t = self.work(tid, "waiting", question="Blue or green?")
        self.assertEqual((t["state"], t["waiting_on"]), ("waiting", "owner"))
        got = self.deliver()
        self.assertEqual([g[0] for g in got], ["/web"], "owner only, no hub")
        self.assertEqual(got[0][3]["question"], "Blue or green?")
        self.assertEqual(got[0][3]["report_url"], f"https://swarm.example/tasks/{tid}/report")
        # Same event again: nothing new.
        self.api.notify_task(self.api.s.one("SELECT * FROM tasks WHERE id = ?", tid))
        self.assertEqual(self.deliver(), [])
        self.req("POST", f"/tasks/{tid}/answer", {"answer": "Blue"}, tok=self.web)
        claimed = self.claim(tid)
        self.assertEqual(claimed["pending_answer"], "Blue")
        tt, cid = claimed["task_token"], claimed["claim_id"]
        self.req("POST", f"/tasks/{tid}/message", {"text": "also the footer"}, tok=self.web)
        hb = self.req("POST", f"/tasks/{tid}/heartbeat", {"claim_id": cid}, tok=tt)
        self.assertEqual([m["text"] for m in hb["messages"]], ["also the footer"])
        self.req("PUT", f"/tasks/{tid}/files/report/REPORT.md", None, tok=tt, headers={"X-Claim-Id": cid})  # empty body ok
        self.req("POST", f"/tasks/{tid}/finish", {"status": "done", "summary": "Header fixed", "claim_id": cid}, tok=tt)
        got = self.deliver()
        self.assertEqual([(g[0], g[3]["state"], g[3]["summary"]) for g in got], [("/web", "done", "Header fixed")])
        self.assertTrue(got[0][1].startswith(tid + ":done:"))

    def test_ownerless_and_human_questions_go_to_chief(self):
        tid = self.req("POST", "/tasks", {"prompt": "no owner here"})["task"]["id"]
        self.work(tid, "done")
        got = self.deliver()
        self.assertEqual([(g[0], g[3]["reason"]) for g in got], [("/chief", "no_owner")])
        self.assertEqual(got[0][2], "Bearer hook-secret")
        tid = self.req("POST", "/tasks", {"prompt": "pricing call", "ask_human": True}, tok=self.web)["task"]["id"]
        self.work(tid, "waiting", question="Raise prices?")
        got = sorted((g[0], g[3]["reason"]) for g in self.deliver())
        self.assertEqual(got, [("/chief", "waiting_on_human"), ("/web", "owner")])
        self.req("POST", f"/tasks/{tid}/answer", {"answer": "yes"}, tok=self.web, expect=409)
        self.req("POST", f"/tasks/{tid}/message", {"text": "yes"}, tok=self.web, expect=409)
        items = self.req("GET", "/waiting", tok=self.chief)["items"]
        self.assertIn(tid, [i["id"] for i in items])
        self.req("POST", f"/waiting/{tid}", {"action": "answer", "answer": "Yes, 10 percent"}, tok=self.chief)
        self.assertEqual(self.req("GET", f"/tasks/{tid}")["task"]["state"], "queued")
        self.req("POST", f"/tasks/{tid}/cancel", {}, tok=self.web)

    def test_failed_delivery_falls_back_to_chief(self):
        tid = self.req("POST", "/tasks", {"prompt": "shop work"}, tok=self.shop)["task"]["id"]
        self.work(tid, "done")
        first = self.deliver()
        self.assertEqual([g[0] for g in first], ["/fail"])
        second = self.deliver()
        self.assertEqual([(g[0], g[3]["reason"], g[3]["delivery_failed_for"]) for g in second], [("/chief", "fallback", "shop")])
        events = [e["type"] for e in self.req("GET", f"/tasks/{tid}")["task"]["events"]]
        self.assertIn("notify_failed", events)

    def test_next_limit_and_attempt_cap(self):
        tid = self.req("POST", "/tasks", {"prompt": "long job", "max_attempts": 2}, tok=self.web)["task"]["id"]
        t = self.work(tid, "next", next_step="run part 2")
        self.assertEqual((t["state"], t["next_step"]), ("queued", "run part 2"))
        t = self.work(tid, "limit")
        self.assertEqual(t["state"], "waiting")
        self.assertEqual(t["outcome"], "attempt_cap")
        self.assertEqual([g[3]["outcome"] for g in self.deliver()], ["attempt_cap"])
        self.req("POST", f"/tasks/{tid}/cancel", {}, tok=self.web)
        tid = self.req("POST", "/tasks", {"prompt": "limit job"}, tok=self.web)["task"]["id"]
        t = self.work(tid, "limit")
        self.assertEqual(t["state"], "queued")
        self.assertGreater(t["not_before"], time.time() + 3000)
        self.req("POST", f"/tasks/{tid}/cancel", {}, tok=self.web)

    def test_reaper_requeues_lost_tasks(self):
        tid = self.req("POST", "/tasks", {"prompt": "will be lost"}, tok=self.web)["task"]["id"]
        self.claim(tid)
        self.api.s.run("UPDATE tasks SET lease_until = 1 WHERE id = ?", tid)
        self.api.reap()
        t = self.req("GET", f"/tasks/{tid}")["task"]
        self.assertEqual((t["state"], t["outcome"]), ("queued", "lost"))
        self.req("POST", f"/tasks/{tid}/cancel", {}, tok=self.web)

    def test_waiting_list_dedupe_and_drop(self):
        body = {"project": "web", "kind": "deliverable-review", "ask": "Review the About page?",
                "recommendation": "Publish", "links": ["https://example.com/a"], "dedupe_key": "about-v1"}
        a = self.req("POST", "/waiting", body, tok=self.web)
        b = self.req("POST", "/waiting", dict(body, ask="Review the revised About page?"), tok=self.web)
        self.assertEqual(a["item"]["id"], b["item"]["id"])
        self.assertTrue(b["updated"])
        self.req("POST", f"/waiting/{a['item']['id']}", {"action": "drop"}, tok=self.web, expect=403)
        self.req("POST", f"/waiting/{a['item']['id']}", {"action": "drop"}, tok=self.chief)
        got = self.deliver()
        self.assertEqual([(g[0], g[3]["dropped"]) for g in got], [("/web", True)])
        err = self.req("POST", "/waiting", body, tok=self.web, expect=409)
        self.assertIn("dropped", err["error"])

    def test_push_rules(self):
        self.req("POST", "/tasks", {"prompt": "x", "open_pr": True}, tok=self.web, expect=400)
        self.req("POST", "/tasks", {"prompt": "x", "repo": "a/b", "open_pr": True, "untrusted_input": True}, tok=self.web, expect=400)
        self.req("POST", "/tasks", {"prompt": "x", "repo": "a/b", "push": True, "report_only": True}, tok=self.web, expect=400)

    def test_bot_cannot_promote_itself(self):
        self.req("PUT", "/bots/web", {"role": "chief"}, tok=self.web, expect=403)
        self.req("PUT", "/bots/shop", {"notify_url": "https://x.example"}, tok=self.web, expect=403)


if __name__ == "__main__":
    unittest.main()
