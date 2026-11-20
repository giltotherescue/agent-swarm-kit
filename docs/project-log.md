# Project log

For short shipped, blocked and decision lines, use the separate [activity feed](activity-and-decisions.md).
This page covers the detailed project log. Its weekly digest is separate from the nightly memory export.

Every project gets a simple history: what was made, who approved it, what you decided, what you said,
and what the bots learned from it. Projects with code have git history; this gives the same memory to
projects without a repo, like marketing, sales or a newsletter. It is built into the backend.

The design comes from a marketing setup that worked well. Its main ideas: every deliverable has a
status and a version, your feedback turns into a short list of rules the bots read before every draft,
and changing those rules needs your approval.

## What goes in it

The log is one typed list of entries per project. Each entry has a kind:

| Kind | What it records | Required besides the summary |
| --- | --- | --- |
| `item` | A deliverable (an ad, a post, an email) with a status and a version | `status` |
| `decision` | A choice that was made | `by` (who decided), `answer`; `source` is optional (default `chat`) |
| `feedback` | What someone said about the work, optionally about one item | `by` (who said it) |
| `learning` | A lesson. Starts `pending`; becomes `standing` only through an approved rule | none |
| `rule` | A proposed change to the project's rules, with the text before and after | `after` for a new rule |
| `activity` | Work done that is not a deliverable: "Sent the November newsletter" | none |
| `result` | A finished or failed worker task (added automatically) | none |

Every entry has a stable number (`id`) and the time it was added. Items, learnings and rules also have a
stable `ref` such as `item-12`, `learning-15` or `rule-30` that stays the same for their whole life.

**Append-only.** Entries are never edited or deleted. A change is a new entry with the same `ref`, and
the newest one is the current state. The one exception is throwaway items (see below).

**Bad records are refused** with a clear error: an unknown kind or status, a `bot` that is not
registered, feedback or a decision or a verdict without `by`, a decision without `answer`, or a `ref`
that does not exist in that project.

## Items: status and version

An item's status is one of `draft`, `needs-review`, `approved`, `changes` (changes requested) or
`rejected`. It starts at version 1 as `draft` or `needs-review`.

- **A verdict** (`approved`, `changes`, `rejected`) needs `by`. Send the `version` you looked at; if the
  item has moved on to a newer version, the verdict is refused, because it was about old work.
- **A new version voids older verdicts.** When a bot re-drafts an item after a verdict (status back to
  `draft` or `needs-review`), the version goes up by one, so the re-draft starts clean. A bot can also
  start a new version on purpose with `new_version`.
- **Throwaway items** (a test banner, a one-off draft) get `expires_days` when created. As soon as they
  expire they drop out of every view (`log list`, `log items`, the brief and the export), and the daily
  cleanup then deletes their entries. Feedback about an expired item stays.

`swarmctl log items PROJECT` shows the current state of every item.

## From feedback to rules

1. You say what you think, in chat, in a few words. The bot logs it as `feedback` with `by` set to you.
2. **Every piece of feedback becomes a pending learning automatically.**
3. When a learning looks lasting, a bot proposes a `rule` change based on it. The proposal stores the
   rule text before and after.
4. **You review it**: through the Chief of Staff (or with the admin token), approve or reject. Only
   then does the rule change, and the learning becomes `standing`. Project bots cannot approve.
5. **Before drafting, every project bot reads the brief**: the standing rules plus a digest of the last
   14 days of feedback (`swarmctl log brief PROJECT`, or `GET /log/brief?project=PROJECT`).

So there are two lanes. Records (items, decisions, feedback, learnings, activity) go straight in. Rule
changes wait for review.

**One short rules doc per project.** It holds at most 25 rules (`SWARM_RULES_CAP`), so a bot can read
it before every draft. Adding a rule when the doc is full is refused; merge or remove one first. The
doc ends with a changelog of every approved or rejected change, with who decided and the before and
after text. Read it with `swarmctl log rules PROJECT`.

Rule changes waiting for review are listed in the weekly export under "Rules", so none is forgotten.

## Example: a marketing ad

```
# The marketing bot drafts an ad and asks for review.
swarmctl log add marketing --kind item --status draft "Spring sale ad, Facebook"
  -> item-41, version 1, draft
swarmctl log add marketing --kind item --ref item-41 --status needs-review

# Sam asks for changes, and says why. The feedback becomes pending learning-45.
swarmctl log add marketing --kind item --ref item-41 --status changes --by Sam --version 1
swarmctl log add marketing --kind feedback --ref item-41 --by Sam "Too salesy. Lead with the customer's problem."

# The bot re-drafts. This is version 2; the "changes" verdict on version 1 no longer counts.
swarmctl log add marketing --kind item --ref item-41 --status needs-review

# Sam approves version 2.
swarmctl log add marketing --kind item --ref item-41 --status approved --by Sam --version 2

# The bot proposes a rule from the learning (it gets an id, say 48). Sam approves it through the Chief of Staff.
swarmctl log add marketing --kind rule --after "Lead with the customer's problem, not the discount." --learning learning-45
swarmctl log add marketing --kind rule --status approved --proposal 48 --by Sam      (Chief of Staff or admin only)

# From now on every draft starts from this:
swarmctl log brief marketing
```

## The weekly review and export

**Export on demand.** `swarmctl log export PROJECT --days 7 --out file.md` writes a readable Markdown
file: current items, a rules summary, the Waiting on you summary for the project, then the log grouped
by day. `swarmctl log export --dir DIR` writes one log file and one rules file per project you can read.

**Weekly export.** If `SWARM_LOG_EXPORT_DIR` is set in `/etc/agent-swarm/swarm.env` (the example config
sets `/var/lib/agent-swarm/log-exports`), the backend writes, once per week, a folder per project with
`YYYY-Www.md` (the last 7 days) and `rules.md`. On a new install the first weekly export is written in
the following ISO week (the week you install in is marked done when the backend first starts). For an
export right away, use `swarmctl log export --dir DIR`.

**Optional git export.** For a project where you want to see changes as diffs, turn it on with
`swarmctl project set PROJECT --log-git`. Its export folder becomes a local git repo and each weekly
export is a commit, so `git log -p rules.md` shows every rule change. For a one-off, `swarmctl log
export --dir DIR --git` does the same in any folder. Pushing that repo somewhere is up to you.

**Agents can always read it.** Everything above is plain Markdown or JSON over the backend API and
`swarmctl`, with the bot's own token. There is no login-walled page.

## Keeping Waiting on you small

The list only works if it stays short. `swarmctl waiting list` (and `GET /waiting`) shows counts and age
at the top: how many are open and snoozed, the oldest in days, and how many have been open 14 days or
more. Each item shows its age. The weekly export lists those old items under "Consider dropping these",
and the goals meeting prompt asks the Chief of Staff to suggest drops. Dropping is a decision too, so it
is logged.

## Automatic entries

- A worker task that ends `done` or `failed` adds a `result` entry with its summary and links. A chain
  cycle adds one entry per cycle ([long-running-jobs.md](long-running-jobs.md)).
- Answering or dropping a Waiting on you item, or answering a task's question for the human, adds a
  `decision` entry with `by`, the answer, and the source (`waiting-list` or `answer`).

Automatic entries carry a hidden key, so a retried request never adds them twice. Tasks without a
project add no log entry.

## Who can read and write it

| Who | Add entries | Approve or reject rule changes | Read entries |
| --- | --- | --- | --- |
| You (admin token) | Any project | Yes | All projects |
| Chief of Staff | Any project | Yes | All projects |
| A project bot | Only projects it is bound to | No | Only projects it is bound to |
| A running worker task | Only its own task's project | No | No |
| The worker token | No | No | No |

A project bot is bound with `swarmctl project set PROJECT --bot BOT-ID`, which works for projects with
no repo too.

## The API

All calls use the caller's own token (`Authorization: Bearer`).

- `POST /log` with `project`, `kind`, `summary`, and for typed records `status`, `ref`, `by`, `answer`,
  `source`, `version`, `new_version`, `expires_days`, `after`, `learning` or `proposal`. Also `detail`,
  `links`, `task_id` and `dedupe_key` (same key, same entry). A running task sends no `project`. Adding
  feedback returns the pending learning it created as `learning`.
- `GET /log?project=NAME&kind=feedback,learning&ref=item-41&status=pending&since=UNIX_TIME&limit=20`
  returns entries oldest first.
- `GET /log/items?project=NAME` returns the current state of each item.
- `GET /log/rules?project=NAME` returns the standing rules, changes waiting for review, and the
  changelog. Add `format=md` for the rules doc.
- `GET /log/brief?project=NAME` returns what a bot reads before drafting, as Markdown.
- `GET /log/export?project=NAME&since=UNIX_TIME` returns the Markdown export.

The full permission table is in [backend.md](backend.md).

## How it is stored

One SQLite table, `log_entries`, in the backend database, included in the nightly backup. The 0.3.0
backend adds its columns on first start, so upgrading needs no manual step and old entries keep working.

## Without a worker

With Grok Bot only, there is no backend. Each project bot keeps the same log as a note in its memory,
one line per entry, plus a separate short rules note:

```
## Project log: marketing
- 2026-11-18 item-1 v1 needs-review: Spring sale ad, Facebook
- 2026-11-18 item-1 v1 changes (by Sam). feedback (Sam): Too salesy. Lead with the problem.
- 2026-11-19 item-1 v2 approved (by Sam)
- 2026-11-19 rule proposed: "Lead with the customer's problem, not the discount." Sam approved.
```

The bot asks you before changing its rules note, keeps it under 25 rules, and reads it plus the last
14 days of feedback before drafting. The nightly memory cleanup may summarize old activity, but never
drops feedback, decisions or rules.
