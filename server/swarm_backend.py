#!/usr/bin/env python3
"""swarm-backend: the task backend for agent-swarm-kit.

One small HTTP service plus a SQLite file. Python standard library only.
It holds tasks, their event threads, questions and answers, reports and
artifacts, bots and where to notify them, and the "Waiting on you" list.

Run:  python3 swarm_backend.py              (reads settings from the environment)
      python3 swarm_backend.py --permissions  (print the permission matrix as a Markdown table)
Env:  SWARM_DB                 path to the SQLite file (default ~/.local/share/swarm/swarm.db)
      SWARM_BIND               host:port to listen on (default 127.0.0.1:8750)
      SWARM_PUBLIC_URL         URL bots use to reach this service (used in report links)
      SWARM_ADMIN_TOKEN_FILE   file holding the admin token (owner of the swarm)
      SWARM_BACKEND_WORKER_TOKEN_FILE  file holding the worker token, for checking it
      SWARM_BOT_KEYS_DIR       folder for bot trigger keys (default ~/.config/swarm/bot-keys).
                               Keys never go in the database, so backups never hold them.
      SWARM_ALLOW_HTTP_NOTIFY  1 to allow plain http and private notify URLs (local testing only)
      SWARM_MAX_ATTEMPTS       default attempts per task before it stops resuming (default 5)
      SWARM_TASK_TOKEN_MAX_HOURS  hard maximum age of a task token (default 8)
      SWARM_MAX_CONNECTIONS    requests handled at once (default 32)
      SWARM_AUTH_FAIL_LIMIT    failed logins per client per 5 minutes before HTTP 429 (default 20)
      SWARM_MAX_OPEN_TASKS_PER_OWNER  unfinished tasks one owner may have (default 100)
      SWARM_REMIND_HOURS, SWARM_ESCALATE_HOURS  waiting-task reminder and escalation (12 and 24)
      SWARM_TASK_TIMEOUT       seconds a run may take, used by the watchdog (default 14400)
      SWARM_FILE_RETENTION_DAYS  delete files of tasks finished this long ago (default 90, 0 keeps all)
      SWARM_DB_BACKUP_DIR      if set, /status warns when the newest backup is older than 36 hours

Auth: every request except GET /health needs "Authorization: Bearer <token>".
The full permission matrix is in docs/backend.md and comes from ROUTES below.
  admin token    everything
  chief bot      what admin does, except worker and task calls and making tokens
  project bot    only its own tasks, items and settings, and projects it is bound to
  worker token   claim new work, read task states for cleanup, send alerts. Nothing else.
  task token     minted at claim, one task and one claim only: heartbeat, read its own task,
                 upload its own files, publish check, finish. Dies when the task leaves running.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.2.0"
MAX_FILE = 25 * 1024 * 1024
MAX_BUNDLE = int(os.environ.get("SWARM_MAX_BUNDLE_MB", "100")) * 1024 * 1024
MAX_TASK_BODY = 48 * 1024 * 1024
MAX_SMALL_BODY = 1024 * 1024
TERMINAL = ("done", "failed", "cancelled")
NOTIFY_STATES = ("done", "failed", "cancelled", "waiting")
LEASE_SECONDS = 600
WORKER_SILENT_SECONDS = 600


def now():
    return int(time.time())


def env_path(name, default):
    return os.path.expanduser(os.environ.get(name) or default)


def env_int(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return int(default)


def read_token(name):
    path = os.environ.get(name)
    if not path:
        return None
    try:
        with open(os.path.expanduser(path)) as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS projects(
  name TEXT PRIMARY KEY, default_repo TEXT, default_provider TEXT, default_model TEXT,
  allow_push INTEGER NOT NULL DEFAULT 1, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, created_at INTEGER, updated_at INTEGER, project TEXT, owner TEXT, kind TEXT,
  title TEXT, prompt TEXT NOT NULL, prompt_hash TEXT, repo TEXT, branch TEXT, pr_number INTEGER,
  provider TEXT, model TEXT, command TEXT, report_only INTEGER DEFAULT 0, push INTEGER DEFAULT 0,
  open_pr INTEGER DEFAULT 0, ask_human INTEGER DEFAULT 0, untrusted INTEGER DEFAULT 0,
  idempotency_key TEXT, state TEXT NOT NULL, outcome TEXT, waiting_on TEXT, question TEXT,
  next_step TEXT, pending_answer TEXT, session_provider TEXT, session_id TEXT,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 5, not_before INTEGER DEFAULT 0,
  lease_worker TEXT, lease_until INTEGER, cancel_requested INTEGER DEFAULT 0, pr_url TEXT,
  summary TEXT, error TEXT, finished_at INTEGER, parent_id TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at INTEGER, type TEXT, actor TEXT, text TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at INTEGER, sender TEXT, text TEXT, delivered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS files(
  task_id TEXT, kind TEXT, name TEXT, data BLOB, size INTEGER, at INTEGER, PRIMARY KEY(task_id, kind, name));
CREATE TABLE IF NOT EXISTS bots(
  id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'project', notify_url TEXT, notify_auth TEXT,
  token_hash TEXT, created_at INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, project TEXT, bot TEXT, kind TEXT, ask TEXT, recommendation TEXT, options TEXT,
  links TEXT, dedupe_key TEXT, task_id TEXT, status TEXT NOT NULL DEFAULT 'open', snooze_until INTEGER,
  answer TEXT, revision INTEGER DEFAULT 1, created_at INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, event_id TEXT, target TEXT, reason TEXT,
  url TEXT, payload TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, next_at INTEGER,
  last_error TEXT, created_at INTEGER, sent_at INTEGER);
CREATE TABLE IF NOT EXISTS claims(
  claim_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, worker TEXT,
  created_at INTEGER, expires_at INTEGER, ended_at INTEGER, end_reason TEXT, finish_response TEXT);
CREATE TABLE IF NOT EXISTS workers(name TEXT PRIMARY KEY, last_seen INTEGER, draining INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

# Columns added after 0.1.0. Added in place on an existing database.
MIGRATE_COLUMNS = {
    "tasks": [("claim_id", "TEXT"), ("claimed_at", "INTEGER"), ("follow_up", "INTEGER DEFAULT 0"),
              ("held_behind", "TEXT"), ("push_main", "INTEGER DEFAULT 0"), ("pushed_branch", "TEXT"),
              ("workdir", "TEXT"), ("workspace_host", "TEXT"), ("session_model", "TEXT"),
              ("unpushed", "INTEGER DEFAULT 0"), ("human_snooze_until", "INTEGER"),
              ("waiting_since", "INTEGER"), ("reminded_at", "INTEGER"), ("escalated_at", "INTEGER"),
              ("start_fresh", "INTEGER DEFAULT 0")],
    "bots": [("notify_auth_fp", "TEXT"), ("notify_version", "INTEGER DEFAULT 0")],
    "projects": [("bots", "TEXT"), ("repos", "TEXT"), ("allow_push_main", "INTEGER DEFAULT 0")],
    "notices": [("event", "TEXT"), ("notify_version", "INTEGER DEFAULT 0")],
}

SCHEMA_INDEXES = """
DROP INDEX IF EXISTS tasks_idem;
CREATE UNIQUE INDEX IF NOT EXISTS tasks_idem_owner ON tasks(coalesce(lower(owner), ''), idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, not_before);
CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS messages_task ON messages(task_id, delivered);
CREATE INDEX IF NOT EXISTS claims_task ON claims(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS items_dedupe ON items(bot, dedupe_key) WHERE status IN ('open','snoozed');
"""


class KeyStore:
    """Bot trigger keys live in files (one per bot, mode 600), never in the database."""

    def __init__(self, path):
        self.dir = path

    def _path(self, bot_id):
        if not ID_RE.match(bot_id) or ".." in bot_id:
            raise ValueError("bad bot id")
        return os.path.join(self.dir, bot_id + ".key")

    def write(self, bot_id, value):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        path = self._path(bot_id)
        tmp = f"{path}.tmp-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, value.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def read(self, bot_id):
        try:
            fd = os.open(self._path(bot_id), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        with os.fdopen(fd) as fh:
            return fh.read().strip() or None

    def delete(self, bot_id):
        try:
            os.remove(self._path(bot_id))
        except OSError:
            pass


class Store:
    """All database access. One connection per thread; writes are serialized by SQLite."""

    def __init__(self, path, keys=None):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.local = threading.local()
        self.lock = threading.RLock()
        with self.lock:
            db = self.db()
            db.executescript(SCHEMA_TABLES)
            for table, cols in MIGRATE_COLUMNS.items():
                have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
                for name, decl in cols:
                    if name not in have:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            db.executescript(SCHEMA_INDEXES)
            if keys is not None:
                self.move_keys_out(keys)

    def move_keys_out(self, keys):
        """One-time migration: trigger keys from the 0.1.0 bots.notify_auth column go to key files."""
        rows = self.q("SELECT id, notify_auth FROM bots WHERE notify_auth IS NOT NULL AND notify_auth != ''")
        if not rows:
            return 0
        for r in rows:
            keys.write(r["id"], r["notify_auth"].strip())
            self.run("UPDATE bots SET notify_auth = NULL, notify_auth_fp = ?, notify_version = notify_version + 1 WHERE id = ?",
                     fingerprint(r["notify_auth"].strip()), r["id"])
            self.run("UPDATE notices SET notify_version = (SELECT notify_version FROM bots WHERE id = ?)"
                     " WHERE target = ? AND state = 'pending'", r["id"], r["id"])
        # Rewrite the file so the old values are not left in free pages.
        self.run("PRAGMA wal_checkpoint(TRUNCATE)")
        self.run("VACUUM")
        print(f"moved {len(rows)} trigger key(s) out of the database into {keys.dir}", file=sys.stderr, flush=True)
        return len(rows)

    def db(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self.local.conn = conn
        return conn

    def q(self, sql, *args):
        return [dict(r) for r in self.db().execute(sql, args).fetchall()]

    def one(self, sql, *args):
        r = self.db().execute(sql, args).fetchone()
        return dict(r) if r else None

    def run(self, sql, *args):
        return self.db().execute(sql, args)

    def tx(self):
        return _Tx(self)


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT. Nested use joins the outer transaction."""

    def __init__(self, store):
        self.store = store
        self.outer = False

    def __enter__(self):
        self.store.lock.acquire()
        depth = getattr(self.store.local, "depth", 0)
        self.outer = depth == 0
        if self.outer:
            self.store.db().execute("BEGIN IMMEDIATE")
        self.store.local.depth = depth + 1
        return self.store

    def __exit__(self, exc_type, exc, tb):
        try:
            self.store.local.depth -= 1
            if self.outer:
                self.store.db().execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store.lock.release()
        return False


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def new_id(prefix="t"):
    return prefix + time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)


def clean_text(value, limit, field):
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ApiError(400, f"{field} must be text")
    value = str(value).strip()
    if len(value) > limit:
        raise ApiError(400, f"{field} is too long (max {limit})")
    return value


def text_list(value, field, item_re=None, limit=50):
    if value is None:
        return None
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    if not isinstance(value, list) or len(value) > limit:
        raise ApiError(400, f"{field} must be a list (max {limit})")
    out = []
    for v in value:
        v = clean_text(v, 200, field)
        if item_re and not item_re.match(v):
            raise ApiError(400, f"bad value in {field}: {v!r}")
        out.append(v)
    return out


def norm_repo(repo):
    repo = re.sub(r"^https://github\.com/", "", repo).removesuffix(".git").strip("/")
    if not REPO_RE.match(repo):
        raise ApiError(400, "repo must look like owner/name")
    return repo


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}$")
BRANCH_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<!/)(?<!\.lock)$")


# ---------- outbound notices: SSRF checks ----------
def allow_private_notify():
    return os.environ.get("SWARM_ALLOW_HTTP_NOTIFY") == "1"


def public_addresses(host, port):
    """Resolve host and return its addresses. Every address must be public, or ValueError."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ValueError(f"cannot resolve {host}: {e}")
    addrs = []
    for info in infos:
        a = info[4][0].split("%", 1)[0]
        ip = ipaddress.ip_address(a)
        mapped = getattr(ip, "ipv4_mapped", None)
        for check in (ip, mapped):
            if check is not None and (not check.is_global or check.is_multicast or check.is_reserved
                                      or check.is_loopback or check.is_link_local or check.is_private):
                raise ValueError(f"{host} resolves to a private or local address ({a}); notices only go to public addresses")
        if a not in addrs:
            addrs.append(a)
    if not addrs:
        raise ValueError(f"cannot resolve {host}")
    return addrs


def valid_notify_url(url, resolve=True):
    """Return None when the URL is fine, otherwise the reason."""
    try:
        p = urllib.parse.urlparse(url)
        port = p.port
    except ValueError:
        return "not a valid URL"
    allow = allow_private_notify()
    if p.scheme not in (("https", "http") if allow else ("https",)):
        return "notify_url must use https"
    if p.username or p.password or not p.hostname:
        return "notify_url must have a host and no user name or password in it"
    if len(url) > 2000:
        return "notify_url is too long"
    if allow or not resolve:
        return None
    try:
        public_addresses(p.hostname, port or 443)
    except ValueError as e:
        return str(e)
    return None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a checked IP address, with the certificate checked against the host name."""

    def __init__(self, host, port, addr, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._addr = addr

    def connect(self):
        sock = socket.create_connection((self._addr, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def post_json(url, data, headers, timeout=15):
    """POST once. Never follows redirects. Returns (status, body bytes)."""
    p = urllib.parse.urlparse(url)
    path = (p.path or "/") + (("?" + p.query) if p.query else "")
    port = p.port or (443 if p.scheme == "https" else 80)
    if allow_private_notify():
        cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        conn = cls(p.hostname, port, timeout=timeout)
    else:
        if p.scheme != "https":
            raise ValueError("notify_url must use https")
        conn = PinnedHTTPSConnection(p.hostname, port, public_addresses(p.hostname, port)[0], timeout)
    try:
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read(65536)
    finally:
        conn.close()


def redact_url(url):
    if not url:
        return url
    p = urllib.parse.urlparse(url)
    tail = "/..." if (p.path not in ("", "/") or p.query) else "/"
    return f"{p.scheme}://{p.netloc}{tail}"


class Backend:
    def __init__(self, store, public_url="", keys=None):
        self.s = store
        self.keys = keys or KeyStore(env_path("SWARM_BOT_KEYS_DIR", "~/.config/swarm/bot-keys"))
        self.public_url = public_url.rstrip("/")
        self.admin_token = read_token("SWARM_ADMIN_TOKEN_FILE")
        self.worker_token = read_token("SWARM_BACKEND_WORKER_TOKEN_FILE")
        self.max_attempts = env_int("SWARM_MAX_ATTEMPTS", 5)
        self.token_max_age = env_int("SWARM_TASK_TOKEN_MAX_HOURS", 8) * 3600
        self.max_open = env_int("SWARM_MAX_OPEN_TASKS_PER_OWNER", 100)
        self.started_at = now()
        self.limiter = FailLimiter(env_int("SWARM_AUTH_FAIL_LIMIT", 20))

    # ---------- auth ----------
    def who(self, header):
        if not header or not header.lower().startswith("bearer "):
            raise ApiError(401, "missing bearer token")
        tok = header.split(None, 1)[1].strip() if len(header.split(None, 1)) > 1 else ""
        if not tok:
            raise ApiError(401, "missing bearer token")
        if self.admin_token and hmac.compare_digest(tok, self.admin_token):
            return {"role": "admin", "bot": None}
        if self.worker_token and hmac.compare_digest(tok, self.worker_token):
            return {"role": "worker", "bot": None}
        if tok.startswith("swt_"):
            c = self.s.one("SELECT * FROM claims WHERE token_hash = ?", token_hash(tok))
            if c:
                return {"role": "task", "bot": None, "task": c["task_id"], "claim": c["claim_id"],
                        "active": self.claim_active(c)}
        bot = self.s.one("SELECT id, role FROM bots WHERE token_hash = ?", token_hash(tok))
        if bot:
            return {"role": "chief" if bot["role"] == "chief" else "bot", "bot": bot["id"]}
        raise ApiError(401, "token not accepted")

    def claim_active(self, c, t=None):
        """A task token works only while its claim is the task's current, live claim."""
        if c["ended_at"] or (c["expires_at"] or 0) <= now():
            return False
        t = t or self.s.one("SELECT state, claim_id, lease_until FROM tasks WHERE id = ?", c["task_id"])
        return bool(t and t["state"] == "running" and t["claim_id"] == c["claim_id"] and (t["lease_until"] or 0) >= now())

    @staticmethod
    def need(who, *roles):
        if who["role"] not in roles:
            raise ApiError(403, "this token cannot do that")

    def can_see(self, who, task):
        if who["role"] in ("admin", "chief"):
            return True
        if who["role"] == "task":
            return task["id"] == who["task"]
        return who["role"] == "bot" and (task.get("owner") or "").lower() == who["bot"].lower()

    def get_task(self, who, task_id):
        if who["role"] == "task":
            return self.own_task(who, task_id)
        t = self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
        if not t or not self.can_see(who, t):
            raise ApiError(404, "no such task")
        return t

    def own_task(self, who, task_id, claim_id=None):
        """For task tokens: the task must be the token's own task, and the claim must still be live."""
        if who["role"] != "task":
            raise ApiError(403, "this needs the task's own token")
        if task_id != who["task"]:
            raise ApiError(403, "a task token only works for its own task")
        if claim_id is not None and claim_id != who["claim"]:
            raise ApiError(409, "claim_id does not match this token", {"claim_ended": True})
        t = self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
        c = self.s.one("SELECT * FROM claims WHERE claim_id = ?", who["claim"])
        if not t or not c or not self.claim_active(c, t):
            raise ApiError(409, f"this claim has ended (task is {t['state'] if t else 'gone'})",
                           {"claim_ended": True, "state": t["state"] if t else None})
        return t

    # ---------- events and notices ----------
    def event(self, task_id, typ, actor, text=None, meta=None):
        self.s.run("INSERT INTO events(task_id, at, type, actor, text, meta) VALUES(?,?,?,?,?,?)",
                   task_id, now(), typ, actor, text, json.dumps(meta) if meta else None)

    def chief(self):
        return self.s.one("SELECT * FROM bots WHERE role = 'chief' AND notify_url IS NOT NULL ORDER BY created_at LIMIT 1")

    def payload(self, task, event_id, reason):
        title = next((ln.strip() for ln in (task.get("title") or task["prompt"]).splitlines() if ln.strip()), "")
        unread = self.s.one("SELECT COUNT(*) AS n FROM messages WHERE task_id = ? AND delivered = 0", task["id"])["n"]
        p = {
            "event": "task", "event_id": event_id, "idempotency_key": event_id, "reason": reason,
            "task_id": task["id"], "owner": task.get("owner"), "project": task.get("project"),
            "kind": task.get("kind"), "title": title[:200], "state": task["state"],
            "outcome": task.get("outcome"), "waiting_on": task.get("waiting_on"),
            "summary": (task.get("summary") or task.get("error") or "")[:500] or None,
            "pr_url": task.get("pr_url"), "parent_id": task.get("parent_id"),
            "report_url": f"{self.public_url}/tasks/{task['id']}/report" if self.public_url else None,
        }
        if unread and task["state"] in TERMINAL:
            p["unread_messages"] = unread
        if task["state"] == "waiting":
            p["question"] = task.get("question")
        return p

    def queue_notice(self, event_id, target_bot, reason, payload):
        """Insert one delivery. The UNIQUE key keeps one row per event per bot (delivery is at least once)."""
        if not target_bot or not target_bot.get("notify_url"):
            if reason != "owner":
                print(f"notice {event_id} ({reason}) dropped: no Chief of Staff bot with a notify_url", file=sys.stderr, flush=True)
            return False
        key = f"{event_id}>{target_bot['id']}"
        body = dict(payload, notify_target=target_bot["id"], reason=reason)
        body.setdefault("event", "task")
        cur = self.s.run("INSERT OR IGNORE INTO notices(key, event_id, target, reason, event, url, payload, next_at, created_at,"
                         " notify_version) VALUES(?,?,?,?,?,?,?,?,?,?)", key, event_id, target_bot["id"], reason, body["event"],
                         target_bot["notify_url"], json.dumps(body), now(), now(), target_bot.get("notify_version") or 0)
        return cur.rowcount == 1

    def bot(self, bot_id):
        return self.s.one("SELECT * FROM bots WHERE lower(id) = lower(?)", bot_id) if bot_id else None

    def notify_task(self, task):
        """Route a task event: owner bot first, Chief of Staff only for ownerless, waiting-on-human or fallback."""
        if task["state"] not in NOTIFY_STATES:
            return
        seq = self.s.one("SELECT COUNT(*) AS n FROM events WHERE task_id = ? AND type = 'state' AND text = ?",
                         task["id"], task["state"])["n"]
        event_id = f"{task['id']}:{task['state']}:{seq}"
        owner = self.bot(task.get("owner"))
        chief = self.chief()
        if owner and owner.get("notify_url"):
            self.queue_notice(event_id, owner, "owner", self.payload(task, event_id, "owner"))
            if task["state"] == "waiting" and task.get("waiting_on") == "human" and chief and chief["id"] != owner["id"]:
                self.queue_notice(event_id, chief, "waiting_on_human", self.payload(task, event_id, "waiting_on_human"))
        else:
            reason = "no_owner" if not task.get("owner") else "owner_not_registered"
            self.queue_notice(event_id, chief, reason, self.payload(task, event_id, reason))

    def alert(self, key, text, actor="system", event="alert"):
        event_id = f"alert:{key}"
        payload = {"event": event, "event_id": event_id, "idempotency_key": event_id, "text": text, "from": actor}
        return self.queue_notice(event_id, self.chief(), "alert", payload)

    def set_state(self, task, state, actor, notify=True, **fields):
        fields["state"] = state
        fields["updated_at"] = now()
        if state in TERMINAL:
            fields.setdefault("finished_at", now())
        if state != "running":
            fields["lease_worker"] = None
            fields["lease_until"] = None
        if state == "waiting":
            fields.update(waiting_since=now(), reminded_at=None, escalated_at=None)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.s.run(f"UPDATE tasks SET {cols} WHERE id = ?", *fields.values(), task["id"])
        if state != "running":
            self.s.run("UPDATE claims SET ended_at = ?, end_reason = ? WHERE task_id = ? AND ended_at IS NULL", now(), state, task["id"])
        self.event(task["id"], "state", actor, state, {k: v for k, v in fields.items()
                                                        if k in ("outcome", "waiting_on", "question", "next_step", "error")} or None)
        fresh = self.s.one("SELECT * FROM tasks WHERE id = ?", task["id"])
        if notify:
            self.notify_task(fresh)
        return fresh

    # ---------- projects and binding ----------
    @staticmethod
    def shape_project(p):
        if not p:
            return None
        p = dict(p)
        p["bots"] = json.loads(p.get("bots") or "[]")
        p["repos"] = json.loads(p.get("repos") or "[]")
        return p

    def project(self, name):
        return self.shape_project(self.s.one("SELECT * FROM projects WHERE name = ?", name)) if name else None

    def bound_projects(self, bot_id):
        return [p for p in (self.shape_project(r) for r in self.s.q("SELECT * FROM projects ORDER BY name"))
                if bot_id.lower() in (b.lower() for b in p["bots"])]

    @staticmethod
    def project_repos(p):
        return [r for r in [p.get("default_repo")] + p.get("repos", []) if r]

    # ---------- tasks ----------
    def create_task(self, who, b):
        self.need(who, "admin", "chief", "bot")
        follow = clean_text(b.get("follow_up"), 80, "follow_up")
        if follow:
            return self.follow_up(who, follow, b)
        return self._create(who, b)

    def _create(self, who, b, follow_up=False, held_behind=None):
        prompt = clean_text(b.get("prompt"), 200_000, "prompt")
        command = clean_text(b.get("command"), 4000, "command")
        if not prompt:
            raise ApiError(400, "prompt is required (for a script task, say what the command is for)")
        owner = clean_text(b.get("owner"), 80, "owner") or who["bot"]
        if who["role"] == "bot" and owner and owner.lower() != who["bot"].lower():
            raise ApiError(403, "a project bot can only queue tasks it owns")
        project = clean_text(b.get("project"), 80, "project")
        repo = clean_text(b.get("repo"), 200, "repo")
        repo = norm_repo(repo) if repo else None
        proj = self.project(project)
        if who["role"] == "bot":
            bound = self.bound_projects(who["bot"])
            if project and not any(p["name"] == project for p in bound):
                raise ApiError(403, f"{who['bot']} is not bound to project {project}. The owner can bind it with: "
                                    f"swarmctl project set {project} --bot {who['bot']}")
            if repo and not project:
                match = [p for p in bound if repo in self.project_repos(p)]
                if not match:
                    raise ApiError(403, f"{who['bot']} may not work on {repo}: no project bound to this bot lists that repo")
                proj, project = match[0], match[0]["name"]
        repo = repo or (proj or {}).get("default_repo")
        if repo and proj and repo not in self.project_repos(proj) and who["role"] == "bot":
            raise ApiError(403, f"repo {repo} is not one of project {project}'s repos")
        if project and not proj and (repo or b.get("push") or b.get("open_pr") or b.get("push_main")):
            raise ApiError(400, f"unknown project {project}. Create it first: swarmctl project set {project} --repo OWNER/REPO")
        provider = clean_text(b.get("provider"), 20, "provider") or (proj or {}).get("default_provider") or os.environ.get("SWARM_DEFAULT_PROVIDER", "claude")
        if provider not in ("claude", "codex"):
            raise ApiError(400, "provider must be claude or codex")
        model = clean_text(b.get("model"), 80, "model") or (proj or {}).get("default_model") or os.environ.get("SWARM_DEFAULT_MODEL", "default")
        report_only = 1 if b.get("report_only") or command else 0
        untrusted = 1 if (b.get("untrusted_input") or b.get("untrusted")) else 0
        push_main = 1 if b.get("push_main") else 0
        push = 1 if (b.get("push") or b.get("open_pr") or push_main) else 0
        if b.get("pr_number") is not None and not (report_only or untrusted):
            push = 1  # working on an existing PR means updating it
        open_pr = 1 if b.get("open_pr") and not push_main else 0
        if (push or open_pr) and (report_only or untrusted):
            raise ApiError(400, "report-only, script and untrusted-input tasks cannot push or open a PR")
        if (push or open_pr) and not repo:
            raise ApiError(400, "push and open_pr need a repo")
        if push and proj and not proj.get("allow_push"):
            raise ApiError(400, f"project {project} does not allow pushing")
        if push_main and not (proj and proj.get("allow_push_main")):
            raise ApiError(400, "push-main is off for this project. The owner can allow it with: "
                                f"swarmctl project set {project or 'NAME'} --allow-push-main")
        branch = clean_text(b.get("branch"), 200, "branch")
        if branch and not BRANCH_RE.match(branch):
            raise ApiError(400, "bad branch name")
        kind = clean_text(b.get("kind"), 20, "kind") or ("script" if command else "code" if repo and not report_only else "research")
        idem = clean_text(b.get("idempotency_key"), 200, "idempotency_key")
        pr_number = b.get("pr_number")
        if pr_number is not None and not str(pr_number).isdigit():
            raise ApiError(400, "pr_number must be a number")
        parent_id = clean_text(b.get("parent_id"), 80, "parent_id")
        if parent_id and not follow_up:
            self.get_task(who, parent_id)  # you may only point at a parent you can see
        tid = clean_text(b.get("id"), 80, "id") or new_id()
        if not ID_RE.match(tid):
            raise ApiError(400, "id may use letters, digits, dot, dash, underscore and colon")
        attachments = b.get("attachments") or []
        if not isinstance(attachments, list) or len(attachments) > 20:
            raise ApiError(400, "attachments must be a list of at most 20 files")
        decoded = []
        for a in attachments:
            if not isinstance(a, dict):
                raise ApiError(400, "each attachment is {name, content_base64}")
            name = os.path.basename(str(a.get("name", "")))
            if not SAFE_NAME.match(name):
                raise ApiError(400, f"bad attachment name: {name!r}")
            try:
                data = base64.b64decode(a.get("content_base64", ""), validate=True)
            except (ValueError, TypeError):
                raise ApiError(400, f"attachment {name} is not valid base64")
            if len(data) > MAX_FILE:
                raise ApiError(400, f"attachment {name} is larger than 25 MB")
            decoded.append((name, data))
        # Everything that changes the work is part of the duplicate check.
        canon = {"prompt": prompt, "repo": repo, "command": command, "provider": provider, "model": model,
                 "branch": branch, "pr_number": pr_number, "report_only": report_only, "push": push, "open_pr": open_pr,
                 "push_main": push_main, "ask_human": bool(b.get("ask_human")), "untrusted": untrusted, "kind": kind,
                 "project": project, "parent_id": parent_id, "follow_up": follow_up,
                 "attachments": sorted((n, hashlib.sha256(d).hexdigest()) for n, d in decoded)}
        phash = hashlib.sha256(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()
        scope = (owner or "").lower()
        t = now()
        with self.s.tx():
            if idem:
                prior = self.s.one("SELECT * FROM tasks WHERE coalesce(lower(owner), '') = ? AND idempotency_key = ?", scope, idem)
                if prior:
                    if prior["prompt_hash"] != phash:
                        raise ApiError(409, "idempotency_key was already used for a different task. Use a new key for new work.",
                                       {"task_id": prior["id"]})
                    return {"task": self.public_task(prior), "duplicate": True}
            elif not b.get("allow_duplicate"):
                prior = self.s.one("SELECT * FROM tasks WHERE coalesce(lower(owner), '') = ? AND prompt_hash = ?"
                                   " AND state IN ('queued','running','waiting') AND created_at > ? ORDER BY created_at DESC LIMIT 1",
                                   scope, phash, now() - 86400)
                if prior:
                    return {"task": self.public_task(prior), "duplicate": True}
            open_n = self.s.one("SELECT COUNT(*) AS n FROM tasks WHERE coalesce(lower(owner), '') = ? AND state NOT IN ('done','failed','cancelled')",
                                scope)["n"]
            if self.max_open and open_n >= self.max_open:
                raise ApiError(429, f"{owner or 'this owner'} already has {open_n} unfinished tasks (limit {self.max_open}). "
                                    "Finish or cancel some first.")
            try:
                self.s.run(
                    "INSERT INTO tasks(id, created_at, updated_at, project, owner, kind, title, prompt, prompt_hash, repo,"
                    " branch, pr_number, provider, model, command, report_only, push, open_pr, ask_human, untrusted,"
                    " idempotency_key, state, max_attempts, parent_id, follow_up, held_behind, push_main, workspace_host)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tid, t, t, project, owner, kind, clean_text(b.get("title"), 200, "title"), prompt, phash, repo,
                    branch, int(pr_number) if pr_number is not None else None,
                    provider, model, command, report_only, push, open_pr, 1 if b.get("ask_human") else 0, untrusted,
                    idem, "queued", min(int(b.get("max_attempts") or self.max_attempts), 50), parent_id,
                    1 if follow_up else 0, held_behind, push_main, b.get("_workspace_host"))
            except sqlite3.IntegrityError:
                raise ApiError(409, "a task with that id or idempotency_key already exists")
            for name, data in decoded:
                self.s.run("INSERT INTO files(task_id, kind, name, data, size, at) VALUES(?,?,?,?,?,?)",
                           tid, "attachment", name, data, len(data), t)
            self.event(tid, "brief", owner or who["role"], prompt[:4000])
            self.event(tid, "state", "backend", "queued", {"held_behind": held_behind} if held_behind else None)
        out = {"task": self.public_task(self.s.one("SELECT * FROM tasks WHERE id = ?", tid)), "duplicate": False}
        if held_behind:
            out["held_behind"] = held_behind
        return out

    def follow_up(self, who, parent_id, b):
        """--follow-up: continue earlier work. See docs/concepts.md for the rules."""
        parent = self.get_task(who, parent_id)
        st = parent["state"]
        if st in ("queued", "running"):
            raise ApiError(409, f"task {parent_id} is still {st}. Send it a message instead: swarmctl message {parent_id} \"...\"",
                           {"task_id": parent_id})
        if st == "waiting":
            for flag in ("push", "open_pr", "push_main"):
                if b.get(flag) and not parent.get(flag):
                    raise ApiError(400, f"task {parent_id} is waiting for an answer. A follow-up answers it and cannot add "
                                        f"{flag} when the task did not have it")
            r = self.answer(who, parent_id, {"answer": b.get("prompt")}, via="follow-up")
            r.update(answered_parent=True, duplicate=False)
            return r
        inherit = {k: parent[k] for k in ("project", "repo", "provider", "kind", "pr_number", "push", "open_pr", "push_main",
                                          "report_only", "untrusted", "ask_human", "owner")}
        inherit["model"] = parent.get("session_model") or parent.get("model")
        if parent.get("command"):
            inherit["command"] = parent["command"]
        if parent.get("repo") and not parent.get("report_only"):
            inherit["branch"] = parent.get("pushed_branch") or parent.get("branch") or f"agent/{parent['id']}"
        body = dict(inherit)
        body.update({k: v for k, v in b.items() if v not in (None, False, "") and k != "follow_up"})
        body["parent_id"] = parent["id"]
        body["_workspace_host"] = parent.get("workspace_host")
        held = self.s.one("SELECT id FROM tasks WHERE parent_id = ? AND follow_up = 1 AND state NOT IN ('done','failed','cancelled')"
                          " ORDER BY created_at DESC LIMIT 1", parent["id"])
        return self._create(who, body, follow_up=True, held_behind=held["id"] if held else None)

    def public_task(self, t, events=False):
        out = {k: v for k, v in t.items() if k not in ("prompt_hash", "session_id")}
        out["has_report"] = bool(self.s.one("SELECT 1 FROM files WHERE task_id = ? AND kind = 'report'", t["id"]))
        out["artifacts"] = [r["name"] for r in self.s.q("SELECT name FROM files WHERE task_id = ? AND kind = 'artifact' ORDER BY name", t["id"])]
        if events:
            out["events"] = self.s.q("SELECT at, type, actor, text, meta FROM events WHERE task_id = ? ORDER BY id", t["id"])
            for e in out["events"]:
                e["meta"] = json.loads(e["meta"]) if e["meta"] else None
        return out

    def list_tasks(self, who, qs):
        where, args = [], []
        for f in ("state", "project", "owner", "kind", "waiting_on", "parent_id"):
            if qs.get(f):
                where.append(f"lower({f}) = lower(?)")
                args.append(qs[f])
        if who["role"] == "bot":
            where.append("lower(owner) = lower(?)")
            args.append(who["bot"])
        if qs.get("updated_since"):
            try:
                args.append(int(float(qs["updated_since"])))
            except ValueError:
                raise ApiError(400, "updated_since must be unix seconds")
            where.append("updated_at > ?")
        try:
            limit = min(int(qs.get("limit") or 50), 500)
        except ValueError:
            raise ApiError(400, "limit must be a number")
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC LIMIT ?"
        return {"tasks": [self.public_task(t) for t in self.s.q(sql, *args, limit)]}

    def answer(self, who, task_id, b, via="answer"):
        self.need(who, "admin", "chief", "bot")
        text = clean_text(b.get("answer") or b.get("text"), 20000, "answer")
        if not text:
            raise ApiError(400, "answer is required")
        actor = who["bot"] or who["role"]
        with self.s.tx():
            t = self.get_task(who, task_id)
            if t["state"] != "waiting":
                raise ApiError(409, f"task is {t['state']}, not waiting")
            if t["waiting_on"] == "human" and who["role"] == "bot":
                raise ApiError(409, "this task waits on the human. A bot cannot answer for them. Ask them, then the Chief of Staff records the answer.")
            self.event(task_id, "answer", actor, text, {"via": via})
            self.s.run("UPDATE items SET status = 'answered', answer = ?, updated_at = ? WHERE task_id = ? AND status IN ('open','snoozed')",
                       text, now(), task_id)
            extra = {}
            if t["outcome"] == "work_unavailable":
                # The worker asked because unpushed work was lost. Any answer means: start again from what was pushed.
                extra = {"unpushed": 0, "start_fresh": 1}
            t = self.set_state(t, "queued", actor, pending_answer=text, waiting_on=None, question=None, outcome=None, not_before=0,
                               human_snooze_until=None, **extra)
        return {"task": self.public_task(t), "resumed": True}

    def message(self, who, task_id, b):
        self.need(who, "admin", "chief", "bot")
        text = clean_text(b.get("text"), 20000, "text")
        if not text:
            raise ApiError(400, "text is required")
        with self.s.tx():
            t = self.get_task(who, task_id)
            if t["state"] in TERMINAL:
                raise ApiError(409, f"task already ended. Queue a follow-up instead: swarmctl enqueue --follow-up {task_id} brief.md")
            if t["state"] == "waiting":
                if t["waiting_on"] == "human" and who["role"] == "bot":
                    raise ApiError(409, "task waits on the human; a message cannot answer for them")
                r = self.answer(who, task_id, {"answer": text}, via="message")
                r["treated_as_answer"] = True
                return r
            sender = clean_text(b.get("from"), 80, "from") if who["role"] in ("admin", "chief") else None
            sender = sender or who["bot"] or who["role"]
            cur = self.s.run("INSERT INTO messages(task_id, at, sender, text) VALUES(?,?,?,?)", task_id, now(), sender, text)
            self.event(task_id, "message", sender, text)
        return {"ok": True, "stored": True, "message_id": cur.lastrowid}

    def cancel(self, who, task_id, note=None):
        self.need(who, "admin", "chief", "bot")
        with self.s.tx():
            t = self.get_task(who, task_id)
            if t["state"] in TERMINAL:
                return {"task": self.public_task(t)}
            was_running = t["state"] == "running"
            self.s.run("UPDATE items SET status = 'done', updated_at = ? WHERE task_id = ? AND status IN ('open','snoozed')", now(), task_id)
            actor = who["bot"] or who["role"]
            if note:
                self.event(task_id, "dropped", actor, note)
            # The owner cancelling its own task needs no notice; anyone else cancelling it does.
            by_owner = bool(who["bot"]) and who["bot"].lower() == (t["owner"] or "").lower()
            t = self.set_state(t, "cancelled", actor, notify=not by_owner, outcome="cancelled",
                               cancel_requested=1 if was_running else 0)
        out = {"task": self.public_task(t)}
        if was_running:
            out["note"] = "cancelled. The worker stops the run within about 30 seconds and publishes nothing."
        return out

    # ---------- worker side ----------
    def seen(self, worker, draining=None):
        if draining is None:
            self.s.run("INSERT INTO workers(name, last_seen) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen",
                       worker, now())
        else:
            self.s.run("INSERT INTO workers(name, last_seen, draining) VALUES(?, ?, ?) ON CONFLICT(name) DO UPDATE SET"
                       " last_seen = excluded.last_seen, draining = excluded.draining", worker, now(), 1 if draining else 0)

    def claim(self, who, b):
        self.need(who, "worker")
        worker = clean_text(b.get("worker"), 80, "worker") or "worker"
        draining = bool(b.get("draining"))
        with self.s.tx():
            self.seen(worker, draining)
            if draining:
                return None
            pick = None
            for t in self.s.q("SELECT * FROM tasks WHERE state = 'queued' AND not_before <= ? ORDER BY created_at LIMIT 100", now()):
                if t["held_behind"]:
                    h = self.s.one("SELECT state FROM tasks WHERE id = ?", t["held_behind"])
                    if h and h["state"] not in TERMINAL:
                        continue
                host = t["workspace_host"]
                if host and host != worker:
                    w = self.s.one("SELECT last_seen FROM workers WHERE name = ?", host)
                    if w and (w["last_seen"] or 0) > now() - 3600:
                        continue  # that worker has the folder and session; leave it for that worker
                pick = t
                break
            if not pick:
                return None
            t = pick
            attempts = t["attempts"] + 1
            claim_id = "c" + secrets.token_hex(8)
            tok = "swt_" + secrets.token_urlsafe(32)
            self.s.run("UPDATE tasks SET state = 'running', attempts = ?, lease_worker = ?, lease_until = ?, updated_at = ?,"
                       " claim_id = ?, claimed_at = ?, cancel_requested = 0 WHERE id = ?",
                       attempts, worker, now() + LEASE_SECONDS, now(), claim_id, now(), t["id"])
            self.s.run("INSERT INTO claims(claim_id, task_id, token_hash, worker, created_at, expires_at) VALUES(?,?,?,?,?,?)",
                       claim_id, t["id"], token_hash(tok), worker, now(), now() + self.token_max_age)
            self.event(t["id"], "state", worker, "running", {"attempt": attempts, "claim_id": claim_id})
            t = self.s.one("SELECT * FROM tasks WHERE id = ?", t["id"])
        out = {k: v for k, v in t.items() if k != "prompt_hash"}
        out["task_token"] = tok
        out["attachments"] = [r["name"] for r in self.s.q("SELECT name FROM files WHERE task_id = ? AND kind = 'attachment'", t["id"])]
        out["answers"] = self.s.q("SELECT at, actor, text FROM events WHERE task_id = ? AND type = 'answer' ORDER BY id", t["id"])
        out["has_bundle"] = bool(self.s.one("SELECT 1 FROM files WHERE task_id = ? AND kind = 'bundle'", t["id"]))
        if t["parent_id"] and t["follow_up"]:
            p = self.s.one("SELECT * FROM tasks WHERE id = ?", t["parent_id"]) or {}
            rep = self.s.one("SELECT data FROM files WHERE task_id = ? AND kind = 'report' ORDER BY at DESC LIMIT 1", t["parent_id"])
            out["parent"] = {k: p.get(k) for k in ("id", "state", "workdir", "workspace_host", "session_provider", "session_model",
                                                   "session_id", "pushed_branch", "branch", "unpushed", "repo")}
            out["parent"]["has_bundle"] = bool(self.s.one("SELECT 1 FROM files WHERE task_id = ? AND kind = 'bundle'", t["parent_id"]))
            out["parent"]["report"] = bytes(rep["data"]).decode(errors="replace")[:300_000] if rep else None
        return out

    def task_states(self, who, qs):
        self.need(who, "worker")
        ids = [i for i in (qs.get("ids") or "").split(",") if i][:500]
        rows = self.s.q(f"SELECT id, state, finished_at, updated_at FROM tasks WHERE id IN ({','.join('?' * len(ids))})", *ids) if ids else []
        return {"states": {r["id"]: {k: r[k] for k in ("state", "finished_at", "updated_at")} for r in rows}}

    def heartbeat(self, who, task_id, b):
        with self.s.tx():
            t = self.own_task(who, task_id, b.get("claim_id"))
            self.s.run("UPDATE tasks SET lease_until = ?, updated_at = ? WHERE id = ?", now() + LEASE_SECONDS, now(), task_id)
            ack = b.get("ack") or []
            if not isinstance(ack, list) or not all(isinstance(i, int) for i in ack):
                raise ApiError(400, "ack must be a list of message ids")
            if ack:
                self.s.run(f"UPDATE messages SET delivered = 1 WHERE task_id = ? AND id IN ({','.join('?' * len(ack))})", task_id, *ack)
            msgs = self.s.q("SELECT id, at, sender, text FROM messages WHERE task_id = ? AND delivered = 0 ORDER BY id", task_id)
            if b.get("note"):
                self.event(task_id, "progress", t["lease_worker"] or "worker", clean_text(b["note"], 2000, "note"))
            if t["lease_worker"]:
                self.seen(t["lease_worker"])
        return {"cancel": False, "messages": msgs, "claim_id": who["claim"]}

    def publish_check(self, who, task_id, b):
        """The runner asks right before it pushes: is this claim live and is publishing still allowed?"""
        with self.s.tx():
            t = self.own_task(who, task_id, b.get("claim_id"))
            self.s.run("UPDATE tasks SET lease_until = ? WHERE id = ?", now() + LEASE_SECONDS, task_id)
        proj = self.project(t["project"])
        allowed, reason = True, None
        if not t["repo"] or not t["push"] or t["report_only"] or t["untrusted"]:
            allowed, reason = False, "this task does not publish"
        elif proj and not proj.get("allow_push"):
            allowed, reason = False, f"project {t['project']} no longer allows pushing"
        elif t["push_main"] and not (proj and proj.get("allow_push_main")):
            allowed, reason = False, "push-main is no longer allowed for this project"
        return {"allowed": allowed, "reason": reason, "repo": t["repo"], "branch": t["branch"], "push": bool(t["push"]),
                "open_pr": bool(t["open_pr"]), "push_main": bool(t["push_main"]), "pr_number": t["pr_number"]}

    def finish(self, who, task_id, b):
        """status: done | waiting | next | failed | limit. Applies retry rules and fires notices.
        Idempotent per claim: a retry from the same claim gets the first answer back."""
        if who["role"] != "task" or task_id != who["task"]:
            raise ApiError(403, "only the task's own token can finish it")
        if b.get("claim_id") is not None and b.get("claim_id") != who["claim"]:
            raise ApiError(409, "claim_id does not match this token", {"claim_ended": True})
        status = b.get("status")
        if status not in ("done", "waiting", "next", "failed", "limit"):
            raise ApiError(400, "status must be done, waiting, next, failed or limit")
        actor = clean_text(b.get("worker"), 80, "worker") or "worker"
        with self.s.tx():
            c = self.s.one("SELECT * FROM claims WHERE claim_id = ?", who["claim"])
            if c and c["finish_response"]:
                return dict(json.loads(c["finish_response"]), replayed=True)
            t = self.own_task(who, task_id)
            common = dict(summary=clean_text(b.get("summary"), 2000, "summary") or t["summary"],
                          pr_url=clean_text(b.get("pr_url"), 500, "pr_url") or t["pr_url"],
                          session_provider=clean_text(b.get("session_provider"), 20, "session_provider") or t["session_provider"],
                          session_model=clean_text(b.get("session_model"), 80, "session_model") or t["session_model"],
                          session_id=clean_text(b.get("session_id"), 200, "session_id") or t["session_id"],
                          workdir=clean_text(b.get("workdir"), 120, "workdir") or t["workdir"],
                          pushed_branch=clean_text(b.get("pushed_branch"), 200, "pushed_branch") or t["pushed_branch"],
                          unpushed=1 if b.get("unpushed") else 0,
                          workspace_host=c["worker"] if (b.get("workdir") or t["workdir"]) else t["workspace_host"],
                          pending_answer=None)
            if "session_id" in b and b["session_id"] is None and b.get("session_reset"):
                common["session_id"] = None
            for w in (b.get("warnings") or [])[:20]:
                self.event(task_id, "warning", actor, clean_text(str(w), 2000, "warning"))
            if status == "done":
                t = self.set_state(t, "done", actor, outcome="done", error=None, **common)
            elif status == "waiting":
                q = clean_text(b.get("question"), 8000, "question") or "The worker needs an answer (no question text was given)."
                on = "human" if t["ask_human"] or b.get("waiting_on") == "human" else "owner"
                t = self.set_state(t, "waiting", actor, outcome="waiting", question=q, waiting_on=on, **common)
            elif status in ("next", "limit") and t["attempts"] < t["max_attempts"]:
                delay = 0 if status == "next" else min(3600 * 2 ** max(t["attempts"] - 1, 0), 86400)
                step = clean_text(b.get("next_step"), 4000, "next_step")
                t = self.set_state(t, "queued", actor, outcome=status, next_step=step, not_before=now() + delay, **common)
            elif status in ("next", "limit"):
                q = (f"This task stopped after {t['attempts']} attempts (last status: {status}). "
                     "Answer with how to continue, or cancel it.")
                t = self.set_state(t, "waiting", actor, outcome="attempt_cap", question=q, waiting_on="owner", **common)
            else:
                t = self.set_state(t, "failed", actor, outcome=clean_text(b.get("outcome"), 40, "outcome") or "failed",
                                   error=clean_text(b.get("error"), 4000, "error"), **common)
            resp = {"task": self.public_task(t)}
            self.s.run("UPDATE claims SET finish_response = ? WHERE claim_id = ?", json.dumps(resp, default=str), who["claim"])
        return resp

    def put_file(self, who, task_id, kind, name, data, claim_id):
        with self.s.tx():
            self.own_task(who, task_id, claim_id)
            if kind not in ("report", "artifact", "bundle"):
                raise ApiError(400, "kind must be report, artifact or bundle")
            name = os.path.basename(name)
            if not SAFE_NAME.match(name):
                raise ApiError(400, "bad file name")
            if len(data) > (MAX_BUNDLE if kind == "bundle" else MAX_FILE):
                raise ApiError(413, "file is too large")
            self.s.run("INSERT OR REPLACE INTO files(task_id, kind, name, data, size, at) VALUES(?,?,?,?,?,?)",
                       task_id, kind, name, data, len(data), now())
            self.event(task_id, kind, "worker", name, {"size": len(data)})
        return {"ok": True}

    def parent_bundle(self, who, task_id):
        t = self.own_task(who, task_id)
        if not t["parent_id"] or not t["follow_up"]:
            raise ApiError(404, "this task is not a follow-up")
        f = self.s.one("SELECT data FROM files WHERE task_id = ? AND kind = 'bundle' ORDER BY at DESC LIMIT 1", t["parent_id"])
        if not f:
            raise ApiError(404, "the parent task saved no work bundle")
        return ("application/octet-stream", bytes(f["data"]))

    def own_bundle(self, who, task_id):
        self.own_task(who, task_id)
        f = self.s.one("SELECT data FROM files WHERE task_id = ? AND kind = 'bundle' ORDER BY at DESC LIMIT 1", task_id)
        if not f:
            raise ApiError(404, "no saved work bundle")
        return ("application/octet-stream", bytes(f["data"]))

    def reap(self):
        """Requeue running tasks whose worker stopped heartbeating; release snoozed items."""
        with self.s.tx():
            for t in self.s.q("SELECT * FROM tasks WHERE state = 'running' AND lease_until < ?", now()):
                if t["attempts"] < t["max_attempts"]:
                    self.set_state(t, "queued", "reaper", outcome="lost", not_before=now() + 60)
                else:
                    self.set_state(t, "failed", "reaper", outcome="lost", error="worker stopped responding too many times")
            self.s.run("UPDATE claims SET ended_at = ?, end_reason = 'expired' WHERE ended_at IS NULL AND expires_at <= ?", now(), now())
            self.s.run("UPDATE items SET status = 'open', snooze_until = NULL, updated_at = ? WHERE status = 'snoozed' AND snooze_until <= ?",
                       now(), now())

    # ---------- reminders, escalation, watchdog, retention ----------
    def escalate(self):
        remind = env_int("SWARM_REMIND_HOURS", 12) * 3600
        esc = env_int("SWARM_ESCALATE_HOURS", 24) * 3600
        with self.s.tx():
            for t in self.s.q("SELECT * FROM tasks WHERE state = 'waiting' AND waiting_on = 'owner'"):
                since = t["waiting_since"] or t["updated_at"] or now()
                age = now() - since
                if age >= remind and not t["reminded_at"]:
                    eid = f"{t['id']}:reminder:{since}"
                    p = dict(self.payload(t, eid, "owner"), event="reminder",
                             text=f"Task {t['id']} has waited {age // 3600} hours for your answer.")
                    owner = self.bot(t["owner"])
                    if owner and owner.get("notify_url"):
                        self.queue_notice(eid, owner, "owner", p)
                    else:
                        self.queue_notice(eid, self.chief(), "owner_not_registered", p)
                    self.s.run("UPDATE tasks SET reminded_at = ? WHERE id = ?", now(), t["id"])
                if age >= esc and not t["escalated_at"]:
                    bot = t["owner"] or "unowned"
                    key = f"stale-{t['id']}"
                    if not self.s.one("SELECT 1 FROM items WHERE bot = ? AND dedupe_key = ?", bot, key):
                        iid = "i" + secrets.token_hex(6)
                        self.s.run("INSERT INTO items(id, project, bot, kind, ask, recommendation, options, links, dedupe_key, task_id,"
                                   " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", iid, t["project"] or "-", bot, "decision",
                                   f"Task {t['id']} has waited {age // 3600} hours on {bot} for an answer: {(t['question'] or '')[:3000]}",
                                   "Answer it, or drop it if it is no longer needed.", "[]", "[]", key, t["id"], now(), now())
                    eid = f"{t['id']}:escalated:{since}"
                    p = dict(self.payload(t, eid, "alert"), event="escalated",
                             text=f"Task {t['id']} waited {age // 3600} hours on {bot}. It is now on the Waiting on you list.")
                    self.queue_notice(eid, self.chief(), "alert", p)
                    self.s.run("UPDATE tasks SET escalated_at = ? WHERE id = ?", now(), t["id"])

    def watchdog(self):
        with self.s.tx():
            due = self.s.one("SELECT COUNT(*) AS n FROM tasks WHERE state = 'queued' AND not_before <= ?", now() - 60)["n"]
            last = max((self.s.one("SELECT MAX(last_seen) AS m FROM workers")["m"] or 0), self.started_at)
            flag = self.s.one("SELECT value FROM meta WHERE key = 'worker_silent_since'")
            if due and last < now() - WORKER_SILENT_SECONDS:
                if not flag:
                    self.s.run("INSERT OR REPLACE INTO meta(key, value) VALUES('worker_silent_since', ?)", str(last))
                    self.alert(f"worker-silent:{last}", f"No worker has checked in for {(now() - last) // 60} minutes and "
                                                        f"{due} task(s) are waiting to run. Check: systemctl status swarm-worker",
                               "watchdog", event="watchdog")
            elif flag and last >= now() - WORKER_SILENT_SECONDS:
                self.s.run("DELETE FROM meta WHERE key = 'worker_silent_since'")
            limit = env_int("SWARM_TASK_TIMEOUT", 14400) + 1800
            for t in self.s.q("SELECT id, claim_id, claimed_at FROM tasks WHERE state = 'running' AND claimed_at < ?", now() - limit):
                self.alert(f"overdue:{t['id']}:{t['claim_id']}", f"Task {t['id']} has been running for "
                                                               f"{(now() - t['claimed_at']) // 60} minutes, past its time limit.",
                           "watchdog", event="watchdog")

    def retention(self):
        last = self.s.one("SELECT value FROM meta WHERE key = 'retention_at'")
        if last and int(last["value"]) > now() - 86400:
            return
        days = env_int("SWARM_FILE_RETENTION_DAYS", 90)
        with self.s.tx():
            if days > 0:
                self.s.run("DELETE FROM files WHERE kind IN ('artifact','bundle','attachment') AND task_id IN"
                           " (SELECT id FROM tasks WHERE state IN ('done','failed','cancelled') AND finished_at < ?)", now() - days * 86400)
            self.s.run("DELETE FROM notices WHERE state = 'sent' AND sent_at < ?", now() - 30 * 86400)
            self.s.run("DELETE FROM claims WHERE ended_at IS NOT NULL AND ended_at < ?", now() - 30 * 86400)
            self.s.run("INSERT OR REPLACE INTO meta(key, value) VALUES('retention_at', ?)", str(now()))

    # ---------- bots and projects ----------
    def put_bot(self, who, bot_id, b):
        if who["role"] == "bot" and bot_id.lower() != who["bot"].lower():
            raise ApiError(403, "a bot can only change its own settings")
        if not ID_RE.match(bot_id) or ".." in bot_id:
            raise ApiError(400, "bad bot id")
        role = b.get("role")
        if role is not None and who["role"] not in ("admin", "chief"):
            raise ApiError(403, "only admin can set a bot's role")
        if role not in (None, "chief", "project"):
            raise ApiError(400, "role must be chief or project")
        url = b.get("notify_url")
        if url is not None and url != "":
            why = valid_notify_url(str(url))
            if why:
                raise ApiError(400, why)
        auth = b.get("notify_auth")
        if auth is not None and (not isinstance(auth, str) or len(auth) > 4000):
            raise ApiError(400, "notify_auth must be text")
        t = now()
        with self.s.tx():
            prior = self.s.one("SELECT * FROM bots WHERE id = ?", bot_id)
            if not prior:
                self.s.run("INSERT INTO bots(id, role, created_at, updated_at) VALUES(?,?,?,?)", bot_id, role or "project", t, t)
                prior = self.s.one("SELECT * FROM bots WHERE id = ?", bot_id)
            if role:
                self.s.run("UPDATE bots SET role = ? WHERE id = ?", role, bot_id)
            changed = False
            if url is not None and (url or None) != prior["notify_url"]:
                self.s.run("UPDATE bots SET notify_url = ? WHERE id = ?", url or None, bot_id)
                changed = True
            if auth is not None:
                auth = auth.strip()
                if auth:
                    self.keys.write(bot_id, auth)
                else:
                    self.keys.delete(bot_id)
                self.s.run("UPDATE bots SET notify_auth = NULL, notify_auth_fp = ? WHERE id = ?", fingerprint(auth) if auth else None, bot_id)
                changed = True
            if changed:
                # New version: pending deliveries move to the new address and key, never the old address with the new key.
                self.s.run("UPDATE bots SET notify_version = coalesce(notify_version, 0) + 1 WHERE id = ?", bot_id)
                cur = self.s.one("SELECT notify_url, notify_version FROM bots WHERE id = ?", bot_id)
                self.s.run("UPDATE notices SET url = ?, notify_version = ? WHERE target = ? AND state = 'pending'",
                           cur["notify_url"], cur["notify_version"], bot_id)
            self.s.run("UPDATE bots SET updated_at = ? WHERE id = ?", t, bot_id)
        return {"bot": self.public_bot(self.s.one("SELECT * FROM bots WHERE id = ?", bot_id), who)}

    def public_bot(self, b, who=None):
        full = who is not None and who["role"] == "admin"
        return {"id": b["id"], "role": b["role"], "notify_url": b["notify_url"] if full else redact_url(b["notify_url"]),
                "has_notify_auth": bool(b.get("notify_auth_fp") or b.get("notify_auth")), "has_token": bool(b["token_hash"]),
                "notify_version": b.get("notify_version") or 0,
                "projects": [p["name"] for p in self.bound_projects(b["id"])]}

    def bot_token(self, who, bot_id):
        if not self.s.one("SELECT 1 FROM bots WHERE id = ?", bot_id):
            raise ApiError(404, "no such bot; PUT /bots/<id> first")
        tok = "swb_" + secrets.token_urlsafe(32)
        self.s.run("UPDATE bots SET token_hash = ?, updated_at = ? WHERE id = ?", token_hash(tok), now(), bot_id)
        return {"bot": bot_id, "token": tok, "note": "Shown once. Store it in a file with chmod 600. Never paste it in chat."}

    def put_project(self, who, name, b):
        if not ID_RE.match(name):
            raise ApiError(400, "bad project name")
        with self.s.tx():
            cur = self.project(name) or {"allow_push": 1, "allow_push_main": 0, "bots": [], "repos": []}
            vals = {}
            for k in ("default_repo", "default_provider", "default_model"):
                v = clean_text(b[k], 200, k) if k in b else cur.get(k)
                vals[k] = v or None
            if vals["default_repo"]:
                vals["default_repo"] = norm_repo(vals["default_repo"])
            if vals["default_provider"] not in (None, "claude", "codex"):
                raise ApiError(400, "default_provider must be claude or codex")
            bots = text_list(b.get("bots"), "bots", ID_RE)
            bots = list(cur["bots"]) if bots is None else bots
            for x in text_list(b.get("add_bots"), "add_bots", ID_RE) or []:
                if x.lower() not in (y.lower() for y in bots):
                    bots.append(x)
            rm = [x.lower() for x in text_list(b.get("remove_bots"), "remove_bots", ID_RE) or []]
            bots = [x for x in bots if x.lower() not in rm]
            repos = text_list(b.get("repos"), "repos")
            repos = list(cur["repos"]) if repos is None else [norm_repo(r) for r in repos]
            for r in text_list(b.get("add_repos"), "add_repos") or []:
                r = norm_repo(r)
                if r not in repos:
                    repos.append(r)
            allow = 1 if b.get("allow_push", cur.get("allow_push")) else 0
            allow_main = 1 if b.get("allow_push_main", cur.get("allow_push_main")) else 0
            self.s.run("INSERT OR REPLACE INTO projects(name, default_repo, default_provider, default_model, allow_push, updated_at,"
                       " bots, repos, allow_push_main) VALUES(?,?,?,?,?,?,?,?,?)", name, vals["default_repo"], vals["default_provider"],
                       vals["default_model"], allow, now(), json.dumps(bots), json.dumps(repos), allow_main)
        return {"project": self.project(name)}

    def list_projects(self, who):
        if who["role"] == "bot":
            return {"projects": self.bound_projects(who["bot"])}
        return {"projects": [self.shape_project(p) for p in self.s.q("SELECT * FROM projects ORDER BY name")]}

    # ---------- Waiting on you ----------
    def shape_item(self, r):
        r = dict(r)
        r["options"] = json.loads(r["options"] or "[]")
        r["links"] = json.loads(r["links"] or "[]")
        r["source"] = "filed"
        return r

    def check_item_task(self, who, bot, task_id):
        """An item may link only to a task owned by the bot that files it."""
        if not task_id:
            return
        t = self.get_task(who, task_id) if who["role"] == "bot" else self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
        if not t:
            raise ApiError(404, "no such task")
        if t["owner"] and t["owner"].lower() != (bot or "").lower():
            raise ApiError(403, f"task {task_id} belongs to another bot")

    def file_item(self, who, b):
        bot = clean_text(b.get("bot"), 80, "bot") or who["bot"]
        if who["role"] == "bot" and (bot or "").lower() != who["bot"].lower():
            raise ApiError(403, "a bot can only file items as itself")
        fields = {k: clean_text(b.get(k), lim, k) for k, lim in
                  (("project", 80), ("kind", 40), ("ask", 4000), ("recommendation", 2000), ("dedupe_key", 200), ("task_id", 80))}
        fields["bot"] = bot
        for k in ("project", "bot", "kind", "ask", "dedupe_key"):
            if not fields.get(k):
                raise ApiError(400, f"{k} is required")
        if fields["kind"] not in ("decision", "deliverable-review"):
            raise ApiError(400, "kind must be decision or deliverable-review")
        self.check_item_task(who, bot, fields["task_id"])
        lists = {}
        for k in ("links", "options"):
            v = b.get(k) or []
            if not isinstance(v, list) or len(v) > 20:
                raise ApiError(400, f"{k} must be a list (max 20)")
            lists[k] = json.dumps([str(x)[:2000] for x in v])
        t = now()
        with self.s.tx():
            dropped = self.s.one("SELECT id FROM items WHERE bot = ? AND dedupe_key = ? AND status = 'dropped'", bot, fields["dedupe_key"])
            if dropped and not b.get("refile_dropped"):
                raise ApiError(409, "the human dropped this ask. Do not file it again unless they ask for it.", {"item_id": dropped["id"]})
            cur = self.s.one("SELECT * FROM items WHERE bot = ? AND dedupe_key = ? AND status IN ('open','snoozed')", bot, fields["dedupe_key"])
            if cur:
                self.s.run("UPDATE items SET project=?, kind=?, ask=?, recommendation=?, options=?, links=?, task_id=COALESCE(?, task_id),"
                           " revision = revision + 1, updated_at = ? WHERE id = ?", fields["project"], fields["kind"], fields["ask"],
                           fields["recommendation"], lists["options"], lists["links"], fields["task_id"], t, cur["id"])
                iid, updated = cur["id"], True
            else:
                iid, updated = "i" + secrets.token_hex(6), False
                self.s.run("INSERT INTO items(id, project, bot, kind, ask, recommendation, options, links, dedupe_key, task_id, created_at, updated_at)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", iid, fields["project"], bot, fields["kind"], fields["ask"],
                           fields["recommendation"], lists["options"], lists["links"], fields["dedupe_key"], fields["task_id"], t, t)
        return {"item": self.shape_item(self.s.one("SELECT * FROM items WHERE id = ?", iid)), "updated": updated}

    def list_items(self, who, qs):
        status = qs.get("status") or "due"
        if status not in ("due", "open", "snoozed", "answered", "dropped", "done", "withdrawn", "all"):
            raise ApiError(400, "bad status")
        if status in ("due", "open"):
            rows = self.s.q("SELECT * FROM items WHERE status = 'open' ORDER BY created_at")
        elif status == "all":
            rows = self.s.q("SELECT * FROM items ORDER BY created_at")
        else:
            rows = self.s.q("SELECT * FROM items WHERE status = ? ORDER BY created_at", status)
        items = [self.shape_item(r) for r in rows]
        if status in ("due", "open", "snoozed", "all"):
            # Every open or snoozed filed item covers its task, whatever the filter shows.
            covered = {r["task_id"] for r in self.s.q("SELECT task_id FROM items WHERE task_id IS NOT NULL AND status IN ('open','snoozed')")}
            for t in self.s.q("SELECT * FROM tasks WHERE state = 'waiting' AND waiting_on = 'human' ORDER BY created_at"):
                if t["id"] in covered:
                    continue
                snoozed = (t["human_snooze_until"] or 0) > now()
                if (status in ("due", "open") and snoozed) or (status == "snoozed" and not snoozed):
                    continue
                items.append({"id": t["id"], "source": "task", "task_id": t["id"], "project": t["project"], "bot": t["owner"],
                              "kind": "decision", "ask": t["question"], "recommendation": None, "options": [], "links":
                              [x for x in (t["pr_url"],) if x], "status": "snoozed" if snoozed else "open",
                              "snooze_until": t["human_snooze_until"] if snoozed else None, "created_at": t["updated_at"]})
        for f in ("project", "bot"):
            if qs.get(f):
                items = [i for i in items if (i.get(f) or "").lower() == qs[f].lower()]
        if who["role"] == "bot":
            items = [i for i in items if (i.get("bot") or "").lower() == who["bot"].lower()]
        return {"items": items}

    def update_item(self, who, item_id, b):
        action = b.get("action")
        if who["role"] == "bot" and action not in ("withdraw", "done", "update"):
            raise ApiError(403, "only the human (through admin or the Chief of Staff) can answer, snooze or drop")
        item = self.s.one("SELECT * FROM items WHERE id = ?", item_id)
        if not item:
            task = self.s.one("SELECT * FROM tasks WHERE id = ? AND state = 'waiting' AND waiting_on = 'human'", item_id)
            if task and who["role"] in ("admin", "chief"):
                if action == "answer":
                    return self.answer(who, item_id, {"answer": b.get("answer")}, via="waiting-list")
                if action == "drop":
                    return self.cancel(who, item_id, note=b.get("note") or "dropped from the Waiting on you list")
                if action == "snooze":
                    until = int(b.get("until") or (now() + 86400))
                    self.s.run("UPDATE tasks SET human_snooze_until = ? WHERE id = ?", until, item_id)
                    return {"item": {"id": item_id, "source": "task", "status": "snoozed", "snooze_until": until}}
            raise ApiError(404, "no such item")
        if who["role"] == "bot" and item["bot"].lower() != who["bot"].lower():
            raise ApiError(404, "no such item")
        t = now()
        linked = self.s.one("SELECT * FROM tasks WHERE id = ?", item["task_id"]) if item["task_id"] else None
        if linked and linked["owner"] and linked["owner"].lower() != (item["bot"] or "").lower():
            linked = None  # never act on a task the filing bot does not own
        if action == "answer":
            ans = clean_text(b.get("answer"), 20000, "answer")
            if not ans:
                raise ApiError(400, "answer is required")
            self.s.run("UPDATE items SET status='answered', answer=?, updated_at=? WHERE id=?", ans, t, item_id)
            if linked and linked["state"] == "waiting":
                self.answer(who, linked["id"], {"answer": ans}, via="waiting-list")
        elif action == "snooze":
            until = int(b.get("until") or (t + 86400))
            self.s.run("UPDATE items SET status='snoozed', snooze_until=?, updated_at=? WHERE id=?", until, t, item_id)
        elif action == "drop":
            self.s.run("UPDATE items SET status='dropped', answer=?, updated_at=? WHERE id=?", clean_text(b.get("note"), 2000, "note")
                       or "Don't need it", t, item_id)
            if linked and linked["state"] not in TERMINAL:
                self.cancel(who, linked["id"])
        elif action in ("done", "withdraw"):
            self.s.run("UPDATE items SET status=?, answer=COALESCE(?, answer), updated_at=? WHERE id=?",
                       "done" if action == "done" else "withdrawn", clean_text(b.get("note"), 2000, "note"), t, item_id)
        elif action == "update":
            return self.file_item(who, dict(item, **{k: v for k, v in b.items() if k != "action"},
                                            options=json.loads(item["options"] or "[]") if "options" not in b else b["options"],
                                            links=json.loads(item["links"] or "[]") if "links" not in b else b["links"]))
        else:
            raise ApiError(400, "action must be answer, snooze, drop, done, withdraw or update")
        item = self.s.one("SELECT * FROM items WHERE id = ?", item_id)
        if action in ("answer", "drop"):
            filer = self.bot(item["bot"])
            eid = f"item:{item_id}:{action}:{item['revision']}"
            payload = {"event": "item_" + action, "event_id": eid, "idempotency_key": eid, "item_id": item_id,
                       "project": item["project"], "ask": item["ask"], "answer": item["answer"], "task_id": item["task_id"],
                       "dropped": action == "drop", "note": "dropped: stop work on this" if action == "drop" else None}
            if filer and filer.get("notify_url"):
                self.queue_notice(eid, filer, "owner", payload)
            else:
                self.queue_notice(eid, self.chief(), "owner_not_registered", payload)
        return {"item": self.shape_item(item)}

    # ---------- status ----------
    def status(self, who):
        mine = who["role"] == "bot"
        where, args = ("WHERE lower(owner) = lower(?)", [who["bot"]]) if mine else ("", [])
        counts = {r["state"]: r["n"] for r in self.s.q(f"SELECT state, COUNT(*) AS n FROM tasks {where} GROUP BY state", *args)}
        waiting = self.s.q(f"SELECT id, owner, project, waiting_on, question FROM tasks WHERE state = 'waiting'"
                           f"{' AND lower(owner) = lower(?)' if mine else ''} ORDER BY updated_at", *args)
        running = self.s.q(f"SELECT id, owner, project, lease_worker, lease_until FROM tasks WHERE state = 'running'"
                           f"{' AND lower(owner) = lower(?)' if mine else ''}", *args)
        stuck = [r["id"] for r in running if (r["lease_until"] or 0) < now()]
        out = {"version": VERSION, "tasks": counts, "running": running, "waiting": waiting,
               "items_due": len(self.list_items(who, {"status": "due"})["items"]), "stuck_running": stuck, "warnings": []}
        if not mine:
            out["notices"] = {r["state"]: r["n"] for r in self.s.q("SELECT state, COUNT(*) AS n FROM notices GROUP BY state")}
            out["workers"] = self.s.q("SELECT name, last_seen, draining FROM workers ORDER BY name")
            if not self.chief():
                out["warnings"].append("No Chief of Staff bot with a notify_url: fallback and alert notices are dropped.")
            failed = out["notices"].get("failed", 0)
            if failed:
                out["warnings"].append(f"{failed} deliveries failed for good. See: swarmctl notices")
            bdir = os.environ.get("SWARM_DB_BACKUP_DIR")
            if bdir and os.path.isdir(bdir):
                newest = max([os.path.getmtime(os.path.join(bdir, f)) for f in os.listdir(bdir) if f.endswith(".db.gz")] or [0])
                if newest < now() - 36 * 3600:
                    out["warnings"].append("The newest database backup is older than 36 hours. Check: systemctl status swarm-backup")
        else:
            out["notices"] = {}
        return out


class FailLimiter:
    """Counts failed logins per client. Too many in the window gives HTTP 429 before any token check."""

    def __init__(self, limit, window=300):
        self.limit, self.window = limit, window
        self.hits = {}
        self.lock = threading.Lock()

    def _prune(self, key):
        cut = time.time() - self.window
        self.hits[key] = [x for x in self.hits.get(key, []) if x > cut]
        if not self.hits[key]:
            del self.hits[key]

    def blocked(self, key):
        with self.lock:
            self._prune(key)
            return self.limit > 0 and len(self.hits.get(key, [])) >= self.limit

    def fail(self, key):
        with self.lock:
            self.hits.setdefault(key, []).append(time.time())
            if len(self.hits) > 10000:
                self.hits.clear()


# ---------- notice delivery ----------
def deliver_once(backend, poster=None):
    poster = poster or post_json
    s = backend.s
    rows = s.q("SELECT * FROM notices WHERE state = 'pending' AND next_at <= ? ORDER BY id LIMIT 20", now())
    for n in rows:
        bot = s.one("SELECT * FROM bots WHERE id = ?", n["target"]) or {}
        if bot and (n["notify_version"] or 0) != (bot.get("notify_version") or 0):
            # The bot's address or key changed after this was queued. Move it to the current version; never mix them.
            s.run("UPDATE notices SET url = ?, notify_version = ? WHERE id = ?", bot.get("notify_url"), bot.get("notify_version") or 0, n["id"])
            n = s.one("SELECT * FROM notices WHERE id = ?", n["id"])
        headers = {"Content-Type": "application/json", "User-Agent": "agent-swarm-kit/" + VERSION,
                   "Idempotency-Key": n["event_id"]}
        ok, err, permanent = False, None, False
        key = backend.keys.read(bot["id"]) if bot.get("notify_auth_fp") else None
        if bot.get("notify_auth_fp") and (not key or fingerprint(key) != bot["notify_auth_fp"]):
            err = "trigger key file is missing or does not match this bot's current key"
        elif not n["url"]:
            err, permanent = "bot has no notify_url", True
        else:
            if key:
                headers["Authorization"] = key if " " in key else f"Bearer {key}"
            try:
                status, body = poster(n["url"], n["payload"].encode(), headers)
                if 200 <= status < 300:
                    ok = True
                    try:
                        if json.loads(body or b"{}").get("duplicate") is True:
                            err = "receiver says duplicate (already received)"
                    except (ValueError, AttributeError):
                        pass
                elif status == 409:
                    ok, err = True, "HTTP 409: receiver already has this event"
                elif 300 <= status < 400:
                    err, permanent = f"HTTP {status}: redirects are not followed", True
                else:
                    err = f"HTTP {status}"
                    permanent = 400 <= status < 500 and status not in (408, 429)
            except Exception as e:  # network errors are retried
                err = str(e)[:300]
        attempts = n["attempts"] + 1
        if ok:
            s.run("UPDATE notices SET state='sent', attempts=?, sent_at=?, last_error=? WHERE id=?", attempts, now(), err, n["id"])
            continue
        if permanent or attempts >= 6:
            s.run("UPDATE notices SET state='failed', attempts=?, last_error=? WHERE id=?", attempts, err, n["id"])
            task_id = json.loads(n["payload"]).get("task_id")
            if task_id:
                backend.event(task_id, "notify_failed", "notify", f"delivery to {n['target']} failed: {err}")
            chief = backend.chief()
            if chief and chief["id"] != n["target"]:
                payload = dict(json.loads(n["payload"]), delivery_failed_for=n["target"], delivery_error=err)
                backend.queue_notice(n["event_id"], chief, "fallback", payload)
        else:
            s.run("UPDATE notices SET attempts=?, last_error=?, next_at=? WHERE id=?",
                  attempts, err, now() + 30 * 2 ** (attempts - 1), n["id"])


def background(backend, stop):
    while not stop.wait(5):
        for job in (backend.reap, lambda: deliver_once(backend), backend.escalate, backend.watchdog, backend.retention):
            try:
                job()
            except Exception as e:  # keep the loop alive
                print("background error:", repr(e), file=sys.stderr, flush=True)


# ---------- HTTP ----------
ROUTES = []
EVERYONE = ("admin", "chief", "bot", "worker", "task")


def route(method, pattern, roles, doc, max_body=MAX_SMALL_BODY):
    """roles=None means public. Role and token are checked before any body is read."""
    def deco(fn):
        ROUTES.append({"method": method, "rx": re.compile("^" + pattern + "$"), "fn": fn, "roles": roles,
                       "doc": doc, "max_body": max_body, "pattern": pattern})
        return fn
    return deco


@route("GET", r"/health", None, "Liveness check, returns the version")
def r_health(api, who, m, qs, body, h):
    return {"ok": True, "version": VERSION}


@route("GET", r"/status", ("admin", "chief", "bot"), "Counts, running and waiting tasks, items due, warnings. A project bot sees only its own")
def r_status(api, who, m, qs, body, h):
    return api.status(who)


@route("POST", r"/tasks", ("admin", "chief", "bot"), "Queue a task, or a follow-up with `follow_up`", MAX_TASK_BODY)
def r_create(api, who, m, qs, body, h):
    return api.create_task(who, body)


@route("GET", r"/tasks", ("admin", "chief", "bot"), "List tasks. A project bot sees only its own")
def r_list(api, who, m, qs, body, h):
    return api.list_tasks(who, qs)


@route("GET", r"/tasks/([^/]+)", ("admin", "chief", "bot", "task"), "One task with its event thread. Bot: own only. Task token: own task only")
def r_get(api, who, m, qs, body, h):
    return {"task": api.public_task(api.get_task(who, m[0]), events=True)}


@route("POST", r"/tasks/([^/]+)/answer", ("admin", "chief", "bot"), "Answer a waiting task. Bot: own only, never for the human")
def r_answer(api, who, m, qs, body, h):
    return api.answer(who, m[0], body)


@route("POST", r"/tasks/([^/]+)/message", ("admin", "chief", "bot"), "Message a queued or running task. Bot: own only")
def r_message(api, who, m, qs, body, h):
    return api.message(who, m[0], body)


@route("POST", r"/tasks/([^/]+)/cancel", ("admin", "chief", "bot"), "Cancel now. Bot: own only")
def r_cancel(api, who, m, qs, body, h):
    return api.cancel(who, m[0])


@route("GET", r"/tasks/([^/]+)/report", ("admin", "chief", "bot"), "REPORT.md as text. Bot: own only")
def r_report(api, who, m, qs, body, h):
    api.get_task(who, m[0])
    f = api.s.one("SELECT data FROM files WHERE task_id = ? AND kind = 'report' ORDER BY at DESC LIMIT 1", m[0])
    if not f:
        raise ApiError(404, "no report yet")
    return ("text/markdown; charset=utf-8", bytes(f["data"]))


@route("GET", r"/tasks/([^/]+)/(artifacts|attachments)", ("admin", "chief", "bot", "task"), "List files. Bot and task token: own only")
def r_files(api, who, m, qs, body, h):
    api.get_task(who, m[0])
    kind = "artifact" if m[1] == "artifacts" else "attachment"
    return {"files": api.s.q("SELECT name, size, at FROM files WHERE task_id = ? AND kind = ? ORDER BY name", m[0], kind)}


@route("GET", r"/tasks/([^/]+)/(artifacts|attachments)/([^/]+)", ("admin", "chief", "bot", "task"), "Download one file. Bot and task token: own only")
def r_file(api, who, m, qs, body, h):
    api.get_task(who, m[0])
    kind = "artifact" if m[1] == "artifacts" else "attachment"
    f = api.s.one("SELECT data FROM files WHERE task_id = ? AND kind = ? AND name = ?", m[0], kind, urllib.parse.unquote(m[2]))
    if not f:
        raise ApiError(404, "no such file")
    return ("application/octet-stream", bytes(f["data"]))


@route("GET", r"/tasks/([^/]+)/bundle", ("task",), "The task's own saved work bundle (resume on a fresh folder)")
def r_bundle(api, who, m, qs, body, h):
    return api.own_bundle(who, m[0])


@route("GET", r"/tasks/([^/]+)/parent-bundle", ("task",), "A follow-up's parent work bundle (unpushed parent work)")
def r_parent_bundle(api, who, m, qs, body, h):
    return api.parent_bundle(who, m[0])


@route("PUT", r"/tasks/([^/]+)/files/(report|artifact|bundle)/([^/]+)", ("task",),
       "Upload a report, artifact or work bundle (raw body, `X-Claim-Id` header)", MAX_BUNDLE + 1024)
def r_put_file(api, who, m, qs, body, h):
    return api.put_file(who, m[0], m[1], urllib.parse.unquote(m[2]), body, h.get("X-Claim-Id"))


@route("POST", r"/worker/claim", ("worker",), "Take the oldest due queued task. Returns a task token for it")
def r_claim(api, who, m, qs, body, h):
    return {"task": api.claim(who, body)}


@route("GET", r"/worker/task-states", ("worker",), "States only (no content) for `ids=a,b,c`, used by cleanup")
def r_task_states(api, who, m, qs, body, h):
    return api.task_states(who, qs)


@route("POST", r"/tasks/([^/]+)/heartbeat", ("task",), "Extend the lease, get new messages, acknowledge delivered ones")
def r_heartbeat(api, who, m, qs, body, h):
    return api.heartbeat(who, m[0], body)


@route("POST", r"/tasks/([^/]+)/publish-check", ("task",), "Right before a push: is the claim live and is publishing still allowed")
def r_publish_check(api, who, m, qs, body, h):
    return api.publish_check(who, m[0], body)


@route("POST", r"/tasks/([^/]+)/finish", ("task",), "End the run: `done`, `waiting`, `next`, `limit` or `failed`. Idempotent per claim")
def r_finish(api, who, m, qs, body, h):
    return api.finish(who, m[0], body)


@route("GET", r"/bots", ("admin", "chief"), "List bots (URLs shortened except for admin, keys never shown)")
def r_bots(api, who, m, qs, body, h):
    return {"bots": [api.public_bot(b, who) for b in api.s.q("SELECT * FROM bots ORDER BY id")]}


@route("GET", r"/bots/([^/]+)", ("admin", "chief", "bot"), "One bot. Bot: itself only")
def r_bot(api, who, m, qs, body, h):
    if who["role"] == "bot" and who["bot"].lower() != m[0].lower():
        raise ApiError(403, "not your bot")
    b = api.bot(m[0])
    if not b:
        raise ApiError(404, "no such bot")
    return {"bot": api.public_bot(b, who)}


@route("PUT", r"/bots/([^/]+)", ("admin", "chief", "bot"), "Set `notify_url` and `notify_auth` (stored in a key file). Bot: itself only; only admin or chief set `role`")
def r_put_bot(api, who, m, qs, body, h):
    return api.put_bot(who, m[0], body)


@route("POST", r"/bots/([^/]+)/token", ("admin",), "Make a new token for the bot. Shown once")
def r_bot_token(api, who, m, qs, body, h):
    return api.bot_token(who, m[0])


@route("POST", r"/bots/([^/]+)/test", ("admin", "chief", "bot"), "Send one test notice to the bot's inbox. Bot: itself only")
def r_bot_test(api, who, m, qs, body, h):
    if who["role"] == "bot" and who["bot"].lower() != m[0].lower():
        raise ApiError(403, "not your bot")
    b = api.bot(m[0])
    if not b or not b["notify_url"]:
        raise ApiError(400, "bot has no notify_url")
    nonce = clean_text(body.get("nonce"), 40, "nonce") or secrets.token_hex(4)
    eid = f"test:{b['id']}:{nonce}"
    queued = api.queue_notice(eid, b, "test", {"event": "test", "event_id": eid, "idempotency_key": eid, "owner": b["id"],
                                               "text": "Setup check from the swarm backend. Reply is not needed."})
    return {"queued": queued, "event_id": eid}


@route("GET", r"/projects", ("admin", "chief", "bot"), "Project settings. Bot: only projects it is bound to")
def r_projects(api, who, m, qs, body, h):
    return api.list_projects(who)


@route("PUT", r"/projects/([^/]+)", ("admin", "chief"), "Set repo, extra repos, bound bots, provider, model, `allow_push`, `allow_push_main`")
def r_put_project(api, who, m, qs, body, h):
    return api.put_project(who, m[0], body)


@route("GET", r"/waiting", ("admin", "chief", "bot"), "The Waiting on you list. Bot: its own items only")
def r_items(api, who, m, qs, body, h):
    return api.list_items(who, qs)


@route("POST", r"/waiting", ("admin", "chief", "bot"), "File an item. A linked task must belong to the filing bot")
def r_file_item(api, who, m, qs, body, h):
    return api.file_item(who, body)


@route("POST", r"/waiting/([^/]+)", ("admin", "chief", "bot"), "answer, snooze, drop (human, through admin or chief); done, withdraw, update (filing bot too)")
def r_update_item(api, who, m, qs, body, h):
    return api.update_item(who, m[0], body)


@route("POST", r"/alerts", ("admin", "worker"), "Body `{key, text}`. One alert to the Chief of Staff per key")
def r_alert(api, who, m, qs, body, h):
    key = clean_text(body.get("key"), 200, "key")
    text = clean_text(body.get("text"), 4000, "text")
    if not key or not text:
        raise ApiError(400, "key and text are required")
    return {"queued": api.alert(key, text, who["role"])}


@route("GET", r"/notices", ("admin", "chief"), "The last 100 deliveries with state and last error")
def r_notices(api, who, m, qs, body, h):
    return {"notices": api.s.q("SELECT id, key, target, reason, event, state, attempts, last_error, created_at, sent_at"
                               " FROM notices ORDER BY id DESC LIMIT 100")}


def permission_table():
    """The permission matrix, as published in docs/backend.md."""
    cols = list(EVERYONE)
    lines = ["| Method | Path | " + " | ".join(cols) + " | What it does |",
             "| --- | --- | " + " | ".join("---" for _ in cols) + " | --- |"]
    for r in ROUTES:
        path = r["pattern"].replace("([^/]+)", "ID").replace("(artifacts|attachments)", "artifacts or attachments") \
            .replace("(report|artifact|bundle)", "KIND").replace("\\", "")
        cells = ["public" if r["roles"] is None else ("yes" if c in r["roles"] else "no") for c in cols]
        lines.append(f"| {r['method']} | `{path}` | " + " | ".join(cells) + f" | {r['doc']} |")
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    api = None
    server_version = "swarm-backend/" + VERSION
    timeout = 30  # seconds for any single read or write on the socket

    def log_message(self, fmt, *args):
        if os.environ.get("SWARM_QUIET") != "1":
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def _send(self, status, obj, ctype="application/json"):
        data = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def client_key(self):
        peer = self.client_address[0]
        fwd = self.headers.get("X-Forwarded-For")
        if fwd and peer in ("127.0.0.1", "::1"):
            return fwd.split(",")[0].strip()[:64]
        return peer

    def _handle(self, method):
        parsed = urllib.parse.urlparse(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body_read = False
        try:
            found = None
            for r in ROUTES:
                if r["method"] == method:
                    match = r["rx"].match(parsed.path)
                    if match:
                        found = (r, match)
                        break
            if not found:
                raise ApiError(404, "no such route")
            r, match = found
            who = None
            if r["roles"] is not None:
                client = self.client_key()
                if self.api.limiter.blocked(client):
                    raise ApiError(429, "too many failed logins from this address; wait a few minutes")
                try:
                    who = self.api.who(self.headers.get("Authorization"))
                except ApiError as e:
                    if e.status == 401:
                        self.api.limiter.fail(client)
                    raise
                if who["role"] not in r["roles"]:
                    raise ApiError(403, "this token cannot do that")
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(411, "send a Content-Length; chunked bodies are not accepted")
            cl = self.headers.get("Content-Length") or "0"
            if not cl.isdigit():
                raise ApiError(400, "bad Content-Length")
            length = int(cl)
            if length > r["max_body"]:
                raise ApiError(413, "request too large")
            raw = self.rfile.read(length) if length else b""
            if len(raw) != length:
                raise ApiError(400, "body shorter than Content-Length")
            body_read = True
            if r["fn"] is r_put_file:
                body = raw
            else:
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    raise ApiError(400, "body must be JSON")
                if not isinstance(body, dict):
                    raise ApiError(400, "body must be a JSON object")
            res = r["fn"](self.api, who, match.groups(), qs, body, self.headers)
            if isinstance(res, tuple):
                return self._send(200, res[1], res[0])
            return self._send(200, res)
        except ApiError as e:
            if not body_read:
                self.close_connection = True
            self._send(e.status, dict({"error": e.message}, **e.extra))
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except Exception as e:  # never leak a stack trace to the client
            print("error:", repr(e), file=sys.stderr, flush=True)
            self.close_connection = True
            try:
                self._send(500, {"error": "internal error"})
            except OSError:
                pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


class Server(ThreadingHTTPServer):
    """Threaded server with a cap on requests in flight. Extra connections get 503 at once."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, addr, handler, max_conn):
        self.slots = threading.BoundedSemaphore(max_conn)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(db_path, bind, public_url=""):
    keys = KeyStore(env_path("SWARM_BOT_KEYS_DIR", "~/.config/swarm/bot-keys"))
    store = Store(db_path, keys)
    api = Backend(store, public_url, keys)
    host, port = bind.rsplit(":", 1)
    handler = type("BoundHandler", (Handler,), {"api": api})
    return Server((host.strip("[]"), int(port)), handler, env_int("SWARM_MAX_CONNECTIONS", 32)), api


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--permissions":
        print(permission_table())
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(VERSION)
        return
    db = env_path("SWARM_DB", "~/.local/share/swarm/swarm.db")
    bind = os.environ.get("SWARM_BIND", "127.0.0.1:8750")
    srv, api = make_server(db, bind, os.environ.get("SWARM_PUBLIC_URL", ""))
    if not api.admin_token:
        print("SWARM_ADMIN_TOKEN_FILE is missing or empty; refusing to start", file=sys.stderr)
        sys.exit(2)
    stop = threading.Event()
    threading.Thread(target=background, args=(api, stop), daemon=True).start()
    print(f"swarm-backend {VERSION} listening on {bind}, db {db}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
