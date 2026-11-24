"""Regression checks for the bot templates and report docs (PR 30 review findings)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text()


def section(text, heading):
    """Body of a '## heading' section, up to the next '## ' heading."""
    m = re.search(r"^## " + re.escape(heading) + r"\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else None


def flat(text):
    return " ".join(text.split())


class MessageRuleReachesSenders(unittest.TestCase):
    """Setup installs chief-of-staff.md on the Chief and project-bot-brief.md on project bots.
    The sender-side message rule must be in both, with the same wording."""

    def test_rule_in_both_briefs_identical(self):
        chief = section(read("templates/chief-of-staff.md"), "Messages to managers")
        proj = section(read("templates/project-bot-brief.md"), "Messages to managers")
        self.assertIsNotNone(chief)
        self.assertIsNotNone(proj, "project bots send to the Chief; their brief needs the rule")
        rule = flat(chief)
        self.assertIn(rule, flat(proj))
        self.assertIn("**verified**", rule)
        self.assertIn("**asserted**", rule)

    def test_escalation_routines_point_at_rule(self):
        routines = flat(read("templates/routine-prompts.md"))
        n = routines.count('Send small open questions to the Chief of Staff, in the "Messages to managers" form')
        self.assertEqual(n, 2, "results routine (step 4) and work heartbeat (step 7)")

    def test_question_and_link_optional(self):
        for rel in ("templates/chief-of-staff.md", "templates/project-bot-brief.md"):
            rule = flat(section(read(rel), "Messages to managers"))
            self.assertIn("at most one open question and at most one link", rule, rel)


class FactsWorthKeepingIsRead(unittest.TestCase):
    """The results routine promotes facts from 'Facts worth keeping'; reading rules must not skip it."""

    def test_routine_still_uses_section(self):
        self.assertIn('"Facts worth keeping"', read("templates/routine-prompts.md"))

    def test_reader_rules_include_facts(self):
        texts = {
            "task-brief": flat(read("templates/task-brief.md")),
            "concepts": flat(read("docs/concepts.md")),
            "chief": flat(section(read("templates/chief-of-staff.md"), "Push work to workers")),
        }
        for name, t in texts.items():
            self.assertNotIn("reads only Summary and Handoff", t, name)
            self.assertNotIn("Read only a report's Summary and Handoff", t, name)
            self.assertRegex(t, r"Summary, (the )?Handoff.*?and Facts worth keeping", name)

    def test_report_evidence_need_not_be_a_link(self):
        for rel in ("templates/task-brief.md", "docs/concepts.md"):
            t = flat(read(rel))
            self.assertIn("at most one link", t, rel)
            self.assertIn("artifact path", t, rel)


class Formatting(unittest.TestCase):
    def test_new_template_sections_wrapped(self):
        for rel, heads in (
            ("templates/chief-of-staff.md", ("Live calls with the human", "Messages to managers",
                                             "Push work to workers")),
            ("templates/project-bot-brief.md", ("Messages to managers",)),
        ):
            text = read(rel)
            for h in heads:
                for line in section(text, h).splitlines():
                    self.assertLessEqual(len(line), 108, f"{rel} [{h}]: {line}")


if __name__ == "__main__":
    unittest.main()
