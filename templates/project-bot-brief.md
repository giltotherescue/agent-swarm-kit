# Project brief: PROJECT NAME

Owner bot: BOT-ID. Last updated: DATE, by WHO. Anything unknown is marked "TBD (ask the owner)".
Every bot reads its brief at the start of each conversation. Keep it short and current.

## What this project is
Two or three sentences: what it sells or does, for whom, and where it stands today.

## Goals right now
What the owner wants in the next few weeks and how they will judge success. Plain sentences.

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
- The worker: anything long, anything in a repo, anything needing a high-end or specific model.
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
Each recurring job: when it runs, what it does, and its pass/fail check. A passing run sends no message.

## Memory and decisions
Where this project's notes, decisions log and task list live. Only record decisions the owner made.

## Open decisions
Questions waiting on the owner, each with a recommended answer. Also filed on the Waiting on you list.
