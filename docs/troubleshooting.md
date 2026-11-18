# Troubleshooting

This doc lists common problems as symptom, check, fix. Start with the two commands that answer most questions:

```bash
sudo swarmctl status
journalctl -u swarm-backend -u swarm-worker -n 100 --no-pager
```

Run `swarmctl` as root on the worker to use the admin token, or as yourself with `SWARM_URL` and `SWARM_TOKEN_FILE` set.

## The backend does not start

- **Check:** `journalctl -u swarm-backend -n 50`. Look for `SWARM_ADMIN_TOKEN_FILE is missing or empty; refusing to start`.
- **Fix:** make sure `/etc/agent-swarm/admin.token` exists, is not empty, and is readable by the `swarm` user (`root:swarm`, mode 640). Re-running `scripts/setup-worker.sh` recreates missing tokens and keeps existing ones. Also check `/var/lib/agent-swarm` is owned by `swarm`.

## HTTP 401 or 403

- **401 `missing bearer token` or `token not accepted`:** the token file is wrong, empty, or for a different backend. Check `SWARM_TOKEN_FILE` and `SWARM_URL`. A bot token stops working when you make a new one with `swarmctl bot token`.
- **403 `this token cannot do that`:** the role cannot call that route. For example, project bots cannot claim tasks or list all bots. See the table in [backend.md](backend.md).
- **403 on queueing:** a project bot can queue only tasks it owns. Leave `--owner` off or set it to the bot itself.

## A bot is not getting its results

1. **Check the bot is registered:** `sudo swarmctl bot show website-bot`. Is `notify_url` set? Is it https?
2. **Send a test:** `sudo swarmctl bot test website-bot`. Wait 10 seconds.
3. **Look at the outbox:** `sudo swarmctl notices` lists the last 100 deliveries (`GET /notices`). Each row has `state` (`pending`, `sent`, `failed`) and `last_error`.
   - `HTTP 401` or `403`: the webhook key is wrong. Set it again with `--notify-auth-file`.
   - `HTTP 404`: the webhook URL is wrong or the routine was deleted.
   - A network error: the backend retries 6 times with growing waits.
4. **Fallback:** after a failure that will not fix itself (most 4xx errors) or 6 tries, the event goes to the Chief of Staff with `reason: "fallback"` and `delivery_failed_for`. A `notify_failed` event is added to the task.
5. **Wrong bot got it:** the owner must match the bot id exactly as registered (case does not matter). A task with no owner goes to the Chief of Staff.

Walk the bot through [../templates/bot-setup-checklist.md](../templates/bot-setup-checklist.md) again.

## Duplicate tasks or duplicate messages

- **Same work queued twice:** the same prompt within 24 hours while the first is still active returns the first task with `duplicate: true`. Use `--key` (idempotency key) for exact control.
- **Error `idempotency_key was already used for a different task`:** you reused a key for new work. Use a new key for each new piece of work.
- **A bot handled the same event twice:** every notice carries an `event_id` (also sent as the `Idempotency-Key` header). The backend never sends the same event to the same bot twice, but a bot routine that crashed mid-way may see a retry. The handler must skip event ids it already handled.

## A task is stuck in running

- **Check:** `swarmctl status` lists it under `STUCK (no heartbeat)` when its lease ran out.
- **What happens:** the worker heartbeats every 30 seconds. With no heartbeat for 10 minutes, the backend requeues the task (outcome `lost`), or fails it after `max_attempts`. Nothing to do in most cases.
- **If it is running but slow:** look at the agent's log in `/home/agent/work/TASK_ID/.task-context/run.log` and `agent-output.jsonl`. Runs stop after `SWARM_TASK_TIMEOUT` seconds (default 4 hours).
- **To stop it:** `swarmctl cancel TASK_ID`. It stops at the next heartbeat.

## A task waits forever

- **Check:** `swarmctl show TASK_ID`. Look at `waiting_on` and `question`.
- `waiting_on: owner`: the owning bot must answer (`swarmctl answer`). Check that bot gets notices (above).
- `waiting_on: human`: it is on the Waiting on you list. Answer or drop it there.
- `outcome: attempt_cap`: it stopped resuming after too many tries. Answer with how to continue, or cancel it.

## Usage limits

- **Sign:** run-agent exits with code 75 when the output mentions a usage or rate limit.
- **What happens:** if `SWARM_FALLBACK_PROVIDER` is set (default `codex`), the task is retried on that provider right away. Otherwise the task goes back to the queue with a wait of 1 hour, then 2, 4 and so on, up to 24 hours, until `max_attempts`.
- **Fix:** wait, move work to the provider with quota, or lower `SWARM_MAX_PARALLEL`. See [usage-and-cost.md](usage-and-cost.md).

## The agent ended without AGENT_STATUS

- **Sign:** task `failed` with outcome `no_status`.
- **Check:** `.task-context/run.log` and `agent-stderr.log` in the task folder. Often the CLI is logged out, crashed, or timed out (exit 124).
- **Fix:** fix the cause, then queue it again, or add "Always end by writing AGENT_STATUS" to the brief.

## Push or pull request failed

- **Sign:** outcome `publish_failed` with the git or gh error.
- **Check:** `sudo -iu agent gh auth status`. Does the account have write access to the repo?
- **Fix:** `sudo -iu agent gh auth login`, or grant access. The commits are still in the task folder; queue a follow-up with `--push --branch agent/TASK_ID` or push by hand. The worker never pushes `main` or `master`.

## Out of memory

- **Check:** `journalctl -k | grep -i 'killed process'` and `free -h`.
- **Fix:** lower `SWARM_MAX_PARALLEL`, move full test suites to CI (continuous integration) or a sandbox, or grow the box. See [sizing-and-sandboxes.md](sizing-and-sandboxes.md).

## Disk full

- **Sign:** an alert from the Chief of Staff saying the disk is over 80 percent.
- **Check:** `df -h /` and `sudo du -sh /home/agent/work/* | sort -h | tail`.
- **Fix:** `sudo -iu agent python3 /opt/agent-swarm-kit/server/swarm_cleanup.py --dry-run` to see what cleanup would remove, then run it without `--dry-run`. Lower `SWARM_KEEP_DAYS`. Clear old caches (`npm cache clean --force`). Grow the disk if it keeps happening.

## Bots cannot reach the backend over Tailscale Funnel

- **Check:** `tailscale funnel status` on the worker. From a phone off your network, open `https://YOUR-WORKER.YOUR-TAILNET.ts.net/health`; it should return `{"ok": true, ...}`.
- **Fix:** run `sudo tailscale funnel --bg 8750` again. Make sure Funnel is allowed for this machine in the Tailscale admin console (it is controlled by your tailnet policy). Check `swarm-backend` is running and `SWARM_BIND` is `127.0.0.1:8750`.

## The worker does not claim new tasks

- **Check:** `journalctl -u swarm-worker -n 50`. Tasks stay `queued` while the box looks idle.
- **Causes:**
  - The health gate: load per CPU above `SWARM_MAX_LOAD_PER_CPU` (1.5) or free memory below `SWARM_MIN_FREE_MEM_MB` (1500). Check with `uptime` and `free -m`.
  - All slots full: `SWARM_MAX_PARALLEL` tasks already running.
  - A task waiting out a usage limit backoff is not due yet.
  - `claim failed` in the log: wrong `SWARM_URL` or worker token. Compare `/home/agent/.config/swarm/worker.token` with `/etc/agent-swarm/worker.token`.

## The model CLIs are logged out

- **Sign:** every task fails fast, and `agent-stderr.log` mentions login or authentication.
- **Fix:** log in again as the agent user: `sudo -iu agent claude` then `/login`, and `sudo -iu agent codex login --device-auth`. Then requeue the failed tasks.
