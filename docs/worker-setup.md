# Worker server setup

This doc takes you from "I have no server" to "a worker is running tasks safely". The worker is an always-on Linux server that runs Claude Code and Codex on your behalf. It also runs the task backend by default. Any cloud provider works. You need about an hour, most of it waiting for installs.

**Tested on: not yet.** The setup script has been checked with shellcheck (a script checker) and its parts were tested one by one, but it has not been run end to end on a fresh server. Run it first on a server you can throw away and rebuild, and run the smoke test (step 8) afterwards.

If you do not want a server yet, read [grokbot-only.md](grokbot-only.md) first. You can add a worker later.

## Who this is for (the trust model)

The kit assumes **one trusted owner** (you) and a **dedicated worker** that runs nothing else. Inside that:

- a task cannot read or change other tasks;
- a project bot cannot see or act on another bot's tasks, items or projects;
- a task never holds the long-lived worker token or the GitHub login that pushes code.

It is not built for several people who do not trust each other sharing one worker. [secrets.md](secrets.md) has the details and the honest limits.

## 1. Pick a provider and a size

Any provider that gives you an Ubuntu LTS (Long Term Support) virtual machine works. A few that people use:

- Hetzner (good price for the power)
- DigitalOcean
- Vultr
- Linode (now part of Akamai)
- AWS Lightsail (simple Amazon option)

Start with **4 vCPU (virtual CPUs) and 8 GB of memory**, and at least 80 GB of disk. That runs two tasks at once. [sizing-and-sandboxes.md](sizing-and-sandboxes.md) helps you decide if you need more.

When you create the server:

- Choose **Ubuntu 24.04 LTS** (22.04 also works).
- Add your **SSH key** (Secure Shell key) at creation. Do not use a password.
- If the provider has a cloud firewall, allow SSH (port 22) only from your own IP address for now. You close it fully in step 9.
- Turn on the provider's snapshot or backup option if it is cheap. It is a nice safety net.

## 2. Get the kit onto the server

Log in as root (or the default sudo user) with the SSH key, then:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/YOUR-ACCOUNT/agent-swarm-kit.git
cd agent-swarm-kit
```

If your copy of the repo is private, you need a GitHub account that can read it. See [updating-the-kit.md](updating-the-kit.md).

## 3. Run the setup script

```bash
sudo bash scripts/setup-worker.sh --tags tag:swarm-worker
```

`--tags` only works once your Tailscale access rules list `tag:swarm-worker` (see [Tailscale access rules](#tailscale-access-rules) below). Leave it off on the very first run if you have not set them up yet.

The script is safe to run again; a second run is an upgrade. It never prints a token. It ends with exit code 0 when all is done, 1 when it stopped on an error, and 3 when it finished but lists things you still need to do. What it does, in order:

1. Checks it runs as root on Ubuntu 22.04 or 24.04, and that the `agent` account has no sudo rights.
2. Installs updates and basic tools, and turns on **automatic security updates**.
3. Caps the system log at 1 GB and adds a **swap file** (4 GB by default) if the box has none.
4. Installs the **GitHub CLI** (`gh`) and **Node.js LTS**.
5. Installs **Tailscale** and joins your tailnet (your private Tailscale network) with Tailscale SSH turned on.
6. Creates three users:
   - `swarm` runs the backend and owns the database in `/var/lib/agent-swarm`.
   - `swarm-runner` runs the runner: it takes tasks from the backend, prepares the code, starts the agent, and pushes the result. It holds the worker token and the GitHub login.
   - `agent` runs Claude Code, Codex and script tasks. It has no sudo, no worker token and no GitHub login.
7. Caps memory and process count for all tasks together, so a runaway task cannot starve SSH, Tailscale or the backend.
8. Installs the kit into `/opt/agent-swarm-kit/releases/VERSION-TIME` and points `/opt/agent-swarm-kit/current` at it. On an upgrade it first drains the worker and waits for running tasks (`--max-wait`, default an hour).
9. Copies settings to `/etc/agent-swarm/swarm.env` (only if it does not exist yet), and creates the admin and worker tokens.
10. Installs Claude Code and Codex CLI for the `agent` user.
11. Installs the helpers: `agent-exec` (the runner's only way to start anything as `agent`, through one sudo rule), `with-secrets` and `swarmctl`.
12. Installs the systemd services and timers and starts the backend.

Options:

| Option | What it does |
| --- | --- |
| `--auth-key FILE` | Join Tailscale with an auth key read from a file, without a browser. The key is never printed. `--tailscale-authkey-file FILE` still works |
| `--tags TAGS` | Tailscale tags to advertise, for example `tag:swarm-worker` |
| `--no-tailscale` | Skip Tailscale (not recommended) |
| `--no-claude` / `--no-codex` | Skip installing one of the model CLIs |
| `--swap-gb N` | Swap size in GB (default 4, `0` to skip) |
| `--max-wait SECONDS` | On upgrade, how long to wait for running tasks (default 3600) |
| `--force-os` | Run on an Ubuntu version other than 22.04 or 24.04 |
| `--lockdown` | Only run the lockdown step (step 9) |

## 4. Log in the model CLIs with your own subscriptions

The default is plain Claude Code and Codex logged in with your own subscriptions. No API keys.

```bash
sudo -iu agent claude              # then type /login and follow the link
sudo -iu agent codex login --device-auth
```

Heavy users with several subscriptions can add a proxy later. See [optional-services.md](optional-services.md).

## 5. Log in GitHub as the runner (for repo work)

Only `swarm-runner` holds a GitHub login. Agents never do.

```bash
sudo -u swarm-runner -H gh auth login
sudo -u swarm-runner -H gh auth setup-git
```

Use a **fine-grained token** limited to the repos the swarm works on, with "Contents: read and write" and "Pull requests: read and write" only. The runner pushes `agent/<task-id>` branches and opens draft pull requests. It never pushes the main branch unless a project allows it (`swarmctl project set NAME --allow-push-main`), and it never merges.

If you logged in GitHub as `agent` before, log it out: `sudo -iu agent gh auth logout`.

## 6. Start the worker

```bash
sudo systemctl start swarm-worker
sudo swarmctl status
```

`swarmctl` as root uses the admin token through `/root/.config/swarm/client.env`, which the script wrote.

## 7. Let your bots reach the backend

Recommended: put Tailscale on each bot's computer, tag it `tag:swarm-bot`, and publish the backend to your tailnet only:

```bash
sudo tailscale serve --bg 8750
```

Then set `SWARM_PUBLIC_URL=https://YOUR-WORKER.YOUR-TAILNET.ts.net` in `/etc/agent-swarm/swarm.env` and `sudo systemctl restart swarm-backend`.

Option 2, only if a bot's computer cannot run Tailscale, is Tailscale Funnel (`sudo tailscale funnel --bg 8750`). Funnel puts the backend **on the public internet**, so the tokens become the only gate. Read the warnings in [backend.md](backend.md#option-2-tailscale-funnel-public-internet) first.

Then register your bots ([results-routing.md](results-routing.md)).

## 8. Run the smoke test

```bash
sudo bash /opt/agent-swarm-kit/current/scripts/smoke-test.sh
```

It checks users, file owners and modes, the sudo rule, services, timers and health, then runs one small script task (`echo ok`, no model, no cost) from queued to done. It prints PASS, WARN or FAIL per line and never prints a token. Use `--no-task` to skip the task.

## 9. Close public SSH (lockdown)

Do this only after you can log in over Tailscale from your laptop:

```bash
ssh YOUR-ADMIN-USER@YOUR-WORKER        # the machine's Tailscale name
sudo bash /opt/agent-swarm-kit/current/scripts/setup-worker.sh --lockdown
```

The script asks you to confirm first. It then sets the firewall (ufw) so SSH is reachable only over Tailscale, turns off password and root login for SSH, checks the settings really took effect, and reloads SSH. Also remove port 22 from your provider's cloud firewall. If something goes wrong, your provider's web console (serial console) is the way back in.

## Tailscale access rules

Tailscale access rules (the ACL, "access control list", in the Tailscale admin console) decide which of your machines may talk to which. A tag is a label for a machine instead of a person. This sample lets bot computers reach only the backend on the worker, and lets only your admin group log in, never as root:

```json
{
  "groups": {
    "group:swarm-admins": ["you@example.com"]
  },
  "tagOwners": {
    "tag:swarm-worker": ["group:swarm-admins"],
    "tag:swarm-bot": ["group:swarm-admins"]
  },
  "acls": [
    { "action": "accept", "src": ["group:swarm-admins"], "dst": ["tag:swarm-worker:*", "tag:swarm-bot:*"] },
    { "action": "accept", "src": ["tag:swarm-bot"], "dst": ["tag:swarm-worker:443"] }
  ],
  "ssh": [
    {
      "action": "check",
      "src": ["group:swarm-admins"],
      "dst": ["tag:swarm-worker"],
      "users": ["YOUR-ADMIN-USER"]
    }
  ]
}
```

Notes:

- Replace `you@example.com` and `YOUR-ADMIN-USER` (a normal user with sudo, not `root`).
- The worker itself needs no rule to reach out: notices to bots go out over the internet.
- "check" asks you to log in again in the browser now and then before SSH works. Use "accept" if that gets in the way.
- Tailscale SSH follows these rules, not the OpenSSH settings on the box. If the `ssh` section lets in `root`, root can log in.

## Security baseline checklist

Each line has a command to check it. The smoke test checks most of them for you.

| Item | How to check |
| --- | --- |
| No public SSH. Tailscale only, plus the cloud firewall | `sudo ufw status` shows port 22 only on `tailscale0`, plus `41641/udp`. From outside your tailnet, `ssh` to the public IP times out |
| Password and root SSH login off | `sudo sshd -T \| grep -E 'passwordauthentication\|permitrootlogin'` shows `no` |
| Tailscale SSH limited to admins, not root | The `ssh` section of your tailnet rules, as in the sample above |
| Backend not on the public internet | `tailscale funnel status` shows nothing, or you chose Funnel on purpose |
| Agents run as `agent`, with no sudo | `sudo -l -U agent` says the user may not run sudo |
| The runner can start only `agent-exec` as `agent` | `sudo cat /etc/sudoers.d/swarm-runner` |
| `agent` cannot read any token | `sudo -u agent cat /var/lib/swarm-runner/worker.token` and `sudo -u agent cat /etc/agent-swarm/admin.token` both fail |
| `agent` has no GitHub login | `sudo -iu agent gh auth status` says not logged in |
| Backend runs as its own `swarm` user | `systemctl show -p User swarm-backend` |
| Automatic security updates | `systemctl is-enabled unattended-upgrades` |
| Services restart on crash and on reboot | `systemctl is-enabled swarm-backend swarm-worker` and `systemctl show -p Restart swarm-backend` |
| Daily cleanup of finished task folders (keeps queued, running and waiting tasks) | `systemctl list-timers swarm-cleanup.timer` |
| Nightly database backup | `systemctl list-timers swarm-backup.timer` and `sudo ls /var/lib/agent-swarm/backups` |
| Hourly disk alert to the Chief of Staff above 80 percent | `systemctl list-timers swarm-disk-alert.timer` |
| Per-task and all-task CPU and memory limits | `SWARM_TASK_CPU_QUOTA` and `SWARM_TASK_MEMORY_MAX` in `swarm.env`, and `systemctl cat user-$(id -u agent).slice` |

### An honest note about agent permissions

The worker runs Claude Code and Codex with permission prompts turned off (`bypassPermissions` and `--dangerously-bypass-approvals-and-sandbox`). There is nobody there to click "allow". That means **anything the `agent` user can do, an agent can do**. What the kit does about it:

- `agent` holds no tokens and no GitHub login. Each task gets a task token that works only for that one task and stops when the task ends.
- Tasks get code through a git bundle (a single file holding the commits) and hand their commits back the same way. The runner checks the new lines for secrets and pushes only to the task's own branch.

What it does not do:

- All tasks run as the same `agent` user, so they share that user's home folder and the Claude and Codex logins.
- Outgoing network access is not limited. A task can reach the internet.

So keep that user small: no production database passwords, payment keys or cloud admin keys in its files. If a task needs a shared key, use [with-secrets](secrets.md#shared-keys-for-tasks-with-secrets).
