"""gitlink_skipped in task records, bus notices and legacy webhook payloads (agent-control PR 63): redacted, delivered, bounded."""
import json
import unittest

from helpers import Backend  # noqa: F401
from test_bus_notices import NoticeBase, FAKE_SECRET, CLOZY_HOOK

TOKEN_PATH = "vendor/" + FAKE_SECRET + "/engine"
FIXTURE = "TOKEN=abcdefgh12345"   # agent-control PR 63's fixture, unchanged
SKIPPED = [{"path": TOKEN_PATH, "old": FAKE_SECRET, "new": "bbbbbbb"}, {"path": FIXTURE, "old": FIXTURE, "new": FIXTURE},
           {"path": "engine", "old": "aaaaaaa", "new": "bbbbbbb"}]
LEAKS = (FAKE_SECRET, "abcdefgh12345", "abcdefgh", "A1b2C3d4")


def leaks(text):
    return [x for x in LEAKS if x in (text or "")]


class BusGitlinkTest(NoticeBase):
    def report(self, tid):
        st, out = self.b.call("GET", f"/tasks/{tid}/report")
        self.assertEqual(st, 200, out)
        return out.decode() if isinstance(out, bytes) else out if isinstance(out, str) else json.dumps(out)

    def test_token_shaped_path_bus_notice_is_delivered_and_redacted_in_every_sink(self):
        t = self.finished("clozy-bot", "tg1", report="## Summary\nreview report\n", gitlink_skipped=SKIPPED)
        m = self.bus_msgs()
        self.assertEqual(len(m), 1, "delivered, not rejected by the secret check")
        self.assertEqual(leaks(m[0]["body"] + (m[0]["meta"] or "")), [])
        self.assertRegex(m[0]["body"], r"WARNING: 3 submodule pin change\(s\).*\[redacted\].*TOKEN=\[redacted\].*engine \(aaaaaaa -> bbbbbbb\)")
        meta = json.loads(m[0]["meta"])
        self.assertEqual(meta["gitlink_skipped_count"], 3)
        self.assertEqual(meta["gitlink_skipped"][0], {"path": "vendor/[redacted]/engine", "old": "[redacted]", "new": "bbbbbbb"})
        self.assertEqual(meta["gitlink_skipped"][1], {"path": "TOKEN=[redacted]", "old": "[redacted]", "new": "[redacted]"})
        # task record
        self.assertEqual(leaks(t["gitlink_skipped"]), [])
        self.assertEqual(t["gitlink_skipped_count"], 3)
        rec = self.b.req("GET", "/tasks/tg1")["task"]
        self.assertEqual(rec["gitlink_skipped"][2]["path"], "engine")
        # event
        evs = self.api.s.q("SELECT text FROM events WHERE task_id = 'tg1' AND type = 'publish'")
        self.assertEqual(len(evs), 1)
        self.assertEqual(leaks(evs[0]["text"]), [])
        self.assertIn("engine (aaaaaaa -> bbbbbbb)", evs[0]["text"])
        # report: original text plus exactly one sanitized note
        rep = self.report("tg1")
        self.assertIn("review report", rep)
        self.assertEqual(rep.count("gitlink pin not committed"), 1)
        self.assertIn("TOKEN=[redacted]", rep)
        self.assertEqual(leaks(rep), [])

    def test_note_phrase_in_report_prose_does_not_suppress_the_note(self):
        prose = "## Summary\nThe phrase gitlink pin not committed appears here in ordinary prose.\n"
        self.finished("clozy-bot", "tg8", report=prose, gitlink_skipped=[{"path": "engine", "old": "aaaaaaa", "new": "bbbbbbb"}])
        rep = self.report("tg8")
        self.assertIn("ordinary prose", rep)
        self.assertIn("1 submodule pin change(s) left uncommitted: engine (aaaaaaa -> bbbbbbb)", rep)
        self.assertEqual(rep.count("<!-- swarm:gitlink-skip-note -->"), 1)

    NOTE_TAG = "<!-- swarm:gitlink-skip-note -->"

    def resumed(self, tid, first, second=None, report="## Summary\nfirst run\n", omit=False):
        """waiting (with `first`) -> answer -> claim again -> finish done with `second` (no new report)."""
        self.b.req("POST", "/tasks", {"id": tid, "prompt": "Ship " + tid, "repo": "acme/site", "push": True, "owner": "clozy-bot"})
        c = self.b.claim()
        self.b.req("PUT", f"/tasks/{tid}/files/report/REPORT.md", None, tok=c["task_token"], raw=report.encode(),
                   headers={"X-Claim-Id": c["claim_id"]})
        st, out = self.b.finish(c, "waiting", question="Which pin?", waiting_on="human", gitlink_skipped=first)
        self.assertEqual(st, 200, out)
        self.b.req("POST", f"/tasks/{tid}/answer", {"answer": "the second"})
        c2 = self.b.claim()
        self.assertEqual(c2["id"], tid)
        extra = {} if omit else {"gitlink_skipped": second}
        body = dict({"pr_url": "https://github.com/acme/site/pull/9", "pushed_branch": f"agent/{tid}", "head_sha": "c" * 40,
                     "unpushed_commits": 0}, **extra)
        st, out = self.b.finish(c2, "done", **body)
        self.assertEqual(st, 200, out)
        # a replayed finish of the same claim changes nothing
        st2, out2 = self.b.finish(c2, "done", **body)
        self.assertEqual(st2, 200, out2)
        self.assertTrue(out2.get("replayed"), out2)
        st, out = self.b.call("GET", f"/tasks/{tid}/report")
        return self.b.req("GET", f"/tasks/{tid}")["task"], (None if st == 404 else self.report(tid))

    def test_resumed_claim_replaces_the_note_with_the_new_pin(self):
        t, rep = self.resumed("tr1", [{"path": "first-pin", "old": "aaaaaaa", "new": "bbbbbbb"}],
                              [{"path": "second-pin", "old": "ccccccc", "new": "ddddddd"}])
        self.assertEqual([g["path"] for g in t["gitlink_skipped"]], ["second-pin"])
        self.assertEqual(t["gitlink_skipped_count"], 1)
        self.assertIn("first run", rep)
        self.assertIn("second-pin (ccccccc -> ddddddd)", rep)
        self.assertNotIn("first-pin", rep)
        self.assertEqual(rep.count(self.NOTE_TAG), 1)

    def test_explicit_empty_observation_clears_list_count_and_note(self):
        t, rep = self.resumed("tr2", [{"path": "first-pin", "old": "aaaaaaa", "new": "bbbbbbb"}], [])
        self.assertEqual(t["gitlink_skipped"], [])
        self.assertFalse(t["gitlink_skipped_count"])
        self.assertIn("first run", rep)
        self.assertNotIn("first-pin", rep)
        self.assertNotIn(self.NOTE_TAG, rep)
        self.assertNotIn("gitlink pin not committed", rep)

    def test_empty_observation_removes_a_note_only_report(self):
        t, rep = self.resumed("tr3", [{"path": "first-pin", "old": "", "new": "bbbbbbb"}], [], report="")
        self.assertEqual(t["gitlink_skipped"], [])
        self.assertIsNone(rep, "the report held only the generated note, so it is gone")
        self.assertFalse(t["has_report"])

    def test_omitted_observation_keeps_the_earlier_one(self):
        t, rep = self.resumed("tr4", [{"path": "first-pin", "old": "aaaaaaa", "new": "bbbbbbb"}], omit=True)
        self.assertEqual([g["path"] for g in t["gitlink_skipped"]], ["first-pin"])
        self.assertEqual(t["gitlink_skipped_count"], 1)
        self.assertEqual(rep.count(self.NOTE_TAG), 1)
        self.assertIn("first-pin (aaaaaaa -> bbbbbbb)", rep)

    def test_report_note_on_publish_failure_without_a_report(self):
        self.finished("clozy-bot", "tg7", status="failed", outcome="publish_failed", error="git fetch failed",
                      gitlink_skipped=SKIPPED[1:])
        rep = self.report("tg7")
        self.assertIn("gitlink pin not committed", rep)
        self.assertEqual(leaks(rep), [])

    def test_true_count_survives_colliding_and_overflowing_paths(self):
        # 204 distinct pins whose display strings collide after redaction; the worker sends at most 200 plus the true count
        many = [{"path": f"TOKEN=secret{i:04d}xx", "old": "a" * 7, "new": "b" * 7} for i in range(204)]
        t = self.finished("clozy-bot", "tg8", gitlink_skipped=many[:200], gitlink_skipped_count=204)
        self.assertEqual(t["gitlink_skipped_count"], 204)
        meta = json.loads(self.bus_msgs()[0]["meta"])
        self.assertEqual((meta["gitlink_skipped_count"], len(meta["gitlink_skipped"])), (204, 10))
        self.assertIn("WARNING: 204 submodule pin change(s)", self.bus_msgs()[0]["body"])
        self.assertIn("+194 more", self.bus_msgs()[0]["body"])

    def test_warning_precedes_a_long_report_excerpt(self):
        self.finished("clozy-bot", "tg2", report="## Summary\n" + "x" * 3000 + "\n", gitlink_skipped=SKIPPED[2:])
        body = self.bus_msgs()[0]["body"]
        self.assertIn("WARNING: 1 submodule pin change(s)", body)
        self.assertLess(body.index("WARNING"), body.index("REPORT.md (excerpt)"))

    def test_no_warning_when_nothing_skipped(self):
        self.finished("clozy-bot", "tg3")
        m = self.bus_msgs()[0]
        self.assertNotIn("WARNING", m["body"])
        self.assertNotIn("gitlink_skipped", json.loads(m["meta"]))

    def test_notice_list_is_bounded_but_count_is_accurate(self):
        many = [{"path": f"p{i}" + "y" * 500, "old": "a" * 40, "new": "b" * 40} for i in range(25)]
        self.finished("clozy-bot", "tg4", gitlink_skipped=many)
        meta = json.loads(self.bus_msgs()[0]["meta"])
        self.assertEqual((len(meta["gitlink_skipped"]), meta["gitlink_skipped_count"]), (10, 25))
        self.assertTrue(len(meta["gitlink_skipped"][0]["path"]) <= 200)

    def test_failed_publish_still_carries_it(self):
        self.finished("clozy-bot", "tg5", status="failed", outcome="publish_failed", error="git fetch failed", gitlink_skipped=SKIPPED[2:])
        self.assertIn("WARNING: 1 submodule pin", self.bus_msgs()[0]["body"])


class LegacyGitlinkTest(NoticeBase):
    notices = "legacy"

    def test_legacy_webhook_payload_carries_redacted_gitlink_fields(self):
        self.finished("clozy-bot", "tg6", gitlink_skipped=SKIPPED)
        rows = self.notice_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(leaks(rows[0]["payload"]), [])
        p = json.loads(rows[0]["payload"])
        self.assertEqual(p["gitlink_skipped_count"], 3)
        self.assertEqual(p["gitlink_skipped"][0]["path"], "vendor/[redacted]/engine")
        self.assertEqual(p["gitlink_skipped"][1]["path"], "TOKEN=[redacted]")
        # and over the wire
        import swarm_backend as sb
        calls = []
        sb.deliver_once(self.api, poster=lambda url, data, headers: (calls.append((url, data)) or (200, b"{}")))
        self.assertEqual([c[0] for c in calls], [CLOZY_HOOK])
        self.assertEqual(leaks(calls[0][1].decode()), [])


class EnqueueOptInTest(NoticeBase):
    def test_allow_gitlink_is_stored_and_inherited_by_follow_ups(self):
        r = self.b.req("POST", "/tasks", {"id": "ta1", "prompt": "bump the pin", "repo": "acme/site", "push": True, "allow_gitlink": True})
        self.assertEqual(r["task"]["allow_gitlink"], 1)
        c = self.b.claim()
        self.assertEqual(c["allow_gitlink"], 1, "the worker sees it on the claimed task")
        r2 = self.b.req("POST", "/tasks", {"id": "ta2", "prompt": "no bump", "repo": "acme/site", "push": True})
        self.assertEqual(r2["task"]["allow_gitlink"], 0)
        self.assertNotEqual(r2["task"]["id"], "ta1", "the opt-in is part of the duplicate check")

    def test_follow_up_inherits_only_when_omitted_and_explicit_values_override(self):
        self.finished("clozy-bot", "tp", create={"allow_gitlink": True})
        self.assertEqual(self.row("tp")["allow_gitlink"], 1)
        cases = [("omitted", None, 1), ("json-false", False, 0), ("string-false", "false", 0), ("zero", 0, 0),
                 ("json-true", True, 1)]
        for name, value, want in cases:
            body = {"prompt": f"follow-up {name}", "follow_up": "tp", "allow_duplicate": True}
            if value is not None:
                body["allow_gitlink"] = value
            t = self.b.req("POST", "/tasks", body)["task"]
            self.assertEqual(t["parent_id"], "tp", name)
            self.assertEqual(t["allow_gitlink"], want, name)
        st, out = self.b.call("POST", "/tasks", {"prompt": "bad", "follow_up": "tp", "allow_gitlink": "maybe"})
        self.assertEqual(st, 400, out)

    def test_follow_up_of_unopted_parent_opts_in_only_explicitly(self):
        self.finished("clozy-bot", "tq")
        self.assertEqual(self.b.req("POST", "/tasks", {"prompt": "x", "follow_up": "tq"})["task"]["allow_gitlink"], 0)
        self.assertEqual(self.b.req("POST", "/tasks", {"prompt": "y", "follow_up": "tq",
                                                       "allow_gitlink": True, "allow_duplicate": True})["task"]["allow_gitlink"], 1)

    def test_explicit_null_allow_gitlink_is_refused(self):
        self.finished("clozy-bot", "tn", create={"allow_gitlink": True})
        for body in ({"prompt": "n1", "follow_up": "tn", "allow_gitlink": None},
                     {"prompt": "n2", "repo": "acme/site", "allow_gitlink": None}):
            st, out = self.b.call("POST", "/tasks", body)
            self.assertEqual(st, 400, (body, out))
            self.assertIn("allow_gitlink", out["error"])
        self.assertEqual(self.b.req("POST", "/tasks", {"prompt": "n3", "follow_up": "tn"})["task"]["allow_gitlink"], 1)

    def test_allow_gitlink_must_be_a_real_boolean(self):
        base = {"repo": "acme/site", "push": True}
        self.assertEqual(self.b.req("POST", "/tasks", dict(base, prompt="a", allow_gitlink="false"))["task"]["allow_gitlink"], 0)
        self.assertEqual(self.b.req("POST", "/tasks", dict(base, prompt="b", allow_gitlink=False))["task"]["allow_gitlink"], 0)
        self.assertEqual(self.b.req("POST", "/tasks", dict(base, prompt="c", allow_gitlink="true"))["task"]["allow_gitlink"], 1)
        for bad in ("maybe", 2, [True], {"x": 1}):
            st, out = self.b.call("POST", "/tasks", dict(base, prompt=f"d{bad!r}", allow_gitlink=bad))
            self.assertEqual(st, 400, (bad, out))
            self.assertIn("allow_gitlink", out["error"])


class WorkerSourceTest(unittest.TestCase):
    """The worker sanitizes the collector's list before the finish request carries it."""
    def test_decide_sends_only_sanitized_entries_and_the_true_count(self):
        import test_bus_worker as tbw
        import swarm_worker as sw
        raw = SKIPPED + [{"path": f"p{i}", "old": "", "new": ""} for i in range(250)]
        state, extra = sw.Runner.decide(tbw.Stub(), 75, 0, "claude", "m", {"files": {}, "git": {"head": "a" * 40, "gitlink_skipped": raw}})
        self.assertEqual(leaks(json.dumps(extra)), [])
        self.assertEqual(extra["gitlink_skipped_count"], len(raw))
        self.assertEqual(len(extra["gitlink_skipped"]), 200)
        self.assertEqual(extra["gitlink_skipped"][1]["path"], "TOKEN=[redacted]")


if __name__ == "__main__":
    unittest.main()
