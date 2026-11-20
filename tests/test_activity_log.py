"""Activity feed and nightly memory snapshots, through the real backend and CLI."""
import json
import os
import runpy
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from helpers import Backend, SERVER, WORKER, sb


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.addCleanup(self.backend.close)
        self.bot = self.backend.bot("project-bot", projects=("project",))
        self.chief = self.backend.bot("chief-bot", role="chief")
        self.other = self.backend.bot("other-bot", projects=("other",))

    def add(self, kind="activity", token=None, **fields):
        body = dict(project="project", kind=kind, summary="Updated the checklist")
        body.update(fields)
        return self.backend.req("POST", "/activity-log", body, tok=token or self.bot)

    def ctl(self, *args, input=None):
        token_file = Path(self.backend.tmp, "cli-token")
        token_file.write_text(self.bot)
        return subprocess.run([str(Path(SERVER, "swarmctl")), "activity-log", *args],
                              env=dict(os.environ, SWARM_URL=self.backend.base, SWARM_TOKEN_FILE=str(token_file)),
                              capture_output=True, text=True, input=input)

    def test_access_validation_and_idempotency(self):
        first = self.add(dedupe_key="checklist-v2", bot="other-bot", link="https://example.com/checklist")
        self.assertEqual(first["entry"]["bot"], "project-bot")
        self.assertTrue(self.add(dedupe_key="checklist-v2", link="https://example.com/checklist")["duplicate"])
        for fields, status in (({"dedupe_key": "checklist-v2"}, 409), ({"summary": "a\nb"}, 400),
                               ({"summary": "uses | separator"}, 400), ({"kind": "result"}, 400),
                               ({"link": "https://example.com/preview?private=value"}, 400),
                               ({"lasting": True}, 400), ({"kind": "decision/owner"}, 403),
                               ({"kind": "decision/delegated"}, 403), ({"project": "other"}, 404)):
            with self.subTest(fields=fields):
                body = {"project": "project", "summary": "Changed layout", "kind": "activity", **fields}
                self.backend.req("POST", "/activity-log", body, tok=self.bot, expect=status)
        for route in ("/activity-log", "/activity-log/export", "/activity-log/snapshot"):
            self.backend.req("GET", route + "?project=project", tok=self.other, expect=404)
            self.backend.req("GET", route + "?project=project", tok=WORKER, expect=403)
        self.backend.req("GET", "/activity-log?project=project&limit=0", tok=self.bot, expect=400)
        self.assertEqual(self.backend.req("GET", "/activity-log?project=project&limit=1", tok=self.chief)["entries"],
                         [first["entry"]])

    def test_nightly_snapshot_and_overrule_across_central_month_boundary(self):
        before = int(datetime(2026, 7, 1, 4, 59, tzinfo=timezone.utc).timestamp())
        with patch.object(sb, "now", return_value=before):
            delegated = self.add("decision/delegated", self.chief, lasting=True, summary="Keep the short checklist")["entry"]
        with patch.object(sb, "now", return_value=before + 60):
            owner = self.add("decision/owner", self.chief, overrules=delegated["id"], summary="Use the full checklist")["entry"]
            self.add("decision/bot", lasting=True, summary="Write a checklist note <here>")
            one_off = self.add("decision/bot", summary="One-off layout choice")["entry"]
        snap = self.backend.req("GET", "/activity-log/snapshot?project=project", tok=self.bot)
        files = snap["files"]
        self.assertEqual(snap["through"], one_off["id"])
        self.assertEqual(files["project/log/2026-06.md"], delegated["line"] + "\n")
        self.assertIn("2026-06-30 23:59 CT", delegated["line"])
        self.assertIn("2026-07-01 00:00 CT", owner["line"])
        self.assertIn(f"overrules #{delegated['id']}", owner["line"])
        self.assertIn(one_off["line"], files["project/log/2026-07.md"])
        block = files["project/decisions.md"]
        for text in ("<!-- activity-log:begin -->", "<!-- activity-log:end -->", "### Owner decided",
                     "### Delegated by Chief of Staff", "### Project bot calls", delegated["line"], owner["line"], "&lt;here&gt;"):
            self.assertIn(text, block)
        self.assertNotIn(one_off["summary"], block)
        after = f"/activity-log/snapshot?project=project&after={snap['through']}"
        self.assertEqual(self.backend.req("GET", after, tok=self.bot)["files"], {})
        self.add("shipped")
        next_files = self.backend.req("GET", after, tok=self.bot)["files"]
        self.assertEqual(len(next_files), 1)
        self.assertNotIn("project/decisions.md", next_files)
        self.backend.req("POST", "/activity-log", dict(project="other", kind="decision/owner", summary="No",
                                                        overrules=delegated["id"]), tok=self.chief, expect=400)

    def test_cli_and_offline_month_apply(self):
        added = self.ctl("add", "project", "--kind", "blocked", "Waiting for a review", "--key", "review")
        self.assertEqual(added.returncode, 0, added.stderr)
        entry = json.loads(added.stdout)["entry"]
        listed = self.ctl("list", "project")
        self.assertEqual(listed.stdout, entry["line"] + "\n")
        export = self.ctl("export", "project", "--month", entry["line"][2:9])
        self.assertEqual(export.stdout, listed.stdout)
        nightly = self.ctl("export", "project", "--nightly")
        self.assertEqual(nightly.returncode, 0, nightly.stderr)
        with tempfile.TemporaryDirectory() as target:
            applied = self.ctl("apply", "-", "--dir", target, input=nightly.stdout)
            expected = f"project/log/{entry['line'][2:9]}.md"
            self.assertEqual(applied.stdout, expected + "\n", applied.stderr)
            self.assertEqual(Path(target, expected).read_text(), listed.stdout)
            self.assertEqual(self.ctl("apply", "-", "--dir", target, input=nightly.stdout).stdout, "")
            malicious = json.dumps({"files": {"../../elsewhere.md": "no"}})
            self.assertNotEqual(self.ctl("apply", "-", "--dir", target, input=malicious).returncode, 0)
        quiet = self.ctl("export", "project", "--nightly", "--after", str(entry["id"]))
        self.assertEqual((quiet.returncode, quiet.stdout), (0, ""))

    def test_worker_claim_must_opt_in_and_stay_in_project(self):
        route = "/activity-log"
        body = {"kind": "blocked", "summary": "Waiting for copy"}
        for prompt, untrusted, expected in (("do work", False, 403), ("Activity log: yes", True, 403),
                                            ("Activity log: yes", False, 200)):
            self.backend.req("POST", "/tasks", {"project": "project", "prompt": prompt, "untrusted_input": untrusted}, tok=self.bot)
            claim = self.backend.claim()
            token = claim["task_token"]
            result = self.backend.req("POST", route, body, tok=token, expect=expected)
            if expected == 200:
                self.assertEqual(result["entry"]["bot"], "project-bot")
                self.assertTrue(self.backend.req("POST", route, body, tok=token)["duplicate"])
                self.backend.req("POST", route, dict(body, project="other"), tok=token, expect=403)
                self.backend.req("POST", route, dict(body, kind="decision/delegated"), tok=token, expect=403)
            self.backend.finish(claim)
            self.backend.req("POST", route, body, tok=token, expect=409)

    def test_push_main_grant_does_not_follow_another_repo(self):
        self.backend.req("PUT", "/projects/memory", {"default_repo": "org/memory", "allow_push_main": True})
        body = {"project": "memory", "prompt": "Export activity", "push_main": True}
        self.backend.req("POST", "/tasks", dict(body, repo="org/code"), tok=self.chief, expect=403)
        self.backend.req("POST", "/tasks", body, tok=self.chief)
        claim = self.backend.claim()
        route = f"/tasks/{claim['id']}/publish-check"
        self.assertTrue(self.backend.req("POST", route, tok=claim["task_token"])["allowed"])
        self.backend.req("PUT", "/projects/memory", {"default_repo": "org/new-memory"})
        self.assertFalse(self.backend.req("POST", route, tok=claim["task_token"])["allowed"])


class ActivityHelpersTest(unittest.TestCase):
    def test_old_database_gains_activity_store_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as target:
            path = str(Path(target, "old.db"))
            store = sb.Store(path)
            with sqlite3.connect(path) as db:
                db.execute("DROP TABLE activity_log")
                db.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
                db.execute("INSERT INTO projects(name) VALUES('project')")
            upgraded = sb.Store(path)
            self.assertEqual(upgraded.one("SELECT value FROM meta WHERE key = 'schema_version'")["value"], "6")
            self.assertEqual(upgraded.one("SELECT name FROM projects")["name"], "project")
            self.assertEqual(upgraded.one("SELECT COUNT(*) AS count FROM activity_log")["count"], 0)
            store.db().close()
            upgraded.db().close()

    def test_marker_merge_preserves_manual_bytes_and_rejects_partial_markers(self):
        merge = runpy.run_path(str(Path(SERVER, "swarmctl")))["merge_activity_decisions"]
        block = b"<!-- activity-log:begin -->\nnew entry\n<!-- activity-log:end -->\n"
        manual = b"# Decisions\r\nManual before.\r\n"
        suffix = b"\r\n\r\nManual after."
        prior = b"<!-- activity-log:begin -->\r\nold block\r\n<!-- activity-log:end -->"
        self.assertEqual(merge(manual + prior + suffix, block), manual + block.rstrip(b"\n") + suffix)
        self.assertTrue(merge(manual, block).startswith(manual))
        with self.assertRaises(ValueError):
            merge(manual + b"<!-- activity-log:end -->\n", block)
        with self.assertRaisesRegex(ValueError, "append-only"):
            merge(block.replace(b"new entry", b"- newer entry"), block)
