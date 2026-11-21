"""Follow-up and child tasks get the parent's uploaded report and artifacts in .task-context/parent/ (agent-control PR 71, ported)."""
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_worker  # noqa: E402

WorkerTest = test_worker.WorkerTest
del test_worker


class ParentContext(WorkerTest):

    def parent(self, report=b"## Summary\nStatus: done\n", artifacts=None, command="true"):
        """A claimed parent task with uploaded files, written straight into the backend's files table."""
        pid = self.admin("POST", "/tasks", dict({"prompt": "parent job", "owner": "web"}, **({"command": command} if command else {})))["task"]["id"]
        self.w.api("POST", "/worker/claim", {"worker": "test"})
        for kind, name, data in ([("report", "REPORT.md", report)] if report is not None else []) + \
                [("artifact", n, d) for n, d in (artifacts or {}).items()]:
            self.api.s.run("INSERT OR REPLACE INTO files(task_id, kind, name, data, size, at) VALUES(?,?,?,?,?,?)",
                           pid, kind, name, data, len(data), int(time.time()))
        self.api.s.run("UPDATE tasks SET state = 'done' WHERE id = ?", pid)
        return pid

    def inbox_for(self, body):
        tid = self.admin("POST", "/tasks", dict(body, prompt="fix it", owner="web", command="true"))["task"]["id"]
        task = self.w.api("POST", "/worker/claim", {"worker": "test"})["task"]
        self.assertEqual(task["id"], tid)
        sem = threading.BoundedSemaphore(1)
        sem.acquire()
        r = self.w.Runner(task, sem)
        r.fill_inbox()
        self.addCleanup(lambda: __import__("shutil").rmtree(r.inbox, ignore_errors=True))
        return r, task

    def read(self, r, *parts):
        with open(os.path.join(r.inbox, *parts), "rb") as fh:
            return fh.read()

    def test_follow_up_copies_report_and_artifacts(self):
        pid = self.parent(artifacts={"shot.png": b"\x89PNG", "notes.txt": b"hi"})
        r, task = self.inbox_for({"follow_up": pid})
        self.assertEqual(self.read(r, "parent", "REPORT.md"), b"## Summary\nStatus: done\n")
        self.assertEqual(self.read(r, "parent", "shot.png"), b"\x89PNG")
        self.assertEqual(self.read(r, "parent", "notes.txt"), b"hi")
        self.assertIn(b".task-context/parent/", self.read(r, "README"))
        self.assertNotIn(task["task_token"].encode(), self.read(r, "README") + self.read(r, "parent", "NOTES.md"))

    def test_parent_id_only_task(self):
        pid = self.parent(artifacts={"a.txt": b"A"})
        r, _ = self.inbox_for({"parent_id": pid})
        self.assertEqual(self.read(r, "parent", "a.txt"), b"A")
        self.assertTrue(os.path.exists(os.path.join(r.inbox, "parent", "REPORT.md")))

    def test_missing_report_is_a_note_not_a_crash(self):
        pid = self.parent(report=None)
        r, _ = self.inbox_for({"parent_id": pid})
        self.assertFalse(os.path.exists(os.path.join(r.inbox, "parent", "REPORT.md")))
        self.assertIn(b"parent report not available", self.read(r, "README"))
        self.assertIn(b"parent report not available", self.read(r, "parent", "NOTES.md"))

    def test_unsafe_names_are_skipped(self):
        pid = self.parent(artifacts={"..": b"x", "../evil": b"x", "/etc/passwd": b"x", "a..b": b"x", ".hidden": b"x", "ok.txt": b"ok"})
        r, _ = self.inbox_for({"parent_id": pid})
        self.assertEqual(sorted(os.listdir(os.path.join(r.inbox, "parent"))), ["NOTES.md", "REPORT.md", "ok.txt"])
        self.assertIn(b"skipped", self.read(r, "parent", "NOTES.md"))
        self.assertFalse(os.path.exists(os.path.join(r.inbox, "evil")))

    def test_task_without_parent_gets_nothing(self):
        r, _ = self.inbox_for({})
        self.assertFalse(os.path.exists(os.path.join(r.inbox, "parent")))

    def test_brief_mentions_parent_folder(self):
        text = self.w.build_prompt({"prompt": "go", "follow_up": True, "parent_id": "p1"}, "start")
        self.assertIn(".task-context/parent/", text)
        self.assertIn(".task-context/parent/", self.w.build_prompt({"prompt": "go"}, "start"))

    def test_agent_sees_parent_files_in_its_folder(self):
        agent = os.path.join(self.tmp, "parent-agent")
        with open(agent, "w") as fh:
            fh.write("#!/usr/bin/env bash\ncd \"$3\"; mkdir -p artifacts\n"
                     "{ echo Status: done; cat .task-context/parent/REPORT.md .task-context/parent/a.txt; "
                     "grep -c 'task-context/parent' .task-context/README; } > REPORT.md\necho done > AGENT_STATUS\n")
        os.chmod(agent, 0o755)
        self.w.RUN_AGENT = agent
        pid = self.parent(report=b"PARENT-REPORT\n", artifacts={"a.txt": b"ART\n"}, command=None)
        tid = self.admin("POST", "/tasks", {"prompt": "fix", "owner": "web", "follow_up": pid})["task"]["id"]
        t = self.run_one()
        self.assertEqual(t["id"], tid)
        self.assertEqual(t["state"], "done")
        rep = self.admin("GET", f"/tasks/{tid}/report")
        self.assertIn("PARENT-REPORT", rep)
        self.assertIn("ART", rep)

    def test_task_token_reads_parent_only(self):
        pid = self.parent(artifacts={"a.txt": b"A"})
        other = self.parent(artifacts={"b.txt": b"B"})
        r, task = self.inbox_for({"parent_id": pid})
        tok, tid = task["task_token"], task["id"]
        h = self.w.http
        self.assertEqual(h("GET", f"/tasks/{pid}/artifacts/a.txt", tok), b"A")
        for path in (f"/tasks/{other}/report", f"/tasks/{other}/artifacts", f"/tasks/{pid}/attachments", f"/tasks/{pid}/bundle"):
            with self.assertRaises(Exception, msg=path):
                h("GET", path, tok)


# Only the tests above; do not rerun the inherited worker tests here.
for _n in dir(ParentContext):
    if _n.startswith("test_") and _n not in ParentContext.__dict__:
        setattr(ParentContext, _n, None)

del WorkerTest


if __name__ == "__main__":
    unittest.main()
