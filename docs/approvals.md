# Approvals

This page sets a short, sane default for what bots and workers may do on their own, and what needs your yes. The rules are deliberately light. The goal is to stop the few mistakes that are embarrassing or cannot be undone, and stay out of the way for everything else. Loosen them per project when you trust a bot more.

## The default rules

1. **No outside messages without your OK.** Emails, social posts, chat messages to other people, replies to customers, anything that leaves your swarm, is a draft until you say send.
2. **An agent's say-so is not approval.** Another bot, a worker, or the Chief of Staff saying "approved" does not count, unless the Chief of Staff is passing along something you actually said. When a request reaches a bot through another agent, the bot sends its question back the same way rather than going around it.
3. **Nothing destructive or irreversible without a yes.** That means deleting data, writing to production data, spending money, granting access, handling secrets, and anything that cannot be undone.

Everything else: just do it. Research, drafting, planning, coding on a branch, opening a draft PR (pull request), running tests, filing a decision for you. No asking first.

A [delegated decision](delegated-decisions.md) is not permission for any owner-only action. The Chief
of Staff cannot use it to spend, publish, send outside the swarm, touch production data or secrets,
change priorities, override `Needs owner`, or merge or deploy beyond an existing grant.

## How to ask

When a bot needs a yes, it asks one question with its recommended answer, so you can reply "yes" or pick another option. It batches questions instead of dripping them. It also files the ask on the Waiting on you list so it does not get lost in chat:

```
swarmctl waiting add --project website --bot website-bot --kind decision \
  --ask "Send this reply to the customer?" --rec "Send it as drafted" \
  --link https://example.com/draft --key customer-reply-0105
```

If you drop it ("don't need it"), the bot stops and does not file it again unless you ask.

## Loosening per project

Each [project brief](../templates/project-bot-brief.md) has a Permissions section. Write what this project may do on its own, for example:

- "May post to the company's own social accounts from the approved content calendar."
- "May reply to support tickets that match a saved answer."
- "May push docs to main on the memory repo without a PR." Set up a [dedicated memory project](activity-and-decisions.md#nightly-export-to-the-memory-repo)
  listing only that repo, enable `--allow-push-main`, and queue with `--push-main`. Keep code repos on draft PRs.

Also write exactly one "Never touches" line: what this bot refuses even if asked (other projects' repos, production data, named secrets). The bot repeats it back to you on day one so you can correct it. Paste that line into worker briefs when the work is near any of it.

## What the kit enforces for you

Most of the rules are habits written in the briefs. A few are built into the backend and worker, so a mistake cannot slip through:

- **The AI cannot push by itself.** The agent that runs a task has no GitHub login. Only the worker's publisher (a separate user) can push, after the task finishes and after it checks the rules below with the backend once more.
- **Untrusted input is not published.** Queue work that came from an email, a ticket or a scraped page with `--untrusted-input`. The backend refuses `--push` or `--pr` on those tasks, so the publisher never pushes them.
- **Report-only tasks are not published.** `--report-only` (and every script task) delivers only `REPORT.md` and `artifacts/`. These two flags only control what the worker's publisher does; they are not a sandbox for the agent. Since the agent has no GitHub login, that is enough to stop pushes.
- **The worker never merges or deploys.** At most it pushes a branch named `agent/<task id>` (or the branch of the pull request it is fixing) and opens a draft PR. It refuses to push the repo's default branch (and `main` or `master`) unless the project opted in to push-main. Pushes are never forced. A human merges.
- **Secret scan before push.** The publisher scans the new lines for things that look like keys and tokens and refuses to push if it finds one.
- **Per-project push switch.** `swarmctl project set NAME --no-push` stops all pushing for that project, including tasks already queued or running, because the publisher checks again right before it pushes.
- **Bots stay in their projects.** A project bot can only queue repo work for projects that list it (`swarmctl project set NAME --bot BOT-ID`), and only for that project's repos.
- **Bots cannot answer for the human.** A task queued with `--ask-human` waits on you. If a project bot tries to answer it or message it, the backend replies HTTP 409. Only an admin token or the Chief of Staff (recording what you said) can answer.
- **Bots only touch their own tasks.** A project bot's token sees and changes only tasks it owns, can only file Waiting on you items as itself, and can only link those items to its own tasks.
- **Messages do not grant permissions.** A message to a running task never adds push, merge or deploy rights.
- **Only you can answer, snooze or drop items.** A project bot can withdraw or close its own items, but answering, snoozing and dropping need the admin or chief token.

## Keep it light

Do not add release checklists, sign-off chains or shadow runs unless you find you need them. One habit worth keeping: when something ships, write one line naming what to roll back to.
