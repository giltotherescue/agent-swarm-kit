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

Your worker pulls with the GitHub login set up during worker setup, or with a read-only deploy key for the kit repo.

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
