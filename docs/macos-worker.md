# A Mac as a worker (optional, simpler, less isolated)

A spare Mac, or a Mac at home that stays on, can be a worker. It is simpler to set up than the Ubuntu path:
one user (you), Homebrew, and launchd (the Mac's service manager). No `sudo`, no SSH setup, no firewall
steps.

**It is less isolated than the Ubuntu worker, on purpose. Read this before you choose it.**

| | Ubuntu worker | Mac worker |
| --- | --- | --- |
| Users | `agent` runs jobs, `swarm-runner` holds the GitHub login and worker token, `swarm` runs the backend | **One user, you.** Jobs run as you |
| What a job can read | Only the `agent` user's files | **Everything you can**: your files, your GitHub login, your model logins, your keychain |
| Memory and CPU caps per job | Yes (systemd scopes) | **None.** A runaway job can slow the Mac down |
| Jobs cannot reach the worker token or GitHub login | True | **False.** They share your user |
| Setup | Script run as root | Script run as you |

So use a Mac that does nothing else: a spare Mac mini, an old laptop with its own user account, a home
machine you do not keep private files on. Do not use your everyday laptop. The kit's promise that "a job
never holds the long-lived worker token or the GitHub login" does **not** hold here; the trust model in
[secrets.md](secrets.md) applies only to the Ubuntu path. For code that matters, or for anything touching
other people's data, use Ubuntu.

## Before you start

- macOS with Homebrew installed ([brew.sh](https://brew.sh); its own installer asks for your Mac password
  once). The Command Line Tools too (`xcode-select --install`): the kit's helper programs use
  `/usr/bin/python3`.
- A user account that logs in automatically and does not sleep. A LaunchAgent runs only while its user is
  logged in. In System Settings turn on automatic login for that user, and set the Mac to stay awake
  (and to start up after a power cut).
- Either a backend elsewhere (an Ubuntu worker, see [multi-worker.md](multi-worker.md)), or let this Mac keep the queue too (`--with-backend`).

## Install

From a copy of this repo, as your normal user (never with `sudo`; the script refuses):

```bash
# a second worker for a backend that runs somewhere else (the token file comes from that backend, over a private channel)
bash scripts/setup-macos.sh --backend-url https://BACKEND-PRIVATE-ADDRESS --worker-token-file ~/worker.token --worker-name spare-mac

# or: this Mac does everything (backend and worker)
bash scripts/setup-macos.sh --with-backend
```

It installs `coreutils git gh node jq sops age` with Homebrew, Claude Code and Codex for you, puts the kit under
`~/.local/share/agent-swarm-kit`, settings under `~/.config/agent-swarm` (all mode 600), and writes the launchd
agents. Run it again to upgrade; it keeps your settings and tokens. `--uninstall` removes the agents and leaves your data.

| launchd agent | What it does |
| --- | --- |
| `com.agent-swarm-kit.worker` | The runner, always on, restarted if it stops. Runs the secrets sync first, like the Linux service |
| `com.agent-swarm-kit.sync` | Pulls and decrypts the runtime secret bundles at login and every 10 minutes |
| `com.agent-swarm-kit.cleanup` | Removes old finished task folders, daily at 03:30 |
| `com.agent-swarm-kit.backend` | Only with `--with-backend`: the task backend |

Logs are in `~/Library/Logs/agent-swarm-*.log`. All four run the same small program,
`~/.local/lib/agent-swarm/swarm-launchd ROLE`. It loads `~/.config/agent-swarm/swarm.env` (launchd has no
environment file of its own) and puts Homebrew and GNU coreutils first on the PATH, because the runner needs GNU `timeout`.

## Sign in (once, on the Mac itself)

The Mac has a screen and a browser, so there is no helper: sign in the normal way in Terminal.

```bash
claude                          # then /login
codex login --device-auth       # turn on device-code login in ChatGPT Security Settings first
gh auth login && gh auth setup-git
```

For Claude you can also run `claude setup-token` and put the token in `~/.config/agent-swarm/agent.env` as
`CLAUDE_CODE_OAUTH_TOKEN=...` (one `KEY=VALUE` per line, mode 600). `--bare` mode ignores it.

## Secrets

The installer makes this Mac its own age key (`~/.config/swarm-secrets/age.key`, private, mode 600) and prints
the public half. Add that public key as a recipient and set up the read-only checkout exactly as in
[secrets.md](secrets.md#add-a-machine-rotate-or-recover); the Mac keeps its checkout in `~/.config/swarm-secrets/repo`. The `sync` agent keeps the
cache fresh, and `with-secrets` works the same as on Linux.

## What works differently

- **Restarting while idle.** `swarmctl restart-when-idle` calls systemd, so on a Mac do it by hand: `swarmctl drain`, wait until
  `swarmctl status` shows nothing running, `launchctl kickstart -k gui/$(id -u)/com.agent-swarm-kit.worker`, `swarmctl undrain`.
- **Memory check.** The runner reads `vm_stat` and takes new work only when about 1.5 GB is free (`SWARM_MIN_FREE_MEM_MB`). It starts with
  one task at a time (`SWARM_MAX_PARALLEL=1`); raise it in `swarm.env` if the Mac has room.
- **No limits per job.** `SWARM_TASK_SCOPE=0`. Keep `SWARM_TASK_TIMEOUT` sensible.
- **Reaching the backend** (with `--with-backend`): install the Tailscale app, then publish port 8750 as in [backend.md](backend.md).
- **Not included:** the nightly database backup and the disk alert timers (use Time Machine, or run `scripts/backup.sh` from your own launchd job), and the Linux hardening and lockdown steps.

## Tested, and not

Tested here, on Linux: the setup script with stand-ins for `launchctl` and Homebrew (files, modes, settings, upgrade, uninstall, and the
second-worker rules); the plist files parse with a standard plist reader; the launchd wrapper loads settings, orders the secrets sync before
the runner, and stops if the sync fails; the `vm_stat` parser, the Mac PATH and the `gtimeout` fallback; and a real single-user run, started through
the same wrapper launchd uses: backend, runner, and one script task from queued to done.

**Not tested on a real Mac:** `launchctl bootstrap` itself, the plists loading under launchd, Homebrew installs, the Claude and Codex installers on macOS,
`vm_stat` output on a real machine (the parser was written against the documented format), the login-session behaviour, and Apple silicon
versus Intel differences. The helper programs also need Python 3.9 or newer (the Command Line Tools have it).
Try it on a Mac you can reset, and tell us what breaks.
