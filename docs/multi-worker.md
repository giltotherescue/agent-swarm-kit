# Optional: more than one worker

Start with one worker. Add another only if there is a reason: a home computer plus a cloud machine
so either can take unpinned jobs, more parallel capacity, a GPU or other special hardware, a Mac for
Apple-only builds, or a separate box for untrusted work. The supplied installer uses Ubuntu and
systemd. A native Mac runner is not part of the kit; it needs its own runner integration.

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

## Route jobs only when you need to

- Unpinned tasks go to the first eligible worker. Pin one when it needs something specific:
  `swarmctl enqueue --worker build-box -p website brief.md` (API field: `target_worker`). A pinned
  task waits rather than spilling to another machine. Follow-ups inherit the pin unless you supply
  a different worker; answering a waiting task does not change the pin.
- To limit a worker to certain projects, set `SWARM_WORKER_PROJECTS=website,docs` and restart it.
  Names must match exactly. With a list, tasks without a project are not eligible. Omit the setting
  to allow all projects; an explicitly empty list allows none. A pin never overrides an allowlist.
- Unpinned follow-ups may wait for the worker that holds the earlier workspace while it is still
  checking in. Drain that worker before moving work. A different worker can use saved reports and
  bundles, but it does not have the previous model session.

SQLite gives each task one active claim. Each claim has its own token. If a lease expires and another
worker takes it, the old token cannot update or finish the task. The old process may still run until
it sees the lease loss, so this does not make external side effects exactly once. The one backend
is still a single point of failure. Names and allowlists are routing settings, not a security boundary:
all workers with the shared token are trusted by the queue. For untrusted work, use separate credentials,
no shared runtime secrets, and a separate backend if you can't trust its runner with the same queue.
