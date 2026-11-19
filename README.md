# agent-swarm-kit

**A small team of AI helpers that keeps your projects moving while you get on with your day.** You chat
with a few Grok Bots that plan, remember and ask you only what matters, and an optional always-on worker
does the long, heavy jobs for them on the Claude and ChatGPT subscriptions you already pay for.

> **Easiest start:** tell your Grok Bot
> *"Set me up with this repo: https://github.com/giltotherescue/agent-swarm-kit"*.
> It follows [SETUP-WITH-GROKBOT.md](SETUP-WITH-GROKBOT.md) and walks you through every step, one
> plain question at a time.

**Contents:** [What you get](#what-you-get) ·
[How it works](#how-it-works-in-four-pictures) ·
[**What you need**](#what-you-need) ·
[Where to run the worker](#where-to-run-the-worker) ·
[Set it up with your Grok Bot](#set-it-up-with-your-grok-bot-recommended) ·
[Which tool when](#which-tool-when-short-version) ·
[Set it up by hand](#set-it-up-by-hand) ·
[Trust model](#trust-model) ·
[What is in the box](#what-is-in-the-box) ·
[Docs](#docs) ·
[The rules](#the-rules-in-one-breath) ·
[Version](#version)

## What you get

- **A Chief of Staff bot.** One bot you talk to about everything. It keeps track of what needs you and
  sends you one short list of decisions each morning.
- **A project bot for each project.** A business, a website, an app, your marketing. Each one owns its
  project from end to end and knows its goals, people and limits.
- **A morning review call.** You can also talk the morning list through out loud.
  [More below](#the-morning-review-call).
- **A weekly goals meeting and a daily work heartbeat.** Clear goals each week, a quiet daily check, and
  a nudge only when something is stuck.
- **A project log.** Every project, with or without code, keeps a history of what was done, what you
  decided, and your feedback, so bots learn what you like.
- **A worker that does the heavy lifting (optional).** A rented server that runs long jobs, code changes,
  research and anything that needs a strong or specific AI model, while you sleep if you like.
- **Results that come back to the right bot.** When a job finishes, the bot that asked for it gets the
  result directly.
- **One "Waiting on you" list.** Every decision that needs you lands in one place. You can answer, snooze
  or drop each one. Dropping is fine.
- **Light rules.** Nothing goes outside, nothing is deleted, and no money is spent without your yes.
  Everything else just gets done.

### The morning review call

Overnight and through the day, your bots file every open question and action item on the Waiting on
you list. Each morning the Chief of Staff rolls them all up into one list. If you would rather talk
than type, start a voice conversation with the Chief of Staff in the Grok Bot app and go through the
items back and forth, hands free, for example on a drive. You can ask what an item means, push back
on a recommendation, or say "drop it". When the call ends, the Chief of Staff writes each answer to its
item on the list (answered, snoozed or dropped), which also logs it as a decision for that project, and
messages the project bots so they get back to work on it. A worker task that was waiting on one of those answers picks up again on its own.

## How it works, in four pictures

Orange outlines mark where you act. The longer story is in [docs/how-it-works.md](docs/how-it-works.md).

### Who does what

<img src="docs/images/who-does-what.png" width="800" alt="Four columns: you, the Chief of Staff, one project bot per project, and the worker that runs Claude Code or Codex.">

Results come straight back to the bot that asked. [See the step-by-step swimlane](docs/how-it-works.md#who-does-what).

### A request's journey

<img src="docs/images/request-journey.png" width="800" alt="You ask, the bot plans, the worker does it, you review a draft pull request.">

The worker opens a draft pull request; only you merge. [See the step-by-step swimlane](docs/how-it-works.md#a-requests-journey).

### Where decisions land

<img src="docs/images/decisions-and-goals.png" width="800" alt="Bots file decisions on one Waiting on you list, the Chief of Staff sends one morning message, you reply in one line or talk it through on a voice call, and a weekly goals meeting sets 1 to 3 goals per project.">

Answer in one line, or on a [voice call](#the-morning-review-call). [See the step-by-step swimlane](docs/how-it-works.md#where-decisions-land).

### Which tool when

<img src="docs/images/which-tool-when.png" width="800" alt="Four routes: the bot itself, Grok Bot connectors, treg, and the worker.">

Also as a [table below](#which-tool-when-short-version). [See the step-by-step swimlane](docs/how-it-works.md#which-tool-when).

## What you need

New to all of this? This is every tool and service the kit uses, in plain words. You can start with
just a Grok Bot and add the rest later.

**Key:** ✅ Essential · 🖥️ Essential once you add a worker · ➕ Nice to have

| Tool | What it is | Why the kit uses it | Need it? |
| --- | --- | --- | --- |
| [Grok Bot](https://docs.x.ai/grok-bot/overview) | xAI's always-on AI helpers that you chat with | Your Chief of Staff and project bots live here | ✅ Essential |
| A worker computer | A computer that stays on all day (see [where to run it](#where-to-run-the-worker)) | Runs the long, heavy jobs and all coding | ➕ Nice to have (skip it at first) |
| [Ubuntu 24.04](https://ubuntu.com/download/server) | A free operating system for servers | The setup script only runs on Ubuntu (22.04 or 24.04) | 🖥️ Essential |
| [Claude subscription](https://claude.com/pricing) and [Claude Code](https://claude.com/product/claude-code) | Anthropic's AI, and its tool that writes code | The worker's main AI for coding and research | 🖥️ Essential |
| [ChatGPT subscription](https://chatgpt.com/pricing) and [Codex](https://chatgpt.com/codex) | OpenAI's AI, and its tool that writes code | Backup when Claude hits its limit, and second opinions | ➕ Nice to have |
| Codex image generation | Picture making built into Codex | Ad graphics and mockups on your ChatGPT plan, no image API key ([how](docs/model-routing.md#images)) | ➕ Nice to have |
| [Tailscale](https://tailscale.com) | A private network for your own devices | You and your bots reach the worker without opening it to the internet | 🖥️ Essential |
| [GitHub](https://github.com) | Where code lives online | The worker saves code changes there as draft pull requests for you to review | 🖥️ Essential if you have code |
| [treg](https://treg.to) | Pay per call access to paid data | SEO and contact data without a monthly plan; also reaches your Google Search Console and Google Ads | ➕ Nice to have |
| [Executor](https://executor.sh) | Keeps logins for your own tools in one place (cloud or self-hosted) | Lets a worker job use your connected tools too | ➕ Nice to have (most people skip it) |
| An email inbox per bot | A separate email address for one bot | Sign-ups and codes stay out of your own inbox | ➕ Nice to have |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | A helper that spreads AI requests over several subscriptions | For heavy users who hit one plan's limits ([read the cautions first](docs/optional-services.md#cliproxyapi-advanced)) | ➕ Nice to have (advanced) |
| [Vercel](https://vercel.com) (or a similar web host) | A service that hosts websites | Puts your website online. It hosts websites, not the worker | ➕ Nice to have (only for websites) |

More on each one: [optional services](docs/optional-services.md) and [which tool when](docs/which-tool-when.md).

### Where to run the worker

The worker can be a rented cloud server or a computer in your home. Both work the same once set up.

| | ☁️ Cloud server | 🏠 Computer at home |
| --- | --- | --- |
| **Always on** | Yes, built for it | Yes, if you turn off sleep (and, on a laptop, sleep when the lid closes) |
| **Power and internet cuts** | Not your problem | A cut stops the worker. Set it to turn back on after a power cut (a BIOS setting) |
| **Power bill** | Included in the price | Small for a mini PC, a bit more for an old laptop |
| **Reaching it from anywhere** | Tailscale | Tailscale, so there is nothing to set up on your router |
| **Setup** | Click to create, about an hour | Install Ubuntu first, then the same steps |
| **Cost** | Monthly fee (below) | Free if you already own it, or a one-time purchase |
| **Best for** | Most people | You have a spare computer, or want no monthly bill |

**Operating system:** the kit is tested on Ubuntu 24.04. **macOS is not supported today**: the setup
script stops on anything other than Ubuntu. So for a home worker, put Ubuntu 24.04 on an old laptop or
a mini PC. A Mac mini can't run the kit directly (running Ubuntu in a virtual machine on it is untested).

**Cloud servers.** Any provider with Ubuntu works. Start with 4 CPUs and 8 GB of memory
([sizing](docs/sizing-and-sandboxes.md)). Prices are from each provider's own pricing page, checked on
2026-11-18; they change, so check before you buy.

| Provider | A plan that fits | Price per month |
| --- | --- | --- |
| [DigitalOcean](https://www.digitalocean.com/pricing/droplets) | Basic Droplet, 4 CPUs, 8 GB, 160 GB disk | $48 |
| [Hetzner](https://www.hetzner.com/cloud/regular-performance) | CPX32 (Europe) or CPX31 (USA), 4 CPUs, 8 GB, 160 GB disk | €30.49 to €35.99 in Europe, €62.99 in the USA, plus VAT |
| [Akamai (Linode)](https://www.akamai.com/cloud/pricing/cpu) | Linode 8 GB shared, 4 CPUs, 160 GB disk | $48 |
| [AWS Lightsail](https://aws.amazon.com/lightsail/pricing/) | 8 GB with 2 CPUs (160 GB disk), or 8 GB with 4 CPUs (320 GB disk) | $44, or $84 for 4 CPUs |
| [boat](https://boat.dev) | Ubuntu VMs made for AI agents; "default" size, 4 CPUs, 8 GB, 51 GB disk. Turn off auto-stop (paid plan only). Europe only. Not tested with the kit yet | $0.036 an hour, so about $26 if never stopped ($20 minimum) |
| ⚙️ Advanced: [Google Cloud Compute Engine](https://cloud.google.com/products/compute) or [AWS EC2](https://aws.amazon.com/ec2/) | Powerful, but harder to set up and their bills have many parts. Try a simpler host above first | Depends on many options ([Google](https://cloud.google.com/compute/vm-instance-pricing), [AWS](https://aws.amazon.com/ec2/pricing/)) |

**A computer at home.** Any of these works once Ubuntu 24.04 is on it:

| Computer | Good | Watch out for |
| --- | --- | --- |
| An old laptop you no longer use | Free, and its battery rides out short power cuts | Turn off sleep on lid close; older ones can be slow or loud |
| A new mini PC, like a [Beelink](https://www.bee-link.com) or an Intel NUC | Small, quiet, low power | Pick one with at least 8 GB of memory and 256 GB of disk |
| A [Mac mini](https://www.apple.com/mac-mini/) | Small, quiet, low power | macOS is not supported (see above); not a good pick today |

## Set it up with your Grok Bot (recommended)

1. Open a chat with the Grok Bot you want as your Chief of Staff (any bot will do; it can be a new one).
2. Say: *"Set me up with this repo: https://github.com/giltotherescue/agent-swarm-kit"*. If the repo is
   private, give the bot read access first, or upload a copy of it.
3. Answer its questions. For each choice it explains the options in a sentence or two and tells you
   what it recommends. Saying "use your recommendation" is always fine.

The bot never asks you to paste a password or key into chat (it uses Grok Bot's secret request form),
and it asks before anything that costs money, like renting a server. Plan on about an hour without a
worker, or an afternoon with one.

## Which tool when (short version)

| Use | For | Examples |
| --- | --- | --- |
| **Grok Bot connectors** | Your own accounts, quick reads and drafts | Gmail, Slack, your calendar, analytics |
| **treg** | Paid data with no subscription, and some private tools it connects to | Keyword and SERP data, contact enrichment, Google Search Console, Google Ads |
| **The worker** | All coding (any size), jobs over about 15 minutes, design, deep research, bulk scraping, a specific model | A pricing page change, a 40-page competitor study, a second-model review |
| **A chain on the worker** | Jobs that run for days as their own orchestrator, in short cycles that hand on a checkpoint ([long-running jobs](docs/long-running-jobs.md)) | A "polisher" that keeps walking the app, fixing bugs and testing again |
| **The bot itself** | Quick lookups, coordination, messages | "What did we decide about the logo?", replying in Slack |
| **Executor** | Later, only if the worker needs your connected tools too | A worker task that must read your CRM |

## Set it up by hand

Prefer to do it yourself? Two paths. You can start with the first and add a worker later; nothing has
to be redone.

### Path A: Grok Bot only (no server, about an hour)

1. Create a Chief of Staff bot. Paste [templates/chief-of-staff.md](templates/chief-of-staff.md) into its
   instructions.
2. Create one project bot. Fill in [templates/project-bot-brief.md](templates/project-bot-brief.md) as its
   brief.
3. Add the routines from [templates/routine-prompts.md](templates/routine-prompts.md): the morning
   decisions, the nightly cleanup, the weekly goals meeting and the daily work heartbeat.
4. Read [docs/grokbot-only.md](docs/grokbot-only.md) for what works, what does not, and how to add a
   worker later.

### Path B: Grok Bot plus a worker (about half a day)

You will type commands on the server. The CLI (command line interface, a program you run by typing) for
the swarm is called `swarmctl`. A token is a long random password that a bot or program uses to prove who
it is. Tailscale is a private network that links your own devices, so nothing has to be open to the
whole internet.

1. Rent an Ubuntu LTS server (4 vCPU, 8 GB to start), or put Ubuntu 24.04 on a spare computer at home
   ([where to run the worker](#where-to-run-the-worker)). See [docs/worker-setup.md](docs/worker-setup.md).
2. Get the kit onto the server. If you have no copy there, download a release file, or pack your own
   copy with `git archive` and send it with `scp` (secure copy). For a private repo, use a read-only key,
   never your own GitHub login. All three ways are in
   [docs/worker-setup.md](docs/worker-setup.md), step 2.
3. If the server gave you only `root`, pick a name for your own admin user (a normal login with sudo).
   The script makes it for you with `--admin-user NAME`. Later steps turn off root login, so you need it.
4. Run the setup script:

   ```bash
   sudo bash scripts/setup-worker.sh --admin-user NAME --tailscale later
   ```

   **Heads up: without `--tailscale later` the script stops at the Tailscale step and waits** (up to
   15 minutes) until you open the link it prints and approve the server. That is fine if you are at the
   screen. With `--tailscale later` nothing waits; you join afterwards with `sudo tailscale up --ssh`.
   `--no-tailscale` skips Tailscale fully, but then public SSH can never be closed (step 7).
5. When the script has finished, log in to Claude Code and Codex as the `agent` user (the user that runs
   the AI jobs), and GitHub as the `swarm-runner` user (the user that pushes code; the jobs themselves
   never get your GitHub login). Then start the worker and run the smoke test (a quick check that
   everything is in place):

   ```bash
   sudo -iu agent claude            # then /login
   sudo -iu agent codex login --device-auth
   sudo -u swarm-runner -H gh auth login
   sudo -u swarm-runner -H gh auth setup-git
   sudo systemctl start swarm-worker
   sudo bash scripts/smoke-test.sh
   ```

   On a new box, expect `0 failed` with up to 2 warnings (GitHub login, Tailscale) until you do those.
6. Join Tailscale if you have not yet, and let your bots reach the worker. The recommended way: put
   Tailscale on your bot's computer and use Tailscale Serve on the worker, so it is never on the public
   internet ([docs/worker-setup.md](docs/worker-setup.md)). Then register each bot's own inbox
   ([templates/bot-setup-checklist.md](templates/bot-setup-checklist.md)).
7. Close public SSH (remote login from the internet) once you can log in as your admin user over
   Tailscale: `sudo bash scripts/setup-worker.sh --lockdown`.
8. Follow the [first week walkthrough](docs/first-week.md) to take one task from start to finish.

**Honest note (tested on):** version 0.3.0 passed a clean install on a fresh Ubuntu 24.04 server on
2026-11-18: setup, smoke test, admin user, firewall and SSH hardening, lockdown refusal checks, an upgrade
by running setup again, the project log, time limits and chains (with a stand-in for the model), and the
task and cleanup flow. Version 0.3.1 fixes the five small defects that run found. Not yet tested on a real
server: joining Tailscale (so the real lockdown, Tailscale Serve and Tailscale SSH), real Claude and Codex
logins and model tasks, GitHub login and repo pushes, and bot notify delivery. Run
`scripts/smoke-test.sh` after setup and tell us what fails.

## Trust model

This kit is for **one trusted owner** (you) running a **dedicated worker** (a server used only for this).
The bots are your own bots. It is not a shared service for strangers. Within that:

- One job on the worker cannot read or change another job.
- A project bot cannot see or act on another bot's jobs, or read another project's log.
- Jobs never hold the worker's long-lived key or your GitHub login. The worker itself pushes code and
  opens draft pull requests after checking the rules.

Details are in [secrets](docs/secrets.md).

## What is in the box

| Folder | What it holds |
| --- | --- |
| [docs/](docs/) | How everything works and how to set it up |
| [templates/](templates/) | Bot instructions, project brief, setup checklist, routine prompts, model routing table, review prompt, task brief |
| [server/](server/) | The task tracker, the worker, the `swarmctl` command line, cleanup, the `with-secrets` helper, service files |
| [scripts/](scripts/) | Setup script for a fresh server, smoke test, backup, disk alert, secret scanner, restart when idle |
| [config/swarm.env.example](config/swarm.env.example) | The one settings file, with placeholders only |
| [tests/](tests/) | Tests: `python3 -m unittest discover -s tests` |

Everything on the server uses only Python 3 and its standard library, with SQLite for storage. There is
no web dashboard on purpose; `swarmctl status` shows what matters on one screen. Running the task tracker
on Cloudflare instead of the worker is a design only, not built.

## Docs

Start with the concepts, then follow your path.

- [Set it up with your Grok Bot](SETUP-WITH-GROKBOT.md): the guided path, every choice explained.
- [Concepts](docs/concepts.md): bots, tasks, states, reports, the Waiting on you list.
- [Which tool when](docs/which-tool-when.md): connectors, treg, the worker or Executor.
- [Goals meeting and work heartbeat](docs/goals-and-heartbeat.md): how bots keep moving each week.
- [Project log](docs/project-log.md): items with status and version, decisions, feedback, and a short rules doc per project.
- [Long-running jobs](docs/long-running-jobs.md): jobs that run for days, as a chain of short cycles.
- [Grok Bot only setup](docs/grokbot-only.md): no server needed.
- [Worker server setup](docs/worker-setup.md): any provider, Tailscale, and the security baseline.
- [Sizing and sandboxes](docs/sizing-and-sandboxes.md): how big a server, and when to offload tests.
- [How it works, in four pictures](docs/how-it-works.md): the long version of the README diagrams.
- [How it works, technical](docs/how-it-works-technical.md): flow charts and states for developers.
- [Backend](docs/backend.md): the task tracker, its API and who can call what.
- [Results routing](docs/results-routing.md): straight to the owner bot, with a fallback.
- [Model routing](docs/model-routing.md): which model for which job.
- [Approvals](docs/approvals.md): a light default you can loosen.
- [Secrets](docs/secrets.md): the trust model, where keys live, and the `with-secrets` helper.
- [Usage and cost](docs/usage-and-cost.md): keep bots quiet and spend low.
- [Optional services](docs/optional-services.md): GitHub, Tailscale, treg, Executor, email inboxes, the
  multi-subscription proxy.
- [Routines](docs/routines.md): morning decisions, nightly memory cleanup, result handling.
- [First week](docs/first-week.md): one task end to end.
- [Updating the kit](docs/updating-the-kit.md): pull improvements and send yours back.
- [Troubleshooting](docs/troubleshooting.md).

## The rules in one breath

Bots plan, the worker works. Results go to the bot that owns the task. Nothing goes outside, nothing is
deleted, and no money is spent without your yes; another bot's yes does not count. Secrets never go in
chat. Bots stay quiet unless something changed. Anything for you lands on one list, and dropping an item
from it is fine.

## Version

See [VERSION](VERSION) and [CHANGELOG.md](CHANGELOG.md). License: [MIT](LICENSE).
