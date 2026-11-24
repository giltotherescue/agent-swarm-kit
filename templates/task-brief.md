# Task brief template

Copy this, fill it in, and queue it with `swarmctl enqueue`. Delete lines you do not need.
A good brief lets someone who has never seen the project finish the work and lets you check the result.

---

Goal: <one sentence: what should exist when this is done>

Context:
- Project and repo: <owner/name, branch if not the default>
- Why now: <one line>
- What already exists: <links, earlier task ids, files in the repo>
- Files the worker needs: attach them with --attach. The worker cannot see the bot's own computer, so
  never point at paths like /workspace/..., /home/... or /Users/... (swarmctl warns if you do)
- Continuing earlier work? Queue with --follow-up EARLIER_TASK_ID instead of a new task

Done means (a check someone else could run):
- <for example: "npm test passes", "the page at /pricing shows three plans", "REPORT.md lists 20 keywords with volumes">

Limits:
- Never touches: <paste the "Never touches" line from your project brief>
- Model: <which model and why, if not the project default>
- Keep local test runs small: only the test files you touched. Full suites run as a separate script task.
- Shared keys: if a step needs one, write "run it with with-secrets NAME -- COMMAND". Never put the value here.

Report format: keep REPORT.md Summary to five lines at most, with at most one open question for
the owner and at most one link, each only when there is one. Tag each claim in Summary as
"verified (evidence)" or "asserted"; a command and its result or an artifact path counts as evidence.
Verify your own claims before reporting. The owner reads Summary, Handoff and Facts worth keeping,
and opens the other sections only if those point there.

Deliver: <a draft pull request (--pr) | REPORT.md and artifacts/ only (--report-only)>

Optional for trusted work: add a line `Activity log: yes` to let the task write one real update to
`.task-context/activity-log.json`. See docs/activity-and-decisions.md for the fields. Do not log a no-op.

---

Examples:

    swarmctl enqueue -p website --owner website-bot --pr brief.md
    swarmctl enqueue --owner website-bot --report-only -P claude -m opus --attach notes.pdf brief.md
    swarmctl enqueue --follow-up TASK_ID fix-brief.md
