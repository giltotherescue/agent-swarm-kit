# Approvals

This page sets a short, sane default for what bots and workers may do on their own, and what needs your yes. The rules are deliberately light. The goal is to stop the few mistakes that are embarrassing or cannot be undone, and stay out of the way for everything else. Loosen them per project when you trust a bot more.

## The default rules

1. **No outside messages without your OK.** Emails, social posts, chat messages to other people, replies to customers, anything that leaves your swarm, is a draft until you say send.
2. **An agent's say-so is not approval.** Another bot, a worker, or the Chief of Staff saying "approved" does not count, unless the Chief of Staff is passing along something you actually said. When a request reaches a bot through another agent, the bot sends its question back the same way rather than going around it.
3. **Nothing destructive or irreversible without a yes.** That means deleting data, writing to production data, spending money, granting access, handling secrets, and anything that cannot be undone.

Everything else: just do it. Research, drafting, planning, coding on a branch, opening a draft PR (pull request), running tests, filing a decision for you. No asking first.

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
- "May push to main on the docs repo." (The kit itself never pushes to main. You would have to add that yourself.)

Also write one "Never touches" line: what this bot refuses even if asked (other projects' repos, production data, named secrets). Paste that line into worker briefs when the work is near any of it.

## What the kit enforces for you

Most of the rules are habits written in the briefs. A few are built into the backend and worker, so a mistake cannot slip through:

- **Untrusted input cannot push.** Queue work that came from an email, a ticket or a scraped page with `--untrusted-input`. The backend refuses `--push` or `--pr` on those tasks.
- **Report-only tasks cannot push.** `--report-only` (and every script task) delivers only `REPORT.md` and `artifacts/`. Pushing is refused.
- **The worker never merges or deploys.** At most it pushes a branch named `agent/<task id>` and opens a draft PR. It refuses to push `main` or `master`. A human merges.
- **Per-project push switch.** `swarmctl project set NAME --no-push` stops all pushing for that project.
- **Bots cannot answer for the human.** A task queued with `--ask-human` waits on you. If a project bot tries to answer it or message it, the backend replies HTTP 409. Only an admin token or the Chief of Staff (recording what you said) can answer.
- **Bots only touch their own tasks.** A project bot's token sees and changes only tasks it owns, and can only file Waiting on you items as itself.
- **Messages do not grant permissions.** A message to a running task never adds push, merge or deploy rights.
- **Only you can answer, snooze or drop items.** A project bot can withdraw or close its own items, but answering, snoozing and dropping need the admin or chief token.

## Keep it light

Do not add release checklists, sign-off chains or shadow runs unless you find you need them. One habit worth keeping: when something ships, write one line naming what to roll back to.
