# Project brief: PROJECT NAME

Owner bot: BOT-ID. Last updated: DATE, by WHO. Anything unknown is marked "TBD (ask the owner)".
Every bot reads its brief at the start of each conversation. Keep it short and current.

## What this project is
Two or three sentences: what it sells or does, for whom, and where it stands today.

## Goals right now
This week's 1 to 3 goals from the weekly goals meeting, each with its "Done means" line, as confirmed by
the owner (the Chief of Staff sends them; see templates/goals.md). Below them, in a sentence or two,
what the owner wants in the next few months.

Daily work heartbeat (prompt 7 in templates/routine-prompts.md): once a day, check each goal is moving.
On track or fixable: keep working and send nothing. Send small questions to the Chief of Staff. File
one Waiting on you item with a recommendation for anything the owner must decide.

## Messages to managers
A message to a manager bot (a project bot or the Chief of Staff) is five lines at most, with at most
one open question and at most one link, each only when there is one. Tag every claim **verified**
(with the evidence: a command and its result, a test result, an artifact path or a link) or
**asserted** (nobody checked it). Verify your own claims before sending, so the manager does not
have to redo the check.
Send your questions and status to the Chief of Staff in this form.

## People and roles
Who is involved, what they own, how to reach them. Include other bots that touch this project.

## Repos and environments
Repos (owner/name and default branch), hosting, and each environment with its address (production,
staging). How deploys happen. Who merges (default: a human). The project must list this bot
(`swarmctl project set PROJECT --repo OWNER/REPO --bot BOT-ID`) before it can queue repo work.

## Channels to watch
Inboxes, chat channels, support queues, and how often to check them.

## Connectors and apps
Connectors the bot uses and what each is for. The bot's own results inbox (trigger URL registered
with the backend, see templates/bot-setup-checklist.md) and the date it was last tested.

## Where work runs
- The bot itself: coordination, chat, connector work, quick lookups.
- The worker: all coding of any size, anything over about 15 minutes, design, deep research, bulk
  scraping, and anything needing a high-end or specific model. See docs/which-tool-when.md.
- Model routing table (copy of templates/model-routing.md, with any changes for this project):

  | Job | Provider and model |
  | --- | --- |
  | ... | ... |

## Tests
How tests run for each repo: the command for one file, and for the full suite. Full suites run as
script tasks (`swarmctl enqueue --command "..."`), not inside a model session: no tokens, reliable, and
they run next to model work. If tests are heavy, say where they run instead (CI or a cloud sandbox,
proven once against CI). Never edit tests just to make them pass somewhere else. Anything that cannot
run on the worker (needs real services or secrets) and where it runs instead.

## Permissions
Defaults are in docs/approvals.md. Changes for this project:
- Code: draft pull requests on agent/ branches (default). Merges by: a human.
- Production data: none / read-only through WHAT.
- Always needs the owner's yes: messages that go outside, spending money, deleting things, LIST MORE.
- Never touches: ONE LINE. Paste it into every task brief for work near any of it.

There is exactly one "Never touches" line. On day one the bot repeats it back to the owner in one
message ("I will never touch: ...") so the owner can correct it.

## Routines and their checks
Each recurring job: when it runs and its cheap first check. No change means no message or log entry.

## Docs, memory and logging
Memory repo and project folder: ORG/MEMORY-REPO, PROJECT/. Queue docs-only updates with `--push-main`
on the dedicated memory project. Keep code in draft PRs. No secrets or personal data.

Log a real change: `swarmctl activity-log add PROJECT --kind KIND "SUMMARY" --key EVENT-ID`.
Use `activity`, `shipped`, `blocked`, or `decision --decider bot`. Send actual owner decisions to the Chief of Staff
to record. Add `--lasting` for a change in how the project works. No change, no entry.
Read recent lines: `swarmctl activity-log list PROJECT`.
Nightly export updates `log/YYYY-MM.md` and the marked block of `decisions.md`. Per date:
`Owner decided`, `Delegated by Chief of Staff`, `Project bot calls`. Never edit old decisions.

Needs owner (the Chief of Staff must never decide these): LIST PROJECT-SPECIFIC LIMITS.
Delegation: default / off / EXACT OWNER-APPROVED EXCEPTIONS. See docs/delegated-decisions.md.

The detailed project log still holds items, feedback, learnings and rules. Task results and owner
answers go there automatically. Use docs/project-log.md.
**Before drafting new work, read the brief: `swarmctl log brief PROJECT`** (the standing rules plus the
last 14 days of feedback) and follow it.
Without a worker, use short memory notes instead. Other notes and task list: LOCATION.

## Open decisions
Questions waiting on the owner, each with a recommended answer. File each once on Waiting on you.
