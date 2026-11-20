# Optional services

This page lists the outside services the swarm can use, why each one helps, and when you can skip it. Only two are needed for a worker: GitHub (if you do repo work) and Tailscale. Everything else is optional. Add a service when you feel the need for it, not before.

## GitHub

**What it is:** where your code lives.

**Why:** the worker clones repos (code repositories), works on a branch named `agent/<task id>`, pushes it, and can open a draft PR (pull request) for you to review. Without GitHub the worker can still do research, writing and script tasks, just not repo work.

**Setup:** log in once as the publisher user, not the agent user: `sudo -u swarm-runner -H gh auth login`, then `sudo -u swarm-runner -H gh auth setup-git`. The AI jobs never get this login; the publisher pushes for them. Prefer a fine-grained GitHub token limited to the repos the swarm works on over a full account login. A separate GitHub account for the swarm, added as a collaborator to those repos, keeps its access easy to see and easy to remove.

**Skip it if:** you have no code.

## Tailscale

**What it is:** a private network between your own devices. Your laptop, phone and the worker can talk to each other as if they were in the same room, without opening ports to the internet.

**Why:** it lets you reach the worker over SSH (secure shell, remote login) with public SSH closed, and it lets your bots reach the backend. The recommended way: install Tailscale on your bot's computer too (a Grok Bot box is a real Linux machine that can join your tailnet) and use Tailscale Serve, so the backend is never on the public internet. Tailscale Funnel (public) is a second option with warnings. See [worker setup](worker-setup.md) for both, the sample access rules, and the lockdown step.

**Skip it if:** you have no worker. With a worker, treat it as required.

## treg

**What it is:** a pay-per-call service for paid data that you would otherwise need a subscription for, such as SEO (search engine optimization) keyword data, social media data or contact enrichment.

**Why:** a bot or task can buy one lookup when it needs it, instead of you paying monthly for a tool you use twice. You pay per call, with no subscriptions.

**Skip it if:** you do not need paid data, or you already pay for those tools.

**Tip:** treat it like spending money. Put a monthly cap in the project brief, and have bots ask before large batches.

## Executor cloud

**What it is:** a host for MCP (Model Context Protocol) servers and custom tools. MCP is a standard way to give a model access to a tool or app.

**Why:** if you have your own tools or MCP servers (an internal database, a custom API, a niche app), Executor lets your bots and workers use them, with the logins kept in one secure place instead of in each bot.

**Skip it if:** your bots' built-in connectors already cover what you use. Most people can skip it.

## A per-bot email inbox

**What it is:** a separate email address for a bot.

**Why:** bots that sign up for services, receive verification codes, or get newsletters for research need somewhere to receive mail that is not your personal inbox. One inbox per bot keeps things separate and easy to shut off.

**Skip it if:** your bots never sign up for anything.

**Careful:** email is untrusted input. Tasks seeded from email should be queued with `--untrusted-input`, which blocks pushing. Sending mail from that inbox to people still follows the [approval rules](approvals.md).

## CLIProxyAPI (advanced)

**What it is:** a local proxy that sits between the model CLIs (command line interfaces) and the providers, and spreads requests over several Claude or ChatGPT subscriptions.

**Why:** heavy users who run many tasks a day can hit the limits of one subscription. A proxy lets work continue on another account.

**Before you use it:**

- Check each provider's terms of service. Some limit how subscriptions may be shared or automated. You are responsible for staying within them.
- It adds a moving part. When it is down, every task fails.
- Never restart it while tasks are running. A restart drops live model streams and those tasks fail. Its rate-limit cooldown reflects the real provider limit, so restarting does not clear anything. Use the same pattern as the worker: `sudo swarmctl drain`, wait until `swarmctl status` shows nothing running, restart the proxy, then `sudo swarmctl undrain`.

**Skip it if:** one Claude subscription plus one ChatGPT subscription covers your work. The default setup (plain CLIs, your own logins, Codex as fallback) is enough for most people.

**Setup:** `scripts/setup-cliproxy.sh` (below) installs it the safe way and sets up the web panel. Pointing Claude Code and Codex at the proxy is a separate step and stays manual: they keep using their own logins until you set their base URL settings. Tasks run with a clean environment, so put those base URL settings in `/etc/agent-swarm/agent.env` (one `KEY=VALUE` per line, owned by root, readable by the `agent` group).

### The web panel, over Tailscale only (advanced)

The proxy has a web page, the management panel, where you add Codex and Claude accounts by signing in with your browser. It is the easy way to add several accounts. The setup keeps it private:

- The proxy listens on `127.0.0.1` only (`server.host: "127.0.0.1"`), and `management.allow-remote` stays `false`.
- The panel's password (the management key) comes from the secret request form into a file, and the installer copies it into the config. It is never in chat or on a command line. The proxy replaces it with a hash when it starts.
- A small gate (`cliproxy-gate`, on `127.0.0.1:8318`) passes only the panel page and the management API to the proxy and answers 404 for everything else, so the model API (`/v1/...`) is never reachable from your tailnet.
- `tailscale serve` publishes the gate, and only to your tailnet, never to the internet (not `tailscale funnel`).

```bash
# the bot saves the key from the secret form to a file with mode 600, then, on the worker:
sudo bash /opt/agent-swarm-kit/current/scripts/setup-cliproxy.sh --management-key-file /root/cliproxy.key --serve
```

The key must be 16 to 200 plain characters. `--serve` runs `tailscale serve --bg --https=8444 http://127.0.0.1:8318`; the panel is then at `https://YOUR-WORKER.YOUR-TAILNET.ts.net:8444/management.html`. The script downloads the release, checks it against the release's `checksums.txt`, installs it under `/opt/cliproxyapi` and runs it as its own `cliproxy` user with a systemd service. It does not restart a proxy that is already running.

**Add accounts in the panel.** Open the panel from a computer on your tailnet, sign in with the management key, go to *OAuth Login*, pick Codex or Claude. The panel shows a sign-in link; open it and sign in. The provider then sends your browser to a `http://localhost:PORT/...` address (Codex uses port 1455, Claude 54545). **Nothing is listening there on your computer, so the page will fail to load. That is expected.** Copy the whole address from the browser's address bar, paste it in the panel's *Callback URL* box and press *Submit Callback URL*. The panel finishes the sign-in. Repeat for each account. Credentials are stored under `/var/lib/cliproxyapi/auth`.

**Never restart the proxy while tasks are running.** A restart drops live model streams and those tasks fail. That includes `systemctl restart cliproxyapi`, an upgrade of the proxy, and a reboot of the worker with tasks running. Drain first and restart when idle: `sudo swarmctl restart-when-idle --also cliproxyapi`. Adding accounts in the panel should not need a restart, but we did not verify that; if it seems to, use the drain command above.

**What was tested and what was not.** Tested: the gate, with a stand-in upstream (what it passes, what it refuses, path tricks, dead upstream), and the installer's config and key checks. Also done by hand with the real CLIProxyAPI 8.0.79 (the default version) on Linux: it started with the config the installer writes, listened only on `127.0.0.1`, rewrote the key as a hash at start, served the panel, and the panel logged in and loaded its pages **through the gate** in a real browser; the panel's Codex sign-in window opened with the link and the callback box. **Not tested:** `tailscale serve` itself with this setup, the systemd units, a full OAuth sign-in with a real Codex or Claude account (so the paste-the-callback step is checked only as far as the panel's box and the proxy's `oauth-callback` endpoint existing), and pointing the CLIs at the proxy. The panel and its API belong to the CLIProxyAPI project and can change between its releases; the default version is pinned for that reason.

## Cloudflare

**What it is:** a hosting platform. Its Workers product runs small web services, and D1 is its database.

**Status: design only, not built.** There is no Cloudflare Worker, D1 database setup or deploy file in this kit. Do not plan on it as a setup path today.

**Why it might come later:** to keep task tracking working while the worker is down. The design is written up in [backend](backend.md).

**Skip it:** the backend runs on the worker, and that is the only path that ships.

## Adding a service to a bot

When you add any service, write it into the bot's [project brief](../templates/project-bot-brief.md) under "Connectors and apps": what it is for, which bot may use it, any spending cap, and where its login lives (by path or name, never the value; see [secrets](secrets.md)).
