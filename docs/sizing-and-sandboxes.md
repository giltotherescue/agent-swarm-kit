# Sizing the worker, and when to use sandboxes

This doc helps you decide how big your worker server should be, and when to send heavy jobs somewhere else instead of buying a bigger box. The answer depends on two things: what your repos need to build and test, and how many tasks you want running at the same time.

## The starting point

**4 vCPU (virtual CPUs), 8 GB of memory, 80 GB or more of disk, with `SWARM_MAX_PARALLEL=2`.**

That runs two tasks at once with room to spare for most small projects. Change `SWARM_MAX_PARALLEL` in `/etc/agent-swarm/swarm.env` and run `sudo systemctl restart swarm-worker`.

## What actually uses the resources

- **The model CLIs are light.** Claude Code and Codex mostly wait for the model, which runs in the provider's cloud. Each uses a few hundred MB of memory.
- **Your repo's tools are heavy.** Installing packages, builds, type checks, full test suites, databases, headless browsers and dev servers are what fill CPU and memory.
- **Disk grows with checkouts.** Each task gets its own folder in `/home/agent/work`. The daily cleanup removes them after `SWARM_KEEP_DAYS` (default 7).

Each task is limited to `SWARM_TASK_CPU_QUOTA` (default `200%`, meaning 2 CPUs) and `SWARM_TASK_MEMORY_MAX` (default `75%` of memory). The worker also refuses new work while the box is busy: load per CPU above `SWARM_MAX_LOAD_PER_CPU` (1.5) or free memory below `SWARM_MIN_FREE_MEM_MB` (1500).

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
2. **Script tasks.** A script task runs a shell command with no model at all, so no tokens are spent watching a suite:
   ```bash
   swarmctl enqueue -r YOUR-ORG/app --command "npm ci && npm test" --owner app-bot - <<< "Run the full test suite"
   ```
   The output lands in `artifacts/script.log` on the task, and a nonzero exit marks the task `failed` with outcome `script_failed`. A script task still runs on the worker, so it helps with tokens, not CPU. Point the command at a sandbox to help with CPU too.
3. **Cloud sandboxes.** Services such as Cloudflare Sandbox, E2B, Modal or Daytona start a fresh container, run a command and give you the output. Use them from a script task command, or tell agents in the project brief to use them for full suites. Never put production secrets in a sandbox.
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
  3. Copy the first box's worker token into `/home/agent/.config/swarm/worker.token` (owner `agent`, `chmod 600`). Copy it with `scp` over Tailscale, never through chat.
  4. Start the worker: `sudo systemctl restart swarm-worker`.

  Each worker names itself with `SWARM_WORKER_NAME` (default: its hostname), which shows in `swarmctl status`.
