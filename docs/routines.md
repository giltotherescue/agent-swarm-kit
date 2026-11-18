# Routines

This doc explains the few recurring jobs that keep a swarm healthy without you watching it. Some run on the worker as plain scripts and cost no tokens. Others are Grok Bot routines: a saved prompt that runs on a schedule or when a webhook (a web address another service calls) fires. The copy-paste prompts are in [../templates/routine-prompts.md](../templates/routine-prompts.md).

## The rule for every routine

Every routine has a **pass/fail check** that its own bot runs. A passing run sends no message. You only hear about failures and decisions. Write the check at the same time as the routine, and put both in the project brief ([../templates/project-bot-brief.md](../templates/project-bot-brief.md)).

A good check is concrete: "the sitemap returns 200 and lists at least 60 pages", not "the site looks fine".

## Routines on the worker (no AI, no tokens)

These are installed by `scripts/setup-worker.sh` as systemd timers.

| Timer | When | What it does |
| --- | --- | --- |
| `swarm-cleanup.timer` | Daily, 03:40 | `server/swarm_cleanup.py`. Removes git-ignored bulky folders (such as `node_modules`) of tasks finished over 1 day ago, and whole folders of tasks finished over 7 days ago. Saves unpushed commits and uncommitted changes to `~/backups/work/DATE/` first. Never touches queued, running or waiting tasks, or anything changed in the last 48 hours |
| `swarm-backup.timer` | Nightly, 03:10 | `scripts/backup.sh`. Copies the backend database to `/var/lib/agent-swarm/backups` |
| `swarm-disk-alert.timer` | Hourly | `scripts/disk-alert.sh`. If the disk is over 80 percent, sends one alert per day to the Chief of Staff |

The backend itself requeues tasks whose worker went quiet and resumes tasks that ended with `next` or hit a usage limit. You do not need an AI supervisor for that.

If you make a folder by hand on the worker, put it in `/home/agent/work/scratch/`. Cleanup removes scratch folders unchanged for 14 days, after backing up unpushed work.

## Grok Bot routines

| Routine | Who runs it | Trigger | Why |
| --- | --- | --- | --- |
| Task result handler | Every bot | Its own webhook | Acts on its own task results and questions. See [results-routing.md](results-routing.md) |
| Morning decision batch | Chief of Staff | Daily, early morning | All decisions for the human in one message, each with a recommendation |
| Nightly memory cleanup | Every bot | Daily, night | Keeps memory short, which keeps every run cheaper |
| Fallback and alerts handler | Chief of Staff | Its own webhook | Ownerless results, failed deliveries, waiting-on-human questions, disk alerts |
| Weekly kit improvement note | Chief of Staff, optional | Weekly | Turns patterns in failures into concrete suggestions |

### Morning decision batch

The Waiting on you list (`GET /waiting?status=due`) holds every open decision and every task waiting on the human. The Chief of Staff reads it once a day and sends one numbered message. You reply in one line, for example "1 yes, 2 drop, 3 snooze". "Drop" means "don't need it": the bot stops work on that ask and must not file it again. Dropping is guilt-free. That is the point of the list.

### Nightly memory cleanup

Bot memory grows every day. Long memory makes every run slower and more expensive. The cleanup merges duplicates, moves lasting facts into the project brief, and drops stale notes. It also withdraws the bot's own obsolete Waiting on you items.

## Keeping routines cheap

- Prefer webhooks over polling. A webhook routine runs only when there is news.
- If you must poll, use `GET /tasks?owner=MY-BOT-ID&updated_since=UNIX_TIME`, which returns only what changed.
- Stay silent when nothing changed. "Nothing to report" messages cost a full turn for you and for whoever reads them.
- Batch small updates into one message.

More in [usage-and-cost.md](usage-and-cost.md).
