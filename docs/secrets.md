# Secrets

Set up encrypted bundles even with one worker. Keep them in a separate repo, give each machine its own
key, and keep a recovery key in your password manager. Bots refer to bundles by name. Nobody ever
pastes a secret into chat.

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
| Shared keys for tasks | Encrypted `runtime/` bundles; local `~/.config/swarm-secrets/cache.json` (600) | the `agent` user, so every task. See below |

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

## Set up the encrypted repo

1. Install [SOPS](https://getsops.io/) and [age](https://github.com/FiloSottile/age) on your own computer
   and on the worker. On the worker, put the executables on the service's path (`/usr/local/bin` or `/usr/bin`).
2. On your own computer, run the commands below. They make a private key and show only its public half.
   On the worker, run the same commands in a shell started with `sudo -iu agent`. Never copy a machine's
   private key to another machine. Separately make a recovery identity with `age-keygen -o RECOVERY_FILE`
   in a private directory outside the repo. Save that private key in your password manager, get its public
   key with `age-keygen -y RECOVERY_FILE`, then remove the local recovery file. Do not put it on a worker.

   ```bash
   umask 077
   install -d -m 700 ~/.config/swarm-secrets
   age-keygen -o ~/.config/swarm-secrets/age.key
   age-keygen -y ~/.config/swarm-secrets/age.key
   ```

3. On your computer, make a separate private Git repo. Make `runtime/` for bundles allowed on workers and
   `admin/` for bundles only you can decrypt. Put this `.sops.yaml` at the repo root. Replace the labels
   with **public** age recipients, not private keys. Give a bundle its own rule above the scope rule if it
   needs a narrower set of recipients. SOPS uses the first matching rule.

   ```yaml
   creation_rules:
     - path_regex: '^runtime/[^/]+\.json$'
       age: OWNER_PUBLIC_KEY,RECOVERY_PUBLIC_KEY,WORKER_PUBLIC_KEY
     - path_regex: '^admin/[^/]+\.json$'
       age: OWNER_PUBLIC_KEY,RECOVERY_PUBLIC_KEY
   ```

4. From the secrets repo root, make an encrypted `tools` bundle to test setup. It can start empty. This
   creates only ciphertext on disk. Do not use `>` on an existing bundle; it would truncate it.

   ```bash
   mkdir -p runtime admin
   printf '{}\n' | sops encrypt --input-type json --output-type json \
     --filename-override runtime/tools.json /dev/stdin > runtime/tools.json
   ```

   Commit and push only `.sops.yaml` and encrypted bundle files. Use the same command with an `admin/`
   path for an owner-only bundle. Keep the admin token, runner token and bot trigger keys in their
   protected service files; do not put them in a runtime bundle. If you back them up here, use `admin/`.
5. As `agent` on the worker, clone the secrets repo into `~/.config/swarm-secrets/repo` using a dedicated
   read-only Git credential for that repo. Use one that works without a prompt or a desktop SSH agent,
   since the service does not inherit your login session. Write the
   runtime bundle names you want on this machine, one per line, then sync:

   ```bash
   umask 077
   git clone SECRETS_REPO_URL ~/.config/swarm-secrets/repo
   printf '%s\n' tools > ~/.config/swarm-secrets/runtime-bundles
   swarm-sync-secrets
   with-secrets --check tools
   ```

The worker service runs the same sync before it starts. A sync pulls with fast-forward only, decrypts
only the named `runtime/` files, and replaces the private cache only if all of them succeed. An error
keeps the old cache but stops startup; fix the checkout, Git access, recipient list or bundle before
starting the service again. Run `sudo -iu agent swarm-sync-secrets` after an edit if you do not want to
restart the worker. Don't put the cache or machine keys in backups, Git, or a shared folder.

## Add a secret once

On your own computer, set `SOPS_AGE_KEY_FILE` to your private age key **path** and run
`sops edit runtime/tools.json` from the secrets repo. Use a trusted local editor with backups and swap
files disabled. Add a JSON property whose name is the environment variable and whose value is a string.
Save, then commit the encrypted change and push. Sync each worker that uses the bundle. In a brief, say
“Run it with `with-secrets tools -- COMMAND`.” `with-secrets --check tools` lists names only. The wrapper
loads values for the command and its children; do not run a command that prints its environment.

**Honest limit:** every task runs as `agent` and can use every secret that the worker key can decrypt,
even ones you leave out of the cache list. The age recipients, not the cache list or directory name,
are the access control. Use restricted keys with spending caps and keep admin recipients off workers.

## Add a machine, rotate, or recover

- **Add a machine:** make its own key there. Add its public recipient to the matching runtime rules, not
  to `admin/`. Run `sops updatekeys PATH` once per affected bundle, commit and push, then set up its
  read-only checkout and cache list as above.
- **Rotate a service secret:** replace it at the provider, edit the bundle once, push and sync, verify
  the consumer works, and revoke the old value. If the old secret leaked, revoke it right away.
- **Replace a machine key:** add the replacement recipient and test it before removing the old one.
  On removal, run `sops updatekeys PATH` and then `sops rotate -i PATH` for every affected bundle,
  commit, push and re-sync. Remove the old machine's Git access and cache. Old Git commits stay
  readable with an old key, so revoke and replace any service secrets the retired key could read.
- **Recover:** on a trusted owner computer, restore the password manager's recovery identity to a
  temporary mode-600 file. Set `SOPS_AGE_KEY_FILE` to that path. Create a new owner key, replace the lost
  public recipient in the rules, and run `sops updatekeys PATH` on all affected bundles. Test with the
  new owner key before removing the temporary file. If a key might have leaked, rotate as above.

Upgrading from plain files? Old `with-secrets` calls keep working until you configure `runtime-bundles`.
Move the values into encrypted bundles with `sops edit`, sync and verify, then remove the old `.env`
files (and retire any plaintext backups). The wrapper does not fall back to those files after migration.

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
