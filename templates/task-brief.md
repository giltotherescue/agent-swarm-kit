# Task brief template

Copy this, fill it in, and queue it with `swarmctl enqueue`. Delete lines you do not need.
A good brief lets someone who has never seen the project finish the work and lets you check the result.

---

Goal: <one sentence: what should exist when this is done>

Context:
- Project and repo: <owner/name, branch if not the default>
- Why now: <one line>
- What already exists: <links, earlier task ids, files in the repo>
- Files the worker needs: attach them with --attach (the worker cannot see your computer)

Done means (a check someone else could run):
- <for example: "npm test passes", "the page at /pricing shows three plans", "REPORT.md lists 20 keywords with volumes">

Limits:
- Never touches: <paste the "Never touches" line from your project brief>
- Model: <which model and why, if not the project default>
- Keep local test runs small: only the test files you touched. Full suites run as a separate script task.

Deliver: <a draft pull request (--pr) | REPORT.md and artifacts/ only (--report-only)>

---

Examples:

    swarmctl enqueue -p website --owner website-bot --pr brief.md
    swarmctl enqueue --owner website-bot --report-only -P claude -m opus --attach notes.pdf brief.md
