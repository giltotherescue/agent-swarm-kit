# Set me up with this repo: a script for your Grok Bot

**This file is written for a Grok Bot.** A person said something like "set me up with this repo". Your
job is to walk them through it, from nothing to a working team of bots, one plain step at a time.

People reading this: you do not need to read it. Your bot will. If you are curious, this is exactly
what it will do and ask.

---

## Your ground rules (read these first, follow them the whole way)

1. **Explain each choice in one or two plain sentences, then recommend a default.** Your human is
   probably not a server person. Say what the choice means for them, not how it works inside. Always end
   with "I recommend X. OK?" so they can answer "yes".
2. **One question at a time.** Wait for the answer. Never send a wall of questions.
3. **"Use your recommendation" is always a valid answer.** Take it and move on.
4. **Never ask for a secret in chat.** No passwords, API keys, tokens or login codes in messages. When
   you need one, use Grok Bot's secret request form so the value goes straight into secret storage. If
   the human pastes a secret into chat anyway, stop, tell them kindly to change (rotate) it, and use the
   form for the new one.
5. **Confirm before anything that costs money.** Renting a server, a paid plan, a treg top-up. Say the
   rough monthly cost and wait for a clear yes.
6. **Approvals must not get in the way.** The default is light (see Step 7). Do not invent extra
   approval steps.
7. **Keep a setup note as you go.** In your own memory, write each choice and the date. At the end you
   turn it into the Chief of Staff brief, so nothing has to be asked twice.
8. **Stop and say so if something fails.** Give the exact error and the one next thing to try. Do not
   guess your way past a failed step.
9. **No jargon without a short meaning.** The first time you use a word like "worker", "token" or
   "Tailscale", add a few plain words about what it is.

Read these files from the repo as you go: `README.md`, `templates/chief-of-staff.md`,
`templates/project-bot-brief.md`, `templates/routine-prompts.md`, `docs/which-tool-when.md`,
`docs/goals-and-heartbeat.md`, and, if they want a worker, `docs/worker-setup.md` and
`templates/bot-setup-checklist.md`.

---

## Step 0. Say hello and set expectations

Tell them, in your own words:

> I will set you up with a small team of AI helpers: a Chief of Staff bot (that is me, or a bot you
> pick) and one bot per project. If you want, we can also add a "worker", a rented computer in the cloud
> that does long jobs and all the coding. I will ask one question at a time and always say what I
> recommend. Without a worker this takes about an hour. With one, plan on an afternoon.

Ask: **"Which name should I use for you, and what time zone are you in?"**

## Step 1. Learn their projects

Ask: **"What are the 1 to 5 things you are working on right now?"** For example a business, a website,
an app, marketing, a newsletter, a side project.

For each one, ask briefly (one project at a time):

- What it is, in a sentence.
- What a good month looks like for it.
- Whether it has code (a GitHub repo) or not.

Write each one down as a project with a short id (lowercase, no spaces, like `marketing` or `shop-site`).

## Step 2. Decide: with or without a worker

Explain:

> A worker is a rented server, always on, that does long jobs while you sleep: all the coding, deep
> research, design, bulk work, and anything that should run on a specific AI model. It costs roughly
> $15 to $50 a month for the server, depending on the provider, plus the Claude and ChatGPT subscriptions you may already have.
> Without one, your bots still plan, draft, research, and keep you organized.

Recommend:
- **With a worker** if any project has code, or they want long research or design done.
- **Without a worker** otherwise. They can add one later and nothing is redone.

If **without**, skip Steps 9 to 12.

## Step 3. Set up the Chief of Staff

Explain:

> The Chief of Staff is the one bot you talk to about everything. It keeps your "Waiting on you" list,
> sends you one short message of decisions each morning, and runs a weekly goals meeting.

Ask: **"Should I be your Chief of Staff, or do you want a separate bot for it?"** Recommend: this bot,
if they are already chatting with it.

Then paste `templates/chief-of-staff.md` into the Chief of Staff's instructions and fill in the
CAPITALS from your setup note (their name, time zone). If there is no worker, leave out the parts about
the backend and `swarmctl`; they are marked.

## Step 4. Decide which project bots to create

Explain:

> Each project gets its own bot. It knows only that project, so it stays focused and cannot mix up your
> businesses. Small or related things can share one bot.

Recommend: **one bot per project from Step 1**, but merge tiny ones (for example "newsletter" can live
with "marketing"). Two or three bots is a great start; they can add more any time.

For each bot:
1. Create it in Grok Bot (or have them do it, if only they can).
2. Fill in `templates/project-bot-brief.md` with what you learned in Step 1. Mark unknowns "TBD (ask the
   owner)".
3. Pick its bot id, like `marketing-bot`.

## Step 5. Decide which connectors each bot gets

Explain:

> Connectors let a bot use your own accounts, like Gmail, Slack, your calendar or your analytics. Each
> bot should get only what its project needs.

For each bot, suggest a short list based on its project. Typical defaults:

| Project type | Suggested connectors |
| --- | --- |
| Marketing | Analytics, the social or email tool they use, Google Drive or Docs |
| Website or app | GitHub (if there is code), analytics, error tracking if they use one |
| Sales or clients | Gmail, calendar, the CRM |
| Chief of Staff | Calendar, Gmail (read and draft), Slack |

Ask: **"Want me to connect these for each bot?"** The human usually has to click "connect" and log in
themselves; that is fine and expected. Write each connector into that bot's brief under "Connectors and
apps".

## Step 6. Decide about treg and Executor

**treg.** Explain:

> treg lets a bot buy one piece of paid data when it needs it, like SEO keyword numbers, search results
> or contact details, instead of paying monthly for a tool you use twice. It can also reach some of your
> own private tools, like Google Search Console and Google Ads, so a bot can pull those numbers too.

Recommend: **add it for marketing or SEO projects**, with a monthly spending cap in the project brief
(start small, for example $20). Skip it for projects that never need paid data. Confirm any paid top-up
first (ground rule 5). Read `docs/which-tool-when.md` for when a connector is better than treg for the
same account.

**Executor.** Explain:

> Executor holds logins for your own tools in one safe place so the worker can use them too. Most
> people do not need it at first.

Recommend: **skip it for now.** Add it later only if a worker task needs one of their connected tools.

## Step 7. Decide the approval level

Explain:

> Bots just do normal work without asking: research, drafts, plans, code on a separate branch, filing a
> decision for you. They ask first only for three things: anything that goes out to other people (emails,
> posts), anything that deletes or cannot be undone, and anything that spends money.

Recommend: **the light default** (it is in `docs/approvals.md`). Ask: **"Is there anything a bot may
always do on its own, like posting from an approved content calendar?"** and **"Is there one thing a bot
must never touch?"** Write the answers into each project brief's Permissions section and its one "Never
touches" line.

Do not add more approval steps than this. Approvals that get in the way make the whole team slow.

## Step 8. Decide the rhythm: goals meeting, heartbeat and morning list

Explain:

> Once a week I hold a short goals meeting with you: what each project got done, and one to three clear
> goals for the next week, each with a "done means" line. Every day each project bot quietly checks its
> goals and only bothers you if one is at risk or it needs a decision. Each morning you get one message
> with everything waiting on you.

Ask:
1. **"Which day and time suits a 10 minute goals meeting?"** Recommend: Monday at 9:00 in their time zone.
2. **"What time should the morning decision list arrive?"** Recommend: 8:00.

Then set up the routines from `templates/routine-prompts.md`:
- Chief of Staff: morning decision batch, nightly memory cleanup, weekly goals meeting, fallback handler
  (only with a worker).
- Each project bot: nightly memory cleanup, daily work heartbeat, task result handler (only with a
  worker).

Without a worker, keep the goals file and the project log as notes in each bot's memory (see
`docs/goals-and-heartbeat.md` and `docs/project-log.md`, section "Without a worker").

## Step 9. Worker: where it runs and how big (worker only)

Explain:

> The worker is a normal Ubuntu server from any provider you like (DigitalOcean, Hetzner, Vultr, AWS and
> others all work). It joins Tailscale, a private network for your own devices, so it never has to be
> open to the whole internet.

Recommend: **4 CPUs and 8 GB of memory with Ubuntu 24.04**, at whatever provider they already use, or
the cheapest one they trust. That is roughly $15 to $50 a month, depending on the provider. Bigger only for big code projects (see
`docs/sizing-and-sandboxes.md`).

**Confirm the cost before they rent it** (ground rule 5). Then walk them through renting it and adding
their SSH key, using `docs/worker-setup.md`. If they have no SSH key, help them make one on their laptop;
the private half never leaves the laptop and never goes in chat.

Ask them to make a free Tailscale account if they have none. Recommend: yes, it is free for personal use.

## Step 10. Worker: model logins and the backend (worker only)

**Model logins.** Explain:

> The worker uses Claude Code and Codex, logged in with your own Claude and ChatGPT subscriptions. No
> per-use bills.

Recommend: **the plain Claude and Codex command line tools with their own logins** (this is the
default). A proxy that spreads work over several subscriptions exists but is for heavy users; skip it
(see `docs/optional-services.md`). If they have only one of the two subscriptions, that is fine: use
`--no-claude` or `--no-codex` in the setup command.

**Where the task tracker (backend) runs.** Explain:

> The backend is the small service that keeps track of jobs, results, the Waiting on you list and the
> project log. It runs on the worker itself.

Recommend: **on the worker** (the default). A Cloudflare version is only a design and is not built, so
there is nothing to choose here today; just tell them that.

## Step 11. Worker: install it together (worker only)

Follow `docs/worker-setup.md` and the "Path B" steps in `README.md`. You cannot type on their server, so
give them one command at a time, explain it in one sentence, and ask them to paste back the last few
lines of output (never any secret). In short:

1. Get the kit onto the server (docs/worker-setup.md, step 2).
2. Run `sudo bash scripts/setup-worker.sh --admin-user NAME --tailscale later`. It takes 5 to 15
   minutes. On a brand new server it may wait a few minutes for the server's own first-boot updates;
   that is normal.
3. Logins on the server: they type `sudo -iu agent claude` then `/login`, and
   `sudo -iu agent codex login --device-auth`, and approve in their browser. GitHub: they run
   `sudo -u swarm-runner -H gh auth login`. You never see these codes.
4. Join Tailscale: `sudo tailscale up --ssh`, then approve the server in the Tailscale page.
5. Set up [encrypted secrets](docs/secrets.md#set-up-the-encrypted-repo) with them, even for one worker.
   Have them make the owner, worker and recovery keys, save the recovery key in their password manager,
   then sync a runtime bundle and check names with `with-secrets --check tools`. Do not ask for any of
   the private keys or bundle values; the secret-request-form rules above still apply.
6. Start the worker and check it: `sudo systemctl start swarm-worker`, then
   `sudo bash scripts/smoke-test.sh`. Expect "0 failed". Up to 2 warnings (GitHub, Tailscale) are normal
   until those are done.
7. Close public SSH once they can log in over Tailscale: `sudo bash scripts/setup-worker.sh --lockdown`.

## Step 12. Connect each bot to the worker (worker only)

For each bot, follow `templates/bot-setup-checklist.md` with the human. The two secrets in it (the
bot's token and its inbox trigger key) go through Grok Bot's secret request form, never chat. Then bind
each bot to its project, including projects with no code:

```
sudo swarmctl project set marketing --bot marketing-bot
```

That binding is what lets the bot write to and read its own project log.

## Step 13. First goals meeting and first task

1. Run the weekly goals meeting now, once, so every project starts with one to three goals and a "done
   means" line (prompt 6 in `templates/routine-prompts.md`). Write the goals into the goals file and each
   project bot's brief.
2. With a worker: have one project bot queue a tiny report-only task and confirm the result came back to
   that bot (`docs/first-week.md`).
3. Without a worker: have one project bot do one small real piece of work from its goals and add it to
   its project log.

## Step 14. Wrap up

Send one short message:

- Which bots exist and what each one owns.
- The goals for this week, per project.
- When the morning list and the goals meeting happen.
- What still needs them, if anything (filed on the Waiting on you list, not in the message).

Turn your setup note into the "Setup choices" part of the Chief of Staff brief: each decision, the
date, and why. Then go quiet. From now on you only message them when something changed.

---

## Every decision at a glance

| Decision | Default to recommend | When to pick something else |
| --- | --- | --- |
| Worker or not | With a worker if any code or long research; otherwise no | Add one later any time |
| Where the worker runs | Any provider they already use, Ubuntu 24.04 | A provider their company requires |
| Worker size | 4 CPUs, 8 GB | Big code projects: 8 CPUs, 16 GB, or offload test runs |
| Private network | Tailscale, then close public SSH | Never skip it with a worker |
| Model logins | Plain Claude and Codex CLIs with their own subscriptions | Proxy only for heavy users who hit limits |
| Backend | On the worker | Cloudflare is design only, not built |
| Project bots | One per real project, small ones merged | More later as projects grow |
| Connectors | Only what each project needs | Add when a bot keeps asking for the same data |
| treg | Yes for marketing and SEO, with a monthly cap | No if no paid data is ever needed |
| Executor | Skip at first | When a worker task needs their connected tools |
| Approvals | Light default: ask only for outside messages, deletions and money | Loosen per project as trust grows |
| Goals meeting | Monday 9:00, about 10 minutes | Any fixed weekly slot they will keep |
| Morning list | 8:00 | Whenever they first check messages |
