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
- Tokens: `/etc/agent-swarm/*.token` and `/home/agent/.config/swarm/worker.token`. Never in the repo.
- Data: `/var/lib/agent-swarm/`.
- Bot briefs, routines and model routing tables live with your bots. Copy them from `templates/` once, then edit your copies.

If you do change kit code, keep it on your own git branch (for example `local`) and rebase it on each update.

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

4. Re-run the setup script. It is safe to repeat: it keeps your settings and tokens, copies the new code to `/opt/agent-swarm-kit`, and reinstalls the services.

```bash
sudo bash scripts/setup-worker.sh
sudo systemctl restart swarm-backend swarm-worker
swarmctl status
```

   The quick alternative, for code-only changes: copy `server/` to `/opt/agent-swarm-kit/server/` and run `sudo systemctl restart swarm-backend swarm-worker`.

5. Compare your `swarm.env` with the new `config/swarm.env.example` and add any new settings you want:

```bash
diff /etc/agent-swarm/swarm.env /opt/agent-swarm-kit/config/swarm.env.example
```

The database schema uses `CREATE TABLE IF NOT EXISTS`, so new tables appear on their own when the backend starts. If a release ever changes existing tables, its changelog entry includes the exact migration step.

Restarting the worker stops any running task. The backend requeues it after the lease runs out (about 10 minutes) and it resumes. To avoid that, update when `swarmctl status` shows nothing running.

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
