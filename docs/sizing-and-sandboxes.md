# Sizing the worker, and when to use sandboxes

This doc helps you decide how big your worker server should be, and when to send heavy jobs somewhere else instead of buying a bigger box. The answer depends on two things: what your repos need to build and test, and how many tasks you want running at the same time.

## The starting point

**4 vCPU (virtual CPUs), 8 GB of memory, 80 GB or more of disk, with `SWARM_MAX_PARALLEL=2`.**

That runs two tasks at once with room to spare for most small projects. Change `SWARM_MAX_PARALLEL` in `/etc/agent-swarm/swarm.env` and run `sudo systemctl restart swarm-worker`.

## What actually uses the resources

- **The model CLIs are light.** Claude Code and Codex mostly wait for the model, which runs in the provider's cloud. Each uses a few hundred MB of memory.
- **Your repo's tools are heavy.** Installing packages, builds, type checks, full test suites, databases, headless browsers and dev servers are what fill CPU and memory.
- **Disk grows with checkouts.** Each task gets its own folder in `/home/agent/work`, and the publisher keeps a private copy of the repo while the task is open. The daily cleanup removes them after `SWARM_KEEP_DAYS` (default 7).

Each task is limited to `SWARM_TASK_CPU_QUOTA` (default `200%`, meaning 2 CPUs) and `SWARM_TASK_MEMORY_MAX` (default `75%` of memory). All tasks together are also capped by a limit on the whole `agent` user that the setup script installs, so a runaway task cannot starve SSH, Tailscale or the backend. Every run, model or script, has a hard time limit (`SWARM_TASK_TIMEOUT`). The worker also refuses new work while the box is busy: load per CPU above `SWARM_MAX_LOAD_PER_CPU` (1.5) or free memory below `SWARM_MIN_FREE_MEM_MB` (1500).

## Decision guide

| Your repos | 1 to 2 tasks at once | 3 to 4 tasks at once | 5 or more |
| --- | --- | --- | --- |
| Docs, marketing, research, content | 2 vCPU / 4 GB is enough | 4 vCPU / 8 GB | 4 vCPU / 8 GB, the models are the limit |
| Small web app, tests under 5 minutes | 4 vCPU / 8 GB | 8 vCPU / 16 GB | 8 vCPU / 16 GB, offload full suites |
| Big monorepo, long test suites | 4 vCPU / 8 GB, offload full suites | 8 vCPU / 16 GB, offload full suites | Offload, or add a second worker |
| PHP or database-heavy (MySQL, Postgres, Redis in tests) | 8 vCPU / 16 GB, or offload tests | Offload tests | Offload tests |
| Browser tests (Playwright, Cypress) | 8 vCPU / 16 GB | Offload | Offload |

Rule of thumb: **if a full test suite takes more than about 10 minutes or more than about 2 GB of memory, offload it.** A bigger server still runs it one task at a time. A sandbox or CI (continuous integration) runner runs it next to the box and costs nothing while idle.

## Ways to offload heavy work

1. **GitHub Actions as the test runner.** The simplest. The worker opens a draft pull request and CI (continuous integration) runs the full suite there. The task only runs the tests for the files it touched.
2. **Script tasks.** The rule: full test suites always run as script tasks, never inside a model session. A script task runs a shell command with no model at all, so no tokens are spent watching a suite, the result is reliable, and it can run in parallel with model work:
   ```bash
   swarmctl enqueue -r YOUR-ORG/app --command "npm ci && npm test" --owner app-bot - <<< "Run the full test suite"
   ```
   The output lands in `artifacts/script.log` on the task, and a nonzero exit marks the task `failed` with outcome `script_failed`. A script task still runs on the worker, so it helps with tokens, not CPU. Point the command at a sandbox to help with CPU too.
3. **Cloud sandboxes (the remote-run pattern).** Services such as Cloudflare Sandbox, E2B, Modal or Daytona start a fresh container, run a command and give you the output. Use them from a script task command, or tell agents in the project brief to use them for full suites. Prove the setup once against CI (same commit, same result) before you trust it, and never edit the project's tests to make them pass in the sandbox. Never put production secrets in a sandbox.

   For a whole suite, run it as parallel shards rather than one long command:
   - **Shard and run in parallel.** Split the suite into balanced shards (by file count or past timings) and run each in its own sandbox at the same time.
   - **Pack once.** Archive the tree a single time and reuse that snapshot for every shard, instead of re-uploading it per shard.
   - **Pass pins through the environment.** The snapshot has no `.git`, so anything the sandbox cannot compute (the base commit, the PR base ref) is computed locally and passed as an environment variable.
   - **One total, honest exit codes.** Print one combined total across shards. Treat a runner-error exit (125-style: the sandbox or runner failed, not your tests) as a runner failure to retry or investigate, never as a failing test.
4. **A second cheap box.** A plain small server that runs only tests, reached over Tailscale.

Write your rule into each project brief ([../templates/project-bot-brief.md](../templates/project-bot-brief.md)), for example "full suite runs in CI, agents run only touched test files".

## Measure for one week

Before you resize, look at real use:

- `htop` during a busy hour. Are all CPUs pinned? Is swap in use?
- `swarmctl status` a few times a day. Are tasks sitting in `queued` while the box is idle? Then raise `SWARM_MAX_PARALLEL`.
- Out of memory kills: `journalctl -k | grep -i 'killed process'`.
- Worker log: `journalctl -u swarm-worker --since "7 days ago" | grep -i claim`.
- Disk: `df -h /` and the disk alert messages from the Chief of Staff.

## Growing later: bigger box or second worker

- **Resize the server** (most providers do this in a few minutes with a reboot). Best when one repo needs more memory per task.
- **Add a second worker.** Best when you want more tasks at once. Several workers can share one backend. On the new box, run the setup script, then:
  1. Stop and disable its own backend: `sudo systemctl disable --now swarm-backend`.
  2. Set `SWARM_URL` in its `/etc/agent-swarm/swarm.env` to the first box's backend address over Tailscale, for example `https://YOUR-WORKER.YOUR-TAILNET.ts.net`.
  3. Copy the first box's worker token into `/var/lib/swarm-runner/worker.token` (owner `swarm-runner`, `chmod 600`). Copy it with `scp` over Tailscale, never through chat. Never give it to the `agent` user.
  4. Start the worker: `sudo systemctl restart swarm-worker`.

  Each worker names itself with `SWARM_WORKER_NAME` (default: its hostname), which shows in `swarmctl status`.

### Resume is pinned to one worker

A task folder and the Claude or Codex session live only on the worker that ran it. So a task that waits for an answer, or ends with `next`, is resumed on the same worker when that worker is still checking in. If that worker is gone (or you moved to a new server and copied only the database), the task starts fresh on another worker with the full brief and every earlier answer, from the branch it pushed or from its saved bundle of unpushed commits. The report says it started fresh. If unpushed work was lost, the task waits with a clear question instead of guessing.
