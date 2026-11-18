"""Worker end to end with a fake agent CLI: question, answer, resume, done, script task."""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

FAKE_AGENT = r"""#!/usr/bin/env bash
# fake run-agent: <provider> <model> <workdir> <prompt> [session]
cd "$3"
mkdir -p artifacts .task-context
echo "sess-123" > .task-context/session
if grep -q "Here is the answer" "$4"; then
  [[ "$5" == "sess-123" ]] || { echo "expected resume of sess-123, got '$5'"; exit 3; }
  printf '## Summary\nStatus: done\n\nPainted it %s.\n' "$(grep -o 'Use [a-z]*' "$4")" > REPORT.md
  echo chart > artifacts/chart.txt
  echo done > AGENT_STATUS
else
  printf '## Summary\nStatus: blocked\n' > REPORT.md
  printf 'blocked\nWhich color?\n' > AGENT_STATUS
fi
"""


class WorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        for name, val in (("admin", "adm"), ("worker", "wrk")):
            open(os.path.join(cls.tmp, name), "w").write(val)
        agent = os.path.join(cls.tmp, "fake-agent")
        open(agent, "w").write(FAKE_AGENT)
        os.chmod(agent, os.stat(agent).st_mode | stat.S_IEXEC)
        os.environ.update(SWARM_ADMIN_TOKEN_FILE=os.path.join(cls.tmp, "admin"),
                          SWARM_BACKEND_WORKER_TOKEN_FILE=os.path.join(cls.tmp, "worker"),
                          SWARM_WORKER_TOKEN_FILE=os.path.join(cls.tmp, "worker"),
                          SWARM_WORK_DIR=os.path.join(cls.tmp, "work"), SWARM_RUN_AGENT=agent,
                          SWARM_TASK_SCOPE="0", SWARM_QUIET="1", SWARM_AGENT_USER="",
                          SWARM_HANDOFF_DIR=os.path.join(cls.tmp, "handoff"), SWARM_RUNNER_DIR=os.path.join(cls.tmp, "runner"),
                          SWARM_BOT_KEYS_DIR=os.path.join(cls.tmp, "keys"), SWARM_HEARTBEAT_SECONDS="1")
        import swarm_backend as sb
        cls.srv, cls.api = sb.make_server(os.path.join(cls.tmp, "t.db"), "127.0.0.1:0")
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["SWARM_URL"] = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        import swarm_worker
        cls.w = swarm_worker

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def admin(self, method, path, body=None):
        r = urllib.request.Request(os.environ["SWARM_URL"] + path, method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Authorization": "Bearer adm", "Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            data = resp.read()
            return json.loads(data) if resp.headers.get_content_type() == "application/json" else data.decode()

    def run_one(self):
        task = self.w.api("POST", "/worker/claim", {"worker": "test"})["task"]
        sem = threading.BoundedSemaphore(1)
        sem.acquire()
        self.w.Runner(task, sem).run()
        return self.admin("GET", f"/tasks/{task['id']}")["task"]

    def test_question_answer_resume(self):
        tid = self.admin("POST", "/tasks", {"prompt": "Paint the fence", "owner": "web"})["task"]["id"]
        t = self.run_one()
        self.assertEqual((t["id"], t["state"], t["question"]), (tid, "waiting", "Which color?"))
        self.admin("POST", f"/tasks/{tid}/answer", {"answer": "Use green"})
        t = self.run_one()
        self.assertEqual(t["state"], "done", t)
        self.assertIn("Painted it Use green", self.admin("GET", f"/tasks/{tid}/report"))
        self.assertEqual(t["artifacts"], ["chart.txt"])

    def test_script_task(self):
        tid = self.admin("POST", "/tasks", {"prompt": "run checks", "command": "echo hello-from-script; exit 3"})["task"]["id"]
        t = self.run_one()
        self.assertEqual((t["id"], t["state"], t["outcome"]), (tid, "failed", "script_failed"))
        log = self.admin("GET", f"/tasks/{tid}/artifacts/script.log")
        self.assertIn("hello-from-script", log)


if __name__ == "__main__":
    unittest.main()
