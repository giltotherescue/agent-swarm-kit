"""Publish policy (agent-control PR 65, ported): open_pr veto, PR base selection, terminal target PRs reported honestly,
explicit-null pr_url clearing, and --base persisted on enqueue, follow-ups and answers.

End to end with the fake agent from test_worker and a fake `gh` on PATH that answers from a JSON state file and logs calls.
The kit never merges or deploys, so the merge gate and deploy credit of PR 65 have no counterpart here (see CHANGELOG)."""
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_worker as tw  # noqa: E402

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, re, sys
state = json.load(open(os.environ["FAKE_GH_STATE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
def num(ref):
    return re.search(r"(\d+)/?$", ref)[1]
if args[:2] == ["pr", "view"]:
    pr = state.get("prs", {}).get(num(args[2]))
    if pr is None or state.get("view_fail"):
        sys.stderr.write("GraphQL: Could not resolve to a PullRequest\n"); sys.exit(1)
    print(json.dumps(pr)); sys.exit(0)
if args[:2] == ["pr", "list"]:
    print(state.get("open_pr_on_head", "")); sys.exit(0)
if args[:2] == ["pr", "create"]:
    print("https://github.com/acme/site/pull/99"); sys.exit(0)
if args[:2] == ["repo", "view"]:
    if not state.get("default"):
        sys.stderr.write("HTTP 502\n"); sys.exit(1)
    print(json.dumps({"defaultBranchRef": {"name": state["default"]}})); sys.exit(0)
sys.stderr.write("fake gh: unexpected call\n"); sys.exit(2)
"""


def pr(n, state, base="main", head=None):
    return {"url": f"https://github.com/acme/site/pull/{n}", "state": state, "baseRefName": base,
            "headRefName": head or f"feat/pr-{n}", "isCrossRepository": False}


class PublishPolicyTest(tw.RepoWorkerTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        bindir = os.path.join(cls.tmp, "fakebin")
        os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        open(gh, "w").write(FAKE_GH)
        os.chmod(gh, 0o755)
        cls.old_path = os.environ["PATH"]
        os.environ.update(PATH=bindir + os.pathsep + cls.old_path, FAKE_GH_STATE=os.path.join(cls.tmp, "gh-state.json"),
                          FAKE_GH_LOG=os.path.join(cls.tmp, "gh-log.jsonl"))

    @classmethod
    def tearDownClass(cls):
        os.environ["PATH"] = cls.old_path
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.gh_state({"default": "trunk", "prs": {}})
        open(os.environ["FAKE_GH_LOG"], "w").close()

    def gh_state(self, st):
        with open(os.environ["FAKE_GH_STATE"], "w") as f:
            json.dump(st, f)

    def gh_calls(self, *prefix):
        with open(os.environ["FAKE_GH_LOG"]) as f:
            calls = [json.loads(ln) for ln in f if ln.strip()]
        return [c for c in calls if c[:len(prefix)] == list(prefix)]

    def base_of(self, create):
        return create[create.index("--base") + 1] if "--base" in create else None

    def events(self, tid, typ):
        return [e for e in self.admin("GET", f"/tasks/{tid}?events=1")["task"]["events"] if e["type"] == typ]

    def report(self, tid):
        return self.admin("GET", f"/tasks/{tid}/report")

    def queue(self, prompt, **kw):
        return self.admin("POST", "/tasks", dict({"prompt": prompt, "repo": "acme/site"}, **kw))["task"]["id"]

    # ---------- fresh PR base ----------
    def test_fresh_pr_uses_task_base_then_real_repo_default_never_a_guessed_main(self):
        self.queue("COMMIT b1.txt", open_pr=True, base="release/x")
        t = self.run_one()
        self.assertEqual((t["state"], t["pr_url"]), ("done", "https://github.com/acme/site/pull/99"), t)
        self.assertEqual(self.base_of(self.gh_calls("pr", "create")[-1]), "release/x")
        self.assertIn("task --base", self.events(t["id"], "publish")[-1]["text"])
        # No --base: the repo default GitHub reports (trunk), not main.
        self.queue("COMMIT b2.txt", open_pr=True)
        t = self.run_one()
        self.assertEqual(self.base_of(self.gh_calls("pr", "create")[-1]), "trunk")
        self.assertIn("repo default", self.events(t["id"], "publish")[-1]["text"])

    def test_unknown_repo_default_leaves_base_to_github(self):
        saved = self.w.repo_default_branch
        self.w.repo_default_branch = lambda *a, **k: ""
        try:
            self.queue("COMMIT b3.txt", open_pr=True)
            t = self.run_one()
        finally:
            self.w.repo_default_branch = saved
        self.assertEqual(t["state"], "done", t)
        create = self.gh_calls("pr", "create")[-1]
        self.assertNotIn("--base", create)
        self.assertIn("GitHub to choose", self.events(t["id"], "publish")[-1]["text"])

    def test_repo_default_lookup_falls_back_to_origin_head_then_gives_up(self):
        w = self.w
        def fake(gh_ok, lsremote):
            def sh(cmd, cwd=None, check=True, env=None, timeout=1800):
                if cmd[0] == "gh":
                    return SimpleNamespace(returncode=0 if gh_ok else 1,
                                           stdout=json.dumps({"defaultBranchRef": {"name": "dev"}}) if gh_ok else "", stderr="")
                return SimpleNamespace(returncode=0 if lsremote else 128, stdout=lsremote or "", stderr="")
            return sh
        saved = w.sh
        try:
            w.sh = fake(True, None)
            self.assertEqual(w.repo_default_branch("acme/site"), "dev")
            w.sh = fake(False, "ref: refs/heads/stable\tHEAD\nabc\tHEAD\n")
            self.assertEqual(w.repo_default_branch("acme/site"), "stable")
            w.sh = fake(False, None)
            self.assertEqual(w.repo_default_branch("acme/site"), "")
        finally:
            w.sh = saved
        self.assertEqual(w.fresh_pr_base(None, "p-base", lambda: "trunk")[0], "p-base")
        self.assertEqual(w.fresh_pr_base("t-base", "p-base", lambda: "trunk")[0], "t-base")
        self.assertEqual(w.fresh_pr_base(None, None, lambda: "")[0], "")

    # ---------- open_pr veto ----------
    def test_push_only_task_never_opens_a_pr(self):
        tid = self.queue("COMMIT v1.txt", push=True)
        t = self.run_one()
        self.assertEqual((t["id"], t["state"], t["pr_url"]), (tid, "done", None), t)
        self.assertEqual(self.gh_calls("pr", "create"), [])
        self.assertEqual(self.files_on(f"agent/{tid}"), ["README.md", "v1.txt"])

    def test_backend_refuses_a_pr_url_from_a_task_without_open_pr(self):
        tid = self.queue("plain push", push=True)
        c = self.w.api("POST", "/worker/claim", {"worker": "test"})["task"]
        self.assertEqual(c["id"], tid)
        self.w.http("POST", f"/tasks/{tid}/finish", c["task_token"],
                    {"status": "done", "claim_id": c["claim_id"], "pr_url": "https://github.com/acme/site/pull/5"})
        t = self.admin("GET", f"/tasks/{tid}")["task"]
        self.assertIsNone(t["pr_url"])
        self.assertTrue(any("did not allow opening a PR" in e["text"] for e in self.events(tid, "warning")))

    # ---------- explicit null clears, absent keeps ----------
    def test_explicit_null_pr_url_clears_and_absent_keeps(self):
        url = "https://github.com/acme/site/pull/42"
        for body, want in (({"pr_url": None}, None), ({}, url)):
            tid = self.queue(f"clear {want}", open_pr=True)
            self.api.s.run("UPDATE tasks SET pr_url = ? WHERE id = ?", url, tid)
            c = self.w.api("POST", "/worker/claim", {"worker": "test"})["task"]
            self.w.http("POST", f"/tasks/{tid}/finish", c["task_token"], dict(body, status="failed", claim_id=c["claim_id"]))
            self.assertEqual(self.admin("GET", f"/tasks/{tid}")["task"]["pr_url"], want)

    # ---------- target PRs ----------
    def test_open_target_pr_is_updated_and_reported_without_a_new_pr(self):
        self.gh_state({"default": "trunk", "prs": {"7": pr(7, "OPEN")}})
        tid = self.queue("COMMIT t1.txt", pr_number=7)
        t = self.run_one()
        self.assertEqual((t["id"], t["state"], t["pr_url"]), (tid, "done", "https://github.com/acme/site/pull/7"), t)
        self.assertEqual(self.gh_calls("pr", "create"), [])
        self.assertEqual(self.files_on("feat/pr-7"), ["README.md", "t1.txt"])

    def test_merged_target_without_open_pr_pushes_and_reports_it_honestly(self):
        self.gh_state({"default": "trunk", "prs": {"8": pr(8, "MERGED")}})
        tid = self.queue("COMMIT t2.txt", pr_number=8)
        self.api.s.run("UPDATE tasks SET pr_url = ? WHERE id = ?", "https://github.com/acme/site/pull/8", tid)  # stale, earlier claim
        t = self.run_one()
        self.assertEqual((t["id"], t["state"]), (tid, "done"), t)
        self.assertIsNone(t["pr_url"], "a merged target is never reported as this task's PR")
        self.assertEqual(self.gh_calls("pr", "create"), [])
        self.assertEqual(self.files_on("feat/pr-8"), ["README.md", "t2.txt"], "the work is still pushed")
        note = self.events(tid, "publish")[-1]
        self.assertEqual(note["meta"], {"target_pr_state": "MERGED"})
        for s in ("was merged before publish", "feat/pr-8", "open_pr not set", "not reported as this task's PR or as merged"):
            self.assertIn(s, note["text"])
        self.assertIn("## Publish note", self.report(tid))

    def test_closed_target_replacement_uses_the_target_live_base(self):
        self.gh_state({"default": "trunk", "prs": {"9": pr(9, "CLOSED", base="release/live")}})
        tid = self.queue("COMMIT t3.txt", pr_number=9, open_pr=True, base="other-base")
        t = self.run_one()
        self.assertEqual((t["state"], t["pr_url"]), ("done", "https://github.com/acme/site/pull/99"), t)
        self.assertEqual(self.base_of(self.gh_calls("pr", "create")[-1]), "release/live")
        note = self.events(tid, "publish")[-1]["text"]
        self.assertIn("task --base other-base not used", note)
        self.assertIn("replacement PR", self.report(tid))

    def test_terminal_target_with_unknown_base_opens_nothing(self):
        self.gh_state({"default": "trunk", "prs": {"10": pr(10, "MERGED", base="")}})
        tid = self.queue("COMMIT t4.txt", pr_number=10, open_pr=True, base="release/x")
        t = self.run_one()
        self.assertEqual(self.gh_calls("pr", "create"), [], "never falls back to --base or the default for a target")
        self.assertIsNone(t["pr_url"])
        self.assertEqual((t["state"], t["outcome"]), ("waiting", "blocked"), t)  # a PR was asked for and none exists
        self.assertIn("live base branch is unknown", self.events(tid, "publish")[-1]["text"])
        self.admin("POST", f"/tasks/{tid}/cancel", {})

    def test_follow_up_on_a_merged_inherited_pr_opens_a_replacement_against_its_live_base(self):
        pid = self.queue("COMMIT f1.txt", open_pr=True, base="parent-base")
        p = self.run_one()
        self.assertEqual(p["pr_url"], "https://github.com/acme/site/pull/99")
        self.gh_state({"default": "trunk", "prs": {"99": pr(99, "MERGED", base="release/live", head=f"agent/{pid}")}})
        open(os.environ["FAKE_GH_LOG"], "w").close()
        fid = self.admin("POST", "/tasks", {"follow_up": pid, "prompt": "EXPECT_RESUME COMMIT f2.txt"})["task"]["id"]
        f = self.run_one()
        self.assertEqual((f["id"], f["state"]), (fid, "done"), f)
        creates = self.gh_calls("pr", "create")
        self.assertEqual(self.base_of(creates[-1]), "release/live", "the target's live base wins over the parent's")
        self.assertEqual(self.gh_calls("pr", "list"), [], "the inherited PR is the target; no head lookup")
        self.assertIn("parent-base not used", self.events(fid, "publish")[-1]["text"])

    def test_follow_up_without_new_commits_on_a_merged_target_reports_and_opens_nothing(self):
        w = self.w
        t = {"id": "x", "repo": "acme/site", "open_pr": 1, "prompt": "p", "follow_up": 1,
             "parent": {"pr_url": "https://github.com/acme/site/pull/11", "pushed_branch": "agent/p"}}
        self.gh_state({"default": "trunk", "prs": {"11": pr(11, "MERGED", head="agent/p")}})
        r = w.Runner.__new__(w.Runner)
        r.task, r.clone = t, None
        out = r.publish_pr("agent/p", "a" * 40, 0)
        self.assertEqual((out["pr_url"], out["target_pr_state"]), (None, "MERGED"))
        self.assertIn("added no new commits", out["publish_note"])
        self.assertEqual(self.gh_calls("pr", "create"), [])

    # ---------- --base persisted ----------
    def test_base_is_persisted_on_enqueue_and_inherited_by_follow_ups(self):
        tid = self.queue("COMMIT p1.txt", push=True, base="release/x")
        self.assertEqual(self.admin("GET", f"/tasks/{tid}")["task"]["base"], "release/x")
        self.run_one()
        f = self.admin("POST", "/tasks", {"follow_up": tid, "prompt": "more"})["task"]
        self.assertEqual(f["base"], "release/x")
        g = self.admin("POST", "/tasks", {"follow_up": tid, "prompt": "more again", "base": "release/y"})["task"]
        self.assertEqual(g["base"], "release/y")
        for x in (f, g):
            self.admin("POST", f"/tasks/{x['id']}/cancel", {})
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.queue("bad", push=True, base="../evil")
        self.assertEqual(e.exception.code, 400)

    def test_answering_a_waiting_task_keeps_base_only_on_a_successful_answer(self):
        pid = self.queue("NEED_FILE nothing.txt", push=True, base="old-base")
        self.assertEqual(self.run_one()["state"], "waiting")
        for bad in ({"prompt": "go", "open_pr": True, "base": "evil-base"}, {"prompt": "   ", "base": "evil-base"},
                    {"prompt": "go", "base": "../evil"}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.admin("POST", "/tasks", dict(bad, follow_up=pid))
            self.assertEqual(e.exception.code, 400)
            t = self.admin("GET", f"/tasks/{pid}")["task"]
            self.assertEqual((t["state"], t["base"]), ("waiting", "old-base"))
        r = self.admin("POST", "/tasks", {"follow_up": pid, "prompt": "Use the other file", "base": "release/r37"})
        self.assertTrue(r["answered_parent"])
        self.assertEqual((r["task"]["state"], r["task"]["base"]), ("queued", "release/r37"))
        self.admin("POST", f"/tasks/{pid}/cancel", {})


# The inherited end-to-end tests already run in test_worker; only this file's tests run here.
for _name in dir(tw.RepoWorkerTest):
    if _name.startswith("test_") and _name not in PublishPolicyTest.__dict__:
        setattr(PublishPolicyTest, _name, None)

if __name__ == "__main__":
    unittest.main()
