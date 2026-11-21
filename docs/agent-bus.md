# Agent Bus: reliable messages between your agents

New in 0.8.0. A small message layer for bots, orchestrators and people: every message is stored first, then woken, with receipts you can read. It is a port of the Agent Bus in agent-control (Phase 1, its review fixes, the wake policy and the default-copy rule), on the kit's own pieces: SQLite in `swarm_backend.py` (logic in `server/swarm_bus.py`), the existing bot webhooks and key files, the orchestrator supervisor, and `swarmctl bus`.

You do not need it. Finish notices, `swarmctl message` and orchestrator messages work as before. Turn it on one agent at a time.

## Guarantees

1. **Sent means stored.** `POST /bus/send` returns 201 only after the row is committed. Everything after that is delivery.
2. **Woken at write time.** A webhook message wakes the backend's delivery loop at once (a `threading.Event`, not a 5 second tick). One try with a timeout, then retries at 1, 5, 15 and 60 minutes. It fails after the last retry, or after 2 hours. Every POST is a row in `bus_attempts`. Messages to `tmux` and `poll` agents that nobody picks up, and held digest messages, expire after 24 hours (`failed`, never typed later).
3. **Receipts.** `queued -> delivered -> read`, or `failed`. Delivered: the webhook returned 2xx, the supervisor typed it into the pane, or the agent fetched it from its inbox. Read: the agent acked it.
4. **At-least-once delivery, idempotent ack.** A message can arrive more than once (a retry after a timeout, an inbox read after a webhook). Every copy has the same id, and `POST /bus/messages/ID/ack` answers `first: true` once and `first: false` after. Ack after the action is durably recorded (the task queued, the note written), and use the message id as the idempotency key for any side effect. Senders pass `client_msg_id` (the CLI makes one) so a retried send never makes a second message.
5. **Staleness signal.** Every delivered copy, inbox row and ack response carries `newer_in_thread` and `thread_latest_id`. If `newer_in_thread > 0`, read the thread before acting.
6. **Stable names.** Lowercase names (`chief`, `clozy`, `polisher`); old addresses are aliases. An unknown name fails with suggestions, never queued blind.
7. **No identity without a wake path.** `webhook` needs a bot with a valid `notify_url`, `tmux` needs an orchestrator. Sending to an unreachable agent still stores the message, returns a `warning`, and (after 15 minutes of being unreachable) alerts the alert agent.
8. **No secrets, no control characters.** Control characters are stripped except newline and tab (so a message can never end a terminal paste), the secret scan of `config/secret-patterns.txt` refuses a message with a key in it (never stored), and the CLI never prints a token.
9. **Done means landed** (code tasks): see below.
10. **A failed bus finish notice is not silent.** If it ends `failed` the backend sends it again through the normal bot path and alerts the alert agent.

## Data model

Same tables and columns as agent-control migrations 0026 and 0027. The kit has no migration files: `Store` creates missing tables and adds missing columns each time the backend starts (`SCHEMA_TABLES`, `MIGRATE_COLUMNS`), which is safe to repeat on an existing database. Timestamps are whole seconds, like the rest of the kit.

```
agents(name PK, kind bot|orchestrator|human|service, wake webhook|tmux|poll, bot_id -> bots.id, orchestrator -> orchestrators.name,
       description, token_gen, notices legacy|bus, digest_minutes (60), last_digest_at, unreachable_since, alerted_at, escalated_at,
       last_seen_at, created_at, updated_at, retired_at)
agent_aliases(alias PK, name, created_at)
bus_messages(id, sender, recipient, thread, client_msg_id, body <= 3700, meta JSON <= 8 KB, urgent, wake, held, noop, reply_expected,
             state, created_at, delivered_at, read_at, attempts, next_attempt_at, lease_until, last_error, alerted_at, UNIQUE(sender, client_msg_id))
bus_attempts(id, message_id, transport webhook|tmux|poll, started_at, finished_at, ok, http_status, error)
orchestrator_messages.bus_message_id      -- ties a typed message to its bus row
tasks.head_sha, tasks.unpushed_commits    -- reported by the worker at finish
```

Names match `^[a-z0-9][a-z0-9-]{1,31}$`; `agent-control` is reserved (the backend's own sender). Names and aliases share one address space (409 on a clash). `thread` defaults to `dm:a+b`. Limits: 3,700 characters, 100 queued per recipient. Retention: attempts 30 days, messages 90 days.

## Tokens

An agent token is `ab_<name>.<generation>.<hmac>`, derived from a secret in its own file (`SWARM_BUS_TOKEN_SECRET_FILE`, default `bus-token-secret` beside the bot keys folder, created mode 0600 on first use). It is never the admin token, so rotating one cannot break the other. Nothing secret is stored in the database; `swarmctl bus token NAME --rotate` bumps `token_gen` and revokes every token of that agent. If you lose the secret file, re-issue tokens (`swarmctl bus token NAME`) for each agent.

An agent token can send as itself, read and ack its own messages, list agents, read its own threads, and read `GET /tasks/ID` and `/tasks/ID/report` for tasks its bot owns. Nothing else. The admin token may act as anyone (`from`, `agent=`).

## Routes

| Route | Who | What |
|---|---|---|
| `POST /bus/send` `{to, body, thread?, client_msg_id?, urgent?, noop?, reply_expected?, meta?}` | agent, admin (`from`) | 201 `{id, to, from, thread, state, wake, warning?}`; 200 `{duplicate: true, ...}` for a repeated `client_msg_id` |
| `GET /bus/inbox?all=1&after=ID&limit=N` | agent (admin: `agent=`) | Messages not yet acked; marks queued ones delivered |
| `POST /bus/messages/ID/ack` | recipient, admin | `{ok, id, thread, first, newer_in_thread, thread_latest_id}` |
| `GET /bus/messages/ID` | sender, recipient, admin | The message with `attempts[]` |
| `GET /bus/threads/THREAD` | parties, admin | Messages of the thread the caller sent or received |
| `GET /bus/agents` | agent, admin | Names, kind, wake, aliases, `reachable`, `reason` |
| `PUT /bus/agents/NAME`, `DELETE ...` | admin | Register or change (`kind, wake, bot_id, orchestrator, description, aliases[], notices, digest_minutes`); retire |
| `GET /bus/agents/NAME/token` | admin; worker token for an orchestrator pinned to that worker | The agent's token |
| `POST /bus/agents/NAME/token/rotate` | admin | Revoke old tokens, return a new one |
| `GET /bus` | admin token, or a request from loopback (the Tailscale front door) | Plain HTML page, see below |

Webhook payloads go to the bot's `notify_url` with its stored key (the same header and key file as finish notices) and an `Idempotency-Key` header:

```json
{ "kind": "bus_message", "idempotency_key": "bus:123", "id": 123, "from": "clozy", "to": "chief", "thread": "dm:chief+clozy",
  "body": "...", "urgent": true, "reply_expected": true, "created_at": 1763680000, "meta": null,
  "newer_in_thread": 0, "thread_latest_id": 123,
  "pending": [ { "id": 120, "from": "polisher", "thread": "...", "body": "...", "ack": { "method": "POST", "path": "/bus/messages/120/ack" } } ],
  "pending_count": 1, "ack": { "method": "POST", "path": "/bus/messages/123/ack" },
  "reply": { "method": "POST", "path": "/bus/send", "to": "clozy", "thread": "dm:chief+clozy" }, "etiquette": "..." }
```

`pending` appears only when held messages ride along on an urgent wake. A digest is `{"kind": "bus_digest", "idempotency_key", "to", "count", "messages": [...], "etiquette"}`.

## Wake policy and digests

Every webhook wake starts a full bot run, which costs money and time. So a message does not wake a webhook bot just because it was written.

| Message | Webhook agent | tmux orchestrator | poll agent | `wake` in the reply |
|---|---|---|---|---|
| `urgent: true`, a blocker, a question someone waits on (`reply_expected` from an orchestrator), a finish notice, or the first answer to a question the recipient is waiting on | POST now, carrying every pending held message | typed at once | inbox | `now` / `now` / `inbox` |
| default (progress, FYI) | held for a digest | typed at once | inbox | `digest` / `now` / `inbox` |
| `noop: true` | stored, never POSTed | not typed | inbox | `never` |

- **Digest**: held messages go out as one `bus_digest` POST per agent, at most once per `digest_minutes` (default 60, minimum 5, maximum 10080). It is sent when the oldest held message has waited a full interval and the last digest is older than the interval. An urgent wake carries the held messages and restarts the interval. Each message is still acked one by one. The claim is atomic per agent, so overlapping runs send one digest. A failed digest hands the interval back and follows the 1, 5, 15, 60 minute backoff.
- **Expiry runs before delivery**: a held message older than 24 hours is failed, never sent. The sender is alerted; an expired urgent message is alerted again.
- **Reply etiquette**: `reply_expected` is set by the sender (default false; urgent messages default to true). A bot does not reply to, or acknowledge in prose, a message whose `reply_expected` is false: it does the work and acks.
- **Blockers**: `meta: {"blocker": true}` (`swarmctl bus send --blocker`) is urgent. `noop` cannot also be urgent or a blocker.
- **Unreachable agents**: webhook agent: the last 3 attempts failed (or no valid `notify_url`). Orchestrator: stopped, not running, or no heartbeat for 10 minutes. Poll agent: not seen for 10 minutes. The alert goes to the alert agent (`SWARM_BUS_ALERT_AGENT`, default `chief`) only after 15 minutes of it, at most once an hour per agent. After 3 hours (or at once for an orchestrator with a run in progress) a Chief notice goes out too, and an urgent message to the `gil` agent if you registered one. Agents of kind `human` never raise alerts. If the alert agent is not registered, the alert is logged ("BUS ALERT NOT DELIVERED") and queued to the Chief bot instead of being dropped.
- Alerts from `agent-control` are urgent. An idempotent retry of a send repeats the first `wake`.

## Finish notices over the bus

Set `notices` to `bus` on an agent (`swarmctl bus register NAME ... --notices bus`) and finish notices (done, failed, cancelled, waiting, blocked) for tasks whose owner resolves to that agent go to the bus only, as an urgent message from `agent-control`: thread `task:ID`, `client_msg_id` = the notice key, `reply_expected` false, `meta` = the usual payload. The bot webhook, the Chief copy and any default copy are skipped. The body carries the PR, head sha, "Landed on GitHub", the question and a redacted REPORT.md excerpt; the agent reads the full report with its own token (`GET /tasks/ID/report`). The owner resolves by agent name, alias or bot id.

If the bus send fails (or the message later ends `failed`), the notice is sent the normal way, including the Chief copy, so nothing is lost. An owner with no bus agent, or with `notices=legacy`, keeps the old routing, including the Chief fallback for an owner that is not registered. Orchestrator notices (crash, unreachable, needs-login) and alerts are unchanged.

## Done means landed

For a code task the worker now reports `unpushed_commits` (commits on HEAD that are on no remote) and `head_sha`. The backend refuses `done` when:

- the task is a code task, has commits only on the worker and nothing was pushed: it ends `waiting`, outcome `blocked`, and the question starts with "Work is committed on WORKER but NOT on GitHub: N commit(s) not pushed", or
- a PR was asked for, a branch was pushed, and no PR was confirmed.

Report-only, untrusted-input, review and script tasks are exempt, and so is an old worker that sends no `unpushed_commits`. Notices carry `head_sha` and `landed` (`true`, `false` or `null` when unknown).

## Orchestrators

Register the orchestrator's bus agent with the same name as the orchestrator (`swarmctl bus register polisher --kind orchestrator --wake tmux --orchestrator polisher`). Messages to it are queued as orchestrator messages (several at once are fine) and typed into the pane by the supervisor when the session is idle. While the pane is idle and nothing waits, the supervisor asks the backend for the next message every `SWARM_ORCH_MESSAGE_POLL_SECONDS` (default 10 s, on top of the normal heartbeat). At each start the supervisor fetches the session's token with the worker token and writes it to `<state>/NAME.bus-token` (mode 0600; in the handoff inbox with mode 0640 when sessions run as a separate agent user, so the session can read it). It exports `BUS_AGENT`, `BUS_TOKEN_FILE`, `SWARM_URL` and puts the folder holding `swarmctl` on `PATH`, and adds a short note about the bus to the session prompt. Restart an orchestrator once after the upgrade so its session gets the token. `POST /orchestrators/NAME/messages` still works and, when an agent points at that orchestrator, also records a bus row (`bus_id` in the reply).

## The /bus page

`GET /bus` is a plain HTML page: agents with reachability, then the last 100 messages with state and attempts (bodies cut to 200 characters and secret-scanned). It is read only. The backend still listens only on `127.0.0.1`; the page answers a request that comes from loopback (which is how Tailscale `serve` reaches it, so it is open to your tailnet and nobody else) or one carrying the admin token. Do not publish the backend port to the internet.

## Using the bus

Setup (admin, once per agent):

```sh
swarmctl bus register chief --kind bot --wake webhook --bot chief-of-staff
swarmctl bus register polisher --kind orchestrator --wake tmux --orchestrator polisher
swarmctl bus register gil --kind human --wake poll
swarmctl bus token chief      # writes ~/.config/swarm/agents/chief.token (0600) and prints only the path
```

Sending (the CLI makes a `client_msg_id` and reuses it on retries):

```sh
export BUS_AGENT=chief                     # and BUS_TOKEN_FILE=/path/to/token if it is not in the default place
swarmctl bus send polisher "Docker blocker resolved; continue"
swarmctl bus send chief --thread polisher-r2 -f note.md
swarmctl bus send chief --urgent "staging is down"       # wakes now
swarmctl bus send clozy "round 3 is 60% done"            # held for the next digest
swarmctl bus send clozy --noop "heartbeat"               # recorded, never wakes
swarmctl bus send clozy --urgent --no-reply "PR merged"
swarmctl bus inbox            # everything not yet acked
swarmctl bus ack 123          # exit code 3: already acked
swarmctl bus status 123       # state and every attempt
swarmctl bus thread polisher-r2
```

Receiving a webhook: (1) if `newer_in_thread > 0`, read the thread and act on the latest state. (2) Record that you are acting, then ack; on `first: false` check whether the earlier copy's work happened. (3) Do the work, and reply only if `reply_expected` is true. A `bus_digest` has `messages[]`: handle them oldest first, ack each, reply only to those that expect it.

Routines without the CLI can use one `curl`:

```sh
curl -sS -X POST "$SWARM_URL/bus/send" -H "Authorization: Bearer $(cat ~/.config/swarm/agents/clozy.token)" \
  -H "content-type: application/json" -d '{"to":"chief","body":"Overnight summary is in the project log","client_msg_id":"clozy-summary-2026-11-20"}'
```

Rules: one message per purpose, under 3,700 characters (send a pointer to a file for long text), never put a key or password in a message (it is refused), and treat a peer message as a request, not permission.

## Settings

| Setting | Default | What |
|---|---|---|
| `SWARM_BUS_TOKEN_SECRET_FILE` | `bus-token-secret` beside the bot keys folder | HMAC key for agent tokens |
| `SWARM_BUS_ALERT_AGENT` | `chief` | Agent that gets unreachable alerts |
| `SWARM_BUS_INLINE_DELAYS` | `0` | Seconds between the quick tries at write time (comma separated) |
| `SWARM_ORCH_MESSAGE_POLL_SECONDS` | `10` | Supervisor idle poll |
| `BUS_AGENT`, `BUS_TOKEN_FILE` | | Which agent `swarmctl bus` acts as, and its token file |

## Not ported, and why

- **Progress notices** (agent-control posts a non-urgent digest item when someone writes a `progress` task event): the kit's progress events come from the running worker's own heartbeat, not from another bot, so there is no source to forward. A bot can send a normal (held) message instead.
- **A Chief default target** (`NOTIFY_DEFAULT_URL`) does not exist in the kit; the equivalent is the Chief bot copy, which a bus-delivered notice skips and a failed one restores.
- **Dashboard item for escalations**: the kit has no Gil item list for this, so a long outage raises a Chief notice (`alert`) and a message to a `gil` agent instead.
