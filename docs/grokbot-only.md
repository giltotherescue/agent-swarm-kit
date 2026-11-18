# Grok Bot only (no server)

This path is for people who do not want to run a server, or who want to start small. You get a Chief of Staff bot and one bot per project, all inside Grok Bot. There is no worker and no backend. A lot already works this way, and you can add a worker later without starting over.

## What you set up

1. **A Chief of Staff bot.** Paste in [the Chief of Staff instructions](../templates/chief-of-staff.md). Skip the parts about the backend and the worker for now; they are marked.
2. **One bot per project.** Fill in [the project bot brief](../templates/project-bot-brief.md) for each one and paste it into the bot. Leave "Repos" and "Tests" empty if you have none.
3. **Connectors.** Connect the apps each bot needs (email, calendar, chat, payments, docs). Give each bot only what its project needs.
4. **Routines.** Set up the morning decision batch and the nightly memory cleanup from [routine prompts](../templates/routine-prompts.md).
5. **A Waiting on you note.** See below.

## What still works

- **Chat and coordination.** You talk to each bot in its own chat. The Chief of Staff coordinates across projects.
- **Connectors.** Bots read and draft in your connected apps.
- **Drafting.** Emails, posts, replies, plans. They stay drafts until you say send (see [approvals](approvals.md)).
- **Research inside the bot.** Short web research, summaries, comparisons.
- **Project briefs.** Each bot knows its project, goals, people and limits.
- **Approvals.** The same light rules apply.
- **Routines.** Scheduled prompts run inside Grok Bot: the morning decision batch, the nightly memory cleanup, and any pass/fail checks you add (for example "the site homepage loads").
- **A Waiting on you list.** Without a backend, keep it as a pinned note or a memory file the Chief of Staff owns. Each line: project, the ask, the recommended answer, a link, and the date filed. Bots add a line whenever they hand you a decision or deliverable. You answer, snooze, or write "drop". A dropped line means stop work on it, and nobody files it again unless you ask.

A simple format for that note:

```
## Waiting on you
- [website] Publish the new About page? Recommend: yes, the shorter version. Link: ... Filed 2026-01-05.
- [shop] Raise the shipping fee to cover costs? Recommend: no, wait a month. Filed 2026-01-06.
## Answered or dropped (cleared weekly)
- [website] Pick a hero image. Answer: the second one. 2026-01-04.
```

## What does not work without a worker

- **Long jobs.** A bot turn has a time limit. Anything that needs more than a few minutes of steady work is out.
- **Repo work.** No cloning, editing code, running tests, or opening a PR (pull request).
- **Choosing a model.** The bot runs on whatever model the bot platform uses. You cannot send a job to a specific stronger or cheaper model.
- **Overnight work.** Nothing keeps going while you sleep and resumes after a pause or a usage limit.
- **Files and reports in one place.** Results live in chat history instead of a task record.

If you notice you keep asking a bot for something it cannot finish in one turn, that is the sign to add a worker.

## Upgrading later

Adding a worker does not replace anything. Your bots, briefs, connectors and routines stay as they are.

1. Rent a server and follow [worker setup](worker-setup.md). It installs the worker and the backend together.
2. Make your bots able to reach the backend. The recommended way is to install Tailscale (a private network for your own devices) on your bot's computer and use Tailscale Serve on the worker, so the worker is never on the public internet. See [worker setup](worker-setup.md).
3. For each bot, walk through [the per-bot setup checklist](../templates/bot-setup-checklist.md): give it a token (its own password for the worker), bind it to its project, set up its own results trigger, and test it.
4. Update each brief: add repos, tests, and the [model routing table](../templates/model-routing.md). Add the backend sections back into the Chief of Staff instructions.
5. Move the Waiting on you note into the backend. For each open line, the bot that filed it runs `swarmctl waiting add` with a stable `--key`. Then archive the note.
6. Run the [first week walkthrough](first-week.md) with one small task.

There is nothing else to migrate. Briefs are just text you copy, and past chat history stays where it is.
