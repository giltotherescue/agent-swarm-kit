# Usage and cost

This page is about keeping the swarm cheap. Every time a bot wakes up it spends a full turn of tokens, and every worker run uses part of your subscription limits. Most waste comes from bots that talk when nothing changed and from hubs that relay messages. The habits below keep spend low without slowing real work.

## Subscriptions, not API billing

By default the worker runs Claude Code and Codex CLI (command line interface) logged in with your own Claude and ChatGPT subscriptions. You pay a flat monthly price and work within its usage limits.

Do not put API (application programming interface) keys on the worker unless you really mean to pay per token. If a CLI finds an API key in its environment it may use it instead of your subscription, and the bill can grow fast. If you do want API billing for some jobs, write that down in the project brief with a monthly cap.

Heavy users can spread work over several subscriptions with a proxy; see CLIProxyAPI in [optional services](optional-services.md).

## Bots stay quiet when nothing changed

- **No "nothing to report" messages.** A passing check needs no message. Silence means fine.
- **Idle until there is work.** Bots do not run "anything new?" loops in chat. Results arrive by trigger (see [results routing](results-routing.md)), so the bot wakes only when one of its tasks changed.
- **Speak only when it adds something.** In group rooms, a bot speaks only when it adds something essential.
- **Batch small updates** into one message, and batch questions for the human into the morning decision batch.

## Routines check cheaply first

A routine should do a cheap check, and only think hard when something changed.

```
swarmctl list --owner website-bot --since 1767600000
```

or the same as a web request: `GET /tasks?owner=website-bot&updated_since=1767600000`. It returns only tasks updated after that time (a Unix timestamp, seconds since 1970). Remember the time of your last check. If the list is empty, stop there: no summary, no message.

Other cheap checks:

- `swarmctl status` gives one screen: counts, running tasks, waiting tasks, items due.
- `swarmctl waiting list` shows only open items.

See [routine prompts](../templates/routine-prompts.md) for routines written this way.

## No hub bot

The backend posts each result straight to the owning bot. The Chief of Staff does not relay results, so you do not pay for two bot turns per result. The Chief of Staff only wakes for ownerless tasks, questions for the human, alerts and fallbacks.

Notices are sent only when a task changes to done, failed, cancelled or waiting. Nothing is sent for "queued" or "running", and the same event is never sent twice.

## Pick the right model

- Use the smallest model that does the job well. Bulk lookups and scraping go to `haiku` or a small Codex model. See [model routing](model-routing.md).
- Save the strongest model for strategy, important copy and design, and hard bugs.
- Use a second model to review important work instead of running the strongest model twice.

## No model for scripts

Test suites, builds, data exports and other fixed commands do not need a model watching them. Queue them as script tasks:

```
swarmctl enqueue -r you/app --command "npm ci && npm test" --owner app-bot - <<< "Run the full test suite"
```

The worker runs the command, uploads `artifacts/script.log`, and reports the exit code. Zero tokens.

Tell coding agents to run only the tests for the files they touched, and leave the full suite to a script task or to your CI (continuous integration, for example GitHub Actions).

## Limits and fallback

- `SWARM_MAX_PARALLEL` (default 2) caps how many tasks run at once. More parallel tasks burn your subscription limits faster and need a bigger server. See [sizing and sandboxes](sizing-and-sandboxes.md).
- When Claude hits a usage limit, the worker retries on `SWARM_FALLBACK_PROVIDER` (Codex by default). Without a fallback, the task waits and retries later (one hour, then doubling up to a day).
- `SWARM_MAX_ATTEMPTS` (default 5) caps how many times a task resumes on its own before it asks its owner. This stops a confused task from looping all night.
- `SWARM_TASK_TIMEOUT` (default 14400 seconds, 4 hours) stops a single run that never ends.
- The worker does not claim new work when the box is busy (`SWARM_MAX_LOAD_PER_CPU`, `SWARM_MIN_FREE_MEM_MB`).

All of these live in the [settings file](../config/swarm.env.example).

## Duplicates

Queuing the same brief twice while the first copy is still queued, running or waiting returns the first task instead of starting a second run. Use `--key` with a stable idempotency key when a bot might retry, and `--allow-duplicate` only when you really want two runs.

## A monthly look

Once a month, look at what ran: `swarmctl list --limit 200`. Ask: which tasks failed or looped, which bot queued the most, and which jobs could have been a script or a smaller model.
