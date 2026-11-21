"""The Agent Bus (docs/agent-bus.md): named agents, direct messages with receipts, one delivery path per wake type.

  webhook  POST to the bot's notify_url from the backend's delivery loop (woken at write time, retried at 1, 5, 15, 60 minutes)
  tmux     a row in orchestrator_messages; the pinned worker's supervisor types it and confirms (on_orchestrator_delivered)
  poll     delivered when the agent fetches its inbox

Wake policy (docs/agent-bus.md "Wake policy and digests"): only urgent messages, answers to a question the recipient is waiting on,
and finish notices wake a webhook bot at once. Everything else is held for one digest wake per agent per digest_minutes.

Auth: agent token ab_<name>.<gen>.<hmac> (nothing stored but token_gen, which revokes) or the admin token (may act as anyone).
No secrets are accepted and none are returned. Standard library only.
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import time

import swarm_gitlink

# Timestamps are whole seconds, like the rest of the kit.
BODY_MAX = 3700            # the tmux header adds ~250; the supervisor pastes at most 4,000 characters
TMUX_TOTAL_MAX = 4000
META_MAX = 8 * 1024
MAX_QUEUED = 100
LEASE = 60
SEEN_REFRESH = 60
URGENT_ALERT = 5 * 60
EXPIRE = 2 * 3600
HELD_EXPIRE = 24 * 3600
BACKOFF = [60, 5 * 60, 15 * 60, 3600]   # by completed rounds; a 5th failed round = failed
STALE = 600
ALERT_AFTER = 15 * 60
ALERT_EVERY = 3600
ESCALATE_AFTER = 3 * 3600
DIGEST_DEFAULT = 60
DIGEST_MIN = 5            # a zero-minute digest would defeat the atomic per-recipient claim
DIGEST_MAX = 7 * 24 * 60
DIGEST_BATCH = 50
POLL_WINDOW_MIN = 10
POLL_WINDOW_MAX = 7 * 24 * 60
RETENTION_BATCH = 500
SYSTEM = "agent-control"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
THREAD_RE = re.compile(r"^[a-z0-9][a-z0-9:._+-]{0,79}$")
CMID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9@._:+-]{0,79}$")
KINDS = ("bot", "orchestrator", "human", "service")
WAKES = ("webhook", "tmux", "poll")
SECRET_MIN = 32
ETIQUETTE = ("Reply only to a message with reply_expected true. If it is false, do your work (or nothing) and do not reply or "
             "acknowledge in prose; ack by POSTing to the ack path.")
CTRL_RE = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f  ]")


def clean_body(s):
    """Strip control characters except \\n and \\t, so pasted text can never end a bracketed paste (ESC[201~)."""
    return CTRL_RE.sub("", "" if s is None else str(s))


def norm_name(raw):
    n = str("" if raw is None else raw).strip().lower()
    return n[1:] if n.startswith("@") else n


def slug(s):
    out = re.sub(r"[^a-z0-9._+-]", "-", str(s).lower())
    out = re.sub(r"^[^a-z0-9]+", "", out)
    return out or "x"


def dm_thread(a, b):
    return ("dm:" + "+".join(sorted([slug(a), slug(b)])))[:80]


def clean_from(s):
    return re.sub(r"[^\w.@ -]", "", str(s or "admin"))[:80] or "admin"


def parse_meta(s):
    try:
        return json.loads(s) if s else None
    except ValueError:
        return None


class Bus:
    """All bus logic. `be` is the Backend (database, bot lookup, key files, secret scan, post_json)."""

    def __init__(self, be, helpers, poster=None):
        self.be = be
        self.h = helpers                  # valid_notify_url, post_json, fingerprint, VERSION from the backend module
        self.s = be.s
        self.poster = poster              # tests inject a fake; None = the backend's post_json
        self.wake = threading.Event()     # set at write time: wakes the delivery loop at once
        self._secret = None
        self.alert_agent = os.environ.get("SWARM_BUS_ALERT_AGENT") or "chief"
        self.inline_delays = self._delays()

    @staticmethod
    def _delays():
        try:
            v = [float(x) for x in os.environ.get("SWARM_BUS_INLINE_DELAYS", "0").split(",") if x.strip()]
            return [x for x in v if x >= 0] or [0]
        except ValueError:
            return [0]

    # ---------------- tokens ----------------
    def secret(self):
        """HMAC key for agent tokens: its own file (never the admin token, so one rotation cannot break the other)."""
        if self._secret:
            return self._secret
        path = os.path.expanduser(os.environ.get("SWARM_BUS_TOKEN_SECRET_FILE") or
                                  os.path.join(os.path.dirname(os.path.abspath(self.be.keys.dir)), "bus-token-secret"))
        try:
            with open(path) as fh:
                v = fh.read().strip()
        except OSError:
            v = ""
        if len(v) < SECRET_MIN:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            v = secrets.token_hex(32)
            tmp = f"{path}.tmp-{secrets.token_hex(4)}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, v.encode())
            finally:
                os.close(fd)
            os.replace(tmp, path)
        self._secret = v
        return v

    def mint(self, name, gen):
        mac = hmac.new(self.secret().encode(), f"agent-bus:{name}:{gen}".encode(), hashlib.sha256).hexdigest()
        return f"ab_{name}.{gen}.{mac}"

    def token_for(self, name):
        a = self.s.one("SELECT name, token_gen, retired_at FROM agents WHERE name = ?", str(name or ""))
        return self.mint(a["name"], a["token_gen"]) if a and not a["retired_at"] else None

    def agent_from_token(self, token):
        """The agent row this token acts as, or None. Touches last_seen_at at most once a minute."""
        t = str(token or "")
        if not t.startswith("ab_"):
            return None
        parts = t[3:].split(".")
        if len(parts) != 3 or not parts[1].isdigit():
            return None
        a = self.s.one("SELECT * FROM agents WHERE name = ?", parts[0])
        if not a or a["retired_at"] or a["token_gen"] != int(parts[1]):
            return None
        if not hmac.compare_digest(t, self.mint(a["name"], a["token_gen"])):
            return None
        n = self.now()
        if not a["last_seen_at"] or n - a["last_seen_at"] > SEEN_REFRESH:
            self.s.run("UPDATE agents SET last_seen_at = ? WHERE name = ?", n, a["name"])
            a["last_seen_at"] = n
        return a

    @staticmethod
    def now():
        return int(time.time())

    # ---------------- names ----------------
    def resolve(self, raw):
        """Agent row (maybe retired) for a name or alias, or None."""
        n = norm_name(raw)
        if not n:
            return None
        a = self.s.one("SELECT * FROM agents WHERE name = ?", n)
        if a:
            return a
        al = self.s.one("SELECT name FROM agent_aliases WHERE alias = ?", n)
        return self.s.one("SELECT * FROM agents WHERE name = ?", al["name"]) if al else None

    def did_you_mean(self, raw):
        q = norm_name(raw)
        names = [r["name"] for r in self.s.q("SELECT name FROM agents WHERE retired_at IS NULL ORDER BY name")]
        als = self.s.q("SELECT alias, name FROM agent_aliases")
        out = []

        def hit(x):
            return len(q) >= 2 and (q in x or x in q or x[:2] == q[:2])
        for n in names:
            if hit(n) and n not in out:
                out.append(n)
        for r in als:
            if hit(r["alias"]) and r["name"] in names and r["name"] not in out:
                out.append(r["name"])
        return out[:3]

    def owner_agent(self, owner, bus_only=True):
        """The bus agent a task owner string resolves to: by name, alias, or its bot id."""
        o = str(owner or "").strip()
        if not o:
            return None
        n = norm_name(o)
        bot = self.be.bot(o)
        a = self.s.one(
            "SELECT * FROM agents WHERE retired_at IS NULL" + (" AND notices = 'bus'" if bus_only else "") +
            " AND (name = ? OR name IN (SELECT name FROM agent_aliases WHERE alias = ?) OR lower(bot_id) = lower(?) OR lower(bot_id) = ?)"
            " ORDER BY name LIMIT 1", n, n, bot["id"] if bot else "\u0000", o.lower())
        return a

    # ---------------- reachability ----------------
    @staticmethod
    def _reach(ok, why=None):
        return {"reachable": ok, "reason": why}

    def reachability(self, a):
        if not a:
            return self._reach(False, "unknown agent")
        if a["retired_at"]:
            return self._reach(False, "retired")
        t = self.now()
        if a["wake"] == "webhook":
            bot = self.be.bot(a["bot_id"]) if a["bot_id"] else None
            if not bot:
                return self._reach(False, "no bot registered for the webhook")
            if not bot.get("notify_url") or self.h["valid_notify_url"](bot["notify_url"], resolve=False):
                return self._reach(False, "bot has no valid notify_url")
            rows = self.s.q("SELECT t.ok, t.error FROM bus_attempts t JOIN bus_messages m ON m.id = t.message_id"
                            " WHERE m.recipient = ? AND t.transport = 'webhook' ORDER BY t.id DESC LIMIT 3", a["name"])
            if len(rows) == 3 and not any(r["ok"] for r in rows):
                return self._reach(False, f"last 3 webhook attempts failed (latest: {rows[0]['error'] or 'unknown error'})")
            return self._reach(True)
        if a["wake"] == "tmux":
            o = self.be.orch_row(a["orchestrator"]) if a["orchestrator"] else None
            if not o:
                return self._reach(False, "orchestrator not found")
            if o["desired"] != "running":
                return self._reach(False, f"orchestrator is {'stopped' if o['desired'] == 'stopped' else 'not meant to be running'}")
            if o["state"] != "running":
                return self._reach(False, f"orchestrator state is {o['state']}")
            if not o["last_heartbeat_at"] or t - o["last_heartbeat_at"] > STALE:
                return self._reach(False, "orchestrator has not sent a heartbeat for 10 minutes")
            return self._reach(True)
        # A poll agent may declare how often it checks in (poll_window_minutes), so a daily poller is not unreachable after 10 minutes.
        win = a.get("poll_window_minutes")
        if not a["last_seen_at"] or t - a["last_seen_at"] > (win * 60 if win else STALE):
            return self._reach(False, f"has not checked in for {win if win else 10} minutes")
        return self._reach(True)

    # ---------------- shapes ----------------
    @staticmethod
    def shape_msg(r):
        return {"id": r["id"], "from": r["sender"], "to": r["recipient"], "thread": r["thread"], "body": r["body"],
                "meta": parse_meta(r["meta"]), "urgent": bool(r["urgent"]), "reply_expected": bool(r["reply_expected"]),
                "noop": bool(r["noop"]), "held": bool(r["held"]), "state": r["state"], "created_at": r["created_at"],
                "delivered_at": r["delivered_at"], "read_at": r["read_at"], "attempts": r["attempts"], "last_error": r["last_error"]}

    def thread_stats(self, m):
        s = self.s.one("SELECT COALESCE(SUM(id > ?), 0) AS newer, MAX(id) AS latest FROM bus_messages"
                       " WHERE thread = ? AND (sender = ? OR recipient = ?)", m["id"], m["thread"], m["recipient"], m["recipient"])
        return {"newer_in_thread": int(s["newer"] or 0), "thread_latest_id": s["latest"] if s["latest"] is not None else m["id"]}

    def shape_agent(self, a):
        r = self.reachability(a)
        aliases = [x["alias"] for x in self.s.q("SELECT alias FROM agent_aliases WHERE name = ? ORDER BY alias", a["name"])]
        return {"name": a["name"], "kind": a["kind"], "wake": a["wake"], "bot_id": a["bot_id"], "orchestrator": a["orchestrator"],
                "description": a["description"], "notices": a["notices"],
                "digest_minutes": max(DIGEST_MIN, a["digest_minutes"] if a["digest_minutes"] is not None else DIGEST_DEFAULT),
                "poll_window_minutes": a.get("poll_window_minutes"),
                "unreachable_since": a["unreachable_since"], "last_digest_at": a["last_digest_at"], "last_seen_at": a["last_seen_at"],
                "retired_at": a["retired_at"], "aliases": aliases, "reachable": r["reachable"], "reason": r["reason"]}

    # ---------------- webhook delivery ----------------
    @staticmethod
    def ack_of(m):
        return {"method": "POST", "path": f"/bus/messages/{m['id']}/ack"}

    def item_of(self, m):
        return {"id": m["id"], "from": m["sender"], "thread": m["thread"], "body": m["body"], "urgent": bool(m["urgent"]),
                "reply_expected": bool(m["reply_expected"]), "created_at": m["created_at"], "meta": parse_meta(m["meta"]), "ack": self.ack_of(m)}

    def webhook_payload(self, m, stats, pending=()):
        p = {"kind": "bus_message", "idempotency_key": f"bus:{m['id']}", "id": m["id"], "from": m["sender"], "to": m["recipient"],
             "thread": m["thread"], "body": m["body"], "urgent": bool(m["urgent"]), "reply_expected": bool(m["reply_expected"]),
             "created_at": m["created_at"], "meta": parse_meta(m["meta"]), **stats}
        if pending:
            p["pending"] = [self.item_of(x) for x in pending]
            p["pending_count"] = len(pending)
        p.update(ack=self.ack_of(m), reply={"method": "POST", "path": "/bus/send", "to": m["sender"], "thread": m["thread"]},
                 etiquette=ETIQUETTE)
        return p

    def digest_payload(self, name, rows):
        return {"kind": "bus_digest", "idempotency_key": f"bus-digest:{rows[0]['id']}:{len(rows)}", "to": name, "count": len(rows),
                "messages": [dict(self.item_of(m), reply={"method": "POST", "path": "/bus/send", "to": m["sender"], "thread": m["thread"]})
                             for m in rows], "etiquette": ETIQUETTE}

    def post(self, bot, payload):
        """One webhook POST using the bot's key file, exactly like a finish notice. Returns {ok, status, error}."""
        h = self.h
        if not bot or not bot.get("notify_url"):
            return {"ok": False, "error": "bot has no notify_url"}
        if h["valid_notify_url"](bot["notify_url"], resolve=False):
            return {"ok": False, "error": "bot notify_url is not valid"}
        key = self.be.keys.read(bot["id"]) if bot.get("notify_auth_fp") else None
        if bot.get("notify_auth_fp") and (not key or h["fingerprint"](key) != bot["notify_auth_fp"]):
            return {"ok": False, "error": "trigger key file is missing or does not match this bot's current key"}
        headers = {"Content-Type": "application/json", "User-Agent": "agent-swarm-kit/" + h["VERSION"], "Idempotency-Key": str(payload.get("idempotency_key"))}
        if key:
            headers["Authorization"] = key if " " in key else f"Bearer {key}"
        try:
            status, _ = (self.poster or h["post_json"])(bot["notify_url"], json.dumps(payload).encode(), headers)
        except Exception as e:  # network errors are retried
            return {"ok": False, "error": str(e)[:200]}
        if 200 <= status < 300:
            return {"ok": True, "status": status}
        return {"ok": False, "status": status, "error": f"HTTP {status}"}

    def webhook_try(self, m, bot, at, pending=()):
        aid = self.s.run("INSERT INTO bus_attempts(message_id, transport, started_at) VALUES(?, 'webhook', ?)", m["id"], at()).lastrowid
        res = self.post(bot, self.webhook_payload(m, self.thread_stats(m), pending))
        self.s.run("UPDATE bus_attempts SET finished_at = ?, ok = ?, http_status = ?, error = ? WHERE id = ?", at(),
                   1 if res["ok"] else 0, res.get("status"), None if res["ok"] else str(res.get("error") or "failed")[:300], aid)
        return res

    def settle_failure(self, m, res, t, by_age=True):
        attempts = m["attempts"] + 1
        err = str(res.get("error") or "failed")[:300]
        if attempts > len(BACKOFF) or (by_age and t - m["created_at"] > EXPIRE):
            self.s.run("UPDATE bus_messages SET state = 'failed', attempts = ?, lease_until = NULL, next_attempt_at = NULL, last_error = ?"
                       " WHERE id = ? AND state = 'queued'", attempts, err, m["id"])
        else:
            self.s.run("UPDATE bus_messages SET attempts = ?, lease_until = NULL, next_attempt_at = ?, last_error = ?"
                       " WHERE id = ? AND state = 'queued'", attempts, t + BACKOFF[attempts - 1], err, m["id"])

    def expire_message(self, m, t, own_lease=False):
        """Fail one queued message older than 24 h so it is never delivered. An urgent one clears alerted_at so the sender is alerted again."""
        with self.s.tx():
            cur = self.s.run(
                "UPDATE bus_messages SET state = 'failed', last_error = 'not picked up within 24 hours; expired', lease_until = NULL,"
                " next_attempt_at = NULL" + (", alerted_at = NULL" if m["urgent"] else "") +
                " WHERE id = ? AND state = 'queued' AND delivered_at IS NULL AND read_at IS NULL"
                " AND (? OR lease_until IS NULL OR lease_until < ?)"
                " AND NOT EXISTS (SELECT 1 FROM bus_attempts x WHERE x.message_id = bus_messages.id AND x.finished_at IS NULL AND x.started_at > ?)",
                m["id"], 1 if own_lease else 0, t, t - LEASE)
            if not cur.rowcount:
                return False
            # Never type a stale message into a pane later.
            self.s.run("UPDATE orchestrator_messages SET delivered_at = ? WHERE bus_message_id = ? AND delivered_at IS NULL", t, m["id"])
        return True

    def expire_held(self, name, t):
        n = 0
        for m in self.s.q("SELECT * FROM bus_messages WHERE recipient = ? AND held = 1 AND state = 'queued' AND created_at <= ?"
                          " ORDER BY id LIMIT 100", name, t - HELD_EXPIRE):
            n += 1 if self.expire_message(m, t) else 0
        return n

    def claim_held(self, name, at, expire=True):
        """Lease up to DIGEST_BATCH held messages of one agent. Expired ones are failed first, so a stale one never rides along."""
        if expire:
            self.expire_held(name, at)
        with self.s.tx():
            rows = self.s.q("SELECT * FROM bus_messages WHERE recipient = ? AND held = 1 AND state = 'queued' AND created_at > ?"
                            " AND (lease_until IS NULL OR lease_until < ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                            " ORDER BY id LIMIT ?", name, at - HELD_EXPIRE, at, at, DIGEST_BATCH)
            for r in rows:
                self.s.run("UPDATE bus_messages SET lease_until = ? WHERE id = ?", at + LEASE, r["id"])
        return rows

    def deliver_held(self, rows, a, t, res):
        for h in rows:
            self.s.run("INSERT INTO bus_attempts(message_id, transport, started_at, finished_at, ok, http_status) VALUES(?, 'webhook', ?, ?, 1, ?)",
                       h["id"], t, t, res.get("status"))
            self.s.run("UPDATE bus_messages SET state = 'delivered', held = 0, delivered_at = ?, attempts = attempts + 1, lease_until = NULL,"
                       " next_attempt_at = NULL, last_error = NULL WHERE id = ? AND state = 'queued'", t, h["id"])
        if rows:
            self.s.run("UPDATE agents SET last_digest_at = ? WHERE name = ?", t, a["name"])

    def webhook_round(self, mid, delays=(0,), now=None):
        """One delivery round for a queued "now" (non-held) webhook message. Held messages for the same agent ride along."""
        def at():
            return now if now is not None else self.now()
        cur = self.s.run("UPDATE bus_messages SET lease_until = ? WHERE id = ? AND state = 'queued' AND held = 0"
                         " AND (lease_until IS NULL OR lease_until < ?)", at() + LEASE, mid, at())
        if not cur.rowcount:
            return None
        m = self.s.one("SELECT * FROM bus_messages WHERE id = ?", mid)
        if at() - m["created_at"] > HELD_EXPIRE:
            self.expire_message(m, at(), own_lease=True)
            return {"ok": False, "error": "expired"}
        a = self.s.one("SELECT * FROM agents WHERE name = ?", m["recipient"])
        bot = self.be.bot(a["bot_id"]) if a and a["bot_id"] else None
        res, pending = {"ok": False, "error": "recipient is not a webhook agent"}, []
        if a and a["wake"] == "webhook":
            pending = self.claim_held(a["name"], at())
            for i, d in enumerate(delays):
                if d:
                    time.sleep(d)
                if i:
                    self.s.run("UPDATE bus_messages SET lease_until = ? WHERE id = ?", at() + LEASE, mid)
                res = self.webhook_try(m, bot, at, pending)
                if res["ok"]:
                    break
        t = at()
        if res["ok"]:
            self.s.run("UPDATE bus_messages SET state = 'delivered', delivered_at = ?, attempts = ?, lease_until = NULL, next_attempt_at = NULL,"
                       " last_error = NULL WHERE id = ? AND state = 'queued'", t, m["attempts"] + 1, mid)
            self.deliver_held(pending, a, t, res)
        else:
            self.settle_failure(m, res, t)
            for h in pending:  # not sent: release untouched, the digest still owns them
                self.s.run("UPDATE bus_messages SET lease_until = NULL WHERE id = ?", h["id"])
        return res

    def digest_round(self, name, now=None):
        """One bus_digest POST with every pending non-urgent message of an agent. None when nothing is pending."""
        def at():
            return now if now is not None else self.now()
        a = self.s.one("SELECT * FROM agents WHERE name = ? AND wake = 'webhook' AND retired_at IS NULL", name)
        if not a:
            return None
        prev = a["last_digest_at"]
        # Atomic per recipient: only one overlapping run can move last_digest_at past the interval; the loser sends nothing.
        won = self.s.run("UPDATE agents SET last_digest_at = ? WHERE name = ? AND (last_digest_at IS NULL OR last_digest_at +"
                         " MAX(COALESCE(digest_minutes, 60), 5) * 60 <= ?) AND last_digest_at IS ?", at(), name, at(), prev).rowcount
        if not won:
            return None
        rows = self.claim_held(name, at())
        if not rows:
            self.s.run("UPDATE agents SET last_digest_at = ? WHERE name = ? AND last_digest_at = ?", prev, name, at())
            return None
        res = self.post(self.be.bot(a["bot_id"]) if a["bot_id"] else None, self.digest_payload(name, rows))
        t = at()
        if res["ok"]:
            self.deliver_held(rows, a, t, res)
        else:
            self.s.run("UPDATE agents SET last_digest_at = ? WHERE name = ? AND last_digest_at = ?", prev, name, at())  # a failed digest does not count
            for h in rows:
                self.s.run("INSERT INTO bus_attempts(message_id, transport, started_at, finished_at, ok, http_status, error)"
                           " VALUES(?, 'webhook', ?, ?, 0, ?, ?)", h["id"], t, t, res.get("status"), str(res.get("error") or "failed")[:300])
                self.settle_failure(h, res, t, by_age=False)
        return res

    # ---------------- logs ----------------
    CREDENTIAL_RE = re.compile(r"bearer|authorization|secret|token|passw|api[_-]?key|\bab_", re.I)

    def safe_err(self, e):
        """A log-safe description of an exception: its category plus redacted, bounded text. Text that mentions a credential is withheld."""
        name = type(e).__name__[:40]
        text = str(e)
        if self.be.secret_hit(text) or self.CREDENTIAL_RE.search(text):
            return f"{name}: [text withheld]"
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", self.be.scrub(text))[:160]   # outside the f-string: Python 3.11
        return f"{name}: {text}"

    def log(self, what, mid, e):
        print(what, mid, self.safe_err(e), file=sys.stderr, flush=True)

    # ---------------- tmux ----------------
    def tmux_enqueue(self, m, a):
        """Enqueue the tmux copy once: keyed by bus_message_id, so retries, the duplicate path and the flush repair never double up."""
        head = f'{"URGENT " if m["urgent"] else ""}bus #{m["id"]} thread {m["thread"]} | reply_expected={"true" if m["reply_expected"] else "false"} | reply: swarmctl bus send {m["sender"]} "..." | after acting: swarmctl bus ack {m["id"]}'
        if len(head) + 2 + len(m["body"]) > TMUX_TOTAL_MAX:
            raise ValueError("bus message too long for a tmux paste")  # unreachable: BODY_MAX leaves room
        self.s.run("INSERT INTO orchestrator_messages(name, at, sender, text, bus_message_id) SELECT ?,?,?,?,? WHERE NOT EXISTS"
                   " (SELECT 1 FROM orchestrator_messages WHERE bus_message_id = ?)",
                   a["orchestrator"], self.now(), str(m["sender"])[:80], f"{head}\n\n{m['body']}", m["id"], m["id"])

    def repair_tmux(self, m, a):
        """Re-enqueue a stored, still queued tmux-wake message whose orchestrator message row is missing. Never raises."""
        try:
            if m and m["state"] == "queued" and not m["noop"] and a and a["wake"] == "tmux" and a["orchestrator"]:
                self.tmux_enqueue(m, a)
        except Exception as e:
            self.log("bus tmux repair failed", m and m["id"], e)

    def repair_stored_tmux(self, dup):
        """Duplicate path: repair for the STORED row's recipient (the duplicate key is sender/client_msg_id only), never the new request's."""
        try:
            self.repair_tmux(dup, self.s.one("SELECT * FROM agents WHERE name = ?", dup["recipient"]))
        except Exception as e:
            self.log("bus tmux repair failed", dup["id"], e)

    def on_orchestrator_delivered(self, om_id):
        """The supervisor typed an orchestrator message into the pane: mark its bus row delivered."""
        r = self.s.one("SELECT bus_message_id FROM orchestrator_messages WHERE id = ?", om_id)
        if not r or not r["bus_message_id"]:
            return
        t = self.now()
        if self.s.run("UPDATE bus_messages SET state = 'delivered', held = 0, delivered_at = ?, lease_until = NULL"
                      " WHERE id = ? AND state = 'queued'", t, r["bus_message_id"]).rowcount:
            self.s.run("INSERT INTO bus_attempts(message_id, transport, started_at, finished_at, ok) VALUES(?, 'tmux', ?, ?, 1)",
                       r["bus_message_id"], t, t)

    def mirror_orchestrator_message(self, name, sender, body, om_id):
        """A legacy POST /orchestrators/NAME/messages: when an agent points at that orchestrator, record a bus row too."""
        a = self.s.one("SELECT * FROM agents WHERE orchestrator = ? AND retired_at IS NULL ORDER BY name LIMIT 1", name)
        if not a:
            return None
        sender = clean_from(sender)
        n = self.now()
        mid = self.s.run("INSERT INTO bus_messages(sender, recipient, thread, body, created_at) VALUES(?,?,?,?,?)",
                         sender, a["name"], dm_thread(sender, a["name"]), body, n).lastrowid
        self.s.run("UPDATE orchestrator_messages SET bus_message_id = ? WHERE id = ?", mid, om_id)
        return mid

    # ---------------- send ----------------
    def send(self, m, now=None):
        """Validate, store and wake one message. m = {from, to, body, thread?, client_msg_id?, urgent?, noop?, reply_expected?, meta?};
        `from` is already the trusted sender name. Returns (status, data). Sent means stored: 201 only after the row is committed."""
        def at():
            return now if now is not None else self.now()
        body = clean_body(m.get("body")) if isinstance(m.get("body"), str) else ""
        if not body.strip():
            return 400, {"error": "body is required"}
        if len(body) > BODY_MAX:
            return 413, {"error": f"message is longer than {BODY_MAX} characters; put long text in a file the agent can read and send a pointer"}
        if self.be.secret_hit(body):
            return 400, {"error": "message looks like it contains a secret value; not stored"}
        meta = None
        mm = m.get("meta")
        if mm is not None:
            if not isinstance(mm, dict):
                return 400, {"error": "meta must be an object"}
            meta = json.dumps(mm)
            if len(meta) > META_MAX:
                return 413, {"error": f"meta is larger than {META_MAX} bytes"}
            if self.be.secret_hit(meta):
                return 400, {"error": "meta looks like it contains a secret value; not stored"}
        to = self.resolve(m.get("to"))
        if not to:
            return 404, {"error": f"unknown agent {str(m.get('to') or '')[:80]}", "did_you_mean": self.did_you_mean(m.get("to"))}
        if to["retired_at"]:
            return 410, {"error": f"agent {to['name']} is retired"}
        sender = str(m.get("from") or "")
        thread = dm_thread(sender, to["name"]) if m.get("thread") in (None, "") else str(m["thread"])
        if not THREAD_RE.match(thread):
            return 400, {"error": "thread must match ^[a-z0-9][a-z0-9:._+-]{0,79}$"}
        cmid = None if m.get("client_msg_id") in (None, "") else str(m["client_msg_id"])
        if cmid and not CMID_RE.match(cmid):
            return 400, {"error": "client_msg_id must match ^[A-Za-z0-9._:-]{1,100}$"}

        def out(r, **extra):
            return {"id": r["id"], "to": r["recipient"], "from": r["sender"], "thread": r["thread"], "state": r["state"], **extra}
        if cmid:
            dup = self.s.one("SELECT * FROM bus_messages WHERE sender = ? AND client_msg_id = ?", sender, cmid)
            if dup:
                self.repair_stored_tmux(dup)
                return 200, out(dup, duplicate=True, **({"wake": dup["wake"]} if dup["wake"] else {}))
        noop = m.get("noop") is True
        # Urgent when asked, when meta.blocker is true, or when an orchestrator asks a question (reply_expected true).
        blocker = isinstance(mm, dict) and mm.get("blocker") is True
        from_orch = (not noop and m.get("reply_expected") is True and
                     bool(self.s.one("SELECT 1 AS x FROM agents WHERE name = ? AND kind = 'orchestrator' AND retired_at IS NULL", sender)))
        urgent = m.get("urgent") is True or (not noop and (blocker or from_orch))
        if noop and (m.get("urgent") is True or blocker):
            return 400, {"error": "a noop message never wakes anyone, so it cannot also be urgent or a blocker"}
        # An answer to a question the recipient is waiting on (they sent a reply_expected message on this thread, nothing has answered it) wakes now.
        answers = False
        if not noop and not urgent and to["wake"] == "webhook":
            q0 = self.s.one("SELECT id FROM bus_messages WHERE thread = ? AND sender = ? AND recipient = ? AND reply_expected = 1"
                            " ORDER BY id DESC LIMIT 1", thread, to["name"], sender)
            if q0:
                answers = not self.s.one("SELECT 1 AS x FROM bus_messages WHERE thread = ? AND sender = ? AND recipient = ? AND id > ?"
                                         " AND noop = 0 AND held = 0 LIMIT 1", thread, sender, to["name"], q0["id"])
        wake = ("never" if noop else "digest" if to["wake"] == "webhook" and not urgent and not answers
                else "inbox" if to["wake"] == "poll" else "now")
        reply_expected = urgent if m.get("reply_expected") is None else m.get("reply_expected") is True
        n = self.s.one("SELECT COUNT(*) AS n FROM bus_messages WHERE recipient = ? AND state = 'queued'", to["name"])
        if int(n["n"] or 0) >= MAX_QUEUED:
            return 429, {"error": f"{MAX_QUEUED} messages are already queued for {to['name']}; try again after it catches up"}
        reach = self.reachability(to)
        try:
            with self.s.tx():
                mid = self.s.run(
                    "INSERT INTO bus_messages(sender, recipient, thread, client_msg_id, body, meta, urgent, wake, held, noop, reply_expected,"
                    " state, delivered_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    sender, to["name"], thread, cmid, body, meta, 1 if urgent else 0, wake, 1 if wake == "digest" else 0, 1 if noop else 0,
                    1 if reply_expected else 0, "delivered" if noop else "queued", at() if noop else None, at()).lastrowid
                row = self.s.one("SELECT * FROM bus_messages WHERE id = ?", mid)
        except Exception as e:
            dup = self.s.one("SELECT * FROM bus_messages WHERE sender = ? AND client_msg_id = ?", sender, cmid) \
                if cmid and "UNIQUE" in str(e).upper() else None
            if not dup:
                raise
            self.repair_stored_tmux(dup)
            return 200, out(dup, duplicate=True, **({"wake": dup["wake"]} if dup["wake"] else {}))
        # The row is stored: from here nothing may turn this into a failure (a caller that falls back on failure, such as finish
        # notices, would deliver twice). Dispatch and bookkeeping errors are logged; flush() repairs and retries.
        if not noop and to["wake"] == "tmux" and to["orchestrator"]:
            try:
                self.tmux_enqueue(row, to)
            except Exception as e:
                self.log("bus tmux dispatch failed", row["id"], e)
        elif not noop and to["wake"] == "webhook" and not row["held"]:
            self.wake.set()  # the delivery loop fires the webhook now
        extra = {"wake": wake}
        cur = row
        # Reachability bookkeeping is independent of the optional re-read; a failed episode start is found by the flush sweep.
        try:
            if reach["reachable"] and to["unreachable_since"]:
                self.clear_unreachable(to["name"])
            if not reach["reachable"]:
                extra["warning"] = (f"{to['name']} is unreachable: {reach['reason']}. Stored; it will be delivered when {to['name']} is reachable.")
                if sender != SYSTEM:
                    self.alert_unreachable(to, row, reach, now=now)
        except Exception as e:
            self.log("bus post-store bookkeeping failed", row["id"], e)
        try:
            cur = self.s.one("SELECT m.* FROM bus_messages m WHERE m.id = ?", row["id"]) or row   # optional re-read, after the commit
        except Exception as e:
            self.log("bus post-store read failed", row["id"], e)
        return 201, out(cur, **extra)

    # ---------------- alerts ----------------
    def alert_to_agent(self, key, body, avoid=()):
        """Tell the alert agent (SWARM_BUS_ALERT_AGENT, default chief). When it is not registered, log loudly and use the backend's
        Chief notice instead. Returns True, False (nothing to do) or "failed"."""
        target = self.s.one("SELECT * FROM agents WHERE name = ? AND retired_at IS NULL", self.alert_agent)
        if not target:
            print(f"BUS ALERT NOT DELIVERED: alert agent \"{self.alert_agent}\" is not registered. Alert: {body}", file=sys.stderr, flush=True)
            self.be.alert(f"bus:{key}", f"{body} (the alert agent \"{self.alert_agent}\" is not registered on the bus)", actor="agent-bus")
            return False
        if target["name"] in avoid:
            return False
        st, data = self.send({"from": SYSTEM, "to": target["name"], "thread": "alerts", "urgent": True, "reply_expected": False,
                              "client_msg_id": key[:100], "body": body})
        if st not in (200, 201):
            print(f"BUS ALERT ENQUEUE REJECTED ({st}) for \"{target['name']}\": {str(data.get('error') or '')[:150]}", file=sys.stderr, flush=True)
            return "failed"
        return True

    def alert_unreachable(self, to, row, r, now=None, since=None):
        """An agent could not be reached. Never raises. The first sighting starts an episode (agents.unreachable_since); the alert agent
        is told only after 15 minutes of it, at most once an hour per agent. After 3 hours (or at once for an orchestrator with a run
        in progress) a human is told too, once per episode. Humans are never alerted about."""
        try:
            t = now if now is not None else self.now()
            if to["kind"] == "human":
                self.clear_unreachable(to["name"])
                return
            self.s.run("UPDATE agents SET unreachable_since = COALESCE(unreachable_since, ?) WHERE name = ?", since if since is not None else t, to["name"])
            a = self.s.one("SELECT unreachable_since, alerted_at, escalated_at FROM agents WHERE name = ?", to["name"])
            if not a:
                return
            waited = t - a["unreachable_since"]
            orch = self.be.orch_row(to["orchestrator"]) if to["kind"] == "orchestrator" and to["orchestrator"] else None
            running = bool(orch) and orch["desired"] == "running" and orch["state"] == "running"
            what = f" Message #{row['id']} from {row['sender']} is stored and will be delivered when {to['name']} is reachable." if row else ""
            body = f"{to['name']} is unreachable: {r['reason']}.{what}"
            if waited >= ALERT_AFTER:
                claim = self.s.run("UPDATE agents SET alerted_at = ? WHERE name = ? AND (alerted_at IS NULL OR alerted_at <= ?)",
                                   t, to["name"], t - ALERT_EVERY).rowcount
                if claim:
                    try:
                        sent = self.alert_to_agent(f"unreachable:{to['name']}:{t // ALERT_EVERY}", body,
                                                   avoid=[to["name"], row["sender"] if row else None])
                    except Exception as e:
                        print("bus ops alert threw", repr(e), file=sys.stderr, flush=True)
                        sent = "failed"
                    # A rejected or failed enqueue must not use up the hourly claim: put it back so the next sweep retries.
                    if sent == "failed":
                        self.s.run("UPDATE agents SET alerted_at = ? WHERE name = ? AND alerted_at = ?", a["alerted_at"], to["name"], t)
            if not a["escalated_at"] and (waited >= ESCALATE_AFTER or running):
                if self.s.run("UPDATE agents SET escalated_at = ? WHERE name = ? AND escalated_at IS NULL", t, to["name"]).rowcount:
                    why = (f"{to['name']} is an orchestrator with a run in progress" if running
                           else f"{to['name']} has been unreachable for {round(waited / 3600)} hours")
                    ask = f"{why}: {r['reason']}.{what} Needs a human to look."
                    self.be.alert(f"bus-escalate:{to['name']}:{a['unreachable_since']}", ask, actor="agent-bus")
                    gil = self.s.one("SELECT name FROM agents WHERE name = 'gil' AND retired_at IS NULL")
                    if gil and gil["name"] != to["name"]:
                        self.send({"from": SYSTEM, "to": "gil", "thread": "alerts", "urgent": True, "reply_expected": False,
                                   "client_msg_id": f"escalate:{to['name']}:{a['unreachable_since']}", "body": ask}, now=now)
        except Exception as e:
            print("bus unreachable alert failed", repr(e), file=sys.stderr, flush=True)

    def clear_unreachable(self, name):
        """The episode is over (alerted_at stays, so the hourly limit survives a flapping agent)."""
        self.s.run("UPDATE agents SET unreachable_since = NULL, escalated_at = NULL WHERE name = ? AND unreachable_since IS NOT NULL", name)

    def fallback_legacy_notice(self, meta):
        """A bus finish notice never arrived: send it again through the normal bot path (and Chief copies)."""
        try:
            task = self.s.one("SELECT * FROM tasks WHERE id = ?", meta.get("task_id"))
            if task:
                self.be.notify_task(task, no_bus=True)
        except Exception as e:
            print("bus notice fallback failed", repr(e), file=sys.stderr, flush=True)

    # ---------------- finish notices (agents.notices = 'bus') ----------------
    def send_notice(self, agent, task, payload):
        """A finish notice over the bus, urgent so it wakes the owner now. True when stored."""
        try:
            thread = re.sub(r"[^a-z0-9:._+-]", "-", ("task:" + str(task["id"])).lower())[:80]
            cmid = re.sub(r"[^A-Za-z0-9._:-]", "_", str(payload["idempotency_key"]))
            if len(cmid) > 100:
                cmid = cmid[:35] + ":" + hashlib.sha256(cmid.encode()).hexdigest()[:48]
            st, data = self.send({"from": SYSTEM, "to": agent["name"], "thread": thread, "client_msg_id": cmid,
                                  "body": self.notice_body(task, payload), "meta": payload, "urgent": True, "reply_expected": False})
            if st in (200, 201):
                return True
            print("bus notice rejected", task["id"], st, data.get("error"), file=sys.stderr, flush=True)
        except Exception as e:
            print("bus notice failed", task["id"], repr(e), file=sys.stderr, flush=True)
        return False

    def notice_body(self, task, p):
        lines = [f"[swarm] Task {task['id']} {p.get('state') or task['state']}/{p.get('outcome') or 'none'}: {p.get('title') or p.get('summary') or ''}"]
        if p.get("summary"):
            lines.append(p["summary"])
        if p.get("pr_url"):
            lines.append(f"PR: {p['pr_url']}")
        if p.get("head_sha"):
            lines.append(f"Head: {p['head_sha']}")
        if p.get("landed") is not None:
            lines.append(f"Landed on GitHub: {'yes' if p['landed'] else 'no'}")
        if p.get("question"):
            lines.append(f"Question: {p['question']}")
        gw = swarm_gitlink.warning_line(p.get("gitlink_skipped") or [], p.get("gitlink_skipped_count") or 0)
        if gw:
            lines.append(gw)  # before the report excerpt, so a long report cannot push it out
        lines.append(f"Report: GET /tasks/{task['id']}/report (your bus token can read it)")
        body = self.be.scrub("\n".join(lines))
        room = BODY_MAX - len(body) - 40
        if room > 100:
            f = self.s.one("SELECT data FROM files WHERE task_id = ? AND kind = 'report' ORDER BY at DESC LIMIT 1", task["id"])
            ex = bytes(f["data"]).decode(errors="replace").strip()[:1500] if f else ""
            if ex and not self.be.secret_hit(ex):
                body += "\n\n--- REPORT.md (excerpt) ---\n" + self.be.scrub(ex)[:min(1500, room)]
        return clean_body(body)[:BODY_MAX]

    # ---------------- the delivery loop ----------------
    def flush(self, now=None, force=False):
        """Expire stale messages, retry due webhooks, send digests, re-check unreachable agents, alert senders, apply retention."""
        n = now if now is not None else self.now()
        stats = {"retried": 0, "alerts": 0}
        for m in self.s.q("SELECT m.* FROM bus_messages m JOIN agents a ON a.name = m.recipient AND (a.wake != 'webhook' OR m.held = 1)"
                          " WHERE m.state = 'queued' AND m.created_at <= ? ORDER BY m.id LIMIT 50", n - HELD_EXPIRE):
            try:
                if self.expire_message(m, n):
                    stats["expired"] = stats.get("expired", 0) + 1
            except Exception as e:
                print("bus expiry failed", repr(e), file=sys.stderr, flush=True)
        # New webhook messages (no schedule yet) go out at once; failed ones wait for next_attempt_at.
        for d in self.s.q("SELECT m.id FROM bus_messages m JOIN agents a ON a.name = m.recipient AND a.wake = 'webhook'"
                          " WHERE m.state = 'queued' AND m.held = 0 AND (m.lease_until IS NULL OR m.lease_until < ?)"
                          " AND (m.next_attempt_at IS NULL OR m.next_attempt_at <= ?) ORDER BY m.id LIMIT 50", n, n):
            try:
                if self.webhook_round(d["id"], delays=self.inline_delays if now is None else (0,), now=now):
                    stats["retried"] += 1
            except Exception as e:
                print("bus retry failed", repr(e), file=sys.stderr, flush=True)
        for h in self.s.q("SELECT a.name, a.digest_minutes, a.last_digest_at, MIN(m.created_at) AS oldest FROM agents a JOIN bus_messages m"
                          " ON m.recipient = a.name AND m.held = 1 AND m.state = 'queued' WHERE a.wake = 'webhook' AND a.retired_at IS NULL"
                          " GROUP BY a.name"):
            iv = max(DIGEST_MIN, h["digest_minutes"] if h["digest_minutes"] is not None else DIGEST_DEFAULT) * 60
            if h["oldest"] + iv > n or (h["last_digest_at"] is not None and h["last_digest_at"] + iv > n):
                continue
            try:
                if self.digest_round(h["name"], now=now):
                    stats["digests"] = stats.get("digests", 0) + 1
            except Exception as e:
                print("bus digest failed", repr(e), file=sys.stderr, flush=True)
        for a in self.s.q("SELECT * FROM agents WHERE unreachable_since IS NOT NULL AND retired_at IS NULL LIMIT 50"):
            try:
                r = self.reachability(a)
                if r["reachable"]:
                    self.clear_unreachable(a["name"])
                else:
                    self.alert_unreachable(a, None, r, now=now)
            except Exception as e:
                print("bus episode check failed", repr(e), file=sys.stderr, flush=True)
        # Repair: a stored tmux-wake message whose orchestrator message row is missing (dispatch failed after the row committed).
        for m in self.s.q("SELECT m.* FROM bus_messages m JOIN agents a ON a.name = m.recipient AND a.wake = 'tmux' AND a.orchestrator IS NOT NULL"
                          " WHERE m.state = 'queued' AND m.noop = 0 AND m.created_at <= ? AND NOT EXISTS"
                          " (SELECT 1 FROM orchestrator_messages o WHERE o.bus_message_id = m.id) ORDER BY m.id LIMIT 50", n - 30):
            self.repair_tmux(m, self.s.one("SELECT * FROM agents WHERE name = ?", m["recipient"]))
            stats["tmux_repaired"] = stats.get("tmux_repaired", 0) + 1
        # Episode discovery: queued mail from a non-system sender and no open episode (the write at send time failed). Webhook agents are
        # excluded (failed delivery starts theirs); humans never alert.
        for x in self.s.q("SELECT DISTINCT a.name FROM agents a JOIN bus_messages m ON m.recipient = a.name AND m.state = 'queued' AND m.noop = 0"
                          " AND m.sender != ? WHERE a.unreachable_since IS NULL AND a.retired_at IS NULL AND a.kind != 'human'"
                          " AND a.wake != 'webhook' LIMIT 50", SYSTEM):
            try:
                a = self.s.one("SELECT * FROM agents WHERE name = ?", x["name"])
                r = self.reachability(a)
                if not r["reachable"]:
                    self.alert_unreachable(a, None, r, now=now)
            except Exception as e:
                self.log("bus episode discovery failed", None, e)
        for m in self.s.q("SELECT * FROM bus_messages WHERE alerted_at IS NULL AND (state = 'failed' OR (state = 'queued' AND urgent = 1"
                          " AND sender != ? AND created_at <= ?)) ORDER BY id LIMIT 50", SYSTEM, n - URGENT_ALERT):
            claimed = None
            try:
                if not self.s.run("UPDATE bus_messages SET alerted_at = ? WHERE id = ? AND alerted_at IS NULL", n, m["id"]).rowcount:
                    continue
                claimed = m["id"]
                failed = m["state"] == "failed"
                if m["sender"] == SYSTEM:
                    # A failed finish notice carries the task payload: send it again the normal way and tell the alert agent.
                    # Alerts themselves (no task payload) are never alerted about, so this cannot loop.
                    meta = parse_meta(m["meta"]) or {}
                    if failed and meta.get("task_id"):
                        self.fallback_legacy_notice(meta)
                        to = self.s.one("SELECT * FROM agents WHERE name = ?", m["recipient"])
                        if to:
                            self.alert_unreachable(to, m, {"reason": m["last_error"] or "delivery failed"}, now=now, since=m["created_at"])
                        stats["alerts"] += 1
                    continue
                sender = self.s.one("SELECT name FROM agents WHERE name = ? AND retired_at IS NULL", m["sender"])
                if not sender:
                    continue
                text = (f"Message #{m['id']} to {m['recipient']} (thread {m['thread']}) failed: {m['last_error'] or 'delivery attempts exhausted'}. "
                        "It was not delivered.") if failed else \
                    f"Urgent message #{m['id']} to {m['recipient']} (thread {m['thread']}) is still undelivered after 5 minutes."
                st, data = self.send({"from": SYSTEM, "to": sender["name"], "thread": "alerts", "urgent": True,
                                      "client_msg_id": f"{'failed' if failed else 'urgent'}:{m['id']}", "body": text}, now=now)
                if st not in (200, 201):
                    raise RuntimeError(f"alert enqueue rejected ({st}): {str(data.get('error') or '')[:150]}")
                if failed:
                    to = self.s.one("SELECT * FROM agents WHERE name = ?", m["recipient"])
                    if to:
                        self.alert_unreachable(to, m, {"reason": m["last_error"] or "delivery failed"}, now=now, since=m["created_at"])
                stats["alerts"] += 1
            except Exception as e:
                print("bus alert failed", repr(e), file=sys.stderr, flush=True)
                # A failed alert is a failure, not an alert: stay retryable.
                if claimed is not None:
                    self.s.run("UPDATE bus_messages SET alerted_at = NULL WHERE id = ?", claimed)
                    stats["alertFailures"] = stats.get("alertFailures", 0) + 1
        if force or (n // 60) % 60 == 17:  # hourly, bounded batches
            self.s.run("DELETE FROM bus_attempts WHERE id IN (SELECT id FROM bus_attempts WHERE started_at < ? ORDER BY started_at LIMIT ?)",
                       n - 30 * 86400, RETENTION_BATCH)
            self.s.run("DELETE FROM bus_messages WHERE id IN (SELECT id FROM bus_messages WHERE created_at < ? ORDER BY created_at LIMIT ?)",
                       n - 90 * 86400, RETENTION_BATCH)
        return stats

    # ---------------- HTTP ----------------
    def inbox(self, me, a, qs):
        all_ = qs.get("all") == "1"
        after = int(qs.get("after") or 0) if str(qs.get("after") or "0").isdigit() else 0
        lim = self._int(qs.get("limit"), 50, 100)
        rows = self.s.q(f"SELECT * FROM bus_messages WHERE recipient = ? AND id > ? {'' if all_ else 'AND state != ' + chr(39) + 'read' + chr(39)}"
                        " ORDER BY id LIMIT ?", a["name"], after, lim)
        t, msgs = self.now(), []
        for r in rows:
            if r["state"] == "queued":
                if self.s.run("UPDATE bus_messages SET state = 'delivered', held = 0, delivered_at = ?, lease_until = NULL"
                              " WHERE id = ? AND state = 'queued'", t, r["id"]).rowcount:
                    self.s.run("INSERT INTO bus_attempts(message_id, transport, started_at, finished_at, ok) VALUES(?, 'poll', ?, ?, 1)", r["id"], t, t)
                    r["state"], r["delivered_at"] = "delivered", t
            msgs.append({**self.shape_msg(r), **self.thread_stats(r)})
        return {"agent": a["name"], "messages": msgs}

    @staticmethod
    def _int(v, d, mx):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return d
        return min(n, mx) if n > 0 else d

    def ack(self, msg):
        t = self.now()
        first = self.s.run("UPDATE bus_messages SET state = 'read', read_at = ?, delivered_at = COALESCE(delivered_at, ?), lease_until = NULL"
                           " WHERE id = ? AND read_at IS NULL", t, t, msg["id"]).rowcount
        return {"ok": True, "id": msg["id"], "thread": msg["thread"], "first": bool(first), **self.thread_stats(msg)}

    def message_detail(self, msg):
        atts = self.s.q("SELECT id, transport, started_at, finished_at, ok, http_status, error FROM bus_attempts WHERE message_id = ? ORDER BY id", msg["id"])
        return {**self.shape_msg(msg), "attempt_rounds": msg["attempts"], "attempts": [dict(x, ok=bool(x["ok"])) for x in atts]}

    def put_agent(self, name, b):
        """Register or change an agent (admin). Returns (status, data)."""
        if not NAME_RE.match(name) or name == SYSTEM:
            return 400, {"error": "name must be lowercase letters, numbers and hyphens, 2-32 characters"}
        cur = self.s.one("SELECT * FROM agents WHERE name = ?", name)

        def pick(k, d=None):
            return b[k] if b.get(k) is not None else (cur[k] if cur and cur.get(k) is not None else d)
        kind, wake, notices = pick("kind"), pick("wake"), pick("notices", "legacy")
        if kind not in KINDS:
            return 400, {"error": "kind must be bot, orchestrator, human or service"}
        if wake not in WAKES:
            return 400, {"error": "wake must be webhook, tmux or poll"}
        if notices not in ("legacy", "bus"):
            return 400, {"error": "notices must be legacy or bus"}
        dm = pick("digest_minutes", DIGEST_DEFAULT)
        if not isinstance(dm, int) or isinstance(dm, bool) or dm < DIGEST_MIN or dm > DIGEST_MAX:
            return 400, {"error": f"digest_minutes must be a whole number from {DIGEST_MIN} to {DIGEST_MAX}"}
        # poll_window_minutes: wake poll only; sent null clears it, left out keeps it (and drops it if the wake is no longer poll).
        if "poll_window_minutes" in b:
            pw = b["poll_window_minutes"]
            if pw is not None and (wake != "poll" or not isinstance(pw, int) or isinstance(pw, bool) or pw < POLL_WINDOW_MIN or pw > POLL_WINDOW_MAX):
                return 400, {"error": f"poll_window_minutes is for wake poll only: a whole number from {POLL_WINDOW_MIN} to {POLL_WINDOW_MAX} (or null)"}
        else:
            pw = cur.get("poll_window_minutes") if cur and wake == "poll" else None
        bot_id = orch = None
        if wake == "webhook":
            raw = pick("bot_id")
            bot = self.be.bot(str(raw)) if raw else None
            if not bot:
                return 400, {"error": "wake webhook needs bot_id of an existing bot"}
            bad = self.h["valid_notify_url"](bot["notify_url"]) if bot.get("notify_url") else "no notify_url set"
            if bad:
                return 400, {"error": f"bot {bot['id']} has no usable notify_url: {bad}"}
            bot_id = bot["id"]
        elif wake == "tmux":
            raw = pick("orchestrator")
            if not raw or not self.be.orch_row(str(raw)):
                return 400, {"error": "wake tmux needs an existing orchestrator"}
            orch = str(raw)
        aliases = None
        if b.get("aliases") is not None:
            if not isinstance(b["aliases"], list):
                return 400, {"error": "aliases must be an array"}
            aliases = list(dict.fromkeys(norm_name(x) for x in b["aliases"]))
            if any(not ALIAS_RE.match(x) for x in aliases):
                return 400, {"error": "alias must be 1-80 characters: letters, numbers, and . _ : @ + -"}
            aliases = [x for x in aliases if x != name]
            # Names and aliases share one address space.
            for al in aliases:
                n = self.s.one("SELECT name FROM agents WHERE name = ?", al)
                o = self.s.one("SELECT name FROM agent_aliases WHERE alias = ? AND name != ?", al, name)
                if n or o:
                    return 409, {"error": f"alias {al} is already used by {(n or o)['name']}"}
        if not cur and self.s.one("SELECT name FROM agent_aliases WHERE alias = ?", name):
            return 409, {"error": f"{name} is already an alias of another agent"}
        t = self.now()
        desc = str(b["description"] if b.get("description") is not None else (cur or {}).get("description") or "")[:500] or None
        with self.s.tx():
            self.s.run("INSERT INTO agents(name, kind, wake, bot_id, orchestrator, description, notices, digest_minutes, created_at, updated_at)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET kind = excluded.kind, wake = excluded.wake,"
                       " bot_id = excluded.bot_id, orchestrator = excluded.orchestrator, description = excluded.description,"
                       " notices = excluded.notices, digest_minutes = excluded.digest_minutes, updated_at = excluded.updated_at,"
                       " retired_at = NULL", name, kind, wake, bot_id, orch, desc, notices, dm, t, t)
            if aliases is not None:
                self.s.run("DELETE FROM agent_aliases WHERE name = ?", name)
                for al in aliases:
                    self.s.run("INSERT INTO agent_aliases(alias, name, created_at) VALUES(?,?,?)", al, name, t)
            self.s.run("UPDATE agents SET poll_window_minutes = ? WHERE name = ?", pw, name)
        return (200 if cur else 201), self.shape_agent(self.s.one("SELECT * FROM agents WHERE name = ?", name))

    # ---------------- human page ----------------
    def page(self):
        """A plain HTML view: agents with reachability, then the last 100 messages. Read only; bodies are cut short."""
        e = html.escape
        agents = [self.shape_agent(a) for a in self.s.q("SELECT * FROM agents WHERE retired_at IS NULL ORDER BY name")]
        msgs = self.s.q("SELECT * FROM bus_messages ORDER BY id DESC LIMIT 100")
        ts = lambda v: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(v)) if v else ""  # noqa: E731
        rows_a = "".join(
            f"<tr><td>{e(a['name'])}</td><td>{e(a['kind'])}</td><td>{e(a['wake'])}</td><td>{e(a['notices'])}</td><td>{a['digest_minutes']}</td>"
            f"<td>{'yes' if a['reachable'] else 'NO: ' + e(str(a['reason']))}</td><td>{e(ts(a['last_seen_at']))}</td></tr>" for a in agents)
        rows_m = []
        for m in msgs:
            at = self.s.q("SELECT transport, ok, error FROM bus_attempts WHERE message_id = ? ORDER BY id", m["id"])
            att = ", ".join(f"{x['transport']} {'ok' if x['ok'] else 'FAILED ' + str(x['error'] or '')[:80]}" for x in at)
            body = self.be.scrub(m["body"]).replace("\n", " ")[:200]
            rows_m.append(f"<tr><td>{m['id']}</td><td>{e(ts(m['created_at']))}</td><td>{e(m['sender'])} &rarr; {e(m['recipient'])}</td>"
                          f"<td>{e(m['thread'])}</td><td>{e(m['state'])}{' (held)' if m['held'] else ''}{' urgent' if m['urgent'] else ''}</td>"
                          f"<td>{e(body)}</td><td>{e(att)}{' ' + e(str(m['last_error'] or '')) if m['last_error'] else ''}</td></tr>")
        return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Agent Bus</title>"
                "<style>body{font:14px sans-serif;margin:1.5em}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:3px 8px;text-align:left}"
                "</style></head><body><h1>Agent Bus</h1><h2>Agents</h2><table><tr><th>name</th><th>kind</th><th>wake</th><th>notices</th>"
                f"<th>digest min</th><th>reachable</th><th>last seen (UTC)</th></tr>{rows_a}</table><h2>Last 100 messages</h2><table><tr><th>id</th>"
                f"<th>time (UTC)</th><th>from &rarr; to</th><th>thread</th><th>state</th><th>body</th><th>attempts</th></tr>{''.join(rows_m)}</table>"
                "</body></html>")
