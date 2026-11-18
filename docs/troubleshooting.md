# Troubleshooting

This doc lists common problems as symptom, check, fix. Start with the two commands that answer most questions:

```bash
sudo swarmctl status
journalctl -u swarm-backend -u swarm-worker -n 100 --no-pager
```

Run `swarmctl` as root on the worker to use the admin token, or as yourself with `SWARM_URL` and `SWARM_TOKEN_FILE` set.

## Setup problems

- **The setup script seems stuck at "Tailscale".** It is waiting for you to open the login link it printed and approve the machine (up to 15 minutes). Press Ctrl-C and run it again with `--tailscale later` to skip the wait, then join later with `sudo tailscale up --ssh`.
- **"Could not get lock /var/lib/dpkg/lock-frontend".** Another update is running, which is common on a new server. Since 0.2.1 the script waits up to 10 minutes for first-boot setup (cloud-init) and other apt or dpkg runs to finish, then tries apt up to 4 more times. If it still fails, see what holds the lock (`sudo fuser -v /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend`), wait for it to finish, and run the script again.
- **A step failed and the screen shows only a few lines.** The full installer output is in `/var/log/agent-swarm-setup.log`: `sudo tail -n 100 /var/log/agent-swarm-setup.log`.
- **"Claude Code did not install".** The installer failed (often a network problem). Run the setup script again; it skips what is already done.
- **`--lockdown` says "No usable admin account".** Only root can log in, and lockdown turns root login off. Make an admin user first ([worker-setup.md](worker-setup.md), step 3), or pass `--admin-user NAME`.
- **Folders pile up in `/srv/agent-swarm/outbox`.** Since 0.2.1 the runner removes a task's outbox folder (through `agent-exec cleanup`) once its results are saved, and the daily cleanup removes the rest after 7 days. A folder that stays longer belongs to a task that is not finished, or its results were never confirmed saved: check `sudo swarmctl show TASK_ID` and `journalctl -u swarm-worker | grep "outbox kept"`.
- **Every task fails with `PermissionError` on `inbox/...` or `outbox/...`.** This was a 0.2.0 bug. Upgrade to 0.2.1 or later. Check the folders: `stat -c '%a %U:%G' /srv/agent-swarm/inbox /srv/agent-swarm/outbox` must show `2750 swarm-runner:agent` and `2750 agent:swarm-runner`. Re-running the setup script sets them.

## The backend does not start

- **Check:** `journalctl -u swarm-backend -n 50`. Look for `SWARM_ADMIN_TOKEN_FILE is missing or empty; refusing to start`.
- **Fix:** make sure `/etc/agent-swarm/admin.token` exists, is not empty, and is readable by the `swarm` user (`root:swarm`, mode 640). Re-running `scripts/setup-worker.sh` recreates missing tokens and keeps existing ones. Also check `/var/lib/agent-swarm` is owned by `swarm`.

## HTTP 401 or 403

- **401 `missing bearer token` or `token not accepted`:** the token file is wrong, empty, or for a different backend. Check `SWARM_TOKEN_FILE` and `SWARM_URL`. A bot token stops working when you make a new one with `swarmctl bot token`.
- **403 `this token cannot do that`:** the token type cannot call that route. For example, project bots cannot list all bots, and the worker token cannot read reports. See the table in [backend.md](backend.md#who-can-call-what).
- **403 on queueing:** a project bot can queue only tasks it owns, and repo tasks only for projects that list it. Fix with `sudo swarmctl project set NAME --repo OWNER/REPO --bot BOT_ID`.
- **429 too many requests:** one address failed to log in too often (`SWARM_AUTH_FAIL_LIMIT`, 20 in 5 minutes). Fix the token, then wait a few minutes.
- **409 with `claim_ended`:** a runner tried to report on a task it no longer holds (the task was cancelled, answered, or given to another run). This is expected and safe; the runner stops that run and publishes nothing.

## A bot is not getting its results

1. **Check the bot is registered:** `sudo swarmctl bot show website-bot`. Is `notify_url` set? Is it https?
2. **Send a test:** `sudo swarmctl bot test website-bot`. Wait 10 seconds.
3. **Look at the outbox:** `sudo swarmctl notices` lists the last 100 deliveries (`GET /notices`). Each row has `state` (`pending`, `sent`, `failed`) and `last_error`.
   - `HTTP 401` or `403`: the webhook key is wrong. Set it again with `--notify-auth-file`.
   - `HTTP 404`: the webhook URL is wrong or the routine was deleted.
   - `redirects are not followed`: register the final URL, not one that redirects.
   - `not a public address`: the URL points at a private or local address. Only public https URLs are accepted.
   - `trigger key file is missing`: the bot's key file in `/etc/agent-swarm/bot-keys/` is gone. Set the key again with `--notify-auth-file`.
   - A network error: the backend retries 6 times with growing waits.
4. **Fallback:** after a failure that will not fix itself (most 4xx errors) or 6 tries, the event goes to the Chief of Staff with `reason: "fallback"` and `delivery_failed_for`. A `notify_failed` event is added to the task.
5. **Wrong bot got it:** the owner must match the bot id exactly as registered (case does not matter). A task with no owner goes to the Chief of Staff.

Walk the bot through [../templates/bot-setup-checklist.md](../templates/bot-setup-checklist.md) again.

## Duplicate tasks or duplicate messages

- **Same work queued twice:** the same prompt within 24 hours while the first is still active returns the first task with `duplicate: true`. Use `--key` (idempotency key) for exact control.
- **Error `idempotency_key was already used for a different task`:** you reused a key for new work. Use a new key for each new piece of work.
- **A bot handled the same event twice:** every notice carries an `event_id` (also sent as the `Idempotency-Key` header). Delivery is at least once, so a bot can see a repeat when a reply got lost. The handler must skip event ids it already handled, and can answer HTTP 409 or `{"duplicate": true}` so the backend stops retrying.

## A task is stuck in running

- **Check:** `swarmctl status` lists it under `STUCK (no heartbeat)` when its lease ran out.
- **What happens:** the worker heartbeats every 30 seconds. With no heartbeat for 10 minutes, the backend requeues the task (outcome `lost`), or fails it after `max_attempts`. Nothing to do in most cases.
- **If it is running but slow:** look at the runner's log `/var/lib/swarm-runner/logs/TASK_ID.log`, and the agent's files in `/home/agent/work/TASK_ID/.task-context/`. Runs (model and script) stop after `SWARM_TASK_TIMEOUT` seconds (default 4 hours). The watchdog alerts the Chief of Staff if a task runs 30 minutes past that.
- **To stop it:** `swarmctl cancel TASK_ID`. The task is cancelled at once; the runner stops the agent within a minute and publishes nothing.

## A task waits forever

- **Check:** `swarmctl show TASK_ID`. Look at `waiting_on` and `question`.
- `waiting_on: owner`: the owning bot must answer (`swarmctl answer`). Check that bot gets notices (above). After 12 hours the bot gets one reminder; after 24 hours the task goes on Waiting on you.
- `waiting_on: human`: it is on the Waiting on you list. Answer or drop it there.
- `outcome: attempt_cap`: it stopped resuming after too many tries. Answer with how to continue, or cancel it.

## A follow-up is refused or held

- **`409 ... message it instead`:** the parent task is still queued or running. Send it a message with `swarmctl message TASK_ID` instead of a follow-up.
- **`held behind TASK_ID`:** another follow-up of the same parent is not finished yet. Follow-ups of one parent run one at a time, in order. Nothing to do.
- **Follow-up of a task waiting on its owner:** the follow-up becomes the answer, and the parent resumes. It cannot add push or pull request flags the parent did not have.

## "The saved folder and session are not on this worker any more"

- **Sign:** the report's Handoff says the run started fresh.
- **Why:** cleanup removed the task folder, or the worker was rebuilt. The task starts again with the full brief and every earlier answer. Pushed work is checked out again, and unpushed work comes back from the saved work bundle when there is one.
- **Outcome `work_unavailable`:** the earlier run left commits that were never pushed and were not saved. The task waits and asks. Answer `continue fresh` to start again from the pushed branch, or cancel it.

## Pushing to the main branch is refused

- **Sign:** queueing with `--push-main` is refused with `push-main is off for this project`, or outcome `publish_failed` with `refusing to push main`.
- **Why:** the runner pushes only to `agent/<task id>` branches by default. Pushing straight to the main branch needs two opt-ins.
- **Fix:** `sudo swarmctl project set NAME --allow-push-main`, then queue with `swarmctl enqueue --push-main`. Turn it off again with `--no-push-main`.

## Every task fails with "per-task limits could not be applied" (exit 70)

- **Why:** the per-task CPU and memory limits use a systemd user manager for the `agent` user, and it is not running.
- **Fix:** `sudo loginctl enable-linger agent`, then `sudo systemctl restart swarm-worker`. Check with `systemctl status user@$(id -u agent)`.

## Usage limits

- **Sign:** run-agent exits with code 75 when the output mentions a usage or rate limit.
- **What happens:** if `SWARM_FALLBACK_PROVIDER` is set (default `codex`), the task is retried on that provider right away. Otherwise the task goes back to the queue with a wait of 1 hour, then 2, 4 and so on, up to 24 hours, until `max_attempts`.
- **Fix:** wait, move work to the provider with quota, or lower `SWARM_MAX_PARALLEL`. See [usage-and-cost.md](usage-and-cost.md).

## The agent ended without AGENT_STATUS

- **Sign:** task `failed` with outcome `no_status`.
- **Check:** `/var/lib/swarm-runner/logs/TASK_ID.log` and `.task-context/run.log` in the task folder. Often the CLI is logged out, crashed, or timed out (outcome `timeout`).
- **Fix:** fix the cause, then queue it again, or add "Always end by writing AGENT_STATUS" to the brief.

## Push or pull request failed

- **Sign:** outcome `publish_failed` with the git or gh error.
- **Check:** `sudo -u swarm-runner -H gh auth status`. Does the account have write access to the repo? Only `swarm-runner` holds the GitHub login; `agent` must not.
- **Fix:** `sudo -u swarm-runner -H gh auth login` then `sudo -u swarm-runner -H gh auth setup-git`, or grant access. The work is saved as a work bundle with the task; queue a follow-up with `swarmctl enqueue --follow-up TASK_ID` and it picks the work up again.
- **`not pushed: possible secret in new lines`:** the runner found something that looks like a key in the new commits and refused to push. Remove it from the history and rotate the key ([secrets.md](secrets.md#if-a-secret-leaks)).

## Out of memory

- **Check:** `journalctl -k | grep -i 'killed process'` and `free -h`.
- **Fix:** lower `SWARM_MAX_PARALLEL`, move full test suites to CI (continuous integration) or a sandbox, or grow the box. See [sizing-and-sandboxes.md](sizing-and-sandboxes.md).

## Disk full

- **Sign:** an alert from the Chief of Staff saying the disk is over 80 percent.
- **Check:** `df -h /` and `sudo du -sh /home/agent/work/* | sort -h | tail`.
- **Fix:** `sudo -u swarm-runner python3 /opt/agent-swarm-kit/current/server/swarm_cleanup.py --dry-run` to see what cleanup would remove, then run it without `--dry-run`. Before deleting a folder it saves the git work and any files not in git to `/home/agent/backups/work/DATE/`. Lower `SWARM_KEEP_DAYS`. Clear old caches (`npm cache clean --force`). Grow the disk if it keeps happening.

## Bots cannot reach the backend

- **Check, with Tailscale Serve (recommended):** `tailscale serve status` on the worker. On the bot's computer, `tailscale status` shows the worker, and `curl https://YOUR-WORKER.YOUR-TAILNET.ts.net/health` returns `{"ok": true, ...}`.
- **Fix:** run `sudo tailscale serve --bg 8750` again. Check the bot computer has the tag `tag:swarm-bot` and your access rules let it reach `tag:swarm-worker:443` ([worker-setup.md](worker-setup.md#tailscale-access-rules)).
- **With Funnel:** `tailscale funnel status`, and make sure your tailnet policy allows Funnel for this machine.
- Either way: check `swarm-backend` is running and `SWARM_BIND` is `127.0.0.1:8750`.

## The worker does not claim new tasks

- **Check:** `journalctl -u swarm-worker -n 50`. Tasks stay `queued` while the box looks idle.
- **Causes:**
  - The health gate: load per CPU above `SWARM_MAX_LOAD_PER_CPU` (1.5) or free memory below `SWARM_MIN_FREE_MEM_MB` (1500). Check with `uptime` and `free -m`.
  - All slots full: `SWARM_MAX_PARALLEL` tasks already running.
  - A task waiting out a usage limit backoff is not due yet.
  - The worker is drained: `ls /var/lib/swarm-runner/drain`. Undo with `sudo swarmctl undrain`.
  - `claim failed` in the log: wrong `SWARM_URL` or worker token. Compare `/var/lib/swarm-runner/worker.token` with `/etc/agent-swarm/worker.token`.

## The model CLIs are logged out

- **Sign:** every task fails fast, and `agent-stderr.log` mentions login or authentication.
- **Fix:** log in again as the agent user: `sudo -iu agent claude` then `/login`, and `sudo -iu agent codex login --device-auth`. Then requeue the failed tasks.
