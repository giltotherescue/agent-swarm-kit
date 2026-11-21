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
10. **You do the work, they approve.** Your human never SSHes anywhere, never types `sudo`, and never runs a
    command. You build the server and run every command yourself. They only open links and approve:
    the provider token form, the Tailscale approval, and each sign-in. Say what they are about to see
    ("a page will ask you to type a short code") before you send a link.

Read these files from the repo as you go: `README.md`, `templates/chief-of-staff.md`,
`templates/project-bot-brief.md`, `templates/routine-prompts.md`, `docs/which-tool-when.md`,
`docs/goals-and-heartbeat.md`, and, if they want a worker, `docs/cloud-init.md`, `docs/worker-setup.md` (reference) and
`templates/bot-setup-checklist.md`.

---

## Step 0. Say hello and set expectations

Tell them, in your own words:

> I will set you up with a small team of AI helpers: a Chief of Staff bot (that is me, or a bot you
> pick) and one bot per project. If you want, we can also add a "worker", a rented computer in the cloud
> that does long jobs and all the coding. I will ask one question at a time and always say what I
> recommend. If you want a worker, I build it myself: you will not need to open a terminal. You only
> approve a few things in your browser.

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
- Chief of Staff: morning decision batch, missed-question sweep at each heartbeat, nightly memory
  cleanup, weekly goals meeting. With a worker, also set up the fallback handler and nightly activity export.
- Each project bot: nightly memory cleanup, daily work heartbeat, task result handler (only with a
  worker).

Without a worker, keep the goals file and the project log as notes in each bot's memory (see
`docs/goals-and-heartbeat.md` and `docs/project-log.md`, section "Without a worker").
With a worker, offer a separate docs-only memory repo with a main-branch grant for that repo alone.
Use `docs/activity-and-decisions.md`. Ask whether to keep the default in `docs/delegated-decisions.md`.

## Step 9. Worker: where it runs and how big (worker only)

Explain:

> The worker is a normal Ubuntu server from a cloud provider. I will create it for you through the
> provider's API and set it up while it starts, so you do not have to touch it. It joins Tailscale, a
> private network for your own devices, so it is never open to the whole internet.

Recommend: **DigitalOcean, 4 CPUs and 8 GB of memory, Ubuntu 24.04**, or whichever provider they already
use. Roughly $15 to $50 a month, depending on the provider (see the README for current prices). Bigger only
for big code projects (see `docs/sizing-and-sandboxes.md`). Hetzner, Vultr and Linode work the same way
through their own APIs; `docs/cloud-init.md` says what is and is not tested.

Other choices, one question each:
- **A spare Mac at home instead?** Only if they have one that holds nothing private. It is simpler but
  less isolated (no separate users, no limits per job). Recommend a cloud server. If they choose the Mac,
  follow `docs/macos-worker.md` instead of Steps 10 and 11; the sign-ins happen in Terminal on the Mac, so
  this is the one place they do type, and you tell them exactly what.
- **A computer at home with Ubuntu?** Then use the by-hand path (`docs/worker-setup.md`) and coach them.

**Confirm the cost before you create anything** (ground rule 5), then ask for the provider token:

> I need a token from DigitalOcean so I can create the server. A token is a password that only allows
> what you pick. Open the form I send, create the token in DigitalOcean (I will tell you which permissions
> to tick), and paste it into the form, not into chat. When the server is up I will ask you to delete the
> token.

Send the secret request form. Never accept the token in chat. Tick **Droplet create and read, plus read for
images, sizes and regions**.

## Step 10. Worker: Tailscale and the build (worker only)

1. Ask them to make a free Tailscale account if they have none. Recommend yes. Make sure Tailscale is
   running on the computer your bot works from, and that their Tailscale access rules let that computer
   reach the worker with Tailscale SSH as the admin user (sample rules: `docs/worker-setup.md`,
   "Tailscale access rules"). Do this for them where you can.
2. Choose how the server joins Tailscale. Recommend **a one-use key**: send a secret request form and
   ask them to make an auth key in the Tailscale admin page that is *single use*, *expires in an hour* and
   is *pre-approved*, and paste it into the form. The alternative needs no key: you read a login link from
   the new server and they click it. Both are one browser step.
3. Make the bootstrap key and the user data, exactly as in `docs/cloud-init.md`:
   `ssh-keygen` for a bootstrap key in your scratch folder, then `scripts/render-cloud-init.py`. For a
   first server, pass `--lockdown bot`, so public SSH stays open until you have proven Tailscale SSH works.
4. Create the server with the provider's API (`docs/cloud-init.md`, "What the bot does"). Say what you are
   doing in one sentence per step. Wait for it to be active.
5. If they chose the login link: read it from the server and send it ("Open this link and approve the
   machine"). If they gave a key, the server joins by itself.
6. Watch `/var/lib/agent-swarm/firstboot/status` until it says `finished` (about 10 to 15 minutes; tell
   them it is normal to wait). If it says `stopped`, read the log, tell them the exact problem in plain
   words, and fix it yourself.
7. Reach the server over Tailscale SSH (`ssh NAME@HOSTNAME`), check `sudo swarmctl status`, then close
   public SSH yourself: `sudo bash /opt/agent-swarm-kit/current/scripts/setup-worker.sh --lockdown --yes --admin-user NAME`.
8. Clean up: delete the user-data file, the Tailscale key file and the bootstrap private key, and ask them to
   delete the provider token. From now on you only use Tailscale SSH.

## Step 11. Worker: model logins, GitHub and secrets (worker only)

Each sign-in is "open this link". You run the helper over Tailscale SSH and pass on only the link and the
code it prints (`docs/worker-setup.md`, step 5).

> The worker uses Claude Code and Codex, signed in with your own Claude and ChatGPT subscriptions, and GitHub
> for pushing code. No per-use bills. For each one I will send you a link; you open it and sign in.

Recommend **the plain Claude and Codex command line tools with their own sign-ins** (the default). A proxy
that spreads work over several subscriptions exists but is for heavy users; skip it (`docs/optional-services.md`).
If they have only one subscription, that is fine; use `--no-claude` or `--no-codex` in the render step.

1. **Codex.** First check the preflight: device-code sign-in must be turned on in ChatGPT, under Security
   settings (or by their workspace admin). If it is off, the code is rejected. Ask them to turn it on, then run
   `sudo swarmctl login codex` and send them the `URL` and `CODE` lines.
2. **Claude.** Needs a Pro, Max, Ultra, Team or Enterprise plan. Run `sudo swarmctl login claude` and send the
   `URL`. The page will then show them a code. Send a secret request form for that code, then run
   `swarmctl login claude --finish` with the code on standard input. The long-lived token is saved on the
   server and never shown to you or them.
3. **GitHub.** Run `sudo swarmctl login github` and send the `URL` and `CODE`. This is the runner's login.
4. **Encrypted secrets.** The server already has SOPS, age and its own keys. Read the two public key files in
   `/var/lib/agent-swarm/firstboot/`. Help them set up the private secrets repo ([secrets.md](docs/secrets.md)):
   add the age public key as a recipient, add the deploy key to the repo with write access off (they approve
   that in GitHub), keep the recovery key in their password manager, then
   `sudo bash /opt/agent-swarm-kit/current/scripts/setup-secrets.sh --link REPO_URL`. Never ask for any
   private key or bundle value.
5. Run `sudo bash /opt/agent-swarm-kit/current/scripts/smoke-test.sh`. Expect "0 failed". Warnings for sign-ins
   you skipped are fine.

## Step 12. Connect each bot to the worker (worker only)

For each bot, follow `templates/bot-setup-checklist.md`. The two secrets in it (the
bot's token and its inbox trigger key) go through Grok Bot's secret request form, never chat. Then bind
each bot to its project, including projects with no code:

```
sudo swarmctl project set marketing --bot marketing-bot
```

That binding is what lets the bot write to and read its own project log.

Right after each bot's inbox webhook is registered (`swarmctl bot set ... --notify-url ...`), give it a bus
name and token. This is optional and changes nothing until other agents start messaging it
(`docs/agent-bus.md`):

```
sudo swarmctl bus register marketing-bot --kind bot --wake webhook --bot marketing-bot
sudo swarmctl bus token marketing-bot
```

`bus token` writes the token to a file with mode 600 and prints only the path. Move that file into the bot's
secret storage through the secret request form, never chat. Register yourself (the Chief of Staff) the same
way, plus `swarmctl bus register gil --kind human --wake poll` for the owner. Do not add `--notices bus`
until the bot has been told how to read bus messages (`docs/agent-bus.md`, "Using the bus").

If something here does not work for you (no cloud-init at the provider, or you would rather type it
yourself), the manual path is `docs/worker-setup.md` and "Path B" in `README.md`.

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
date, and why.

Also tell them that feedback, problems and ideas for the kit can be emailed to
gilhildebrand@mail.grokbot.com. If you hit a problem or a confusing step during setup, you may email a short
note about it there too, with no secrets in it, but only after you tell them what you will send.

Then go quiet. From now on you only message them when something changed.

---

## Every decision at a glance

| Decision | Default to recommend | When to pick something else |
| --- | --- | --- |
| Worker or not | With a worker if any code or long research; otherwise no | Add one later any time |
| Where the worker runs | DigitalOcean (or the provider they use), Ubuntu 24.04, built by the bot | A provider their company requires (similar API), or a spare Mac with less isolation |
| Worker size | 4 CPUs, 8 GB | Big code projects: 8 CPUs, 16 GB, or offload test runs |
| Private network | Tailscale (one-use key), then close public SSH | Never skip it with a worker. First server: lockdown by the bot, after Tailscale SSH works |
| Model logins | Plain Claude and Codex CLIs with their own subscriptions | Proxy only for heavy users who hit limits |
| Backend | On the worker | Cloudflare is design only, not built |
| Project bots | One per real project, small ones merged | More later as projects grow |
| Connectors | Only what each project needs | Add when a bot keeps asking for the same data |
| treg | Yes for marketing and SEO, with a monthly cap | No if no paid data is ever needed |
| Executor | Skip at first | When a worker task needs their connected tools |
| Approvals | Light default: ask only for outside messages, deletions and money | Loosen per project as trust grows |
| Goals meeting | Monday 9:00, about 10 minutes | Any fixed weekly slot they will keep |
| Morning list | 8:00 | Whenever they first check messages |
