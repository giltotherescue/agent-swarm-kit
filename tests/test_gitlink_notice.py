"""gitlink_skipped in task records, bus notices and legacy webhook payloads (agent-control PR 63): redacted, delivered, bounded."""
import json
import unittest

from helpers import Backend  # noqa: F401
from test_bus_notices import NoticeBase, FAKE_SECRET, CLOZY_HOOK

TOKEN_PATH = "vendor/" + FAKE_SECRET + "/engine"
SKIPPED = [{"path": TOKEN_PATH, "old": FAKE_SECRET, "new": "bbbbbbb"}, {"path": "engine", "old": "aaaaaaa", "new": "bbbbbbb"}]


class BusGitlinkTest(NoticeBase):
    def test_token_shaped_path_bus_notice_is_delivered_and_redacted(self):
        t = self.finished("clozy-bot", "tg1", gitlink_skipped=SKIPPED)
        m = self.bus_msgs()
        self.assertEqual(len(m), 1, "delivered, not rejected by the secret check")
        self.assertNotIn(FAKE_SECRET, m[0]["body"] + (m[0]["meta"] or ""))
        self.assertRegex(m[0]["body"], r"WARNING: 2 submodule pin change\(s\).*\[redacted\].*engine \(aaaaaaa -> bbbbbbb\)")
        meta = json.loads(m[0]["meta"])
        self.assertEqual(meta["gitlink_skipped_count"], 2)
        self.assertEqual(meta["gitlink_skipped"][0], {"path": "[redacted]", "old": "[redacted]", "new": "bbbbbbb"})
        # the stored record and the event are redacted too
        self.assertNotIn(FAKE_SECRET, t["gitlink_skipped"])
        evs = self.api.s.q("SELECT text FROM events WHERE task_id = 'tg1' AND type = 'publish'")
        self.assertEqual(len(evs), 1)
        self.assertNotIn(FAKE_SECRET, evs[0]["text"])
        self.assertIn("engine (aaaaaaa -> bbbbbbb)", evs[0]["text"])
        self.assertEqual(self.b.req("GET", "/tasks/tg1")["task"]["gitlink_skipped"][1]["path"], "engine")

    def test_warning_precedes_a_long_report_excerpt(self):
        self.finished("clozy-bot", "tg2", report="## Summary\n" + "x" * 3000 + "\n", gitlink_skipped=SKIPPED[1:])
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
        self.finished("clozy-bot", "tg5", status="failed", outcome="publish_failed", error="git fetch failed", gitlink_skipped=SKIPPED[1:])
        self.assertIn("WARNING: 1 submodule pin", self.bus_msgs()[0]["body"])


class LegacyGitlinkTest(NoticeBase):
    notices = "legacy"

    def test_legacy_webhook_payload_carries_redacted_gitlink_fields(self):
        self.finished("clozy-bot", "tg6", gitlink_skipped=SKIPPED)
        rows = self.notice_rows()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(FAKE_SECRET, rows[0]["payload"])
        p = json.loads(rows[0]["payload"])
        self.assertEqual(p["gitlink_skipped_count"], 2)
        self.assertEqual(p["gitlink_skipped"][0]["path"], "[redacted]")
        # and over the wire
        import swarm_backend as sb
        calls = []
        sb.deliver_once(self.api, poster=lambda url, data, headers: (calls.append((url, data)) or (200, b"{}")))
        self.assertEqual([c[0] for c in calls], [CLOZY_HOOK])
        self.assertNotIn(FAKE_SECRET.encode(), calls[0][1])


class EnqueueOptInTest(NoticeBase):
    def test_allow_gitlink_is_stored_and_inherited_by_follow_ups(self):
        r = self.b.req("POST", "/tasks", {"id": "ta1", "prompt": "bump the pin", "repo": "acme/site", "push": True, "allow_gitlink": True})
        self.assertEqual(r["task"]["allow_gitlink"], 1)
        c = self.b.claim()
        self.assertEqual(c["allow_gitlink"], 1, "the worker sees it on the claimed task")
        r2 = self.b.req("POST", "/tasks", {"id": "ta2", "prompt": "no bump", "repo": "acme/site", "push": True})
        self.assertEqual(r2["task"]["allow_gitlink"], 0)
        self.assertNotEqual(r2["task"]["id"], "ta1", "the opt-in is part of the duplicate check")


if __name__ == "__main__":
    unittest.main()
