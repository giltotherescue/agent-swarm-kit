# Changelog

All notable changes to this kit. Versions follow semantic versioning (MAJOR.MINOR.PATCH):
MAJOR breaks something you set up, MINOR adds something, PATCH fixes something.
See docs/updating-the-kit.md for how to update.

## 0.3.0 (2026-11-18)

Easier to start and easier to keep going. The README is friendlier, your Grok Bot can now set the whole
kit up for you, bots get weekly goals and a daily check, and every project keeps a log. The backend
changes (the project log and long-running jobs) are covered by automated tests; 0.3.0 has not had its own fresh-server install
run yet (0.2.1 has, see docs/worker-setup.md "Tested on").
Upgrade steps: docs/updating-the-kit.md ("Upgrading from 0.2.1 to 0.3.0").

Added:
- `SETUP-WITH-GROKBOT.md`: tell your Grok Bot "set me up with this repo" and it walks you through
  everything, one question at a time. Every choice (worker or not, where and how big, model logins,
  backend, project bots, connectors, treg, Executor, approvals, goals meeting time) is explained in plain
  words with a recommended default. It never asks for secrets in chat and confirms anything that costs
  money.
- Project log: one append-only, typed history per project in the backend (new `log_entries` table,
  created automatically on upgrade). Docs: docs/project-log.md, with a marketing example.
  - Records: items (deliverables with status draft, needs-review, approved, changes or rejected, and a
    version), decisions (`by`, answer, source, date), feedback (`by` required), learnings (pending or
    standing), rule changes, activity and results. Items, learnings and rules have a stable `ref`; a
    change is a new entry, never an edit.
  - A new version of an item voids older verdicts; a verdict on an old version is refused.
  - Feedback becomes a pending learning automatically. Rule changes are proposals that store the text
    before and after, and only the owner (admin or Chief of Staff) approves them. Each project has one
    short rules doc (at most 25 rules, `SWARM_RULES_CAP`) with a changelog.
  - Bots read the standing rules plus a 14-day feedback digest before drafting: `swarmctl log brief`,
    `GET /log/brief`.
  - Bad records are refused: unknown kind, status or bot, missing `by`, unknown `ref`.
  - Throwaway items take `expires_days`; once expired they leave every view and are deleted.
  - `swarmctl log add|list|export|items|rules|brief`; `POST /log`, `GET /log`, `/log/items`,
    `/log/rules`, `/log/brief`, `/log/export`. Plain Markdown and JSON, readable by agents with their
    own token.
  - Weekly Markdown export per project to `SWARM_LOG_EXPORT_DIR`, and an optional local git repo per
    project (`swarmctl project set NAME --log-git`) so changes show as diffs.
  - Automatic entries: finished and failed tasks add a `result`; answering or dropping a Waiting on you
    item, or answering a task's question for the human, adds a `decision`. Repeated requests never log
    twice. Project bots read and write only projects they are bound to; a running task writes only to
    its own project; the worker token has no access.
- Waiting on you shows counts and age: open, snoozed, oldest in days, and how many have been open 14
  days or more. The weekly export lists those old items under "Consider dropping these", and the goals
  meeting prompt asks the Chief of Staff to suggest drops.
- Long-running jobs (docs/long-running-jobs.md):
  - Chains, the recommended way: `swarmctl enqueue --chain --max-cycles N`. Each cycle is a short, fresh
    run that writes `CHECKPOINT.md`; the backend queues the next cycle as a follow-up with the checkpoint
    attached. One project log entry per cycle; the owner hears only when the chain ends. Cancelling any
    cycle cancels the whole chain.
  - Per-task time limit: `--timeout-minutes`, up to the admin maximum `SWARM_TASK_TIMEOUT_MAX`
    (default one day), and a `long-running` task kind that uses that maximum and keeps a checkpoint in
    its folder. The task token and the watchdog follow the task's own limit.
- Weekly goals meeting (Chief of Staff) and daily work heartbeat (each project bot): prompts 6 to 8 in
  templates/routine-prompts.md, a goals file template (templates/goals.md), and
  docs/goals-and-heartbeat.md, which also explains how this differs from the worker's 30 second machine
  heartbeat.
- docs/which-tool-when.md: Grok Bot connectors, treg (paid data, plus private tools like Search Console
  and Google Ads), the worker (all coding of any size, long jobs, design, deep research, bulk scraping,
  specific models), Executor (later), with examples and when a connector beats treg for the same
  account.
- Four illustrated pictures in docs/images/ for the README.

Changed:
- README: two-sentence opening, the four pictures, "Set it up with your Grok Bot" as the main path, a
  short "which tool when" table, and manual setup as the second path.
- All Mermaid flow charts moved out of the README into docs/how-it-works-technical.md, plus a new chart
  for the project log and the weekly loop.
- Chief of Staff and project bot templates: goals meeting, work heartbeat, project log and the feedback
  loop (bots read recent feedback and learnings before drafting), setup choices, and "all coding goes to
  the worker". The model routing template says the same.
- Docs say plainly that the worker's 30 second machine heartbeat is a network call with no model, so it
  uses no tokens.
- README "which tool when" has a row for chains, linking docs/long-running-jobs.md.
- Logging feedback or a decision through the API now needs `by`. Routine prompt 8 shows the new call.
- Backend, worker, `agent-exec` and `swarmctl` report version 0.3.0.

## 0.2.1 (2026-11-18)

Fix release from the first install test of 0.2.0 on a fresh Ubuntu 24.04 server. That test found that
no task could finish on a new install, that Claude Code did not install, and that the lockdown step could
lock out a server that has only root. 0.2.1 fixes these. **0.2.1 passed a clean install on a fresh Ubuntu
24.04 server on 2026-11-18** (setup, smoke test, admin user, firewall and SSH hardening, lockdown refusal
checks, task and cleanup flow). Still not tested on a real server: joining Tailscale (so the real lockdown,
Tailscale Serve and Tailscale SSH), Claude and Codex login, running a model task, GitHub login and repo
pushes, and bot notify delivery.
Upgrade steps: docs/updating-the-kit.md ("Upgrading from 0.2.0 to 0.2.1").

Why 0.2.1 and not a changed 0.2.0: 0.2.0 was already installed on a test server, and a version that
changes behavior needs its own number so `swarmctl status` and `/health` show which code is running.

Fixed (numbers are the install-test defects):

1. Claude Code now installs. The setup script ran `sudo -iu agent bash -c '...'`, and `sudo -i` escapes the
   command line, so `$f` and `$?` were lost and curl failed. Agent commands now run as
   `sudo -u agent -H bash -lc '...'`. A failed install is reported as "Claude Code did not install" with
   exit code 3, instead of the misleading "claude does not run as agent". The smoke test had the same
   pattern and is fixed too.
2. and 3. Tasks no longer fail while being prepared. The runner and `agent-exec` used `chmod` on new task
   folders in the shared inbox and outbox. Linux clears the setgid bit (the bit that makes new files take
   the folder's group) when someone outside the folder's group runs chmod, so files got the wrong group and
   the other user could not read them. The task folders are no longer chmodded, and both sides now check
   the group and setgid bit after making a folder.
4. `--lockdown` refuses unless a usable non-root admin exists: a normal user with a login shell, sudo
   (group `sudo` or a sudo rule), a password or a passwordless sudo rule, and a non-empty
   `authorized_keys` only that user can change. `--admin-user NAME` makes one: adds it to `sudo`, copies
   root's plain key lines, and asks for a sudo password when run in a terminal. The lockdown message and
   the Tailscale SSH sample now name that user. Lockdown also refuses, with a hint, when Tailscale is not
   installed or not joined.
5. Install-time hardening: the firewall (ufw) is turned on at install, allowing SSH from anywhere (so you
   cannot be locked out) plus SSH over Tailscale and Tailscale's port. Root and password login over SSH are
   turned off at install only when an admin user is confirmed (the script was run through sudo as that
   user, or you answered "yes" after testing `ssh` and `sudo -v` as that user). The end of the run says
   what is and is not hardened. `--harden` runs just this step later.
6. `swarmctl status` no longer warns "newest database backup is older than 36 hours" on a new install. The
   backend records when its database was made and does not warn about a missing backup in the first 36
   hours, and setup makes a first backup. A missing backup after that gives "There is no database backup
   yet".
7. apt waits for other package installs instead of failing at once. Before each apt call the script waits,
   up to 10 minutes with a progress line every 30 seconds, until the server's first-boot setup (cloud-init,
   checked with `cloud-init status --wait` and a time limit) has finished and no process holds the apt or
   dpkg locks (`/var/lib/apt/lists/lock`, `/var/lib/dpkg/lock-frontend`, `/var/lib/dpkg/lock`,
   `/var/cache/apt/archives/lock`, checked with fuser, lsof or /proc). If apt still fails on a lock it runs
   again, up to 4 times, 20 seconds apart, and then stops with a clear message. `apt-get update` also
   retries network errors. The dpkg lock wait inside apt (`DPkg::Lock::Timeout=120`, also written to
   `/etc/apt/apt.conf.d/90-agent-swarm-lock-wait`) still covers the Node.js and Tailscale installers. The
   first version of this fix only set `DPkg::Lock::Timeout`, which does not cover the lock that
   `apt-get update` takes, so the 0.2.1 retest failed at once on a new DigitalOcean server while
   cloud-init was installing droplet-agent.
8. Quieter output: apt, Node.js, Tailscale, Claude Code and Codex installer output goes to
   `/var/log/agent-swarm-setup.log` (mode 600). The screen keeps the `==>` lines and shows the last log
   lines if a step fails. The "apt does not have a stable CLI interface" warning now goes only to the log.
9. Each run removes old release folders, keeping only `current` and `previous`.
10. `--tailscale later` installs Tailscale without joining, so nothing waits for a browser; `--lockdown`
    works once you join with `sudo tailscale up --ssh`. The default join now gives up after 15 minutes
    instead of waiting forever, and a run with no terminal and no auth key joins later on its own.
    `--no-tailscale` (also `--tailscale off`) still works and the docs say what it costs.

Also (from the 0.2.1 retest):

- Outbox task folders no longer pile up. `agent-exec` has a new narrow action, `cleanup TASK`: it removes
  only `/srv/agent-swarm/outbox/TASK`, checks the task id, refuses links and any path outside the outbox,
  and runs as the agent through the same single sudo rule (the rule still allows only `agent-exec`). The
  runner calls it only after the results were uploaded and the backend acknowledged the finish; otherwise
  the folder stays. The runner removes its own inbox folder as before. The daily cleanup now also backs up
  and removes outbox folders of tasks that finished more than `SWARM_KEEP_DAYS` (7) days ago.
- `agent-exec` task and folder names must match in full: a name with a trailing newline was accepted
  before.

Also:

- The smoke test shows "public SSH is still open" as a NOTE, not a WARN, so a new box ends with
  0 failed and 2 warnings (GitHub login, Tailscale).
- Docs: honest "Tested on" notes, how to get the kit onto a server without your own GitHub login (release
  file, `git archive` plus `scp`, or a read-only deploy key or token), a "Make your admin user" step, the
  Tailscale pause and how to skip it, the expected `swarmctl status` warning and smoke-test output on a new
  box, why `sudo -iu agent` is needed for the model CLIs, and a "Setup problems" section in
  troubleshooting.
- Tests: 78 before, 124 now. New: the inbox and outbox handoff run as two real OS users in a throwaway
  container (catches defects 2 and 3), the setup script's lockdown refusal on a root-only box,
  `--admin-user`, `--harden`, release pruning, the Tailscale modes, the Claude Code install path, the
  apt lock option, and the backup warning. Tests that need root or two users run in Docker and skip
  with a loud message when Docker is not there (`SWARM_REQUIRE_USER_TESTS=1` makes that a failure).

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
