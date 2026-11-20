# Routines

This doc explains the few recurring jobs that keep a swarm healthy without you watching it. Some run on the worker as plain scripts and cost no tokens. Others are Grok Bot routines: a saved prompt that runs on a schedule or when a webhook (a web address another service calls) fires. The copy-paste prompts are in [../templates/routine-prompts.md](../templates/routine-prompts.md).

## The rule for every routine

Every routine starts with a **cheap check**. Look for a new event or a change since the last run.
If nothing changed, stop. Send nothing and log nothing. Put the check and the routine in the
[project brief](../templates/project-bot-brief.md).

A good check is concrete: "the sitemap returns 200 and lists at least 60 pages", not "the site looks fine".

## Routines on the worker (no AI, no tokens)

These are installed by `scripts/setup-worker.sh` as systemd timers.

| Timer | When | What it does |
| --- | --- | --- |
| `swarm-cleanup.timer` | Daily, 03:40 | `server/swarm_cleanup.py`. Removes git-ignored bulky folders (such as `node_modules`) of tasks finished over 1 day ago, and whole folders of tasks finished over 7 days ago. Before deleting a folder it saves an archive of everything in it (including new untracked files) plus a git bundle of unpushed commits to `~/backups/work/DATE/`, and checks the archive first. Never touches queued, running or waiting tasks, anything changed in the last 48 hours, or a repo that a live git worktree points into |
| `swarm-backup.timer` | Nightly, 03:10 | `scripts/backup.sh`. Copies the backend database to `/var/lib/agent-swarm/backups` |
| `swarm-disk-alert.timer` | Hourly | `scripts/disk-alert.sh`. If the disk is over 80 percent, sends one alert per day to the Chief of Staff |

The backend itself requeues tasks whose worker went quiet and resumes tasks that ended with `next` or hit a usage limit. It also:

- reminds the owner bot once when a task has waited on it for 12 hours, and puts it on the Waiting on you list (once) after 24 hours;
- sends one alert to the Chief of Staff when tasks are queued but no worker has checked in for 10 minutes, or when a task runs well past its time limit (a watchdog).

You do not need an AI supervisor for that.

If you make a folder by hand on the worker, put it in `/home/agent/work/scratch/`. Cleanup removes scratch folders unchanged for 14 days, after backing up unpushed work.

## Grok Bot routines

| Routine | Who runs it | Trigger | Why |
| --- | --- | --- | --- |
| Task result handler | Every bot | Its own webhook | Acts on its own task results and questions. See [results-routing.md](results-routing.md) |
| Morning decision batch | Chief of Staff | Daily, early morning | All decisions for the human in one message, each with a recommendation |
| Nightly memory cleanup | Every bot | Daily, night | Keeps memory short, which keeps every run cheaper |
| Fallback and alerts handler | Chief of Staff | Its own webhook | Ownerless results, failed deliveries, waiting-on-human questions, escalated waits, watchdog and disk alerts |
| Weekly kit improvement note | Chief of Staff, optional | Weekly | Turns patterns in failures into concrete suggestions |
| Weekly goals meeting | Chief of Staff | Weekly, a fixed slot | Scores last week, sets 1 to 3 checkable goals per project. See [goals-and-heartbeat.md](goals-and-heartbeat.md) |
| Daily work heartbeat | Every project bot | Daily | Checks its goals are moving; silent when on track, one Waiting on you item when a goal is at risk |
| Missed-question sweep | Chief of Staff | Each work heartbeat | Checks open asks and recent questions; delegates or files new asks once |
| Memory export | Chief of Staff | Nightly | Queues one docs-only task only when the [activity feed changed](activity-and-decisions.md#nightly-export-to-the-memory-repo) |

### Morning decision batch

The Chief of Staff checks `GET /waiting?status=due` and the new delegated calls. It sends one message
for items newly due and calls not yet reported. If neither changed, it sends nothing. Reply in one
line, for example "1 yes, 2 drop, 3 snooze" or "overrule #ENTRY-ID: use the full checklist". Dropping
stops work on that ask and the bot must not file it again. See the [morning review call](how-it-works.md#the-morning-review-call)
and [delegation note format](delegated-decisions.md#answer-log-tell-the-owner).

### Weekly goals meeting and daily work heartbeat

The goals meeting gives every project one to three goals a week, each with a "Done means" line you
confirmed. The work heartbeat is each project bot checking those goals once a day. It is not the
worker's machine heartbeat (a plain network check-in every 30 seconds while a task runs, which uses no
model and no tokens, with the task put back in the queue after 10 minutes of silence). Both are explained in [goals-and-heartbeat.md](goals-and-heartbeat.md).
Both read the [project log](project-log.md) and use the [activity feed](activity-and-decisions.md) for new decisions.

### Nightly memory cleanup

Bot memory grows every day. Long memory makes every run slower and more expensive. The cleanup merges duplicates, moves lasting facts into the project brief, and drops stale notes. It also withdraws the bot's own obsolete Waiting on you items, and scans the day's chat for asks to the human that were never filed on the list, and files them (with a stable key, so nothing is filed twice).

### One inbox, one dispatcher

Every bot has one inbox routine. It looks at the `event` field (what happened: `task`, `item_answer`, `item_drop`, `reminder`, `escalated`, `watchdog`, `alert`, `test`) and skips anything whose `notify_target` is not its own bot id. It also skips any `event_id` it already handled, because a delivery can arrive more than once.

## Keeping routines cheap

- Prefer webhooks over polling. A webhook routine runs only when there is news.
- If you must poll, use `GET /tasks?owner=MY-BOT-ID&updated_since=UNIX_TIME`, which returns only what changed.
- Stay silent when nothing changed. "Nothing to report" messages cost a full turn for you and for whoever reads them.
- Batch small updates into one message.

More in [usage-and-cost.md](usage-and-cost.md).
