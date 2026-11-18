# How it works, technical

This page is for developers. It shows the parts on the worker, who holds which credential, how a task
moves through its states, and how results are routed. The README has the plain product view. For every
API route and who may call it, see [backend.md](backend.md). For secrets and the trust model, see
[secrets.md](secrets.md).

## The parts and who holds what

```mermaid
flowchart LR
    subgraph grok["Grok Bot: command and control"]
        cos["Chief of Staff bot"]
        pb["Project bots"]
    end
    subgraph worker["Worker server, reached over Tailscale Serve"]
        backend[("Backend, user swarm: tasks, claims, reports, Waiting on you, outbox")]
        keys[("Bot trigger keys: /etc/agent-swarm/bot-keys")]
        runner["Runner and publisher, user swarm-runner: worker token, GitHub login, private clones"]
        inbox[("Inbox: brief, prompt, attachments, messages, repo bundle")]
        outbox[("Outbox: REPORT.md, AGENT_STATUS, artifacts, work bundle")]
        exec["agent-exec via sudoers: scrubbed env, own scope, hard timeout"]
        agent["Claude Code or Codex, user agent: model logins only"]
    end
    gh[("GitHub")]

    pb -->|"swarmctl enqueue, bot token"| backend
    cos -->|"chief token"| backend
    runner -->|"claim with worker token, then task token"| backend
    runner -->|"writes"| inbox
    runner --> exec --> agent
    agent -->|"reads"| inbox
    agent -->|"writes"| outbox
    runner -->|"reads, no-follow"| outbox
    runner -->|"push branch, draft PR"| gh
    keys --- backend
    backend -->|"result to owner"| pb
    backend -.->|"ownerless, waiting on human, fallback, alerts"| cos
```

Key points:

- The `agent` user never holds the worker token or a GitHub credential. It gets a git bundle of the repo
  to clone from, and writes a bundle of its new commits to the outbox.
- The runner never runs git inside the agent's folder. It fetches the agent's bundle into its own private
  clone, scans the added lines for secrets, checks the branch rules with the backend
  (`publish-check`), and pushes to the expected branch only.
- Bot trigger keys live in files, not in the database, so database backups never contain them.

## Tokens

| Token | Who holds it | What it can do |
| --- | --- | --- |
| admin | root, and the Chief of Staff if you choose | everything |
| chief bot (`swb_`) | the Chief of Staff bot | what admin does, except worker and task calls and making tokens |
| project bot (`swb_`) | one project bot | its own tasks, items, bot settings, and projects it is bound to |
| worker | the runner (`swarm-runner`) | claim, task states for cleanup, alerts. Nothing else |
| task (`swt_`) | the runner, for one claim | heartbeat, read its own task and messages, upload its own files, publish-check, finish. Dies when the task leaves `running`, when the lease runs out, or after `SWARM_TASK_TOKEN_MAX_HOURS` |

## The life of a task

```mermaid
sequenceDiagram
    autonumber
    participant B as Project bot
    participant BE as Backend
    participant R as Runner (swarm-runner)
    participant A as Agent (agent user)
    participant G as GitHub

    B->>BE: POST /tasks (brief, owner, project)
    Note over BE: state = queued
    R->>BE: POST /worker/claim (worker token)
    BE-->>R: task, claim_id, task token (stored only as a hash)
    Note over BE: state = running
    R->>G: clone into private folder, make repo bundle
    R->>A: agent-exec run (scrubbed env, own scope, timeout)
    loop every 30 seconds, from claim until finish is acknowledged
        R->>BE: heartbeat (task token, claim_id, ack of delivered messages)
        BE-->>R: new messages, or 409 claim_ended
    end
    A-->>R: outbox: REPORT.md, AGENT_STATUS, artifacts, work bundle
    R->>BE: upload report and artifacts (task token)
    R->>BE: publish-check (still running, still allowed to push?)
    R->>G: fetch bundle, secret scan, push expected branch, draft PR
    R->>BE: finish: done (claim_id, a retry gets the stored first answer)
    Note over BE: state = done, claim ends, task token dead
    BE->>B: result to the owner bot
```

Other ways a run ends:

- **Waiting.** `AGENT_STATUS` says `blocked` with a question. Finish with `waiting`. The owner bot gets
  the question (and the Chief of Staff too if it waits on the human). An answer puts the task back in
  `queued`, and the next claim resumes the same folder and session.
- **Next.** `AGENT_STATUS` says `next`. The task goes back to `queued` at once and resumes.
- **Limit.** A usage limit. The worker tries the fallback provider; if there is none, the task waits one
  hour, doubling up to a day.
- **Cancel.** Cancelling a running task is immediate. The claim ends, the next heartbeat gets
  `409 claim_ended`, the runner stops the agent (TERM, then KILL after 20 seconds) and publishes nothing.
- **Reap.** If heartbeats stop for 10 minutes, the backend requeues the task (or fails it after
  `max_attempts`). The old claim is dead, so a late runner cannot finish or upload.

## Where results go

```mermaid
flowchart TD
    ev["Task event: done, failed, cancelled or waiting"] --> owner{"Owner bot has an inbox?"}
    owner -->|"yes"| send["Send to the owner bot"]
    owner -->|"no, or no owner"| cos["Send to the Chief of Staff"]
    send --> ok{"Answer from the inbox"}
    ok -->|"2xx"| done["Delivered"]
    ok -->|"409 or duplicate true"| done
    ok -->|"retry 6 times, or a permanent error"| fb["Fallback to the Chief of Staff"]
    ev --> human{"Waiting on the human?"}
    human -->|"yes"| cos2["Also tell the Chief of Staff and show on Waiting on you"]
```

Delivery is at least once. Every payload has `event_id` (also the `Idempotency-Key` header), `event`
(what happened) and `reason` (why this bot got it). Receivers must skip an `event_id` they have already
handled. A 409 answer, or a JSON body with `"duplicate": true`, counts as delivered, so a receiver that
already has the event does not cause a false fallback. Redirects are never followed, and the target
address must be public. Details in [results-routing.md](results-routing.md).

## Follow-ups

```mermaid
flowchart TD
    f["swarmctl enqueue --follow-up PARENT brief.md"] --> st{"Parent state"}
    st -->|"queued or running"| msg["Refused: message the parent instead"]
    st -->|"waiting on its owner"| ans["Counts as the answer: parent resumes"]
    st -->|"waiting on the human"| human["Refused for a project bot"]
    st -->|"done, failed or cancelled"| newt["New task: same repo, branch, flags, owner"]
    newt --> held{"Another follow-up of this parent still open?"}
    held -->|"yes"| wait["Queued, held_behind that task"]
    held -->|"no"| run["Runs"]
    run --> where{"Parent folder still on this worker, same provider?"}
    where -->|"yes"| resume["Resume parent folder and session"]
    where -->|"no"| fresh["Fresh checkout of pushed branch, or restore parent bundle, plus previous-REPORT.md"]
```

## Lifecycle helpers in the backend

- **Reminders.** A task waiting on its owner bot for 12 hours gets one reminder. After 24 hours it is
  filed once on Waiting on you and the Chief of Staff is told.
- **Watchdog.** Tasks queued but no worker has checked in for 10 minutes: one alert. A task running past
  its timeout plus 30 minutes: one alert per claim.
- **Drain.** `sudo swarmctl drain` stops new claims; `sudo swarmctl restart-when-idle` waits until no
  task runs, then restarts the worker.
- **Retention.** Files of tasks finished more than 90 days ago, and sent notices older than 30 days, are
  deleted daily.

## Optional: Cloudflare (design only, not built)

Running the backend on Cloudflare Workers with D1 is a design only. Nothing for it ships in this kit.
See [backend.md](backend.md).
