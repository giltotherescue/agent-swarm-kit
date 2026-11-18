#!/usr/bin/env python3
"""swarm-backend: the task backend for agent-swarm-kit.

One small HTTP service plus a SQLite file. Python standard library only.
It holds tasks, their event threads, questions and answers, reports and
artifacts, bots and where to notify them, and the "Waiting on you" list.

Run:  python3 swarm_backend.py            (reads settings from the environment)
Env:  SWARM_DB                 path to the SQLite file (default ~/.local/share/swarm/swarm.db)
      SWARM_BIND               host:port to listen on (default 127.0.0.1:8750)
      SWARM_PUBLIC_URL         URL bots use to reach this service (used in report links)
      SWARM_ADMIN_TOKEN_FILE   file holding the admin token (owner of the swarm)
      SWARM_BACKEND_WORKER_TOKEN_FILE  file holding the worker token, for checking it
      SWARM_ALLOW_HTTP_NOTIFY  1 to allow plain http notify URLs (local testing only)
      SWARM_MAX_ATTEMPTS       default attempts per task before it stops resuming (default 5)

Auth: every request except GET /health needs "Authorization: Bearer <token>".
  admin token   everything
  worker token  claim, heartbeat, finish, upload files, read tasks
  bot token     made with POST /bots/<id>/token. A chief bot may do what admin does
                except worker calls. A project bot sees and changes only tasks it owns.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
MAX_FILE = 25 * 1024 * 1024
MAX_BODY = 60 * 1024 * 1024
TERMINAL = ("done", "failed", "cancelled")
NOTIFY_STATES = ("done", "failed", "cancelled", "waiting")
LEASE_SECONDS = 600


def now():
    return int(time.time())


def env_path(name, default):
    return os.path.expanduser(os.environ.get(name) or default)


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


SCHEMA = """
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
CREATE UNIQUE INDEX IF NOT EXISTS tasks_idem ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, not_before);
CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated_at);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at INTEGER, type TEXT, actor TEXT, text TEXT, meta TEXT);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
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
CREATE UNIQUE INDEX IF NOT EXISTS items_dedupe ON items(bot, dedupe_key) WHERE status IN ('open','snoozed');
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, event_id TEXT, target TEXT, reason TEXT,
  url TEXT, payload TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, next_at INTEGER,
  last_error TEXT, created_at INTEGER, sent_at INTEGER);
"""


class Store:
    """All database access. One connection per thread; writes are serialized by SQLite."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.local = threading.local()
        self.lock = threading.RLock()
        with self.lock:
            db = self.db()
            db.executescript(SCHEMA)
            db.commit()

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
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.db().execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, exc_type, exc, tb):
        try:
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
    if not isinstance(value, (str, int, float)):
        raise ApiError(400, f"{field} must be text")
    value = str(value).strip()
    if len(value) > limit:
        raise ApiError(400, f"{field} is too long (max {limit})")
    return value


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}$")


def valid_notify_url(url):
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return False
    allow_http = os.environ.get("SWARM_ALLOW_HTTP_NOTIFY") == "1"
    if p.scheme not in (("https", "http") if allow_http else ("https",)):
        return False
    if p.username or p.password or not p.hostname:
        return False
    if not allow_http and (p.hostname in ("localhost", "0.0.0.0") or p.hostname.startswith("127.")):
        return False
    return len(url) <= 2000


class Backend:
    def __init__(self, store, public_url=""):
        self.s = store
        self.public_url = public_url.rstrip("/")
        self.admin_token = read_token("SWARM_ADMIN_TOKEN_FILE")
        self.worker_token = read_token("SWARM_BACKEND_WORKER_TOKEN_FILE")
        self.max_attempts = int(os.environ.get("SWARM_MAX_ATTEMPTS", "5"))

    # ---------- auth ----------
    def who(self, header):
        if not header or not header.lower().startswith("bearer "):
            raise ApiError(401, "missing bearer token")
        tok = header.split(None, 1)[1].strip()
        if self.admin_token and hmac.compare_digest(tok, self.admin_token):
            return {"role": "admin", "bot": None}
        if self.worker_token and hmac.compare_digest(tok, self.worker_token):
            return {"role": "worker", "bot": None}
        bot = self.s.one("SELECT id, role FROM bots WHERE token_hash = ?", token_hash(tok))
        if bot:
            return {"role": "chief" if bot["role"] == "chief" else "bot", "bot": bot["id"]}
        raise ApiError(401, "token not accepted")

    @staticmethod
    def need(who, *roles):
        if who["role"] not in roles:
            raise ApiError(403, "this token cannot do that")

    def can_see(self, who, task):
        if who["role"] in ("admin", "worker", "chief"):
            return True
        return who["role"] == "bot" and (task.get("owner") or "").lower() == who["bot"].lower()

    def get_task(self, who, task_id):
        t = self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
        if not t or not self.can_see(who, t):
            raise ApiError(404, "no such task")
        return t

    # ---------- events and notices ----------
    def event(self, task_id, typ, actor, text=None, meta=None):
        self.s.run("INSERT INTO events(task_id, at, type, actor, text, meta) VALUES(?,?,?,?,?,?)",
                   task_id, now(), typ, actor, text, json.dumps(meta) if meta else None)

    def chief(self):
        return self.s.one("SELECT * FROM bots WHERE role = 'chief' AND notify_url IS NOT NULL ORDER BY created_at LIMIT 1")

    def payload(self, task, event_id, reason):
        title = next((ln.strip() for ln in (task.get("title") or task["prompt"]).splitlines() if ln.strip()), "")
        p = {
            "event_id": event_id, "idempotency_key": event_id, "reason": reason,
            "task_id": task["id"], "owner": task.get("owner"), "project": task.get("project"),
            "kind": task.get("kind"), "title": title[:200], "state": task["state"],
            "outcome": task.get("outcome"), "waiting_on": task.get("waiting_on"),
            "summary": (task.get("summary") or task.get("error") or "")[:500] or None,
            "pr_url": task.get("pr_url"),
            "report_url": f"{self.public_url}/tasks/{task['id']}/report" if self.public_url else None,
        }
        if task["state"] == "waiting":
            p["question"] = task.get("question")
        return p

    def queue_notice(self, event_id, target_bot, reason, payload):
        """Insert one delivery. The UNIQUE key makes the same event to the same bot impossible to send twice."""
        if not target_bot or not target_bot.get("notify_url"):
            if reason != "owner":
                print(f"notice {event_id} ({reason}) dropped: no Chief of Staff bot with a notify_url", file=sys.stderr, flush=True)
            return False
        key = f"{event_id}>{target_bot['id']}"
        body = dict(payload, notify_target=target_bot["id"], reason=reason)
        cur = self.s.run("INSERT OR IGNORE INTO notices(key, event_id, target, reason, url, payload, next_at, created_at)"
                         " VALUES(?,?,?,?,?,?,?,?)", key, event_id, target_bot["id"], reason,
                         target_bot["notify_url"], json.dumps(body), now(), now())
        return cur.rowcount == 1

    def notify_task(self, task):
        """Route a task event: owner bot first, Chief of Staff only for ownerless, waiting-on-human or fallback."""
        if task["state"] not in NOTIFY_STATES:
            return
        seq = self.s.one("SELECT COUNT(*) AS n FROM events WHERE task_id = ? AND type = 'state' AND text = ?",
                         task["id"], task["state"])["n"]
        event_id = f"{task['id']}:{task['state']}:{seq}"
        owner = None
        if task.get("owner"):
            owner = self.s.one("SELECT * FROM bots WHERE lower(id) = lower(?)", task["owner"])
        chief = self.chief()
        sent_to_owner = False
        if owner and owner.get("notify_url"):
            reason = "owner"
            sent_to_owner = True
            self.queue_notice(event_id, owner, reason, self.payload(task, event_id, reason))
        if not sent_to_owner:
            reason = "no_owner" if not task.get("owner") else "owner_not_registered"
            self.queue_notice(event_id, chief, reason, self.payload(task, event_id, reason))
        elif task["state"] == "waiting" and task.get("waiting_on") == "human" and chief and chief["id"] != owner["id"]:
            self.queue_notice(event_id, chief, "waiting_on_human", self.payload(task, event_id, "waiting_on_human"))

    def alert(self, key, text, actor="system"):
        chief = self.chief()
        event_id = f"alert:{key}"
        payload = {"event_id": event_id, "idempotency_key": event_id, "reason": "alert", "text": text, "from": actor}
        return self.queue_notice(event_id, chief, "alert", payload)

    def set_state(self, task, state, actor, notify=True, **fields):
        fields["state"] = state
        fields["updated_at"] = now()
        if state in TERMINAL:
            fields.setdefault("finished_at", now())
            fields["lease_worker"] = None
            fields["lease_until"] = None
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.s.run(f"UPDATE tasks SET {cols} WHERE id = ?", *fields.values(), task["id"])
        self.event(task["id"], "state", actor, state, {k: v for k, v in fields.items()
                                                        if k in ("outcome", "waiting_on", "question", "next_step", "error")} or None)
        fresh = self.s.one("SELECT * FROM tasks WHERE id = ?", task["id"])
        if notify:
            self.notify_task(fresh)
        return fresh

    # ---------- tasks ----------
    def create_task(self, who, b):
        self.need(who, "admin", "chief", "bot")
        prompt = clean_text(b.get("prompt"), 200_000, "prompt")
        command = clean_text(b.get("command"), 4000, "command")
        if not prompt:
            raise ApiError(400, "prompt is required (for a script task, say what the command is for)")
        owner = clean_text(b.get("owner"), 80, "owner") or who["bot"]
        if who["role"] == "bot" and owner and owner.lower() != who["bot"].lower():
            raise ApiError(403, "a project bot can only queue tasks it owns")
        project = clean_text(b.get("project"), 80, "project")
        proj = self.s.one("SELECT * FROM projects WHERE name = ?", project) if project else None
        repo = clean_text(b.get("repo"), 200, "repo") or (proj or {}).get("default_repo")
        if repo:
            repo = re.sub(r"^https://github\.com/", "", repo).removesuffix(".git").strip("/")
            if not REPO_RE.match(repo):
                raise ApiError(400, "repo must look like owner/name")
        provider = clean_text(b.get("provider"), 20, "provider") or (proj or {}).get("default_provider") or os.environ.get("SWARM_DEFAULT_PROVIDER", "claude")
        if provider not in ("claude", "codex"):
            raise ApiError(400, "provider must be claude or codex")
        model = clean_text(b.get("model"), 80, "model") or (proj or {}).get("default_model") or os.environ.get("SWARM_DEFAULT_MODEL", "default")
        report_only = 1 if b.get("report_only") or command else 0
        untrusted = 1 if b.get("untrusted_input") else 0
        push = 1 if (b.get("push") or b.get("open_pr")) else 0
        if b.get("pr_number") is not None and not (b.get("report_only") or command or b.get("untrusted_input")):
            push = 1  # working on an existing PR means updating it
        open_pr = 1 if b.get("open_pr") else 0
        if (push or open_pr) and (report_only or untrusted):
            raise ApiError(400, "report-only, script and untrusted-input tasks cannot push or open a PR")
        if (push or open_pr) and not repo:
            raise ApiError(400, "push and open_pr need a repo")
        if (push or open_pr) and proj and not proj.get("allow_push"):
            raise ApiError(400, f"project {project} does not allow pushing")
        kind = clean_text(b.get("kind"), 20, "kind") or ("script" if command else "code" if repo and not report_only else "research")
        idem = clean_text(b.get("idempotency_key"), 200, "idempotency_key")
        phash = hashlib.sha256(json.dumps([prompt, repo, command, provider, model], sort_keys=True).encode()).hexdigest()
        if idem:
            prior = self.s.one("SELECT * FROM tasks WHERE idempotency_key = ?", idem)
            if prior:
                if prior["prompt_hash"] != phash:
                    raise ApiError(409, "idempotency_key was already used for a different task. Use a new key for new work.",
                                   {"task_id": prior["id"]})
                return {"task": self.public_task(prior), "duplicate": True}
        elif not b.get("allow_duplicate"):
            prior = self.s.one("SELECT * FROM tasks WHERE prompt_hash = ? AND state IN ('queued','running','waiting')"
                               " AND created_at > ? ORDER BY created_at DESC LIMIT 1", phash, now() - 86400)
            if prior:
                return {"task": self.public_task(prior), "duplicate": True}
        pr_number = b.get("pr_number")
        if pr_number is not None and not str(pr_number).isdigit():
            raise ApiError(400, "pr_number must be a number")
        tid = clean_text(b.get("id"), 80, "id") or new_id()
        if not ID_RE.match(tid):
            raise ApiError(400, "id may use letters, digits, dot, dash, underscore and colon")
        attachments = b.get("attachments") or []
        if not isinstance(attachments, list) or len(attachments) > 20:
            raise ApiError(400, "attachments must be a list of at most 20 files")
        decoded = []
        for a in attachments:
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
        t = now()
        with self.s.tx():
            try:
                self.s.run(
                    "INSERT INTO tasks(id, created_at, updated_at, project, owner, kind, title, prompt, prompt_hash, repo,"
                    " branch, pr_number, provider, model, command, report_only, push, open_pr, ask_human, untrusted,"
                    " idempotency_key, state, max_attempts, parent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tid, t, t, project, owner, kind, clean_text(b.get("title"), 200, "title"), prompt, phash, repo,
                    clean_text(b.get("branch"), 200, "branch"), int(pr_number) if pr_number is not None else None,
                    provider, model, command, report_only, push, open_pr, 1 if b.get("ask_human") else 0, untrusted,
                    idem, "queued", int(b.get("max_attempts") or self.max_attempts), clean_text(b.get("parent_id"), 80, "parent_id"))
            except sqlite3.IntegrityError:
                raise ApiError(409, "a task with that id or idempotency_key already exists")
            for name, data in decoded:
                self.s.run("INSERT INTO files(task_id, kind, name, data, size, at) VALUES(?,?,?,?,?,?)",
                           tid, "attachment", name, data, len(data), t)
            self.event(tid, "brief", owner or who["role"], prompt[:4000])
            self.event(tid, "state", "backend", "queued")
        return {"task": self.public_task(self.s.one("SELECT * FROM tasks WHERE id = ?", tid)), "duplicate": False}

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
            where.append("updated_at > ?")
            args.append(int(float(qs["updated_since"])))
        limit = min(int(qs.get("limit") or 50), 500)
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
            t = self.set_state(t, "queued", actor, pending_answer=text, waiting_on=None, question=None, outcome=None, not_before=0)
        return {"task": self.public_task(t), "resumed": True}

    def message(self, who, task_id, b):
        self.need(who, "admin", "chief", "bot")
        text = clean_text(b.get("text"), 20000, "text")
        if not text:
            raise ApiError(400, "text is required")
        t = self.get_task(who, task_id)
        if t["state"] in TERMINAL:
            raise ApiError(409, "task already ended. Queue a follow-up task instead.")
        if t["state"] == "waiting":
            if t["waiting_on"] == "human" and who["role"] == "bot":
                raise ApiError(409, "task waits on the human; a message cannot answer for them")
            r = self.answer(who, task_id, {"answer": text}, via="message")
            r["treated_as_answer"] = True
            return r
        sender = clean_text(b.get("from"), 80, "from") or who["bot"] or who["role"]
        self.s.run("INSERT INTO messages(task_id, at, sender, text) VALUES(?,?,?,?)", task_id, now(), sender, text)
        self.event(task_id, "message", sender, text)
        return {"ok": True, "stored": True}

    def cancel(self, who, task_id):
        self.need(who, "admin", "chief", "bot")
        with self.s.tx():
            t = self.get_task(who, task_id)
            if t["state"] in TERMINAL:
                return {"task": self.public_task(t)}
            if t["state"] == "running":
                self.s.run("UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE id = ?", now(), task_id)
                self.event(task_id, "cancel", who["bot"] or who["role"], "cancel requested; stops at next heartbeat")
                return {"task": self.public_task(self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)), "pending": True}
            self.s.run("UPDATE items SET status = 'done', updated_at = ? WHERE task_id = ? AND status IN ('open','snoozed')", now(), task_id)
            # The owner cancelling its own task needs no notice; anyone else cancelling it does.
            by_owner = bool(who["bot"]) and who["bot"].lower() == (t["owner"] or "").lower()
            t = self.set_state(t, "cancelled", who["bot"] or who["role"], notify=not by_owner, outcome="cancelled")
        return {"task": self.public_task(t)}

    # ---------- worker side ----------
    def claim(self, who, b):
        self.need(who, "worker", "admin")
        worker = clean_text(b.get("worker"), 80, "worker") or "worker"
        with self.s.tx():
            t = self.s.one("SELECT * FROM tasks WHERE state = 'queued' AND not_before <= ? ORDER BY created_at LIMIT 1", now())
            if not t:
                return None
            attempts = t["attempts"] + 1
            self.s.run("UPDATE tasks SET state = 'running', attempts = ?, lease_worker = ?, lease_until = ?, updated_at = ? WHERE id = ?",
                       attempts, worker, now() + LEASE_SECONDS, now(), t["id"])
            self.event(t["id"], "state", worker, "running", {"attempt": attempts})
            t = self.s.one("SELECT * FROM tasks WHERE id = ?", t["id"])
        out = dict(t)
        out["attachments"] = [r["name"] for r in self.s.q("SELECT name FROM files WHERE task_id = ? AND kind = 'attachment'", t["id"])]
        out["answers"] = self.s.q("SELECT at, actor, text FROM events WHERE task_id = ? AND type = 'answer' ORDER BY id", t["id"])
        return out

    def heartbeat(self, who, task_id, b):
        self.need(who, "worker", "admin")
        with self.s.tx():
            t = self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
            if not t:
                raise ApiError(404, "no such task")
            if t["state"] != "running":
                return {"cancel": True, "reason": f"task is {t['state']}"}
            self.s.run("UPDATE tasks SET lease_until = ?, updated_at = ? WHERE id = ?", now() + LEASE_SECONDS, now(), task_id)
            msgs = self.s.q("SELECT id, at, sender, text FROM messages WHERE task_id = ? AND delivered = 0 ORDER BY id", task_id)
            if msgs:
                self.s.run(f"UPDATE messages SET delivered = 1 WHERE id IN ({','.join('?' * len(msgs))})", *[m["id"] for m in msgs])
            if b.get("note"):
                self.event(task_id, "progress", t["lease_worker"] or "worker", clean_text(b["note"], 2000, "note"))
        return {"cancel": bool(t["cancel_requested"]), "messages": msgs}

    def finish(self, who, task_id, b):
        """status: done | waiting | next | failed | limit. Applies retry rules and fires notices."""
        self.need(who, "worker", "admin")
        status = b.get("status")
        if status not in ("done", "waiting", "next", "failed", "limit"):
            raise ApiError(400, "status must be done, waiting, next, failed or limit")
        actor = clean_text(b.get("worker"), 80, "worker") or "worker"
        with self.s.tx():
            t = self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
            if not t:
                raise ApiError(404, "no such task")
            if t["state"] in TERMINAL:
                return {"task": self.public_task(t), "ignored": True}
            common = dict(summary=clean_text(b.get("summary"), 2000, "summary") or t["summary"],
                          pr_url=clean_text(b.get("pr_url"), 500, "pr_url") or t["pr_url"],
                          session_provider=clean_text(b.get("session_provider"), 20, "session_provider") or t["session_provider"],
                          session_id=clean_text(b.get("session_id"), 200, "session_id") or t["session_id"],
                          pending_answer=None)
            if t["cancel_requested"]:
                t = self.set_state(t, "cancelled", actor, outcome="cancelled", **common)
            elif status == "done":
                t = self.set_state(t, "done", actor, outcome="done", error=None, **common)
            elif status == "waiting":
                q = clean_text(b.get("question"), 8000, "question") or "The worker needs an answer (no question text was given)."
                on = "human" if t["ask_human"] or b.get("waiting_on") == "human" else "owner"
                t = self.set_state(t, "waiting", actor, outcome="waiting", question=q, waiting_on=on, **common)
            elif status in ("next", "limit") and t["attempts"] < t["max_attempts"]:
                delay = 0 if status == "next" else min(3600 * 2 ** max(t["attempts"] - 1, 0), 86400)
                step = clean_text(b.get("next_step"), 4000, "next_step")
                t = self.set_state(t, "queued", actor, outcome=status, next_step=step, not_before=now() + delay,
                                   lease_worker=None, lease_until=None, **common)
            elif status in ("next", "limit"):
                q = (f"This task stopped after {t['attempts']} attempts (last status: {status}). "
                     "Answer with how to continue, or cancel it.")
                t = self.set_state(t, "waiting", actor, outcome="attempt_cap", question=q, waiting_on="owner", **common)
            else:
                t = self.set_state(t, "failed", actor, outcome=clean_text(b.get("outcome"), 40, "outcome") or "failed",
                                   error=clean_text(b.get("error"), 4000, "error"), **common)
        return {"task": self.public_task(t)}

    def put_file(self, who, task_id, kind, name, data):
        self.need(who, "worker", "admin")
        if kind not in ("report", "artifact"):
            raise ApiError(400, "kind must be report or artifact")
        name = os.path.basename(name)
        if not SAFE_NAME.match(name):
            raise ApiError(400, "bad file name")
        if len(data) > MAX_FILE:
            raise ApiError(413, "file larger than 25 MB")
        if not self.s.one("SELECT 1 FROM tasks WHERE id = ?", task_id):
            raise ApiError(404, "no such task")
        self.s.run("INSERT OR REPLACE INTO files(task_id, kind, name, data, size, at) VALUES(?,?,?,?,?,?)",
                   task_id, kind, name, data, len(data), now())
        self.event(task_id, kind, "worker", name, {"size": len(data)})
        return {"ok": True}

    def reap(self):
        """Requeue running tasks whose worker stopped heartbeating; release snoozed items."""
        with self.s.tx():
            for t in self.s.q("SELECT * FROM tasks WHERE state = 'running' AND lease_until < ?", now()):
                if t["attempts"] < t["max_attempts"]:
                    self.set_state(t, "queued", "reaper", outcome="lost", lease_worker=None, lease_until=None, not_before=now() + 60)
                else:
                    self.set_state(t, "failed", "reaper", outcome="lost", error="worker stopped responding too many times")
            self.s.run("UPDATE items SET status = 'open', snooze_until = NULL, updated_at = ? WHERE status = 'snoozed' AND snooze_until <= ?",
                       now(), now())

    # ---------- bots and projects ----------
    def put_bot(self, who, bot_id, b):
        self.need(who, "admin", "chief", "bot")
        if who["role"] == "bot" and bot_id.lower() != who["bot"].lower():
            raise ApiError(403, "a bot can only change its own settings")
        if not ID_RE.match(bot_id):
            raise ApiError(400, "bad bot id")
        role = b.get("role")
        if role is not None and who["role"] not in ("admin", "chief"):
            raise ApiError(403, "only admin can set a bot's role")
        if role not in (None, "chief", "project"):
            raise ApiError(400, "role must be chief or project")
        url = b.get("notify_url")
        if url is not None and url != "" and not valid_notify_url(url):
            raise ApiError(400, "notify_url must be an https URL with no credentials in it")
        prior = self.s.one("SELECT * FROM bots WHERE id = ?", bot_id)
        t = now()
        if not prior:
            self.s.run("INSERT INTO bots(id, role, created_at, updated_at) VALUES(?,?,?,?)", bot_id, role or "project", t, t)
        if role:
            self.s.run("UPDATE bots SET role = ? WHERE id = ?", role, bot_id)
        if url is not None:
            self.s.run("UPDATE bots SET notify_url = ? WHERE id = ?", url or None, bot_id)
        if b.get("notify_auth") is not None:
            self.s.run("UPDATE bots SET notify_auth = ? WHERE id = ?", b["notify_auth"] or None, bot_id)
        self.s.run("UPDATE bots SET updated_at = ? WHERE id = ?", t, bot_id)
        return {"bot": self.public_bot(self.s.one("SELECT * FROM bots WHERE id = ?", bot_id))}

    @staticmethod
    def public_bot(b):
        return {"id": b["id"], "role": b["role"], "notify_url": b["notify_url"],
                "has_notify_auth": bool(b["notify_auth"]), "has_token": bool(b["token_hash"])}

    def bot_token(self, who, bot_id):
        self.need(who, "admin")
        if not self.s.one("SELECT 1 FROM bots WHERE id = ?", bot_id):
            raise ApiError(404, "no such bot; PUT /bots/<id> first")
        tok = "swb_" + secrets.token_urlsafe(32)
        self.s.run("UPDATE bots SET token_hash = ?, updated_at = ? WHERE id = ?", token_hash(tok), now(), bot_id)
        return {"bot": bot_id, "token": tok, "note": "Shown once. Store it in a file with chmod 600. Never paste it in chat."}

    def put_project(self, who, name, b):
        self.need(who, "admin", "chief")
        if not ID_RE.match(name):
            raise ApiError(400, "bad project name")
        cur = self.s.one("SELECT * FROM projects WHERE name = ?", name) or {"allow_push": 1}
        vals = {k: b.get(k, cur.get(k)) for k in ("default_repo", "default_provider", "default_model")}
        allow = 1 if b.get("allow_push", cur.get("allow_push")) else 0
        self.s.run("INSERT OR REPLACE INTO projects(name, default_repo, default_provider, default_model, allow_push, updated_at)"
                   " VALUES(?,?,?,?,?,?)", name, vals["default_repo"], vals["default_provider"], vals["default_model"], allow, now())
        return {"project": self.s.one("SELECT * FROM projects WHERE name = ?", name)}

    # ---------- Waiting on you ----------
    def shape_item(self, r):
        r = dict(r)
        r["options"] = json.loads(r["options"] or "[]")
        r["links"] = json.loads(r["links"] or "[]")
        r["source"] = "filed"
        return r

    def file_item(self, who, b):
        self.need(who, "admin", "chief", "bot")
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
        lists = {}
        for k in ("links", "options"):
            v = b.get(k) or []
            if not isinstance(v, list) or len(v) > 20:
                raise ApiError(400, f"{k} must be a list (max 20)")
            lists[k] = json.dumps([str(x)[:2000] for x in v])
        dropped = self.s.one("SELECT id FROM items WHERE bot = ? AND dedupe_key = ? AND status = 'dropped'", bot, fields["dedupe_key"])
        if dropped and not b.get("refile_dropped"):
            raise ApiError(409, "the human dropped this ask. Do not file it again unless they ask for it.", {"item_id": dropped["id"]})
        t = now()
        with self.s.tx():
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
        if status in ("due", "open", "all"):
            covered = {i["task_id"] for i in items if i["task_id"] and i["status"] in ("open", "snoozed")}
            for t in self.s.q("SELECT * FROM tasks WHERE state = 'waiting' AND waiting_on = 'human' ORDER BY created_at"):
                if t["id"] not in covered:
                    items.append({"id": t["id"], "source": "task", "task_id": t["id"], "project": t["project"], "bot": t["owner"],
                                  "kind": "decision", "ask": t["question"], "recommendation": None, "options": [], "links":
                                  [x for x in (t["pr_url"],) if x], "status": "open", "created_at": t["updated_at"]})
        for f in ("project", "bot"):
            if qs.get(f):
                items = [i for i in items if (i.get(f) or "").lower() == qs[f].lower()]
        if who["role"] == "bot":
            items = [i for i in items if (i.get("bot") or "").lower() == who["bot"].lower()]
        return {"items": items}

    def update_item(self, who, item_id, b):
        self.need(who, "admin", "chief", "bot")
        action = b.get("action")
        if who["role"] == "bot" and action not in ("withdraw", "done", "update"):
            raise ApiError(403, "only the human (through admin or the Chief of Staff) can answer, snooze or drop")
        item = self.s.one("SELECT * FROM items WHERE id = ?", item_id)
        if not item:
            task = self.s.one("SELECT * FROM tasks WHERE id = ? AND state = 'waiting' AND waiting_on = 'human'", item_id)
            if task and action == "answer":
                return self.answer(who, item_id, {"answer": b.get("answer")}, via="waiting-list")
            if task and action == "drop":
                self.event(item_id, "dropped", who["bot"] or who["role"], b.get("note") or "dropped from the Waiting on you list")
                return self.cancel(who, item_id)
            raise ApiError(404, "no such item")
        if who["role"] == "bot" and item["bot"].lower() != who["bot"].lower():
            raise ApiError(403, "not your item")
        t = now()
        if action == "answer":
            ans = clean_text(b.get("answer"), 20000, "answer")
            if not ans:
                raise ApiError(400, "answer is required")
            self.s.run("UPDATE items SET status='answered', answer=?, updated_at=? WHERE id=?", ans, t, item_id)
            if item["task_id"]:
                task = self.s.one("SELECT * FROM tasks WHERE id = ?", item["task_id"])
                if task and task["state"] == "waiting":
                    self.answer({"role": "admin", "bot": None}, task["id"], {"answer": ans}, via="waiting-list")
        elif action == "snooze":
            until = int(b.get("until") or (t + 86400))
            self.s.run("UPDATE items SET status='snoozed', snooze_until=?, updated_at=? WHERE id=?", until, t, item_id)
        elif action == "drop":
            self.s.run("UPDATE items SET status='dropped', answer=?, updated_at=? WHERE id=?", b.get("note") or "Don't need it", t, item_id)
            if item["task_id"]:
                task = self.s.one("SELECT * FROM tasks WHERE id = ?", item["task_id"])
                if task and task["state"] not in TERMINAL:
                    self.cancel({"role": "admin", "bot": None}, task["id"])
        elif action in ("done", "withdraw"):
            self.s.run("UPDATE items SET status=?, answer=COALESCE(?, answer), updated_at=? WHERE id=?",
                       "done" if action == "done" else "withdrawn", b.get("note"), t, item_id)
        elif action == "update":
            return self.file_item(who, dict(item, **{k: v for k, v in b.items() if k != "action"},
                                            options=json.loads(item["options"] or "[]") if "options" not in b else b["options"],
                                            links=json.loads(item["links"] or "[]") if "links" not in b else b["links"]))
        else:
            raise ApiError(400, "action must be answer, snooze, drop, done, withdraw or update")
        item = self.s.one("SELECT * FROM items WHERE id = ?", item_id)
        if action in ("answer", "drop"):
            filer = self.s.one("SELECT * FROM bots WHERE lower(id) = lower(?)", item["bot"])
            eid = f"item:{item_id}:{action}:{item['revision']}"
            payload = {"event_id": eid, "idempotency_key": eid, "reason": "item_" + action, "item_id": item_id,
                       "project": item["project"], "ask": item["ask"], "answer": item["answer"], "task_id": item["task_id"],
                       "dropped": action == "drop", "note": "dropped: stop work on this" if action == "drop" else None}
            if not self.queue_notice(eid, filer, "owner", payload) and not (filer and filer.get("notify_url")):
                self.queue_notice(eid, self.chief(), "owner_not_registered", payload)
        return {"item": self.shape_item(item)}

    # ---------- status ----------
    def status(self, who):
        counts = {r["state"]: r["n"] for r in self.s.q("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")}
        notices = {r["state"]: r["n"] for r in self.s.q("SELECT state, COUNT(*) AS n FROM notices GROUP BY state")}
        waiting = self.s.q("SELECT id, owner, project, waiting_on, question FROM tasks WHERE state = 'waiting' ORDER BY updated_at")
        running = self.s.q("SELECT id, owner, project, lease_worker, lease_until FROM tasks WHERE state = 'running'")
        if who["role"] == "bot":
            waiting = [w for w in waiting if (w["owner"] or "").lower() == who["bot"].lower()]
            running = [r for r in running if (r["owner"] or "").lower() == who["bot"].lower()]
        stuck = [r["id"] for r in running if (r["lease_until"] or 0) < now()]
        return {"version": VERSION, "tasks": counts, "notices": notices, "running": running, "waiting": waiting,
                "items_due": len(self.list_items(who, {"status": "due"})["items"]), "stuck_running": stuck,
                "warnings": [] if self.chief() else ["No Chief of Staff bot with a notify_url: fallback and alert notices are dropped."]}


# ---------- notice delivery ----------
def deliver_once(backend, opener=urllib.request.urlopen):
    s = backend.s
    rows = s.q("SELECT * FROM notices WHERE state = 'pending' AND next_at <= ? ORDER BY id LIMIT 20", now())
    for n in rows:
        bot = s.one("SELECT * FROM bots WHERE id = ?", n["target"]) or {}
        headers = {"Content-Type": "application/json", "User-Agent": "agent-swarm-kit/" + VERSION,
                   "Idempotency-Key": n["event_id"]}
        if bot.get("notify_auth"):
            v = bot["notify_auth"].strip()
            headers["Authorization"] = v if " " in v else f"Bearer {v}"
        ok, err, permanent = False, None, False
        try:
            req = urllib.request.Request(n["url"], data=n["payload"].encode(), headers=headers, method="POST")
            with opener(req, timeout=15) as resp:
                ok = 200 <= resp.status < 300
                err = None if ok else f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            permanent = 400 <= e.code < 500 and e.code not in (408, 429)
        except Exception as e:  # network errors are retried
            err = str(e)[:300]
        attempts = n["attempts"] + 1
        if ok:
            s.run("UPDATE notices SET state='sent', attempts=?, sent_at=?, last_error=NULL WHERE id=?", attempts, now(), n["id"])
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
        try:
            backend.reap()
            deliver_once(backend)
        except Exception as e:  # keep the loop alive
            print("background error:", e, file=sys.stderr, flush=True)


# ---------- HTTP ----------
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


@route("GET", r"/health")
def r_health(api, who, m, qs, body):
    return {"ok": True, "version": VERSION}


@route("GET", r"/status")
def r_status(api, who, m, qs, body):
    return api.status(who)


@route("POST", r"/tasks")
def r_create(api, who, m, qs, body):
    return api.create_task(who, body)


@route("GET", r"/tasks")
def r_list(api, who, m, qs, body):
    return api.list_tasks(who, qs)


@route("GET", r"/tasks/([^/]+)")
def r_get(api, who, m, qs, body):
    return {"task": api.public_task(api.get_task(who, m[0]), events=True)}


@route("POST", r"/tasks/([^/]+)/answer")
def r_answer(api, who, m, qs, body):
    return api.answer(who, m[0], body)


@route("POST", r"/tasks/([^/]+)/message")
def r_message(api, who, m, qs, body):
    return api.message(who, m[0], body)


@route("POST", r"/tasks/([^/]+)/cancel")
def r_cancel(api, who, m, qs, body):
    return api.cancel(who, m[0])


@route("GET", r"/tasks/([^/]+)/report")
def r_report(api, who, m, qs, body):
    api.get_task(who, m[0])
    f = api.s.one("SELECT data FROM files WHERE task_id = ? AND kind = 'report' ORDER BY at DESC LIMIT 1", m[0])
    if not f:
        raise ApiError(404, "no report yet")
    return ("text/markdown; charset=utf-8", bytes(f["data"]))


@route("GET", r"/tasks/([^/]+)/(artifacts|attachments)")
def r_files(api, who, m, qs, body):
    api.get_task(who, m[0])
    kind = "artifact" if m[1] == "artifacts" else "attachment"
    return {"files": api.s.q("SELECT name, size, at FROM files WHERE task_id = ? AND kind = ? ORDER BY name", m[0], kind)}


@route("GET", r"/tasks/([^/]+)/(artifacts|attachments)/([^/]+)")
def r_file(api, who, m, qs, body):
    api.get_task(who, m[0])
    kind = "artifact" if m[1] == "artifacts" else "attachment"
    f = api.s.one("SELECT data FROM files WHERE task_id = ? AND kind = ? AND name = ?", m[0], kind, urllib.parse.unquote(m[2]))
    if not f:
        raise ApiError(404, "no such file")
    return ("application/octet-stream", bytes(f["data"]))


@route("PUT", r"/tasks/([^/]+)/files/(report|artifact)/([^/]+)")
def r_put_file(api, who, m, qs, body):
    return api.put_file(who, m[0], m[1], urllib.parse.unquote(m[2]), body)


@route("POST", r"/worker/claim")
def r_claim(api, who, m, qs, body):
    t = api.claim(who, body)
    return {"task": t}


@route("POST", r"/tasks/([^/]+)/heartbeat")
def r_heartbeat(api, who, m, qs, body):
    return api.heartbeat(who, m[0], body)


@route("POST", r"/tasks/([^/]+)/finish")
def r_finish(api, who, m, qs, body):
    return api.finish(who, m[0], body)


@route("GET", r"/bots")
def r_bots(api, who, m, qs, body):
    api.need(who, "admin", "chief")
    return {"bots": [api.public_bot(b) for b in api.s.q("SELECT * FROM bots ORDER BY id")]}


@route("GET", r"/bots/([^/]+)")
def r_bot(api, who, m, qs, body):
    if who["role"] == "bot" and who["bot"].lower() != m[0].lower():
        raise ApiError(403, "not your bot")
    b = api.s.one("SELECT * FROM bots WHERE lower(id) = lower(?)", m[0])
    if not b:
        raise ApiError(404, "no such bot")
    return {"bot": api.public_bot(b)}


@route("PUT", r"/bots/([^/]+)")
def r_put_bot(api, who, m, qs, body):
    return api.put_bot(who, m[0], body)


@route("POST", r"/bots/([^/]+)/token")
def r_bot_token(api, who, m, qs, body):
    return api.bot_token(who, m[0])


@route("POST", r"/bots/([^/]+)/test")
def r_bot_test(api, who, m, qs, body):
    """Send one test notice so a bot can check its own inbox and trigger."""
    if who["role"] == "bot" and who["bot"].lower() != m[0].lower():
        raise ApiError(403, "not your bot")
    b = api.s.one("SELECT * FROM bots WHERE lower(id) = lower(?)", m[0])
    if not b or not b["notify_url"]:
        raise ApiError(400, "bot has no notify_url")
    eid = f"test:{b['id']}:{body.get('nonce') or secrets.token_hex(4)}"
    queued = api.queue_notice(eid, b, "test", {"event_id": eid, "idempotency_key": eid, "reason": "test",
                                                "owner": b["id"], "text": "Setup check from the swarm backend. Reply is not needed."})
    return {"queued": queued, "event_id": eid}


@route("GET", r"/projects")
def r_projects(api, who, m, qs, body):
    return {"projects": api.s.q("SELECT * FROM projects ORDER BY name")}


@route("PUT", r"/projects/([^/]+)")
def r_put_project(api, who, m, qs, body):
    return api.put_project(who, m[0], body)


@route("GET", r"/waiting")
def r_items(api, who, m, qs, body):
    return api.list_items(who, qs)


@route("POST", r"/waiting")
def r_file_item(api, who, m, qs, body):
    return api.file_item(who, body)


@route("POST", r"/waiting/([^/]+)")
def r_update_item(api, who, m, qs, body):
    return api.update_item(who, m[0], body)


@route("POST", r"/alerts")
def r_alert(api, who, m, qs, body):
    api.need(who, "admin", "worker")
    key = clean_text(body.get("key"), 200, "key")
    text = clean_text(body.get("text"), 4000, "text")
    if not key or not text:
        raise ApiError(400, "key and text are required")
    return {"queued": api.alert(key, text, who["role"])}


@route("GET", r"/notices")
def r_notices(api, who, m, qs, body):
    api.need(who, "admin", "chief")
    return {"notices": api.s.q("SELECT id, key, target, reason, state, attempts, last_error, created_at, sent_at"
                               " FROM notices ORDER BY id DESC LIMIT 100")}


class Handler(BaseHTTPRequestHandler):
    api = None
    server_version = "swarm-backend/" + VERSION

    def log_message(self, fmt, *args):
        if os.environ.get("SWARM_QUIET") != "1":
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def _send(self, status, obj, ctype="application/json"):
        data = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        parsed = urllib.parse.urlparse(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "request too large")
            raw = self.rfile.read(length) if length else b""
            for m, rx, fn in ROUTES:
                if m != method:
                    continue
                match = rx.match(parsed.path)
                if not match:
                    continue
                who = None if fn is r_health else self.api.who(self.headers.get("Authorization"))
                if fn is r_put_file:
                    body = raw
                else:
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        raise ApiError(400, "body must be JSON")
                    if not isinstance(body, dict):
                        raise ApiError(400, "body must be a JSON object")
                res = fn(self.api, who, match.groups(), qs, body)
                if isinstance(res, tuple):
                    return self._send(200, res[1], res[0])
                return self._send(200, res)
            raise ApiError(404, "no such route")
        except ApiError as e:
            self._send(e.status, dict({"error": e.message}, **e.extra))
        except Exception as e:  # never leak a stack trace to the client
            print("error:", repr(e), file=sys.stderr, flush=True)
            self._send(500, {"error": "internal error"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


def make_server(db_path, bind, public_url=""):
    store = Store(db_path)
    api = Backend(store, public_url)
    host, port = bind.rsplit(":", 1)
    handler = type("BoundHandler", (Handler,), {"api": api})
    return ThreadingHTTPServer((host, int(port)), handler), api


def main():
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
