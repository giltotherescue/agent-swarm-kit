# The task backend

This doc explains the task backend: the small service that keeps the list of tasks, their questions and answers, reports and files, the bots and where to notify them, and the Waiting on you list. It covers how it runs, every API (Application Programming Interface) route, how your bots reach it, backups, and the optional Cloudflare design.

## The default: on the worker

The backend is `server/swarm_backend.py`. It is one Python file using only the standard library, with a SQLite database file. It runs on the worker as the `swarm` user under systemd (`swarm-backend.service`), listens on `127.0.0.1:8750`, and keeps its data in `/var/lib/agent-swarm/swarm.db`. Settings come from `/etc/agent-swarm/swarm.env`.

A background loop runs every 5 seconds. It:

- requeues running tasks whose worker stopped heartbeating (the lease is 10 minutes), or fails them after `max_attempts`;
- reopens snoozed Waiting on you items when the snooze ends;
- delivers pending notices to bot inboxes, with retries and a fallback to the Chief of Staff.

### Tables

| Table | What it holds |
| --- | --- |
| `tasks` | One row per task: brief, owner, repo, provider and model, state, question, answer waiting to be used, session id, attempts, lease |
| `events` | The thread of each task: brief, state changes, answers, messages, files, failed deliveries |
| `messages` | Notes sent to a running task, waiting to be passed to the agent |
| `files` | Attachments, `REPORT.md` and artifacts (up to 25 MB each) |
| `bots` | Bot id, role (`chief` or `project`), notify URL, notify auth, hashed token |
| `projects` | Default repo, provider and model per project, and whether pushing is allowed |
| `items` | The Waiting on you list |
| `notices` | The delivery outbox. One row per event per bot, with a unique key so nothing is sent twice |

Task states: `queued`, `running`, `waiting`, `done`, `failed`, `cancelled`. A waiting task has `waiting_on` set to `owner` (the bot that queued it) or `human`.

## Who can call what

Every request except `GET /health` needs `Authorization: Bearer <token>`.

| Role | Token | Can do |
| --- | --- | --- |
| admin | `/etc/agent-swarm/admin.token` | Everything. Keep it on the worker and with the Chief of Staff only |
| worker | `/etc/agent-swarm/worker.token` | Claim, heartbeat, finish, upload files, read tasks, send alerts |
| chief bot | made with `swarmctl bot token ID` for a bot with role `chief` | Everything admin can, except worker calls and making tokens |
| project bot | made with `swarmctl bot token ID` | Only its own tasks, items and bot settings |

## API reference

| Method | Path | Who | What it does |
| --- | --- | --- | --- |
| GET | `/health` | anyone | Liveness check, returns the version |
| GET | `/status` | any token | Counts by state, running and waiting tasks, items due, stuck tasks. Project bots see only their own |
| POST | `/tasks` | admin, chief, bot | Queue a task. Body: `prompt` (required), `project`, `repo`, `provider`, `model`, `owner`, `kind`, `title`, `report_only`, `push`, `open_pr`, `pr_number`, `branch`, `ask_human`, `untrusted_input`, `command`, `idempotency_key`, `allow_duplicate`, `parent_id`, `max_attempts`, `attachments` (`[{name, content_base64}]`, 20 files, 25 MB each) |
| GET | `/tasks` | any token | List. Filters: `state`, `project`, `owner`, `kind`, `waiting_on`, `parent_id`, `updated_since` (unix seconds), `limit` |
| GET | `/tasks/ID` | any token that can see it | One task with its event thread |
| POST | `/tasks/ID/answer` | admin, chief, owner bot | Answer a waiting task. Body `{"answer": "..."}`. It goes back to `queued` and resumes. A bot cannot answer a task that waits on the human |
| POST | `/tasks/ID/message` | admin, chief, owner bot | Body `{"text": "..."}`. Added to `.task-context/messages.md` of a running task. On a task waiting on its owner it counts as the answer |
| POST | `/tasks/ID/cancel` | admin, chief, owner bot | Cancel now, or at the next heartbeat if running |
| GET | `/tasks/ID/report` | any token that can see it | `REPORT.md` as text |
| GET | `/tasks/ID/artifacts` and `/tasks/ID/attachments` | same | List files |
| GET | `/tasks/ID/artifacts/NAME` and `/tasks/ID/attachments/NAME` | same | Download one file |
| PUT | `/tasks/ID/files/report/NAME` and `/tasks/ID/files/artifact/NAME` | worker, admin | Upload a report or artifact (raw body) |
| POST | `/worker/claim` | worker, admin | Take the oldest queued task that is due |
| POST | `/tasks/ID/heartbeat` | worker, admin | Extend the lease, collect new messages, learn about a cancel |
| POST | `/tasks/ID/finish` | worker, admin | `status`: `done`, `waiting` (with `question`), `next` (with `next_step`), `limit` or `failed` |
| GET | `/bots` | admin, chief | List bots |
| GET | `/bots/ID` | admin, chief, that bot | One bot (the auth value is never shown, only `has_notify_auth`) |
| PUT | `/bots/ID` | admin, chief, that bot | Set `notify_url` (https only) and `notify_auth`. Only admin or chief can set `role` |
| POST | `/bots/ID/token` | admin | Make a new token for the bot. Shown once |
| POST | `/bots/ID/test` | admin, chief, that bot | Send one test notice to the bot's inbox |
| GET | `/projects` | any token | List project defaults |
| PUT | `/projects/NAME` | admin, chief | Set `default_repo`, `default_provider`, `default_model`, `allow_push` |
| GET | `/waiting` | any token | The Waiting on you list. `status` is `due` (default), `open`, `snoozed`, `answered`, `dropped`, `done`, `withdrawn` or `all`. Includes tasks waiting on the human |
| POST | `/waiting` | admin, chief, bot | File an item. Required: `project`, `bot`, `kind` (`decision` or `deliverable-review`), `ask`, `dedupe_key`. Same key while open updates it |
| POST | `/waiting/ID` | see note | `action`: `answer`, `snooze`, `drop` (human only, through admin or chief), or `done`, `withdraw`, `update` (the filing bot too) |
| POST | `/alerts` | admin, worker | Body `{key, text}`. Sends an alert to the Chief of Staff once per key |
| GET | `/notices` | admin, chief | The last 100 deliveries with state and last error |

`swarmctl` wraps all of these. Run `swarmctl --help` and `swarmctl SUBCOMMAND --help`.

## Reaching the backend from your bots

Grok Bot bots run in the cloud, not on your worker. To queue tasks and read reports, they must reach the backend over the internet. Notices to bots go the other way: the backend calls the bot's inbox URL. That is an outbound call, so it needs no inbound opening.

| Option | How | Good | Watch out |
| --- | --- | --- | --- |
| 1. Tailscale Funnel (recommended) | `sudo tailscale funnel --bg 8750` | Simplest. Gives a public https address like `https://YOUR-WORKER.YOUR-TAILNET.ts.net` with a real certificate. No ports opened in your firewall | Anyone on the internet can reach the address. The bearer tokens are the only gate |
| 2. Tailscale Serve (tailnet only) | `sudo tailscale serve --bg 8750` | Not reachable from the internet at all | Works only if the bot's computer can join your tailnet. Most hosted bots cannot |
| 3. Reverse proxy or Cloudflare Tunnel | Caddy or nginx, or `cloudflared`, in front of port 8750 | You can add a second layer, such as Cloudflare Access or an IP allow list | More moving parts to set up and keep working |

If you pick Funnel, keep the gate strong:

- Give every project bot **its own token** (`swarmctl bot token website-bot`). A leaked bot token exposes only that bot's tasks, and you can replace it any time.
- Only the Chief of Staff holds the admin token or a chief token. Never give the admin token to a project bot.
- Set `SWARM_PUBLIC_URL` in `swarm.env` to the Funnel address so report links in notices work, then `sudo systemctl restart swarm-backend`.
- Check exposure with `tailscale funnel status`. Turn it off with `sudo tailscale funnel reset`.

## The tradeoff of running on the worker

One box is simple and cheap. The cost: **when the worker is down, tracking is down too.** Bots cannot queue tasks or read reports until it is back. Nothing is lost: the data is a file on disk, backed up every night, and systemd restarts the service after a crash or reboot. Queue attempts made during the outage fail with a clear error, and the bot can retry with the same `idempotency_key` without making a duplicate.

## Optional: Cloudflare Workers and D1 (plan, not included yet)

Some people want tracking to keep working while the worker is down for maintenance. For them, the backend could run on Cloudflare Workers (small programs on Cloudflare's network) with D1 (Cloudflare's SQLite database).

**This kit ships the on-worker backend only.** The Cloudflare version is a design for now:

1. Same API surface and the same JSON shapes, so `swarmctl` and bots do not change. Only `SWARM_URL` changes.
2. D1 uses the same SQL tables as the `SCHEMA` in `swarm_backend.py`.
3. A Cron Trigger every minute does what the background loop does: requeue lost tasks, reopen snoozed items, deliver notices.
4. Files go to R2 (Cloudflare's file storage), or to D1 blobs if small.
5. Tokens are Worker secrets. The worker box connects over https with the worker token, and needs no inbound exposure at all.

Steps to build it: port the route handlers to a Worker, apply the schema to D1, add the cron trigger, add R2 for files, port the checks in `tests/test_backend.py` (they call the HTTP API, but start the Python server themselves, so they need a small change to point at another URL), then point `SWARM_URL` on the worker at the new address. Contributions welcome ([updating-the-kit.md](updating-the-kit.md)).

## Backups and restore

`swarm-backup.timer` runs `scripts/backup.sh` every night at 03:10. It makes a consistent copy with SQLite's backup API, gzips it to `/var/lib/agent-swarm/backups/swarm-YYYY-MM-DD.db.gz`, and keeps `SWARM_BACKUP_KEEP_DAYS` days (default 14). Set `SWARM_OFFSITE_CMD` to also copy the folder off the box.

Restore:

```bash
sudo systemctl stop swarm-worker swarm-backend
sudo -u swarm cp /var/lib/agent-swarm/swarm.db /var/lib/agent-swarm/swarm.db.before-restore
sudo -u swarm sh -c 'gunzip -c /var/lib/agent-swarm/backups/swarm-YYYY-MM-DD.db.gz > /var/lib/agent-swarm/swarm.db'
sudo rm -f /var/lib/agent-swarm/swarm.db-wal /var/lib/agent-swarm/swarm.db-shm
sudo systemctl start swarm-backend swarm-worker
swarmctl status
```

## Moving to a new box

1. Set up the new box with `scripts/setup-worker.sh`.
2. On the old box: `sudo systemctl stop swarm-worker swarm-backend`, then run `sudo -u swarm /opt/agent-swarm-kit/scripts/backup.sh` with the env file loaded, or simply copy `/var/lib/agent-swarm/swarm.db`.
3. Copy the database, `/etc/agent-swarm/swarm.env`, `admin.token` and `worker.token` to the new box over Tailscale (`scp`). Keep owners and modes: `root:swarm 640` for tokens, `swarm:swarm` for the database.
4. Put the worker token copy in `/home/agent/.config/swarm/worker.token` (owner `agent`, mode 600).
5. Start the backend and worker, move the Funnel (`tailscale funnel --bg 8750` on the new box), and update `SWARM_PUBLIC_URL` if the name changed.
6. Running tasks from the old box are requeued by the reaper after their lease runs out.
