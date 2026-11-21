"""Migration safety for the Agent Bus tables and columns (agent-control migrations 0026 and 0027 in the kit's in-place style).
Old databases are built by running the kit's own schema text with the bus tables cut out (a 0.7.1 database), or with the bus tables but
without the wake-policy columns (a database from before the columns were added)."""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import swarm_backend as sb  # noqa: E402

BUS_TABLES = ("agents", "agent_aliases", "bus_messages", "bus_attempts")
AGENT_NEW = ("digest_minutes", "last_digest_at", "unreachable_since", "alerted_at", "escalated_at")
MSG_NEW = ("wake", "held", "noop", "reply_expected")


def schema_without_bus():
    text = sb.SCHEMA_TABLES
    for t in BUS_TABLES:
        text = re.sub(rf"CREATE TABLE IF NOT EXISTS {t}\(.*?\);\n?", "", text, flags=re.S)
    return text


def cols(conn, table):
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}   # name -> cid, name, type, notnull, dflt, pk


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


class SchemaBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "old.db")

    def raw(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def open_store(self):
        st = sb.Store(self.path)
        self.addCleanup(st.db().close)
        return st

    def old_071(self):
        """A 0.7.1 database: no bus tables, no bus columns, some rows in tasks, bots and orchestrator_messages."""
        conn = self.raw()
        conn.executescript(schema_without_bus())
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '7')")
        conn.execute("INSERT INTO meta(key, value) VALUES('created_at', '1700000000')")
        conn.execute("INSERT INTO bots(id, role, notify_url, created_at, updated_at) VALUES('web', 'project', 'http://127.0.0.1:9/web', 1, 2)")
        conn.execute("INSERT INTO tasks(id, created_at, updated_at, owner, kind, prompt, state, outcome, pr_url) "
                     "VALUES('t1', 10, 11, 'web', 'code', 'Ship it', 'done', 'done', 'https://github.com/acme/site/pull/1')")
        conn.execute("INSERT INTO tasks(id, created_at, updated_at, prompt, state) VALUES('t2', 12, 13, 'Queued one', 'queued')")
        conn.execute("INSERT INTO orchestrator_messages(name, at, sender, text, delivered_at) VALUES('steward', 5, 'admin', 'hello', 6)")
        conn.execute("INSERT INTO orchestrator_messages(name, at, sender, text) VALUES('steward', 7, 'admin', 'not delivered yet')")
        conn.commit()
        return conn


class MigrateFrom071Test(SchemaBase):
    def setUp(self):
        super().setUp()
        conn = self.old_071()
        self.assertTrue(set(BUS_TABLES).isdisjoint(tables(conn)), "the fixture really is a pre-bus database")
        self.assertNotIn("bus_message_id", cols(conn, "orchestrator_messages"))
        self.assertNotIn("head_sha", cols(conn, "tasks"))
        conn.close()
        self.st = self.open_store()
        self.conn = self.raw()

    def test_bus_tables_exist(self):
        self.assertTrue(set(BUS_TABLES) <= tables(self.conn))

    def test_agents_has_every_column_with_defaults(self):
        c = cols(self.conn, "agents")
        for name in ("name", "kind", "wake", "bot_id", "orchestrator", "description", "token_gen", "notices", "last_seen_at", "created_at",
                     "updated_at", "retired_at") + AGENT_NEW:
            self.assertIn(name, c)
        self.assertEqual(c["digest_minutes"][2:5], ("INTEGER", 1, "60"))
        for name in ("last_digest_at", "unreachable_since", "alerted_at", "escalated_at"):
            self.assertEqual((c[name][3], c[name][4]), (0, None), name)
        self.assertEqual(c["token_gen"][4], "1")
        self.assertEqual(c["notices"][4], "'legacy'")

    def test_bus_messages_has_every_column_with_defaults(self):
        c = cols(self.conn, "bus_messages")
        for name in ("id", "sender", "recipient", "thread", "client_msg_id", "body", "meta", "urgent", "state", "created_at", "delivered_at",
                     "read_at", "attempts", "next_attempt_at", "lease_until", "last_error", "alerted_at") + MSG_NEW:
            self.assertIn(name, c)
        self.assertEqual((c["wake"][2], c["wake"][3]), ("TEXT", 0))
        for name in ("held", "noop", "reply_expected"):
            self.assertEqual(c[name][2:5], ("INTEGER", 1, "0"), name)

    def test_attempts_and_aliases_tables(self):
        self.assertEqual(set(cols(self.conn, "agent_aliases")), {"alias", "name", "created_at"})
        self.assertEqual(set(cols(self.conn, "bus_attempts")),
                         {"id", "message_id", "transport", "started_at", "finished_at", "ok", "http_status", "error"})

    def test_orchestrator_messages_and_tasks_gain_their_columns(self):
        self.assertIn("bus_message_id", cols(self.conn, "orchestrator_messages"))
        for name in ("head_sha", "unpushed_commits"):
            self.assertIn(name, cols(self.conn, "tasks"))

    def test_existing_rows_are_untouched(self):
        self.assertEqual(self.conn.execute("SELECT id, role, notify_url, created_at, updated_at FROM bots").fetchall(),
                         [("web", "project", "http://127.0.0.1:9/web", 1, 2)])
        self.assertEqual(self.conn.execute("SELECT id, created_at, updated_at, owner, kind, prompt, state, outcome, pr_url, head_sha,"
                                           " unpushed_commits FROM tasks ORDER BY id").fetchall(),
                         [("t1", 10, 11, "web", "code", "Ship it", "done", "done", "https://github.com/acme/site/pull/1", None, None),
                          ("t2", 12, 13, None, None, "Queued one", "queued", None, None, None, None)])
        self.assertEqual(self.conn.execute("SELECT name, at, sender, text, delivered_at, bus_message_id FROM orchestrator_messages"
                                           " ORDER BY id").fetchall(),
                         [("steward", 5, "admin", "hello", 6, None), ("steward", 7, "admin", "not delivered yet", None, None)])
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key = 'created_at'").fetchone()[0], "1700000000")

    def test_schema_version_is_8(self):
        self.assertEqual(sb.SCHEMA_VERSION, 8)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "8")

    def test_indexes_exist(self):
        idx = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
        for name in ("idx_bus_messages_recipient", "idx_bus_messages_thread", "idx_bus_messages_state", "idx_bus_attempts_message",
                     "bus_messages_created", "bus_attempts_started", "bus_messages_held"):
            self.assertIn(name, idx)

    def test_the_new_defaults_apply_to_new_rows(self):
        self.st.run("INSERT INTO agents(name, kind, wake, created_at, updated_at) VALUES('chief', 'bot', 'poll', 1, 1)")
        a = self.st.one("SELECT * FROM agents WHERE name = 'chief'")
        self.assertEqual((a["digest_minutes"], a["token_gen"], a["notices"], a["last_digest_at"], a["unreachable_since"], a["alerted_at"],
                          a["escalated_at"]), (60, 1, "legacy", None, None, None, None))
        self.st.run("INSERT INTO bus_messages(sender, recipient, thread, body, created_at) VALUES('a', 'chief', 'dm:a+chief', 'hi', 1)")
        m = self.st.one("SELECT * FROM bus_messages")
        self.assertEqual((m["state"], m["urgent"], m["held"], m["noop"], m["reply_expected"], m["wake"], m["attempts"]),
                         ("queued", 0, 0, 0, 0, None, 0))

    def test_check_constraints_reject_bad_values(self):
        bad = [
            "INSERT INTO agents(name, kind, wake, created_at, updated_at) VALUES('x1', 'robot', 'poll', 1, 1)",
            "INSERT INTO agents(name, kind, wake, created_at, updated_at) VALUES('x2', 'bot', 'carrier-pigeon', 1, 1)",
            "INSERT INTO agents(name, kind, wake, notices, created_at, updated_at) VALUES('x3', 'bot', 'poll', 'maybe', 1, 1)",
            "INSERT INTO bus_messages(sender, recipient, thread, body, state, created_at) VALUES('a', 'b', 't', 'x', 'lost', 1)",
            "INSERT INTO bus_attempts(message_id, transport, started_at) VALUES(1, 'smoke-signal', 1)",
            "INSERT INTO agents(name, wake, created_at, updated_at) VALUES('x4', 'poll', 1, 1)",
            "INSERT INTO bus_messages(sender, recipient, thread, created_at) VALUES('a', 'b', 't', 1)",
        ]
        for sql in bad:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.st.run(sql)
        self.assertEqual(self.st.one("SELECT COUNT(*) AS n FROM agents")["n"], 0)
        self.assertEqual(self.st.one("SELECT COUNT(*) AS n FROM bus_messages")["n"], 0)

    def test_good_values_are_accepted_for_every_kind_wake_and_state(self):
        for i, kind in enumerate(("bot", "orchestrator", "human", "service")):
            for j, wake in enumerate(("webhook", "tmux", "poll")):
                self.st.run("INSERT INTO agents(name, kind, wake, created_at, updated_at) VALUES(?,?,?,1,1)", f"a{i}{j}", kind, wake)
        for k, state in enumerate(("queued", "delivered", "read", "failed")):
            self.st.run("INSERT INTO bus_messages(sender, recipient, thread, body, state, created_at) VALUES('a','b','t','x',?,1)", state)
        for transport in ("webhook", "tmux", "poll"):
            self.st.run("INSERT INTO bus_attempts(message_id, transport, started_at) VALUES(1, ?, 1)", transport)
        self.assertEqual(self.st.one("SELECT COUNT(*) AS n FROM agents")["n"], 12)

    def test_client_msg_id_is_unique_per_sender(self):
        ins = "INSERT INTO bus_messages(sender, recipient, thread, client_msg_id, body, created_at) VALUES(?, 'b', 't', ?, 'x', 1)"
        self.st.run(ins, "a", "k1")
        self.st.run(ins, "c", "k1")
        self.st.run(ins, "a", None)
        self.st.run(ins, "a", None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.st.run(ins, "a", "k1")

    def test_a_backend_opens_on_the_migrated_database(self):
        self.st.db().close()
        self.st.local.conn = None
        env = {"SWARM_ADMIN_TOKEN_FILE": os.path.join(self.tmp, "admin"), "SWARM_BACKEND_WORKER_TOKEN_FILE": os.path.join(self.tmp, "worker"),
               "SWARM_BOT_KEYS_DIR": os.path.join(self.tmp, "bot-keys"), "SWARM_QUIET": "1"}
        for name in ("admin", "worker"):
            with open(os.path.join(self.tmp, name), "w") as fh:
                fh.write(name + "-token-for-the-schema-test")
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            srv, api = sb.make_server(self.path, "127.0.0.1:0", "https://swarm.example")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(srv.server_close)
        api.s.run("INSERT INTO agents(name, kind, wake, created_at, updated_at) VALUES('chief', 'bot', 'poll', 1, 1)")
        st, data = api.bus.send({"from": "agent-control", "to": "chief", "body": "hello"})
        self.assertEqual((st, data["wake"], data["state"]), (201, "inbox", "queued"))
        self.assertEqual(api.s.one("SELECT COUNT(*) AS n FROM tasks")["n"], 2)


class IdempotentMigrationTest(SchemaBase):
    def snapshot(self):
        conn = sqlite3.connect(self.path)
        try:
            schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
            rows = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("tasks", "bots", "orchestrator_messages", "agents", "bus_messages", "bus_attempts", "agent_aliases")}
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        return schema, rows, meta

    def test_opening_twice_changes_nothing(self):
        self.old_071().close()
        self.open_store()
        first = self.snapshot()
        self.open_store()
        self.assertEqual(self.snapshot(), first)

    def test_opening_a_fresh_file_twice_changes_nothing(self):
        self.open_store()
        first = self.snapshot()
        self.assertEqual(first[2]["schema_version"], "8")
        self.open_store()
        self.assertEqual(self.snapshot(), first)

    def test_a_fresh_file_and_a_migrated_one_end_with_the_same_columns(self):
        self.old_071().close()
        self.open_store()
        conn = self.raw()
        migrated = {t: [(n, r[2], r[3], r[4], r[5]) for n, r in sorted(cols(conn, t).items())] for t in BUS_TABLES + ("orchestrator_messages", "tasks")}
        fresh_path = os.path.join(self.tmp, "fresh.db")
        st = sb.Store(fresh_path)
        self.addCleanup(st.db().close)
        fresh = sqlite3.connect(fresh_path)
        self.addCleanup(fresh.close)
        self.assertEqual({t: [(n, r[2], r[3], r[4], r[5]) for n, r in sorted(cols(fresh, t).items())] for t in migrated}, migrated)

    def test_data_written_between_opens_survives(self):
        self.old_071().close()
        st = self.open_store()
        st.run("INSERT INTO agents(name, kind, wake, created_at, updated_at, digest_minutes) VALUES('clozy', 'bot', 'poll', 1, 1, 15)")
        st.run("INSERT INTO bus_messages(sender, recipient, thread, body, created_at, held, wake) VALUES('a', 'clozy', 't', 'x', 1, 1, 'digest')")
        self.open_store()
        conn = self.raw()
        self.assertEqual(conn.execute("SELECT digest_minutes FROM agents WHERE name = 'clozy'").fetchone()[0], 15)
        self.assertEqual(conn.execute("SELECT held, wake FROM bus_messages").fetchone(), (1, "digest"))


class MigrateFromBusWithout0027Test(SchemaBase):
    """The bus tables as agent-control migration 0026 made them: the kit's base schema text, before MIGRATE_COLUMNS."""

    def setUp(self):
        super().setUp()
        conn = self.raw()
        conn.executescript(sb.SCHEMA_TABLES)
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '7')")
        conn.execute("INSERT INTO agents(name, kind, wake, bot_id, notices, token_gen, last_seen_at, created_at, updated_at)"
                     " VALUES('clozy', 'bot', 'webhook', 'clozy-bot', 'bus', 3, 99, 1, 2)")
        conn.execute("INSERT INTO agents(name, kind, wake, created_at, updated_at, retired_at) VALUES('old-one', 'service', 'poll', 1, 2, 50)")
        conn.execute("INSERT INTO agent_aliases(alias, name, created_at) VALUES('clozy-old', 'clozy', 1)")
        conn.execute("INSERT INTO bus_messages(sender, recipient, thread, client_msg_id, body, meta, urgent, state, created_at, delivered_at,"
                     " attempts, alerted_at) VALUES('polisher', 'clozy', 'dm:clozy+polisher', 'k1', 'old message', '{\"a\": 1}', 1,"
                     " 'delivered', 100, 101, 1, 150)")
        conn.execute("INSERT INTO bus_messages(sender, recipient, thread, body, state, created_at, last_error) VALUES('polisher', 'clozy',"
                     " 'dm:clozy+polisher', 'failed one', 'failed', 200, 'HTTP 503')")
        conn.execute("INSERT INTO bus_attempts(message_id, transport, started_at, finished_at, ok, http_status) VALUES(1, 'webhook', 100, 101, 1, 200)")
        conn.commit()
        for t, names in (("agents", AGENT_NEW), ("bus_messages", MSG_NEW)):
            for n in names:
                self.assertNotIn(n, cols(conn, t), "the fixture predates the wake-policy columns")
        conn.close()
        self.open_store()
        self.conn = self.raw()

    def test_new_columns_are_added_with_defaults(self):
        a = cols(self.conn, "agents")
        for n in AGENT_NEW:
            self.assertIn(n, a)
        m = cols(self.conn, "bus_messages")
        for n in MSG_NEW:
            self.assertIn(n, m)
        self.assertIn("bus_message_id", cols(self.conn, "orchestrator_messages"))
        self.assertEqual(self.conn.execute("SELECT name, digest_minutes, last_digest_at, unreachable_since, alerted_at, escalated_at"
                                           " FROM agents ORDER BY name").fetchall(),
                         [("clozy", 60, None, None, None, None), ("old-one", 60, None, None, None, None)])
        self.assertEqual(self.conn.execute("SELECT id, wake, held, noop, reply_expected FROM bus_messages ORDER BY id").fetchall(),
                         [(1, None, 0, 0, 0), (2, None, 0, 0, 0)])

    def test_existing_rows_are_kept(self):
        self.assertEqual(self.conn.execute("SELECT name, kind, wake, bot_id, notices, token_gen, last_seen_at, retired_at FROM agents"
                                           " ORDER BY name").fetchall(),
                         [("clozy", "bot", "webhook", "clozy-bot", "bus", 3, 99, None), ("old-one", "service", "poll", None, "legacy", 1, None, 50)])
        self.assertEqual(self.conn.execute("SELECT alias, name FROM agent_aliases").fetchall(), [("clozy-old", "clozy")])
        self.assertEqual(self.conn.execute("SELECT sender, recipient, client_msg_id, body, meta, urgent, state, created_at, delivered_at, attempts,"
                                           " alerted_at, last_error FROM bus_messages ORDER BY id").fetchall(),
                         [("polisher", "clozy", "k1", "old message", '{"a": 1}', 1, "delivered", 100, 101, 1, 150, None),
                          ("polisher", "clozy", None, "failed one", None, 0, "failed", 200, None, 0, None, "HTTP 503")])
        self.assertEqual(self.conn.execute("SELECT message_id, transport, ok, http_status FROM bus_attempts").fetchall(), [(1, "webhook", 1, 200)])
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "8")

    def test_old_rows_work_with_the_bus(self):
        """An old delivered message is not held and not noop, so the new queries ignore it; an old queued one stays deliverable."""
        self.conn.close()
        st = sb.Store(self.path)
        self.addCleanup(st.db().close)
        self.assertEqual(st.q("SELECT id FROM bus_messages WHERE held = 1 OR noop = 1"), [])
        self.assertEqual(st.one("SELECT digest_minutes FROM agents WHERE name = 'clozy'")["digest_minutes"], 60)
        self.assertEqual(st.one("SELECT token_gen FROM agents WHERE name = 'clozy'")["token_gen"], 3)


if __name__ == "__main__":
    unittest.main()
