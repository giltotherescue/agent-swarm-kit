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
        routines = read("templates/routine-prompts.md")
        rule = 'Send small open questions to the Chief of Staff, in the "Messages to managers" form'
        for heading in ("1. Task result handler (every bot, webhook-triggered)",
                        "7. Daily work heartbeat (every project bot)"):
            body = section(routines, heading)
            self.assertIsNotNone(body, heading)
            self.assertIn(rule, flat(body), heading)

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


POINTER = "More owner questions in Open questions"


def write_report(questions):
    """A worker following the task-brief Report format: one owner question in Summary, the rest in
    Open questions, and the pointer line in Summary when any are left there."""
    summary = ["Status: done", "Shipped the pricing page (verified: npm test passed)."]
    summary.append("Question: " + questions[0])
    rest = questions[1:]
    if rest:
        summary.append(f"{POINTER}: {len(rest)}")
    open_q = "\n".join(f"{i}. {q}" for i, q in enumerate(rest, 1)) or "None."
    return (
        "## Summary\n" + "\n".join(summary) + "\n\n"
        "## Handoff\n- Decided: none\n- Unsure: none\n- Re-check: the page renders.\n\n"
        "## What changed\nPricing page.\n\n## Verification\nnpm test passed.\n\n"
        "## Open questions\n" + open_q + "\n\n## Facts worth keeping\nNone.\n"
    )


def read_report(report, follow_pointer=True):
    """An owner following the reader rule: Summary, Handoff and Facts worth keeping, plus Open
    questions when Summary carries the pointer line. Returns the text the owner actually saw."""
    seen = [section(report, h) for h in ("Summary", "Handoff", "Facts worth keeping")]
    if follow_pointer and POINTER in section(report, "Summary"):
        seen.append(section(report, "Open questions"))
    return "\n".join(seen)


class EveryOwnerQuestionSurfaces(unittest.TestCase):
    """Review t20261124160625-b67032: a second owner question in Open questions could go unseen."""

    def test_writer_rule_requires_pointer(self):
        for rel in ("templates/task-brief.md", "docs/concepts.md"):
            t = flat(read(rel))
            self.assertIn(f'"{POINTER}: N"', t, rel)
            self.assertIn("every other owner question in Open questions" if "task-brief" in rel
                          else "Any other question for the owner goes in Open questions", t, rel)

    def test_reader_rules_follow_pointer(self):
        routines = read("templates/routine-prompts.md")
        texts = {
            "task-brief": flat(read("templates/task-brief.md")),
            "concepts": flat(read("docs/concepts.md")),
            "chief": flat(section(read("templates/chief-of-staff.md"), "Push work to workers")),
            "results routine": flat(section(routines, "1. Task result handler (every bot, webhook-triggered)")),
        }
        for name, t in texts.items():
            self.assertIn(POINTER, t, name)
        self.assertIn('"task-TASK_ID-qN"', texts["results routine"], "one Waiting item per question")

    def test_two_independent_questions_both_reach_owner(self):
        q1 = "Should the annual plan show a discount?"
        q2 = "May we email the beta list about the new prices?"
        report = write_report([q1, q2])
        self.assertIn(q2, section(report, "Open questions"))
        self.assertNotIn(q2, section(report, "Summary"))
        seen = read_report(report)
        self.assertIn(q1, seen)
        self.assertIn(q2, seen)
        # Without the pointer rule the second question is lost: this is the bug being fixed.
        self.assertNotIn(q2, read_report(report, follow_pointer=False))

    def test_single_question_needs_no_pointer(self):
        report = write_report(["Ship it?"])
        self.assertNotIn(POINTER, report)
        self.assertIn("Ship it?", read_report(report))


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
