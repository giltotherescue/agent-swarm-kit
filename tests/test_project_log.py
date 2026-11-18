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
        self.b.req("POST", "/log", {"project": "site", "kind": "feedback", "summary": "Hero image feels stock", "by": "Gil"}, tok=self.site)
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

    # ---- typed records (items, verdicts, feedback, learnings, rules) ----
    def test_ad_goes_draft_review_changes_approved_then_a_learning(self):
        def post(body, tok=None, expect=200):
            return self.b.req("POST", "/log", dict(body, project="marketing"), tok=tok or self.mkt, expect=expect)
        ad = post({"kind": "item", "status": "draft", "summary": "Spring sale ad, Facebook"})["entry"]
        ref = ad["ref"]
        self.assertEqual((ad["version"], ad["status"]), (1, "draft"))
        self.assertTrue(ref.startswith("item-"))
        post({"kind": "item", "ref": ref, "status": "needs-review"})
        # A verdict needs to say who gave it.
        self.assertEqual(self.b.call("POST", "/log", {"project": "marketing", "kind": "item", "ref": ref, "status": "changes"},
                                     tok=self.mkt)[0], 400)
        post({"kind": "item", "ref": ref, "status": "changes", "by": "Sam", "version": 1})
        fb = post({"kind": "feedback", "ref": ref, "by": "Sam", "summary": "Too salesy. Lead with the customer's problem."})
        self.assertEqual(fb["entry"]["version"], 1)
        # Feedback becomes a pending learning.
        self.assertEqual((fb["learning"]["kind"], fb["learning"]["status"]), ("learning", "pending"))
        # Re-drafting after a verdict starts version 2: the old verdict no longer counts.
        v2 = post({"kind": "item", "ref": ref, "status": "needs-review", "summary": "Spring sale ad, Facebook (v2)"})["entry"]
        self.assertEqual((v2["version"], v2["status"]), (2, "needs-review"))
        st, err = self.b.call("POST", "/log", {"project": "marketing", "kind": "item", "ref": ref, "status": "approved",
                                               "by": "Sam", "version": 1}, tok=self.mkt)
        self.assertEqual(st, 409, err)
        post({"kind": "item", "ref": ref, "status": "approved", "by": "Sam", "version": 2})
        items = self.b.req("GET", "/log/items?project=marketing", tok=self.mkt)["items"]
        cur = [i for i in items if i["ref"] == ref][0]
        self.assertEqual((cur["version"], cur["status"]), (2, "approved"))
        # The learning goes into the rules doc only through a reviewed rule change.
        lrn = fb["learning"]["ref"]
        self.assertEqual(self.b.call("POST", "/log", {"project": "marketing", "kind": "learning", "status": "standing",
                                                      "summary": "x"}, tok=self.mkt)[0], 400)
        prop = post({"kind": "rule", "after": "Lead with the customer's problem, not the discount.", "learning": lrn})["entry"]
        self.assertEqual(prop["status"], "proposed")
        review = {"kind": "rule", "status": "approved", "proposal": prop["id"], "by": "Sam"}
        self.assertEqual(self.b.call("POST", "/log", dict(review, project="marketing"), tok=self.mkt)[0], 403)
        self.assertEqual(self.b.call("POST", "/log", dict(review, project="marketing", by=None), tok=self.chief)[0], 400)
        post(review, tok=self.chief)
        self.assertEqual(self.b.call("POST", "/log", dict(review, project="marketing"), tok=self.chief)[0], 409)  # reviewed once
        rules = self.b.req("GET", "/log/rules?project=marketing", tok=self.mkt)
        self.assertEqual([r["after"] for r in rules["rules"]], ["Lead with the customer's problem, not the discount."])
        self.assertEqual(rules["changelog"][-1]["by"], "Sam")
        standing = self.entries("marketing", kind="learning", ref=lrn)
        self.assertEqual([e["status"] for e in standing], ["pending", "standing"])
        # A change stores before and after.
        rule = rules["rules"][0]["ref"]
        ch = post({"kind": "rule", "ref": rule, "after": "Lead with the problem. Mention the discount once, at the end."})["entry"]
        self.assertEqual(ch["before"], "Lead with the customer's problem, not the discount.")
        post({"kind": "rule", "status": "approved", "proposal": ch["id"], "by": "Sam"}, tok=self.chief)
        brief = self.b.call("GET", "/log/brief?project=marketing", tok=self.mkt)[1].decode()
        self.assertIn("1. Lead with the problem. Mention the discount once, at the end.", brief)
        self.assertIn("Too salesy", brief)
        doc = self.b.call("GET", "/log/rules?project=marketing&format=md", tok=self.mkt)[1].decode()
        self.assertIn("## Changelog", doc)
        self.assertIn("Lead with the customer's problem, not the discount. -> Lead with the problem.", doc)
        self.assertEqual(self.b.call("GET", "/log/brief?project=marketing", tok=self.site)[0], 404)

    def test_bad_records_are_refused(self):
        bad = [{"kind": "feedback", "summary": "no author"},
               {"kind": "decision", "summary": "Budget", "answer": "500"},
               {"kind": "decision", "summary": "Budget", "by": "Sam"},
               {"kind": "item", "status": "great", "summary": "x"},
               {"kind": "item", "status": "approved", "summary": "a new item cannot start approved"},
               {"kind": "activity", "status": "draft", "summary": "x"},
               {"kind": "learning", "status": "maybe", "summary": "x"},
               {"kind": "rule", "summary": "a new rule with no text"},
               {"kind": "activity", "summary": "x", "bot": "nobody-bot"}]
        for body in bad:
            st, err = self.b.call("POST", "/log", dict(body, project="marketing"))
            self.assertEqual(st, 400, (body, err))
        self.assertEqual(self.b.call("POST", "/log", {"project": "marketing", "kind": "feedback", "ref": "item-99999",
                                                      "by": "Sam", "summary": "x"})[0], 404)
        d = self.b.req("POST", "/log", {"project": "marketing", "kind": "decision", "summary": "Monthly ad budget", "by": "Sam",
                                        "answer": "$500", "source": "weekly goals meeting"})["entry"]
        self.assertEqual((d["by"], d["answer"], d["source"]), ("Sam", "$500", "weekly goals meeting"))

    def test_rules_doc_is_capped(self):
        os.environ["SWARM_RULES_CAP"] = "2"
        try:
            for n in range(3):
                p = self.b.req("POST", "/log", {"project": "capped", "kind": "rule", "after": f"Rule number {n}"})["entry"]
                review = {"project": "capped", "kind": "rule", "status": "approved", "proposal": p["id"], "by": "Sam"}
                self.assertEqual(self.b.call("POST", "/log", review)[0], 200 if n < 2 else 409)
        finally:
            os.environ.pop("SWARM_RULES_CAP")

    def test_throwaway_items_expire(self):
        e = self.b.req("POST", "/log", {"project": "site", "kind": "item", "status": "draft", "summary": "Test banner",
                                        "expires_days": 1}, tok=self.site)["entry"]
        self.assertIn(e["ref"], str(self.b.req("GET", "/log/items?project=site", tok=self.site)))
        self.b.api.s.run("UPDATE log_entries SET expires_at = ? WHERE ref = ?", int(time.time()) - 5, e["ref"])
        self.assertNotIn(e["ref"], str(self.b.req("GET", "/log/items?project=site", tok=self.site)))
        self.b.api.s.run("DELETE FROM meta WHERE key = 'retention_at'")
        self.b.api.retention()
        self.assertEqual(self.entries("site", ref=e["ref"]), [])

    def test_waiting_list_shows_counts_and_age(self):
        old = self.b.req("POST", "/waiting", {"project": "site", "kind": "decision", "ask": "Redo the logo?",
                                              "dedupe_key": "logo-redo"}, tok=self.site)["item"]
        self.b.api.s.run("UPDATE items SET created_at = ? WHERE id = ?", int(time.time()) - 20 * 86400, old["id"])
        r = self.b.req("GET", "/waiting")
        self.assertGreaterEqual(r["counts"]["open"], 1)
        self.assertGreaterEqual(r["counts"]["oldest_days"], 20)
        self.assertGreaterEqual(r["counts"]["stale"], 1)
        md = self.b.call("GET", "/log/export?project=site")[1].decode()
        self.assertIn("Consider dropping", md)
        self.assertIn("Redo the logo?", md)

    def test_weekly_export_writes_files_and_commits_to_git(self):
        out = tempfile.mkdtemp()
        os.environ["SWARM_LOG_EXPORT_DIR"] = out
        try:
            self.b.req("PUT", "/projects/newsletter", {"log_git": True})
            self.b.req("POST", "/log", {"project": "newsletter", "kind": "activity", "summary": "Sent issue 12"})
            self.b.api.s.run("DELETE FROM meta WHERE key = 'log_export_week'")
            self.b.api.weekly_export()
        finally:
            os.environ.pop("SWARM_LOG_EXPORT_DIR")
        files = sorted(os.listdir(os.path.join(out, "newsletter")))
        self.assertIn("rules.md", files)
        week = [f for f in files if f.endswith(".md") and f != "rules.md"][0]
        self.assertIn("Sent issue 12", open(os.path.join(out, "newsletter", week)).read())
        git_log = subprocess.run(["git", "log", "--oneline"], cwd=os.path.join(out, "newsletter"), capture_output=True, text=True)
        self.assertIn("Project log export", git_log.stdout)
        self.assertFalse(os.path.isdir(os.path.join(out, "site", ".git")))  # only projects that asked for git


    # ---- export ----
    def test_markdown_export(self):
        self.b.req("POST", "/log", {"project": "marketing", "kind": "feedback", "summary": "Too formal for our readers", "by": "Sam",
                                    "detail": "Write like we talk to customers.\nShorter paragraphs."}, tok=self.chief)
        md = self.b.call("GET", "/log/export?project=marketing", tok=self.mkt)[1].decode()
        self.assertTrue(md.startswith("# Project log: marketing"))
        self.assertIn("- **feedback** Too formal for our readers (by Sam, via chief)", md)
        self.assertIn("  Shorter paragraphs.", md)
        self.assertRegex(md, r"\n### \d{4}-\d{2}-\d{2}\n")
        self.assertIn("## Waiting on you", md)
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
        self.assertEqual(sorted(os.listdir(os.path.join(tmp, "all"))), ["marketing-log.md", "marketing-rules.md"])
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
