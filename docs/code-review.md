# Code review

Every pull request (PR) that gets reviewed is reviewed by the **other model family**: code Claude wrote gets a Codex
review, and code Codex wrote gets a Claude review. Different models miss different things. Each bot runs this loop
itself: it queues the review, fixes what blocks, and ships under its own standing rules, without waiting for you to
chase each PR.

This page is the kit's port of the review policy the owner approved on 2026-11-25 (agent-control has the same rules).
The pieces are `swarmctl review`, `swarmctl review-tier`, the two prompts in `templates/` (`review-prompt.md`,
`review-recheck-prompt.md`), the policy module `server/swarm_codereview.py`, and a few lines in the worker.

## Who reviews

| Wrote it | Reviews it |
| --- | --- |
| Claude (opus, sonnet) | Codex, `SWARM_REVIEW_CODEX_MODEL` (ordinary) or `SWARM_REVIEW_CODEX_HIGH_MODEL` (high risk) |
| Codex (any model) | Claude, `SWARM_REVIEW_CLAUDE_MODEL` (default `opus`) |

Pass the task that wrote the PR and `swarmctl` picks for you:

```bash
swarmctl review -r you/app --pr-number 12 --dev-task TASK_ID --owner app-bot
```

It uses the provider that actually ran last on that task, so if a usage-limit fallback switched families mid-task, the
family that did **not** write the final commit reviews. `--author-provider claude|codex` works when there is no task;
`-P` and `-m` still override everything. The model settings live in `/etc/agent-swarm/swarm.env` on the worker (and in
the bot's environment for `swarmctl`). Out of the box both Codex settings are `default`; set the high-risk one to your
strongest Codex model.

The author family is recorded on the review's marker. A recheck keeps the same independent reviewer, `-P` naming the
author's family is refused, a worker refuses to run a marked review on the author's family, and a usage limit on a
marked review does not fall back to the other family (`SWARM_FALLBACK_PROVIDER`); it finishes as a normal limit and is
retried later.

## Risk tiers

| Tier | What | Review | Rounds (cap) |
| --- | --- | --- | --- |
| **high** | anything that can touch auth or security, deletion or retention, money or entitlements, or production data. That includes backup and restore scripts, billing webhooks, migrations, and agent skills or linters that can publish, charge, delete, or grant access. | full, opposite family, high-risk model. Green CI does not skip it. | 3 |
| **ordinary** | ordinary UI, copy and application code | light: one opposite-family pass, no loop | 2 |
| **none** | typo, changelog, comment-only or docs-only changes that cannot publish, charge, delete or change access | not required when CI is green. A bot may still ask for a light review if it is unsure. | 2 if asked |

The tier comes from the changed paths: path words such as `auth`, `session`, `billing`, `stripe`, `webhook`,
`migrations`, `delete`, `retention`, `backup`, `restore`, `deploy` or `publish`, plus `*.sql`. A prose file (`.md`,
`.txt`, changelog) is never high on its words alone, except under a `skills/` folder, because a skill is something an
agent runs. A diff whose changed lines are all comments counts as comment-only.

- **Check before you queue:** `gh pr diff 12 --name-only | swarmctl review-tier --author-provider claude` prints the
  tier, the reason, the cap and the reviewer. `none` with green CI means you can skip the review.
- **Override:** `swarmctl review --risk high` (or `ordinary`) when you know better than the paths. A high-risk path
  cannot be overridden down to `none`.
- **Add your own high-risk paths:** `SWARM_REVIEW_HIGH_RISK_PATHS=infra/*,ops/**` (comma-separated globs) on the worker.
- When you do not pass `--risk`, the worker sets the tier from the PR's diff on the host before the review runs, and
  writes it at the end of the report under "Review policy".

## What blocks a PR

A finding is **must fix** only when it is security or auth, data loss or corruption (including a failed deletion of
data that should have been removed), wrong behavior on a common path, money or entitlements, or red CI. Judge the
impact, not the mechanism: a race, retry or interleaving still blocks if a plausible real-world trigger can leak data,
lose or corrupt data, take or grant money or access, skip auth, or break a common user or operator action. A blocking
finding names that trigger in one or two sentences.

Everything else is a **follow-up**: probe-only interleavings with no realistic trigger, leftover hardening, lint or docs
false positives, contrived encodings after the realistic cases are fixed. Follow-ups do not start another fix round.

The reviewer writes them under two headings in REPORT.md, `## Must-fix findings` and `## Follow-up findings`, each item
with a `path:line`.

## Rounds and rechecks

One full review per PR. After fixes, queue a recheck of that review:

```bash
swarmctl review --recheck REVIEW_TASK_ID --owner app-bot
```

The recheck gets only the earlier must-fix items. It verifies each one, and may raise a new must-fix only if the new
issue itself meets the blocking bar. It may not raise new edge, race or hardening items as blockers. `swarmctl` refuses a
recheck when the next round would pass the cap, and when the earlier review is not done or its report has no
`## Must-fix findings` section (the result is unavailable; re-run it). When the earlier review had no must-fix items,
`swarmctl` says "Ship it" only if the reviewed commit is still the PR's head and every CI check on it passed (it asks
`gh pr view`); otherwise it says why and gives no ship advice.

## When the cap is hit

Ordinary PRs get two rounds (the first review plus one recheck). High-risk PRs get three. At the cap the bot does one
of two things:

- **ship** with the residual non-blocking items recorded (the follow-up issue below), only once a review with no
  must-fix items covers the PR's current head and CI is green, or
- **retire** the PR and close it with the reason written on it.

It files a Waiting on you item only if the choice between shipping and retiring truly needs you. Parked is not a resting
state, and a cap hit is not a reason to ask you by default. If a review task past the cap reaches the worker anyway, the
worker finishes it as done without running a model and says why; no Waiting on you item is created.

## Where follow-ups go

The review session has no GitHub access, so the **worker** files follow-ups on the host after the review finishes: one
open GitHub issue per PR, in the same repo, titled `PR #N review follow-ups: <first item>`, labeled `review-followup`
(created if missing), and linked from the PR with a comment. A later round adds its follow-ups to the same issue as a
comment. The issue link is at the end of the review report under "Review policy". If `gh` fails, the report says so and
the follow-ups are still in the report; file the issue by hand.

Filing the issue is a write with the worker's GitHub credentials, so it happens only when the caller opted in: `swarmctl
review` does unless you pass `--no-followup-issue` (use that when the worker's token has no Issues write; the
residuals then stay in the report). The worker never files for a task queued with untrusted input, because the report
may quote a private ticket or email. A fine-grained token needs **Issues: read and write** on the repository for this.

If a bot will really do the work later, it may also queue a follow-up task on the same branch. Do not leave residuals
only in a review report, and do not put them on the Waiting on you list unless you have to decide something.

## Older reviews

A review queued without `swarmctl review` (a raw `POST /tasks` with `kind: review`) has no policy marker: the worker
runs it as before, with only the path:line format check.
