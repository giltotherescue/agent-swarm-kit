# Worker server setup

> **This is the by-hand path.** The default now is [cloud-init](cloud-init.md): your Grok Bot creates the server and it sets itself up, with no SSH or `sudo` typed by you. Use this page if you want to do it yourself, if your provider has no cloud-init, or for a computer at home.

This doc takes you from "I have no server" to "a worker is running tasks safely". The worker is an always-on Linux server that runs Claude Code and Codex on your behalf. It also runs the task backend by default. Any cloud provider works. You need about an hour, most of it waiting for installs.

**Tested on:** version 0.3.0 passed a clean install on a fresh Ubuntu 24.04 server (DigitalOcean, 4 vCPU, 8 GB) on 2026-11-18. That run covered setup right after first boot, the smoke test (0 failed, 2 expected warnings), the admin user, firewall and SSH hardening, the lockdown refusal checks (and the lockdown rules themselves with a stand-in for Tailscale), an upgrade by running setup again, the project log, time limits and chains (with a stand-in for the model), and the task and cleanup flow. Version 0.3.1 fixes the five small defects that run found. **Not yet tested on a real server:** joining Tailscale (so also the real lockdown, Tailscale Serve and Tailscale SSH), real Claude and Codex logins and model tasks, GitHub login and repo pushes, and bot notify delivery. Earlier, version 0.2.0 had real bugs that 0.2.1 fixed, and 0.2.1 passed its own clean install the same day. Version 0.5.0 adds the cloud-init path, `swarmctl login` and the Mac worker; those are described, with what was and was not tested, in [cloud-init.md](cloud-init.md), the 0.5.0 entry of the changelog and [macos-worker.md](macos-worker.md). Run the script first on a server you can throw away and rebuild, and run the smoke test (step 9) afterwards.

If you do not want a server yet, read [grokbot-only.md](grokbot-only.md) first. You can add a worker later.

## Who this is for (the trust model)

The kit assumes **one trusted owner** (you) and a **dedicated worker** that runs nothing else. Inside that:

- a task cannot read or change other tasks;
- a project bot cannot see or act on another bot's tasks, items or projects;
- a task never holds the long-lived worker token or the GitHub login that pushes code.

It is not built for several people who do not trust each other sharing one worker. [secrets.md](secrets.md) has the details and the honest limits.

## 1. Pick a provider and a size

Any provider that gives you an Ubuntu LTS (Long Term Support) virtual machine works. A spare computer at home with Ubuntu 24.04 also works (for a Mac, see [macos-worker.md](macos-worker.md): a simpler but less isolated option); the README compares the two in [Where to run the worker](../README.md#where-to-run-the-worker), with prices. A few cloud providers that people use:

- Hetzner (good price for the power)
- DigitalOcean
- Vultr
- Linode (now part of Akamai)
- AWS Lightsail (simple Amazon option)

Start with **4 vCPU (virtual CPUs) and 8 GB of memory**, and at least 80 GB of disk. That runs two tasks at once. [sizing-and-sandboxes.md](sizing-and-sandboxes.md) helps you decide if you need more.

When you create the server:

- Choose **Ubuntu 24.04 LTS** (22.04 also works).
- Add your **SSH key** (Secure Shell key) at creation. Do not use a password.
- If the provider has a cloud firewall, allow SSH (port 22) only from your own IP address for now. You close it fully in step 10.
- Turn on the provider's snapshot or backup option if it is cheap. It is a nice safety net.

## 2. Get the kit onto the server

Log in as root (or the default sudo user) with the SSH key. You need a copy of the kit on the server. Pick the way that fits:

**A. The repo is public, or you have a release file.** Download it on the server (replace OWNER and the version):

```bash
curl -fsSL -o kit.tar.gz https://github.com/OWNER/agent-swarm-kit/archive/refs/tags/v0.2.1.tar.gz
mkdir agent-swarm-kit && tar -xzf kit.tar.gz -C agent-swarm-kit --strip-components=1
cd agent-swarm-kit
```

**B. You have a copy on your laptop** (for example a friend gave you access, or you cloned it there). Pack it on your laptop and copy it over with scp (secure copy, part of SSH). This needs no GitHub login on the server:

```bash
# on your laptop, inside your copy of the repo
git archive --format=tar.gz -o agent-swarm-kit.tar.gz HEAD
scp agent-swarm-kit.tar.gz root@YOUR-SERVER-IP:
# then on the server
mkdir agent-swarm-kit && tar -xzf agent-swarm-kit.tar.gz -C agent-swarm-kit
cd agent-swarm-kit
```

**C. The repo is private and you want to pull updates straight on the server.** Do not leave your own GitHub login on the server. Use a credential that can only read this one repo: a **deploy key** (an SSH key added to the repo's settings, with "Allow write access" left off), or a fine-grained token with read-only "Contents" access to this repo only. Then `git clone` with it. [updating-the-kit.md](updating-the-kit.md) has the details.

## 3. Make your admin user (if you only have root)

Many providers (DigitalOcean, Hetzner and others) give you only the `root` account. Later steps turn off root login over SSH, so you first need a normal user with sudo (admin rights) and your SSH key. Skip this if you already log in as a normal user with sudo, for example `ubuntu`.

The setup script can do this for you with `--admin-user NAME` (step 4): it makes the user, adds it to the `sudo` group, copies root's SSH keys to it, and asks you to choose a password for sudo. Or do it yourself as root (replace NAME):

```bash
adduser NAME                                         # asks for a password; sudo uses it, SSH does not
usermod -aG sudo NAME
rsync --archive --chown=NAME:NAME /root/.ssh /home/NAME   # copies root's SSH keys to NAME
```

Then, from your laptop, check that `ssh NAME@YOUR-SERVER-IP` works and that `sudo -v` there accepts the password. The setup script checks the same things before it turns off root login: the user exists, is not root, has a login shell, is in the `sudo` group, has a password (or a passwordless sudo rule), and has a non-empty `~/.ssh/authorized_keys` that only the user can change.

## 4. Run the setup script

```bash
sudo bash scripts/setup-worker.sh --admin-user NAME
```

**The script stops and waits at the Tailscale step.** Tailscale (a private network for your own devices) prints a login link, and the script waits, up to 15 minutes, until you open that link and approve the machine. Choose one:

- Stay at the screen and open the link when it appears. This is the default.
- `--auth-key FILE`: join with an auth key from the Tailscale admin console, saved in a file. Nothing waits. The key is never printed.
- `--tailscale later`: install Tailscale but do not join now. Nothing waits. Join any time after with `sudo tailscale up --ssh` (it prints the link), then do the lockdown step.
- `--no-tailscale`: skip Tailscale fully. The cost: you get no private SSH and no private way for bots to reach the backend, and `--lockdown` refuses to run, so SSH stays open to the whole internet. Only use it if you have another plan for that.

If you started without one of these and do not want to wait, press Ctrl-C and run it again with `--tailscale later`. Running it again is safe.

`--tags tag:swarm-worker` only works once your Tailscale access rules list `tag:swarm-worker` (see [Tailscale access rules](#tailscale-access-rules) below). Leave it off on the very first run if you have not set them up yet.

The screen shows one `==>` line per step. The long output of the package, Node.js, Tailscale and Claude Code installers goes to `/var/log/agent-swarm-setup.log`; look there if a step fails (`sudo tail -n 50 /var/log/agent-swarm-setup.log`). A first run takes about 10 minutes.

The script is safe to run again; a second run is an upgrade. It never prints a token. It ends with exit code 0 when all is done, 1 when it stopped on an error, and 3 when it finished but lists things you still need to do. What it does, in order:

1. Checks it runs as root on Ubuntu 22.04 or 24.04, and that the `agent` account has no sudo rights.
2. Installs updates and basic tools, and turns on **automatic security updates**. A brand new server often installs updates by itself in its first minutes (cloud-init, the first-boot setup). The script waits for that and for any other apt or dpkg run, up to 10 minutes, printing a progress line every 30 seconds. If apt still finds a lock, it tries again up to 4 times, 20 seconds apart, then stops with a clear message.
3. Caps the system log at 1 GB and adds a **swap file** (4 GB by default) if the box has none.
4. Installs the **GitHub CLI** (`gh`) and **Node.js LTS**.
5. Installs **Tailscale** and joins your tailnet (your private Tailscale network) with Tailscale SSH turned on, unless you chose `--tailscale later` or `--no-tailscale`.
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
13. Removes old release folders, keeping only the current and the previous one.
14. Makes the first database backup, if there is none yet.
15. Hardens SSH and the firewall as far as is safe before Tailscale works (see below).

What is hardened when the script ends:

- **Firewall (ufw) on:** incoming connections are blocked except SSH and Tailscale. SSH is **still open to the whole internet** on purpose, so you cannot lock yourself out before Tailscale works. Step 10 (lockdown) closes it.
- **Root login and password login over SSH off**, but only if a working admin user was confirmed: either you ran the script through sudo as that user, or you answered "yes" when it asked you to test `ssh NAME@...` and `sudo -v` in a second window. Otherwise root login stays on, the script says so at the end, and you can finish later with `sudo bash scripts/setup-worker.sh --harden --admin-user NAME`.

Options:

| Option | What it does |
| --- | --- |
| `--admin-user NAME` | Your normal login with sudo. Made if it does not exist, with root's SSH keys copied to it. Used to turn off root login safely |
| `--auth-key FILE` | Join Tailscale with an auth key read from a file, without a browser. The key is never printed. `--tailscale-authkey-file FILE` still works |
| `--tailscale later` | Install Tailscale but do not join, so nothing waits for a browser. Join later with `sudo tailscale up --ssh` |
| `--tags TAGS` | Tailscale tags to advertise, for example `tag:swarm-worker` |
| `--no-tailscale` | Skip Tailscale (same as `--tailscale off`). Costs you private SSH, and `--lockdown` refuses to run |
| `--no-claude` / `--no-codex` | Skip installing one of the model CLIs |
| `--swap-gb N` | Swap size in GB (default 4, `0` to skip) |
| `--max-wait SECONDS` | On upgrade, how long to wait for running tasks (default 3600) |
| `--force-os` | Run on an Ubuntu version other than 22.04 or 24.04 |
| `--harden` | Only run the hardening step above (firewall on, root login off once an admin works; public SSH stays open) |
| `--lockdown` | Only run the lockdown step (step 10) |

## 5. Sign in the model CLIs with your own subscriptions

The default is plain Claude Code and Codex signed in with your own subscriptions. No API keys.

**The easy way: the sign-in helper.** It starts each sign-in in the background and prints only the link and
code, so a bot (or you) can pass them on. Run it on the worker with `sudo`. Every sign-in is "open this link in
your browser", and Codex and GitHub also show a short code to type.

```bash
sudo swarmctl login codex
sudo swarmctl login claude        # prints a link; after you sign in, the page shows a code. Then:
echo -n "THE-CODE" | sudo swarmctl login claude --finish
```

Each command prints lines like `URL: ...`, `CODE: ...`, `EXPIRES: ...` and `NEXT: ...` (add `--json` for one JSON object).
`sudo swarmctl login NAME --status` says where it is, and `--cancel` stops it. A bot gets the Claude code from you
through the secret request form, never chat, and pipes it in on standard input.

| Tool | What the helper runs, as which user | Notes |
| --- | --- | --- |
| Codex | `codex login --device-auth` as `agent` | **Before you start,** turn on device-code login in ChatGPT: *Settings, Security* for a personal account, or your workspace admin does it under workspace permissions. Without it the code is rejected. The code lasts 15 minutes |
| Claude Code | `claude setup-token` as `agent` | Needs a Pro, Max, Ultra, Team or Enterprise plan. It makes a **one-year** token and the helper saves it as `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/agent-swarm/agent.env` (owned by root, readable by the `agent` group, never printed). Tasks get it through their environment. `claude --bare` ignores it, so scripts that use `--bare` need an API key instead |

Already have a token (made on your own computer with `claude setup-token`)? Send it through the secret form and run
`sudo swarmctl login claude --token-stdin` with the token on standard input. It is checked for shape and saved the same way.
The token can only make model requests. It lasts one year, so put a reminder in your calendar to make a new one.

**By hand, if you prefer.** The old way still works:

```bash
sudo -iu agent claude              # then type /login and follow the link
sudo -iu agent codex login --device-auth
```

Use `sudo -iu agent` exactly like this. The `-i` starts a login shell for `agent`, which reads its `~/.profile` and puts `~/.local/bin` (where Claude Code and Codex are installed) on the PATH (the list of folders searched for commands). With plain `sudo -u agent claude` you get "command not found". If you log in this way, a saved `/login` still works; the env token, when present, wins over it.

Heavy users with several subscriptions can add a proxy later. See [optional-services.md](optional-services.md).

## 6. Sign in GitHub as the runner (for repo work)

Only `swarm-runner` holds a GitHub login. Agents never do.

```bash
sudo swarmctl login github        # prints a link and a one-time code; also runs "gh auth setup-git" when done
```

That runs `gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key` as `swarm-runner`, then `gh auth setup-git`.
By hand: `sudo -u swarm-runner -H gh auth login` and `sudo -u swarm-runner -H gh auth setup-git`.

The device flow gives the runner whatever access the account you sign in with has. If you want a narrower login, use a
**fine-grained token** limited to the repos the swarm works on, with "Contents: read and write" and "Pull requests: read and write" only, and give it
to `gh auth login --with-token` instead. The runner pushes `agent/<task-id>` branches and opens draft pull requests. It never pushes the main branch unless a project allows it (`swarmctl project set NAME --allow-push-main`), and it never merges.

If you logged in GitHub as `agent` before, log it out: `sudo -iu agent gh auth logout`.

## 7. Start the worker

First [set up encrypted secrets](secrets.md#set-up-the-encrypted-repo) and run the first sync as `agent`.
Do this even if you only run one worker. Do not paste values into chat or the setup script.

```bash
sudo systemctl start swarm-worker
sudo swarmctl status
```

`swarmctl` as root uses the admin token through `/root/.config/swarm/client.env`, which the script wrote.

On a new box, `swarmctl status` shows one warning that is expected: "No Chief of Staff bot with a notify_url". It goes away when you register your Chief of Staff bot ([results-routing.md](results-routing.md)). In 0.2.0 a second warning, "The newest database backup is older than 36 hours", also showed on every new box; 0.2.1 makes a first backup during setup and does not warn in the first 36 hours. If you see a backup warning now, the nightly backup really is not running: `systemctl status swarm-backup`.

## 8. Let your bots reach the backend

Recommended: put Tailscale on each bot's computer, tag it `tag:swarm-bot`, and publish the backend to your tailnet only:

```bash
sudo tailscale serve --bg 8750
```

Then set `SWARM_PUBLIC_URL=https://YOUR-WORKER.YOUR-TAILNET.ts.net` in `/etc/agent-swarm/swarm.env` and `sudo systemctl restart swarm-backend`.

Option 2, only if a bot's computer cannot run Tailscale, is Tailscale Funnel (`sudo tailscale funnel --bg 8750`). Funnel puts the backend **on the public internet**, so the tokens become the only gate. Read the warnings in [backend.md](backend.md#option-2-tailscale-funnel-public-internet) first.

Then register your bots ([results-routing.md](results-routing.md)).

## 9. Run the smoke test

```bash
sudo bash /opt/agent-swarm-kit/current/scripts/smoke-test.sh
```

It checks users, file owners and modes, the sudo rule, services, timers and health, then runs one small script task (`echo ok`, no model, no cost) from queued to done. It prints PASS, WARN, FAIL or NOTE per line and never prints a token. Use `--no-task` to skip the task.

On a new box, before you log in GitHub and join Tailscale, a good result ends with `0 failed, 2 warnings`. The two warnings are expected:

- `WARN  swarm-runner is not logged in to GitHub` (step 6)
- `WARN  Tailscale is not connected` (until you join Tailscale, for example after `--tailscale later`)

You also see `NOTE  public SSH is still open` until you do step 10. Any `FAIL` line is a real problem.

## 10. Close public SSH (lockdown)

You need your admin user from step 3 first. Do this only after you can log in as that user over Tailscale from your laptop:

```bash
ssh NAME@YOUR-WORKER        # NAME is your admin user; YOUR-WORKER is the machine's Tailscale name
sudo bash /opt/agent-swarm-kit/current/scripts/setup-worker.sh --lockdown
```

The script first checks that a usable admin user exists (the same checks as in step 3). It refuses, and changes nothing, if the only account that can log in is root, and prints the three commands to make one. You can also pass `--admin-user NAME` to have it made. It refuses as well if Tailscale is not installed or not joined. Then it asks you to confirm you can log in as that user over Tailscale. Only then does it set the firewall (ufw) so SSH is reachable only over Tailscale, turn off password and root login for SSH, check the settings really took effect, and reload SSH. Also remove port 22 from your provider's cloud firewall. If something goes wrong, your provider's web console (serial console) is the way back in.

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

- Replace `you@example.com`, and `YOUR-ADMIN-USER` with the admin user from step 3 (a normal user with sudo, never `root`). The lockdown step prints the sample line with your user filled in.
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
| Encrypted runtime bundle is available without opening its values | `sudo -iu agent with-secrets --check tools` |
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

If this page does not solve your problem, email gilhildebrand@mail.grokbot.com with the kit version, what
happened and the exact error text, with any secrets removed.
