# Changelog

All notable changes to this kit. Versions follow semantic versioning (MAJOR.MINOR.PATCH):
MAJOR breaks something you set up, MINOR adds something, PATCH fixes something.
See docs/updating-the-kit.md for how to update.

## 0.2.0 (2026-11-18)

Security and reliability release, from two reviews of 0.1.0. **Not yet run end to end on a fresh server.**
The setup script passes shellcheck and its parts are tested one by one; run it on a server you can rebuild,
then run `scripts/smoke-test.sh`. Upgrade steps: docs/updating-the-kit.md ("Upgrading from 0.1.0 to 0.2.0").

What changed:

- Each task now gets its own task token when it is claimed. It works only for that task and that run, and
  stops when the task stops running, when its lease runs out, or after 8 hours. The worker token can only
  take tasks, read task states and send alerts. It can no longer read any task, report or file.
- Every run has a claim id. A runner that lost its task (cancelled, answered, given to another run) gets
  HTTP 409 and can never finish or publish it. Repeating a finish gives back the first answer. Cancel is
  immediate, and the runner stops the agent and publishes nothing.
- Three users on the worker: `swarm` (backend), `swarm-runner` (runner, worker token, GitHub login) and
  `agent` (model CLIs, no tokens, no GitHub login). Code moves between them as git bundles. The runner
  checks new lines for secrets and pushes only to the task's own branch.
- Bots see only their own things: duplicate checks, Waiting on you items, projects and follow-ups are all
  per bot. Repo tasks need a project that lists the bot (`swarmctl project set NAME --repo ... --bot ...`).
- A route permission table lives in the code, is printed by `swarm_backend.py --permissions`, and is in
  docs/backend.md. A test keeps them the same.
- Bot trigger keys move out of the database into one file per bot (`/etc/agent-swarm/bot-keys/`). The
  first start moves old keys and wipes them from the database. Changing a bot's URL or key never sends the
  new key to the old URL.
- Tailscale Serve, with Tailscale on the bot's computer, is now the recommended way for bots to reach the
  backend, with a sample access rule set. Funnel is option 2, with warnings.
- The backend checks the token before reading a request, rejects bad lengths, has timeouts, a connection
  cap and a limit on failed logins. Notify URLs must be public https addresses (checked again at every
  delivery) and redirects are not followed.
- Delivery is described honestly as "at least once". A receiver answering 409 or `{"duplicate": true}`
  counts as delivered. Notices carry `event` (what happened) and `reason` (why this bot got it).
- `swarmctl enqueue --follow-up TASK_ID` continues a finished task, including unpushed work saved as a
  bundle. Follow-ups of one parent run one at a time (`held_behind`).
- A task whose saved folder is gone starts fresh with the full brief and every answer, and says so. If
  unpushed work was lost, it asks first.
- Waiting tasks get one reminder after 12 hours and go on Waiting on you after 24. A watchdog alerts when
  no runner checks in or a task runs far past its time limit. Old files are deleted after 90 days, and each
  bot can have at most 100 open tasks.
- Review tasks: if findings lack a `path:line` reference, the worker asks once for the format to be fixed.
- Script tasks have the same time limit as model runs. A failed or timed-out agent is never marked done.
- Pushing to the main branch is off unless a project allows it (`--allow-push-main`) and the task asks for
  it (`--push-main`). Default and common protected branch names are refused otherwise.
- `with-secrets NAME -- command` gives one command a shared key, with clear refusals and no printing.
- Cleanup saves untracked and binary files before deleting a folder, skips repos a live git worktree points
  into, and deletes as the `agent` user only after checking the saved copy.
- `swarmctl drain`, `undrain` and `restart-when-idle` restart the worker without killing tasks. Upgrades
  drain and wait first, install into a new release folder, and roll back if the backend does not start.
- Setup: `--auth-key FILE` and `--tags`, checks the `agent` account has no sudo, SSH hardening that wins
  over cloud defaults and is verified, limits for all tasks together, honest exit code 3 when something is
  left to do. New `scripts/smoke-test.sh` and `scripts/restore-backup.sh` (checks the backup before
  touching anything). Backups are checked, timestamped and private.
- `swarmctl` works through the installed link, `project set|list|show` (the old form still works), token
  files are written with mode 600 and never through a link, and `enqueue` warns about local file paths in a
  brief.
- `check-secrets.sh` prints the file, line and pattern number, never the secret, and a scan error is exit 2.
- README rewritten for non-technical readers, with product-language charts. Technical charts moved to
  docs/how-it-works-technical.md.
- Tests: 13 before, now a full suite covering tokens, permissions, delivery, the runner with a real git
  repo, and the tools.

Open items (not fixed in this release):

- All tasks share the `agent` user: the separation keeps tasks out of each other's backend data, but not
  out of each other's files or the shared CLI logins. Per-task users or containers are a bigger change.
- Outgoing network access from tasks is not limited. It needs a firewall design that does not break the
  model CLIs.
- The backend reads its admin and worker tokens from files at start; root can read them. Expected on a
  single-owner box.
- Development mode (`SWARM_AGENT_USER` empty) runs the runner and agent as one user. Used by the tests only.
- The setup script, systemd units and Tailscale steps are not verified on a real fresh server yet.
- Cloudflare Workers backend: design only, not built.

## 0.1.0 (2026-11-18)

First version.

- On-worker backend (`server/swarm_backend.py`): tasks, owners, questions and answers, messages to running
  tasks, reports and artifacts, bot registry with per-bot tokens, Waiting on you list, status command,
  direct results routing to the owning bot with Chief of Staff fallback, idempotent delivery.
  Python standard library and SQLite only.
- Worker (`server/swarm_worker.py`, `server/run-agent`): runs Claude Code or Codex on your own subscriptions,
  resumes the same session after an answer, falls back to the other provider on usage limits, script tasks
  with no model, pushes `agent/<task-id>` and opens draft pull requests when asked.
- Command line (`server/swarmctl`).
- Daily cleanup of finished task folders, nightly database backup, hourly disk alert, systemd units.
- `scripts/setup-worker.sh` for a fresh Ubuntu LTS server, with a separate lockdown step.
- Docs for both paths (Grok Bot only, and Grok Bot plus worker) and templates for bots and routines.
