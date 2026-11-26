"""Code review policy (server/swarm_codereview.py, docs/code-review.md): tiers, caps, reviewer pick, recheck scope,
residuals to a review-followup issue, and swarmctl review / review-tier."""
import json
import os
import subprocess
import tempfile
import unittest

from helpers import ADMIN, SERVER, Backend
import swarm_codereview as cr


class TierTest(unittest.TestCase):
    def test_high_risk_paths(self):
        for p in ("app/auth/login.ts", "src/billing/stripe_webhook.py", "db/migrations/0042_add.sql", "scripts/backup.sh",
                  "scripts/restore-backup.sh", "lib/retention.rb", "skills/publish-post/SKILL.md", "api/entitlements.go"):
            self.assertEqual(cr.classify([p])[0], "high", p)

    def test_ordinary_and_docs(self):
        self.assertEqual(cr.classify(["src/components/Button.tsx", "README.md"])[0], "ordinary")
        self.assertEqual(cr.classify(["docs/setup.md", "CHANGELOG.md", "LICENSE"])[0], "none")
        # prose about billing is still docs-only
        self.assertEqual(cr.classify(["docs/billing.md"])[0], "none")
        self.assertEqual(cr.classify([])[0], "ordinary")

    def test_comment_only_diff(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-# teh cache\n+# the cache\n"
        self.assertEqual(cr.classify(["src/x.py"], diff)[0], "none")
        self.assertEqual(cr.classify(["src/x.py"], diff + "+x = 1\n")[0], "ordinary")

    def test_override_and_env_globs(self):
        self.assertEqual(cr.classify(["src/ui.tsx"], override="high")[0], "high")
        self.assertEqual(cr.classify(["src/auth.ts"], override="ordinary")[0], "ordinary")
        self.assertEqual(cr.classify(["src/auth.ts"], override="none")[0], "high")  # cannot skip a high-risk path
        globs = cr.extra_high_risk_globs({"SWARM_REVIEW_HIGH_RISK_PATHS": "infra/*, ops/**"})
        self.assertEqual(cr.classify(["infra/dns.tf"], extra_globs=globs)[0], "high")

    def test_caps(self):
        self.assertEqual((cr.CAPS["ordinary"], cr.CAPS["high"]), (2, 3))


class ReviewerTest(unittest.TestCase):
    def test_opposite_family(self):
        env = {"SWARM_REVIEW_CODEX_MODEL": "sol", "SWARM_REVIEW_CODEX_HIGH_MODEL": "astra"}
        self.assertEqual(cr.reviewer_for("claude", "ordinary", env), ("codex", "sol"))
        self.assertEqual(cr.reviewer_for("claude", "high", env), ("codex", "astra"))
        self.assertEqual(cr.reviewer_for("codex", "high", env), ("claude", "opus"))
        self.assertEqual(cr.reviewer_for("claude", "high", {}), ("codex", "default"))

    def test_marker_round_trip(self):
        m = cr.parse_marker("Review it.\n" + cr.marker(2, "high", "policy", "t1") + "\n")
        self.assertEqual((m["round"], m["tier"], m["model"], m["prior"]), (2, "high", "policy", "t1"))
        self.assertIsNone(cr.parse_marker("no marker"))
        m = cr.parse_marker(cr.marker(1, author="codex", followups=False))
        self.assertEqual((m["author"], m["followups"]), ("codex", "report"))
        self.assertEqual(cr.parse_marker(cr.marker(1))["followups"], "issue")
        self.assertNotIn("author", cr.parse_marker(cr.marker(1)))


class ShipCheckTest(unittest.TestCase):
    def test_only_a_pass_on_the_current_head_with_green_ci(self):
        green = {"headRefOid": "abc123", "statusCheckRollup": [{"name": "unittest", "conclusion": "SUCCESS"},
                                                                {"context": "lint", "state": "SUCCESS"}]}
        self.assertTrue(cr.ship_check("abc123", green)[0])
        self.assertIn("not the reviewed", cr.ship_check("old999", green)[1])
        self.assertFalse(cr.ship_check(None, green)[0])
        self.assertFalse(cr.ship_check("abc123", None)[0])
        self.assertFalse(cr.ship_check("abc123", {"headRefOid": "abc123", "statusCheckRollup": []})[0])
        pending = {"headRefOid": "abc123", "statusCheckRollup": [{"name": "unittest", "status": "IN_PROGRESS", "conclusion": ""}]}
        self.assertIn("pending", cr.ship_check("abc123", pending)[1])
        red = {"headRefOid": "abc123", "statusCheckRollup": [{"name": "unittest", "conclusion": "FAILURE"}]}
        self.assertFalse(cr.ship_check("abc123", red)[0])

    def test_findings_sections(self):
        self.assertTrue(cr.has_findings_sections(REPORT))
        self.assertTrue(cr.has_findings_sections("## Must-fix findings\n- None\n"))
        self.assertFalse(cr.has_findings_sections("## Summary\nok\n## Findings\n- High: bad\n"))
        self.assertFalse(cr.has_findings_sections(""))


REPORT = """## Summary
Merge after small fixes

## Must-fix findings
1. src/auth.ts:10 skips the session check on the admin route.
   Trigger: any logged-out visitor opens /admin.
2. src/pay.ts:4 double charges on retry

## Follow-up findings
- src/a.ts:3 hardening: clamp the page size
- None beyond these

## Verification
- ran the unit tests
"""


class FindingsTest(unittest.TestCase):
    def test_split(self):
        must, follow = cr.split_findings(REPORT)
        self.assertEqual(len(must), 2)
        self.assertIn("Trigger: any logged-out visitor", must[0])
        self.assertEqual(follow, ["src/a.ts:3 hardening: clamp the page size"])
        self.assertEqual(cr.split_findings("## Must-fix findings\n- None\n## Follow-up findings\n- None\n"), ([], []))

    def test_file_followups_creates_issue_and_links_pr(self):
        calls = []

        def run(cmd):
            calls.append(cmd)
            if cmd[:3] == ["gh", "issue", "list"]:
                return 0, "\n"
            if cmd[:3] == ["gh", "issue", "create"]:
                return 0, "https://github.com/acme/app/issues/9\n"
            return 0, ""
        url, note = cr.file_followups("acme/app", 12, ["src/a.ts:3 clamp it"], run, "t1")
        self.assertEqual((url, note), ("https://github.com/acme/app/issues/9", None))
        create = next(c for c in calls if c[:3] == ["gh", "issue", "create"])
        self.assertIn("review-followup", create)
        self.assertTrue(create[create.index("--title") + 1].startswith("PR #12 review follow-ups: "))
        self.assertIn(["gh", "pr", "comment", "12", "-R", "acme/app", "--body",
                       "Review follow-ups (non-blocking): https://github.com/acme/app/issues/9"], calls)

    def test_file_followups_reuses_open_issue(self):
        calls = []

        def run(cmd):
            calls.append(cmd)
            return (0, "https://github.com/acme/app/issues/9\n") if cmd[:3] == ["gh", "issue", "list"] else (0, "")
        url, note = cr.file_followups("acme/app", 12, ["x"], run)
        self.assertEqual(url, "https://github.com/acme/app/issues/9")
        self.assertFalse(any(c[:3] == ["gh", "issue", "create"] for c in calls))
        self.assertTrue(any(c[:3] == ["gh", "issue", "comment"] for c in calls))

    def test_file_followups_failure_is_a_note(self):
        url, note = cr.file_followups("acme/app", 12, ["x"], lambda cmd: (1, ""))
        self.assertIsNone(url)
        self.assertIn("only in this report", note)


class SwarmctlReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Backend()
        cls.home = tempfile.mkdtemp()
        tok = os.path.join(cls.home, "admin.token")
        with open(tok, "w") as fh:
            fh.write(ADMIN)
        # A fake gh that prints $FAKE_GH_JSON for `gh pr view` (or fails when it is unset)
        bindir = os.path.join(cls.home, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "gh"), "w") as fh:
            fh.write('#!/usr/bin/env bash\n[[ -n "$FAKE_GH_JSON" ]] || exit 1\nprintf "%s" "$FAKE_GH_JSON"\n')
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        cls.env = dict(os.environ, HOME=cls.home, SWARM_URL=cls.b.base, SWARM_TOKEN_FILE=tok, ENQUEUE_OWNER="tester",
                       PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        cls.env.pop("FAKE_GH_JSON", None)

    @classmethod
    def tearDownClass(cls):
        cls.b.close()

    def sw(self, *args, stdin=None, gh=None):
        env = dict(self.env, FAKE_GH_JSON=json.dumps(gh)) if gh is not None else self.env
        return subprocess.run([os.path.join(SERVER, "swarmctl")] + list(args), env=env, capture_output=True, text=True,
                              input=stdin)

    def done_review(self, report, head="abc123", author="codex"):
        r = self.sw("review", "-r", "acme/app", "--pr-number", "8", "--author-provider", author, "--risk", "ordinary")
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = r.stdout.split()[0]
        c = self.claim(tid)
        if report is not None:
            self.put_report(c, report)
        self.b.finish(c, "done", head_sha=head)
        return tid

    def test_recheck_of_an_unfinished_or_legacy_review_is_no_ship_advice(self):
        r = self.sw("review", "-r", "acme/app", "--pr-number", "8", "--author-provider", "codex")
        queued = r.stdout.split()[0]
        r = self.sw("review", "--recheck", queued)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not done", r.stderr)
        self.assertNotIn("Ship it", r.stdout + r.stderr)
        self.claim(queued)  # out of the way of later claims
        for report in (None, "## Summary\nStatus: done\n\n## Findings\n- High: bad thing at a.py:1\n"):
            r = self.sw("review", "--recheck", self.done_review(report))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("result is unavailable", r.stderr)
            self.assertNotIn("Ship it", r.stdout + r.stderr)

    def test_pass_is_ship_advice_only_on_the_current_head_with_green_ci(self):
        clean = "## Summary\nReady to merge\n\n## Must-fix findings\n- None\n\n## Follow-up findings\n- None\n"
        tid = self.done_review(clean, head="abc123")
        green = {"headRefOid": "abc123", "statusCheckRollup": [{"name": "unittest", "conclusion": "SUCCESS"}]}
        r = self.sw("review", "--recheck", tid, gh=green)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ship it", r.stdout)
        for gh in (dict(green, headRefOid="def456"), {"headRefOid": "abc123", "statusCheckRollup": [{"name": "u", "conclusion": "FAILURE"}]},
                   None):
            r = self.sw("review", "--recheck", tid, gh=gh)
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Ship it", r.stdout + r.stderr)
            self.assertIn("Not ship advice", r.stderr)

    def test_recheck_keeps_the_independent_family(self):
        # Claude wrote it, so Codex reviews; the recheck must not drop back to the default provider (claude).
        first = self.done_review(REPORT, author="claude")
        self.assertEqual(self.task(first)["provider"], "codex")
        r = self.sw("review", "--recheck", first)
        self.assertEqual(r.returncode, 0, r.stderr)
        t2 = self.task(r.stdout.split()[0])
        self.assertEqual(t2["provider"], "codex")
        self.assertEqual(cr.parse_marker(t2["prompt"])["author"], "claude")
        self.claim(t2["id"])
        r = self.sw("review", "--recheck", first, "-P", "claude")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("other family", r.stderr)
        r = self.sw("review", "--recheck", first, "--pr-number", "99")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("do not match", r.stderr)

    def test_no_followup_issue_opt_out_is_on_the_marker(self):
        r = self.sw("review", "-r", "acme/app", "--pr-number", "9", "--author-provider", "claude", "--no-followup-issue")
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = r.stdout.split()[0]
        self.assertEqual(cr.parse_marker(self.task(tid)["prompt"])["followups"], "report")
        self.claim(tid)

    def task(self, tid):
        return self.b.req("GET", f"/tasks/{tid}")["task"]

    def test_dev_task_picks_the_opposite_family(self):
        dev = self.b.req("POST", "/tasks", {"prompt": "build it", "provider": "claude", "owner": "tester"})["task"]["id"]
        r = self.sw("review", "-r", "acme/app", "--pr-number", "5", "--dev-task", dev, "--risk", "high")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = self.task(r.stdout.split()[0])
        self.assertEqual((t["provider"], t["kind"]), ("codex", "review"))
        m = cr.parse_marker(t["prompt"])
        self.assertEqual((m["round"], m["tier"], m["model"]), (1, "high", "policy"))
        self.assertIn("Must-fix findings", t["prompt"])

    def test_recheck_scope_and_cap(self):
        r = self.sw("review", "-r", "acme/app", "--pr-number", "6", "--author-provider", "codex", "--risk", "ordinary")
        first = r.stdout.split()[0]
        self.assertEqual(self.task(first)["provider"], "claude")
        c = self.claim(first)
        self.put_report(c, REPORT)
        self.b.finish(c, "done")
        r = self.sw("review", "--recheck", first)
        self.assertEqual(r.returncode, 0, r.stderr)
        t2 = self.task(r.stdout.split()[0])
        self.assertEqual((t2["repo"], t2["pr_number"], t2["parent_id"]), ("acme/app", 6, first))
        self.assertEqual(cr.parse_marker(t2["prompt"])["round"], 2)
        self.assertIn("skips the session check", t2["prompt"])
        self.assertNotIn("clamp the page size", t2["prompt"])  # follow-ups are not rechecked
        c = self.claim(t2["id"])
        self.put_report(c, REPORT)
        self.b.finish(c, "done")
        r = self.sw("review", "--recheck", t2["id"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Review cap reached", r.stderr)
        self.assertIn("retire the PR", r.stderr)

    def claim(self, tid):
        while True:  # other tests leave tasks queued: finish those first
            c = self.b.claim()
            if c["id"] == tid:
                return c
            self.b.finish(c, "failed", error="not this test's task")

    def put_report(self, c, text):
        self.b.req("PUT", f"/tasks/{c['id']}/files/report/REPORT.md", tok=c["task_token"], raw=text.encode(),
                   headers={"X-Claim-Id": c["claim_id"]})

    def test_review_tier(self):
        r = self.sw("review-tier", "--author-provider", "claude", stdin="docs/a.md\nCHANGELOG.md\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["tier"], out["cap"]), ("none", 2))
        out = json.loads(self.sw("review-tier", stdin="src/billing/charge.py\n").stdout)
        self.assertEqual((out["tier"], out["cap"]), ("high", 3))


if __name__ == "__main__":
    unittest.main()
