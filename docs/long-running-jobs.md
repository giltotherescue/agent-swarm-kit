# Long-running jobs

Most worker tasks finish in minutes or a few hours. Some jobs are meant to run for days and act as
their own orchestrator: for example a product "polisher" that keeps walking through the app, fixing the
bugs it finds and testing again. A normal run stops at `SWARM_TASK_TIMEOUT` (4 hours by default). This
page shows two small ways to run longer jobs, and which one to pick.

## Recommended: a chain of short cycles

A chain is a task that, when one cycle ends, queues its own next cycle as a follow-up with a checkpoint
attached.

```
swarmctl enqueue --chain --max-cycles 30 --timeout-minutes 120 \
    --owner app-bot -r you/app --pr polish.md
```

How it works:

1. Each cycle is a normal, fresh run with its own time limit. The brief tells the agent which cycle it
   is, to do one useful round of work, and to write `CHECKPOINT.md` with everything the next cycle needs.
2. When the cycle ends `done` with a checkpoint, the backend queues the next cycle as a follow-up: same
   repo, branch and owner, the same folder while it is still on that worker, the same brief, and the checkpoint attached as
   `.task-context/attachments/checkpoint.md`. The next cycle starts a **fresh model session**, so it reads
   the checkpoint instead of carrying days of conversation.
3. The chain ends when a cycle leaves no checkpoint (the agent decided the job is finished), when it
   reaches `--max-cycles` (default 20, at most `SWARM_MAX_CYCLES`), or when a cycle fails. There is no
   unlimited chain: `--max-cycles 0` is refused with an error, so set a real limit and let the agent end
   the chain early by leaving no checkpoint.
   **Chains are for model tasks only.** A script task (`--command`) never writes a checkpoint, so
   `--chain` with `--command` is refused. A script can still use `--timeout-minutes`.
4. **Each cycle adds one entry to the project log** ("Cycle 3 done: Polisher", with its summary and what
   happens next). Owner notices are sent only when the chain ends or stops, not for every cycle.
5. Each cycle's checkpoint is also saved on that task as the artifact `checkpoint.md`, so you can read it
   any time: `swarmctl get-artifact TASK_ID checkpoint.md -o checkpoint.md`.

**Cancel stops the whole chain.** `swarmctl cancel ID` with the id of any cycle, even one that already
finished, cancels every cycle that is still queued, waiting or running, and logs "Chain ... cancelled".
A running cycle stops within about 30 seconds and publishes nothing, and since its finish is refused it
cannot queue another cycle. Its handoff folder is removed right away too, since the backend will never take
those results; the work itself stays in the agent's folder until the daily cleanup.

**Restarting after a failure.** Fetch the last good checkpoint and start a new chain with it. Add
`--branch` with the old chain's branch to keep working on the same branch and pull request:

```
swarmctl get-artifact LAST_GOOD_CYCLE_ID checkpoint.md -o checkpoint.md
swarmctl enqueue --chain --max-cycles 30 --owner app-bot -r you/app --pr --branch agent/FIRST_CYCLE_ID \
    --attach checkpoint.md polish.md
```

The first cycle of the new chain is told to read the attached checkpoint.

### Why the chain is the default

- **A stuck cycle is cheap.** If the agent loops or hangs, you lose one short cycle, not three days.
  The time limit catches it quickly and the chain stops with a clear failure.
- **Fresh context every cycle.** Long sessions get slower, costlier and more confused. A checkpoint
  forces a short written summary of what matters.
- **Everything else keeps working.** Each cycle is a normal task: the usual time limit, attempt counts,
  usage-limit fallback, secret scan before every push, follow-up rules and reports. Worker restarts and
  updates happen between cycles, not in the middle of a three-day run.
- **You can see progress.** One log entry per cycle, a checkpoint you can read, and cancel works at any
  point.

## Alternative: one long run

For a job that truly cannot be split, a single task can ask for a longer time limit.

```
swarmctl enqueue --timeout-minutes 1440 --owner app-bot -r you/app --pr job.md
swarmctl enqueue --kind long-running --owner app-bot -r you/app --pr job.md
```

- `--timeout-minutes` sets this task's own limit. It cannot be more than the admin maximum,
  `SWARM_TASK_TIMEOUT_MAX` in `/etc/agent-swarm/swarm.env` (default 86400 seconds, one day); a larger
  ask is refused with a clear error. The owner raises that setting to allow longer runs.
- The `long-running` kind uses the admin maximum when no timeout is given. Its brief tells the agent to
  rewrite `CHECKPOINT.md` in its folder after every cycle of work, and to read it first if it exists. If
  the run is restarted on the same worker (for example after a reboot), it picks up from there.
- The worker keeps heartbeating the whole time, as it does for every task.
- A task is either a chain or long-running, not both.

The downsides are the reverse of the chain's strengths: one stuck run can burn its whole time limit, the
session grows all the time, and a worker update waits for the run to end
([updating-the-kit.md](updating-the-kit.md)).

## When neither fits: an orchestrator

A chain or a long run both end. For a product-level agent that must keep **one session** going for days on
one worker, with a fixed name other bots can message, the kit has orchestrators: one Claude Code session in
tmux, kept up by its own supervisor outside the runner, with heartbeats, crash restarts that resume the
session, and a crash-loop stop. See [orchestrators.md](orchestrators.md). Prefer a chain whenever the job
can be split into cycles.

## Heartbeats, and what they cost

The worker sends a **machine heartbeat** every 30 seconds while a task runs, chain or not. It is a plain
network call from the worker program to the backend saying "this task is still running", which also picks
up any messages for the task. **It runs no model and uses no tokens.** If it stops for 10 minutes, the
backend puts the task back in the queue.

This is unrelated to the daily **work heartbeat** routine, where a project bot reads its log and checks
its goals ([goals-and-heartbeat.md](goals-and-heartbeat.md)). That one does use a few tokens, once a day.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `SWARM_TASK_TIMEOUT` | 14400 (4 hours) | Time limit for a run that does not ask for its own |
| `SWARM_TASK_TIMEOUT_MAX` | 86400 (1 day) | The most a task may ask for with `--timeout-minutes` |
| `SWARM_MAX_CYCLES` | 200 | The most cycles a chain may ask for |

API: `POST /tasks` takes `timeout_minutes`, `chain` and `max_cycles`. A task shows `timeout` (seconds),
`chain`, `chain_root` (the first cycle's id), `cycle` and `max_cycles`. The worker sends the checkpoint
to the backend as `checkpoint` when it finishes a cycle.
