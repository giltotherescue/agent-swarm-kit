# Project log

Every project gets a simple history: what was done, what you decided, what came back from the worker,
your feedback, and what the bots learned. Projects with code have git history; this gives the same
memory to projects without a repo, like marketing, sales or a newsletter. It is built into the backend
(new in 0.3.0).

## What goes in it

One entry is one line of summary, with optional detail and links. Each entry has a kind:

| Kind | What it records | Who adds it |
| --- | --- | --- |
| `activity` | Work done: "Sent the November newsletter", "Posted 3 LinkedIn drafts for review" | Project bots, the worker |
| `decision` | A choice that was made, and by whom | **Automatic** when you answer or drop a Waiting on you item, or answer a task's question. Bots add their own small decisions; the Chief of Staff adds the weekly goals |
| `result` | A finished or failed worker task, with its summary and links | **Automatic** when a task ends as done or failed |
| `feedback` | What you said about a deliverable: "Too formal", "Love the second headline" | The bot you told, or the Chief of Staff |
| `learning` | Something that will help next time: "Emails sent Tuesday get more replies" | Project bots, the worker |

The log is append-only: entries are never edited or deleted, so it stays a trustworthy record. If
something was wrong, add a new entry that corrects it.

Only add what someone would want to read later. A good log has a few entries a day, not hundreds.

## The feedback loop

This is the part that makes bots better over time.

1. You look at a deliverable and say what you think, in chat, in a few words.
2. The bot logs it as `feedback`, linked to the task if there was one.
3. If the feedback is a lasting preference, the bot also adds a `learning` ("Write like we talk to
   customers; no corporate words").
4. **Before drafting new work, every project bot reads its recent feedback and learnings** (the last 30
   days, or the last 20 entries). It is in the project bot brief and in the daily work heartbeat prompt.

So you give a note once, and it sticks.

## Who can read and write it

| Who | Add entries | Read entries |
| --- | --- | --- |
| You (admin token) | Any project | All projects |
| Chief of Staff | Any project | All projects |
| A project bot | Only projects it is bound to | Only projects it is bound to |
| A running worker task | Only its own task's project | No |
| The worker token | No | No |

A project bot is bound to a project with `swarmctl project set PROJECT --bot BOT-ID`. That works for
projects with no repo too. A bot that is not bound gets a clear "not bound" error.

## Using it from the command line

```
# add
swarmctl log add marketing --kind activity "Sent the November newsletter" --link https://example.com/issue
swarmctl log add marketing --kind feedback "Too formal. Write like we talk to customers."
swarmctl log add marketing --kind learning "Short subject lines get more opens" --detail notes.md

# read
swarmctl log list marketing --days 7
swarmctl log list marketing --kind feedback,learning --days 30

# export a readable Markdown file
swarmctl log export marketing --days 7 --out marketing-log.md
swarmctl log export --dir ~/project-logs --days 7     # one file per project you can read
```

`--key KEY` on `log add` keeps an entry once even if the same command runs twice (useful in routines).

## Using it from a bot (the API)

All calls use the bot's own token in the `Authorization: Bearer` header.

- `POST /log` with `{"project", "kind", "summary", "detail", "links", "task_id", "dedupe_key"}`. Only
  `project`, `kind` and `summary` are required. A running task sends no `project`; it is filled in from
  the task.
- `GET /log?project=NAME&kind=feedback,learning&since=UNIX_TIME&limit=20` returns entries oldest first.
  Without `project`, a bot gets all its bound projects.
- `GET /log/export?project=NAME&since=UNIX_TIME` returns a Markdown digest grouped by day.

The full permission table is in [backend.md](backend.md).

## The weekly digest

`swarmctl log export` writes one Markdown file per project, grouped by day, readable without any tools.
A simple weekly habit: the Chief of Staff exports the last 7 days before the goals meeting, reads it to
score last week's goals, and can share the file with you if you like reading the details.

The goals meeting reads the log (results, decisions and feedback from the last 7 days) to score goals,
and writes the confirmed goals back as `decision` entries. See
[goals-and-heartbeat.md](goals-and-heartbeat.md).

## How it is stored

One SQLite table, `log_entries`, in the backend database: project, bot, kind, summary, detail, links,
optional task id, and the time. It is created automatically the first time a 0.3.0 backend starts, so
upgrading needs no manual step, and it is included in the nightly database backup. Automatic entries
carry a hidden key so a retried request never adds them twice. Tasks without a project add no log
entry.

## Without a worker

With Grok Bot only, there is no backend. Each project bot keeps the same log as a note in its memory,
newest at the bottom, one line per entry:

```
## Project log: marketing
- 2026-11-18 activity: Sent the November newsletter. (link)
- 2026-11-18 feedback: Too formal. Write like we talk to customers.
- 2026-11-19 learning: Short subject lines get more opens.
- 2026-11-23 decision: Weekly goals confirmed by Sam (see goals file).
```

The nightly memory cleanup may summarize entries older than 90 days into a few lines, but never drops
feedback or learnings.
