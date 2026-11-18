# Worker server setup

This doc takes you from "I have no server" to "a worker is running tasks safely". The worker is an always-on Linux server that runs Claude Code and Codex on your behalf. It also runs the task backend by default. Any cloud provider works. You need about an hour, most of it waiting for installs.

If you do not want a server yet, read [grokbot-only.md](grokbot-only.md) first. You can add a worker later.

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
- If the provider has a cloud firewall, allow SSH (port 22) only from your own IP address for now. You will close it fully later.
- Turn on the provider's snapshot or backup option if it is cheap. It is a nice safety net.

## 2. Get the kit onto the server

Log in as root (or the default sudo user) with the SSH key, then:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/YOUR-ACCOUNT/agent-swarm-kit.git
cd agent-swarm-kit
```

The repo is private. You need a GitHub account that has access. See [updating-the-kit.md](updating-the-kit.md).

## 3. Run the setup script

```bash
sudo bash scripts/setup-worker.sh
```

The script is safe to run again. It never prints a token. Here is what it does, in order:

1. Checks it runs as root on Ubuntu.
2. Installs updates and basic tools (git, curl, jq, tmux, python3, sqlite3, build tools).
3. Turns on **automatic security updates** (unattended-upgrades).
4. Caps the system log (journald) at 1 GB so logs cannot fill the disk.
5. Adds a **swap file** (4 GB by default) if the box has none. Swap gives memory spikes somewhere to go.
6. Installs the **GitHub CLI** (`gh`) and **Node.js LTS**.
7. Installs **Tailscale** and joins your tailnet (your private Tailscale network). It prints a link to approve the machine.
8. Creates two users:
   - `agent` runs the model CLIs and tasks. It has no sudo.
   - `swarm` is a system user that runs the backend and owns the database in `/var/lib/agent-swarm`.
9. Copies the kit to `/opt/agent-swarm-kit` (owned by root, so agents cannot change it) and links `swarmctl` into `/usr/local/bin`.
10. Copies settings to `/etc/agent-swarm/swarm.env` (only if it does not exist yet).
11. Creates two random tokens: `/etc/agent-swarm/admin.token` and `/etc/agent-swarm/worker.token`. The worker gets its own copy at `/home/agent/.config/swarm/worker.token` (only `agent` can read it).
12. Installs Claude Code and Codex CLI for the `agent` user.
13. Installs the systemd services and timers, starts the backend, and checks `http://127.0.0.1:8750/health`.

Options:

| Option | What it does |
| --- | --- |
| `--tailscale-authkey-file FILE` | Join Tailscale with an auth key read from a file, without a browser |
| `--no-tailscale` | Skip Tailscale (not recommended) |
| `--no-claude` / `--no-codex` | Skip installing one of the model CLIs |
| `--swap-gb N` | Swap size in GB (default 4, `0` to skip) |
| `--lockdown` | Only run the lockdown step (step 7 below) |

## 4. Log in the model CLIs with your own subscriptions

The default is plain Claude Code and Codex logged in with your own subscriptions. No API keys.

```bash
sudo -iu agent claude              # then type /login and follow the link
sudo -iu agent codex login --device-auth
```

Heavy users with several subscriptions can add a proxy later. See [optional-services.md](optional-services.md).

## 5. Log in GitHub (for repo work)

```bash
sudo -iu agent gh auth login
```

Use an account or token that can reach only the repos the swarm should touch. A fine-grained token limited to a few repos is the safest choice. The worker pushes `agent/<task-id>` branches and opens draft pull requests. It never pushes `main` and never merges.

## 6. Start the worker

```bash
sudo systemctl start swarm-worker
sudo swarmctl status
```

`swarmctl` as root uses the admin token through `/root/.config/swarm/client.env`, which the script wrote.

Next: let your bots reach the backend ([backend.md](backend.md)), register your bots ([results-routing.md](results-routing.md)), and run the [first week](first-week.md).

## 7. Close public SSH (lockdown)

Do this only after you can log in over Tailscale from your laptop:

```bash
ssh YOUR-ADMIN-USER@YOUR-WORKER        # the machine's Tailscale name
sudo bash /opt/agent-swarm-kit/scripts/setup-worker.sh --lockdown
```

The script asks you to confirm first. It then sets the firewall (ufw) to accept traffic only from Tailscale, turns off password and root login for SSH, and reloads SSH. Also remove port 22 from your provider's cloud firewall. If something goes wrong, your provider's web console (serial console) is the way back in.

## Security baseline checklist

Each line has a command to check it.

| Item | How to check |
| --- | --- |
| No public SSH. Tailscale only, plus the cloud firewall | `sudo ufw status` shows only `tailscale0` and `41641/udp`. From a network outside your tailnet, `ssh` to the public IP times out. |
| Password and root SSH login off | `sudo sshd -T \| grep -E 'passwordauthentication\|permitrootlogin'` shows `no` |
| Agents run as `agent`, a normal user with no sudo | `sudo -l -U agent` says the user may not run sudo |
| Backend runs as its own `swarm` user | `ps -o user= -C python3` and `systemctl show -p User swarm-backend` |
| Automatic security updates | `systemctl is-enabled unattended-upgrades` and `cat /etc/apt/apt.conf.d/20auto-upgrades` |
| Services restart on crash and on reboot | `systemctl is-enabled swarm-backend swarm-worker` and `systemctl show -p Restart swarm-backend` (shows `always`) |
| Daily cleanup of finished task folders (keeps queued, running and waiting tasks) | `systemctl list-timers swarm-cleanup.timer` and `sudo -iu agent python3 /opt/agent-swarm-kit/server/swarm_cleanup.py --dry-run` |
| Nightly database backup | `systemctl list-timers swarm-backup.timer` and `sudo ls /var/lib/agent-swarm/backups` |
| Off-box backup copy (optional) | Set `SWARM_OFFSITE_CMD` in `/etc/agent-swarm/swarm.env`, for example `rclone copy {dir} YOUR-REMOTE:swarm-backups` |
| Hourly disk alert to the Chief of Staff above 80 percent | `systemctl list-timers swarm-disk-alert.timer` |
| System log capped at 1 GB | `cat /etc/systemd/journald.conf.d/swarm.conf` |
| Swap present | `swapon --show` |
| Per-task CPU and memory limits | `SWARM_TASK_CPU_QUOTA` and `SWARM_TASK_MEMORY_MAX` in `swarm.env`; while a task runs, `sudo -iu agent systemctl --user list-units 'swarm-task-*'` |
| Tokens readable only by the right users | `ls -l /etc/agent-swarm /home/agent/.config/swarm` |

### An honest note about agent permissions

The worker runs Claude Code and Codex with permission prompts turned off (`bypassPermissions` and `--dangerously-bypass-approvals-and-sandbox`). There is nobody there to click "allow". That means **anything the `agent` user can do, an agent can do**. Keep that user small:

- No sudo. No root SSH keys in its home folder.
- GitHub access limited to the repos the swarm works on.
- No production database passwords, payment keys or cloud admin keys in its files or environment.
- The admin token stays in `/etc/agent-swarm/admin.token`, which `agent` cannot read. The agent can read only the worker token.

If a task needs a real secret, see [secrets.md](secrets.md).

## Optional: cap all tasks together

`server/systemd/agent-tasks.slice.example` caps the memory of all tasks together, so a runaway task cannot starve SSH, Tailscale or the backend. Copy it to `/home/agent/.config/systemd/user/agent-tasks.slice` if you want that extra backstop.
