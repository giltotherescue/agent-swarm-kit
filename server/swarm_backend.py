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
import html
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import swarm_bus
import swarm_gitlink
import swarm_review

VERSION = "0.8.2"
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
CREATE TABLE IF NOT EXISTS log_entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, bot TEXT, kind TEXT NOT NULL,
  summary TEXT NOT NULL, detail TEXT, links TEXT, task_id TEXT, dedupe_key TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS activity_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, bot TEXT NOT NULL, kind TEXT NOT NULL,
  decider TEXT, summary TEXT NOT NULL, link TEXT, overrules INTEGER, lasting INTEGER NOT NULL DEFAULT 0,
  dedupe_key TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orchestrators(
  name TEXT PRIMARY KEY, worker_id TEXT NOT NULL, project TEXT NOT NULL, repo TEXT, owner_bot_id TEXT,
  provider TEXT NOT NULL DEFAULT 'claude', model TEXT, brief TEXT NOT NULL, secret_bundles TEXT NOT NULL DEFAULT '[]',
  relay_agent_name TEXT, desired TEXT NOT NULL DEFAULT 'stopped', state TEXT NOT NULL DEFAULT 'stopped', session_id TEXT,
  pid INTEGER, tmux_session TEXT, restart_count INTEGER NOT NULL DEFAULT 0, started_at INTEGER, last_heartbeat_at INTEGER,
  last_exit TEXT, created_at INTEGER, updated_at INTEGER, desired_at INTEGER, generation INTEGER NOT NULL DEFAULT 0,
  restart_generation INTEGER NOT NULL DEFAULT 0, supervisor_id TEXT, log_tail TEXT, log_tail_at INTEGER, deleted_at INTEGER);
CREATE TABLE IF NOT EXISTS orchestrator_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, at INTEGER, type TEXT, actor TEXT, text TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS orchestrator_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, at INTEGER, sender TEXT, text TEXT, delivered_at INTEGER);
-- Agent Bus (docs/agent-bus.md). Same tables and columns as agent-control migration 0026; the 0027 wake-policy columns
-- are added by MIGRATE_COLUMNS below, so a new and an existing database take the same path.
CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('bot','orchestrator','human','service')),
  wake TEXT NOT NULL CHECK (wake IN ('webhook','tmux','poll')),
  bot_id TEXT, orchestrator TEXT, description TEXT,
  token_gen INTEGER NOT NULL DEFAULT 1,
  notices TEXT NOT NULL DEFAULT 'legacy' CHECK (notices IN ('legacy','bus')),
  last_seen_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, retired_at INTEGER);
CREATE TABLE IF NOT EXISTS agent_aliases(alias TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bus_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipient TEXT NOT NULL, thread TEXT NOT NULL,
  client_msg_id TEXT, body TEXT NOT NULL, meta TEXT, urgent INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','delivered','read','failed')),
  created_at INTEGER NOT NULL, delivered_at INTEGER, read_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER, lease_until INTEGER, last_error TEXT, alerted_at INTEGER,
  UNIQUE (sender, client_msg_id));
CREATE TABLE IF NOT EXISTS bus_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
  transport TEXT NOT NULL CHECK (transport IN ('webhook','tmux','poll')),
  started_at INTEGER NOT NULL, finished_at INTEGER, ok INTEGER NOT NULL DEFAULT 0, http_status INTEGER, error TEXT);
-- Review queue (docs/review-queue.md, a simple port of agent-control PR 62). One row per filed item; the latest decision is
-- on the row (decision JSON), delivered over the bus or left answered and unacked for the bot to read.
CREATE TABLE IF NOT EXISTS review_items(
  id TEXT PRIMARY KEY, project TEXT NOT NULL, bot TEXT NOT NULL, key TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'decision',
  title TEXT, ask TEXT, body TEXT, recommendation TEXT, options TEXT, links TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','changes_requested','approved','rejected','closed')),
  revision INTEGER NOT NULL DEFAULT 1, previous_item_id TEXT, close_reason TEXT,
  decision TEXT, decided_at INTEGER, delivery TEXT, bus_message_id INTEGER, delivery_error TEXT, acked_at INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
"""

# Columns added after 0.1.0. Added in place on an existing database.
MIGRATE_COLUMNS = {
    "tasks": [("claim_id", "TEXT"), ("claimed_at", "INTEGER"), ("follow_up", "INTEGER DEFAULT 0"),
              ("held_behind", "TEXT"), ("push_main", "INTEGER DEFAULT 0"), ("pushed_branch", "TEXT"), ("pushed_current", "INTEGER"),
              ("workdir", "TEXT"), ("workspace_host", "TEXT"), ("session_model", "TEXT"),
              ("unpushed", "INTEGER DEFAULT 0"), ("human_snooze_until", "INTEGER"),
              ("waiting_since", "INTEGER"), ("reminded_at", "INTEGER"), ("escalated_at", "INTEGER"),
              ("start_fresh", "INTEGER DEFAULT 0"), ("timeout", "INTEGER"), ("chain", "INTEGER DEFAULT 0"),
              ("chain_root", "TEXT"), ("cycle", "INTEGER DEFAULT 0"), ("max_cycles", "INTEGER"),
              ("target_worker", "TEXT"), ("failure_escalated_at", "INTEGER"), ("failure_resolved_at", "INTEGER"),
              ("head_sha", "TEXT"), ("unpushed_commits", "INTEGER"), ("base", "TEXT"),
              # 0.8.1 (agent-control PR 63): opt-in for pin bumps; submodule pins the auto-commit left uncommitted (JSON list)
              ("allow_gitlink", "INTEGER DEFAULT 0"), ("gitlink_skipped", "TEXT"), ("gitlink_skipped_count", "INTEGER")],
    "bots": [("notify_auth_fp", "TEXT"), ("notify_version", "INTEGER DEFAULT 0")],
    "projects": [("bots", "TEXT"), ("repos", "TEXT"), ("allow_push_main", "INTEGER DEFAULT 0"), ("log_git", "INTEGER DEFAULT 0")],
    "log_entries": [("ref", "TEXT"), ("status", "TEXT"), ("version", "INTEGER"), ("author", "TEXT"), ("source", "TEXT"),
                    ("answer", "TEXT"), ("before_text", "TEXT"), ("after_text", "TEXT"), ("expires_at", "INTEGER"),
                    ("about", "INTEGER")],
    "notices": [("event", "TEXT"), ("notify_version", "INTEGER DEFAULT 0")],
    "workers": [("projects", "TEXT")],
    # 0.8.0 Agent Bus: links a message typed into an orchestrator pane to its bus row; then the wake policy (agent-control 0027).
    "orchestrator_messages": [("bus_message_id", "INTEGER"),
                              # 0.8.1: an admin released a delivery-unknown hold for one more typing (counter, see release)
                              ("release_count", "INTEGER NOT NULL DEFAULT 0")],
    "agents": [("digest_minutes", "INTEGER NOT NULL DEFAULT 60"), ("last_digest_at", "INTEGER"), ("unreachable_since", "INTEGER"),
               ("alerted_at", "INTEGER"), ("escalated_at", "INTEGER"),
               # Review queue port: a poll agent's declared check-in window (minutes); reachability allows that long.
               ("poll_window_minutes", "INTEGER")],
    "bus_messages": [("wake", "TEXT"), ("held", "INTEGER NOT NULL DEFAULT 0"), ("noop", "INTEGER NOT NULL DEFAULT 0"),
                     ("reply_expected", "INTEGER NOT NULL DEFAULT 0"),
                     # 0.8.2: informational messages (bus send --fyi) are actionable = 0; every older row and unmarked message is 1.
                     ("actionable", "INTEGER NOT NULL DEFAULT 1")],
}

SCHEMA_INDEXES = """
DROP INDEX IF EXISTS tasks_idem;
CREATE UNIQUE INDEX IF NOT EXISTS tasks_idem_owner ON tasks(coalesce(lower(owner), ''), idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, not_before);
CREATE INDEX IF NOT EXISTS tasks_follow_up ON tasks(parent_id) WHERE follow_up = 1;
CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS messages_task ON messages(task_id, delivered);
CREATE INDEX IF NOT EXISTS claims_task ON claims(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS items_dedupe ON items(bot, dedupe_key) WHERE status IN ('open','snoozed');
CREATE INDEX IF NOT EXISTS log_project ON log_entries(project, id);
CREATE UNIQUE INDEX IF NOT EXISTS log_dedupe ON log_entries(project, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS log_ref ON log_entries(project, ref, id);
CREATE INDEX IF NOT EXISTS tasks_chain ON tasks(chain_root);
CREATE INDEX IF NOT EXISTS activity_by_project ON activity_log(project, created_at, id);
CREATE INDEX IF NOT EXISTS activity_cursor ON activity_log(project, id);
CREATE INDEX IF NOT EXISTS activity_dedupe ON activity_log(project, bot, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS orch_events_name ON orchestrator_events(name, id);
CREATE INDEX IF NOT EXISTS orch_messages_name ON orchestrator_messages(name, delivered_at, id);
CREATE INDEX IF NOT EXISTS idx_bus_messages_recipient ON bus_messages(recipient, id);
CREATE INDEX IF NOT EXISTS idx_bus_messages_thread ON bus_messages(thread, id);
CREATE INDEX IF NOT EXISTS idx_bus_messages_state ON bus_messages(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_bus_attempts_message ON bus_attempts(message_id, id);
CREATE INDEX IF NOT EXISTS bus_messages_created ON bus_messages(created_at);
CREATE INDEX IF NOT EXISTS bus_attempts_started ON bus_attempts(started_at);
CREATE INDEX IF NOT EXISTS bus_messages_held ON bus_messages(recipient, held, state);
CREATE UNIQUE INDEX IF NOT EXISTS review_items_live ON review_items(project, lower(bot), key) WHERE status IN ('open','changes_requested');
CREATE INDEX IF NOT EXISTS review_items_bot ON review_items(lower(bot), updated_at);
"""

# Bumped when the schema changes. 3 = project log (0.3.0). 4 = typed log records, rules, chains (0.3.0).
# 7 = orchestrators (0.7.0). 8 = agent bus and wake policy (0.8.0).
SCHEMA_VERSION = 8
ACTIVITY_KINDS = ("activity", "shipped", "blocked", "decision")
# Reserved owner for scripts/smoke-test.sh. Finish notices for it are dropped unless a bot with this id is registered.
HEALTHCHECK_OWNER = "healthcheck"
DECIDERS = ("owner", "delegated", "bot")
CROSS_PROJECT = "cross-project"  # swarm-wide entries: any bot may write, only admin and chief read
DEDUPE_WINDOW = 24 * 3600
CENTRAL = ZoneInfo("America/Chicago")
LOG_KINDS = ("activity", "decision", "result", "item", "feedback", "learning", "rule")
ITEM_STATUSES = ("draft", "needs-review", "approved", "changes", "rejected")
VERDICTS = ("approved", "changes", "rejected")
LEARNING_STATUSES = ("pending", "standing")
RULE_STATUSES = ("proposed", "approved", "rejected")
DIGEST_DAYS = 14
STALE_DAYS = 14  # Waiting-on-you items this old are suggested for dropping in the weekly review
ORCH_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
RELAY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,80}$")
ORCH_FIELDS = {"worker_id", "project", "repo", "owner_bot_id", "provider", "model", "brief", "secret_bundles", "relay_agent_name"}
ORCH_STATES = ("stopped", "starting", "running", "restarting", "stopping", "crash-loop", "unreachable", "blocked", "needs-login")
ORCH_EVENT_TYPES = ("start", "stop", "crash", "restart", "crash-loop", "adopt", "relay", "blocked", "supervisor", "needs-login",
                    "first-run", "message-delivered-busy", "delivery-deferred", "delivery-unknown")
ORCH_DELETABLE = ("stopped", "crash-loop", "blocked", "needs-login", "unreachable")   # states with no session to end
# A pending sign-in link in a terminal tail: anyone holding it while it is open can finish the sign-in.
SIGNIN_URL_RE = re.compile(r"https?://\S*(?:oauth|authorize|code_challenge|device|/login)\S*", re.I)
SIGNIN_LINE_RE = re.compile(r"code_challenge|redirect_uri=|response_type=code|[?&]state=", re.I)
ORCH_STALE_SECONDS = 600       # no heartbeat for this long: unreachable, and the owner is told
ORCH_SUPERVISOR_LEASE = 90     # a second supervisor is refused while the first one reported this recently
ORCH_BRIEF_MAX = 20000
ORCH_MESSAGE_MAX = 4000
ORCH_TAIL_MAX = 32 * 1024


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
            # When this database was first opened by a backend that records it (0.2.1 or later).
            db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (str(now()),))
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
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


class Reply:
    """A route result with its own HTTP status (the bus answers 201, 404 with hints, 429, ...)."""
    def __init__(self, status, data):
        self.status = status
        self.data = data


def new_id(prefix="t"):
    return prefix + time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)


def parse_flag(value, field):
    """A yes/no API field: a JSON boolean, 0/1, or the strings true/false/1/0/yes/no. Anything else is a 400, so "false"
    can never switch something on."""
    if value is None or value is False:
        return 0
    if value is True:
        return 1
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "false", "0", "no", ""):
        return 1 if value.strip().lower() in ("true", "1", "yes") else 0
    raise ApiError(400, f"{field} must be true or false")


def clean_text(value, limit, field):
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ApiError(400, f"{field} must be text")
    value = str(value).strip()
    if len(value) > limit:
        raise ApiError(400, f"{field} is too long (max {limit})")
    return value


def activity_text(value, limit, field):
    if value is not None and (not isinstance(value, str) or not value.isprintable()):
        raise ApiError(400, f"{field} must be one plain line")
    return clean_text(value, limit, field) or None


def activity_entry(row):
    stamp = datetime.fromtimestamp(row["created_at"], CENTRAL).strftime("%Y-%m-%d %H:%M CT")
    summary = re.sub(r"([\\\[\]`*_~|])", r"\\\1", html.escape(row["summary"], quote=False))
    kind = row["kind"]
    if kind == "decision":
        kind = f"decision ({row['decider']})"
        summary += f" (#{row['id']})" if row["overrules"] is None else f" (#{row['id']}, overrules #{row['overrules']})"
    return {**{field: value for field, value in row.items() if field != "dedupe_key"},
            "line": f"- {stamp} | {row['bot']} | {kind} | {summary} | {row['link'] or '-'}"}


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


def load_secret_patterns():
    """The same list the runner scans new lines with before a push (config/secret-patterns.txt)."""
    path = os.environ.get("SWARM_SECRET_PATTERNS_FILE") or os.path.join(os.path.dirname(os.path.realpath(__file__)), "..",
                                                                         "config", "secret-patterns.txt")
    pats = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.split("  #", 1)[0].strip()
                if line and not line.startswith("#"):
                    pats.append(re.compile(line))
    except (OSError, re.error):
        pass
    return pats


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
        self.secret_patterns = load_secret_patterns()
        self.bus = swarm_bus.Bus(self, {"valid_notify_url": valid_notify_url, "post_json": post_json, "fingerprint": fingerprint,
                                        "VERSION": VERSION})
        self.review = swarm_review.Review(self)

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
        if tok.startswith("ab_"):
            a = self.bus.agent_from_token(tok)
            if a:
                return {"role": "agent", "bot": None, "agent": a}
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
        if who["role"] == "agent":  # a bus agent reads the tasks its own bot owns
            a = self.bus.owner_agent(task.get("owner"), bus_only=False)
            return bool(a and a["name"] == who["agent"]["name"])
        return who["role"] == "bot" and (task.get("owner") or "").lower() == who["bot"].lower()

    def get_task(self, who, task_id):
        if who["role"] == "task":
            return self.own_task(who, task_id)
        t = self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
        if not t or not self.can_see(who, t):
            raise ApiError(404, "no such task")
        return t

    def get_readable_task(self, who, task_id, kind=None):
        """Like get_task, but a task token may also read the report and artifacts of the parent (or follow-up source)
        of the task it currently holds, even when another worker ran that parent. Never attachments or anything else."""
        if who["role"] == "task" and task_id != who["task"] and kind in ("report", "artifacts"):
            mine = self.own_task(who, who["task"])
            if mine["parent_id"] and mine["parent_id"] == task_id:
                t = self.s.one("SELECT * FROM tasks WHERE id = ?", task_id)
                if t:
                    return t
            raise ApiError(403, "a task token only works for its own task, or its parent's report and artifacts")
        return self.get_task(who, task_id)

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
            "head_sha": task.get("head_sha"), "landed": self.landed(task),
            "report_url": f"{self.public_url}/tasks/{task['id']}/report" if self.public_url else None,
        }
        if unread and task["state"] in TERMINAL:
            p["unread_messages"] = unread
        gl = swarm_gitlink.public_gitlinks(self.stored_gitlinks(task), patterns=self.secret_patterns,
                                           count=task.get("gitlink_skipped_count"))
        if gl["count"]:
            p["gitlink_skipped"], p["gitlink_skipped_count"] = gl["items"], gl["count"]
        if task["state"] == "waiting":
            p["question"] = task.get("question")
        return p

    def set_report_note(self, task_id, note):
        """Make the task's stored REPORT.md carry exactly this (sanitized) gitlink skip note, replacing any generated note
        from an earlier finish; note "" removes it. A task without a report gets one; a report that held only the note
        is removed with it. Detection is by the note's markers, never by its wording."""
        f = self.s.one("SELECT name, data FROM files WHERE task_id = ? AND kind = 'report' ORDER BY at DESC LIMIT 1", task_id)
        text = bytes(f["data"]).decode(errors="replace") if f else ""
        base = swarm_gitlink.strip_report_note(text)
        if base == text and not note:
            return
        name = f["name"] if f else "REPORT.md"
        data = (base.rstrip() + note).lstrip("\n").encode() if note else (base.rstrip() + "\n").lstrip("\n").encode()
        if f and data == bytes(f["data"]):
            return
        if not data.strip():
            self.s.run("DELETE FROM files WHERE task_id = ? AND kind = 'report' AND name = ?", task_id, name)
            return
        self.s.run("INSERT OR REPLACE INTO files(task_id, kind, name, data, size, at) VALUES(?,?,?,?,?,?)",
                   task_id, "report", name, data, len(data), now())

    @staticmethod
    def stored_gitlinks(t):
        try:
            v = json.loads(t.get("gitlink_skipped") or "[]")
        except (TypeError, ValueError):
            return []
        return v if isinstance(v, list) else []

    @staticmethod
    def cannot_push(task):
        """Tasks that can never publish are exempt from the landed rules: report-only, untrusted input, reviews and scripts."""
        return bool(task.get("report_only") or task.get("untrusted") or task.get("kind") in ("script", "review"))

    @staticmethod
    def has_push_evidence(task):
        """A pushed branch counts only when the current claim's finish reported it. A branch left over from an earlier claim says
        nothing about the current head, unless nothing is unpushed. pushed_current NULL (rows from before this field existed) is treated
        like 0: with known unpushed commits (a positive count) it is not evidence; a missing or zero count keeps the old trust."""
        if not task.get("pushed_branch"):
            return False
        unpushed = task.get("unpushed_commits")
        return task.get("pushed_current") == 1 or not (isinstance(unpushed, int) and unpushed > 0)

    def landed(self, task):
        """True: on GitHub (and the PR confirmed when one was asked for). False: known not to be. None: unknown (old worker) or not applicable."""
        if self.has_push_evidence(task) and task.get("kind") == "code":
            return bool(not task.get("open_pr") or task.get("pr_url"))
        if (task.get("unpushed_commits") or 0) > 0 and task.get("kind") == "code" and not self.cannot_push(task):
            return False
        return None

    def unlanded_detail(self, t, worker):
        """Done means landed (code tasks): commits only on the worker, or a requested PR never confirmed, is not done. Inert for an
        old worker (no unpushed_commits field), ops and review tasks and tasks without commits."""
        if t.get("kind") != "code" or self.cannot_push(t):
            return None
        if (t.get("unpushed_commits") or 0) > 0 and not self.has_push_evidence(t):
            return (f"Work is committed on {worker or 'the worker'} but NOT on GitHub: {t['unpushed_commits']} commit(s) not pushed. "
                    "Re-queue with --push or --pr, or recover the branch.")
        if t.get("open_pr") and self.has_push_evidence(t) and not t.get("pr_url"):
            return f"Pushed {t['pushed_branch']} but no PR was confirmed on GitHub."
        return None

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

    def is_quiet_healthcheck(self, task):
        """A smoke-test task whose owner is not a registered bot or a bus agent name, alias or bot id: nobody owns its notices."""
        if (task.get("owner") or "").lower() != HEALTHCHECK_OWNER or self.bot(HEALTHCHECK_OWNER):
            return False
        try:
            return not self.bus.owner_agent(task["owner"], bus_only=False)
        except Exception:
            return False  # lookup failed: keep the normal routing

    def notify_task(self, task, no_bus=False):
        """Route a task event: owner bot first, Chief of Staff only for ownerless, waiting-on-human or fallback.
        An owner that resolves to a bus agent with notices=bus gets the notice on the bus only (no bot webhook, no Chief copy);
        if the bus send fails, the normal path below runs, Chief copy included."""
        if task["state"] not in NOTIFY_STATES:
            return
        if self.is_quiet_healthcheck(task):
            return  # the smoke test's own task: nobody to tell, and no stray notice to the Chief of Staff
        seq = self.s.one("SELECT COUNT(*) AS n FROM events WHERE task_id = ? AND type = 'state' AND text = ?",
                         task["id"], task["state"])["n"]
        event_id = f"{task['id']}:{task['state']}:{seq}"
        if not no_bus and task.get("owner"):
            agent = None
            try:
                agent = self.bus.owner_agent(task["owner"])
            except Exception as e:
                print("bus owner lookup failed:", repr(e), file=sys.stderr, flush=True)
            if agent and self.bus.send_notice(agent, task, dict(self.payload(task, event_id, "owner"), notify_target="bus")):
                return
            if agent and task["state"] == "cancelled":
                return  # an informational cancellation is never worth the legacy fallback wake
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
        b = {k: v for k, v in b.items() if not str(k).startswith("_")}  # internal fields are never taken from a request
        if "allow_gitlink" in b and b["allow_gitlink"] is None:
            raise ApiError(400, "allow_gitlink must be true or false; omit it to inherit a follow-up's parent value")
        follow = clean_text(b.get("follow_up"), 80, "follow_up")
        if follow:
            return self.follow_up(who, follow, b, public=True)
        return self._create(who, b, public=True)

    def _create(self, who, b, follow_up=False, held_behind=None, public=False):
        prompt = clean_text(b.get("prompt"), 200_000, "prompt")
        command = clean_text(b.get("command"), 4000, "command")
        if not prompt:
            raise ApiError(400, "prompt is required (for a script task, say what the command is for)")
        owner = clean_text(b.get("owner"), 80, "owner") or who["bot"]
        if public and not owner:
            # Only the public path asks for an owner. Chains and other server-created tasks go through follow_up(public=False).
            raise ApiError(400, "owner is required: pass `--owner <bot id or alias>` (JSON field `owner`). A follow-up or child "
                                "inherits its parent's owner. The kit has no bot alias table; an owner is a bot id or a bus agent name/alias")
        if who["role"] == "bot" and owner and owner.lower() != who["bot"].lower():
            raise ApiError(403, "a project bot can only queue tasks it owns")
        project = clean_text(b.get("project"), 80, "project")
        target_worker = clean_text(b.get("target_worker"), 80, "target_worker")
        if target_worker and not SAFE_NAME.fullmatch(target_worker):
            raise ApiError(400, "target_worker must be a worker name (letters, digits, dot, dash or underscore)")
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
        allow_gitlink = parse_flag(b.get("allow_gitlink"), "allow_gitlink")
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
        if push_main and repo not in self.project_repos(proj):
            raise ApiError(403, "push-main needs a repo listed on the project")
        branch = clean_text(b.get("branch"), 200, "branch")
        if branch and not BRANCH_RE.match(branch):
            raise ApiError(400, "bad branch name")
        base = self.clean_base(b.get("base"))
        if base and not repo:
            raise ApiError(400, "base needs a repo")
        kind = clean_text(b.get("kind"), 20, "kind") or ("script" if command else "code" if repo and not report_only else "research")
        timeout, chain, max_cycles = self.run_limits(b, kind, command)
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
                 "project": project, "parent_id": parent_id, "follow_up": follow_up, "timeout": timeout, "chain": chain,
                 "max_cycles": max_cycles, "cycle": b.get("_cycle"), "attachments": sorted((n, hashlib.sha256(d).hexdigest()) for n, d in decoded)}
        if target_worker:
            canon["target_worker"] = target_worker
        if allow_gitlink:
            canon["allow_gitlink"] = 1
        if base:
            canon["base"] = base
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
                    " idempotency_key, state, max_attempts, parent_id, follow_up, held_behind, push_main, workspace_host,"
                    " timeout, chain, chain_root, cycle, max_cycles, target_worker, allow_gitlink, base)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tid, t, t, project, owner, kind, clean_text(b.get("title"), 200, "title"), prompt, phash, repo,
                    branch, int(pr_number) if pr_number is not None else None,
                    provider, model, command, report_only, push, open_pr, 1 if b.get("ask_human") else 0, untrusted,
                    idem, "queued", min(int(b.get("max_attempts") or self.max_attempts), 50), parent_id,
                    1 if follow_up else 0, held_behind, push_main, b.get("_workspace_host"),
                    timeout, chain, (b.get("_chain_root") or tid) if chain else None, int(b.get("_cycle") or 1) if chain else 0,
                    max_cycles, target_worker, allow_gitlink, base)
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

    @staticmethod
    def run_limits(b, kind, command=None):
        """Per-task timeout (bounded by the admin maximum) and chain settings. See docs/long-running-jobs.md."""
        tmax = env_int("SWARM_TASK_TIMEOUT_MAX", 86400)
        timeout = None
        if b.get("timeout_minutes") not in (None, ""):
            try:
                timeout = int(float(b["timeout_minutes"]) * 60)
            except (TypeError, ValueError):
                raise ApiError(400, "timeout_minutes must be a number")
            if timeout < 60:
                raise ApiError(400, "timeout_minutes must be at least 1")
            if timeout > tmax:
                raise ApiError(400, f"timeout_minutes is above the admin maximum of {tmax // 60} minutes. "
                                    "The owner can raise SWARM_TASK_TIMEOUT_MAX on the worker.")
        elif kind == "long-running":
            timeout = tmax
        chain = 1 if b.get("chain") else 0
        if chain and command:
            raise ApiError(400, "a script task (command) cannot be a chain: a script never writes a checkpoint, so it would "
                                "run once and stop. Use a model task for a chain, or a script with --timeout-minutes.")
        if chain and kind == "long-running":
            raise ApiError(400, "a task is either a chain or long-running, not both. Chains are the default; see docs/long-running-jobs.md")
        max_cycles = None
        if chain:
            cap = env_int("SWARM_MAX_CYCLES", 200)
            raw = b.get("max_cycles")
            try:
                # 0 is refused, not read as "use the default": a chain always has a real limit.
                max_cycles = 20 if raw is None or raw == "" else int(raw)
            except (TypeError, ValueError):
                raise ApiError(400, "max_cycles must be a number")
            if not 1 <= max_cycles <= cap:
                raise ApiError(400, f"max_cycles must be between 1 and {cap}. There is no unlimited chain; "
                                    "a cycle that leaves no checkpoint ends the chain early.")
        elif b.get("max_cycles") not in (None, ""):
            raise ApiError(400, "max_cycles needs chain")
        return timeout, chain, max_cycles

    def next_cycle(self, t, checkpoint):
        """Queue a chain's next cycle as a follow-up with the checkpoint attached. Returns (task id or None, note)."""
        if t["cycle"] >= (t["max_cycles"] or 1):
            return None, f"Chain stopped: it reached its limit of {t['max_cycles']} cycles. The last checkpoint is saved on {t['id']}."
        owner = self.bot(t["owner"]) if t["owner"] else None
        who = {"role": "bot", "bot": owner["id"]} if owner and owner["role"] != "chief" else {"role": "admin", "bot": None}
        n = t["cycle"] + 1
        body = {"prompt": t["prompt"], "title": t["title"], "chain": True, "max_cycles": t["max_cycles"],
                "timeout_minutes": t["timeout"] / 60 if t["timeout"] else None, "_cycle": n, "_chain_root": t["chain_root"],
                "idempotency_key": f"{t['chain_root']}:cycle:{n}",
                "attachments": [{"name": "checkpoint.md", "content_base64": base64.b64encode(checkpoint.encode()).decode()}]}
        try:
            r = self.follow_up(who, t["id"], body)
        except ApiError as e:
            return None, f"Chain stopped: cycle {n} could not be queued ({e.message})."
        return r["task"]["id"], f"Next: cycle {n} queued as {r['task']['id']}."

    def follow_up(self, who, parent_id, b, public=False):
        """--follow-up: continue earlier work. See docs/concepts.md for the rules."""
        parent = self.get_task(who, parent_id)
        st = parent["state"]
        if st in ("queued", "running"):
            raise ApiError(409, f"task {parent_id} is still {st}. Send it a message instead: swarmctl message {parent_id} \"...\"",
                           {"task_id": parent_id})
        if st == "waiting":
            if b.get("target_worker") and b["target_worker"] != parent.get("target_worker"):
                raise ApiError(400, "a follow-up to a waiting task answers it; it cannot change the worker pin")
            for flag in ("push", "open_pr", "push_main"):
                if b.get(flag) and not parent.get(flag):
                    raise ApiError(400, f"task {parent_id} is waiting for an answer. A follow-up answers it and cannot add "
                                        f"{flag} when the task did not have it")
            # --base is not a publish permission: it is kept on the resumed task, written with the answer itself.
            r = self.answer(who, parent_id, {"answer": b.get("prompt"), "base": b.get("base")}, via="follow-up")
            r.update(answered_parent=True, duplicate=False)
            return r
        inherit = {k: parent[k] for k in ("project", "repo", "provider", "kind", "pr_number", "push", "open_pr", "push_main",
                                          "report_only", "untrusted", "ask_human", "owner", "target_worker", "allow_gitlink", "base")}
        inherit["model"] = parent.get("session_model") or parent.get("model")
        if parent.get("command"):
            inherit["command"] = parent["command"]
        if parent.get("repo") and not parent.get("report_only"):
            inherit["branch"] = parent.get("pushed_branch") or parent.get("branch") or f"agent/{parent['id']}"
        body = dict(inherit)
        body.update({k: v for k, v in b.items() if v is not None and v is not False and v != "" and k != "follow_up"})
        if "allow_gitlink" in b:
            # an explicit value, JSON false included, overrides the parent's opt-in; only an omitted field inherits it
            # (an explicit null was refused in create_task)
            body["allow_gitlink"] = parse_flag(b["allow_gitlink"], "allow_gitlink")
        body["parent_id"] = parent["id"]
        body["_workspace_host"] = parent.get("workspace_host")
        held = self.s.one("SELECT id FROM tasks WHERE parent_id = ? AND follow_up = 1 AND state NOT IN ('done','failed','cancelled')"
                          " ORDER BY created_at DESC LIMIT 1", parent["id"])
        return self._create(who, body, follow_up=True, held_behind=held["id"] if held else None, public=public)

    def public_task(self, t, events=False):
        out = {k: v for k, v in t.items() if k not in ("prompt_hash", "session_id")}
        out["gitlink_skipped"] = self.stored_gitlinks(t)
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

    def answer(self, who, task_id, b, via="answer", log=True):
        self.need(who, "admin", "chief", "bot")
        text = clean_text(b.get("answer") or b.get("text"), 20000, "answer")
        if not text:
            raise ApiError(400, "answer is required")
        base = self.clean_base(b.get("base"))
        actor = who["bot"] or who["role"]
        with self.s.tx():
            t = self.get_task(who, task_id)
            if t["state"] != "waiting":
                raise ApiError(409, f"task is {t['state']}, not waiting")
            if t["waiting_on"] == "human" and who["role"] == "bot":
                raise ApiError(409, "this task waits on the human. A bot cannot answer for them. Ask them, then the Chief of Staff records the answer.")
            self.event(task_id, "answer", actor, text, {"via": via})
            if log and t["waiting_on"] == "human":
                self.log_decision(t["project"], t["owner"], t["question"], text, task_id,
                                  f"task:{task_id}:answer:{t['waiting_since'] or t['updated_at']}", actor, via)
            self.s.run("UPDATE items SET status = 'answered', answer = ?, updated_at = ? WHERE task_id = ? AND status IN ('open','snoozed')",
                       text, now(), task_id)
            extra = {}
            if t["outcome"] == "work_unavailable":
                # The worker asked because unpushed work was lost. Any answer means: start again from what was pushed.
                extra = {"unpushed": 0, "start_fresh": 1}
            if base and base != t.get("base"):
                extra["base"] = base  # only with an accepted answer: every refusal above raises before this
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
            if t["state"] == "failed" and t["failure_resolved_at"] is None:
                self.s.run("UPDATE tasks SET failure_resolved_at = ?, updated_at = ? WHERE id = ?", now(), now(), task_id)
                self.event(task_id, "failure_resolved", who["bot"] or who["role"], note or "failure dismissed")
                t = self.get_task(who, task_id)
            if t["chain_root"]:
                return self.cancel_chain(who, t, note)
            return self.cancel_one(who, t, note)

    def cancel_chain(self, who, t, note=None):
        """Cancelling any task of a chain, even a finished cycle, cancels every cycle still queued, waiting or running."""
        live = self.s.q("SELECT * FROM tasks WHERE chain_root = ? AND state NOT IN ('done','failed','cancelled') ORDER BY created_at",
                        t["chain_root"])
        outs = [self.cancel_one(who, x, note) for x in live]
        if live:
            actor = who["bot"] or who["role"]
            text = f"Chain {t['chain_root']} cancelled at cycle {max(x['cycle'] for x in live)} by {actor}."
            self.event(t["chain_root"], "chain", actor, text)
            self.add_log(t["project"], t["owner"], "activity", text, note, (), live[-1]["id"], f"chain:{t['chain_root']}:cancelled")
        out = {"task": self.public_task(self.s.one("SELECT * FROM tasks WHERE id = ?", t["id"])),
               "chain_cancelled": [x["id"] for x in live]}
        if any(o.get("note") for o in outs):
            out["note"] = next(o["note"] for o in outs if o.get("note"))
        return out

    def cancel_one(self, who, t, note=None):
        task_id = t["id"]
        with self.s.tx():
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

    def primary_gate(self, claimer):
        """None when the claimer may take any unpinned task: SWARM_PRIMARY_WORKER is unset or empty (first-come), the
        claimer is the primary, or the primary is stale (never seen, draining, or quiet for SWARM_PRIMARY_STALE_SECONDS).
        Otherwise the primary's allowlist and the queued-before cutoff for the claim query. Liveness is workers.last_seen,
        written on every claim and task heartbeat. Call inside the claim transaction."""
        primary = (os.environ.get("SWARM_PRIMARY_WORKER") or "").strip()
        if not primary or primary == claimer:
            return None
        row = self.s.one("SELECT last_seen, draining, projects FROM workers WHERE name = ?", primary)
        if not row or not row["last_seen"] or row["draining"] or now() - row["last_seen"] > env_int("SWARM_PRIMARY_STALE_SECONDS", 90):
            return None
        return {"queued_before": now() - env_int("SWARM_FALLBACK_AFTER_SECONDS", 120),
                "primary_all": 1 if row["projects"] is None else 0,
                "primary_list": row["projects"] if row["projects"] is not None else "[]"}

    def claim(self, who, b):
        self.need(who, "worker")
        worker = clean_text(b.get("worker"), 80, "worker") or "worker"
        projects = b.get("projects")
        if projects is not None and (not isinstance(projects, list) or len(projects) > 100 or
                                     any(not isinstance(project, str) or not project.strip() or len(project) > 80
                                         for project in projects)):
            raise ApiError(400, "projects must be a list of up to 100 project names")
        draining = bool(b.get("draining"))
        with self.s.tx():
            self.seen(worker, draining)
            # Kept so an orchestrator can only be pinned to a worker whose allowlist takes its project.
            self.s.run("UPDATE workers SET projects = ? WHERE name = ?", json.dumps(projects) if projects is not None else None, worker)
            if draining or projects == []:
                return None
            sql = ("SELECT * FROM tasks t WHERE t.state = 'queued' AND t.not_before <= ?"
                   " AND (t.target_worker IS NULL OR t.target_worker = ?)"
                   " AND NOT EXISTS (SELECT 1 FROM tasks h WHERE h.id = t.held_behind"
                   " AND h.state NOT IN ('done','failed','cancelled'))"
                   " AND (t.target_worker IS NOT NULL OR NOT EXISTS (SELECT 1 FROM workers w"
                   " WHERE w.name = t.workspace_host AND w.name != ? AND w.last_seen > ? AND w.draining = 0))")
            args = [now(), worker, worker, now() - 3600]
            gate = self.primary_gate(worker)
            if gate:
                # Primary/fallback routing (agent-control PR 68, docs/multi-worker.md): while SWARM_PRIMARY_WORKER is live,
                # another worker takes an unpinned task only if it has waited SWARM_FALLBACK_AFTER_SECONDS (counted from the
                # later of creation or not_before, or from the requeue/answer time once it has run), the project is off the
                # primary's allowlist, or the task's workspace is on this worker.
                sql += (" AND (t.target_worker IS NOT NULL OR t.workspace_host IS ?"
                        " OR max(CASE WHEN t.attempts = 0 THEN t.created_at ELSE t.updated_at END, t.not_before) <= ?"
                        " OR (? = 0 AND t.project NOT IN (SELECT value FROM json_each(?))))")
                args.extend([worker, gate["queued_before"], gate["primary_all"], gate["primary_list"]])
            if projects is not None:
                sql += " AND t.project IN (" + ",".join("?" for project in projects) + ")"
                args.extend(projects)
            t = self.s.one(sql + " ORDER BY t.created_at, t.rowid LIMIT 1", *args)
            if not t:
                return None
            attempts = t["attempts"] + 1
            claim_id = "c" + secrets.token_hex(8)
            tok = "swt_" + secrets.token_urlsafe(32)
            self.s.run("UPDATE tasks SET state = 'running', attempts = ?, lease_worker = ?, lease_until = ?, updated_at = ?,"
                       " claim_id = ?, claimed_at = ?, cancel_requested = 0 WHERE id = ?",
                       attempts, worker, now() + LEASE_SECONDS, now(), claim_id, now(), t["id"])
            self.s.run("INSERT INTO claims(claim_id, task_id, token_hash, worker, created_at, expires_at) VALUES(?,?,?,?,?,?)",
                       claim_id, t["id"], token_hash(tok), worker, now(), now() + max(self.token_max_age, (t["timeout"] or 0) + 3600))
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
                                                   "session_id", "pushed_branch", "branch", "unpushed", "repo", "pr_url", "base")}
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
        elif t["push_main"] and t["repo"] not in self.project_repos(proj):
            allowed, reason = False, "this repo is no longer listed on the project"
        return {"allowed": allowed, "reason": reason, "repo": t["repo"], "branch": t["branch"], "push": bool(t["push"]),
                "open_pr": bool(t["open_pr"]), "push_main": bool(t["push_main"]), "pr_number": t["pr_number"]}

    @staticmethod
    def clean_base(value):
        base = clean_text(value, 200, "base")
        if base and not BRANCH_RE.match(base):
            raise ApiError(400, "bad base branch name")
        return base

    def finish_pr_url(self, t, b, actor):
        """The pr_url to store. An explicit null clears it (the worker withdrew an earlier claim's PR: the target was merged or
        closed and no new PR was opened); an absent one keeps the stored value. open_pr=0 is an absolute veto on new PRs: a URL
        is accepted from such a task only when it is the PR it was told to update (--pr-number, or the parent's PR it
        inherited as a follow-up)."""
        if "pr_url" in b and b["pr_url"] is None:
            return None
        url = clean_text(b.get("pr_url"), 500, "pr_url")
        if not url:
            return t["pr_url"]
        if not t["open_pr"] and url != t["pr_url"]:
            targets = []
            if t["pr_number"] and t["repo"]:
                targets.append(f"https://github.com/{t['repo']}/pull/{t['pr_number']}")
            if t["parent_id"] and t["follow_up"]:
                p = self.s.one("SELECT pr_url FROM tasks WHERE id = ?", t["parent_id"])
                if p and p["pr_url"]:
                    targets.append(p["pr_url"])
            if url.rstrip("/").lower() not in [x.rstrip("/").lower() for x in targets]:
                self.event(t["id"], "warning", actor, f"PR {url} was not recorded: this task did not allow opening a PR (open_pr not set)")
                return t["pr_url"]
        return url

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
            pr_url = self.finish_pr_url(t, b, actor)
            common = dict(summary=clean_text(b.get("summary"), 2000, "summary") or t["summary"],
                          pr_url=pr_url,
                          session_provider=clean_text(b.get("session_provider"), 20, "session_provider") or t["session_provider"],
                          session_model=clean_text(b.get("session_model"), 80, "session_model") or t["session_model"],
                          session_id=clean_text(b.get("session_id"), 200, "session_id") or t["session_id"],
                          workdir=clean_text(b.get("workdir"), 120, "workdir") or t["workdir"],
                          pushed_branch=clean_text(b.get("pushed_branch"), 200, "pushed_branch") or t["pushed_branch"],
                          pushed_current=1 if clean_text(b.get("pushed_branch"), 200, "pushed_branch") else 0,
                          unpushed=1 if b.get("unpushed") else 0,
                          head_sha=clean_text(b.get("head_sha"), 64, "head_sha") or t["head_sha"],
                          unpushed_commits=b["unpushed_commits"] if isinstance(b.get("unpushed_commits"), int)
                          and not isinstance(b.get("unpushed_commits"), bool) and b["unpushed_commits"] >= 0 else t["unpushed_commits"],
                          workspace_host=c["worker"] if (b.get("workdir") or t["workdir"]) else t["workspace_host"],
                          pending_answer=None)
            skipped = b.get("gitlink_skipped")
            if isinstance(skipped, list):
                # An explicit observation ([] included) replaces the earlier one: it describes the worktree now. Omitting
                # the field keeps what was stored. Redact on the full value (then cut) before anything is stored: the task
                # record, the event, the report note and every notice see only this. The true count is kept apart from the
                # bounded list, never re-derived from display strings. A replayed finish returns above, so this runs once.
                gl = swarm_gitlink.public_gitlinks(skipped, limit=swarm_gitlink.MAX_STORED, patterns=self.secret_patterns,
                                                   count=b.get("gitlink_skipped_count"))
                if gl["count"]:
                    common["gitlink_skipped"] = json.dumps(gl["items"])
                    common["gitlink_skipped_count"] = gl["count"]
                    shown = gl["items"][:swarm_gitlink.MAX_ITEMS]
                    self.event(task_id, "publish", actor, "Auto-commit " + swarm_gitlink.format_note(shown, gl["count"]))
                    self.set_report_note(task_id, swarm_gitlink.report_note(shown, gl["count"]))
                else:
                    if t.get("gitlink_skipped_count"):
                        self.event(task_id, "publish", actor, "Auto-commit: no submodule pin changes left uncommitted now")
                    common["gitlink_skipped"], common["gitlink_skipped_count"] = None, None
                    self.set_report_note(task_id, "")
            elif t.get("gitlink_skipped_count"):
                # Field omitted: the stored observation still stands, and the current report must say so. A resumed claim
                # may have uploaded a fresh REPORT.md (without the note) just before this finish; render the stored note
                # into it. set_report_note replaces any earlier generated note, so this never duplicates it.
                gl = swarm_gitlink.public_gitlinks(self.stored_gitlinks(t), limit=swarm_gitlink.MAX_STORED,
                                                   patterns=self.secret_patterns, count=t["gitlink_skipped_count"])
                if gl["count"]:
                    self.set_report_note(task_id, swarm_gitlink.report_note(gl["items"][:swarm_gitlink.MAX_ITEMS], gl["count"]))
            if "session_id" in b and b["session_id"] is None and b.get("session_reset"):
                common["session_id"] = None
            for w in (b.get("warnings") or [])[:20]:
                self.event(task_id, "warning", actor, clean_text(str(w), 2000, "warning"))
            for key in ("pr_base_note", "publish_note"):
                if b.get(key):
                    tps = clean_text(b.get("target_pr_state"), 20, "target_pr_state")
                    self.event(task_id, "publish", actor, clean_text(str(b[key]), 4000, key),
                               {"target_pr_state": tps} if tps and key == "publish_note" else None)
            checkpoint = clean_text(b.get("checkpoint"), 200_000, "checkpoint") if t["chain"] and status == "done" else None
            if status == "done":
                # A chain cycle that hands on a checkpoint is not news; only the chain's end notifies the owner.
                quiet = bool(checkpoint) and t["cycle"] < (t["max_cycles"] or 0)
                unlanded = self.unlanded_detail(dict(t, **common), actor)
                if unlanded and not quiet:
                    # The agent's own "done" would read as success; lead with why it is not done.
                    q = unlanded + (f"\nAgent's report: {common['summary'][:500]}" if common.get("summary") else "")
                    t = self.set_state(t, "waiting", actor, outcome="blocked", question=q, waiting_on="owner", **common)
                else:
                    t = self.set_state(t, "done", actor, notify=not quiet, outcome="done", error=None, **common)
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
            chain_note = None
            if t["chain"] and t["state"] in ("done", "failed"):
                if checkpoint:
                    self.s.run("INSERT OR REPLACE INTO files(task_id, kind, name, data, size, at) VALUES(?,?,?,?,?,?)",
                               t["id"], "artifact", "checkpoint.md", checkpoint.encode(), len(checkpoint.encode()), now())
                if t["state"] == "failed":
                    chain_note = f"Chain stopped: cycle {t['cycle']} failed. See docs/long-running-jobs.md to restart it."
                elif not checkpoint:
                    chain_note = f"Chain finished: cycle {t['cycle']} left no checkpoint."
                else:
                    nxt, chain_note = self.next_cycle(t, checkpoint)
                    if not nxt and quiet:
                        self.notify_task(t)
                self.event(t["id"], "chain", "backend", chain_note)
            if t["state"] in ("done", "failed"):
                self.log_result(t, chain_note)
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
        self.escalate_failures()

    def escalate_failures(self):
        unresolved = ("t.state = 'failed' AND t.failure_escalated_at IS NULL AND t.failure_resolved_at IS NULL"
                      " AND NOT EXISTS (SELECT 1 FROM tasks f WHERE f.parent_id = t.id AND f.follow_up = 1"
                      " AND f.state != 'cancelled')")
        failed = self.s.q(f"SELECT t.id, t.owner FROM tasks t WHERE {unresolved} ORDER BY t.finished_at LIMIT 100")
        for task in failed:
            with self.s.tx():
                marked = self.s.run(f"UPDATE tasks AS t SET failure_escalated_at = ? WHERE t.id = ? AND {unresolved}",
                                    now(), task["id"]).rowcount
            if not marked or self.is_quiet_healthcheck(task):
                continue  # a failed health check is marked handled but never alerts the Chief
            try:
                self.alert(f"task-failed:{task['id']}",
                           f"Task {task['id']} failed and has no follow-up. Review its report or queue a follow-up.",
                           "supervisor")
            except Exception:
                print(f"failure escalation could not be queued for task {task['id']}", file=sys.stderr, flush=True)

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
            default = env_int("SWARM_TASK_TIMEOUT", 14400)
            for t in self.s.q("SELECT id, claim_id, claimed_at, timeout FROM tasks WHERE state = 'running'"):
                if (t["claimed_at"] or now()) >= now() - (t["timeout"] or default) - 1800:
                    continue
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
            # Throwaway items are the one thing that leaves the log: every entry of an expired item goes.
            self.s.run("DELETE FROM log_entries WHERE kind = 'item' AND expires_at IS NOT NULL AND expires_at < ?", now())
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
            cur = self.project(name) or {"allow_push": 1, "allow_push_main": 0, "log_git": 0, "bots": [], "repos": []}
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
            log_git = 1 if b.get("log_git", cur.get("log_git")) else 0
            self.s.run("INSERT OR REPLACE INTO projects(name, default_repo, default_provider, default_model, allow_push, updated_at,"
                       " bots, repos, allow_push_main, log_git) VALUES(?,?,?,?,?,?,?,?,?,?)", name, vals["default_repo"],
                       vals["default_provider"], vals["default_model"], allow, now(), json.dumps(bots), json.dumps(repos), allow_main, log_git)
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
        for i in items:
            i["age_days"] = max(0, (now() - (i.get("created_at") or now())) // 86400)
        open_items = [i for i in items if i.get("status") == "open"]
        counts = {"open": len(open_items), "snoozed": sum(1 for i in items if i.get("status") == "snoozed"),
                  "oldest_days": max((i["age_days"] for i in open_items), default=0),
                  "stale": sum(1 for i in open_items if i["age_days"] >= STALE_DAYS), "stale_days": STALE_DAYS}
        return {"items": items, "counts": counts}

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
            self.log_decision(item["project"], item["bot"], item["ask"], ans, item["task_id"],
                              f"item:{item_id}:answer:{item['revision']}", who["bot"] or who["role"])
            if linked and linked["state"] == "waiting":
                self.answer(who, linked["id"], {"answer": ans}, via="waiting-list", log=False)
        elif action == "snooze":
            until = int(b.get("until") or (t + 86400))
            self.s.run("UPDATE items SET status='snoozed', snooze_until=?, updated_at=? WHERE id=?", until, t, item_id)
        elif action == "drop":
            note = clean_text(b.get("note"), 2000, "note") or "Don't need it"
            self.s.run("UPDATE items SET status='dropped', answer=?, updated_at=? WHERE id=?", note, t, item_id)
            self.log_decision(item["project"], item["bot"], item["ask"], "Dropped: " + note, item["task_id"],
                              f"item:{item_id}:drop:{item['revision']}", who["bot"] or who["role"])
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

    # ---------- project log ----------
    # One append-only, typed log per project. Records that change over time (items, learnings, rules) are a run
    # of entries sharing a stable `ref`; the newest entry is the current state. Nothing is edited in place.
    REC_COLS = ("ref", "status", "version", "author", "source", "answer", "before_text", "after_text", "expires_at", "about")

    def add_log(self, project, bot, kind, summary, detail=None, links=(), task_id=None, dedupe_key=None, **rec):
        """Append one entry. With a dedupe_key, a repeat of the same key is ignored (auto-entries stay single)."""
        if not project:
            return None
        cols = ["project", "bot", "kind", "summary", "detail", "links", "task_id", "dedupe_key", "created_at"]
        vals = [project, bot, kind, summary[:500], (detail or None) and detail[:20000],
                json.dumps([str(x)[:2000] for x in links if x][:20]), task_id, dedupe_key, now()]
        for k in self.REC_COLS:
            if rec.get(k) is not None:
                cols.append(k)
                vals.append(rec[k])
        cur = self.s.run(f"INSERT OR IGNORE INTO log_entries({', '.join(cols)}) VALUES({','.join('?' * len(cols))})", *vals)
        return cur.lastrowid if cur.rowcount else None

    def next_log_id(self):
        """The id the next entry gets (call inside a transaction). Used to give a new record a stable ref."""
        r = self.s.one("SELECT seq FROM sqlite_sequence WHERE name = 'log_entries'")
        return (r["seq"] if r else 0) + 1

    def log_result(self, t, chain_note=None):
        title = next((ln.strip() for ln in (t.get("title") or t["prompt"]).splitlines() if ln.strip()), "")[:200]
        word = "Done" if t["state"] == "done" else "Failed"
        if t.get("chain"):
            word = f"Cycle {t['cycle']} {word.lower()}"
        detail = t["summary"] if t["state"] == "done" else (t["error"] or t["summary"])
        if chain_note:
            detail = ((detail or "") + "\n" + chain_note).strip()
        links = [t["pr_url"], f"{self.public_url}/tasks/{t['id']}/report" if self.public_url else None]
        self.add_log(t["project"], t["owner"], "result", f"{word}: {title}", detail, links, t["id"], f"task:{t['id']}:result")

    def log_decision(self, project, bot, ask, answer, task_id, key, actor, source="waiting-list"):
        ask = " ".join((ask or "").split())
        self.add_log(project, bot, "decision", f"Decided: {ask[:200]}", f"Answer: {answer}\nRecorded by: {actor}",
                     (), task_id, key, author=actor, answer=(answer or "")[:4000], source=source)

    def log_projects(self, who):
        """Projects this caller may read and write in the log. None means all."""
        if who["role"] in ("admin", "chief"):
            return None
        if who["role"] == "task":
            t = self.s.one("SELECT project FROM tasks WHERE id = ?", who["task"])
            return [t["project"]] if t and t["project"] else []
        return [p["name"] for p in self.bound_projects(who["bot"])]

    def check_log_project(self, who, project):
        allowed = self.log_projects(who)
        if allowed is not None and project.lower() not in (p.lower() for p in allowed):
            raise ApiError(404, "no such project log")

    def activity_project(self, who, project, write=False):
        if project == CROSS_PROJECT:
            if who["role"] in ("admin", "chief") or (write and who["role"] == "bot"):
                return project
            raise ApiError(404, "no such project activity log")
        if not isinstance(project, str) or not ID_RE.fullmatch(project):
            raise ApiError(400, f"project must be a registered project name or {CROSS_PROJECT}")
        if not self.project(project) or (who["role"] == "bot" and
                                        not any(p["name"] == project for p in self.bound_projects(who["bot"]))):
            raise ApiError(404, "no such project activity log")
        return project

    def post_activity(self, who, body):
        task = self.own_task(who, who["task"]) if who["role"] == "task" else None
        if task and (task["untrusted"] or "Activity log: yes" not in task["prompt"].splitlines() or
                     body.get("project") not in (None, task["project"])):
            raise ApiError(403, "a trusted task brief must opt in, and a task can only write to its own project")
        project = self.activity_project(who, task["project"] if task else body.get("project"), write=True)
        kind = body.get("kind")
        if kind not in ACTIVITY_KINDS:
            raise ApiError(400, "kind must be one of: " + ", ".join(ACTIVITY_KINDS))
        if task and kind == "decision":
            raise ApiError(403, "a worker task can log activity, shipped or blocked, not decisions")
        decider = body.get("decider")
        if (decider is not None) != (kind == "decision") or (decider is not None and decider not in DECIDERS):
            raise ApiError(400, "a decision needs a decider (" + ", ".join(DECIDERS) + "), and only a decision has one")
        if decider in ("owner", "delegated") and who["role"] not in ("chief", "admin"):
            raise ApiError(403, "only the Chief of Staff or admin can record owner or delegated decisions")
        if decider == "delegated" and who["role"] != "chief":
            raise ApiError(403, "only the Chief of Staff makes delegated decisions")
        summary = activity_text(body.get("summary"), 600, "summary")
        if not summary:
            raise ApiError(400, "summary is required")
        link = activity_text(body.get("link"), 500, "link")
        if link:
            try:
                url = urllib.parse.urlsplit(link)
                valid = url.scheme == "https" and url.hostname and not url.username and url.password is None and "@" not in url.netloc
            except ValueError:
                valid = False
            if not valid or any(char in '?<>"\\|' or char.isspace() for char in link):
                raise ApiError(400, "link must be an HTTPS URL without credentials or a query string")
        key = (f"task:{task['id']}:{who['claim']}" if task else activity_text(body.get("dedupe_key"), 120, "dedupe_key"))
        lasting = body.get("lasting", False)
        if not isinstance(lasting, bool) or (lasting and kind != "decision"):
            raise ApiError(400, "lasting must be a boolean and only decisions may set it to true")
        overrules = body.get("overrules")
        if overrules is not None and (kind != "decision" or type(overrules) is not int or not 0 < overrules < 2**63):
            raise ApiError(400, "only a decision may overrule, and overrules must be an entry ID")
        bot = (task["owner"] if ID_RE.fullmatch(task["owner"] or "") else "worker") if task else who["bot"] or "admin"
        with self.s.tx():
            if task:
                self.own_task(who, task["id"])
            if overrules is not None:
                target = self.s.one("SELECT decider, lasting FROM activity_log WHERE id = ? AND project = ? AND kind = 'decision'",
                                    overrules, project)
                if not target:
                    raise ApiError(400, "overrules must name an earlier decision in this project")
                if target["decider"] == "owner" and decider != "owner":
                    raise ApiError(403, "only another owner decision can overrule an owner decision")
                lasting = lasting or bool(target["lasting"])
            prior = self.s.one("SELECT * FROM activity_log WHERE project = ? AND bot = ? AND dedupe_key = ? AND created_at > ?"
                               " ORDER BY id DESC LIMIT 1", project, bot, key, now() - DEDUPE_WINDOW) if key else None
            if prior:
                fields = ("kind", "decider", "summary", "link", "overrules", "lasting")
                if (kind, decider, summary, link, overrules, lasting) != tuple(prior[f] for f in fields):
                    raise ApiError(409, "dedupe_key was used for a different entry in the last 24 hours")
                return {"entry": activity_entry(prior), "duplicate": True}
            entry_id = self.s.run("INSERT INTO activity_log(project, bot, kind, decider, summary, link, overrules, lasting,"
                                  " dedupe_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                  project, bot, kind, decider, summary, link, overrules, lasting, key, now()).lastrowid
            row = self.s.one("SELECT * FROM activity_log WHERE id = ?", entry_id)
        return {"entry": activity_entry(row), "duplicate": False}

    def list_activity(self, who, qs):
        project = self.activity_project(who, qs.get("project"))
        try:
            limit = int(qs.get("limit", "20"))
            since = int(qs.get("since", "0"))
        except ValueError:
            raise ApiError(400, "limit and since must be numbers")
        if not 1 <= limit <= 1000:
            raise ApiError(400, "limit must be between 1 and 1000")
        sql, args = "SELECT * FROM activity_log WHERE project = ? AND created_at >= ?", [project, since]
        for field, allowed in (("bot", None), ("kind", ACTIVITY_KINDS), ("decider", DECIDERS)):
            value = qs.get(field)
            if value is None:
                continue
            if (allowed and value not in allowed) or not ID_RE.fullmatch(value):
                raise ApiError(400, f"bad {field} filter")
            sql += f" AND {field} = ?"
            args.append(value)
        rows = self.s.q(sql + " ORDER BY id DESC LIMIT ?", *args, limit)
        return {"entries": [activity_entry(row) for row in reversed(rows)]}

    def export_activity(self, who, qs):
        project = self.activity_project(who, qs.get("project"))
        month = qs.get("month") or datetime.fromtimestamp(now(), CENTRAL).strftime("%Y-%m")
        return self.activity_month(project, month)

    def activity_month(self, project, month, through=2**63 - 1):
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
                raise ValueError
            start = datetime.strptime(month, "%Y-%m").replace(tzinfo=CENTRAL)
            end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
            bounds = int(start.timestamp()), int(end.timestamp())
        except (ValueError, OverflowError, OSError):
            raise ApiError(400, "month must be a valid YYYY-MM")
        rows = self.s.q("SELECT * FROM activity_log WHERE project = ? AND created_at >= ? AND created_at < ? AND id <= ?"
                        " ORDER BY id", project, *bounds, through)
        return "".join(activity_entry(row)["line"] + "\n" for row in rows)

    def activity_cursor(self, project):
        row = self.s.one("SELECT value FROM meta WHERE key = ?", f"activity_published:{project}")
        return int(row["value"]) if row else 0

    def activity_snapshot(self, who, qs):
        project = self.activity_project(who, qs.get("project"))
        try:
            after = int(qs["after"]) if qs.get("after") is not None else self.activity_cursor(project)
            if not 0 <= after < 2**63:
                raise ValueError
        except ValueError:
            raise ApiError(400, "after must be an activity entry ID or zero")
        through = self.s.one("SELECT MAX(id) AS newest FROM activity_log WHERE project = ?", project)["newest"] or 0
        if through <= after:
            return {"after": after, "through": through, "files": {}}
        new = self.s.q("SELECT created_at, lasting FROM activity_log WHERE project = ? AND id > ? AND id <= ?",
                       project, after, through)
        months = {datetime.fromtimestamp(row["created_at"], CENTRAL).strftime("%Y-%m") for row in new}
        files = {f"{project}/log/{month}.md": self.activity_month(project, month, through) for month in sorted(months)}
        if any(row["lasting"] for row in new):
            groups = {}
            for row in self.s.q("SELECT * FROM activity_log WHERE project = ? AND lasting = 1 AND id <= ? ORDER BY id",
                                project, through):
                date = datetime.fromtimestamp(row["created_at"], CENTRAL).strftime("%Y-%m-%d")
                groups.setdefault(date, {}).setdefault(row["decider"], []).append(activity_entry(row)["line"])
            block = ["<!-- activity-log:begin -->"]
            for date, entries in sorted(groups.items()):
                block += ["", f"## {date}"]
                for label, decider in (("Owner decided", "owner"),
                                       ("Delegated by Chief of Staff", "delegated"), ("Project bot calls", "bot")):
                    if decider == "bot" and decider not in entries:
                        continue
                    block += ["", f"### {label}", "", *entries.get(decider, ["None."])]
            files[f"{project}/decisions.md"] = "\n".join([*block, "", "<!-- activity-log:end -->", ""])
        return {"after": after, "through": through, "files": files}

    def mark_activity_published(self, who, body):
        """Advance the nightly export cursor. Call only after the export task is done (its push succeeded)."""
        project = self.activity_project(who, body.get("project"))
        through = body.get("through")
        with self.s.tx():
            newest = self.s.one("SELECT MAX(id) AS newest FROM activity_log WHERE project = ?", project)["newest"] or 0
            if type(through) is not int or not 0 <= through <= newest:
                raise ApiError(400, f"through must be an entry ID from a snapshot of this project (0 to {newest})")
            self.s.run("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", f"activity_published:{project}", str(through))
        return {"project": project, "published_through": through}

    def latest(self, project, kind, ref, status=None):
        sql, args = "SELECT * FROM log_entries WHERE lower(project) = lower(?) AND kind = ? AND ref = ?", [project, kind, ref]
        if status:
            sql += " AND status = ?"
            args.append(status)
        return self.s.one(sql + " ORDER BY id DESC LIMIT 1", *args)

    def rule_text(self, project, ref):
        r = self.latest(project, "rule", ref, "approved")
        return r["after_text"] if r else None

    def standing_rules(self, project):
        rows = self.s.q("SELECT * FROM log_entries WHERE id IN (SELECT MAX(id) FROM log_entries WHERE lower(project) = lower(?)"
                        " AND kind = 'rule' AND status = 'approved' GROUP BY ref)", project)
        return sorted((r for r in rows if r["after_text"]), key=lambda r: int(r["ref"].rsplit("-", 1)[-1]))

    def pending_rules(self, project):
        return self.s.q("SELECT * FROM log_entries p WHERE lower(p.project) = lower(?) AND p.kind = 'rule' AND p.status = 'proposed'"
                        " AND NOT EXISTS (SELECT 1 FROM log_entries r WHERE r.kind = 'rule' AND r.about = p.id) ORDER BY p.id", project)

    def current_items(self, project):
        rows = self.s.q("SELECT * FROM log_entries WHERE id IN (SELECT MAX(id) FROM log_entries WHERE lower(project) = lower(?)"
                        " AND kind = 'item' GROUP BY ref) ORDER BY id", project)
        return [r for r in rows if not r["expires_at"] or r["expires_at"] > now()]

    def log_record(self, who, project, kind, b, summary, by, status, ref, nid):
        """Check one typed record and return (columns, summary). Raises ApiError on a bad record."""
        rec = {"author": by}
        source = clean_text(b.get("source"), 200, "source")
        if kind in ("activity", "result"):
            if status or ref:
                raise ApiError(400, f"{kind} entries take no status or ref")
        elif kind == "decision":
            if status or ref:
                raise ApiError(400, "decision entries take no status or ref")
            if not by:
                raise ApiError(400, "a decision needs by: who decided")
            answer = clean_text(b.get("answer"), 4000, "answer")
            if not answer:
                raise ApiError(400, "a decision needs answer: what was decided")
            rec.update(answer=answer, source=source or "chat")
        elif kind == "feedback":
            if status:
                raise ApiError(400, "feedback takes no status")
            if not by:
                raise ApiError(400, "feedback needs by: who gave it")
            if ref:
                item = self.latest(project, "item", ref)
                if not item:
                    raise ApiError(404, f"no item {ref} in project {project}")
                rec.update(ref=ref, version=item["version"])
            rec["source"] = source or "chat"
        elif kind == "learning":
            if ref or status == "standing":
                raise ApiError(400, "a learning becomes standing only when a rule change based on it is approved. "
                                    "Propose one: kind rule, after RULE TEXT, learning REF")
            if status not in (None, "pending"):
                raise ApiError(400, "status must be pending (learnings start pending)")
            rec.update(ref=f"learning-{nid}", status="pending", source=source)
        elif kind == "item":
            if status not in ITEM_STATUSES:
                raise ApiError(400, "status must be one of: " + ", ".join(ITEM_STATUSES))
            if not ref:
                if status not in ("draft", "needs-review"):
                    raise ApiError(400, "a new item starts as draft or needs-review")
                if not summary:
                    raise ApiError(400, "summary is required (what the deliverable is)")
                rec.update(ref=f"item-{nid}", version=1, status=status, source=source)
                if b.get("expires_days") is not None:
                    try:
                        days = float(b["expires_days"])
                    except (TypeError, ValueError):
                        raise ApiError(400, "expires_days must be a number")
                    if not 0 < days <= 365:
                        raise ApiError(400, "expires_days must be between 0 and 365")
                    rec["expires_at"] = now() + int(days * 86400)
            else:
                cur = self.latest(project, "item", ref)
                if not cur:
                    raise ApiError(404, f"no item {ref} in project {project}")
                version = cur["version"]
                if status in VERDICTS:
                    if not by:
                        raise ApiError(400, "a verdict needs by: who gave it")
                    asked = b.get("version")
                    if asked is not None and str(asked) != str(version):
                        raise ApiError(409, f"version {asked} is out of date: {ref} is at version {version}. "
                                            "Its older verdicts no longer count. Review the new version.", {"version": version})
                elif b.get("new_version") or cur["status"] in VERDICTS:
                    version += 1  # a new draft: verdicts on older versions no longer count
                rec.update(ref=ref, status=status, version=version, expires_at=cur["expires_at"], source=source)
                summary = summary or f"{cur['summary']}"
        elif kind == "rule":
            status = status or "proposed"
            if status not in RULE_STATUSES:
                raise ApiError(400, "status must be one of: " + ", ".join(RULE_STATUSES))
            if status == "proposed":
                after = clean_text(b.get("after"), 1000, "after") or None
                if ref:
                    before = self.rule_text(project, ref)
                    if before is None:
                        raise ApiError(404, f"no standing rule {ref} in project {project}")
                else:
                    if not after:
                        raise ApiError(400, "a new rule needs after: the rule text")
                    before, ref = None, f"rule-{nid}"
                if after == before:
                    raise ApiError(400, "after is the same as the current rule")
                learning = clean_text(b.get("learning"), 80, "learning")
                if learning and not self.latest(project, "learning", learning):
                    raise ApiError(404, f"no learning {learning} in project {project}")
                rec.update(ref=ref, status=status, before_text=before, after_text=after, source=learning)
                summary = summary or ("Add rule" if before is None else "Remove rule" if after is None else "Change rule") + \
                    f": {after or before}"
            else:
                if who["role"] not in ("admin", "chief"):
                    raise ApiError(403, "rule changes need review: only the owner (admin token or Chief of Staff) can approve or reject")
                if not by:
                    raise ApiError(400, "a review needs by: who approved or rejected")
                try:
                    pid = int(b.get("proposal"))
                except (TypeError, ValueError):
                    raise ApiError(400, "proposal must be the id of a proposed rule change")
                p = self.s.one("SELECT * FROM log_entries WHERE id = ? AND lower(project) = lower(?) AND kind = 'rule' AND status = 'proposed'",
                               pid, project)
                if not p:
                    raise ApiError(404, f"no proposed rule change {pid} in project {project}")
                if self.s.one("SELECT 1 FROM log_entries WHERE kind = 'rule' AND about = ?", pid):
                    raise ApiError(409, f"rule change {pid} was already reviewed")
                if status == "approved":
                    if self.rule_text(project, p["ref"]) != p["before_text"]:
                        raise ApiError(409, f"{p['ref']} changed after this proposal. Propose the change again.")
                    cap = env_int("SWARM_RULES_CAP", 25)
                    if p["before_text"] is None and len(self.standing_rules(project)) >= cap:
                        raise ApiError(409, f"the rules doc for {project} is full ({cap} rules). Merge or remove a rule first, "
                                            "so the doc stays short enough to read before every draft.")
                rec.update(ref=p["ref"], status=status, before_text=p["before_text"], after_text=p["after_text"], about=pid,
                           source=p["source"])
                summary = summary or f"{status.capitalize()}: {p['summary']}"
        return rec, summary

    def post_log(self, who, b):
        if who["role"] == "task":
            t = self.own_task(who, who["task"])
            if not t["project"]:
                raise ApiError(400, "this task has no project, so it has no project log")
            if b.get("project") and b["project"] != t["project"]:
                raise ApiError(403, "a task can only write to its own project's log")
            project, bot, task_id = t["project"], t["owner"] or "worker", t["id"]
        else:
            project = clean_text(b.get("project"), 80, "project")
            if not project or not ID_RE.match(project):
                raise ApiError(400, "project is required")
            allowed = self.log_projects(who)
            if allowed is not None and project.lower() not in (p.lower() for p in allowed):
                raise ApiError(403, f"this bot is not bound to project {project}. Bind it: swarmctl project set {project} --bot BOT_ID")
            named = clean_text(b.get("bot"), 80, "bot")
            if who["bot"]:
                bot = who["bot"]
            elif named and not self.bot(named):
                raise ApiError(400, f"unknown bot {named}. Register it first: swarmctl bot set {named} --role project")
            else:
                bot = named or who["role"]
            task_id = clean_text(b.get("task_id"), 80, "task_id")
            if task_id and who["role"] == "bot":
                self.check_item_task(who, who["bot"], task_id)  # a bot may only link its own tasks
        kind = b.get("kind")
        if kind not in LOG_KINDS:
            raise ApiError(400, "kind must be one of: " + ", ".join(LOG_KINDS))
        summary = clean_text(b.get("summary"), 500, "summary")
        detail = clean_text(b.get("detail"), 20000, "detail")
        links = b.get("links") or []
        if not isinstance(links, list) or len(links) > 20:
            raise ApiError(400, "links must be a list (max 20)")
        key = clean_text(b.get("dedupe_key"), 200, "dedupe_key")
        by = clean_text(b.get("by"), 80, "by")
        status = clean_text(b.get("status"), 20, "status")
        ref = clean_text(b.get("ref"), 80, "ref")
        extra = {}
        with self.s.tx():
            if key:
                row = self.s.one("SELECT * FROM log_entries WHERE project = ? AND dedupe_key = ?", project, key)
                if row:
                    return {"entry": self.shape_log(row), "duplicate": True}
            nid = self.next_log_id()
            rec, summary = self.log_record(who, project, kind, b, summary, by, status, ref, nid)
            if not summary:
                raise ApiError(400, "summary is required")
            eid = self.add_log(project, bot, kind, summary, detail, links, task_id, key, **rec)
            if kind == "feedback":
                # Every piece of feedback becomes a pending learning. The owner turns the lasting ones into rules.
                lid = self.add_log(project, bot, "learning", summary, None, (), task_id, None, ref=f"learning-{self.next_log_id()}",
                                   status="pending", author=by, source=f"feedback {eid}", about=eid)
                extra["learning"] = self.shape_log(self.s.one("SELECT * FROM log_entries WHERE id = ?", lid))
            if kind == "rule" and rec["status"] == "approved" and rec.get("source"):
                lrn = self.latest(project, "learning", rec["source"])
                if lrn and lrn["status"] != "standing":
                    self.add_log(project, bot, "learning", lrn["summary"], None, (), None, None, ref=lrn["ref"], status="standing",
                                 author=by, source=f"rule {rec['ref']}", about=eid)
        return dict({"entry": self.shape_log(self.s.one("SELECT * FROM log_entries WHERE id = ?", eid)), "duplicate": False}, **extra)

    @staticmethod
    def shape_log(r):
        r = dict(r)
        r["links"] = json.loads(r["links"] or "[]")
        r.pop("dedupe_key", None)
        r["by"] = r.pop("author", None)
        r["before"] = r.pop("before_text", None)
        r["after"] = r.pop("after_text", None)
        return {k: v for k, v in r.items() if v is not None or k in ("bot", "detail", "task_id")}

    def list_log(self, who, qs):
        # Expired throwaway items are hidden everywhere at once, before the daily cleanup deletes them.
        where, args = ["NOT (kind = 'item' AND expires_at IS NOT NULL AND expires_at <= ?)"], [now()]
        allowed = self.log_projects(who)
        if qs.get("project"):
            if allowed is not None and qs["project"].lower() not in (p.lower() for p in allowed):
                raise ApiError(404, "no such project log")
            where.append("lower(project) = lower(?)"); args.append(qs["project"])
        elif allowed is not None:
            if not allowed:
                return {"entries": []}
            where.append("lower(project) IN (" + ",".join("lower(?)" for _ in allowed) + ")"); args += allowed
        if qs.get("kind"):
            kinds = [k for k in qs["kind"].split(",") if k]
            if any(k not in LOG_KINDS for k in kinds):
                raise ApiError(400, "kind must be one of: " + ", ".join(LOG_KINDS))
            where.append("kind IN (" + ",".join("?" for _ in kinds) + ")"); args += kinds
        for f in ("ref", "status"):
            if qs.get(f):
                where.append(f"{f} = ?"); args.append(qs[f])
        for key, op in (("since", ">="), ("until", "<")):
            if qs.get(key):
                try:
                    where.append(f"created_at {op} ?"); args.append(int(qs[key]))
                except ValueError:
                    raise ApiError(400, f"{key} must be a unix time")
        try:
            limit = max(1, min(int(qs.get("limit") or 100), 1000))
        except ValueError:
            raise ApiError(400, "limit must be a number")
        sql = "SELECT * FROM log_entries" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.s.q(sql, *args, limit)
        return {"entries": [self.shape_log(r) for r in reversed(rows)]}

    def log_items(self, who, qs):
        project = self.need_project(who, qs)
        items = [self.shape_log(r) for r in self.current_items(project)]
        if qs.get("status"):
            items = [i for i in items if i["status"] in qs["status"].split(",")]
        return {"items": items}

    def log_rules(self, who, qs):
        project = self.need_project(who, qs)
        changes = self.s.q("SELECT * FROM log_entries WHERE lower(project) = lower(?) AND kind = 'rule' AND status != 'proposed'"
                           " ORDER BY id", project)
        return {"project": project, "cap": env_int("SWARM_RULES_CAP", 25),
                "rules": [self.shape_log(r) for r in self.standing_rules(project)],
                "pending": [self.shape_log(r) for r in self.pending_rules(project)],
                "changelog": [self.shape_log(r) for r in changes]}

    def need_project(self, who, qs):
        project = qs.get("project")
        if not project:
            raise ApiError(400, "project is required")
        self.check_log_project(who, project)
        return project

    def log_brief(self, who, qs):
        """What a bot reads before drafting: the standing rules and the last 14 days of feedback."""
        project = self.need_project(who, qs)
        r = self.log_rules(who, qs)
        out = [rules_markdown(r), f"\n## Feedback from the last {DIGEST_DAYS} days\n"]
        fb = self.s.q("SELECT * FROM log_entries WHERE lower(project) = lower(?) AND kind = 'feedback' AND created_at >= ? ORDER BY id",
                      project, now() - DIGEST_DAYS * 86400)
        out += [f"- {day(e['created_at'])} {e['author']}: {e['summary']}" + (f" (on {e['ref']} v{e['version']})" if e["ref"] else "")
                for e in fb] or ["None."]
        changes = [i for i in self.current_items(project) if i["status"] == "changes"]
        if changes:
            out.append("\n## Items waiting for your changes\n")
            out += [f"- {i['ref']} v{i['version']}: {i['summary']}" for i in changes]
        return "\n".join(out).rstrip() + "\n"

    def export_log(self, who, qs):
        project = self.need_project(who, qs)
        entries = self.list_log(who, dict(qs, limit=qs.get("limit") or "1000"))["entries"]
        since = int(qs["since"]) if qs.get("since") else None
        head = [f"# Project log: {project}\n", f"Exported {day(now())} (UTC)."]
        items = self.current_items(project)
        if items:
            head.append("\n## Items\n")
            head += [f"- {i['ref']} v{i['version']} **{i['status']}**: {i['summary']}" +
                     (f" (throwaway, expires {day(i['expires_at'])})" if i["expires_at"] else "") for i in items]
        rules = self.log_rules({"role": "admin", "bot": None}, {"project": project})
        head.append(f"\n## Rules\n\n{len(rules['rules'])} standing rules (cap {rules['cap']}), "
                    f"{len(rules['pending'])} rule change(s) waiting for review. Full rules doc: swarmctl log rules {project}")
        for p in rules["pending"]:
            head.append(f"- waiting for review: change {p['id']}: {p['summary']}")
        w = self.list_items(who, {"status": "due", "project": project})
        c = w["counts"]
        head.append(f"\n## Waiting on you\n\n{c['open']} open, {c['snoozed']} snoozed, oldest {c['oldest_days']} days.")
        stale = [i for i in w["items"] if i["age_days"] >= STALE_DAYS]
        if stale:
            head.append(f"Open for {STALE_DAYS} days or more. Consider dropping these if they no longer matter:")
            head += [f"- {i['id']} ({i['age_days']} days): {(i['ask'] or '')[:120]}" for i in stale]
        return "\n".join(head) + "\n\n" + log_markdown(project, entries, since, title="## Log")

    def weekly_export(self):
        """Once per ISO week, write every project's last 7 days to SWARM_LOG_EXPORT_DIR. Commits to git where asked."""
        root = os.environ.get("SWARM_LOG_EXPORT_DIR")
        if not root:
            return
        week = time.strftime("%G-W%V", time.gmtime())
        last = self.s.one("SELECT value FROM meta WHERE key = 'log_export_week'")
        if last and last["value"] == week:
            return
        admin = {"role": "admin", "bot": None}
        for p in self.s.q("SELECT name, log_git FROM projects ORDER BY name"):
            d = os.path.join(os.path.expanduser(root), p["name"])
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{week}.md"), "w") as fh:
                fh.write(self.export_log(admin, {"project": p["name"], "since": str(now() - 7 * 86400)}))
            with open(os.path.join(d, "rules.md"), "w") as fh:
                fh.write(rules_markdown(self.log_rules(admin, {"project": p["name"]})))
            if p["log_git"]:
                git_commit(d, f"Project log export {week}")
        self.s.run("INSERT OR REPLACE INTO meta(key, value) VALUES('log_export_week', ?)", week)

    # ---------- status ----------
    # ---------- orchestrators (one long-lived agent session per name, pinned to one worker) ----------
    def orch_row(self, name):
        return self.s.one("SELECT * FROM orchestrators WHERE name = ? AND deleted_at IS NULL", name)

    def shape_orch(self, r, full=False):
        out = {k: v for k, v in r.items() if k not in ("log_tail", "supervisor_id", "brief", "deleted_at")}
        out["secret_bundles"] = json.loads(r.get("secret_bundles") or "[]")
        out["tmux_session"] = r.get("tmux_session") or f"orch-{r['name']}"
        out["uptime"] = now() - r["started_at"] if r.get("started_at") and r["state"] == "running" else None
        out["heartbeat_age"] = now() - r["last_heartbeat_at"] if r.get("last_heartbeat_at") else None
        if full:
            out["brief"] = r["brief"]
        return out

    def get_orch(self, who, name):
        r = self.orch_row(name) if ORCH_NAME_RE.match(name or "") else None
        if not r or not (who["role"] in ("admin", "chief") or
                         (who["role"] == "bot" and (r["owner_bot_id"] or "").lower() == who["bot"].lower())):
            raise ApiError(404, "no such orchestrator")
        return r

    def orch_event(self, name, typ, actor, text=None, meta=None):
        self.s.run("INSERT INTO orchestrator_events(name, at, type, actor, text, meta) VALUES(?,?,?,?,?,?)",
                   name, now(), typ, actor, (text or "")[:500] or None, json.dumps(meta) if meta else None)

    def notify_orch(self, r, kind, text):
        """One notice per state change: the owner bot first, the Chief of Staff when there is no reachable owner."""
        seq = self.s.one("SELECT COUNT(*) AS n FROM orchestrator_events WHERE name = ? AND type = ?", r["name"], kind)["n"]
        event_id = f"orch:{r['name']}:{kind}:{seq}"
        payload = {"event": "orchestrator", "event_id": event_id, "idempotency_key": event_id, "orchestrator": r["name"],
                   "kind": kind, "state": r["state"], "desired": r["desired"], "worker_id": r["worker_id"],
                   "project": r["project"], "owner": r["owner_bot_id"], "text": text}
        owner = self.bot(r["owner_bot_id"])
        if owner and owner.get("notify_url"):
            self.queue_notice(event_id, owner, "owner", payload)
        else:
            self.queue_notice(event_id, self.chief(), "no_owner" if not r["owner_bot_id"] else "owner_not_registered", payload)

    def secret_hit(self, text):
        return next((i + 1 for i, p in enumerate(self.secret_patterns) if p.search(text or "")), None)

    def scrub(self, text):
        return "\n".join("[line hidden: it looks like a secret]" if self.secret_hit(ln) else ln for ln in (text or "").splitlines())

    def scrub_tail(self, text):
        """A terminal tail: secrets as above, and every sign-in link (the supervisor hides them too)."""
        return "\n".join("[line hidden: part of a sign-in link]" if SIGNIN_LINE_RE.search(ln) else
                         SIGNIN_URL_RE.sub("[sign-in link hidden]", ln) for ln in self.scrub(text).splitlines())

    def orch_fields(self, b, cur=None):
        unknown = sorted(set(b) - ORCH_FIELDS - ({"name", "start"} if cur is None else set()))
        if unknown:
            raise ApiError(400, f"unknown field(s): {', '.join(unknown)}. Name secret bundles in secret_bundles; never send values")
        cur = cur or {}
        f = {}
        for k in ("worker_id", "project", "repo", "owner_bot_id", "provider", "model", "relay_agent_name"):
            f[k] = clean_text(b[k], 200, k) or None if k in b else cur.get(k)
        f["brief"] = clean_text(b["brief"], ORCH_BRIEF_MAX, "brief") if "brief" in b else cur.get("brief")
        bundles = text_list(b["secret_bundles"], "secret_bundles", BUNDLE_RE, 10) if "secret_bundles" in b else None
        f["secret_bundles"] = json.dumps(bundles) if bundles is not None else cur.get("secret_bundles") or "[]"
        if not f["worker_id"] or not SAFE_NAME.fullmatch(f["worker_id"]):
            raise ApiError(400, "worker_id is required: the name of the one worker that runs this orchestrator")
        if not f["project"]:
            raise ApiError(400, "project is required")
        proj = self.project(f["project"])
        if not proj:
            raise ApiError(404, f"no such project {f['project']}. Create it first: swarmctl project set {f['project']} --repo OWNER/REPO")
        if not f["brief"]:
            raise ApiError(400, "brief is required: the standing instructions, with no secrets in it")
        hit = self.secret_hit(f["brief"])
        if hit:
            raise ApiError(400, f"the brief looks like it holds a secret (secret pattern {hit}). Put keys in a runtime bundle "
                                "and name the bundle in secret_bundles")
        if f["repo"]:
            f["repo"] = norm_repo(f["repo"])
            if f["repo"] not in self.project_repos(proj):
                raise ApiError(400, f"repo {f['repo']} is not one of project {f['project']}'s repos")
        f["provider"] = f["provider"] or "claude"
        if f["provider"] != "claude":
            raise ApiError(400, "provider must be claude: an orchestrator is one persistent Claude Code session")
        f["model"] = f["model"] or proj.get("default_model") or "default"
        if not MODEL_RE.match(f["model"]):
            raise ApiError(400, "bad model name")
        if f["owner_bot_id"]:
            owner = self.bot(f["owner_bot_id"])
            if not owner:
                raise ApiError(404, f"no such bot {f['owner_bot_id']}")
            if owner["role"] != "chief" and owner["id"].lower() not in (x.lower() for x in proj["bots"]):
                raise ApiError(400, f"{owner['id']} is not bound to project {f['project']}")
            f["owner_bot_id"] = owner["id"]
        if f["relay_agent_name"]:
            if not RELAY_NAME_RE.match(f["relay_agent_name"]):
                raise ApiError(400, "relay_agent_name: lowercase letters, digits, dash or underscore, 2 to 64 characters")
            taken = self.s.one("SELECT name FROM orchestrators WHERE relay_agent_name = ? AND deleted_at IS NULL AND name != ?",
                               f["relay_agent_name"], cur.get("name") or "")
            if taken:
                raise ApiError(409, f"relay_agent_name {f['relay_agent_name']} is already used by orchestrator {taken['name']}")
        w = self.s.one("SELECT name, projects FROM workers WHERE name = ?", f["worker_id"])
        if not w:
            raise ApiError(400, f"worker {f['worker_id']} has never checked in. Start its runner first (it checks in every 20 seconds)")
        if w["projects"] is not None and f["project"] not in json.loads(w["projects"]):
            raise ApiError(403, f"worker {f['worker_id']} does not take project {f['project']} (its SWARM_WORKER_PROJECTS allowlist)")
        return f

    def create_orch(self, who, b):
        self.need(who, "admin")
        name = clean_text(b.get("name"), 40, "name") or ""
        if not ORCH_NAME_RE.match(name):
            raise ApiError(400, "name: 2 to 32 characters, lowercase letters, digits and dashes, starting with a letter or digit")
        with self.s.tx():
            if self.orch_row(name):
                raise ApiError(409, f"orchestrator {name} already exists. Change it with PUT /orchestrators/{name}")
            f = self.orch_fields(b)
            old = self.s.one("SELECT name FROM orchestrators WHERE name = ?", name)
            if old:  # a deleted record with the same name: its events stay, the row is replaced
                self.s.run("DELETE FROM orchestrators WHERE name = ?", name)
                self.s.run("DELETE FROM orchestrator_messages WHERE name = ?", name)
            desired = "running" if b.get("start") else "stopped"
            self.s.run("INSERT INTO orchestrators(name, worker_id, project, repo, owner_bot_id, provider, model, brief, secret_bundles,"
                       " relay_agent_name, desired, state, tmux_session, created_at, updated_at, desired_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       name, f["worker_id"], f["project"], f["repo"], f["owner_bot_id"], f["provider"], f["model"], f["brief"],
                       f["secret_bundles"], f["relay_agent_name"], desired, "stopped", f"orch-{name}", now(), now(), now())
            self.orch_event(name, "created", "admin", f"pinned to worker {f['worker_id']}, project {f['project']}")
            if desired == "running":
                self.orch_event(name, "start-requested", "admin")
        return {"orchestrator": self.shape_orch(self.orch_row(name), full=True)}

    def change_orch(self, who, name, b):
        self.need(who, "admin")
        with self.s.tx():
            r = self.get_orch(who, name)
            f = self.orch_fields(b, r)
            live = r["state"] in ("running", "starting", "stopping") and (r["last_heartbeat_at"] or 0) > now() - ORCH_STALE_SECONDS
            if f["worker_id"] != r["worker_id"] and (r["desired"] == "running" or live):
                raise ApiError(409, "stop it before moving it to another worker")
            cols = ", ".join(f"{k} = ?" for k in f)
            self.s.run(f"UPDATE orchestrators SET {cols}, updated_at = ? WHERE name = ?", *f.values(), now(), name)
            changed = sorted(k for k in f if f[k] != r.get(k))
            self.orch_event(name, "config", "admin", "changed: " + (", ".join(changed) or "nothing")
                            + (". Takes effect at the next start or restart" if changed else ""))
        return {"orchestrator": self.shape_orch(self.orch_row(name), full=True)}

    def control_orch(self, who, name, action):
        self.need(who, "admin")
        with self.s.tx():
            r = self.get_orch(who, name)
            if action == "start":
                self.s.run("UPDATE orchestrators SET desired = 'running', desired_at = ?, generation = generation + 1, updated_at = ?"
                           " WHERE name = ?", now(), now(), name)
            elif action == "restart":
                self.s.run("UPDATE orchestrators SET desired = 'running', desired_at = ?, generation = generation + 1,"
                           " restart_generation = generation + 1, updated_at = ? WHERE name = ?", now(), now(), name)
            elif action == "stop":
                self.s.run("UPDATE orchestrators SET desired = 'stopped', desired_at = ?, updated_at = ? WHERE name = ?", now(), now(), name)
            elif action == "delete":
                if r["desired"] == "running":
                    raise ApiError(409, f"stop it first: swarmctl orchestrator stop {name}")
                # The session must be down first, or deleting the record and disabling the unit leaves it running unseen.
                quiet = (r["last_heartbeat_at"] or 0) < now() - ORCH_STALE_SECONDS
                if r["state"] not in ORCH_DELETABLE and not quiet:
                    raise ApiError(409, f"orchestrator {name} is still {r['state']}; wait until swarmctl orchestrator status "
                                        f"{name} shows stopped, then delete it")
                self.s.run("UPDATE orchestrators SET deleted_at = ?, updated_at = ? WHERE name = ?", now(), now(), name)
                self.s.run("DELETE FROM orchestrator_messages WHERE name = ? AND delivered_at IS NULL", name)
            self.orch_event(name, f"{action}-requested", "admin")
        return {"orchestrator": self.shape_orch(self.orch_row(name)) if action != "delete" else None, "action": action}

    def list_orchs(self, who):
        rows = self.s.q("SELECT * FROM orchestrators WHERE deleted_at IS NULL ORDER BY name")
        if who["role"] == "bot":
            rows = [r for r in rows if (r["owner_bot_id"] or "").lower() == who["bot"].lower()]
        return {"orchestrators": [self.shape_orch(r) for r in rows]}

    def show_orch(self, who, name):
        r = self.get_orch(who, name)
        events = self.s.q("SELECT at, type, actor, text FROM orchestrator_events WHERE name = ? AND at >= ? ORDER BY id DESC LIMIT 20",
                          name, r["created_at"] or 0)
        pending = self.s.one("SELECT id, at, sender FROM orchestrator_messages WHERE name = ? AND delivered_at IS NULL ORDER BY id LIMIT 1", name)
        return {"orchestrator": self.shape_orch(r, full=True), "events": events, "pending_message": pending}

    def orch_logs(self, who, name, qs):
        r = self.get_orch(who, name)
        try:
            n = int(qs.get("lines") or 100)
        except ValueError:
            raise ApiError(400, "lines must be a number")
        if not 1 <= n <= 500:
            raise ApiError(400, "lines must be 1 to 500")
        lines = self.scrub(r["log_tail"]).splitlines()[-n:] if r["log_tail"] else []
        return {"name": name, "at": r["log_tail_at"], "lines": lines}

    def orch_message(self, who, name, b):
        text = clean_text(b.get("text"), ORCH_MESSAGE_MAX, "text")
        text = swarm_bus.clean_body(text) if text else text   # no ESC or CR: it is typed into a terminal
        if not text:
            raise ApiError(400, "text is required")
        if self.secret_hit(text):
            raise ApiError(400, "the message looks like it holds a secret; refer to a secret bundle by name instead")
        sender = who["bot"] or who["role"]
        with self.s.tx():
            r = self.get_orch(who, name)
            if r["desired"] != "running":
                raise ApiError(409, f"orchestrator {name} is stopped")
            if self.s.one("SELECT 1 FROM orchestrator_messages WHERE name = ? AND delivered_at IS NULL AND bus_message_id IS NULL", name):
                raise ApiError(409, "one message at a time: the last message has not been delivered yet")
            cur = self.s.run("INSERT INTO orchestrator_messages(name, at, sender, text) VALUES(?,?,?,?)", name, now(), sender, text)
            self.orch_event(name, "message", sender, f"message queued ({len(text)} characters)")
            bus_id = None
            try:
                bus_id = self.bus.mirror_orchestrator_message(name, sender, text, cur.lastrowid)
            except Exception as e:  # the bus must never break the old path
                print("bus mirror failed:", repr(e), file=sys.stderr, flush=True)
        return {"id": cur.lastrowid, "queued": True, **({"bus_id": bus_id} if bus_id else {})}

    def orch_release(self, who, name, mid, b):
        """An admin checked the session after a delivery-unknown event. Default: the message arrived, mark it delivered (the
        supervisor drops its hold, nothing is typed). resend: type it once more (the supervisor sees the release count
        go up on the pending message). Either way the supervisor never guesses on its own."""
        self.need(who, "admin")
        resend = b.get("resend", False)
        if not isinstance(resend, bool):
            raise ApiError(400, "resend must be true or false")
        with self.s.tx():
            self.get_orch(who, name)
            m = self.s.one("SELECT id, delivered_at FROM orchestrator_messages WHERE id = ? AND name = ?", mid, name)
            if not m:
                raise ApiError(404, f"orchestrator {name} has no message {mid}")
            if m["delivered_at"]:
                raise ApiError(409, f"message {mid} is already delivered")
            if resend:
                self.s.run("UPDATE orchestrator_messages SET release_count = release_count + 1 WHERE id = ?", mid)
                self.orch_event(name, "message-released", "admin", f"message {mid} released: it is typed once more")
            else:
                self.s.run("UPDATE orchestrator_messages SET delivered_at = ? WHERE id = ?", now(), mid)
                try:
                    self.bus.on_orchestrator_delivered(mid)
                except Exception as e:
                    print("bus delivered mark failed:", repr(e), file=sys.stderr, flush=True)
                self.orch_event(name, "message-released", "admin", f"message {mid} marked delivered by an admin")
        return {"id": mid, "released": True, "resend": resend}

    def orch_heartbeat(self, who, name, b):
        """The supervisor on the pinned worker reports, and gets back what it should be doing."""
        self.need(who, "worker")
        worker = clean_text(b.get("worker"), 80, "worker") or ""
        sup = clean_text(b.get("supervisor_id"), 64, "supervisor_id") or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", sup):
            raise ApiError(400, "supervisor_id is required")
        with self.s.tx():
            r = self.s.one("SELECT * FROM orchestrators WHERE name = ?", name) if ORCH_NAME_RE.match(name) else None
            if not r:
                raise ApiError(404, "no such orchestrator")
            if r["worker_id"] != worker:
                raise ApiError(403, f"orchestrator {name} is pinned to another worker; nothing may start here")
            if r["supervisor_id"] and r["supervisor_id"] != sup and (r["last_heartbeat_at"] or 0) > now() - ORCH_SUPERVISOR_LEASE:
                raise ApiError(409, "another supervisor is running this orchestrator", {"other_supervisor": True})
            self.seen(worker)
            state = clean_text(b.get("state"), 20, "state") or r["state"]
            if state not in ORCH_STATES or state == "unreachable":
                raise ApiError(400, "bad state")
            # A supervisor that shuts down cleanly hands over at once, so its replacement need not wait out the lease.
            vals = {"state": state, "supervisor_id": None if b.get("release") else sup, "last_heartbeat_at": now(), "updated_at": now()}
            for k, rx in (("session_id", r"[A-Za-z0-9_-]{1,100}"), ("tmux_session", r"orch-[a-z0-9-]{1,40}")):
                v = clean_text(b.get(k), 100, k) if k in b else r[k]
                if v and not re.fullmatch(rx, v):
                    raise ApiError(400, f"bad {k}")
                vals[k] = v or None
            for k in ("pid", "restart_count", "started_at"):
                v = b.get(k, r[k])
                if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                    raise ApiError(400, f"{k} must be a whole number")
                vals[k] = v
            if "last_exit" in b:
                vals["last_exit"] = clean_text(b["last_exit"], 300, "last_exit")
            if isinstance(b.get("log_tail"), str):
                vals["log_tail"] = self.scrub_tail(b["log_tail"][-ORCH_TAIL_MAX:])
                vals["log_tail_at"] = now()
            events = b.get("events") or []
            if not isinstance(events, list) or len(events) > 50:
                raise ApiError(400, "events must be a list of at most 50")
            cols = ", ".join(f"{k} = ?" for k in vals)
            self.s.run(f"UPDATE orchestrators SET {cols} WHERE name = ?", *vals.values(), name)
            for e in events:
                typ = clean_text((e or {}).get("type"), 30, "event type") if isinstance(e, dict) else None
                if typ not in ORCH_EVENT_TYPES:
                    raise ApiError(400, f"bad event type {typ!r}")
                self.orch_event(name, typ, worker, self.scrub(clean_text(e.get("text"), 500, "event text")))
            fresh = self.s.one("SELECT * FROM orchestrators WHERE name = ?", name)
            if r["state"] == "unreachable" and state != "unreachable":
                self.orch_event(name, "recovered", "backend", f"the supervisor is reporting again ({state})")
            if state == "needs-login" and r["state"] != "needs-login":
                self.notify_orch(fresh, "needs-login", f"Orchestrator {name} on worker {worker} needs a Claude sign-in and was "
                                                       f"stopped; queued messages wait. {fresh['last_exit'] or ''} ".strip())
            if state == "crash-loop" and r["state"] != "crash-loop":
                self.notify_orch(fresh, "crash-loop", f"Orchestrator {name} crashed too often and was not restarted. "
                                                      f"Last exit: {fresh['last_exit'] or 'unknown'}. Restart it with: "
                                                      f"swarmctl orchestrator restart {name}")
            ack = b.get("delivered_message_id")
            if isinstance(ack, int) and not isinstance(ack, bool):
                try:
                    self.bus.on_orchestrator_delivered(ack)
                except Exception as e:
                    print("bus delivered mark failed:", repr(e), file=sys.stderr, flush=True)
                if self.s.run("UPDATE orchestrator_messages SET delivered_at = ? WHERE id = ? AND name = ? AND delivered_at IS NULL",
                              now(), ack, name).rowcount:
                    self.orch_event(name, "message-delivered", worker, f"message {ack} typed into the session")
                    waited = b.get("delivered_busy_seconds")
                    if isinstance(waited, int) and not isinstance(waited, bool) and waited >= 0:
                        # In the same first-delivery branch, so a replayed receipt never records it twice.
                        self.orch_event(name, "message-delivered-busy", worker,
                                        f"message {ack} typed while the session was busy, after {waited}s (queued by Claude Code)")
            msg = None
            if not r["deleted_at"] and r["desired"] == "running":
                msg = self.s.one("SELECT id, at, sender, text, release_count AS release FROM orchestrator_messages"
                                 " WHERE name = ? AND delivered_at IS NULL ORDER BY id LIMIT 1", name)
        return {"desired": "stopped" if r["deleted_at"] else r["desired"], "deleted": bool(r["deleted_at"]),
                "generation": r["generation"], "restart_generation": r["restart_generation"], "message": msg,
                "config": {k: r[k] for k in ("project", "repo", "provider", "model", "brief", "relay_agent_name", "session_id")}
                | {"secret_bundles": json.loads(r["secret_bundles"] or "[]")}}

    def orchestrator_watch(self):
        """Mark orchestrators whose supervisor went quiet as unreachable, and tell the owner once."""
        with self.s.tx():
            for r in self.s.q("SELECT * FROM orchestrators WHERE deleted_at IS NULL AND desired = 'running'"
                              " AND state NOT IN ('unreachable', 'crash-loop')"
                              " AND max(coalesce(last_heartbeat_at, 0), coalesce(desired_at, 0)) < ?", now() - ORCH_STALE_SECONDS):
                self.s.run("UPDATE orchestrators SET state = 'unreachable', updated_at = ? WHERE name = ?", now(), r["name"])
                last = f"{(now() - r['last_heartbeat_at']) // 60} minutes ago" if r["last_heartbeat_at"] else "never"
                self.orch_event(r["name"], "unreachable", "backend", f"no heartbeat from worker {r['worker_id']}; last one {last}")
                self.notify_orch(dict(r, state="unreachable"), "unreachable",
                                 f"Orchestrator {r['name']} on worker {r['worker_id']} has not reported for over "
                                 f"{ORCH_STALE_SECONDS // 60} minutes (last heartbeat {last}). Check: "
                                 f"systemctl status swarm-orchestrator@{r['name']}")

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
                stamps = [os.path.getmtime(os.path.join(bdir, f)) for f in os.listdir(bdir) if f.endswith(".db.gz")]
                if stamps:
                    if max(stamps) < now() - 36 * 3600:
                        out["warnings"].append("The newest database backup is older than 36 hours. Check: systemctl status swarm-backup")
                else:
                    # A new install has no backup until the first nightly run, so give it 36 hours before warning.
                    created = int((self.s.one("SELECT value FROM meta WHERE key = 'created_at'") or {}).get("value") or 0)
                    if created < now() - 36 * 3600:
                        out["warnings"].append("There is no database backup yet. Check: systemctl status swarm-backup")
        else:
            out["notices"] = {}
        out["orchestrators"] = [{k: o[k] for k in ("name", "state", "desired", "worker_id", "last_heartbeat_at", "restart_count")}
                                for o in self.list_orchs(who)["orchestrators"]]
        for o in out["orchestrators"]:
            if o["desired"] == "running" and o["state"] in ("unreachable", "crash-loop", "blocked", "needs-login"):
                out["warnings"].append(f"Orchestrator {o['name']} is {o['state']}. See: swarmctl orchestrator status {o['name']}")
        return out


def day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def log_line(e):
    """One entry as a Markdown list line: kind, record state, summary, who."""
    tag = e["kind"]
    if e.get("ref"):
        tag += f" {e['ref']}" + (f" v{e['version']}" if e.get("version") else "")
    if e.get("status"):
        tag += f" {e['status']}"
    line = f"- **{tag}** {e['summary']}"
    if e.get("by"):
        line += f" (by {e['by']}" + (f", via {e['bot']})" if e.get("bot") and e["bot"] != e["by"] else ")")
    elif e.get("bot"):
        line += f" ({e['bot']})"
    if e.get("task_id"):
        line += f" task `{e['task_id']}`"
    return line


def log_markdown(project, entries, since=None, title=None):
    """A readable digest of one project's log, grouped by day, newest day last."""
    sub = "###" if title else "##"
    head = (title or f"# Project log: {project}") + "\n\n"
    head += (f"Entries since {day(since)} (UTC). " if since else "All entries (UTC). ")
    counts = {}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    head += ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ".\n" if counts else "No entries.\n"
    out, last = [head], None
    for e in entries:
        d = day(e["created_at"])
        if d != last:
            out.append(f"\n{sub} {d}\n")
            last = d
        out.append(log_line(e))
        if e.get("answer"):
            out.append(f"  Answer: {e['answer']}" + (f" (source: {e['source']})" if e.get("source") else ""))
        if e["kind"] == "rule" and (e.get("before") or e.get("after")):
            out.append(f"  Before: {e.get('before') or '(none)'}")
            out.append(f"  After: {e.get('after') or '(removed)'}")
        for ln in (e.get("detail") or "").strip().splitlines()[:12]:
            out.append("  " + ln if ln.strip() else "")
        for link in e.get("links") or []:
            out.append(f"  - {link}")
    return "\n".join(out).rstrip() + "\n"


def rules_markdown(r):
    """The short rules doc for one project, with its changelog. r is the shape log_rules returns."""
    out = [f"# Rules: {r['project']}\n",
           f"{len(r['rules'])} of at most {r['cap']} rules. Read these before drafting. They change only through an approved rule change.\n"]
    out += [f"{n}. {x['after']} ({x['ref']}, since {day(x['created_at'])})" for n, x in enumerate(r["rules"], 1)] or ["No rules yet."]
    if r["pending"]:
        out.append("\n## Waiting for review\n")
        out += [f"- change {p['id']} ({p['ref']}): {p.get('before') or '(new rule)'} -> {p.get('after') or '(remove)'}"
                for p in r["pending"]]
    if r["changelog"]:
        out.append("\n## Changelog\n")
        out += [f"- {day(c['created_at'])} {c['status']} by {c.get('by') or '-'}: {c['ref']}: "
                f"{c.get('before') or '(new rule)'} -> {c.get('after') or '(removed)'}" for c in r["changelog"]]
    return "\n".join(out) + "\n"


def git_commit(folder, message):
    """Commit a project's export folder to its own local git repo, so each week shows as a diff."""
    import subprocess
    try:
        if not os.path.isdir(os.path.join(folder, ".git")):
            subprocess.run(["git", "init", "-q"], cwd=folder, check=True, capture_output=True, timeout=60)
        swarm_gitlink.commit_leftover(folder, message, allow_gitlink=False,
                                      log=lambda m: print("log export:", m, file=sys.stderr, flush=True))
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        # commit_leftover already redacts its own messages; redact again so an OSError's path text is covered too.
        print("log export: git commit failed:", type(e).__name__, swarm_gitlink.redact(str(e)), file=sys.stderr, flush=True)


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
    # backend.bus.wake is set when a webhook message is written, so the first delivery try runs at once, not on the next tick.
    while not stop.is_set():
        backend.bus.wake.wait(5)
        backend.bus.wake.clear()
        if stop.is_set():
            break
        for job in (backend.reap, lambda: deliver_once(backend), backend.bus.flush, backend.escalate, backend.watchdog, backend.retention,
                    backend.weekly_export, backend.orchestrator_watch):
            try:
                job()
            except Exception as e:  # keep the loop alive
                print("background error:", repr(e), file=sys.stderr, flush=True)


# ---------- HTTP ----------
ROUTES = []
EVERYONE = ("admin", "chief", "bot", "worker", "task", "agent")


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


@route("GET", r"/tasks/([^/]+)", ("admin", "chief", "bot", "task", "agent"), "One task with its event thread. Bot: own only. Task token: own task only. Bus agent: tasks its bot owns")
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


@route("GET", r"/tasks/([^/]+)/report", ("admin", "chief", "bot", "agent", "task"), "REPORT.md as text. Bot: own only. Bus agent: tasks its bot owns. Task token: own task or its parent")
def r_report(api, who, m, qs, body, h):
    api.get_readable_task(who, m[0], "report")
    f = api.s.one("SELECT data FROM files WHERE task_id = ? AND kind = 'report' ORDER BY at DESC LIMIT 1", m[0])
    if not f:
        raise ApiError(404, "no report yet")
    return ("text/markdown; charset=utf-8", bytes(f["data"]))


@route("GET", r"/tasks/([^/]+)/(artifacts|attachments)", ("admin", "chief", "bot", "task"), "List files. Bot and task token: own only (a task token may also list its parent's artifacts)")
def r_files(api, who, m, qs, body, h):
    api.get_readable_task(who, m[0], m[1])
    kind = "artifact" if m[1] == "artifacts" else "attachment"
    return {"files": api.s.q("SELECT name, size, at FROM files WHERE task_id = ? AND kind = ? ORDER BY name", m[0], kind)}


@route("GET", r"/tasks/([^/]+)/(artifacts|attachments)/([^/]+)", ("admin", "chief", "bot", "task"), "Download one file. Bot and task token: own only (a task token may also read its parent's artifacts)")
def r_file(api, who, m, qs, body, h):
    api.get_readable_task(who, m[0], m[1])
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


@route("POST", r"/log", ("admin", "chief", "bot", "task"),
       "Add a project log entry. Bot: projects it is bound to. Task token: its own task's project")
def r_log_add(api, who, m, qs, body, h):
    return api.post_log(who, body)


@route("GET", r"/log", ("admin", "chief", "bot"), "Read project log entries (`project`, `kind`, `since`, `limit`). Bot: its bound projects only")
def r_log_list(api, who, m, qs, body, h):
    return api.list_log(who, qs)


@route("GET", r"/log/export", ("admin", "chief", "bot"), "One project's log as a Markdown digest. Bot: its bound projects only")
def r_log_export(api, who, m, qs, body, h):
    return ("text/markdown; charset=utf-8", api.export_log(who, qs).encode())


@route("POST", r"/activity-log", ("admin", "chief", "bot", "task"), "Append one activity entry. Bot: bound projects only. Task: own project with opt-in")
def r_activity_add(api, who, m, qs, body, h):
    return api.post_activity(who, body)


@route("GET", r"/activity-log", ("admin", "chief", "bot"), "Read recent activity (`project`, `limit`, `bot`, `kind`, `decider`, `since`). Bot: bound projects only")
def r_activity_list(api, who, m, qs, body, h):
    return api.list_activity(who, qs)


@route("GET", r"/activity-log/export", ("admin", "chief", "bot"), "Export a Central Time month (`project`, `month`) as Markdown")
def r_activity_export(api, who, m, qs, body, h):
    return ("text/markdown; charset=utf-8", api.export_activity(who, qs).encode())


@route("GET", r"/activity-log/snapshot", ("admin", "chief", "bot"), "Nightly activity files changed since the last published entry (`project`, `after` overrides it)")
def r_activity_snapshot(api, who, m, qs, body, h):
    return api.activity_snapshot(who, qs)


@route("POST", r"/activity-log/published", ("admin", "chief"), "Save the nightly export position (`project`, `through`) after a verified push")
def r_activity_published(api, who, m, qs, body, h):
    return api.mark_activity_published(who, body)


@route("GET", r"/log/items", ("admin", "chief", "bot"), "Current state of each item (deliverable) in a project. Bot: bound projects only")
def r_log_items(api, who, m, qs, body, h):
    return api.log_items(who, qs)


@route("GET", r"/log/rules", ("admin", "chief", "bot"), "A project's standing rules, rule changes waiting for review, and the changelog "
       "(`format=md` for the rules doc)")
def r_log_rules(api, who, m, qs, body, h):
    r = api.log_rules(who, qs)
    return ("text/markdown; charset=utf-8", rules_markdown(r).encode()) if qs.get("format") == "md" else r


@route("GET", r"/log/brief", ("admin", "chief", "bot"), "Read before drafting: standing rules plus the last 14 days of feedback, as Markdown")
def r_log_brief(api, who, m, qs, body, h):
    return ("text/markdown; charset=utf-8", api.log_brief(who, qs).encode())


@route("GET", r"/orchestrators", ("admin", "chief", "bot"), "List orchestrators with state, worker and last heartbeat. Bot: the ones it owns")
def r_orch_list(api, who, m, qs, body, h):
    return api.list_orchs(who)


@route("POST", r"/orchestrators", ("admin",), "Create an orchestrator pinned to one worker (secret bundles by name only). `start: true` starts it")
def r_orch_create(api, who, m, qs, body, h):
    return api.create_orch(who, body)


@route("GET", r"/orchestrators/([^/]+)", ("admin", "chief", "bot"), "One orchestrator with its brief and last 20 events. Bot: own only")
def r_orch_get(api, who, m, qs, body, h):
    return api.show_orch(who, m[0])


@route("PUT", r"/orchestrators/([^/]+)", ("admin",), "Change an orchestrator's settings; they apply at the next start or restart")
def r_orch_change(api, who, m, qs, body, h):
    return api.change_orch(who, m[0], body)


@route("POST", r"/orchestrators/([^/]+)/(start|stop|restart|delete)", ("admin",),
       "Set it to run, stop it gracefully, restart it (also clears a crash loop), or delete a stopped one")
def r_orch_control(api, who, m, qs, body, h):
    return api.control_orch(who, m[0], m[1])


@route("GET", r"/orchestrators/([^/]+)/logs", ("admin", "chief", "bot"), "The last lines of its terminal (`lines`, up to 500), secret-scanned. Bot: own only")
def r_orch_logs(api, who, m, qs, body, h):
    return api.orch_logs(who, m[0], qs)


@route("POST", r"/orchestrators/([^/]+)/messages", ("admin", "chief", "bot"), "Queue one message (`text`, up to 4000 characters); "
       "typed into the session when the agent is idle. One at a time. Bot: own only")
def r_orch_message(api, who, m, qs, body, h):
    return api.orch_message(who, m[0], body)


@route("POST", r"/orchestrators/([^/]+)/messages/([^/]+)/release", ("admin",), "After a delivery-unknown event: mark the message "
       "delivered (default), or `resend: true` to have the supervisor type it once more")
def r_orch_release(api, who, m, qs, body, h):
    if not m[1].isdigit():
        raise ApiError(404, "no such message")
    return api.orch_release(who, m[0], int(m[1]), body)


@route("POST", r"/worker/orchestrators/([^/]+)/heartbeat", ("worker",), "Supervisor on the pinned worker only: report state and the "
       "terminal tail, get the desired state, settings and one pending message")
def r_orch_heartbeat(api, who, m, qs, body, h):
    return api.orch_heartbeat(who, m[0], body)


@route("GET", r"/notices", ("admin", "chief"), "The last 100 deliveries with state and last error")
def r_notices(api, who, m, qs, body, h):
    return {"notices": api.s.q("SELECT id, key, target, reason, event, state, attempts, last_error, created_at, sent_at"
                               " FROM notices ORDER BY id DESC LIMIT 100")}


# ---- Agent Bus (docs/agent-bus.md) ----
def bus_who(who, raw=None, bus=None):
    """The agent a request acts as: the token's agent, or (admin) the one named."""
    if who["role"] == "agent":
        return who["agent"]
    return bus.resolve(raw) if raw else None


@route("POST", r"/bus/send", ("admin", "agent"), "Send a direct message to an agent (`to`, `body`, `thread`, `client_msg_id`, `urgent`, `actionable`, `noop`, "
       "`reply_expected`, `meta`). Stored first, then woken. Admin sets `from`")
def r_bus_send(api, who, m, qs, body, h):
    if who["role"] == "agent":
        frm = who["agent"]["name"]
    else:
        f = api.bus.resolve(body.get("from")) if body.get("from") else None
        frm = f["name"] if f else swarm_bus.clean_from(body.get("from"))
    status, data = api.bus.send({"from": frm, "to": body.get("to"), "body": body.get("body"), "thread": body.get("thread"),
                                 "client_msg_id": body.get("client_msg_id"), "urgent": body.get("urgent") is True,
                                 "noop": body.get("noop") is True, "actionable": body.get("actionable"),
                                 "reply_expected": body["reply_expected"] if isinstance(body.get("reply_expected"), bool) else None,
                                 "meta": body.get("meta")})
    return Reply(status, data)


@route("GET", r"/bus/inbox", ("admin", "agent"), "An agent's undelivered and unread messages (`all=1`, `after`, `limit`). Reading marks them delivered. Admin names `agent`")
def r_bus_inbox(api, who, m, qs, body, h):
    a = bus_who(who, qs.get("agent"), api.bus)
    if not a:
        return Reply(400, {"error": "agent= is required for the admin token"})
    return api.bus.inbox(who, a, qs)


def bus_message(api, who, raw_id, ack):
    msg = api.s.one("SELECT * FROM bus_messages WHERE id = ?", int(raw_id)) if raw_id.isdigit() else None
    party = msg and (who["role"] == "admin" or msg["recipient"] == who["agent"]["name"] or (not ack and msg["sender"] == who["agent"]["name"]))
    if not party:
        raise ApiError(404, "not found")
    return msg


@route("GET", r"/bus/messages/([^/]+)", ("admin", "agent"), "One message with its delivery attempts. Agent: sender or recipient")
def r_bus_message(api, who, m, qs, body, h):
    return api.bus.message_detail(bus_message(api, who, m[0], False))


@route("POST", r"/bus/messages/([^/]+)/ack", ("admin", "agent"), "Mark a message read. `first` is true only once; also returns `newer_in_thread`. Agent: recipient only")
def r_bus_ack(api, who, m, qs, body, h):
    return api.bus.ack(bus_message(api, who, m[0], True))


@route("GET", r"/bus/threads/([^/]+)", ("admin", "agent"), "The last messages of a thread (`limit`, up to 100). Agent: only ones it sent or received")
def r_bus_thread(api, who, m, qs, body, h):
    th = urllib.parse.unquote(m[0])
    lim = api.bus._int(qs.get("limit"), 100, 100)
    if who["role"] == "admin":
        rows = api.s.q("SELECT * FROM (SELECT * FROM bus_messages WHERE thread = ? ORDER BY id DESC LIMIT ?) ORDER BY id", th, lim)
    else:
        me = who["agent"]["name"]
        rows = api.s.q("SELECT * FROM (SELECT * FROM bus_messages WHERE thread = ? AND (sender = ? OR recipient = ?) ORDER BY id DESC LIMIT ?)"
                       " ORDER BY id", th, me, me, lim)
    return {"thread": th, "messages": [api.bus.shape_msg(r) for r in rows]}


@route("GET", r"/bus/agents", ("admin", "agent"), "Agents with kind, wake, aliases and whether they are reachable")
def r_bus_agents(api, who, m, qs, body, h):
    return {"agents": [api.bus.shape_agent(a) for a in api.s.q("SELECT * FROM agents WHERE retired_at IS NULL ORDER BY name")]}


@route("PUT", r"/bus/agents/([^/]+)", ("admin",), "Register or change an agent (`kind`, `wake`, `bot_id`, `orchestrator`, `description`, `aliases`, `notices`, `digest_minutes`, `poll_window_minutes`)")
def r_bus_put_agent(api, who, m, qs, body, h):
    return Reply(*api.bus.put_agent(swarm_bus.norm_name(urllib.parse.unquote(m[0])), body))


@route("DELETE", r"/bus/agents/([^/]+)", ("admin",), "Retire an agent. Its history stays")
def r_bus_retire(api, who, m, qs, body, h):
    n = swarm_bus.norm_name(urllib.parse.unquote(m[0]))
    t = now()
    if not api.s.run("UPDATE agents SET retired_at = ?, updated_at = ? WHERE name = ? AND retired_at IS NULL", t, t, n).rowcount:
        raise ApiError(404, "not found")
    return {"ok": True, "name": n}


@route("GET", r"/bus/agents/([^/]+)/token", ("admin", "worker"), "The agent's token. Admin: any. Worker token: the `worker` query is required and the agent's orchestrator must be pinned to that worker name (the name is self-declared: the kit has one shared worker token)")
def r_bus_token(api, who, m, qs, body, h):
    a = api.bus.resolve(swarm_bus.norm_name(urllib.parse.unquote(m[0])))
    if not a:
        raise ApiError(404, "not found")
    if a["retired_at"]:
        raise ApiError(410, f"agent {a['name']} is retired")
    if who["role"] != "admin":
        o = api.orch_row(a["orchestrator"]) if a["orchestrator"] else None
        if not o:
            raise ApiError(404, "not found")
        if not qs.get("worker"):
            raise ApiError(400, "the worker query (your worker name) is required")
        if o["worker_id"] != qs["worker"]:
            raise ApiError(404, "not found")
    return {"name": a["name"], "token": api.bus.mint(a["name"], a["token_gen"])}


@route("POST", r"/bus/agents/([^/]+)/token/rotate", ("admin",), "Revoke every token of the agent and return a new one")
def r_bus_rotate(api, who, m, qs, body, h):
    a = api.bus.resolve(swarm_bus.norm_name(urllib.parse.unquote(m[0])))
    if not a:
        raise ApiError(404, "not found")
    if a["retired_at"]:
        raise ApiError(410, f"agent {a['name']} is retired")
    api.s.run("UPDATE agents SET token_gen = token_gen + 1, updated_at = ? WHERE name = ?", now(), a["name"])
    return {"name": a["name"], "token": api.bus.token_for(a["name"])}


@route("GET", r"/bus", ("admin",), "Plain HTML view of agents and the last 100 messages. Read only; admin token only (a reverse proxy makes every request look like loopback, so there is no loopback exception)")
def r_bus_page(api, who, m, qs, body, h):
    return ("text/html; charset=utf-8", api.bus.page().encode())


# ---- Review queue (docs/review-queue.md) ----
REVIEW_FILERS = ("admin", "chief", "bot", "agent")


def review_call(fn, *args):
    try:
        return fn(*args)
    except swarm_review.ReviewError as e:
        raise ApiError(e.status, e.message, e.extra)


@route("PUT", r"/review/items", REVIEW_FILERS, "File or update a review item, upsert by (`project`, `bot`, `key`). Bot and bus agent: as itself. "
       "Admin and chief name `bot`")
def r_review_put(api, who, m, qs, body, h):
    return Reply(*review_call(api.review.upsert, who, body))


@route("POST", r"/review/items", REVIEW_FILERS, "Same as PUT /review/items")
def r_review_post(api, who, m, qs, body, h):
    return Reply(*review_call(api.review.upsert, who, body))


@route("GET", r"/review/items", REVIEW_FILERS, "List review items (`status`, `acked=0`, `project`, `key`, `type`, `limit`; admin and chief: `bot`). "
       "Bot and bus agent: own only")
def r_review_list(api, who, m, qs, body, h):
    return review_call(api.review.list, who, qs)


@route("GET", r"/review/items/([^/]+)", REVIEW_FILERS, "One review item with its `content_hash` and decision. Bot and bus agent: own only")
def r_review_get(api, who, m, qs, body, h):
    return review_call(api.review.detail, who, m[0])


@route("POST", r"/review/items/([^/]+)/decision", ("admin", "chief"), "Decide: `action` approve, changes or reject, with the `content_hash` "
       "read from the item (409 stale and nothing recorded on a mismatch), `note`, `edited_text`, `option`, `revision`")
def r_review_decide(api, who, m, qs, body, h):
    return review_call(api.review.decide, who, m[0], body)


@route("POST", r"/review/items/([^/]+)/ack", REVIEW_FILERS, "Acknowledge a decision once consumed (`revision`). Bot and bus agent: own only")
def r_review_ack(api, who, m, qs, body, h):
    return review_call(api.review.ack, who, m[0], body)


@route("POST", r"/review/items/([^/]+)/close", REVIEW_FILERS, "Withdraw an open item (`reason`). Bot and bus agent: own only")
def r_review_close(api, who, m, qs, body, h):
    return review_call(api.review.close, who, m[0], body)


def permission_table():
    """The permission matrix, as published in docs/backend.md."""
    cols = list(EVERYONE)
    lines = ["| Method | Path | " + " | ".join(cols) + " | What it does |",
             "| --- | --- | " + " | ".join("---" for _ in cols) + " | --- |"]
    for r in ROUTES:
        path = r["pattern"].replace("([^/]+)", "ID").replace("(artifacts|attachments)", "artifacts or attachments") \
            .replace("(report|artifact|bundle)", "KIND").replace("(start|stop|restart|delete)", "ACTION").replace("\\", "")
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
            self.headers.peer = self.client_address[0]
            res = r["fn"](self.api, who, match.groups(), qs, body, self.headers)
            if isinstance(res, Reply):
                return self._send(res.status, res.data)
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

    def do_DELETE(self):
        self._handle("DELETE")


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
