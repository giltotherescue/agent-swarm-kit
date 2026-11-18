# Secrets

This page says where keys and tokens live, who can read them, and what to do if one leaks. The short version: secrets live in files with tight permissions, bots refer to them by path, and nobody ever pastes a secret into chat.

## Trust model

The kit is built for **one trusted owner** (you) running a **dedicated worker** that does nothing else. Inside that, it promises:

- a task cannot read or change other tasks;
- a project bot cannot see or act on another bot's tasks, Waiting on you items or projects;
- a task never holds the long-lived worker token or the GitHub login that pushes code.

It does not promise safety between several people who do not trust each other, and it does not stop a task from using the internet. See [the honest limits](#honest-limits) below.

## Where secrets live

Mode 600 means only the owner can read and write the file. Mode 640 adds read access for one group.

| Secret | Where | Who can read it |
| --- | --- | --- |
| Admin token (full control of the backend) | `/etc/agent-swarm/admin.token` | root and the `swarm` backend user (640) |
| Worker token, backend's copy | `/etc/agent-swarm/worker.token` | root and `swarm` (640) |
| Worker token, runner's copy | `/var/lib/swarm-runner/worker.token` | `swarm-runner` only (600) |
| Task tokens (`swt_`) | made at claim time, passed by the runner to the one task, stored in the backend only as a hash | that task, for as long as it runs |
| Admin CLI settings on the box | `/root/.config/swarm/client.env` | root |
| A bot's token (`swb_`) | wherever that bot keeps secrets, for example `~/.config/swarm/bot-website-bot.token` (600) | that bot only |
| A bot's trigger key (proves a notice came from your backend) | `/etc/agent-swarm/bot-keys/BOT_ID.key` (600, folder 700) | `swarm` only. The database keeps only a short fingerprint |
| GitHub login | under `/var/lib/swarm-runner` (`gh auth login` as `swarm-runner`) | `swarm-runner` only |
| Claude Code and Codex logins | under `/home/agent` | the `agent` user |
| Shared keys for tasks | `/home/agent/.config/agent-secrets/NAME.env` (600) | the `agent` user, so every task. See below |

The [setup script](../scripts/setup-worker.sh) creates the admin and worker tokens this way and never prints them. The [settings file](../config/swarm.env.example) holds paths to token files, never the tokens themselves.

## What each token can do

| Token | Can do | Cannot do |
| --- | --- | --- |
| admin | everything a person can do | runner and task calls |
| chief bot | what admin does | runner and task calls, making tokens |
| project bot | its own tasks, items and bot settings, and projects it is bound to | anything of another bot's |
| worker | take the next task, read task states (no content), send alerts | read any task, report, file, bot or project |
| task | its own task: check in, read it and its attachments, upload its files, ask before publishing, finish | anything of another task; anything at all once the task stops running |

The full route-by-route table is in [backend.md](backend.md#who-can-call-what), and a test keeps it in step with the code.

## Shared keys for tasks: with-secrets

Some tasks need a key that many tasks share, like a text-to-speech or search API key. Do not put it in a brief, a repo or the `agent` user's shell profile. Use `with-secrets`, which loads the key into the environment of **one command only**:

1. As root on the worker, create the file with the key in it:

   ```bash
   sudo -u agent install -d -m 700 /home/agent/.config/agent-secrets
   sudo -u agent sh -c 'umask 077; cat > /home/agent/.config/agent-secrets/voice.env'
   # type or paste:  VOICE_API_KEY=...   then press Ctrl-D
   ```

2. Check it without showing the value: `sudo -u agent with-secrets --check voice` prints `voice: VOICE_API_KEY`.
3. In a brief, tell the agent how to use it: "Run the script with `with-secrets voice -- python3 make_audio.py`."

The file must be a normal file (not a link), owned by `agent`, with mode 600. Otherwise `with-secrets` refuses and says why. It never prints a value, and a missing file is a clear error.

**Honest limit:** every task runs as `agent`, so every task can run `with-secrets` and read every key in that folder. Only add keys that are fine for any task to use. Prefer restricted keys with spending caps, and rotate them now and then.

## Honest limits

- **All tasks share the `agent` user.** They share its home folder, the Claude and Codex logins and the shared keys above. The handoff folders and task tokens keep tasks out of each other's backend data, but not out of each other's files under `/home/agent`.
- **Outgoing network access is open.** A task can send data to any internet address.
- **The backend reads tokens from files at start.** Anyone who is root on the worker can read every token. That is expected for a single-owner box.
- **Development mode.** With `SWARM_AGENT_USER` empty (used by the tests), the runner and the agent are the same user, so the agent can read the worker token. Do not use that on a real worker.

## Rules for bots

- **Never paste a secret in chat,** in a task brief, in a report, or in a commit. Not even "just this once".
- Secrets come in through your bot platform's secret storage or connectors, or through a file you place on the box yourself.
- Refer to a secret by its path or name, never by its value.
- If you must show one to confirm which it is, mask all but the last 4 characters: `****************a1b2`.
- `swarmctl` reads tokens from files (`SWARM_TOKEN_FILE`, default `~/.config/swarm/client.token`) so they never appear on a command line.

## Making and rotating bot tokens

An admin makes a token for each bot:

```
swarmctl bot token website-bot
```

It writes the token to `~/.config/swarm/bot-website-bot.token` with mode 600 (or to `--out FILE`) and prints only the path. It refuses to write through a link. The backend keeps only a hash (a one-way fingerprint), so it cannot show the token again.

To rotate, run the same command again. The old token stops working at once. Give the new file to the bot.

To change the admin or worker token, replace the file contents (keep the same owner and mode), copy the new worker token to the runner's copy, and restart both services: `sudo systemctl restart swarm-backend swarm-worker`.

## Trigger keys

A trigger key is a shared password the backend sends with each notice, so the bot knows the notice is real. Set it from a file so it never appears on a command line:

```
swarmctl bot set website-bot --notify-url https://example.com/hooks/website --notify-auth-file ~/.config/swarm/website.hook
```

The backend saves it as `/etc/agent-swarm/bot-keys/website-bot.key`. The database keeps only a fingerprint and a version number. When you change the key or URL, deliveries still waiting go to the new URL with the new key; none are sent with the old one.

Upgrading from 0.1.0: the first start of 0.2.0 moves old keys out of the database into these files and wipes them from the database file. Backups made before the upgrade still hold the old keys, so delete those backups or change the keys.

## Before you push anything

Run the secret scan on any repo before pushing, including your copy of this kit:

```
scripts/check-secrets.sh .
```

It looks for common token formats (GitHub, Slack, cloud keys, private keys, Tailscale keys, swarm bot and task tokens) and real tailnet hostnames, using the list in `config/secret-patterns.txt`. It prints the file, the line number and which pattern matched, never the secret itself. It exits 1 when it finds something and 2 when it could not scan, so a scan error never looks clean. Add your own names (bot ids, hostnames, project names) one per line to a `.secret-patterns` file at the repo root. That file is ignored by git, so your private names never get committed.

The runner runs the same kind of check on the new lines of every task's commits before it pushes.

## If a secret leaks

1. **Revoke it first.** Rotate a bot token with `swarmctl bot token ID`. For other keys, revoke them at the provider (GitHub, Tailscale, your payment provider). Do this before cleaning up.
2. **Remove it** from chat, notes, the repo and reports. If it reached a git commit, assume it is public even after you delete it; rotating is what makes you safe.
3. **Check what it could do** and look at recent activity with that key.
4. **Write one line** in the project brief: what leaked, when, and that it was rotated.

## Backups

The nightly backup copies the backend database. It holds **no tokens and no trigger keys**: tokens are stored only as hashes, and keys live in their own files. It **does** hold every brief, answer, report and attached file, so it is still private. Keep backups at mode 600 on the box, and use an encrypted remote (rclone "crypt" or restic) for any copy off the box. The token and key files in `/etc/agent-swarm` are not in the backup; keep your own copy in a password manager if you want to restore them.
