# Review queue

One place where bots file things that need you (decisions, copy, creative, a deliverable to look at) and where you decide them: **approve**, **request changes** or **reject**, with an optional note, your edited text or the option you pick. The decision goes back to the bot that filed it. A simple port of agent-control's review queue (its PR 62), on the kit's own pieces: a `review_items` table in the backend's SQLite database, logic in `server/swarm_review.py`, the [Agent Bus](agent-bus.md) for delivery, and `swarmctl review-queue` (short: `swarmctl rq`).

Nothing publishes from the queue. Approving a post approves that text; the bot (or you) posts it.

It sits beside the [Waiting on you list](concepts.md) (`swarmctl waiting`), which keeps working as before. Use the review queue when you want an explicit approve / changes / reject answer back to the bot.

## Who does what

| Who | Token | May |
|---|---|---|
| A bot | its bot token, or its bus agent token (`ab_...`) | file, list and read its own items, ack decisions, close its own open items. Files only as itself |
| You, through the CLI | admin token | everything, including deciding. Name `bot` when filing for a bot |
| Chief of Staff | chief bot token | everything an admin may do here; its decisions are recorded as `by: chief:<bot id>` |

A bot's items are the ones filed as its bot id or as a bus agent tied to it (the agent's name, its aliases, or an agent whose `bot_id` is the bot). Another bot's item answers 404.

## File or update an item

`PUT /review/items` (or `POST`) upserts by **(project, bot, key)**:

- no open item for the key: creates one (201, `result: "created"`);
- an open item: updates it in place. Any change bumps `revision`; an identical re-send returns `result: "unchanged"`. Fields you leave out keep their value; `null` clears one;
- the item is in **changes requested**: a changed upsert reopens that same item as its next revision (`result: "reopened"`);
- the latest item was approved, rejected or closed: a new item is created with `previous_item_id`. If you rejected the previous one, the response carries a `warning`. Do not use this to re-ask something you were told no to.

| field | notes |
|---|---|
| `project` (required) | project name |
| `key` (required) | the bot's stable id for the thing, 1-200 characters `A-Za-z0-9._:+-` (alias `dedupe_key`) |
| `type` | `decision` (default), `copy`, `creative`, `social_post`, `deliverable`, `pr` |
| `title` | one line, up to 200 |
| `ask` | the question, up to 4,000 (defaults to the title). `title` or `ask` is required |
| `body` | the text to review (markdown or post copy), up to 100,000 |
| `recommendation` | up to 2,000 |
| `options` | up to 12: strings or `{label, reason, suggested}` (at most one suggested, labels unique) |
| `links` | up to 20 http(s) URLs |

A 400 lists every problem at once (`errors`). Text that looks like a secret is refused and not stored.

```sh
swarmctl rq file --project offerrun --key pricing-2026-11 --title 'Price the starter plan at $49 or $79?' \
  --rec '$49' --option '$49' --option '$79' --link https://example.com/pricing-draft

curl -sS -X PUT "$SWARM_URL/review/items" -H "Authorization: Bearer $(cat "$BUS_TOKEN_FILE")" -H 'content-type: application/json' \
  -d '{"project":"offerrun","key":"pricing-2026-11","title":"Price the starter plan at $49 or $79?","recommendation":"$49",
       "options":[{"label":"$49","reason":"More signups in week one","suggested":true},{"label":"$79"}]}'
```

`POST /review/items/ID/close {"reason": "..."}` withdraws an open item that no longer needs you.

## Read

- `GET /review/items?status=open|changes_requested|pending|decided|answered|approved|rejected|closed|all&acked=0|1&project=&key=&type=&limit=` lists items, newest change first (default `open`, `limit` up to 500, `total` counts every match). A bot sees only its own; admin and chief may add `bot=`. `pending` is open plus changes requested; `answered` (same as `decided`) is approved, rejected or changes requested.
- `GET /review/items/ID` returns one item.

Every item carries `content_hash`, `status`, `revision`, and once decided `decision: {action, option, note, edited_text, revision, content_hash, by, at}` and `delivery: {state, bus_message_id, error, acked_at}`.

## Decide

`POST /review/items/ID/decision` (admin or chief):

```json
{"action": "approve", "content_hash": "<from the GET>", "option": "$49", "note": "Go.", "edited_text": "...", "revision": 1}
```

- `action`: `approve`, `changes` (also `request_changes`) or `reject`. Status becomes `approved`, `changes_requested` or `rejected`.
- `content_hash` is **required**: it is what you saw, SHA-256 over a canonical JSON of the ask, each option (label, reason) and the recommendation. If the bot changed any of those since you read the item, or you send none, the answer is **409** `{code: "stale", item: <current item>}` and **nothing is recorded**. The check and the write share one transaction. `revision` is optional; when sent it must match too.
- `option`, when the item has options, must be one of their labels.
- Only an `open` item can be decided (409 `not_open` otherwise). After **changes**, the bot revises the item with the same key and it reopens as the next revision.

From the CLI, `swarmctl rq show ID` prints the item and its hash; `swarmctl rq decide ID --action approve --hash HASH [--choose OPTION] [--note TEXT] [--edited-file FILE]`.

## The decision you receive

Right after the decision is recorded, the backend looks for the filing bot's bus agent (by name, alias or bot id, any `notices` setting):

- **It has one:** an urgent bus message from `agent-control` on thread `review:ID`, client id `review:ID:REVISION`, `reply_expected` false. A webhook bot is woken at once, a tmux orchestrator gets it typed, a poll agent finds it in its inbox. `meta` is `{kind: "review_decision", item_id, key, project, decision, revision, option}`.

  ```text
  [review-decision] item=r3f2... key=pricing-2026-11 project=offerrun decision=approve revision=1

  Type: decision · decided by admin

  Title: Price the starter plan at $49 or $79?

  Chosen option: $49

  Note:
  Go.

  Full record: GET https://swarm.example/review/items/r3f2...
  Ack when consumed: POST https://swarm.example/review/items/r3f2.../ack {"revision": 1}
  ```

  If the full text is longer than the bus limit, or the bus refuses it (for example a note that looks like a secret), a pointer-only message goes instead. `delivery.state` is `bus`, or `failed` with `delivery.error` if even that was refused.
- **It has none:** `delivery.state` is `no_agent`. The decision is recorded and the bot reads it from `GET /review/items?status=answered&acked=0` (its own token).

Either way, the bot calls `POST /review/items/ID/ack {"revision": N}` once it has acted on the decision. The first ack answers `first: true` and also marks the bus message read; later ones `first: false`. An ack naming another revision than the one decided answers `{stale: true}` and changes nothing. The answered, unacked list is the source of truth: a bus message to a poll agent that is not fetched in 24 hours expires on the bus, but the decision stays in that list until it is acked.

## Poll-only bots

A bot that cannot take a webhook can register as a poll agent and check in on its own schedule: `swarmctl bus register NAME --kind bot --wake poll --poll-window 1500` (`PUT /bus/agents/NAME {"kind":"bot","wake":"poll","poll_window_minutes":1500}`). It is then not reported unreachable between daily check-ins. See [agent-bus.md](agent-bus.md).

## Not in the kit (compared with agent-control)

Kept out on purpose to keep this small: the dashboard page (the kit has no web UI beyond the read-only `/bus` page), meeting flags, SLA and held-feedback alerts, batches and sync, media uploads, `[NEED: ...]` fill-ins and variants, comments and version snapshots, expiry, the legacy `/gil-items` migration, linked-task and parent-task propagation with its retry ledger, an activity-log row per decision, idempotency keys on decisions, and a retry sweep for decisions whose bus send failed (the bot still finds them in its answered, unacked list).
