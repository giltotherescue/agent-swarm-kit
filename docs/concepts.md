# Concepts

This page explains the parts of the swarm and the words the other docs use. Read it once before you set anything up. It covers who does what, how a task moves from "queued" to "done", how questions come back to you, and where results end up.

## The cast

**The human.** That is you. You set goals, say what good looks like, and make the calls. Bots and workers work toward that. You should only hear from the swarm when something needs your judgment or a deliverable is ready.

**Chief of Staff bot.** One Grok Bot that coordinates across all your projects. It owns cross-project questions, shared setup, your daily decision batch, and anything nobody else owns. It is not a hub for results. Each project bot gets its own results directly (see [results routing](results-routing.md)).

**Project bots.** One Grok Bot per project or company. A project bot owns its project end to end: it watches channels, drafts, triages, plans, and writes tasks for the worker. It brings project decisions to you and cross-project questions to the Chief of Staff.

**The backend.** A small service that keeps the list of tasks, their questions and answers, reports, artifacts (files a task produced), which bot owns what, and your "Waiting on you" list. By default it runs on the worker server and stores everything in one SQLite database file. See [backend](backend.md).

**The worker.** An always-on server that does the real work. It claims tasks from the backend, runs them with the model CLIs (command line interfaces), and posts results back. See [worker setup](worker-setup.md).

**Model CLIs.** Claude Code (`claude`) and Codex CLI (`codex`), logged in with your own subscriptions. The worker runs them without a human at the keyboard.

**GitHub.** Where code lives. The worker clones repos (code repositories), works on a branch, and can open a draft PR (pull request, a proposed change someone reviews before it is merged). A human merges.

## Bot or worker?

Grok Bot is command and control: project management and coordination. It is cheap to keep it that way. Send work to the worker when:

- the job is long (more than about 15 minutes),
- it needs a high-end model (strategy, top-quality copy or design, deep research),
- someone asked for a specific model,
- it touches a repo (code, builds, tests).

Quick lookups, drafting a short reply, triage and status updates stay in the bot.

## Tasks

Every piece of worker work is written down as a task first. A task has a brief (the prompt), an owner (the bot that queued it), and optionally a project, a repo, a provider (`claude` or `codex`) and a model. A good brief says what done looks like in terms someone else could check. See [the task brief template](../templates/task-brief.md).

A task is always in exactly one state:

| State | Meaning |
| --- | --- |
| `queued` | Waiting for the worker to pick it up. |
| `running` | The worker is on it. It checks in every 30 seconds. |
| `waiting` | Stopped with a question. It waits on its owner bot or on the human. |
| `done` | Finished. A report is ready. |
| `failed` | Stopped with an error, or a script returned a non-zero exit code. |
| `cancelled` | Someone cancelled it. Final. |

If the worker stops checking in for 10 minutes, the backend puts the task back in the queue. After 5 attempts (the default) it stops retrying and asks the owner.

## How a run ends

At the end of every run the agent writes a file called `AGENT_STATUS` in its folder. The first line is one word:

- `done`: the work is finished.
- `blocked`: the second line is one question someone must answer. The task becomes `waiting`.
- `next`: the second line is the next concrete step. The task goes straight back in the queue and resumes. This is how long jobs keep going overnight.

If Claude hits a usage limit, the worker tries the fallback provider (Codex by default). If that is not set, the task waits and retries later, starting one hour later and doubling up to one day.

## Reports

The agent also writes `REPORT.md` with these headings, in this order:

1. **Summary**, starting with `Status: done`, `partial` or `blocked`.
2. **Handoff**: three one-line items. Decided (choices it made and why), Unsure (what it could not verify), Re-check (what you must confirm before relying on it).
3. **What changed**
4. **Verification**
5. **Open questions**
6. **Facts worth keeping**

Files for the requester go in `artifacts/`. The worker uploads the report and artifacts to the backend, so a bot can read them without GitHub: `swarmctl report TASK_ID` and `swarmctl artifacts TASK_ID`.

Read the Handoff first, and do the Re-check yourself.

## Questions: waiting on the owner or on the human

A waiting task waits on one of two people:

- **The owner bot** (the default). The bot answers with `swarmctl answer TASK_ID "..."`. The same agent session resumes with the answer.
- **The human**, when the task was queued with `--ask-human`. Only the human can answer. A project bot that tries gets an error (HTTP 409). The Chief of Staff asks you in chat and records your answer.

## The Waiting on you list

One list of everything that needs you: decisions and deliverables to review. Bots file items with `swarmctl waiting add`, and tasks waiting on the human show up there too. You can answer, snooze, or drop an item ("don't need it"). Dropping means stop work on it, and the bot cannot file the same ask again unless you ask for it. It is meant to make saying no easy and guilt-free. See [routines](routines.md) for the morning batch that walks this list.

## Script tasks

Some work needs no model at all, like running a full test suite. Queue it with `--command`:

```
swarmctl enqueue -r you/app --command "npm ci && npm test" --owner app-bot - <<< "Run the full test suite"
```

The worker runs the command, saves the output as `artifacts/script.log`, and marks the task `done` (exit code 0) or `failed`. No tokens spent.

## Follow-ups and messages

- **Message a running task:** `swarmctl message TASK_ID "Also update the footer"`. The worker writes it to `.task-context/messages.md` within about 30 seconds, and the agent checks that file between steps. A message to a task that waits on its owner is treated as the answer.
- **Answer a waiting task:** `swarmctl answer TASK_ID "Use the blue logo"`.
- **Change finished work:** queue a new task with `--parent TASK_ID`, name the branch with `--branch agent/TASK_ID`, and say what to change. A finished task cannot be messaged.

Messages never grant permission to push, merge, deploy or send anything outside.

## Memory belongs to the owner bot

The worker never writes to memory, project briefs or decision logs. It reports "Facts worth keeping" and the owner bot decides what, if anything, to keep in its brief. This keeps one clear owner for every lasting fact.

## Where to go next

- No server yet? Start with the [Grok Bot only path](grokbot-only.md).
- Ready for a worker? Go to [worker setup](worker-setup.md), then the [first week walkthrough](first-week.md).
