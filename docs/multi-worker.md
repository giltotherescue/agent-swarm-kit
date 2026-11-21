# Optional: more than one worker

Start with one worker. Add another only if there is a reason: a home computer plus a cloud machine
so either can take unpinned jobs, more parallel capacity, a GPU or other special hardware, a Mac for
Apple-only builds, or a separate box for untrusted work. The supplied installer for servers uses Ubuntu and
systemd. A Mac has its own, simpler installer (below), with less isolation.

## Point the new worker at the same queue

Keep one backend and its SQLite database on one machine. Do not copy or share the live database.
Install the worker on the extra box but don't start it yet. Disable the extra box's local queue with
`sudo systemctl disable --now swarm-backend`. Keep the original backend reachable from both workers
over your private network (see [backend access](backend.md#reaching-the-backend-from-your-bots)).

Edit `/etc/agent-swarm/swarm.env` on each worker. Choose unique, stable names, and use the original
backend's private HTTPS address for `SWARM_URL` on the new worker. For example:

```dotenv
SWARM_WORKER_NAME=build-box
SWARM_URL=https://BACKEND-PRIVATE-ADDRESS
```

On the extra worker, securely install the shared queue's worker token at
`/var/lib/swarm-runner/worker.token` (owned by `swarm-runner`, mode 600). Never send it through chat.
Set up model logins and repo access there as needed. Follow [Add a machine](secrets.md#add-a-machine-rotate-or-recover)
to give it its own age key and only the secret bundles it needs. Then start `swarm-worker` and check
that it can run a tiny report-only task.

## Example: a Mac as the second worker

A spare Mac or a home machine is an easy second worker, for example to run Apple-only builds. It runs the same
runner as one launchd agent, as one user, with no per-job caps and no separate users, so it is less isolated than
an Ubuntu worker ([macos-worker.md](macos-worker.md) explains exactly how). The settings in this page are the same:
a unique `SWARM_WORKER_NAME`, the original backend's private HTTPS address as `SWARM_URL`, and its worker token. The installer
takes them as options and writes them to `~/.config/agent-swarm/swarm.env`:

```bash
bash scripts/setup-macos.sh --backend-url https://BACKEND-PRIVATE-ADDRESS --worker-token-file ~/worker.token --worker-name spare-mac
```

Send the worker token over a private channel and delete the copy after the install (the installer keeps its own, mode 600). Pin Apple-only work
with `swarmctl enqueue --worker spare-mac ...`, and keep sensitive repos off it with `SWARM_WORKER_PROJECTS`. Because jobs there run as you
and can read your GitHub login, do not use a Mac that holds your personal files for untrusted work.

## Route jobs only when you need to

- Unpinned tasks go to the first eligible worker. Pin one when it needs something specific:
  `swarmctl enqueue --worker build-box -p website brief.md` (API field: `target_worker`). A pinned
  task waits rather than spilling to another machine. Follow-ups inherit the pin unless you supply
  a different worker; answering a waiting task does not change the pin.
- To limit a worker to certain projects, set `SWARM_WORKER_PROJECTS=website,docs` and restart it.
  Names must match exactly. With a list, tasks without a project are not eligible. Omit the setting
  to allow all projects; an explicitly empty list allows none. A pin never overrides an allowlist.
- Optional: prefer one worker. Off by default: unpinned tasks are first-come. To make one worker the
  primary (say a fast home box, with a cloud box as fallback), set on the **backend's** `swarm.env`
  `SWARM_PRIMARY_WORKER=build-box` (that worker's `SWARM_WORKER_NAME`) and restart `swarm-backend`. The
  primary takes unpinned work at once. Any other worker takes an unpinned task only when (1) the primary
  is stale: it has not claimed or heartbeated for `SWARM_PRIMARY_STALE_SECONDS` (default 90; an idle
  worker checks in every `SWARM_POLL_SECONDS`, 20), was never seen, or is draining; (2) the task has
  waited `SWARM_FALLBACK_AFTER_SECONDS` (default 120), which covers a primary that is full and not
  claiming; (3) the project is not on the primary's `SWARM_WORKER_PROJECTS` list; or (4) the task's
  workspace is on that worker. The wait counts from when the task was queued (or its `not_before`), or
  from its last requeue or answer once it has run. Pins (`--worker`) always win, both ways. Liveness is
  the `last_seen` column shown by `swarmctl status`. Empty or unset `SWARM_PRIMARY_WORKER` turns it off.
- Unpinned follow-ups may wait for the worker that holds the earlier workspace while it is still
  checking in. Drain that worker before moving work. A different worker can use saved reports and
  bundles, but it does not have the previous model session.

SQLite gives each task one active claim. Each claim has its own token. If a lease expires and another
worker takes it, the old token cannot update or finish the task. The old process may still run until
it sees the lease loss, so this does not make external side effects exactly once. The one backend
is still a single point of failure. Names and allowlists are routing settings, not a security boundary:
all workers with the shared token are trusted by the queue. For untrusted work, use separate credentials,
no shared runtime secrets, and a separate backend if you can't trust its runner with the same queue.
