# Optional services

This page lists the outside services the swarm can use, why each one helps, and when you can skip it. Only two are needed for a worker: GitHub (if you do repo work) and Tailscale. Everything else is optional. Add a service when you feel the need for it, not before.

## GitHub

**What it is:** where your code lives.

**Why:** the worker clones repos (code repositories), works on a branch named `agent/<task id>`, pushes it, and can open a draft PR (pull request) for you to review. Without GitHub the worker can still do research, writing and script tasks, just not repo work.

**Setup:** log in once as the agent user: `sudo -iu agent gh auth login`. Give that login access only to the repos the swarm should touch. A separate GitHub account for the swarm, added as a collaborator to those repos, keeps its access easy to see and easy to remove.

**Skip it if:** you have no code.

## Tailscale

**What it is:** a private network between your own devices. Your laptop, phone and the worker can talk to each other as if they were in the same room, without opening ports to the internet.

**Why:** it lets you reach the worker over SSH (secure shell, remote login) with public SSH closed, and it lets your bots reach the backend through Tailscale Funnel or Serve. See [backend](backend.md) for the exposure options and their tradeoffs, and [worker setup](worker-setup.md) for the lockdown step.

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
- Never restart it while tasks are running. A restart drops live model streams and those tasks fail. Its rate-limit cooldown reflects the real provider limit, so restarting does not clear anything.

**Skip it if:** one Claude subscription plus one ChatGPT subscription covers your work. The default setup (plain CLIs, your own logins, Codex as fallback) is enough for most people.

**Setup:** not included in the kit. Follow the project's own instructions, keep it bound to `127.0.0.1` (only reachable from the box itself), and point the CLIs at it through their base URL settings.

## Cloudflare

**What it is:** a hosting platform. Its Workers product runs small web services, and D1 is its database.

**Why:** only if you want the backend to live off the worker, so task tracking survives the worker being down. See [backend](backend.md) for that option.

**Skip it if:** you are fine with the default, where the backend runs on the worker.

## Adding a service to a bot

When you add any service, write it into the bot's [project brief](../templates/project-bot-brief.md) under "Connectors and apps": what it is for, which bot may use it, any spending cap, and where its login lives (by path or name, never the value; see [secrets](secrets.md)).
