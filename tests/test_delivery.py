"""Trigger keys out of the database (must-fix 3), delivery rules (CoS 3, Astra 8, 23, 24, 25), reminders and watchdog."""
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ADMIN, Backend, sb

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class Inbox(BaseHTTPRequestHandler):
    """Fake bot inbox. /dup answers 409, /dupbody answers 200 {"duplicate": true}, /redirect answers 302,
    /fail answers 404, everything else 200."""
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Inbox.got.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()
            return
        code = {"/dup": 409, "/fail": 404}.get(self.path, 200)
        data = b'{"duplicate": true}' if self.path == "/dupbody" else b"{}"
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class DeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.rx = ThreadingHTTPServer(("127.0.0.1", 0), Inbox)
        cls.url = f"http://127.0.0.1:{cls.rx.server_address[1]}"
        threading.Thread(target=cls.rx.serve_forever, daemon=True).start()
        cls.chief = cls.b.bot("chief", role="chief", url=cls.url + "/chief")
        cls.web = cls.b.bot("web", url=cls.url + "/web")

    @classmethod
    def tearDownClass(cls):
        cls.b.close()
        cls.rx.shutdown()

    def deliver(self):
        Inbox.got.clear()
        self.b.api.s.run("UPDATE notices SET next_at = 0 WHERE state = 'pending'")
        sb.deliver_once(self.b.api)
        return list(Inbox.got)

    def done_task(self, owner_tok):
        tid = self.b.req("POST", "/tasks", {"prompt": f"work {time.time()}"}, tok=owner_tok)["task"]["id"]
        c = self.b.claim()
        self.assertEqual(c["id"], tid)
        self.b.finish(c, "done", summary="ok")
        return tid

    # ---- trigger keys live in files ----
    def test_trigger_key_is_in_a_file_not_the_database(self):
        keyfile = os.path.join(tempfile.mkdtemp(), "k")
        with open(keyfile, "w") as fh:
            fh.write("hook-key-123")
        self.b.req("PUT", "/bots/web", {"notify_auth": "hook-key-123"})
        path = os.path.join(self.b.api.keys.dir, "web.key")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(open(path).read(), "hook-key-123")
        row = self.b.api.s.one("SELECT * FROM bots WHERE id = 'web'")
        self.assertIsNone(row["notify_auth"])
        self.assertNotIn("hook-key-123", json.dumps(row))
        self.assertTrue(self.b.req("GET", "/bots/web")["bot"]["has_notify_auth"])
        self.done_task(self.web)
        got = self.deliver()
        self.assertEqual([(g[0], g[1]) for g in got], [("/web", "Bearer hook-key-123")])
        # A backup made the way scripts/backup.sh does it holds no key.
        bdir = tempfile.mkdtemp()
        env = dict(os.environ, SWARM_DB=self.b.db, SWARM_DB_BACKUP_DIR=bdir)
        subprocess.run(["bash", os.path.join(ROOT, "scripts", "backup.sh")], env=env, check=True, capture_output=True)
        (gz,) = [f for f in os.listdir(bdir) if f.endswith(".db.gz")]
        data = subprocess.run(["gunzip", "-c", os.path.join(bdir, gz)], capture_output=True, check=True).stdout
        self.assertNotIn(b"hook-key-123", data)
        self.assertEqual(oct(os.stat(os.path.join(bdir, gz)).st_mode & 0o777), "0o600")
        self.b.req("PUT", "/bots/web", {"notify_auth": ""})
        self.assertFalse(os.path.exists(path))

    def test_migration_moves_old_keys_out(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "old.db")
        c = sqlite3.connect(db)
        c.executescript(sb.SCHEMA_TABLES)
        c.execute("INSERT INTO bots(id, role, notify_url, notify_auth) VALUES('legacy-bot', 'project', 'https://x.example/h', 'old-secret-key')")
        c.commit()
        c.close()
        keys = sb.KeyStore(os.path.join(tmp, "keys"))
        store = sb.Store(db, keys)
        row = store.one("SELECT * FROM bots WHERE id = 'legacy-bot'")
        self.assertIsNone(row["notify_auth"])
        self.assertEqual(row["notify_auth_fp"], sb.fingerprint("old-secret-key"))
        self.assertEqual(keys.read("legacy-bot"), "old-secret-key")
        store.db().close()
        with open(db, "rb") as fh:
            self.assertNotIn(b"old-secret-key", fh.read())
        self.assertFalse(os.path.exists(db + "-wal") and b"old-secret-key" in open(db + "-wal", "rb").read())
        # Running it again changes nothing.
        self.assertEqual(sb.Store(db, keys).move_keys_out(keys), 0)

    def test_new_key_never_goes_to_old_address(self):
        self.b.req("PUT", "/bots/web", {"notify_auth": "key-v1"})
        self.done_task(self.web)  # queued for /web with v1
        # Address and key change while that delivery waits.
        self.b.req("PUT", "/bots/web", {"notify_url": self.url + "/web-new", "notify_auth": "key-v2"})
        got = self.deliver()
        self.assertEqual([(g[0], g[1]) for g in got], [("/web-new", "Bearer key-v2")])
        # A delivery whose version does not match the bot is moved, never sent with mixed settings.
        self.done_task(self.web)
        self.b.api.s.run("UPDATE notices SET notify_version = 0, url = ? WHERE state = 'pending'", self.url + "/web")
        got = self.deliver()
        self.assertEqual([(g[0], g[1]) for g in got], [("/web-new", "Bearer key-v2")])
        self.b.req("PUT", "/bots/web", {"notify_url": self.url + "/web", "notify_auth": ""})

    def test_key_file_swapped_behind_the_backends_back_is_not_sent(self):
        self.b.req("PUT", "/bots/web", {"notify_auth": "key-a"})
        with open(os.path.join(self.b.api.keys.dir, "web.key"), "w") as fh:
            fh.write("key-b")
        self.done_task(self.web)
        self.assertEqual(self.deliver(), [])
        n = self.b.api.s.one("SELECT * FROM notices ORDER BY id DESC LIMIT 1")
        self.assertIn("does not match", n["last_error"])
        self.b.req("PUT", "/bots/web", {"notify_auth": ""})
        self.b.api.s.run("UPDATE notices SET state = 'sent' WHERE state = 'pending'")

    # ---- duplicate answers count as delivered ----
    def test_409_and_duplicate_body_count_as_delivered(self):
        for path in ("/dup", "/dupbody"):
            self.b.req("PUT", "/bots/web", {"notify_url": self.url + path})
            self.done_task(self.web)
            got = self.deliver()
            self.assertEqual([g[0] for g in got], [path])
            n = self.b.api.s.one("SELECT * FROM notices WHERE target = 'web' ORDER BY id DESC LIMIT 1")
            self.assertEqual(n["state"], "sent")
            self.assertEqual(self.deliver(), [], "no fallback to the Chief of Staff")
        self.b.req("PUT", "/bots/web", {"notify_url": self.url + "/web"})

    def test_redirect_is_not_followed_and_falls_back(self):
        self.b.req("PUT", "/bots/web", {"notify_url": self.url + "/redirect", "notify_auth": "k-redirect"})
        tid = self.done_task(self.web)
        got = self.deliver()
        self.assertEqual([g[0] for g in got], ["/redirect"])  # never /elsewhere
        got = self.deliver()
        self.assertEqual([(g[0], g[2]["reason"], g[2]["delivery_failed_for"]) for g in got], [("/chief", "fallback", "web")])
        self.assertNotEqual(got[0][1], "Bearer k-redirect")
        self.assertIn("redirects are not followed", self.b.api.s.one(
            "SELECT last_error FROM notices WHERE event_id LIKE ? AND target = 'web'", tid + "%")["last_error"])
        self.b.req("PUT", "/bots/web", {"notify_url": self.url + "/web", "notify_auth": ""})

    # ---- payload names what happened separately from why this bot got it ----
    def test_item_events_keep_their_event_name(self):
        item = self.b.req("POST", "/waiting", {"project": "site", "kind": "decision", "ask": "Ship it?", "dedupe_key": "ship"},
                          tok=self.web)["item"]
        self.b.req("POST", f"/waiting/{item['id']}", {"action": "answer", "answer": "yes"}, tok=self.chief)
        got = self.deliver()
        self.assertEqual([(g[0], g[2]["event"], g[2]["reason"], g[2]["notify_target"]) for g in got],
                         [("/web", "item_answer", "owner", "web")])

    # ---- Astra 24: snoozing a task-derived entry works ----
    def test_snooze_task_waiting_on_human(self):
        tid = self.b.req("POST", "/tasks", {"prompt": "pricing call", "ask_human": True}, tok=self.web)["task"]["id"]
        c = self.b.claim()
        self.b.finish(c, "waiting", question="Raise prices?")
        self.deliver()
        due = [i["id"] for i in self.b.req("GET", "/waiting", tok=self.chief)["items"]]
        self.assertIn(tid, due)
        self.b.req("POST", f"/waiting/{tid}", {"action": "snooze", "until": int(time.time()) + 3600}, tok=self.chief)
        self.assertNotIn(tid, [i["id"] for i in self.b.req("GET", "/waiting", tok=self.chief)["items"]])
        self.assertIn(tid, [i["id"] for i in self.b.req("GET", "/waiting?status=snoozed", tok=self.chief)["items"]])
        # A filed item linked to the task, snoozed, still covers it.
        item = self.b.req("POST", "/waiting", {"project": "site", "kind": "decision", "ask": "Prices?", "dedupe_key": "prices",
                                               "task_id": tid}, tok=self.web)["item"]
        self.b.api.s.run("UPDATE tasks SET human_snooze_until = NULL WHERE id = ?", tid)
        self.b.req("POST", f"/waiting/{item['id']}", {"action": "snooze"}, tok=self.chief)
        due = [i["id"] for i in self.b.req("GET", "/waiting", tok=self.chief)["items"]]
        self.assertNotIn(tid, due)
        self.assertNotIn(item["id"], due)
        self.b.req("POST", f"/tasks/{tid}/cancel", {}, tok=self.web)
        self.deliver()

    # ---- CoS 10: reminder at 12 hours, escalation at 24, watchdog ----
    def test_reminder_then_escalation_once(self):
        tid = self.b.req("POST", "/tasks", {"prompt": "slow answer"}, tok=self.web)["task"]["id"]
        c = self.b.claim()
        self.b.finish(c, "waiting", question="Which logo?")
        self.deliver()
        self.b.api.s.run("UPDATE tasks SET waiting_since = ? WHERE id = ?", int(time.time()) - 13 * 3600, tid)
        self.b.api.escalate()
        self.b.api.escalate()
        got = self.deliver()
        self.assertEqual([(g[0], g[2]["event"]) for g in got], [("/web", "reminder")])
        self.b.api.s.run("UPDATE tasks SET waiting_since = ? WHERE id = ?", int(time.time()) - 25 * 3600, tid)
        self.b.api.escalate()
        self.b.api.escalate()
        got = self.deliver()
        self.assertEqual([(g[0], g[2]["event"]) for g in got], [("/chief", "escalated")])
        items = [i for i in self.b.req("GET", "/waiting", tok=self.chief)["items"] if i.get("task_id") == tid]
        self.assertEqual(len(items), 1)
        # Answering it from the list answers the task.
        self.b.req("POST", f"/waiting/{items[0]['id']}", {"action": "answer", "answer": "The round one"}, tok=self.chief)
        t = self.b.req("GET", f"/tasks/{tid}")["task"]
        self.assertEqual((t["state"], t["pending_answer"]), ("queued", "The round one"))
        self.b.req("POST", f"/tasks/{tid}/cancel", {}, tok=self.web)
        self.deliver()

    def test_watchdog_alerts_once(self):
        tid = self.b.req("POST", "/tasks", {"prompt": "nobody runs me"}, tok=self.web)["task"]["id"]
        self.b.api.s.run("UPDATE tasks SET created_at = ?, not_before = 0 WHERE id = ?", int(time.time()) - 3600, tid)
        self.b.api.s.run("UPDATE workers SET last_seen = ?", int(time.time()) - 3600)
        self.b.api.started_at = int(time.time()) - 3600
        self.b.api.watchdog()
        self.b.api.watchdog()
        got = self.deliver()
        self.assertEqual([(g[0], g[2]["event"]) for g in got], [("/chief", "watchdog")])
        # A task running past its time limit gets one alert per claim.
        c = self.b.claim()
        self.b.api.s.run("UPDATE tasks SET claimed_at = ? WHERE id = ?", int(time.time()) - 100000, c["id"])
        self.b.api.watchdog()
        self.b.api.watchdog()
        got = [g for g in self.deliver() if "running for" in g[2].get("text", "")]
        self.assertEqual(len(got), 1)
        self.b.finish(c, "done")
        self.deliver()


if __name__ == "__main__":
    unittest.main()
