# The task backend

This doc explains the task backend: the small service that keeps the list of tasks, their questions and answers, reports and files, the bots and where to notify them, and the Waiting on you list. It covers how it runs, who may call each API (Application Programming Interface) route, how your bots reach it safely, backups, and the Cloudflare design.

## The default: on the worker

The backend is `server/swarm_backend.py`. It is one Python file using only the standard library, with a SQLite database file. It runs on the worker as the `swarm` user under systemd (`swarm-backend.service`), listens on `127.0.0.1:8750`, and keeps its data in `/var/lib/agent-swarm/swarm.db`. Settings come from `/etc/agent-swarm/swarm.env`.

A background loop runs every few seconds. It:

- puts running tasks back in the queue when their runner stopped checking in (the lease is 10 minutes), or fails them after `SWARM_MAX_ATTEMPTS` tries;
- reopens snoozed Waiting on you items when the snooze ends;
- sends one reminder to a bot whose task has waited on it for `SWARM_REMIND_HOURS` (12), and after `SWARM_ESCALATE_HOURS` (24) puts it on Waiting on you and tells the Chief of Staff;
- runs a watchdog: one alert when tasks are due but no runner has checked in for 10 minutes, and one alert when a task runs 30 minutes past its time limit (its own `timeout`, or `SWARM_TASK_TIMEOUT`);
- delivers pending notices to bot inboxes, with retries and a fallback to the Chief of Staff;
- once a day, deletes files of tasks that finished more than `SWARM_FILE_RETENTION_DAYS` (90) days ago, and sent notices older than 30 days.

### Tables

| Table | What it holds |
| --- | --- |
| `tasks` | One row per task: brief, owner, repo, provider and model, state, question, answer waiting to be used, session id, attempts, lease, current claim |
| `claims` | One row per time a runner took a task: claim id, a hash of the task token, when it ends and why, and the first finish response |
| `events` | The thread of each task: brief, state changes, answers, messages, files, failed deliveries |
| `messages` | Notes sent to a running task, kept until the runner confirms it passed them on |
| `files` | Attachments, `REPORT.md`, artifacts and saved work bundles |
| `bots` | Bot id, role (`chief` or `project`), notify URL, a short fingerprint of its trigger key, a hashed token. Never the key itself |
| `projects` | Default repo, extra repos, bound bots, provider and model, and the push rules |
| `items` | The Waiting on you list |
| `review_items` | The review queue: items bots file for a human decision, upserted by project, bot and key, with the latest decision, its bus delivery and ack. See [review-queue.md](review-queue.md) |
| `notices` | The delivery outbox. One row per event per bot, with a unique key so nothing is queued twice |
| `log_entries` | The project log (0.3.0): one append-only, typed history per project: items (status and version), decisions, feedback, learnings, rule changes, activity and results. See [project-log.md](project-log.md) |
| `activity_log` | Append-only short feed: ID, project, bot, kind, decider, summary, link, overruled ID, lasting flag, dedupe key, and creation time. The nightly export position per project is in `meta` |

Task states: `queued`, `running`, `waiting`, `done`, `failed`, `cancelled`. A waiting task has `waiting_on` set to `owner` (the bot that queued it) or `human`.

The supervisor sends one Chief of Staff escalation for a failed task with no active or completed
follow-up. Queue a follow-up to take over the work, or run `swarmctl cancel TASK_ID` to dismiss the failed
task without changing its failed state. The backend records the escalation attempt even if it cannot queue a
notice. The normal task result still goes to the owning bot; webhook transport retries can redeliver it.

## Tokens

Every request except `GET /health` needs `Authorization: Bearer <token>`. A bearer token is a long random password sent with each request. The backend stores only hashes of bot and task tokens.

| Token | Where it lives | Can do |
| --- | --- | --- |
| admin | `/etc/agent-swarm/admin.token` | Everything a person can do. Keep it on the worker and with the Chief of Staff only |
| chief bot (`swb_`) | made with `swarmctl bot token ID` for a bot with role `chief` | What admin does, except runner and task calls and making tokens |
| project bot (`swb_`) | made with `swarmctl bot token ID` | Only its own tasks, items and bot settings, and the projects it is bound to |
| worker | `/var/lib/swarm-runner/worker.token`, read by the runner only | Take the next task, read task states (no content), send alerts. Nothing else |
| task (`swt_`) | made when a task is claimed and handed to the runner for that run | Only that task and that claim: check in, read its own task and attachments, upload its own files, ask before publishing, finish |

A task token stops working as soon as the task leaves `running` (done, failed, waiting, cancelled or put back in the queue), when its lease runs out, or after `SWARM_TASK_TOKEN_MAX_HOURS` (8). Each claim has a `claim_id`. Calls with an old claim get HTTP 409 with `"claim_ended": true`, so a runner that lost its task can never finish or publish it. A repeated finish from the same claim gets the first answer again instead of changing anything.

## Who can call what

The table below is generated from the code (`python3 server/swarm_backend.py --permissions`) and a test checks that this doc matches it. "yes" means the token type may call the route; "no" means the backend answers 403 before it reads the request body. Columns: admin token, chief bot, project bot, worker token, task token, bus agent token.

| Method | Path | admin | chief | bot | worker | task | agent | What it does |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GET | `/health` | public | public | public | public | public | public | Liveness check, returns the version |
| GET | `/status` | yes | yes | yes | no | no | no | Counts, running and waiting tasks, items due, warnings. A project bot sees only its own |
| POST | `/tasks` | yes | yes | yes | no | no | no | Queue a task, or a follow-up with `follow_up` |
| GET | `/tasks` | yes | yes | yes | no | no | no | List tasks. A project bot sees only its own |
| GET | `/tasks/ID` | yes | yes | yes | no | yes | yes | One task with its event thread. Bot: own only. Task token: own task only. Bus agent: tasks its bot owns |
| POST | `/tasks/ID/answer` | yes | yes | yes | no | no | no | Answer a waiting task. Bot: own only, never for the human |
| POST | `/tasks/ID/message` | yes | yes | yes | no | no | no | Message a queued or running task. Bot: own only |
| POST | `/tasks/ID/cancel` | yes | yes | yes | no | no | no | Cancel now. Bot: own only |
| GET | `/tasks/ID/report` | yes | yes | yes | no | yes | yes | REPORT.md as text. Bot: own only. Bus agent: tasks its bot owns. Task token: own task or its parent |
| GET | `/tasks/ID/artifacts or attachments` | yes | yes | yes | no | yes | no | List files. Bot and task token: own only (a task token may also list its parent's artifacts) |
| GET | `/tasks/ID/artifacts or attachments/ID` | yes | yes | yes | no | yes | no | Download one file. Bot and task token: own only (a task token may also read its parent's artifacts) |
| GET | `/tasks/ID/bundle` | no | no | no | no | yes | no | The task's own saved work bundle (resume on a fresh folder) |
| GET | `/tasks/ID/parent-bundle` | no | no | no | no | yes | no | A follow-up's parent work bundle (unpushed parent work) |
| PUT | `/tasks/ID/files/KIND/ID` | no | no | no | no | yes | no | Upload a report, artifact or work bundle (raw body, `X-Claim-Id` header) |
| POST | `/worker/claim` | no | no | no | yes | no | no | Take the oldest due queued task. Returns a task token for it |
| GET | `/worker/task-states` | no | no | no | yes | no | no | States only (no content) for `ids=a,b,c`, used by cleanup |
| POST | `/tasks/ID/heartbeat` | no | no | no | no | yes | no | Extend the lease, get new messages, acknowledge delivered ones |
| POST | `/tasks/ID/publish-check` | no | no | no | no | yes | no | Right before a push: is the claim live and is publishing still allowed |
| POST | `/tasks/ID/finish` | no | no | no | no | yes | no | End the run: `done`, `waiting`, `next`, `limit` or `failed`. Idempotent per claim |
| GET | `/bots` | yes | yes | no | no | no | no | List bots (URLs shortened except for admin, keys never shown) |
| GET | `/bots/ID` | yes | yes | yes | no | no | no | One bot. Bot: itself only |
| PUT | `/bots/ID` | yes | yes | yes | no | no | no | Set `notify_url` and `notify_auth` (stored in a key file). Bot: itself only; only admin or chief set `role` |
| POST | `/bots/ID/token` | yes | no | no | no | no | no | Make a new token for the bot. Shown once |
| POST | `/bots/ID/test` | yes | yes | yes | no | no | no | Send one test notice to the bot's inbox. Bot: itself only |
| GET | `/projects` | yes | yes | yes | no | no | no | Project settings. Bot: only projects it is bound to |
| PUT | `/projects/ID` | yes | yes | no | no | no | no | Set repo, extra repos, bound bots, provider, model, `allow_push`, `allow_push_main` |
| GET | `/waiting` | yes | yes | yes | no | no | no | The Waiting on you list. Bot: its own items only |
| POST | `/waiting` | yes | yes | yes | no | no | no | File an item. A linked task must belong to the filing bot |
| POST | `/waiting/ID` | yes | yes | yes | no | no | no | answer, snooze, drop (human, through admin or chief); done, withdraw, update (filing bot too) |
| POST | `/alerts` | yes | no | no | yes | no | no | Body `{key, text}`. One alert to the Chief of Staff per key |
| POST | `/log` | yes | yes | yes | no | yes | no | Add a project log entry. Bot: projects it is bound to. Task token: its own task's project |
| GET | `/log` | yes | yes | yes | no | no | no | Read project log entries (`project`, `kind`, `since`, `limit`). Bot: its bound projects only |
| GET | `/log/export` | yes | yes | yes | no | no | no | One project's log as a Markdown digest. Bot: its bound projects only |
| POST | `/activity-log` | yes | yes | yes | no | yes | no | Append one activity entry. Bot: bound projects only. Task: own project with opt-in |
| GET | `/activity-log` | yes | yes | yes | no | no | no | Read recent activity (`project`, `limit`, `bot`, `kind`, `decider`, `since`). Bot: bound projects only |
| GET | `/activity-log/export` | yes | yes | yes | no | no | no | Export a Central Time month (`project`, `month`) as Markdown |
| GET | `/activity-log/snapshot` | yes | yes | yes | no | no | no | Nightly activity files changed since the last published entry (`project`, `after` overrides it) |
| POST | `/activity-log/published` | yes | yes | no | no | no | no | Save the nightly export position (`project`, `through`) after a verified push |
| GET | `/log/items` | yes | yes | yes | no | no | no | Current state of each item (deliverable) in a project. Bot: bound projects only |
| GET | `/log/rules` | yes | yes | yes | no | no | no | A project's standing rules, rule changes waiting for review, and the changelog (`format=md` for the rules doc) |
| GET | `/log/brief` | yes | yes | yes | no | no | no | Read before drafting: standing rules plus the last 14 days of feedback, as Markdown |
| GET | `/orchestrators` | yes | yes | yes | no | no | no | List orchestrators with state, worker and last heartbeat. Bot: the ones it owns |
| POST | `/orchestrators` | yes | no | no | no | no | no | Create an orchestrator pinned to one worker (secret bundles by name only). `start: true` starts it |
| GET | `/orchestrators/ID` | yes | yes | yes | no | no | no | One orchestrator with its brief and last 20 events. Bot: own only |
| PUT | `/orchestrators/ID` | yes | no | no | no | no | no | Change an orchestrator's settings; they apply at the next start or restart |
| POST | `/orchestrators/ID/ACTION` | yes | no | no | no | no | no | Set it to run, stop it gracefully, restart it (also clears a crash loop), or delete a stopped one |
| GET | `/orchestrators/ID/logs` | yes | yes | yes | no | no | no | The last lines of its terminal (`lines`, up to 500), secret-scanned. Bot: own only |
| POST | `/orchestrators/ID/messages` | yes | yes | yes | no | no | no | Queue one message (`text`, up to 4000 characters); typed into the session when the agent is idle. One at a time. Bot: own only |
| POST | `/orchestrators/ID/messages/ID/release` | yes | no | no | no | no | no | After a delivery-unknown event: mark the message delivered (default), or `resend: true` to have the supervisor type it once more |
| POST | `/worker/orchestrators/ID/heartbeat` | no | no | no | yes | no | no | Supervisor on the pinned worker only: report state and the terminal tail, get the desired state, settings and one pending message |
| GET | `/notices` | yes | yes | no | no | no | no | The last 100 deliveries with state and last error |
| POST | `/bus/send` | yes | no | no | no | no | yes | Send a direct message to an agent (`to`, `body`, `thread`, `client_msg_id`, `urgent`, `actionable`, `noop`, `reply_expected`, `meta`). Stored first, then woken. Admin sets `from` |
| GET | `/bus/inbox` | yes | no | no | no | no | yes | An agent's undelivered and unread messages (`all=1`, `after`, `limit`). Reading marks them delivered. Admin names `agent` |
| GET | `/bus/messages/ID` | yes | no | no | no | no | yes | One message with its delivery attempts. Agent: sender or recipient |
| POST | `/bus/messages/ID/ack` | yes | no | no | no | no | yes | Mark a message read. `first` is true only once; also returns `newer_in_thread`. Agent: recipient only |
| GET | `/bus/threads/ID` | yes | no | no | no | no | yes | The last messages of a thread (`limit`, up to 100). Agent: only ones it sent or received |
| GET | `/bus/agents` | yes | no | no | no | no | yes | Agents with kind, wake, aliases and whether they are reachable |
| PUT | `/bus/agents/ID` | yes | no | no | no | no | no | Register or change an agent (`kind`, `wake`, `bot_id`, `orchestrator`, `description`, `aliases`, `notices`, `digest_minutes`, `poll_window_minutes`) |
| DELETE | `/bus/agents/ID` | yes | no | no | no | no | no | Retire an agent. Its history stays |
| GET | `/bus/agents/ID/token` | yes | no | no | yes | no | no | The agent's token. Admin: any. Worker token: the `worker` query is required and the agent's orchestrator must be pinned to that worker name (the name is self-declared: the kit has one shared worker token) |
| POST | `/bus/agents/ID/token/rotate` | yes | no | no | no | no | no | Revoke every token of the agent and return a new one |
| GET | `/bus` | yes | no | no | no | no | no | Plain HTML view of agents and the last 100 messages. Read only; admin token only (a reverse proxy makes every request look like loopback, so there is no loopback exception) |
| PUT | `/review/items` | yes | yes | yes | no | no | yes | File or update a review item, upsert by (`project`, `bot`, `key`). Bot and bus agent: as itself. Admin and chief name `bot` |
| POST | `/review/items` | yes | yes | yes | no | no | yes | Same as PUT /review/items |
| GET | `/review/items` | yes | yes | yes | no | no | yes | List review items (`status`, `acked=0`, `project`, `key`, `type`, `limit`; admin and chief: `bot`). Bot and bus agent: own only |
| GET | `/review/items/ID` | yes | yes | yes | no | no | yes | One review item with its `content_hash` and decision. Bot and bus agent: own only |
| POST | `/review/items/ID/decision` | yes | yes | no | no | no | no | Decide: `action` approve, changes or reject, with the `content_hash` read from the item (409 stale and nothing recorded on a mismatch), `note`, `edited_text`, `option`, `revision` |
| POST | `/review/items/ID/ack` | yes | yes | yes | no | no | yes | Acknowledge a decision once consumed (`revision`). Bot and bus agent: own only |
| POST | `/review/items/ID/close` | yes | yes | yes | no | no | yes | Withdraw an open item (`reason`). Bot and bus agent: own only |

Body fields for `POST /tasks`: `prompt` (required), `project`, `repo`, `provider`, `model`, `owner`, `kind`, `title`, `report_only`, `push`, `push_main`, `open_pr`, `pr_number`, `branch`, `ask_human`, `untrusted_input`, `command`, `idempotency_key`, `allow_duplicate`, `follow_up`, `max_attempts`, `attachments` (`[{name, content_base64}]`, 20 files, 25 MB each).

`POST /activity-log` takes `{project, kind, decider?, summary, link?, dedupe_key?, lasting?, overrules?}`.
`project` is a registered project or `cross-project`. A `decision` needs a `decider` (`owner`, `delegated`
or `bot`). The same `dedupe_key` from the same bot within 24 hours returns the first entry. The response
is `{entry, duplicate}`. Listing returns `{entries}`, oldest first, `limit` 1 to 1000 (default 20).
Snapshot returns `{after, through, files}`; `after` defaults to the saved position from
`POST /activity-log/published`, and `files` is empty when nothing changed. See the
[activity and decisions guide](activity-and-decisions.md) for write permissions and export setup.

Rules a project bot meets:

- It can queue repo tasks only for projects that list it (`swarmctl project set NAME --bot BOT_ID`), and only for that project's repos. With no project it can queue only tasks with no repo, like research or writing.
- `push_main` (pushing straight to the main branch) is refused unless the project has `allow_push_main` and still lists the target repo.
- It can have at most `SWARM_MAX_OPEN_TASKS_PER_OWNER` (100) open tasks.

`swarmctl` wraps all of these. Run `swarmctl --help` and `swarmctl SUBCOMMAND --help`.

## Reaching the backend from your bots

Grok Bot bots run on their own computers, not on your worker. To queue tasks and read reports they must reach the backend. Notices to bots go the other way: the backend calls the bot's inbox URL, which is an outbound call and needs no opening on the worker.

### Option 1 (recommended): Tailscale Serve, with Tailscale on the bot's computer

Tailscale is a private network between your own machines (your "tailnet"). Tailscale Serve publishes the backend to your tailnet only, with a real https certificate. Nobody on the public internet can reach it.

1. Install Tailscale on each bot's computer and join it to your tailnet with the tag `tag:swarm-bot`. A Grok Bot box is a normal Linux machine, so `curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up --auth-key file:/path/to/key --advertise-tags=tag:swarm-bot` works there.
2. On the worker: `sudo tailscale serve --bg 8750`. The address is `https://YOUR-WORKER.YOUR-TAILNET.ts.net`.
3. Use the sample access rules (ACL) in [worker-setup.md](worker-setup.md#tailscale-access-rules) so bot computers can reach only the worker's port 443, and nothing else.
4. Set `SWARM_PUBLIC_URL` in `swarm.env` to that address, then `sudo systemctl restart swarm-backend`.

Check with `tailscale serve status`. Turn it off with `sudo tailscale serve reset`.

### Option 2: Tailscale Funnel (public internet)

Use this only when a bot's computer cannot run Tailscale. `sudo tailscale funnel --bg 8750` gives the same address, but **anyone on the internet can reach it**. The tokens are then the only gate.

Warnings if you use Funnel:

- Every bot needs **its own token**. A leaked bot token exposes only that bot's tasks, and you can replace it any time with `swarmctl bot token BOT_ID`.
- Only the Chief of Staff holds the admin token or a chief token. Never give either to a project bot.
- Expect scanners and password guessing. The backend limits this (below), but watch `journalctl -u swarm-backend` for many 401 or 429 lines.
- Check exposure with `tailscale funnel status`. Turn it off with `sudo tailscale funnel reset`.

### Option 3: a reverse proxy or Cloudflare Tunnel

Caddy, nginx or `cloudflared` in front of port 8750 lets you add a second gate such as Cloudflare Access or an IP allow list. More parts to set up and keep working.

### Protections built into the backend

These apply whichever option you pick:

- The route and the token are checked **before** the request body is read. Requests with a missing, negative or bad `Content-Length` are refused.
- Bodies are limited to 1 MB, except task creation and file uploads.
- Each connection has a 30 second timeout, and at most `SWARM_MAX_CONNECTIONS` (32) are served at once.
- After `SWARM_AUTH_FAIL_LIMIT` (20) failed logins from one address in 5 minutes, that address gets HTTP 429 (too many requests) for a while.
- Notify URLs must be https. Their host name is looked up, and every address must be public: private, loopback (this machine), link-local, carrier-grade NAT, multicast and reserved addresses are refused. This is a guard against SSRF (server-side request forgery: tricking the backend into calling machines on your own network). The check runs when the URL is set and again at every delivery, the connection goes to the address that was checked, and redirects are never followed.

## The tradeoff of running on the worker

One box is simple and cheap. The cost: **when the worker is down, tracking is down too.** Bots cannot queue tasks or read reports until it is back. Nothing is lost: the data is a file on disk, backed up every night, and systemd restarts the service after a crash or reboot. A bot can retry with the same `idempotency_key` without making a duplicate.

## Cloudflare Workers and D1 (design only, not built)

Some people want tracking to keep working while the worker is down for maintenance. For them, the backend could run on Cloudflare Workers (small programs on Cloudflare's network) with D1 (Cloudflare's SQLite database).

**This kit ships the on-worker backend only. The Cloudflare version is a design, not built and not tested.** The design:

1. The same routes, permission table and JSON shapes, so `swarmctl` and bots do not change. Only `SWARM_URL` changes.
2. D1 uses the same tables as `SCHEMA_TABLES` in `swarm_backend.py`, including `activity_log` and its
   per-project export cursor. Match the five `/activity-log` routes and their roles and response shapes.
3. A Cron Trigger every minute does what the background loop does.
4. Files go to R2 (Cloudflare's file storage).
5. Tokens are Worker secrets, and trigger keys are encrypted secrets, never plain table columns. The runner connects out over https with the worker token, so the worker needs no inbound exposure at all.

## Backups and restore

`swarm-backup.timer` runs `scripts/backup.sh` every night at 03:10 as the `swarm` user. It makes a consistent copy with SQLite's backup API, checks it, gzips it to `/var/lib/agent-swarm/backups/swarm-DATE-TIME.db.gz` (mode 600), and keeps `SWARM_BACKUP_KEEP_DAYS` (14) days. Set `SWARM_OFFSITE_CMD` to also copy the folder off the box.

What a backup holds: no tokens and no trigger keys (tokens are stored only as hashes, and keys live in `/etc/agent-swarm/bot-keys/`). It **does** hold every brief, answer, report and attached file, so it is still private. Off the box, use an encrypted remote such as an rclone "crypt" remote or restic.

Restore with the script. It unpacks and checks the backup before it touches anything, keeps the current database as `swarm.db.before-restore-TIME`, then restarts the services:

```bash
sudo bash /opt/agent-swarm-kit/current/scripts/restore-backup.sh /var/lib/agent-swarm/backups/swarm-DATE-TIME.db.gz
swarmctl status
```

Tasks that were running come back as queued after their lease runs out. Bot trigger keys are not in the backup, so after restoring on a new box copy `/etc/agent-swarm/bot-keys/` too, or set each bot's key again with `swarmctl bot set BOT_ID --notify-auth-file FILE`.

## Upgrading from 0.1.0

The first start of the 0.2.0 backend moves any trigger keys stored in the old `bots.notify_auth` column into key files (`/etc/agent-swarm/bot-keys/BOT_ID.key`, mode 600), blanks the column, and runs `VACUUM` so the old values are gone from the database file. Backups made before the upgrade still contain the old keys: delete them, or change each bot's key. The full steps are in [updating-the-kit.md](updating-the-kit.md).

## Moving to a new box

1. Set up the new box with `scripts/setup-worker.sh`.
2. On the old box: `sudo swarmctl drain`, wait for running tasks to end, then `sudo systemctl stop swarm-worker swarm-backend`.
3. Copy over Tailscale (`scp`): the newest backup, `/etc/agent-swarm/swarm.env`, `admin.token`, `worker.token` and the `bot-keys/` folder. Keep owners and modes: `root:swarm 640` for `admin.token` and `worker.token`, `swarm:swarm 600` for key files.
4. Put the runner's copy of the worker token at `/var/lib/swarm-runner/worker.token` (owner `swarm-runner`, mode 600).
5. Restore with `restore-backup.sh`, run `sudo tailscale serve --bg 8750` on the new box, and update `SWARM_PUBLIC_URL` if the name changed.
6. Run `sudo bash /opt/agent-swarm-kit/current/scripts/smoke-test.sh`.
