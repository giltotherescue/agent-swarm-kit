"""Outbox folders (round 3, defect B): agent-exec cleanup, when the runner calls it, and the daily cleanup.

The two-user version (runner and agent as real separate users) is in handoff_users_check.py.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SERVER = os.path.join(ROOT, "server")
AGENT_EXEC = os.path.join(SERVER, "agent-exec")


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


class AgentExecCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.handoff = os.path.join(self.tmp, "handoff")
        self.outbox = os.path.join(self.handoff, "outbox")
        os.makedirs(os.path.join(self.handoff, "inbox"))
        os.makedirs(self.outbox)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        # Something precious outside the outbox that no trick may reach.
        self.precious = os.path.join(self.tmp, "precious")
        write(os.path.join(self.precious, "keep.txt"))

    def cleanup(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", AGENT_EXEC, "--work", self.work, "--handoff", self.handoff,
                               "cleanup"] + list(args), capture_output=True, text=True)

    def assert_precious_intact(self):
        self.assertTrue(os.path.isfile(os.path.join(self.precious, "keep.txt")))

    def test_removes_only_that_task_folder(self):
        write(os.path.join(self.outbox, "t1", "REPORT.md"))
        write(os.path.join(self.outbox, "t1", "artifacts", "a.txt"))
        write(os.path.join(self.outbox, "t2", "REPORT.md"))
        r = self.cleanup("t1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.outbox), ["t2"])

    def test_missing_folder_is_fine(self):
        r = self.cleanup("never-made")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_path_tricks_are_refused(self):
        # A task id that tries to walk out of the outbox, or to hit the outbox itself or the inbox.
        os.symlink(self.precious, os.path.join(self.tmp, "lnk"))
        for bad in ("..", "../precious", "../../precious", "t1/../../precious", "/etc", ".", "", ".hidden",
                    "a/b", "-rf", "t1\n", "..%2fprecious", "x" * 200):
            r = self.cleanup(bad)
            self.assertNotEqual(r.returncode, 0, f"{bad!r} was accepted")
            self.assert_precious_intact()
            self.assertTrue(os.path.isdir(self.outbox))
            self.assertTrue(os.path.isdir(os.path.join(self.handoff, "inbox")))

    def test_extra_arguments_are_refused(self):
        write(os.path.join(self.outbox, "t1", "REPORT.md"))
        for extra in (["t2"], ["--force"], ["--", "rm"]):
            r = self.cleanup("t1", *extra)
            self.assertNotEqual(r.returncode, 0, extra)
        self.assertTrue(os.path.isdir(os.path.join(self.outbox, "t1")))

    def test_task_folder_that_is_a_link_is_refused(self):
        os.symlink(self.precious, os.path.join(self.outbox, "t1"))
        r = self.cleanup("t1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("link", r.stderr)
        self.assert_precious_intact()
        self.assertTrue(os.path.islink(os.path.join(self.outbox, "t1")))

    def test_task_entry_that_is_a_file_is_refused(self):
        write(os.path.join(self.outbox, "t1"))
        self.assertNotEqual(self.cleanup("t1").returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.outbox, "t1")))

    def test_links_inside_the_folder_are_removed_not_followed(self):
        write(os.path.join(self.outbox, "t1", "REPORT.md"))
        os.symlink(self.precious, os.path.join(self.outbox, "t1", "dir-link"))
        os.symlink(os.path.join(self.precious, "keep.txt"), os.path.join(self.outbox, "t1", "file-link"))
        r = self.cleanup("t1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.lexists(os.path.join(self.outbox, "t1")))
        self.assert_precious_intact()

    def test_outbox_that_is_a_link_is_refused(self):
        shutil.rmtree(self.outbox)
        write(os.path.join(self.precious, "t1", "x"))
        os.symlink(self.precious, self.outbox)
        r = self.cleanup("t1")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.precious, "t1", "x")))


class RunnerCleanupTest(unittest.TestCase):
    """The runner removes the outbox only after the results are uploaded and the finish is acknowledged."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["SWARM_HANDOFF_DIR"] = os.path.join(self.tmp, "handoff")
        sys.path.insert(0, SERVER)
        import swarm_worker
        self.sw = swarm_worker

    def runner(self, finished, collected, uploaded):
        r = self.sw.Runner.__new__(self.sw.Runner)
        r.task = {"id": "t1"}
        r.inbox = os.path.join(self.tmp, "inbox", "t1")
        r.outbox = os.path.join(self.tmp, "outbox", "t1")
        write(os.path.join(r.inbox, "brief.md"))
        write(os.path.join(r.outbox, "REPORT.md"))
        r.finished, r.collected, r.uploaded = finished, collected, uploaded
        r.lost = False
        r.calls = []
        r.agent = lambda *a, **k: r.calls.append(a) or subprocess.CompletedProcess(a, 0, "", "")
        return r

    def test_saved_results_lead_to_cleanup(self):
        r = self.runner(finished=True, collected=True, uploaded=True)
        r.cleanup_handoff()
        self.assertEqual(r.calls, [("cleanup", "t1")])
        self.assertFalse(os.path.exists(r.inbox))

    def test_upload_not_finished_keeps_the_outbox(self):
        r = self.runner(finished=True, collected=True, uploaded=False)
        r.cleanup_handoff()
        self.assertEqual(r.calls, [])
        self.assertTrue(os.path.exists(r.outbox))
        self.assertFalse(os.path.exists(r.inbox))

    def test_finish_not_acknowledged_keeps_the_outbox(self):
        r = self.runner(finished=False, collected=True, uploaded=True)
        r.cleanup_handoff()
        self.assertEqual(r.calls, [])

    def test_cancelled_run_removes_the_outbox_at_once(self):
        # Install test 4, defect 4: a cancelled run never finishes, but the backend will not take its results.
        r = self.runner(finished=False, collected=True, uploaded=False)
        r.lost = True
        r.cleanup_handoff()
        self.assertEqual(r.calls, [("cleanup", "t1")])

    def test_a_failed_cleanup_does_not_raise(self):
        r = self.runner(finished=True, collected=True, uploaded=True)
        r.agent = lambda *a, **k: subprocess.CompletedProcess(a, 3, "", "refusing")
        r.cleanup_handoff()   # logs, does not raise

    def test_collect_resets_uploaded(self):
        r = self.runner(finished=False, collected=False, uploaded=True)
        r.agent = lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps({"files": {}}), "")
        r.workname = "t1"
        r.collect()
        self.assertTrue(r.collected)
        self.assertFalse(r.uploaded)


class DailyCleanupOutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.outbox = os.path.join(self.tmp, "handoff", "outbox")
        os.makedirs(self.outbox)
        os.makedirs(os.path.join(self.tmp, "work"))
        os.environ.update(SWARM_WORK_DIR=os.path.join(self.tmp, "work"), SWARM_BACKUP_DIR=os.path.join(self.tmp, "backups"),
                          SWARM_HANDOFF_DIR=os.path.join(self.tmp, "handoff"))
        self.c = load("swarm_cleanup_outbox", os.path.join(SERVER, "swarm_cleanup.py"))
        self.old = 1_000_000

    def make(self, name, old=True):
        p = os.path.join(self.outbox, name)
        write(os.path.join(p, "REPORT.md"), f"report {name}\n")
        write(os.path.join(p, "artifacts", "a.txt"))
        if old:
            for root, dirs, files in os.walk(p):
                for n in dirs + files:
                    os.utime(os.path.join(root, n), (self.old, self.old))
            os.utime(p, (self.old, self.old))
        return p

    def test_old_finished_outbox_is_backed_up_then_removed(self):
        self.make("d1")
        self.c.act({"d1": {"state": "done", "finished_at": self.old}}, dry=False)
        self.assertFalse(os.path.exists(os.path.join(self.outbox, "d1")))
        (day,) = os.listdir(os.path.join(self.tmp, "backups"))
        with tarfile.open(os.path.join(self.tmp, "backups", day, "outbox-d1.files.tar.gz")) as tf:
            self.assertEqual(tf.extractfile("outbox-d1/REPORT.md").read(), b"report d1\n")

    def test_active_unknown_recent_and_linked_outboxes_stay(self):
        for name in ("q1", "w1", "x1"):
            self.make(name)
        self.make("recent", old=False)
        target = os.path.join(self.tmp, "elsewhere")
        write(os.path.join(target, "keep.txt"))
        os.symlink(target, os.path.join(self.outbox, "l1"))
        states = {"q1": {"state": "queued"}, "w1": {"state": "waiting"}, "l1": {"state": "done", "finished_at": self.old},
                  "recent": {"state": "done", "finished_at": self.old}}
        self.c.act(states, dry=False)
        self.assertEqual(sorted(os.listdir(self.outbox)), ["l1", "q1", "recent", "w1", "x1"])
        self.assertTrue(os.path.isfile(os.path.join(target, "keep.txt")))

    def test_list_includes_outbox_names(self):
        self.make("only-in-outbox")
        os.makedirs(os.path.join(self.tmp, "work", "w-task"))
        self.assertEqual(self.c.list_folders(), ["only-in-outbox", "w-task"])


if __name__ == "__main__":
    unittest.main()
