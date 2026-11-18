"""Project log (0.3.0): permissions, auto-entries, export, migration and the swarmctl commands."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import ADMIN, WORKER, Backend, sb

SWARMCTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "swarmctl")


class ProjectLogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.mkt = cls.b.bot("mkt-bot", projects=["marketing"])
        cls.site = cls.b.bot("site-bot", projects=["site"])
        cls.chief = cls.b.bot("chief", role="chief")

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def entries(self, project, tok=ADMIN, **qs):
        q = "&".join(f"{k}={v}" for k, v in dict(qs, project=project).items())
        return self.b.req("GET", "/log?" + q, tok=tok)["entries"]

    def claim_task(self, prompt):
        while True:
            c = self.b.claim()
            self.assertIsNotNone(c, "task was not claimable")
            if c["prompt"] == prompt:
                return c
            self.b.finish(c, "done")

    # ---- permissions ----
    def test_bot_writes_and_reads_only_its_own_projects(self):
        e = self.b.req("POST", "/log", {"project": "marketing", "kind": "activity", "summary": "Drafted the November newsletter",
                                        "links": ["https://example.com/draft"]}, tok=self.mkt)["entry"]
        self.assertEqual(e["bot"], "mkt-bot")
        self.assertEqual(e["links"], ["https://example.com/draft"])
        # Not bound to site: cannot write there, cannot read there.
        st, err = self.b.call("POST", "/log", {"project": "site", "kind": "activity", "summary": "sneaky"}, tok=self.mkt)
        self.assertEqual(st, 403, err)
        self.assertEqual(self.b.call("GET", "/log?project=marketing", tok=self.site)[0], 404)
        self.assertEqual(self.b.call("GET", "/log/export?project=marketing", tok=self.site)[0], 404)
        # Without a project filter, a bot sees only its own projects.
        mine = self.b.req("GET", "/log", tok=self.site)["entries"]
        self.assertTrue(all(x["project"] == "site" for x in mine))
        self.assertNotIn("November newsletter", str(mine))
        # A bot cannot pretend to be another bot.
        e2 = self.b.req("POST", "/log", {"project": "marketing", "kind": "learning", "summary": "Short subject lines win",
                                         "bot": "site-bot"}, tok=self.mkt)["entry"]
        self.assertEqual(e2["bot"], "mkt-bot")
        # Chief and admin read everything.
        self.assertIn("November newsletter", str(self.entries("marketing", tok=self.chief)))
        self.assertIn("November newsletter", str(self.entries("marketing")))

    def test_worker_token_cannot_use_the_log(self):
        self.assertEqual(self.b.call("POST", "/log", {"project": "marketing", "kind": "activity", "summary": "x"}, tok=WORKER)[0], 403)
        self.assertEqual(self.b.call("GET", "/log?project=marketing", tok=WORKER)[0], 403)

    def test_validation_and_append_only(self):
        for body in ({"project": "marketing", "kind": "gossip", "summary": "x"},
                     {"project": "marketing", "kind": "activity"},
                     {"kind": "activity", "summary": "x"},
                     {"project": "marketing", "kind": "activity", "summary": "x", "links": "not a list"}):
            self.assertEqual(self.b.call("POST", "/log", body)[0], 400, body)
        a = self.b.req("POST", "/log", {"project": "marketing", "kind": "activity", "summary": "Weekly post", "dedupe_key": "wk-47"})
        b = self.b.req("POST", "/log", {"project": "marketing", "kind": "activity", "summary": "Weekly post again", "dedupe_key": "wk-47"})
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["entry"]["id"], b["entry"]["id"])
        # No route changes or deletes entries.
        self.assertFalse([r for r in sb.ROUTES if r["pattern"].startswith("/log") and r["method"] != "GET" and r["pattern"] != "/log"])

    def test_filters(self):
        self.b.req("POST", "/log", {"project": "site", "kind": "feedback", "summary": "Hero image feels stock"}, tok=self.site)
        self.b.req("POST", "/log", {"project": "site", "kind": "learning", "summary": "Gil prefers real photos"}, tok=self.site)
        self.b.req("POST", "/log", {"project": "site", "kind": "activity", "summary": "Fixed a typo"}, tok=self.site)
        got = self.entries("site", tok=self.site, kind="feedback,learning")
        self.assertEqual({e["kind"] for e in got}, {"feedback", "learning"})
        self.assertEqual(self.b.call("GET", "/log?project=site&kind=nope")[0], 400)
        self.assertEqual(self.entries("site", since=int(time.time()) + 100), [])
        self.assertEqual(len(self.entries("site", limit=1)), 1)

    # ---- auto-entries ----
    def test_finished_task_adds_one_result_entry(self):
        prompt = "Write three ad headlines for the spring sale"
        self.b.req("POST", "/tasks", {"prompt": prompt, "project": "marketing", "title": "Spring ad headlines"}, tok=self.mkt)
        c = self.claim_task(prompt)
        # The running task may add to its own project's log, and only there.
        e = self.b.req("POST", "/log", {"kind": "learning", "summary": "Numbers in headlines tested better"}, tok=c["task_token"])["entry"]
        self.assertEqual((e["project"], e["bot"], e["task_id"]), ("marketing", "mkt-bot", c["id"]))
        self.assertEqual(self.b.call("POST", "/log", {"project": "site", "kind": "activity", "summary": "x"}, tok=c["task_token"])[0], 403)
        self.assertEqual(self.b.call("GET", "/log?project=marketing", tok=c["task_token"])[0], 403)
        st, _ = self.b.finish(c, "done", summary="Three headlines in the report.", pr_url="https://example.com/pr/1")
        self.assertEqual(st, 200)
        self.b.finish(c, "done", summary="replay")  # replayed finish must not add a second entry
        results = [x for x in self.entries("marketing", kind="result") if x["task_id"] == c["id"]]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["summary"], "Done: Spring ad headlines")
        self.assertIn("Three headlines", results[0]["detail"])
        self.assertIn("https://example.com/pr/1", results[0]["links"])
        self.assertIn(f"https://swarm.example/tasks/{c['id']}/report", results[0]["links"])
        # The task token dies with the claim.
        self.assertEqual(self.b.call("POST", "/log", {"kind": "activity", "summary": "late"}, tok=c["task_token"])[0], 409)

    def test_failed_task_adds_result_and_projectless_task_adds_none(self):
        prompt = "Pull the keyword list (will fail)"
        self.b.req("POST", "/tasks", {"prompt": prompt, "project": "marketing"}, tok=self.mkt)
        c = self.claim_task(prompt)
        self.b.finish(c, "failed", error="The data source timed out.")
        r = [x for x in self.entries("marketing", kind="result") if x["task_id"] == c["id"]]
        self.assertEqual(r[0]["summary"], "Failed: Pull the keyword list (will fail)")
        self.assertIn("timed out", r[0]["detail"])
        before = self.b.api.s.one("SELECT COUNT(*) AS n FROM log_entries")["n"]
        prompt2 = "A task with no project"
        self.b.req("POST", "/tasks", {"prompt": prompt2})
        c2 = self.claim_task(prompt2)
        self.assertEqual(self.b.call("POST", "/log", {"kind": "activity", "summary": "x"}, tok=c2["task_token"])[0], 400)
        self.b.finish(c2, "done")
        self.assertEqual(self.b.api.s.one("SELECT COUNT(*) AS n FROM log_entries")["n"], before)

    def test_answered_and_dropped_items_add_decision_entries(self):
        item = self.b.req("POST", "/waiting", {"project": "marketing", "kind": "decision", "ask": "Run the spring sale at 20% or 30%?",
                                               "recommendation": "20%", "dedupe_key": "spring-discount"}, tok=self.mkt)["item"]
        # A bot cannot answer; nothing is logged for a refused try.
        self.assertEqual(self.b.call("POST", f"/waiting/{item['id']}", {"action": "answer", "answer": "30%"}, tok=self.mkt)[0], 403)
        self.b.req("POST", f"/waiting/{item['id']}", {"action": "answer", "answer": "20%, ends Sunday"}, tok=self.chief)
        d = [x for x in self.entries("marketing", tok=self.mkt, kind="decision") if "spring sale" in x["summary"]]
        self.assertEqual(len(d), 1)
        self.assertIn("20%, ends Sunday", d[0]["detail"])
        self.assertEqual(d[0]["bot"], "mkt-bot")
        drop = self.b.req("POST", "/waiting", {"project": "marketing", "kind": "decision", "ask": "Start a TikTok account?",
                                               "dedupe_key": "tiktok"}, tok=self.mkt)["item"]
        self.b.req("POST", f"/waiting/{drop['id']}", {"action": "drop", "note": "Not this quarter"}, tok=self.chief)
        d2 = [x for x in self.entries("marketing", kind="decision") if "TikTok" in x["summary"]]
        self.assertEqual(len(d2), 1)
        self.assertIn("Dropped: Not this quarter", d2[0]["detail"])

    def test_human_question_answered_adds_one_decision(self):
        prompt = "Rewrite the pricing page copy (ask the human about tone)"
        self.b.req("POST", "/tasks", {"prompt": prompt, "project": "site", "ask_human": True}, tok=self.site)
        c = self.claim_task(prompt)
        self.b.finish(c, "waiting", question="Playful or serious tone?")
        self.b.req("POST", f"/waiting/{c['id']}", {"action": "answer", "answer": "Playful"}, tok=self.chief)
        d = [x for x in self.entries("site", kind="decision") if x["task_id"] == c["id"]]
        self.assertEqual(len(d), 1)
        self.assertIn("Playful or serious tone?", d[0]["summary"])
        self.assertIn("Playful", d[0]["detail"])
        # A question to the owner bot is not a human decision and is not logged.
        prompt2 = "Tidy the footer links"
        self.b.req("POST", "/tasks", {"prompt": prompt2, "project": "site"}, tok=self.site)
        c2 = self.claim_task(prompt2)
        self.b.finish(c2, "waiting", question="Keep the old blog link?")
        self.b.req("POST", f"/tasks/{c2['id']}/answer", {"answer": "No"}, tok=self.site)
        self.assertFalse([x for x in self.entries("site", kind="decision") if x["task_id"] == c2["id"]])

    # ---- export ----
    def test_markdown_export(self):
        self.b.req("POST", "/log", {"project": "marketing", "kind": "feedback", "summary": "Too formal for our readers",
                                    "detail": "Write like we talk to customers.\nShorter paragraphs."}, tok=self.chief)
        md = self.b.call("GET", "/log/export?project=marketing", tok=self.mkt)[1].decode()
        self.assertTrue(md.startswith("# Project log: marketing"))
        self.assertIn("- **feedback** Too formal for our readers (chief)", md)
        self.assertIn("  Shorter paragraphs.", md)
        self.assertRegex(md, r"\n## \d{4}-\d{2}-\d{2}\n")
        self.assertEqual(self.b.call("GET", "/log/export")[0], 400)
        empty = sb.log_markdown("quiet", [])
        self.assertIn("No entries.", empty)

    # ---- swarmctl ----
    def test_swarmctl_add_list_export(self):
        tmp = tempfile.mkdtemp()
        tokf = os.path.join(tmp, "tok")
        with open(tokf, "w") as fh:
            fh.write(self.mkt)
        env = dict(os.environ, SWARM_URL=self.b.base, SWARM_TOKEN_FILE=tokf, HOME=tmp)

        def ctl(*args):
            return subprocess.run([sys.executable, SWARMCTL, *args], env=env, capture_output=True, text=True, timeout=30)

        r = ctl("log", "add", "marketing", "--kind", "learning", "Emails sent Tuesday get more replies")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = ctl("log", "list", "marketing", "--kind", "learning", "--days", "1")
        self.assertIn("Emails sent Tuesday", r.stdout)
        out = os.path.join(tmp, "m.md")
        r = ctl("log", "export", "marketing", "--days", "7", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Emails sent Tuesday", open(out).read())
        r = ctl("log", "export", "--dir", os.path.join(tmp, "all"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(os.path.join(tmp, "all")), ["marketing-log.md"])
        r = ctl("log", "add", "site", "--kind", "activity", "not mine")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("403", r.stderr)


class MigrationTest(unittest.TestCase):
    def test_old_database_gets_the_log_table(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "old.db")
        db = sqlite3.connect(path)
        # A 0.2.x database: no log_entries table, no schema_version.
        db.executescript(sb.SCHEMA_TABLES.split("CREATE TABLE IF NOT EXISTS log_entries")[0])
        db.execute("INSERT INTO projects(name) VALUES('marketing')")
        db.commit()
        db.close()
        store = sb.Store(path)
        self.assertEqual(store.one("SELECT value FROM meta WHERE key = 'schema_version'")["value"], str(sb.SCHEMA_VERSION))
        api = sb.Backend(store)
        self.assertIsNotNone(api.add_log("marketing", "mkt-bot", "activity", "first entry after upgrade"))
        self.assertEqual(store.one("SELECT name FROM projects")["name"], "marketing")
        sb.Store(path)  # opening again is harmless
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM log_entries")["n"], 1)


if __name__ == "__main__":
    unittest.main()
