# Model routing

This page helps each bot pick the right model for a job. The bot itself handles coordination. Real work goes to the worker, where you can choose Claude Code or Codex CLI (command line interface) and a specific model. Each bot keeps its own short routing table in its brief, so you can change a model in one place.

## The default table

Copy [the model routing template](../templates/model-routing.md) into each bot's brief and edit it. Start here:

| Job | Provider (`-P`) | Model (`-m`) |
| --- | --- | --- |
| Strategy, decisions, top-quality design and copy | `claude` | `opus` |
| Second opinion or cross-check on the above | `codex` | your strongest Codex model |
| Daily coding and development | `codex` or `claude` | `default` (Codex) or `sonnet` |
| Frontend and web design | `claude` | `sonnet` |
| Cheap research, scraping, bulk lookups | `claude` or `codex` | `haiku` or a small Codex model |
| Full test suites, builds, data exports | none (script task, `--command`) | no model |
| Images (ad graphics, illustrations, mockups) | `codex` | `default`, using Codex CLI's built-in image generation |
| Support queues, stats, coordination | none | the Grok Bot itself, no task |

A good habit: when a strong model writes something important, have the other provider review it. Different models miss different things. See [the review prompt](../templates/review-prompt.md) and `swarmctl review`.

## Full test suites are script tasks

A full test suite needs no model watching it. Run it as a script task: it is cheap (no tokens), reliable (same command every time), and it can run next to model work.

```
swarmctl enqueue -r you/site --command "npm ci && npm test" --owner website-bot - <<< "Run the full test suite"
```

Coding agents run only the tests for the files they touched. The full suite is a separate script task, or your CI (continuous integration, for example GitHub Actions).

If a project's tests are heavy (big databases, browsers, long runs), run them in a cloud sandbox from the worker: the script task's command starts the sandbox, runs the suite there and prints the result. Prove it once against CI (same commit, same result) before you trust it. Never edit the project's tests to make them pass in the sandbox; fix the sandbox setup instead. See [sizing and sandboxes](sizing-and-sandboxes.md).

## Images

Image work (ad graphics, illustrations, mockups, edits) can go through Codex CLI's built-in image generation on the worker. It runs on your ChatGPT subscription, so no separate image API key is needed.

## Model names change

New models come out every few months. To avoid editing your table each time:

- Claude Code accepts short aliases: `opus`, `sonnet`, `haiku`. They point to the newest model of that tier.
- `default` means "let the CLI use its own default". For Codex this follows whatever model your Codex install is set to.
- Exact model IDs also work. As of November 2026, examples are `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-haiku-5-5`. Check the provider's docs for current Codex model names.

Use an exact ID only when you need a fixed model, for example to compare runs.

## How to choose a model for one task

```
swarmctl enqueue -P claude -m opus --owner website-bot --report-only brief.md
swarmctl enqueue -P codex -m default -r you/site --pr --owner website-bot brief.md
```

If a task does not name one, the backend uses, in order:

1. the project's default (set with `swarmctl project set`),
2. `SWARM_DEFAULT_PROVIDER` and `SWARM_DEFAULT_MODEL` from the [settings file](../config/swarm.env.example) (out of the box: `claude` and `default`).

## Project defaults

Give each project a sensible default so bots do not have to repeat it:

```
swarmctl project set website --repo you/site --provider claude --model sonnet
swarmctl project set data-jobs --provider codex --model default --no-push
```

`--no-push` stops tasks in that project from pushing branches or opening PRs (pull requests). `--bot website-bot` lets that bot queue repo work for the project. The older form `swarmctl project website ...` (without `set`) still works.

## When a provider hits its limit

Subscriptions have usage limits. When a run hits one, the worker:

1. Retries the same task on `SWARM_FALLBACK_PROVIDER` with `SWARM_FALLBACK_MODEL` (out of the box: `codex` with `default`), starting fresh because sessions cannot move between providers.
2. If no fallback is set, or the fallback is the same provider, the task goes back in the queue and waits: one hour after the first limit, doubling each time up to one day.

When everything is out of quota, queue new work on the provider that still has room, and let the rest wait.

## Name the model

Say which model you used whenever you launch work, both in the task and when you report back. The task record keeps `provider` and `model`, and `swarmctl enqueue` prints them. If you went off the table, say why in one line.

## Overrides per project

A project brief may override the table. Write the change and the reason, for example "copy for this brand always uses `opus`, because tone matters more than cost here". If there is no change, write "none".
