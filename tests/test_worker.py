"""Worker end to end with a fake agent CLI: question, answer, resume, done, script task."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
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

CHAIN_AGENT = r"""#!/usr/bin/env bash
# fake run-agent for chain cycles: checks the time limit, the fresh session and the handed-on checkpoint
cd "$3"
mkdir -p artifacts .task-context
echo "timeout=$SWARM_TASK_TIMEOUT session=${5:-none}" >> CALLS
note="first cycle"
if grep -q "cycle 1 of at most 2" "$4"; then
  echo "found bug A" > CHECKPOINT.md
elif grep -q "cycle 2 of at most 2" "$4"; then
  [[ ! -e CHECKPOINT.md ]] || { echo "stale checkpoint left in the folder"; exit 3; }
  grep -q "found bug A" .task-context/attachments/checkpoint.md && note="checkpoint ok"
fi
echo "sess-chain" > .task-context/session
printf '## Summary\nStatus: done\n\n%s\n' "$note" > REPORT.md
echo done > AGENT_STATUS
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
        # The module reads settings at import; set them again in case another test file imported it first.
        w = swarm_worker
        w.URL = os.environ["SWARM_URL"]
        w.WORK = os.environ["SWARM_WORK_DIR"]
        w.HANDOFF = os.environ["SWARM_HANDOFF_DIR"]
        w.RUNNER_DIR = os.environ["SWARM_RUNNER_DIR"]
        w.RUN_AGENT = agent
        w.AGENT_USER = ""
        w.HEARTBEAT_SECONDS = 1
        w.STATE_FILE = os.path.join(cls.tmp, "runner", "runner-state.json")
        w.DRAIN_FILE = os.path.join(cls.tmp, "runner", "drain")
        cls.fake_agent = agent

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

    def tearDown(self):
        self.w.RUN_AGENT = self.fake_agent
        self.w.TIMEOUT = 14400

    def test_question_answer_resume(self):
        tid = self.admin("POST", "/tasks", {"prompt": "Paint the fence", "owner": "web"})["task"]["id"]
        t = self.run_one()
        self.assertEqual((t["id"], t["state"], t["question"]), (tid, "waiting", "Which color?"))
        self.admin("POST", f"/tasks/{tid}/answer", {"answer": "Use green"})
        t = self.run_one()
        self.assertEqual(t["state"], "done", t)
        self.assertIn("Painted it Use green", self.admin("GET", f"/tasks/{tid}/report"))
        self.assertEqual(t["artifacts"], ["chart.txt"])

    def test_chain_cycles_hand_on_a_checkpoint_with_a_fresh_session(self):
        chain_agent = os.path.join(self.tmp, "chain-agent")
        with open(chain_agent, "w") as fh:
            fh.write(CHAIN_AGENT)
        os.chmod(chain_agent, 0o755)
        self.w.RUN_AGENT = chain_agent
        root = self.admin("POST", "/tasks", {"prompt": "Polish the app", "chain": True, "max_cycles": 2,
                                             "timeout_minutes": 90})["task"]["id"]
        t1 = self.run_one()
        self.assertEqual((t1["id"], t1["state"]), (root, "done"), t1)
        self.assertEqual(self.admin("GET", f"/tasks/{root}/artifacts/checkpoint.md").strip(), "found bug A")
        t2 = self.run_one()
        self.assertEqual((t2["state"], t2["cycle"], t2["parent_id"]), ("done", 2, root), t2)
        calls = open(os.path.join(self.w.WORK, root, "CALLS")).read().splitlines()
        self.assertEqual(calls, ["timeout=5400 session=none", "timeout=5400 session=none"])
        self.assertIn("checkpoint ok", self.admin("GET", f"/tasks/{t2['id']}/report"))
        self.assertIsNone(self.w.api("POST", "/worker/claim", {"worker": "test"})["task"])  # cycle 2 was the last

    def test_script_task(self):
        tid = self.admin("POST", "/tasks", {"prompt": "run checks", "command": "echo hello-from-script; exit 3"})["task"]["id"]
        t = self.run_one()
        self.assertEqual((t["id"], t["state"], t["outcome"]), (tid, "failed", "script_failed"))
        log = self.admin("GET", f"/tasks/{tid}/artifacts/script.log")
        self.assertIn("hello-from-script", log)



REPO_AGENT = r"""#!/usr/bin/env bash
# fake run-agent for repo tasks. Words in the prompt drive it. The environment is scrubbed, so paths are baked in.
set -e
cd "$3"
P="$4"; S="${5:-}"
echo "$1 $S" >> CALLS
mkdir -p artifacts .task-context
[[ -n "$S" ]] || S="sess-$RANDOM"
echo "$S" > ".task-context/session-$1"
G=(git -c user.name=t -c user.email=t@example.com)
if grep -q "EXPECT_RESUME" "$P" && [[ "${5:-}" != sess-* ]]; then echo "expected a resumed session"; exit 3; fi
if grep -q "NEED_FILE" "$P"; then
  need=$(grep -o 'NEED_FILE [A-Za-z0-9_.-]*' "$P" | head -1 | cut -d' ' -f2)
  if [[ ! -f "$need" ]]; then printf '## Summary\nStatus: blocked\n' > REPORT.md; printf 'blocked\nmissing %s\n' "$need" > AGENT_STATUS; exit 0; fi
fi
if grep -q "LEAK" "$P"; then echo "token=gh""p_$(printf 'A%.0s' $(seq 36))" > leak.txt; git add leak.txt; "${G[@]}" commit -qm leak; fi
if grep -q "SLEEP" "$P"; then trap '' TERM; sleep 60; fi
f=$(grep -o 'COMMIT [A-Za-z0-9_.-]*' "$P" | head -1 | cut -d' ' -f2 || true)
if [[ -n "$f" ]]; then echo "$f" > "$f"; git add "$f"; "${G[@]}" commit -qm "add $f"; fi
if grep -q "REVIEW" "$P" || grep -q "path:line" "$P"; then
  if grep -q "path:line" "$P"; then printf '## Summary\nStatus: done\n\n## Findings\n- High: bad thing at server/run-agent:42\n' > REPORT.md
  else printf '## Summary\nStatus: done\n\n## Findings\n- High: bad thing somewhere\n' > REPORT.md; fi
else printf '## Summary\nStatus: done\n\nDid it.\n' > REPORT.md; fi
echo done > AGENT_STATUS
"""


class RepoWorkerTest(WorkerTest):
    """The runner clones and pushes from its own copy; the agent only gets a bundle (must-fix 1, 5, 8)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.git_root = os.path.join(cls.tmp, "remotes")
        bare = os.path.join(cls.git_root, "acme", "site.git")
        os.makedirs(os.path.dirname(bare))
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        seed = os.path.join(cls.tmp, "seed")
        subprocess.run(["git", "clone", "-q", bare, seed], check=True, capture_output=True)
        open(os.path.join(seed, "README.md"), "w").write("site\n")
        g = ["git", "-C", seed, "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["commit", "-qm", "init"], check=True)
        subprocess.run(g + ["push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)
        cls.bare = bare
        cls.repo_agent = os.path.join(cls.tmp, "repo-agent")
        open(cls.repo_agent, "w").write(REPO_AGENT)
        os.chmod(cls.repo_agent, 0o755)
        cls.w.GIT_URL = "file://" + cls.git_root + "/{repo}.git"

    def setUp(self):
        self.w.RUN_AGENT = self.repo_agent

    def files_on(self, branch):
        r = subprocess.run(["git", "-C", self.bare, "ls-tree", "--name-only", branch], capture_output=True, text=True)
        return sorted(r.stdout.split()) if r.returncode == 0 else None

    test_question_answer_resume = None  # inherited setup only; these two run in WorkerTest
    test_script_task = None

    def test_push_then_follow_up_resumes_the_parent_folder(self):
        tid = self.admin("POST", "/tasks", {"prompt": "COMMIT a.txt", "repo": "acme/site", "push": True})["task"]["id"]
        t = self.run_one()
        self.assertEqual((t["id"], t["state"]), (tid, "done"), t)
        self.assertEqual(t["pushed_branch"], f"agent/{tid}")
        self.assertEqual(self.files_on(f"agent/{tid}"), ["README.md", "a.txt"])
        self.assertEqual(self.files_on("main"), ["README.md"], "main is never touched")
        # Two follow-ups at once: the second is held behind the first.
        f1 = self.admin("POST", "/tasks", {"follow_up": tid, "prompt": "EXPECT_RESUME NEED_FILE a.txt COMMIT b.txt"})
        f2 = self.admin("POST", "/tasks", {"follow_up": tid, "prompt": "EXPECT_RESUME NEED_FILE b.txt COMMIT c.txt"})
        self.assertEqual(f2["held_behind"], f1["task"]["id"])
        self.assertEqual(f1["task"]["branch"], f"agent/{tid}")
        t1 = self.run_one()
        self.assertEqual((t1["id"], t1["state"]), (f1["task"]["id"], "done"), t1)
        self.assertEqual(t1["workdir"], tid, "follow-up ran in the parent's folder")
        t2 = self.run_one()
        self.assertEqual((t2["id"], t2["state"]), (f2["task"]["id"], "done"), t2)
        self.assertEqual(self.files_on(f"agent/{tid}"), ["README.md", "a.txt", "b.txt", "c.txt"])
        # Follow-up rules for tasks still in flight.
        q = self.admin("POST", "/tasks", {"prompt": "queued one", "repo": "acme/site"})["task"]["id"]
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.admin("POST", "/tasks", {"follow_up": q, "prompt": "more"})
        self.assertEqual(e.exception.code, 409)
        self.admin("POST", f"/tasks/{q}/cancel", {})

    def test_follow_up_restores_unpushed_parent_work(self):
        pid = self.admin("POST", "/tasks", {"prompt": "COMMIT local.txt", "repo": "acme/site"})["task"]["id"]
        p = self.run_one()
        self.assertEqual((p["state"], p["unpushed"]), ("done", 1), p)
        self.assertIsNone(self.files_on(f"agent/{pid}"), "nothing was pushed")
        shutil.rmtree(os.path.join(self.w.WORK, pid))  # the folder is gone (cleanup or another worker)
        fid = self.admin("POST", "/tasks", {"follow_up": pid, "prompt": "NEED_FILE local.txt COMMIT more.txt", "push": True})["task"]["id"]
        t = self.run_one()
        self.assertEqual((t["id"], t["state"]), (fid, "done"), t)
        self.assertEqual(self.files_on(f"agent/{pid}"), ["README.md", "local.txt", "more.txt"])

    def test_follow_up_answers_a_waiting_parent(self):
        pid = self.admin("POST", "/tasks", {"prompt": "NEED_FILE nothing.txt", "repo": "acme/site", "push": True})["task"]["id"]
        self.assertEqual(self.run_one()["state"], "waiting")
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.admin("POST", "/tasks", {"follow_up": pid, "prompt": "go", "open_pr": True})
        self.assertEqual(e.exception.code, 400)
        r = self.admin("POST", "/tasks", {"follow_up": pid, "prompt": "Use the other file"})
        self.assertTrue(r["answered_parent"])
        self.assertEqual((r["task"]["id"], r["task"]["state"], r["task"]["pending_answer"]), (pid, "queued", "Use the other file"))
        self.admin("POST", f"/tasks/{pid}/cancel", {})

    def test_default_branch_is_never_pushed(self):
        self.admin("POST", "/tasks", {"prompt": "COMMIT x.txt", "repo": "acme/site", "push": True, "branch": "main"})
        t = self.run_one()
        self.assertEqual((t["state"], t["outcome"]), ("failed", "publish_failed"), t)
        self.assertEqual(self.files_on("main"), ["README.md"])

    def test_secret_in_new_lines_blocks_the_push(self):
        tid = self.admin("POST", "/tasks", {"prompt": "LEAK COMMIT y.txt", "repo": "acme/site", "push": True})["task"]["id"]
        t = self.run_one()
        self.assertEqual((t["state"], t["outcome"]), ("failed", "publish_failed"), t)
        self.assertIn("leak.txt:1", t["error"])
        self.assertNotIn("AAAAAAAA", t["error"])
        self.assertIsNone(self.files_on(f"agent/{tid}"))

    def test_cancel_stops_a_run_that_ignores_stop_signals(self):
        old = self.w.STOP_GRACE
        self.w.STOP_GRACE = "2"
        try:
            tid = self.admin("POST", "/tasks", {"prompt": "SLEEP COMMIT z.txt", "repo": "acme/site", "push": True})["task"]["id"]
            task = self.w.api("POST", "/worker/claim", {"worker": "test"})["task"]
            sem = threading.BoundedSemaphore(1)
            sem.acquire()
            r = self.w.Runner(task, sem)
            th = threading.Thread(target=r.run)
            th.start()
            time.sleep(3)
            self.admin("POST", f"/tasks/{tid}/cancel", {})
            th.join(30)
            self.assertFalse(th.is_alive(), "runner stopped")
            t = self.admin("GET", f"/tasks/{tid}")["task"]
            self.assertEqual(t["state"], "cancelled")
            self.assertIsNone(self.files_on(f"agent/{tid}"), "nothing published after cancel")
            ps = subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True, text=True).stdout.split()
            pgid = int(open(os.path.join(self.w.WORK, ".swarm-pids", tid)).read())
            self.assertFalse(any(os.getpgid(int(p)) == pgid for p in ps if os.path.exists(f"/proc/{p}")), "agent killed")
        finally:
            self.w.STOP_GRACE = old

    def test_time_limit_marks_failed(self):
        self.w.TIMEOUT = 2
        os.environ["SWARM_TASK_TIMEOUT"] = "2"
        self.admin("POST", "/tasks", {"prompt": "slow", "command": "sleep 20"})
        t = self.run_one()
        os.environ.pop("SWARM_TASK_TIMEOUT")
        self.assertEqual((t["state"], t["outcome"]), ("failed", "timeout"), t)

    def test_review_gate_asks_once_for_path_line(self):
        tid = self.admin("POST", "/tasks", {"prompt": "REVIEW please", "kind": "review", "report_only": True})["task"]["id"]
        t = self.run_one()
        self.assertEqual(t["state"], "done", t)
        self.assertIn("server/run-agent:42", self.admin("GET", f"/tasks/{tid}/report"))
        calls = open(os.path.join(self.w.WORK, tid, "CALLS")).read().splitlines()
        self.assertEqual(len(calls), 2)

    def test_agent_environment_holds_no_worker_token(self):
        tid = self.admin("POST", "/tasks", {"prompt": "show env", "command": "env > artifacts/env.txt; id -un >> artifacts/env.txt"})["task"]["id"]
        self.assertEqual(self.run_one()["state"], "done")
        env = self.admin("GET", f"/tasks/{tid}/artifacts/env.txt")
        self.assertNotIn("SWARM_WORKER_TOKEN_FILE", env)
        self.assertNotIn("wrk", [ln.split("=", 1)[-1] for ln in env.splitlines()])
        self.assertNotIn("SWARM_URL", env)
        self.assertIn("SWARM_TASK_ID=" + tid, env)


if __name__ == "__main__":
    unittest.main()
