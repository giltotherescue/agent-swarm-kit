# Updating the kit, and giving improvements back

This doc explains how the kit changes over time, how you pull new versions onto your worker without losing your settings, and how you or your bots send improvements back. The kit gets better as more people run it.

## Versions

- The current version is in the `VERSION` file and at the top of `CHANGELOG.md`.
- Versions follow semver (semantic versioning): `MAJOR.MINOR.PATCH`.
  - PATCH (0.1.0 to 0.1.1): fixes. Safe to pull any time.
  - MINOR (0.1.x to 0.2.0): new features. Nothing you rely on breaks.
  - MAJOR (0.x to 1.0): something changes in a way you must act on. The changelog says exactly what.
- Every change gets a line in `CHANGELOG.md` under its version, in plain words, with any action you need to take.

## Getting access

The repo is private. To use it you need either:

- to be added as a collaborator by the owner, then clone it, or
- your own private fork (copy) of it, which you keep in step with the original.

Do not put your own GitHub login on the worker just to fetch the kit. Use one of these instead:

- Copy it from your laptop: `git archive --format=tar.gz -o agent-swarm-kit.tar.gz HEAD`, then `scp` the file to the server and unpack it (exact steps in [worker-setup.md](worker-setup.md), step 2). Nothing on the server can read your repos.
- A **deploy key**: an SSH key made on the server (`ssh-keygen -t ed25519`) whose public half you add in the kit repo's settings under "Deploy keys", with "Allow write access" left off. It can read only this one repo.
- A **fine-grained token** with read-only "Contents" access to the kit repo only, used just for `git clone` and `git pull`.

The `swarm-runner` user's GitHub login (for task repos) is separate; it does not need access to the kit repo.

## Where your own settings live

Keep your changes out of the kit's files, so updates never clash with them:

- Settings: `/etc/agent-swarm/swarm.env`. The setup script never overwrites it.
- Tokens and keys: `/etc/agent-swarm/*.token`, `/etc/agent-swarm/bot-keys/` and `/var/lib/swarm-runner/worker.token`. Never in the repo.
- Data: `/var/lib/agent-swarm/`.
- Bot briefs, routines and model routing tables live with your bots. Copy them from `templates/` once, then edit your copies.

If you do change kit code, keep it on your own git branch (for example `local`) and rebase it on each update.

## How versions are installed

Each install goes into its own folder, `/opt/agent-swarm-kit/releases/VERSION-TIME`. Two links point at releases:

- `/opt/agent-swarm-kit/current` is the version that runs. The services start from it.
- `/opt/agent-swarm-kit/previous` is the one before, kept for rollback.

Since 0.2.1 the setup script removes all other release folders at the end of each run, so only these two are kept.

Switching versions means moving the `current` link, which happens in one step. If the new backend does not start, the setup script moves `current` back to `previous` by itself.

## Pulling an update

1. Read the new entries in `CHANGELOG.md` first.
2. Back up the database:

   ```bash
   sudo systemctl start swarm-backup.service
   sudo ls -l /var/lib/agent-swarm/backups
   ```

3. Pull the new version into your clone (the one you ran setup from, not `/opt`):

   ```bash
   cd ~/agent-swarm-kit
   git pull
   cat VERSION
   ```

4. Re-run the setup script. It keeps your settings and tokens. Before switching it **drains** the worker (stops it taking new tasks) and waits for running tasks to end, up to `--max-wait` seconds (default an hour). If tasks are still running then, it stops and changes nothing; run it again later.

   ```bash
   sudo bash scripts/setup-worker.sh
   sudo bash /opt/agent-swarm-kit/current/scripts/smoke-test.sh
   ```

5. Compare your `swarm.env` with the new example and add any new settings you want:

   ```bash
   diff /etc/agent-swarm/swarm.env /opt/agent-swarm-kit/current/config/swarm.env.example
   ```

New tables and columns are added on their own when the backend starts. If a release changes existing data, its changelog entry says exactly what happens.

## Restarting without killing tasks

Restarting the worker stops running tasks. They come back after their lease runs out (about 10 minutes) and resume, but that wastes time and tokens. Instead:

```bash
sudo swarmctl restart-when-idle            # drain, wait until no task runs, restart the worker, undrain
sudo swarmctl restart-when-idle --also swarm-backend --max-wait 7200
```

`scripts/restart-worker-when-idle.sh` does the same. To pause the worker by hand: `sudo swarmctl drain`, and `sudo swarmctl undrain` to carry on. Queued tasks wait; nothing is lost.

## Rolling back

If a new version misbehaves:

```bash
sudo swarmctl drain
sudo ln -sfn "$(readlink -f /opt/agent-swarm-kit/previous)" /opt/agent-swarm-kit/current
sudo systemctl restart swarm-backend swarm-worker
sudo swarmctl undrain
```

The database stays as the newer version left it. Newer versions only add tables and columns, so an older version keeps working with it. If the changelog says otherwise for a release, restore the backup from step 2 with `scripts/restore-backup.sh` ([backend.md](backend.md#backups-and-restore)).

## Upgrading to 0.8.1

Run the setup script as usual; it restarts the supervisors. No database change. Orchestrators now receive a queued
message during a long busy turn, 90 seconds after it was queued (15 if urgent), instead of waiting for an idle pane.
Nothing is typed onto a menu or a draft any more, idle or busy, including the stop wrap-up: a session showing one
when it is stopped gets no wrap-up and ends after the stop grace time (`SWARM_ORCH_STOP_GRACE`, 120 s). The guard
fails closed: a pane without a readable, empty Claude Code input box gets nothing, and a message held back for 2
minutes shows up once as a `delivery-deferred` event. `agent-exec` must be updated with the supervisor (new
`orch-send` exit codes 5, 6 and 7); `swarmctl`, `swarm_worker.py` and `agent-exec` are 0.8.1 together. Update the
backend too: it accepts the new `delivery-deferred` and `delivery-unknown` event types. An older backend refuses
them (HTTP 400); the supervisor then drops those two events, keeps them in its own log, and carries on.

## Upgrading to 0.8.0

Run the setup script as usual. The backend adds the Agent Bus tables (`agents`, `agent_aliases`, `bus_messages`,
`bus_attempts`) and a few columns (`orchestrator_messages.bus_message_id`, `tasks.head_sha`,
`tasks.unpushed_commits`) by itself at start; nothing is changed or removed, and starting twice is safe.
Nothing turns on by itself: no agent is registered, so no message is sent and finish notices go where they went
before. Two behaviour changes apply to everyone:

- **Done means landed.** A code task whose commits exist only on the worker (nothing pushed) now ends `waiting`
  with outcome `blocked` and the question "Work is committed on WORKER but NOT on GitHub", instead of `done`.
  Re-queue it with `--push` or `--pr`, or recover the branch from the task's bundle. A task that asked for a PR
  and pushed but has no confirmed PR also waits. Report-only, untrusted, review and script tasks are exempt. Runners
  from before 0.8.0 do not report the commit count, so for them nothing changes until they are updated.
- The `swarmctl` and `swarm_worker.py` versions are 0.8.0 together; update every worker.

To use the bus ([agent-bus.md](agent-bus.md)): register agents with `swarmctl bus register` and give each a token
with `swarmctl bus token NAME`. The backend makes its token secret in `bus-token-secret` beside the bot keys folder
on first use (mode 600); it is part of your config, not the backup: copy it if you restore onto a new server,
or re-issue tokens. Restart each orchestrator once so its session gets a bus token
(`swarmctl orchestrator restart NAME`); the supervisors are updated by the setup script's restart. The `/bus` page is
read-only and needs the admin token (a request from loopback, such as through Tailscale `serve`, gets 401 without it). To go back to 0.7.x:
the new tables and columns stay unused in the database, which older versions ignore.

## Upgrading to 0.7.1

Re-run setup as above. No database change and no setting is required. Supervisors are restarted by the setup
script; running sessions are adopted. After upgrading, an orchestrator that sits at a Claude sign-in screen is
stopped with state `needs-login` (run `sudo swarmctl login claude` on its worker, then `swarmctl orchestrator
start NAME`), and `swarmctl orchestrator delete` is refused while a session is still running or stopping.
If a first boot was interrupted by a reboot, `swarm-firstboot-resume.service` now finishes it or writes a
`stopped` status that says what to run.

## Upgrading to 0.7.0

Run the setup script as usual. The backend adds the `orchestrators`, `orchestrator_events` and
`orchestrator_messages` tables and a `projects` column on `workers` by itself at start; nothing is changed or
removed. The setup script installs the `swarm-orchestrator@.service` template but enables nothing: no
orchestrator runs until you create one and enable its supervisor ([orchestrators.md](orchestrators.md)).
After an upgrade it restarts any running supervisors (`systemctl try-restart 'swarm-orchestrator@*'`);
their sessions keep running and are adopted. A worker reports its project allowlist again with its next
check-in (every 20 seconds), so create orchestrators after the upgraded runner has checked in. On a Mac,
run `scripts/setup-macos.sh` again (it adds `tmux`), then `--orchestrator NAME` for each one.

## Upgrading to 0.6.1

Re-run setup as above. Nothing changes in the database and no setting is required. If you never got SOPS
on a worker built by cloud-init, run `sudo bash /opt/agent-swarm-kit/current/scripts/setup-secrets.sh --prepare`
again after the upgrade. To pin work to a worker by name, set `SWARM_WORKER_NAME` as shown in
`config/swarm.env.example` ([multi-worker.md](multi-worker.md)).

## Upgrading to 0.6.0

The backend adds the activity table on startup. Existing project log entries stay where they are.
Update the bot prompts and, if you want nightly exports, set up the separate
[memory repo](activity-and-decisions.md#nightly-export-to-the-memory-repo).

## Upgrading to 0.5.0

Re-run setup as above. Nothing changes in the database and no setting is required. What you get and what changes:

- `sudo swarmctl login codex | claude | github` for browser-only sign-ins, and `scripts/setup-secrets.sh`, `scripts/render-cloud-init.py`,
  `scripts/firstboot.sh`, `scripts/setup-macos.sh` and `scripts/setup-cliproxy.sh` arrive with the kit. None of them runs by itself on an existing worker.
- `/etc/agent-swarm/agent.env` becomes mode 640, owner root, group `agent` (it was world-readable). Tasks still read it. If you put a
  proxy address or a Claude token there, this is the safer mode.
- `setup-worker.sh` takes `--yes` (answers its own questions) and the lockdown step refuses with `--yes` unless Tailscale has an address.
- The Claude token path: if you want the long-lived token instead of a saved `/login`, run `sudo swarmctl login claude`.
  Tasks then use the token first. Renew it before a year is up.

## Upgrading to 0.4.0

Re-run setup as above. The database gains new columns in place. A single worker needs no new routing
settings. Follow [Secrets](secrets.md#set-up-the-encrypted-repo) to set up encryption and move any
legacy `with-secrets` values. Until then the existing worker and local `.env` bundles still work.

## Upgrading from 0.3.0 to 0.3.1

0.3.1 is a fix release. Re-run the setup script as in "Pulling an update". Nothing else to do: no new
settings and no database change. If a routine or script used `--max-cycles 0`, give it a real number (it
used to mean 20); and if one used `--chain` with `--command`, drop `--chain` (it never chained).

## Upgrading from 0.2.1 to 0.3.0

0.3.0 adds the project log, the goals meeting and work heartbeat, and new docs. Re-run the setup script
as in "Pulling an update". What changes for you:

- The backend adds the `log_entries` table, and the new log and task columns, by itself the first time
  it starts. Nothing to run by hand, and nothing in your tasks, bots or Waiting on you list changes.
- Feedback and decisions logged through the API now need `by` (who said or decided it). Update any
  routine that logs feedback (prompt 8 in templates/routine-prompts.md has the new wording).
- New settings, all optional, for `/etc/agent-swarm/swarm.env` (setup does not change an existing file):
  `SWARM_LOG_EXPORT_DIR=/var/lib/agent-swarm/log-exports` turns on the weekly log export,
  `SWARM_RULES_CAP` (25), `SWARM_TASK_TIMEOUT_MAX` (86400) and `SWARM_MAX_CYCLES` (200). Restart the
  backend after editing: `sudo systemctl restart swarm-backend`.
- Bind each project bot to its projects if you have not yet, including projects with no repo:
  `sudo swarmctl project set marketing --bot marketing-bot`. A bot can only use the log of projects it is
  bound to.
- Add the new routines to your bots (prompts 6, 7 and 8 in templates/routine-prompts.md), and paste the
  new "Goals right now", "Project log" and "Setup choices" parts of the templates into your bots' briefs.

## Upgrading from 0.2.0 to 0.2.1

0.2.1 is a fix release from the first install test on a fresh server. Re-run the setup script as in "Pulling an update". What changes for you:

1. **Tasks work again.** In 0.2.0 every task failed while it was being prepared (the inbox and outbox folders lost their shared group). Nothing to do; the fix is in the code.
2. **Hardening at install time.** If you pass `--admin-user NAME`, or run the script through sudo as your admin user, the script now turns off root and password login over SSH and turns the firewall on, with public SSH still allowed until you run `--lockdown`. If you only have root, it leaves root login on and says so. See [worker-setup.md](worker-setup.md), step 3.
3. **Lockdown checks for an admin user first** and refuses if only root can log in.
4. **Old release folders are removed**; only `current` and `previous` stay. Old outbox folders in `/srv/agent-swarm/outbox` are removed too: right after each task when its results are saved, and by the daily cleanup after 7 days.
5. `swarmctl status` no longer warns about backups on a new install; the setup script makes a first backup.
6. Long installer output goes to `/var/log/agent-swarm-setup.log`.
7. **apt waits for first-boot updates** (up to 10 minutes) and retries on a lock, so a first run on a brand new server no longer fails at once.

## Upgrading from 0.1.0 to 0.2.0

0.2.0 splits the worker into three users and moves secrets out of the database. The setup script does the moves; this is what changes for you:

1. **The runner is a new user, `swarm-runner`.** Log GitHub in as that user, then log the `agent` user out:

   ```bash
   sudo -u swarm-runner -H gh auth login
   sudo -u swarm-runner -H gh auth setup-git
   sudo -iu agent gh auth logout
   ```

2. **The worker token is replaced and moves** to `/var/lib/swarm-runner/worker.token`. The old one was readable by agents, so setup makes a new one and removes the old copy from `/home/agent/.config/swarm/`.
3. **Trigger keys move to files.** On first start the backend moves each bot's key into `/etc/agent-swarm/bot-keys/BOT_ID.key` and wipes it from the database. **Backups made before the upgrade still contain the keys.** Delete them, or change each bot's key.
4. **Repo tasks need a project.** A project bot can now queue repo work only for projects that list it. For each project: `sudo swarmctl project set NAME --repo OWNER/REPO --bot BOT_ID`.
5. **Exposure.** If you used Tailscale Funnel, consider moving to Tailscale Serve with Tailscale on the bot computers ([backend.md](backend.md#reaching-the-backend-from-your-bots)).
6. **Bots should read `event` and `reason`** in notices, and may answer 409 or `{"duplicate": true}` to repeats ([results-routing.md](results-routing.md)).
7. Run `sudo bash /opt/agent-swarm-kit/current/scripts/smoke-test.sh`.

## Giving improvements back

Found a bug, a better default, or a clearer doc? Send it back:

1. Make a branch in your clone: `git checkout -b fix/short-name`.
2. Make the change. Keep it generic: no names of your projects, bots, people, hostnames, tailnet names, URLs or keys.
3. Add a line to `CHANGELOG.md` under an "Unreleased" heading.
4. Run the checks:

```bash
python3 -m unittest discover -s tests
shellcheck scripts/*.sh server/run-agent
bash scripts/check-secrets.sh .
```

Two tests need two real OS users or root: `test_handoff_users.py` (the inbox and outbox handoff between the runner and the agent) and the root part of `test_setup_script.py` (lockdown and admin user checks). They run in a throwaway Docker container when Docker works, and otherwise skip with a loud message. Set `SWARM_REQUIRE_USER_TESTS=1` to make a skip fail instead.

   `check-secrets.sh` looks for tokens, keys and real tailnet names. Put your own names (project names, bot ids, hostnames) one per line in a `.secret-patterns` file to check those too. That file is git-ignored.

5. Push your branch and open a pull request against the original repo. Describe what changed and why, and how you tested it.

### How a bot can propose an improvement

Bots notice friction first. When a bot sees something worth changing in the kit:

1. It queues a task on the worker to make the change on the kit repo with `--pr`, so the result is a draft pull request.
2. It files a Waiting on you item so the human sees it once, without extra chat:

```bash
swarmctl waiting add --project agent-swarm-kit --bot MY-BOT-ID --kind deliverable-review \
  --ask "Review this kit improvement?" --rec "Merge it" --link https://github.com/YOUR-ACCOUNT/agent-swarm-kit/pull/N \
  --key kit-improvement-short-name
```

3. The human reviews, then merges or drops it. A dropped improvement is not filed again.
