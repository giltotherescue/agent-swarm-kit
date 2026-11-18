# Secrets

This page says where keys and tokens live, who can read them, and what to do if one leaks. The short version: secrets live in files with tight permissions, bots refer to them by path, and nobody ever pastes a secret into chat.

## Where secrets live

| Secret | Where | Who can read it |
| --- | --- | --- |
| Admin token (full control of the backend) | `/etc/agent-swarm/admin.token` | root and the `swarm` service user (mode 640) |
| Worker token, backend's copy | `/etc/agent-swarm/worker.token` | root and `swarm` (mode 640) |
| Worker token, worker's copy | `/home/agent/.config/swarm/worker.token` | the `agent` user (mode 600) |
| Admin CLI settings on the box | `/root/.config/swarm/client.env` | root |
| A bot's token | wherever that bot keeps secrets, for example `~/.config/swarm/bot-website-bot.token` (mode 600) | that bot only |
| A bot's trigger key (for incoming notices) | stored in the backend; `GET /bots` shows only `has_notify_auth` | backend only |
| Claude Code and Codex logins | under `/home/agent` (the CLIs store them) | the `agent` user |
| GitHub login (`gh auth login`) | under `/home/agent` | the `agent` user |

Mode 600 means only the owner can read and write the file. Mode 640 adds read access for one group. The [setup script](../scripts/setup-worker.sh) creates the admin and worker tokens this way and never prints them.

The [settings file](../config/swarm.env.example) holds paths to token files, never the tokens themselves.

## The agent can read what the agent user can read

The worker runs Claude Code and Codex with permission prompts turned off, so they can work unattended. They run as `agent`, a normal user with no `sudo` (no admin rights). That limits the damage, but anything the `agent` user can read, the model can read too.

So:

- The admin token is not readable by `agent`. The setup script puts it in `/etc/agent-swarm` owned by root and the `swarm` group. Keep it that way.
- The worker token is readable by `agent`. With it, a task could read any task and report, claim or finish tasks, and send alerts. It cannot queue tasks, answer questions, create bots or tokens, or change settings.
- Do not put production passwords, payment keys or cloud admin keys in the `agent` user's home. If a task truly needs one, give it a narrow, read-only key for that one job, and remove it after.
- Tasks get no secrets by default. Briefs should never contain secrets.

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

It writes the token to `~/.config/swarm/bot-website-bot.token` with mode 600 (or to `--out FILE`) and prints only the path. The backend keeps only a hash (a one-way fingerprint), so it cannot show the token again.

To rotate, run the same command again. The old token stops working at once. Give the new file to the bot.

To change the admin or worker token, replace the file contents (keep the same owner and mode), copy the new worker token to the worker's copy, and restart both services: `systemctl restart swarm-backend swarm-worker`.

## Before you push anything

Run the secret scan on any repo before pushing, including your copy of this kit:

```
scripts/check-secrets.sh .
```

It looks for common token formats (GitHub, Slack, cloud keys, private keys, Tailscale keys, swarm bot tokens) and real tailnet hostnames. Add your own names (bot ids, hostnames, project names) one per line to a `.secret-patterns` file at the repo root. That file is ignored by git, so your private names never get committed.

## If a secret leaks

1. **Revoke it first.** Rotate a bot token with `swarmctl bot token ID`. For other keys, revoke them at the provider (GitHub, Tailscale, your payment provider). Do this before cleaning up.
2. **Remove it** from chat, notes, the repo and reports. If it reached a git commit, assume it is public even after you delete it; rotating is what makes you safe.
3. **Check what it could do** and look at recent activity with that key.
4. **Write one line** in the project brief: what leaked, when, and that it was rotated.

## Backups contain data, not secrets

The nightly backup copies the backend database (tasks, reports, Waiting on you items, bot trigger keys). Treat backup files as sensitive. The token files in `/etc/agent-swarm` are not in the backup; keep your own copy in a password manager if you want to restore them.
