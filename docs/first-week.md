# Your first week

This doc walks you through your first week with the swarm, one small step a day. By the end, one task has gone all the way through: queued by a bot, run on the worker, asked a question, answered, reported, reviewed by a second model, tested, merged by you, and the bot was told. It assumes the worker is set up ([worker-setup.md](worker-setup.md)) and the backend is reachable from your bots ([backend.md](backend.md)).

Examples use a project called `website` with a bot called `website-bot`, and a Chief of Staff bot called `chief`. Use your own names.

## Day 1: two bots, registered

1. In Grok Bot, create the **Chief of Staff** bot from [../templates/chief-of-staff.md](../templates/chief-of-staff.md).
2. Create one **project bot** from [../templates/project-bot-brief.md](../templates/project-bot-brief.md). Pick a project with low stakes.
3. Give each bot its own webhook routine (its inbox) using the task result handler prompt from [../templates/routine-prompts.md](../templates/routine-prompts.md). Note each webhook URL and its key.
4. On the worker (as the server admin), register both bots. Put each webhook key in a file first, so it never appears in your shell history. The backend stores the key in its own protected file, never in the database:

```bash
sudo swarmctl bot set chief --role chief --notify-url https://WEBHOOK-URL-FOR-CHIEF --notify-auth-file /root/chief.hook
sudo swarmctl bot set website-bot --notify-url https://WEBHOOK-URL-FOR-WEBSITE-BOT --notify-auth-file /root/website.hook
sudo rm /root/chief.hook /root/website.hook
```

5. Make a token for each bot. It is written to a file with mode 600 (only the owner can read it):

```bash
sudo swarmctl bot token chief
sudo swarmctl bot token website-bot
```

   Then bind the project bot to its project and repo. A project bot can only queue repo work for projects that list it:

```bash
sudo swarmctl project set website --repo YOUR-ORG/website --bot website-bot
```

   Put each token in that bot's secret store. Never paste it into chat. See [secrets.md](secrets.md).

6. Each bot walks itself through [../templates/bot-setup-checklist.md](../templates/bot-setup-checklist.md). The key check is the test notice:

```bash
sudo swarmctl bot test website-bot
sudo swarmctl bot test chief
```

   Each bot should see a `reason: "test"` event in its own inbox, and only its own. `sudo swarmctl status` shows `notices: sent N`.

   Each bot adds one real day-one entry to the [activity feed](activity-and-decisions.md). Read it
   with `swarmctl activity-log list PROJECT`. Before the first nightly export, set up the separate
   [memory repo](activity-and-decisions.md#nightly-export-to-the-memory-repo).

## Day 2: a small report-only task

Ask `website-bot` in chat to research something small, for example "list the five slowest pages and why". The bot queues it (with `swarmctl`, or the same thing as `POST /tasks`). If the brief needs files, attach them with `--attach`; the worker cannot see the bot's own computer:

```bash
swarmctl enqueue --owner website-bot --report-only --key website-slow-pages-1 brief.md
```

`--key` is an idempotency key: if the bot sends the same request twice, it gets the same task back. A new piece of work needs a new key. Reusing a key for different work is refused with an error.

Watch it:

```bash
swarmctl status
swarmctl list --owner website-bot
```

When it finishes, the backend posts to `website-bot`'s inbox only. The bot reads the report (`swarmctl report TASK_ID`), does the re-check in its Handoff section, and tells you the result in one message.

## Day 3: a question and an answer

Queue a task that needs a choice, for example a short copy rewrite with two possible tones. When the agent needs an answer, it ends with `blocked` and a question. The task becomes `waiting` and `website-bot` gets the question. The bot answers if its brief covers it:

```bash
swarmctl answer TASK_ID "Use the friendly tone"
```

The task goes back to `queued` and resumes the same agent session. If the question needs you, the bot asks you in chat with its recommendation. Try sending a note to a running task too:

```bash
swarmctl message TASK_ID "Also keep it under 120 words"
```

## Day 4: a code task with a draft pull request

```bash
swarmctl enqueue -p website -r YOUR-ORG/website --owner website-bot --pr brief.md
```

The worker works on branch `agent/TASK_ID`, pushes it when the agent says `done`, and opens a **draft** pull request. The link is in the task (`pr_url`) and in the notice.

## Day 5: review and tests before merge

1. Ask the other model family to review the pull request. Review tasks are report-only and never push. `--dev-task`
   names the task that wrote the PR, so the reviewer is the family that did not write it:

```bash
swarmctl review -r YOUR-ORG/website --pr-number 12 --dev-task TASK_ID --owner website-bot
```

   Docs-only and typo changes with green CI need no review, and the loop is capped (two rounds, three for high-risk
   changes). See [code review](code-review.md).

2. Run the full test suite as a script task. No model runs, so no tokens are spent watching tests:

```bash
swarmctl enqueue -r YOUR-ORG/website --branch agent/TASK_ID --command "npm ci && npm test" --owner website-bot - <<< "Run the full suite on the agent branch"
```

   The output is in the task's `script.log` artifact. A nonzero exit marks the task failed.

3. If the review found must-fix issues, queue a fix as a follow-up of the task that made the pull request: `swarmctl enqueue --follow-up TASK_ID fix-brief.md`. It reuses the same branch and pull request, and the same folder and session if they are still on the worker. When review and tests pass, **you** merge on GitHub. The kit never merges.

## Day 6: the Waiting on you list

Have `website-bot` file a decision (it can only link items to its own tasks):

```bash
swarmctl waiting add --project website --bot website-bot --kind decision \
  --ask "Publish the new pricing page?" --rec "Publish it" --link https://example.com/preview --key pricing-page-v1
```

Then see it and clear it as the human (through the Chief of Staff or the admin token):

```bash
sudo swarmctl waiting list
sudo swarmctl waiting answer ITEM_ID "yes"     # or: waiting drop ITEM_ID, waiting snooze ITEM_ID --days 2
```

The filing bot is told the answer. A dropped item must not be filed again.

## Day 7: routines and a usage check

1. Add the morning decision batch and nightly memory cleanup routines ([routines.md](routines.md)).
2. Look back at the week: `sudo swarmctl list --limit 100`, `sudo swarmctl status`, and your Claude and Codex usage pages. Were any bots chatty when nothing changed? Did tasks wait long for answers? Adjust briefs and the model routing table ([model-routing.md](model-routing.md)).
3. Add a second project bot only when the first one runs smoothly.
