# Model routing (edit me)

Each bot keeps its own copy of this table in its project brief. It is a default, not a rule.
Go off it when you have a reason, and say why. Name the model every time you queue work.

Model names change often. Use the CLI short names (`opus`, `sonnet`, `haiku` for Claude Code, or `default`
to let a CLI pick its own default). Check the current list with `claude` then `/model`, and with `codex` then `/model`.

| Job | Where it runs | Provider and model (`-P` / `-m`) |
| --- | --- | --- |
| Strategy, decisions, top-quality design and copy | worker | `-P claude -m opus` |
| Second opinion or review of work another model did | worker | the other provider: `-P codex -m default` |
| Everyday coding | worker | `-P codex -m default` or `-P claude -m sonnet` |
| Frontend and web design | worker | `-P claude -m sonnet` |
| Cheap research, scraping, bulk lookups | worker | `-P claude -m haiku` or a small Codex model |
| Running a full test suite | worker, no model | `--command "npm test"` (script task) |
| Inbox triage, support queues, stats, scheduling, coordination | the bot itself | none |
| Quick lookups that take under a few minutes | the bot itself | none |

Send to the worker: anything long (over about 15 minutes), anything in a repo, anything that needs a
high-end model, and anything a person asked a specific model to do.
Keep in the bot: short coordination, reading and replying in chat, connector work (email, calendar, CRM).

Overrides for this project: <none, or list them with the reason>
