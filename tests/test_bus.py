"""The Agent Bus (server/swarm_bus.py): mirrors agent-control's bus.test.mjs, bus_review_fixes.test.mjs and bus_review_round2.test.mjs.

Kit differences: whole-second timestamps, no cron (`api.bus.flush(now=...)` is the cron), a fake webhook injected as
`api.bus.poster`. Only fake values are used: 127.0.0.1, example.com and a made-up token string.
"""
import json
import re
import time
import unittest

from helpers import ADMIN, WORKER, Backend, sb

FAKE_SECRET = "sk-ant-" + "x" * 30   # matches the sk- pattern in config/secret-patterns.txt; not a real key
HOOK = "http://127.0.0.1:9/clozy"
MIN = 60
HOUR = 3600
DAY = 86400
EVIL = "ok\x1b[201~rm -rf /\r\nline2\ttab\x1b\x07\x00\u0085\u009b\x7fend"
CLEAN = "ok[201~rm -rf /\nline2\ttabend"
CTRL = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f]")


def beat(b, name, **body):
    body.setdefault("worker", "w1")
    body.setdefault("supervisor_id", "sup-aaaaaaaa")
    return b.req("POST", f"/worker/orchestrators/{name}/heartbeat", body, tok=WORKER)


class BusCase(unittest.TestCase):
    """chief (poll, seen), clozy (webhook), polisher (tmux, running), gil (human, poll), quiet (service, poll)."""
    orch_name = "polisher"

    def setUp(self):
        self.b = Backend()
        self.addCleanup(self.b.close)
        self.api = self.b.api
        self.bus = self.api.bus
        self.posts = []
        self.status = 200
        self.bus.poster = self.poster
        self.t0 = int(time.time())
        self.b.bot("clozy-bot", url=HOOK)
        self.b.bot("app-bot", projects=("app",))
        self.b.req("POST", "/worker/claim", {"worker": "w1"}, tok=WORKER)
        self.add_orch(self.orch_name)
        for name, body in (("clozy", {"kind": "bot", "wake": "webhook", "bot_id": "clozy-bot", "aliases": ["clozy-bot@relay", "@Clozy-Old"]}),
                           ("chief", {"kind": "bot", "wake": "poll"}),
                           ("polisher", {"kind": "orchestrator", "wake": "tmux", "orchestrator": self.orch_name}),
                           ("gil", {"kind": "human", "wake": "poll"}),
                           ("quiet", {"kind": "service", "wake": "poll"})):
            self.b.req("PUT", f"/bus/agents/{name}", body, expect=201)
        self.tok = {n: self.b.req("GET", f"/bus/agents/{n}/token")["token"] for n in ("clozy", "chief", "polisher", "gil", "quiet")}
        self.b.req("GET", "/bus/agents", tok=self.tok["chief"])   # chief checks in

    def add_orch(self, name):
        self.b.req("POST", "/orchestrators", {"name": name, "worker_id": "w1", "project": "app", "owner_bot_id": "app-bot",
                                               "brief": "Keep improving the app.", "relay_agent_name": None, "start": True})
        beat(self.b, name, state="running")

    # fake webhook
    def poster(self, url, data, headers):
        self.posts.append({"url": url, "body": json.loads(data), "headers": headers})
        if self.status == "raise":
            raise OSError("connection refused")
        return self.status, b"{}"

    def send(self, who, expect=None, **body):
        status, out = self.b.call("POST", "/bus/send", body, tok=self.tok.get(who, who))
        if expect is not None:
            self.assertEqual(status, expect, out)
        return out if expect is not None else (status, out)

    def get(self, path, who=ADMIN, expect=200):
        return self.b.req("GET", path, tok=self.tok.get(who, who), expect=expect)

    def rows(self, sql, *a):
        return self.api.s.q(sql, *a)

    def msg(self, mid):
        return self.api.s.one("SELECT * FROM bus_messages WHERE id = ?", mid)

    def agent(self, name):
        return self.api.s.one("SELECT * FROM agents WHERE name = ?", name)

    def flush(self, dt=0, **kw):
        return self.bus.flush(now=self.t0 + dt, **kw)

    def alerts(self, where="1"):
        return self.rows(f"SELECT * FROM bus_messages WHERE sender = 'agent-control' AND {where} ORDER BY id")

    def count(self):
        return self.api.s.one("SELECT COUNT(*) AS n FROM bus_messages")["n"]

    def age(self, mid, seconds):
        self.api.s.run("UPDATE bus_messages SET created_at = ? WHERE id = ?", self.t0 - seconds, mid)

    def fill(self, recipient, n=100, held=1, sender="x", created=None):
        for _ in range(n):
            self.api.s.run("INSERT INTO bus_messages(sender, recipient, thread, body, held, state, created_at) VALUES(?,?,'fill','x',?,'queued',?)",
                           sender, recipient, held, created or self.t0)


class RegistrationTest(BusCase):
    def test_register_validation(self):
        put = lambda n, body, expect, tok=ADMIN: self.b.req("PUT", f"/bus/agents/{n}", body, tok=tok, expect=expect)  # noqa: E731
        self.b.bot("nourl")
        self.api.s.run("INSERT OR REPLACE INTO bots(id, role, notify_url, created_at) VALUES('badurl', 'project', 'ftp://example.com/x', 1)") \
            if False else None
        put("zz", {"kind": "bot", "wake": "webhook", "bot_id": "nope"}, 400)
        put("zz", {"kind": "bot", "wake": "webhook", "bot_id": "nourl"}, 400)
        put("zz", {"kind": "bot", "wake": "webhook"}, 400)
        put("zz", {"kind": "orchestrator", "wake": "tmux", "orchestrator": "missing"}, 400)
        put("zz", {"kind": "orchestrator", "wake": "tmux"}, 400)
        put("zz", {"kind": "robot", "wake": "poll"}, 400)
        put("zz", {"kind": "bot", "wake": "pigeon"}, 400)
        put("zz", {"kind": "bot", "wake": "poll", "notices": "maybe"}, 400)
        put("zz", {"kind": "bot", "wake": "poll", "aliases": "nope"}, 400)
        put("zz", {"kind": "bot", "wake": "poll", "aliases": ["bad alias"]}, 400)
        put("C", {"kind": "bot", "wake": "poll"}, 400)
        put("z", {"kind": "bot", "wake": "poll"}, 400)
        put("x" * 33, {"kind": "bot", "wake": "poll"}, 400)
        put("zz", {"kind": "bot", "wake": "webhook", "bot_id": "clozy-bot"}, 403, tok=WORKER)
        put("zz", {"kind": "bot", "wake": "webhook", "bot_id": "clozy-bot"}, 403, tok=self.tok["chief"])
        self.assertIsNone(self.agent("zz"))

    def test_notify_url_must_be_valid_for_webhook_agents(self):
        self.api.s.run("UPDATE bots SET notify_url = 'ftp://example.com/x' WHERE id = 'app-bot'")
        self.b.req("PUT", "/bus/agents/zz", {"kind": "bot", "wake": "webhook", "bot_id": "app-bot"}, expect=400)

    def test_reserved_name(self):
        self.b.req("PUT", "/bus/agents/agent-control", {"kind": "service", "wake": "poll"}, expect=400)

    def test_aliases_share_the_address_space_and_replace(self):
        a = self.b.req("PUT", "/bus/agents/zed", {"kind": "bot", "wake": "poll", "aliases": ["Old-Zed", "OLD-zed"]}, expect=201)
        self.assertEqual(a["aliases"], ["old-zed"])
        self.assertNotIn("notify", json.dumps(a))
        put = lambda n, body, expect: self.b.req("PUT", f"/bus/agents/{n}", body, expect=expect)  # noqa: E731
        put("chief", {"aliases": ["old-zed"]}, 409)          # alias used by another agent
        put("chief", {"aliases": ["clozy"]}, 409)            # alias equal to another agent's name
        put("old-zed", {"kind": "bot", "wake": "poll"}, 409)  # name equal to an alias
        put("chief", {"aliases": ["boss"]}, 200)
        put("chief", {"aliases": ["lead"]}, 200)
        self.assertEqual([r["alias"] for r in self.rows("SELECT alias FROM agent_aliases WHERE name = 'chief'")], ["lead"])
        self.assertEqual(self.get("/bus/agents")["agents"][0]["name"], "chief")

    def test_digest_minutes_floor(self):
        for v in (0, 1, 4, -1, True, "5", 10 ** 9):
            self.b.req("PUT", "/bus/agents/clozy", {"digest_minutes": v}, expect=400)
        self.assertEqual(self.b.req("PUT", "/bus/agents/clozy", {"digest_minutes": 5})["digest_minutes"], 5)
        self.api.s.run("UPDATE agents SET digest_minutes = 0 WHERE name = 'clozy'")   # legacy row
        self.assertEqual([a for a in self.get("/bus/agents")["agents"] if a["name"] == "clozy"][0]["digest_minutes"], 5)

    def test_retire_hides_agent_and_bumps_nothing_else(self):
        self.b.req("DELETE", "/bus/agents/quiet")
        self.b.req("DELETE", "/bus/agents/quiet", expect=404)
        self.assertNotIn("quiet", [a["name"] for a in self.get("/bus/agents")["agents"]])
        self.send("chief", expect=410, to="quiet", body="hi")


class TokenTest(BusCase):
    def test_token_scoping_and_rotation(self):
        self.assertRegex(self.tok["clozy"], r"^ab_clozy\.1\.[0-9a-f]{64}$")
        self.get("/bus/agents", "clozy")
        for bad in ("ab_clozy.1.deadbeef", "nope", "ab_clozy.x.y", "ab_nobody.1.abc"):
            self.b.req("GET", "/bus/agents", tok=bad, expect=401)
        self.assertEqual(self.bus.token_for("clozy"), self.tok["clozy"])
        # an agent token cannot mint, rotate, register or retire
        self.b.req("GET", "/bus/agents/chief/token", tok=self.tok["clozy"], expect=403)
        self.b.req("POST", "/bus/agents/clozy/token/rotate", tok=self.tok["clozy"], expect=403)
        self.b.req("PUT", "/bus/agents/clozy", {"description": "x"}, tok=self.tok["clozy"], expect=403)
        self.b.req("DELETE", "/bus/agents/chief", tok=self.tok["clozy"], expect=403)
        fresh = self.b.req("POST", "/bus/agents/clozy/token/rotate")["token"]
        self.assertRegex(fresh, r"^ab_clozy\.2\.")
        self.b.req("GET", "/bus/agents", tok=self.tok["clozy"], expect=401)
        self.get("/bus/agents", fresh)
        self.b.req("DELETE", "/bus/agents/clozy")
        self.b.req("GET", "/bus/agents", tok=fresh, expect=401)
        self.send("chief", expect=410, to="clozy", body="hi")
        self.b.req("GET", "/bus/agents/clozy/token", expect=410)
        self.b.req("POST", "/bus/agents/clozy/token/rotate", expect=410)
        self.assertGreater(self.agent("chief")["last_seen_at"], 0)

    def test_agent_token_acts_as_itself_only(self):
        out = self.send("chief", expect=201, to="quiet", body="hi", **{"from": "gil"})
        self.assertEqual(out["from"], "chief")
        out = self.send(ADMIN, expect=201, to="chief", body="as alias", **{"from": "@Clozy-Old"})
        self.send(ADMIN, expect=201, to="chief", body="free", **{"from": "Gil <3>"})
        self.assertEqual(out["from"], "clozy")
        self.assertEqual([r["sender"] for r in self.rows("SELECT sender FROM bus_messages WHERE body IN ('as alias','free') ORDER BY id")], ["clozy", "Gil 3"])

    def test_worker_token_only_for_pinned_orchestrator_agent(self):
        w = self.b.req("GET", "/bus/agents/polisher/token?worker=w1", tok=WORKER)
        self.assertEqual(w["token"], self.tok["polisher"])
        self.b.req("GET", "/bus/agents/polisher/token", tok=WORKER, expect=400)   # the worker name is required
        self.b.req("GET", "/bus/agents/polisher/token?worker=w2", tok=WORKER, expect=404)
        self.b.req("GET", "/bus/agents/chief/token?worker=w1", tok=WORKER, expect=404)
        self.b.req("GET", "/bus/agents/clozy/token", tok=WORKER, expect=404)
        self.b.req("GET", "/bus/agents/nobody/token", tok=WORKER, expect=404)
        self.b.req("POST", "/bus/agents/polisher/token/rotate", tok=WORKER, expect=403)
        self.b.req("POST", "/bus/send", {"to": "chief", "body": "x"}, tok=WORKER, expect=403)
        self.b.req("GET", "/bus/inbox", tok=WORKER, expect=403)

    def test_no_token_without_agent(self):
        self.b.req("GET", "/bus/agents/ghost/token", expect=404)
        self.b.req("POST", "/bus/agents/ghost/token/rotate", expect=404)

    def test_token_strings_only_in_the_token_response(self):
        self.b.bot("plain")   # bot tokens are not bus tokens
        toks = list(self.tok.values())
        a = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        self.flush()
        seen = [self.b.call("PUT", "/bus/agents/clozy", {"description": "d"})[1], self.get("/bus/agents"), self.get(f"/bus/messages/{a['id']}"),
                self.get("/bus/threads/" + a["thread"]), self.get("/bus/inbox?agent=chief&all=1"),
                self.b.call("GET", "/bus", tok=ADMIN)[1], self.b.call("DELETE", "/bus/agents/quiet")[1]]
        dump = "\n".join(json.dumps(x) if not isinstance(x, bytes) else x.decode() for x in seen)
        dump += "\n".join(json.dumps(self.posts)) + "\n".join(str(dict(r)) for r in self.rows("SELECT * FROM bus_messages"))
        for t in toks:
            self.assertNotIn(t, dump)
            self.assertNotIn(t.split(".")[2], dump)

    def test_secret_is_separate_from_the_admin_token(self):
        self.get("/bus/agents", "clozy")
        path = self.b.tmp + "/bus-token-secret"
        with open(path) as fh:
            sec = fh.read().strip()
        self.assertGreaterEqual(len(sec), 32)
        self.assertNotIn(ADMIN, sec)
        self.api.admin_token = "rotated-admin-token-for-tests"
        self.assertTrue(self.bus.agent_from_token(self.tok["clozy"]))   # rotating the admin token leaves bus tokens alone
        self.bus._secret = "other-secret-".ljust(40, "y")
        self.assertIsNone(self.bus.agent_from_token(self.tok["clozy"]))


class SendValidationTest(BusCase):
    def test_empty_oversize_secret_and_bad_fields_are_refused_and_not_stored(self):
        before = self.count()
        self.send("clozy", expect=400, to="chief", body="")
        self.send("clozy", expect=400, to="chief", body="   \n")
        self.send("clozy", expect=400, to="chief")
        self.send("clozy", expect=400, to="chief", body=5)
        self.send("clozy", expect=400, to="chief", body="\x1b\r\x00")
        big = self.send("clozy", expect=413, to="chief", body="x" * 3701)
        self.assertIn("put long text in a file", big["error"])
        self.send("clozy", expect=400, to="chief", body=f"key {FAKE_SECRET}")
        self.send("clozy", expect=400, to="chief", body="ok", meta={"k": FAKE_SECRET})
        self.send("clozy", expect=400, to="chief", body="ok", meta=["list"])
        self.send("clozy", expect=400, to="chief", body="ok", meta="text")
        self.send("clozy", expect=413, to="chief", body="ok", meta={"k": "x" * 9000})
        self.send("clozy", expect=400, to="chief", body="x", thread="Bad Thread")
        self.send("clozy", expect=400, to="chief", body="x", thread="-leading")
        self.send("clozy", expect=400, to="chief", body="x", client_msg_id="a b")
        self.send("clozy", expect=400, to="chief", body="x", client_msg_id="a" * 101)
        self.send("clozy", expect=201, to="chief", body="x" * 3700)
        self.assertEqual(self.count(), before + 1)

    def test_meta_round_trip_and_limits(self):
        self.send("clozy", expect=201, to="chief", body="meta", meta={"a": 1})
        msgs = self.get("/bus/inbox", "chief")["messages"]
        self.assertEqual(msgs[-1]["meta"], {"a": 1})
        self.send("clozy", expect=201, to="chief", body="meta", meta={"k": "x" * 7000})

    def test_control_characters_are_stripped_everywhere(self):
        for to in ("polisher", "clozy", "chief"):
            self.send("gil", expect=201, to=to, body=EVIL, urgent=True)
        self.flush()
        for m in self.rows("SELECT body FROM bus_messages"):
            self.assertIsNone(CTRL.search(m["body"]))
            self.assertEqual(m["body"], CLEAN)
        om = self.rows("SELECT text FROM orchestrator_messages WHERE bus_message_id IS NOT NULL")[0]["text"]
        self.assertNotIn("\r", om)
        self.assertIsNone(re.search("[\u0000-\u0008\u000b-\u000c\u000e-\u001f\u007f-\u009f]", om))
        self.assertTrue(om.endswith(CLEAN))
        self.assertEqual(self.posts[0]["body"]["body"], CLEAN)

    def test_legacy_orchestrator_message_also_strips_control_characters(self):
        r = self.b.req("POST", "/orchestrators/polisher/messages", {"text": EVIL})
        self.assertEqual(self.api.s.one("SELECT text FROM orchestrator_messages WHERE id = ?", r["id"])["text"], CLEAN)
        self.assertEqual(self.msg(r["bus_id"])["body"], CLEAN)

    def test_longest_message_fits_the_supervisor_limit(self):
        long_orch = "o" * 32
        self.add_orch(long_orch)
        self.b.req("PUT", f"/bus/agents/{long_orch}", {"kind": "orchestrator", "wake": "tmux", "orchestrator": long_orch}, expect=201)
        sender = "a" * 32
        self.b.req("PUT", f"/bus/agents/{sender}", {"kind": "service", "wake": "poll"}, expect=201)
        self.tok[sender] = self.b.req("GET", f"/bus/agents/{sender}/token")["token"]
        thread = "t" * 80
        self.send(sender, expect=413, to=long_orch, thread=thread, body="x" * 3701)
        self.api.s.run("UPDATE sqlite_sequence SET seq = 999999999 WHERE name = 'bus_messages'")   # the biggest ids make the biggest header
        self.send(sender, expect=201, to=long_orch, thread=thread, body="x" * 3700)
        om = self.rows("SELECT text FROM orchestrator_messages WHERE name = ? AND bus_message_id IS NOT NULL", long_orch)[0]["text"]
        self.assertLessEqual(len(om), 4000)
        self.assertTrue(om.endswith("x" * 3700))

    def test_unknown_name_404_with_did_you_mean_and_aliases_resolve(self):
        for to in ("clozy-bot@relay", " @Clozy-Old ", "CLOZY"):
            self.assertEqual(self.send("chief", expect=201, to=to, body="via")["to"], "clozy")
        before = self.count()
        status, out = self.send("chief", to="cloz", body="x")
        self.assertEqual(status, 404)
        self.assertRegex(out["error"], "unknown agent cloz")
        self.assertEqual(out["did_you_mean"], ["clozy"])
        self.assertEqual(self.send("chief", to="zzzz-nothing", body="x")[1]["did_you_mean"], [])
        self.assertEqual(self.count(), before)

    def test_default_thread_is_a_sorted_dm(self):
        out = self.send("clozy", expect=201, to="chief", body="hi")
        self.assertEqual(out["thread"], "dm:chief+clozy")
        self.assertEqual(self.send("chief", expect=201, to="clozy", body="hi")["thread"], "dm:chief+clozy")

    def test_idempotent_client_msg_id(self):
        a = self.send("chief", expect=201, to="quiet", body="once", client_msg_id="c-1")
        d = self.send("chief", expect=200, to="quiet", body="once", client_msg_id="c-1")
        self.assertTrue(d["duplicate"])
        self.assertEqual(d["id"], a["id"])
        self.assertEqual(self.count(), 1)
        # the key is per sender
        self.assertNotIn("duplicate", self.send("gil", expect=201, to="quiet", body="once", client_msg_id="c-1"))

    def test_queue_limit_per_recipient_is_100(self):
        self.fill("chief", 100)
        self.send("clozy", expect=429, to="chief", body="one more")
        self.send("clozy", expect=201, to="quiet", body="other recipient is fine")

    def test_noop_cannot_be_urgent(self):
        self.send("clozy", expect=400, to="chief", body="hb", noop=True, urgent=True)
        self.send("clozy", expect=400, to="chief", body="hb", noop=True, meta={"blocker": True})


class WebhookDeliveryTest(BusCase):
    def test_send_stores_then_flush_posts_payload_and_logs_attempt(self):
        out = self.send("chief", expect=201, to="clozy", body="ship it", client_msg_id="c-1", urgent=True)
        self.assertEqual((out["to"], out["from"], out["thread"], out["state"], out["wake"]), ("clozy", "chief", "dm:chief+clozy", "queued", "now"))
        self.assertTrue(self.bus.wake.is_set())
        self.assertEqual(self.posts, [])
        self.assertEqual(self.flush()["retried"], 1)
        self.assertEqual(len(self.posts), 1)
        p = self.posts[0]
        self.assertEqual(p["url"], HOOK)
        self.assertEqual(p["headers"]["Idempotency-Key"], f"bus:{out['id']}")
        body = p["body"]
        self.assertEqual(body, {
            "kind": "bus_message", "idempotency_key": f"bus:{out['id']}", "id": out["id"], "from": "chief", "to": "clozy",
            "thread": "dm:chief+clozy", "body": "ship it", "urgent": True, "reply_expected": True, "created_at": body["created_at"],
            "meta": None, "newer_in_thread": 0, "thread_latest_id": out["id"],
            "ack": {"method": "POST", "path": f"/bus/messages/{out['id']}/ack"},
            "reply": {"method": "POST", "path": "/bus/send", "to": "chief", "thread": "dm:chief+clozy"}, "etiquette": body["etiquette"]})
        g = self.get(f"/bus/messages/{out['id']}", "chief")
        self.assertEqual(g["state"], "delivered")
        self.assertTrue(g["delivered_at"])
        self.assertEqual([(a["transport"], a["ok"], a["http_status"]) for a in g["attempts"]], [("webhook", True, 200)])
        # duplicate: same message, no second webhook
        d = self.send("chief", expect=200, to="clozy", body="ship it", client_msg_id="c-1", urgent=True)
        self.flush(5)
        self.assertEqual((d["duplicate"], d["id"], len(self.posts), self.count()), (True, out["id"], 1, 1))

    def test_bot_key_is_sent_as_authorization(self):
        self.b.req("PUT", "/bots/clozy-bot", {"role": "project", "notify_url": HOOK, "notify_auth": "fake-trigger-key"})
        self.send("chief", expect=201, to="clozy", body="x", urgent=True)
        self.flush()
        self.assertEqual(self.posts[0]["headers"].get("Authorization"), "Bearer fake-trigger-key")
        self.assertNotIn("fake-trigger-key", json.dumps(self.posts[0]["body"]))

    def test_failure_schedules_backoff_and_retry_delivers(self):
        self.status = 500
        out = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        self.flush()
        m = self.msg(out["id"])
        self.assertEqual((m["state"], m["attempts"], m["last_error"], m["next_attempt_at"]), ("queued", 1, "HTTP 500", self.t0 + 60))
        self.assertEqual(len(self.rows("SELECT * FROM bus_attempts WHERE message_id = ? AND ok = 0", out["id"])), 1)
        self.flush(30)
        self.assertEqual(len(self.posts), 1)   # not due yet
        self.status = 200
        self.assertEqual(self.flush(61)["retried"], 1)
        self.assertEqual(len(self.posts), 2)
        m = self.msg(out["id"])
        self.assertEqual((m["state"], m["next_attempt_at"], m["last_error"]), ("delivered", None, None))
        self.assertEqual(len(self.rows("SELECT * FROM bus_attempts WHERE message_id = ? AND ok = 1", out["id"])), 1)

    def test_network_error_is_retried(self):
        self.status = "raise"
        out = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        self.flush()
        m = self.msg(out["id"])
        self.assertEqual((m["state"], m["attempts"]), ("queued", 1))
        self.assertIn("connection refused", m["last_error"])

    def test_backoff_steps_then_failed_and_sender_alerted_once(self):
        self.status = 503
        out = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        t, expect_next = 0, [60, 300, 900, 3600]
        for i, gap in enumerate(expect_next):
            self.flush(t)
            m = self.msg(out["id"])
            self.assertEqual((m["state"], m["attempts"], m["next_attempt_at"]), ("queued", i + 1, self.t0 + t + gap))
            t += gap + 1
        self.flush(t)
        m = self.msg(out["id"])
        self.assertEqual((m["state"], m["attempts"], m["next_attempt_at"]), ("failed", 5, None))
        self.assertTrue(m["alerted_at"])
        self.assertEqual(len(self.posts), 5)
        al = self.alerts()
        self.assertEqual(len(al), 1)   # the 5 minute urgent alert already told the sender; not repeated on failure
        self.assertEqual((al[0]["recipient"], al[0]["client_msg_id"]), ("chief", f"urgent:{out['id']}"))
        self.assertRegex(al[0]["body"], f"#{out['id']} to clozy")
        self.flush(t + 1000)
        self.assertEqual(len(self.alerts("recipient = 'chief' AND client_msg_id LIKE '%:%'")), 1)
        self.assertTrue(any(x["from"] == "agent-control" for x in self.get("/bus/inbox", "chief")["messages"]))

    def test_now_message_fails_by_age_after_two_hours_and_sender_is_told(self):
        self.status = 500
        out = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        self.age(out["id"], 3 * HOUR)
        self.flush()
        self.assertEqual(self.msg(out["id"])["state"], "failed")
        self.assertEqual(len(self.posts), 1)
        al = self.alerts("recipient = 'chief'")
        self.assertEqual([a["client_msg_id"] for a in al], [f"failed:{out['id']}"])
        self.assertRegex(al[0]["body"], "failed: HTTP 500")

    def test_lease_prevents_double_send(self):
        out = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        self.api.s.run("UPDATE bus_messages SET lease_until = ? WHERE id = ?", self.t0 + 60, out["id"])
        self.assertIsNone(self.bus.webhook_round(out["id"], now=self.t0))
        self.assertEqual(self.flush()["retried"], 0)
        self.assertEqual(self.posts, [])
        self.assertTrue(self.bus.webhook_round(out["id"], now=self.t0 + 61)["ok"])
        self.assertIsNone(self.bus.webhook_round(out["id"], now=self.t0 + 62))   # already delivered
        self.assertEqual(len(self.posts), 1)

    def test_orphan_with_expired_lease_is_picked_up(self):
        out = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        self.api.s.run("UPDATE bus_messages SET lease_until = ? WHERE id = ?", self.t0 - 10, out["id"])   # a crashed round left it
        self.flush()
        self.assertEqual((self.msg(out["id"])["state"], len(self.posts)), ("delivered", 1))

    def test_webhook_round_refuses_message_older_than_24h(self):
        out = self.send("chief", expect=201, to="clozy", body="hello", urgent=True)
        self.age(out["id"], 25 * HOUR)
        self.assertEqual(self.bus.webhook_round(out["id"], now=self.t0)["error"], "expired")
        self.assertEqual(self.posts, [])
        m = self.msg(out["id"])
        self.assertEqual((m["state"], m["last_error"]), ("failed", "not picked up within 24 hours; expired"))


class WakePolicyTest(BusCase):
    def test_wake_types(self):
        self.assertEqual(self.send("chief", expect=201, to="clozy", body="a", urgent=True)["wake"], "now")
        self.assertEqual(self.send("chief", expect=201, to="clozy", body="b", meta={"blocker": True})["wake"], "now")
        self.assertEqual(self.send("chief", expect=201, to="clozy", body="c")["wake"], "digest")
        self.assertEqual(self.send("clozy", expect=201, to="chief", body="d")["wake"], "inbox")
        self.assertEqual(self.send("chief", expect=201, to="polisher", body="e")["wake"], "now")
        noop = self.send("chief", expect=201, to="clozy", body="hb", noop=True)
        self.assertEqual((noop["wake"], noop["state"]), ("never", "delivered"))
        held = self.rows("SELECT held FROM bus_messages WHERE body = 'c'")[0]["held"]
        self.assertEqual(held, 1)

    def test_orchestrator_question_is_urgent(self):
        out = self.send("polisher", expect=201, to="clozy", body="need X?", reply_expected=True)
        self.assertEqual(out["wake"], "now")
        self.assertTrue(self.msg(out["id"])["urgent"])

    def test_digest_waits_for_interval_then_sends_one_post(self):
        a = self.send("chief", expect=201, to="clozy", body="a")["id"]
        b = self.send("chief", expect=201, to="clozy", body="b")["id"]
        self.flush(30 * MIN)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.flush(61 * MIN)["digests"], 1)
        self.assertEqual(len(self.posts), 1)
        p = self.posts[0]["body"]
        self.assertEqual((p["kind"], p["count"], [m["id"] for m in p["messages"]]), ("bus_digest", 2, [a, b]))
        self.assertEqual({self.msg(a)["state"], self.msg(b)["state"]}, {"delivered"})
        self.flush(130 * MIN)
        self.assertEqual(len(self.posts), 1)

    def test_failed_digest_does_not_count_and_is_retried(self):
        a = self.send("chief", expect=201, to="clozy", body="a")["id"]
        self.status = 500
        self.flush(61 * MIN)
        self.assertIsNone(self.agent("clozy")["last_digest_at"])
        self.assertEqual((self.msg(a)["state"], self.msg(a)["attempts"]), ("queued", 1))
        self.status = 200
        self.flush(61 * MIN + 61)
        self.assertEqual(self.msg(a)["state"], "delivered")

    def test_urgent_delivery_carries_pending_held_messages(self):
        h = self.send("chief", expect=201, to="clozy", body="held")["id"]
        u = self.send("chief", expect=201, to="clozy", body="now", urgent=True)["id"]
        self.flush()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual((self.posts[0]["body"]["pending_count"], self.posts[0]["body"]["pending"][0]["id"]), (1, h))
        self.assertEqual({self.msg(h)["state"], self.msg(u)["state"]}, {"delivered"})

    def test_digest_minimum_of_five_applies_to_a_stored_zero(self):
        self.api.s.run("UPDATE agents SET digest_minutes = 0 WHERE name = 'clozy'")
        self.send("chief", expect=201, to="clozy", body="a")
        self.flush(2 * MIN)
        self.assertEqual(len(self.posts), 0)
        self.flush(6 * MIN)
        self.assertEqual(len(self.posts), 1)
        self.send("chief", expect=201, to="clozy", body="b")
        self.flush(7 * MIN)
        self.assertEqual(len(self.posts), 1)   # one minute after the last digest: not again

    def test_noop_and_held_do_not_use_up_the_immediate_wake_for_an_answer(self):
        self.send("clozy", expect=201, to="chief", thread="q1", body="need X?", reply_expected=True)
        self.assertEqual(self.send("chief", expect=201, to="clozy", thread="q1", body="hb", noop=True)["wake"], "never")
        self.assertEqual(self.send("chief", expect=201, to="clozy", thread="q1", body="here is X")["wake"], "now")
        self.assertEqual(self.send("chief", expect=201, to="clozy", thread="q1", body="more")["wake"], "digest")

    def test_held_claim_requires_created_within_24h(self):
        old = self.send("chief", expect=201, to="clozy", body="old")["id"]
        fresh = self.send("chief", expect=201, to="clozy", body="fresh")["id"]
        self.age(old, 25 * HOUR)
        self.api.s.run("UPDATE bus_messages SET lease_until = NULL WHERE id = ?", old)
        rows = self.bus.claim_held("clozy", self.t0, expire=False)
        self.assertEqual([m["id"] for m in rows], [fresh])
        self.assertIsNone(self.msg(old)["lease_until"])
        self.assertEqual(self.msg(old)["state"], "queued")
        # with expire (the default) the old one is failed first
        self.bus.claim_held("clozy", self.t0)
        self.assertEqual(self.msg(old)["state"], "failed")


class PollAndAckTest(BusCase):
    def test_inbox_marks_delivered_logs_attempt_and_ack_reports_staleness(self):
        a = self.send("polisher", expect=201, to="chief", body="HOLD", thread="task:t1")
        b = self.send("polisher", expect=201, to="chief", body="GO", thread="task:t1")
        self.assertEqual(a["state"], "queued")
        inbox = self.get("/bus/inbox", "chief")
        self.assertEqual([(m["id"], m["newer_in_thread"], m["thread_latest_id"], m["state"]) for m in inbox["messages"]],
                         [(a["id"], 1, b["id"], "delivered"), (b["id"], 0, b["id"], "delivered")])
        self.assertEqual(len(self.rows("SELECT * FROM bus_attempts WHERE transport = 'poll' AND ok = 1")), 2)
        self.get("/bus/inbox", "chief")
        self.assertEqual(len(self.rows("SELECT * FROM bus_attempts WHERE transport = 'poll'")), 2)   # logged once
        r = self.b.req("POST", f"/bus/messages/{a['id']}/ack", tok=self.tok["chief"])
        self.assertEqual(r, {"ok": True, "id": a["id"], "thread": a["thread"], "first": True, "newer_in_thread": 1, "thread_latest_id": b["id"]})
        self.assertFalse(self.b.req("POST", f"/bus/messages/{a['id']}/ack", tok=self.tok["chief"])["first"])
        self.assertEqual([m["id"] for m in self.get("/bus/inbox", "chief")["messages"]], [b["id"]])
        self.assertEqual([m["id"] for m in self.get("/bus/inbox?all=1&limit=1", "chief")["messages"]], [a["id"]])
        self.assertEqual([m["id"] for m in self.get(f"/bus/inbox?all=1&after={a['id']}", "chief")["messages"]], [b["id"]])
        self.assertEqual(len(self.get("/bus/threads/task:t1", "chief")["messages"]), 2)
        self.assertTrue(self.msg(a["id"])["read_at"])
        self.assertEqual(self.msg(a["id"])["state"], "read")

    def test_message_detail_has_attempts(self):
        a = self.send("polisher", expect=201, to="chief", body="x")
        self.get("/bus/inbox", "chief")
        g = self.get(f"/bus/messages/{a['id']}", "polisher")
        self.assertEqual([(x["transport"], x["ok"]) for x in g["attempts"]], [("poll", True)])
        self.assertEqual((g["from"], g["to"], g["state"]), ("polisher", "chief", "delivered"))

    def test_access_control(self):
        a = self.send("polisher", expect=201, to="chief", body="private", thread="x-1")
        self.get(f"/bus/messages/{a['id']}", "clozy", expect=404)
        self.b.req("POST", f"/bus/messages/{a['id']}/ack", tok=self.tok["clozy"], expect=404)
        self.b.req("POST", f"/bus/messages/{a['id']}/ack", tok=self.tok["polisher"], expect=404)   # the sender cannot ack
        self.get(f"/bus/messages/{a['id']}", "polisher")
        self.get("/bus/messages/9999", "polisher", expect=404)
        self.get("/bus/messages/abc", expect=404)
        self.assertEqual(self.get("/bus/threads/x-1", "clozy")["messages"], [])
        self.assertEqual(len(self.get("/bus/threads/x-1", "polisher")["messages"]), 1)
        self.assertEqual(len(self.get("/bus/threads/x-1")["messages"]), 1)
        self.b.req("GET", "/bus/inbox", expect=400)
        self.assertEqual(len(self.get("/bus/inbox?agent=chief")["messages"]), 1)
        self.assertEqual(self.get("/bus/inbox", "clozy")["messages"], [])
        self.b.req("POST", f"/bus/messages/{a['id']}/ack")
        self.b.req("GET", "/bus/messages/1", tok=WORKER, expect=403)


class TmuxTest(BusCase):
    def om_rows(self):
        return self.rows("SELECT * FROM orchestrator_messages WHERE bus_message_id IS NOT NULL ORDER BY id")

    def test_send_queues_a_pane_message_and_heartbeat_confirms_it(self):
        out = self.send("chief", expect=201, to="polisher", body="please rebase", thread="task:t9")
        self.assertEqual((out["state"], out.get("warning")), ("queued", None))
        om = self.om_rows()
        self.assertEqual((len(om), om[0]["bus_message_id"], om[0]["sender"], om[0]["name"]), (1, out["id"], "chief", "polisher"))
        head, _, rest = om[0]["text"].partition("\n\n")
        self.assertEqual(rest, "please rebase")
        self.assertIn(f"bus #{out['id']} thread task:t9", head)
        self.assertIn(f"swarmctl bus ack {out['id']}", head)
        self.assertIn('swarmctl bus send chief "..."', head)
        hb = beat(self.b, "polisher", state="running")
        self.assertEqual((hb["message"]["id"], hb["message"]["text"]), (om[0]["id"], om[0]["text"]))
        self.assertEqual(self.msg(out["id"])["state"], "queued")
        beat(self.b, "polisher", state="running", delivered_message_id=om[0]["id"])
        g = self.get(f"/bus/messages/{out['id']}", "chief")
        self.assertEqual(g["state"], "delivered")
        self.assertEqual([(a["transport"], a["ok"]) for a in g["attempts"]], [("tmux", True)])
        beat(self.b, "polisher", state="running", delivered_message_id=om[0]["id"])   # again: no second attempt
        self.assertEqual(len(self.get(f"/bus/messages/{out['id']}", "chief")["attempts"]), 1)
        self.assertTrue(self.b.req("POST", f"/bus/messages/{out['id']}/ack", tok=self.tok["polisher"])["first"])

    def test_legacy_orchestrator_message_mirrors_to_the_bus(self):
        r = self.b.req("POST", "/orchestrators/polisher/messages", {"text": "legacy hello"})
        self.assertTrue(r["queued"] and r["id"] and r["bus_id"])
        m = self.msg(r["bus_id"])
        self.assertEqual((m["sender"], m["recipient"], m["thread"], m["body"], m["state"]), ("admin", "polisher", "dm:admin+polisher", "legacy hello", "queued"))
        self.assertEqual(self.api.s.one("SELECT bus_message_id FROM orchestrator_messages WHERE id = ?", r["id"])["bus_message_id"], r["bus_id"])
        beat(self.b, "polisher", state="running", delivered_message_id=r["id"])
        self.assertEqual(self.msg(r["bus_id"])["state"], "delivered")
        self.b.req("DELETE", "/bus/agents/polisher")   # no agent: response unchanged (no bus_id)
        r2 = self.b.req("POST", "/orchestrators/polisher/messages", {"text": "again"})
        self.assertTrue(r2["queued"])
        self.assertNotIn("bus_id", r2)


class ExpiryTest(BusCase):
    def test_poll_and_tmux_messages_expire_after_24h_and_are_never_typed(self):
        a = self.send("clozy", expect=201, to="gil", body="for gil")["id"]
        b = self.send("clozy", expect=201, to="polisher", body="for polisher")["id"]
        c = self.send("clozy", expect=201, to="chief", body="for chief")["id"]
        self.flush(DAY - 60)
        self.assertEqual(self.rows("SELECT * FROM bus_messages WHERE state = 'failed'"), [])
        self.flush(DAY + 60)
        failed = self.rows("SELECT * FROM bus_messages WHERE state = 'failed' AND sender = 'clozy' ORDER BY id")
        self.assertEqual([m["recipient"] for m in failed], ["gil", "polisher", "chief"])
        self.assertTrue(all(m["last_error"] == "not picked up within 24 hours; expired" for m in failed))
        self.assertEqual(self.rows("SELECT * FROM orchestrator_messages WHERE bus_message_id = ? AND delivered_at IS NULL", b), [])
        self.assertEqual(beat(self.b, "polisher", state="running")["message"], None)   # never typed
        to_chief = self.alerts("recipient = 'chief'")
        self.assertTrue(to_chief and all("unreachable" in m["body"] for m in to_chief), [m["body"] for m in to_chief])
        self.assertGreaterEqual(len(self.alerts("recipient = 'clozy'")), 1)   # the sender is told too
        self.assertEqual((a, c), (a, c))

    def test_expiry_never_touches_delivered_read_leased_or_in_flight(self):
        mk = lambda body: self.send("clozy", expect=201, to="chief", body=body)["id"]  # noqa: E731
        dele, read, flight, lease, plain = mk("delivered"), mk("read"), mk("inflight"), mk("leased"), mk("plain")
        self.api.s.run("UPDATE bus_messages SET created_at = ?", self.t0 - 30 * HOUR)
        self.api.s.run("UPDATE bus_messages SET delivered_at = ? WHERE id = ?", self.t0, dele)   # race: delivered_at set, state not yet moved
        self.api.s.run("UPDATE bus_messages SET read_at = ? WHERE id = ?", self.t0, read)
        self.api.s.run("INSERT INTO bus_attempts(message_id, transport, started_at) VALUES(?, 'webhook', ?)", flight, self.t0 - 10)
        self.api.s.run("UPDATE bus_messages SET lease_until = ? WHERE id = ?", self.t0 + 60, lease)
        self.flush()
        st = {m["id"]: m["state"] for m in self.rows("SELECT id, state FROM bus_messages")}
        for mid in (dele, read, flight, lease):
            self.assertNotEqual(st[mid], "failed", mid)
        self.assertEqual(st[plain], "failed")

    def test_expire_message_is_conditional_on_the_row_state(self):
        mid = self.send("clozy", expect=201, to="chief", body="x")["id"]
        row = self.msg(mid)
        self.get("/bus/inbox", "chief")   # delivered meanwhile
        self.assertFalse(self.bus.expire_message(row, self.t0 + 2 * DAY))
        self.assertEqual(self.msg(mid)["state"], "delivered")

    def test_expired_held_messages_fail_and_never_ride_along(self):
        old = self.send("chief", expect=201, to="clozy", body="old")["id"]
        self.age(old, 25 * HOUR)
        self.flush()
        m = self.msg(old)
        self.assertEqual((m["state"], m["last_error"]), ("failed", "not picked up within 24 hours; expired"))
        self.assertEqual(self.posts, [])

    def test_expired_urgent_realerts_the_sender(self):
        out = self.send("clozy", expect=201, to="polisher", body="now!", urgent=True)["id"]
        self.flush(6 * MIN)
        self.assertEqual([a["client_msg_id"] for a in self.alerts("recipient = 'clozy'")], [f"urgent:{out}"])
        self.assertTrue(self.msg(out)["alerted_at"])
        self.flush(25 * HOUR)
        m = self.msg(out)
        self.assertEqual(m["state"], "failed")
        self.assertEqual(sorted(a["client_msg_id"] for a in self.alerts("recipient = 'clozy'")), sorted([f"urgent:{out}", f"failed:{out}"]))
        self.assertEqual(self.rows("SELECT * FROM orchestrator_messages WHERE bus_message_id = ? AND delivered_at IS NULL", out), [])


class ReachabilityTest(BusCase):
    def warn(self, who, to, body="x", **kw):
        return self.send(who, expect=201, to=to, body=body, **kw).get("warning")

    def test_poll_agent_unseen(self):
        w = self.warn("quiet", "gil", "a")
        self.assertRegex(w, r"^gil is unreachable: has not checked in.*Stored; it will be delivered when gil is reachable\.$")
        self.get("/bus/agents", "gil")
        self.assertIsNone(self.warn("quiet", "gil", "b"))
        self.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = 'gil'", self.t0 - 11 * MIN)
        self.assertIsNotNone(self.warn("quiet", "gil", "c"))
        self.assertEqual(self.count(), 3)   # always stored

    def test_orchestrator_reasons(self):
        self.api.s.run("UPDATE orchestrators SET desired = 'stopped'")
        self.assertRegex(self.warn("chief", "polisher", "d"), r"polisher is unreachable: orchestrator is stopped")
        self.assertEqual(len(self.rows("SELECT * FROM orchestrator_messages WHERE bus_message_id IS NOT NULL")), 1)   # still queued for it
        self.api.s.run("UPDATE orchestrators SET desired = 'running', state = 'starting'")
        self.assertRegex(self.warn("chief", "polisher", "e"), r"orchestrator state is starting")
        self.api.s.run("UPDATE orchestrators SET state = 'running', last_heartbeat_at = 1")
        self.assertRegex(self.warn("chief", "polisher", "f"), r"heartbeat")
        self.api.s.run("UPDATE orchestrators SET last_heartbeat_at = ?", self.t0 - 601)
        self.assertRegex(self.warn("chief", "polisher", "g"), r"heartbeat")
        self.api.s.run("UPDATE orchestrators SET last_heartbeat_at = ?", self.t0 - 500)
        self.assertIsNone(self.warn("chief", "polisher", "h"))

    def test_orchestrator_agent_not_running_after_deletion(self):
        self.api.s.run("UPDATE orchestrators SET deleted_at = ?", self.t0)
        self.assertRegex(self.warn("chief", "polisher"), r"orchestrator not found")

    def test_webhook_reasons(self):
        self.api.s.run("UPDATE bots SET notify_url = NULL WHERE id = 'clozy-bot'")
        self.assertRegex(self.warn("chief", "clozy", "g", urgent=True), r"no valid notify_url")
        self.api.s.run("UPDATE bots SET notify_url = ? WHERE id = 'clozy-bot'", HOOK)
        self.api.s.run("DELETE FROM bus_attempts")
        self.api.s.run("UPDATE bus_messages SET state = 'delivered'")
        self.status = 502
        for body in ("h", "i", "j"):   # one failed attempt each
            self.assertIsNone(self.warn("chief", "clozy", body, urgent=True))
            self.flush()
        w = self.warn("chief", "clozy", "k", urgent=True)
        self.assertRegex(w, r"last 3 webhook attempts failed \(latest: HTTP 502\)")
        self.status = 200
        self.flush(70)   # a success clears it
        self.assertIsNone(self.warn("chief", "clozy", "l", urgent=True))

    def test_alert_agent_told_after_15_minutes_once_per_hour(self):
        self.b.req("PUT", "/bus/agents/idle", {"kind": "service", "wake": "poll"}, expect=201)   # never checks in
        self.assertIsNotNone(self.warn("quiet", "idle", "a"))
        self.warn("quiet", "idle", "b")
        self.assertEqual(self.alerts(), [])   # not before 15 minutes of being unreachable
        self.flush(16 * MIN)
        al = self.alerts("recipient = 'chief'")
        self.assertEqual(len(al), 1)
        self.assertTrue(al[0]["client_msg_id"].startswith("unreachable:idle:"))
        self.assertRegex(al[0]["body"], r"^idle is unreachable: has not checked in")
        self.flush(17 * MIN)
        self.assertEqual(len(self.alerts("recipient = 'chief'")), 1)   # same hour: once
        self.flush(80 * MIN)
        self.assertEqual(len(self.alerts("recipient = 'chief'")), 2)   # next hour: again

    def test_recovery_clears_the_episode(self):
        self.warn("gil", "quiet")
        self.assertTrue(self.agent("quiet")["unreachable_since"])
        self.get("/bus/agents", "quiet")
        self.flush(60)
        self.assertIsNone(self.agent("quiet")["unreachable_since"])

    def test_human_agents_never_alert_or_escalate(self):
        self.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = 'gil'", self.t0 - 5 * HOUR)
        self.warn("polisher", "gil", "fyi")
        self.flush(20 * MIN)
        self.flush(4 * HOUR)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.rows("SELECT * FROM notices WHERE key LIKE 'alert:bus%'"), [])
        g = self.agent("gil")
        self.assertEqual((g["unreachable_since"], g["alerted_at"], g["escalated_at"]), (None, None, None))

    def test_reachability_function(self):
        self.assertEqual(self.bus.reachability(None), {"reachable": False, "reason": "unknown agent"})
        self.assertTrue(self.bus.reachability(self.agent("chief"))["reachable"])
        self.b.req("DELETE", "/bus/agents/chief")
        self.assertEqual(self.bus.reachability(self.agent("chief"))["reason"], "retired")


class AlertTest(BusCase):
    def test_urgent_undelivered_after_5_minutes_alerts_the_sender_and_never_loops(self):
        out = self.send("clozy", expect=201, to="polisher", body="now!", urgent=True)["id"]
        self.flush(60)
        self.assertEqual(self.alerts(), [])
        self.flush(6 * MIN)
        al = self.alerts()
        self.assertEqual((len(al), al[0]["client_msg_id"], al[0]["recipient"]), (1, f"urgent:{out}", "clozy"))
        self.assertRegex(al[0]["body"], f"Urgent message #{out} to polisher .* still undelivered after 5 minutes")
        self.flush(20 * MIN)
        self.assertEqual(len(self.alerts()), 1)

    def test_failed_message_alerts_sender_with_failed_key(self):
        out = self.send("clozy", expect=201, to="gil", body="x")["id"]
        self.api.s.run("UPDATE bus_messages SET state = 'failed', last_error = 'boom' WHERE id = ?", out)
        self.flush()
        al = self.alerts("recipient = 'clozy'")
        self.assertEqual([a["client_msg_id"] for a in al], [f"failed:{out}"])
        self.assertRegex(al[0]["body"], f"#{out} to gil .* failed: boom")
        self.assertTrue(al[0]["urgent"])
        self.flush(10)
        self.assertEqual(len(self.alerts("recipient = 'clozy'")), 1)

    def test_alert_to_a_retired_or_unknown_sender_is_skipped(self):
        out = self.send(ADMIN, to="gil", body="x", **{"from": "ghost-sender"})
        self.assertEqual(out[0], 201)
        self.api.s.run("UPDATE bus_messages SET state = 'failed', last_error = 'x'")
        self.flush()
        self.assertEqual(self.alerts("recipient = 'ghost-sender'"), [])

    def test_rejected_alert_enqueue_stays_retryable(self):
        u = self.send("clozy", expect=201, to="chief", body="urgent", urgent=True)["id"]
        self.age(u, 10 * MIN)
        self.fill("clozy", 100)   # the alert to the sender will be rejected with 429
        s1 = self.flush()
        self.assertIsNone(self.msg(u)["alerted_at"])
        self.assertGreaterEqual(s1.get("alertFailures", 0), 1)
        self.api.s.run("DELETE FROM bus_messages WHERE thread = 'fill'")
        self.flush(1)
        self.assertTrue(self.msg(u)["alerted_at"])
        self.assertEqual(len(self.rows("SELECT * FROM bus_messages WHERE client_msg_id = ?", f"urgent:{u}")), 1)

    def test_rejected_ops_alert_does_not_use_up_the_hourly_claim(self):
        self.api.s.run("UPDATE bots SET notify_url = NULL WHERE id = 'clozy-bot'")
        self.send("gil", expect=201, to="clozy", body="x", urgent=True)
        self.fill("chief", 100, created=self.t0 + 19 * MIN)   # chief (the alert agent) is full: 429
        self.flush(20 * MIN)
        self.assertIsNone(self.agent("clozy")["alerted_at"])
        self.assertEqual(self.alerts("recipient = 'chief' AND client_msg_id LIKE 'unreachable:%'"), [])
        self.api.s.run("DELETE FROM bus_messages WHERE thread = 'fill'")
        self.flush(21 * MIN)
        self.assertEqual(len(self.alerts("recipient = 'chief' AND client_msg_id LIKE 'unreachable:clozy:%'")), 1)
        self.assertTrue(self.agent("clozy")["alerted_at"])

    def test_alert_agent_is_never_alerted_about_itself(self):
        self.api.s.run("UPDATE agents SET last_seen_at = ? WHERE name = 'chief'", self.t0 - HOUR)
        self.send("quiet", expect=201, to="chief", body="x")
        self.flush(20 * MIN)
        self.assertEqual(self.alerts("recipient = 'chief' AND client_msg_id LIKE 'unreachable:chief:%'"), [])

    def test_missing_alert_agent_falls_back_to_the_chief_notice(self):
        self.b.bot("cos", role="chief", url="http://127.0.0.1:9/cos")
        self.b.req("DELETE", "/bus/agents/chief")
        self.api.s.run("UPDATE bots SET notify_url = NULL WHERE id = 'clozy-bot'")
        self.send("gil", expect=201, to="clozy", body="x", urgent=True)
        self.flush(20 * MIN)
        self.assertEqual(self.alerts("recipient = 'chief'"), [])
        self.assertTrue(self.rows("SELECT * FROM notices WHERE key LIKE 'alert:bus:unreachable:clozy%'"))


class RetentionTest(BusCase):
    def test_force_flush_deletes_old_attempts_and_messages(self):
        old = self.t0 - 100 * DAY
        self.api.s.run("INSERT INTO bus_messages(sender, recipient, thread, body, state, created_at) VALUES('a','chief','t','old','read',?)", old)
        self.api.s.run("INSERT INTO bus_messages(sender, recipient, thread, body, state, created_at) VALUES('a','chief','t','mid','read',?)", self.t0 - 80 * DAY)
        self.api.s.run("INSERT INTO bus_attempts(message_id, transport, started_at) VALUES(999, 'webhook', ?)", self.t0 - 40 * DAY)
        self.api.s.run("INSERT INTO bus_attempts(message_id, transport, started_at) VALUES(998, 'webhook', ?)", self.t0 - 10 * DAY)
        self.flush(force=True)
        self.assertEqual(self.rows("SELECT * FROM bus_messages WHERE body = 'old'"), [])
        self.assertEqual(len(self.rows("SELECT * FROM bus_messages WHERE body = 'mid'")), 1)
        self.assertEqual(self.rows("SELECT * FROM bus_attempts WHERE message_id = 999"), [])
        self.assertEqual(len(self.rows("SELECT * FROM bus_attempts WHERE message_id = 998")), 1)

    def test_hourly_bounded_batches_over_indexed_columns(self):
        old = self.t0 - 100 * DAY
        for i in range(1200):
            self.api.s.run("INSERT INTO bus_messages(sender, recipient, thread, body, state, created_at) VALUES('a','b','t','old','read',?)", old + i)
        hour = self.t0 - self.t0 % HOUR
        n = lambda: self.api.s.one("SELECT COUNT(*) AS n FROM bus_messages")["n"]  # noqa: E731
        self.bus.flush(now=hour + 5 * MIN)
        self.assertEqual(n(), 1200)   # not the retention minute: nothing deleted
        self.bus.flush(now=hour + 17 * MIN)
        self.assertEqual(n(), 700)    # one batch of 500
        self.bus.flush(force=True)
        self.bus.flush(force=True)
        self.assertEqual(n(), 0)
        idx = [r["name"] for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'index'")]
        self.assertIn("bus_messages_created", idx)
        self.assertIn("bus_attempts_started", idx)


class PageTest(BusCase):
    def test_page_is_admin_or_loopback_and_escapes(self):
        self.send("chief", expect=201, to="quiet", body="<script>alert(1)</script>")
        status, html = self.b.call("GET", "/bus", tok=ADMIN)
        self.assertEqual(status, 200)
        text = html.decode() if isinstance(html, bytes) else str(html)
        self.assertIn("Agent Bus", text)
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("clozy", text)
        for tok in (self.tok["chief"], WORKER, self.tok["clozy"]):
            self.assertEqual(self.b.call("GET", "/bus", tok=tok)[0], 403)

    def test_page_without_a_token_is_refused_even_from_loopback(self):
        # The test server is reached over 127.0.0.1, exactly like a request through a reverse proxy.
        self.assertIn(self.b.call("GET", "/bus", tok=None)[0], (401, 403))


if __name__ == "__main__":
    unittest.main()
