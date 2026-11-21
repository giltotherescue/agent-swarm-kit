# Changelog

All notable changes to this kit. Versions follow semantic versioning (MAJOR.MINOR.PATCH):
MAJOR breaks something you set up, MINOR adds something, PATCH fixes something.
See docs/updating-the-kit.md for how to update.

## 0.8.1 (unreleased)

Owner required on enqueue, ported from agent-control PR 72.

- **`POST /tasks` rejects a missing or empty `owner`** (400, says to pass `--owner <bot id or alias>`). A project bot token still
  defaults to its own id; a follow-up inherits its parent's owner. Chains and other server-created tasks are not affected.
- **`swarmctl enqueue` and `swarmctl review` fail fast without `--owner`** unless `ENQUEUE_OWNER` is set; an explicit `--owner` wins.
  A `--follow-up` needs neither.
- The kit has no bot alias table (only bus agent names and aliases) and no dashboard, so there is no `swarm` alias to add.

Parent report and artifacts for follow-up and child tasks, ported from agent-control PR 71.

- **`.task-context/parent/`** holds the parent's uploaded `REPORT.md` and artifacts (sanitized names; unsafe ones skipped and
  noted). The runner fetches them with the task token before the agent starts; the agent has no key, so the URLs 401.
  Missing pieces are noted in `.task-context/README` and `parent/NOTES.md`, never a failure. The brief and run rules say where.
- **Small backend read rule:** a task token may GET the report and artifact list/files of the parent (`parent_id`) of the
  task it holds, even when another worker ran it. Nothing else of the parent (attachments, bundles) is readable.
- Update `swarm_worker.py`, `agent-exec` and `swarm_backend.py` together.

Docs note on the pack-once, shard-in-parallel remote-run pattern, ported from agent-control PR 70. See
[docs/sizing-and-sandboxes.md](docs/sizing-and-sandboxes.md). Docs only; the kit ships no task-shim or remote-run.

Docs note on Sandbox snapshot metadata and `ENGINE_REVISION`, ported from agent-control PRs 73 and 74: no `._*`/`.DS_Store`
(`COPYFILE_DISABLE=1`), no `.git`, and an optional root `ENGINE_REVISION` file for a pinned submodule SHA. See
[docs/sizing-and-sandboxes.md](docs/sizing-and-sandboxes.md). Docs only; the kit ships no packer or runner.

Per-project test-base env var, ported from agent-control PR 69. See [docs/git-policy.md](docs/git-policy.md).

- **`CLOZY_TEST_BASE=origin/<PR base>`** is exported for the whole task of a `clozy` task (so `test:changed` can narrow); the
  branch is the task's `--base`, else its parent's, else `origin/main`. Unsafe names fall back to `origin/main`; a variable
  already set (for example in `agent.env`) is left alone. Map is `SWARM_TASK_BASE_ENV` (`project=NAME,...`; default
  `clozy=CLOZY_TEST_BASE`, empty turns it off). Update `swarm_worker.py` and `agent-exec` together.

Primary/fallback worker routing, ported from agent-control PR 68. See [docs/multi-worker.md](docs/multi-worker.md).

- **Optional primary worker.** New backend setting `SWARM_PRIMARY_WORKER`, empty by default (first-come, as before). When
  set, other workers take an unpinned task only if the primary is stale (`SWARM_PRIMARY_STALE_SECONDS`, 90), never seen or
  draining, the task has waited `SWARM_FALLBACK_AFTER_SECONDS` (120), the project is off the primary's allowlist, or the
  task's workspace is on the claiming worker. Pins (`--worker`) override. See docs/multi-worker.md.
- **No schema change.** Liveness reuses the existing `workers.last_seen`, already written on every claim and heartbeat.
- **Not ported:** the `remote-run-broker` socket setting and LaunchAgent example; the kit ships no remote-run broker.

The review queue, a simple port of agent-control PR 62. See [docs/review-queue.md](docs/review-queue.md).

- **File, list, decide.** Bots file items (`decision`, `copy`, `creative`, `social_post`, `deliverable`, `pr`) with
  `PUT /review/items`, upserted by project, bot and key (new revision on change, `unchanged` on an identical re-send,
  reopened after changes requested, a new item after approve or reject). The admin token or Chief decides with
  `POST /review/items/ID/decision`: approve, changes or reject, with an optional note, edited text and chosen option. New
  `review_items` table, added in place; `swarmctl review-queue` (`swarmctl rq`) file|list|show|decide|ack|close.
- **Stale-click guard.** Every read carries `content_hash` (SHA-256 of the ask, options and recommendation). A decision must
  send it; a missing or changed hash gets 409 `stale` and nothing is recorded.
- **Delivery.** The decision goes to the filing bot's bus agent as an urgent message on thread `review:ID` (pointer-only if
  the text is too long or refused). A bot without a bus agent reads it from `GET /review/items?status=answered&acked=0`.
  `POST /review/items/ID/ack` marks it consumed (and the bus message read).
- **Poll window.** Poll agents may set `poll_window_minutes` (10 to 10080; `swarmctl bus register --poll-window`) so they are
  not reported unreachable between check-ins. New `agents.poll_window_minutes` column, added in place.
- **Not ported:** the dashboard UI, meeting flags, SLA alerts, batches and sync, media, fill-ins and variants, the legacy
  item migration, and linked-task propagation with its retry ledger.

Auto-commit never stages submodule pins, ported from agent-control PR 63. See [docs/git-policy.md](docs/git-policy.md).

- **Submodule gitlinks are never auto-committed.** The kit's auto-commits, the backend's weekly project-log export and `swarmctl log export --dir DIR --git`, share one guarded commit. It runs in a copy of the index and unstages added, modified and deleted gitlinks (index mode 160000), found from the index so `.gitmodules` is not needed and `diff.ignoreSubmodules=all` cannot hide them. A pin replaced by files, a pin and an ordinary file at the same path, a git error, a 60-second git timeout or a gitlink still staged aborts the commit and leaves the caller's index untouched. The commit is built with plumbing from exactly the inspected index (`write-tree`, `commit-tree`, compare-and-swap `update-ref`), so repository hooks never run for these automated commits: a formatting hook cannot stage the skipped pin, and a slow post-commit hook cannot report a failure after HEAD moved. `swarmctl log export --git` no longer runs `git add -A` on the real index or passes git's output through; it prints a redacted skip note, and on a failure a redacted error with exit code 1. The stock worker still never commits leftover work.
- **Skips are recorded.** A finish may carry `gitlink_skipped` (path, old sha, new sha; at most 200) and `gitlink_skipped_count` (the true total). They are stored on the task, posted as a `publish` event, kept as exactly one note in the stored REPORT.md (also on publish failures; the note sits between `<!-- swarm:gitlink-skip-note -->` and `<!-- /swarm:gitlink-skip-note -->` lines, so report prose quoting its wording or a marker does not count) and added to bus notices (a WARNING line ahead of the report excerpt) and legacy webhook payloads (at most 10 entries, plus the true count). Each finish that sends `gitlink_skipped` replaces the earlier observation, also on a resumed claim; an explicit `[]` clears the list, the count and the note, and omitting the field keeps them and puts the note into the current report (also a fresh one uploaded by a resumed run). Only a generated block, each marker alone on its own line, is ever removed from a report; a marker quoted in prose is left alone.
- **Redacted at the source, before truncation.** Every gitlink field, every log or error line that names one, and git's stderr in an export error goes through the secret patterns and agent-control's key=value rule (`TOKEN=...` becomes `TOKEN=[redacted]`) on the full value, in the worker before sending (including a failing collector's diagnostic and the `/finish` error), in both exports before logging, and again in the backend before storage, bus or webhook. A token-shaped path no longer gets the bus notice rejected.
- **`swarmctl enqueue --allow-gitlink`** records that a pin bump is intended (task field `allow_gitlink`: `true`, `1`, `"true"`, `"1"` or `"yes"` opt in; `false`, `0`, `"false"`, `"0"`, `"no"` or `""` do not; anything else, including JSON `null`, is a 400). A follow-up inherits the parent's value only when it omits the field; an explicit value, JSON `false` included, overrides it. It is metadata for a worker that auto-commits; with the stock kit, commit an intentional pin bump explicitly.

The publish policy, ported from agent-control PR 65 where the kit publishes. See docs/concepts.md, "Pull requests".

- **`open_pr` is an absolute veto.** Only `--pr` opens a PR. The worker's single `gh pr create` call refuses without it,
  and the backend drops (with a warning event) a PR link reported by a task without `--pr` unless it is the PR the task
  was told to update (`--pr-number` or the parent's PR it inherited as a follow-up).
- **PR base, never a guessed `main`.** A replacement for a merged or closed target PR uses that PR's live base, read from
  GitHub after the push; if it cannot be read, no PR is opened. A fresh PR uses `--base`, then the parent's base, then the
  repo default as GitHub (or origin's HEAD) reports it; if neither answers, `--base` is left out and GitHub chooses.
- **Terminal targets reported honestly.** A `--pr-number` or inherited follow-up PR that was merged or closed is never
  reported as the task's PR or as merged by it. The work is still pushed; the branch, commit and reason go to a publish
  event (with `target_pr_state`) and a `## Publish note` at the end of REPORT.md. An open target is now reported as the
  task's `pr_url` (a `--pr --pr-number N` task used to end "no PR was confirmed").
- **Explicit null clears.** A finish with `pr_url: null` clears a PR link an earlier claim stored; an absent `pr_url`
  keeps it.
- **`--base` is persisted** (`swarmctl enqueue --base NAME`, new `tasks.base` column added in place): inherited by
  follow-ups, and kept when a follow-up answers a waiting task, only when the answer is accepted. `POST /tasks/ID/answer`
  takes `base` the same way.
- **Not ported:** PR 65's merge gate (CI allowlist, null/empty rollup handling, SHA-pinned REST merge, auto-merge
  disable) and deploy credit. The kit never merges or deploys; a human merges.

Orchestrator message delivery during long busy turns, and one-line pointer delivery, ported from agent-control PRs 64
and 67. See [docs/orchestrators.md](docs/orchestrators.md) and [docs/agent-bus.md](docs/agent-bus.md).

- **Busy delivery.** A working session no longer holds its queue for the whole turn. The oldest queued message is sent once
  it is `SWARM_ORCH_BUSY_MAX_WAIT_SECONDS` (90) old, or `SWARM_ORCH_BUSY_URGENT_WAIT_SECONDS` (15) when its first line
  says `URGENT`, counted from when the backend queued it; the rest of a continuous backlog follows
  `SWARM_ORCH_BUSY_GAP_SECONDS` (5) apart. Idle delivery on an empty prompt is unchanged (at once).
- **One typed pointer line.** Claude Code 2.1.321 collapses a pasted multi-line message into `[Pasted text #N +L lines]`,
  which cannot be verified, so nothing multi-line is pasted any more. A bus message (header and body) is written by
  `agent-exec` to `<WORK>/orchestrators/NAME/.orchestrator/inbox/msg-ID.md` (mode 0600, written atomically, newest 200
  and at most 7 days kept) and the session gets one line typed as literal keys: `New bus message #41 from chief
  (URGENT): read <that path>`. A short one-line direct message is typed as is, `[Message from SENDER via swarm] TEXT`.
- **Verified, recorded, then Enter.** `agent-exec orch-type` types the line into a confirmed empty editor and waits until
  the editor holds exactly it (rows joined by display width, so an 80x24 pane works); otherwise it erases its own partial
  text and no Enter follows. The supervisor then saves a `sending` record (fsync) and `agent-exec orch-enter` captures the
  same pane ID again, presses Enter only while the editor still holds exactly the line, and confirms. A receipt needs the
  line itself above the editor more often than before typing; Claude Code's generic queued-message hint is not one.
- **Recovery never retypes.** After a crash the record decides: the line above the editor is a receipt, the line still
  in the editor gets Enter (at most 3 in all), anything else is held and reported once as `delivery-unknown`. Holds,
  sending records and pending receipts live in the state file, so a replacement supervisor never types a message twice.
- **Release.** `swarmctl orchestrator release NAME ID` marks a held message delivered; `--resend` allows exactly one more
  typing, used up (saved) before the keys go in; if it does not land the message is held again. New admin route
  `POST /orchestrators/NAME/messages/ID/release` and column `orchestrator_messages.release_count`, added in place.
- **Fail closed.** Nothing is typed and no Enter pressed on a menu, a draft, an unknown footer, an unreadable pane or a
  failed capture, during idle and busy delivery and the stop wrap-up alike. A message held back for
  `SWARM_ORCH_DEFER_REPORT_SECONDS` (120) is reported once per reason as a `delivery-deferred` event, with no pane
  contents. One pane parser, defined in `agent-exec` and loaded by the supervisor.
- **Sign-in and first-run screens.** Sign-in text above a live, empty editor (a tool printing `Invalid API key`) is
  conversation output and no longer ends a healthy session as `needs-login`. The 2.1.321 theme picker (no key hint) is
  recognized from its full layout and gets Enter; the folder trust question is answered from its selection. First-run
  keys are sent only in the first 120 seconds after start; a first-run screen later is reported, never answered.
- **Busy event recorded once.** The receipt carries `delivered_busy_seconds` and the backend records
  `message-delivered-busy` in the same first-delivery update as `message-delivered`.
- **Older backend fallback.** Only the exact `bad event type` 400 for the two new event types makes the supervisor drop
  them; any other 400 keeps every event. `server/swarm_bus.py` imports on Python 3.11 again.
- **Rollout.** The supervisor and `agent-exec` change together (`orch-send` is replaced by `orch-capture`, `orch-type` and
  `orch-enter`); update both, then restart the orchestrator supervisor. See docs/updating-the-kit.md.

## 0.8.0 (2026-11-21)

The Agent Bus: reliable messages between your agents, ported from agent-control. Nothing is on until you register agents.
See [docs/agent-bus.md](docs/agent-bus.md).

- **Messages with receipts.** `swarmctl bus send|inbox|ack|status|thread|agents|whoami` (admin: `register`, `retire`,
  `token`). Every message is stored first, then woken: a webhook to the bot's inbox at write time with retries at 1, 5, 15
  and 60 minutes, typed into an orchestrator's pane, or read from the agent's inbox. States `queued`, `delivered`,
  `read`, `failed`, every attempt logged, idempotent ack (`swarmctl bus ack` exits 3 when already acked), "newer messages in this
  thread" on every delivery, stable names with aliases, 3,700 character limit, secrets refused, control characters stripped.
- **Per-agent tokens** (`ab_...`, HMAC-derived from `bus-token-secret`, revoked by rotating), written to 0600 files and never printed.
- **Wake policy.** Only urgent messages, blockers, questions (`reply_expected`) and finish notices wake a bot at once;
  everything else waits for one digest per agent (`--digest-minutes`, default 60, minimum 5). Held messages expire after
  24 hours. `--noop` stores a message that never wakes anyone. Unreachable agents alert the alert agent (`chief`) only after
  15 minutes, at most once an hour; humans are never alerted.
- **Finish notices over the bus** (opt-in, `--notices bus`): the owner's notice goes to the bus only (no bot webhook, no
  Chief copy). If the bus send fails, the normal path runs, Chief copy included. The agent's token can read its own bot's
  task and report.
- **Done means landed.** The worker reports `unpushed_commits` and `head_sha`. A code task with work only on the worker ends
  `waiting`/`blocked` ("NOT on GitHub") instead of `done`. Notices carry `head_sha` and `landed`.
- **Orchestrators** check for messages every 10 seconds while idle, get a bus token at start (`BUS_AGENT`, `BUS_TOKEN_FILE`,
  `swarmctl` on `PATH`) and strip control characters from pasted text; long messages are cut with a visible marker.
- A bus-notice `done` is judged on the current finish's push evidence: a branch pushed by an earlier claim no longer lets a new unpushed head land. Worker-token fetch of an orchestrator's bus token needs `?worker=NAME` (shared worker token: runners are trusted fleet-wide, see docs/agent-bus.md).
- **Stored means accepted** (agent-control PR 61): once a bus message is stored, later dispatch or bookkeeping errors are logged (category plus redacted text) and the send still succeeds, so a finish notice never falls back to the normal path after storage. The tmux enqueue is idempotent and repaired by the duplicate path and the delivery loop; the loop also starts unreachable episodes it missed.
- A plain HTML page at `/bus` (admin token only; no loopback exception, because a reverse proxy arrives from loopback) lists agents and the last 100 messages.
- New routes `/bus/*` and a bus agent token column in the permission matrix (docs/backend.md). Schema version 8.
- See docs/updating-the-kit.md, "Upgrading to 0.8.0" (also adds the missing "Upgrading to 0.7.1" note).

## 0.7.1 (2026-11-20)

Fixes from the fresh-server install test of 0.7.0 on DigitalOcean Ubuntu 24.04 (install test 6).

- **An orchestrator at Claude's sign-in screen reported `running` and had messages typed into it.** The
  supervisor now reads the screen. Claude Code's first-run screens (theme picker, security notes, folder
  trust) get Enter, at most 3 times each. A sign-in screen, an expired-token message or the bypass-permissions
  warning that stays up for 10 seconds ends the session: new state `needs-login`, `last_exit` says
  "run sudo swarmctl login claude on worker W, then swarmctl orchestrator start NAME", the owner bot is told once,
  `swarmctl status` warns, and queued messages wait in the backend. Nothing is typed into those screens, not
  even the stop wrap-up. It does not restart (also not after a supervisor restart) until an admin starts it.
- Before each start the supervisor checks that Claude has a sign-in for the agent user (new
  `agent-exec orch-check`: a token, key or proxy in `agent.env`, an `apiKeyHelper`, or a saved `/login`) and
  refuses with the same `needs-login` message when there is none. On a Mac with no token it cannot see the
  Keychain, so it starts and relies on the screen check.
- OAuth and device sign-in links are hidden in the uploaded terminal tail, by the supervisor and the backend.
- `orchestrator delete` is refused while the session is still running or stopping ("wait until status shows
  stopped"). A supervisor that is stopped while its record is deleted or should be stopped ends the session
  instead of leaving it, and logs the `systemctl disable --now swarm-orchestrator@NAME` line to run.
  docs/orchestrators.md says to disable the unit after a delete.
- **First boot never finished after a reboot during the Tailscale wait.** `firstboot.sh` now enables
  `swarm-firstboot-resume.service` while it runs. After a reboot during the wait it asks Tailscale for a fresh
  login link, waits again and locks down; after a reboot in any other step it writes `stopped` with the command
  to run. The status file always ends with `finished` or `stopped`, and the unit removes itself.
- Less journal noise: the sudoers rule no longer logs each `agent-exec` call or opens a PAM session for it
  (three journal lines per call, every 5 seconds per orchestrator), and the supervisor polls every 10 seconds.
  A sudo too old for `log_allowed` keeps the plain rule. On a reboot the supervisor says the machine is
  shutting down instead of "the session is left running".
- Setup's "Next steps" and the smoke test now show where to send problems and ideas:
  gilhildebrand@mail.grokbot.com.
- **Tested and not.** Tests run a stand-in Claude that shows the theme picker, the security notes and the
  sign-in menu, a run with no sign-in, the delete guard, a supervisor stopped mid-stop, and the firstboot resume
  with stand-ins. **Not run:** a real Claude Code session at these screens, the new sudoers line under a real
  `visudo`, and a real reboot.

## 0.7.0 (2026-11-20)

- Add orchestrators: one Claude Code session that stays up for days in tmux on one pinned worker, for the rare
  product-level agent that cannot be a chain. A chain stays the default ([docs/orchestrators.md](docs/orchestrators.md)).
- Each orchestrator has its own supervisor (`swarm-orchestrator@NAME` systemd unit, or a launchd agent on a Mac
  with `setup-macos.sh --orchestrator NAME`), outside the runner: no task slot, and runner restarts, updates and
  cleanup leave the session alone. A supervisor restart adopts the running session.
- Heartbeat every 30 seconds with state, pid, tmux session, restart count, last exit and a secret-scanned
  terminal tail. No heartbeat for 10 minutes: `unreachable`, and the owner bot is told.
- Crash restarts with backoff (5 seconds doubling to 5 minutes, reset after 10 healthy minutes), resuming the
  saved Claude session or starting fresh from `CHECKPOINT.md`. 5 crashes in 30 minutes: `crash-loop`, the owner
  bot is told, and an admin restarts it. Every start, stop, crash and restart is an event.
- `swarmctl orchestrator list|status|create|set|start|stop|restart|delete|logs|message` and the matching API.
  Graceful stop sends a wrap-up message first. Messages are typed in one at a time when the agent is idle.
- Secret bundles by name only, loaded through `with-secrets`; a brief or message that looks like a secret is
  refused. Optional Agent Relay registration under a fixed name (`SWARM_RELAY_REGISTER_CMD`); its output is
  never kept. An orchestrator can only be pinned to a worker whose project allowlist takes its project.
- **Tested and not.** Tests run the real backend, supervisor, agent-exec and tmux with stand-ins for Claude Code,
  `with-secrets` and Relay. **Not run:** a real Claude session, real Relay, the systemd unit and user scope on a
  real server, and launchd on a real Mac.

## 0.6.1 (2026-11-20)

Fixes from the fresh-server install test of 0.6.0 on DigitalOcean Ubuntu 24.04 (install test 5).

- **First boot stopped at the Claude and Codex installs.** Stock DigitalOcean Ubuntu 24.04 images mark root's
  password "must be changed", so every `sudo -u agent` failed with "Account or password is expired".
  `setup-worker.sh` now clears that flag (`chage -d TODAY root`) before anything runs as `agent`. With `--yes`
  it does so on its own; by hand it asks first. It sets no password and does not touch SSH settings.
- **SOPS never installed.** `setup-secrets.sh --prepare` rewrote the published checksum line with three
  spaces, so `sha256sum -c` looked for a file named " sops" and refused every download. The line is now
  rewritten correctly, and a download that does not match is still refused.
- `config/swarm.env.example` shows `SWARM_WORKER_NAME` and `SWARM_WORKER_PROJECTS`, commented out, with a
  pointer to `docs/multi-worker.md`. Existing installs keep their settings and their hostname as the name.
- `render-cloud-init.py` no longer defaults to a release tag that may not exist (v0.6.0 was never tagged, so
  first boot got a 404). It uses the tag only when the checkout has it, else the checkout's commit, else the
  main branch, and says which on screen. `--kit-url` still overrides it.

## 0.6.0 (2026-11-20)

- Add a short per-project activity feed with SQLite storage, `swarmctl activity-log`, and an API. Kinds:
  activity, shipped, blocked, decision. A decision names its decider: owner, delegated or bot. Same key
  from the same bot within 24 hours is one entry. `cross-project` holds swarm-wide entries.
- Worker tasks that opt in can log activity, shipped or blocked, never a decision.
- Export changed months and lasting decisions nightly to a separate docs-only memory repo. Preserve manual notes.
  The backend keeps the last published position per project; it moves only with `activity-log published`.
- Let the Chief of Staff answer small, reversible questions. Show those calls once in the morning note and let the owner overrule them.
- Check for missed questions at each Chief of Staff heartbeat. Keep routines quiet when nothing changed.

## 0.5.0 (2026-11-20)

Setup with nobody typing: the bot builds the server, and every sign-in is "open this link".

- **Bot-built server.** `scripts/render-cloud-init.py` fills in `scripts/cloud-init.yaml` (user data for DigitalOcean, and other providers
  that take cloud-init). At first boot `scripts/firstboot.sh` runs `setup-worker.sh --yes --admin-user`, joins Tailscale (a one-use key, or a
  saved login link), prepares encrypted secrets (`scripts/setup-secrets.sh`: SOPS, age, a worker age key and a read-only deploy key, public halves
  only), starts the worker, runs the smoke test, then closes public SSH once Tailscale is connected (or leaves that to the bot with
  `--lockdown bot`). `setup-worker.sh` gains `--yes`. New: [docs/cloud-init.md](docs/cloud-init.md). `SETUP-WITH-GROKBOT.md` is rewritten so the bot does the work and the owner only approves things in a browser. The manual path stays in `docs/worker-setup.md`.
- **Browser-only sign-ins.** `sudo swarmctl login codex|claude|github` starts the sign-in in the background and prints only the link and code.
  Codex uses `codex login --device-auth` (device-code login must be on in ChatGPT Security Settings). Claude uses `claude setup-token` and saves the
  one-year token as `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/agent-swarm/agent.env`, never printed (`--bare` ignores it). GitHub uses `gh auth login --web`,
  then `gh auth setup-git`, for the runner user. `/etc/agent-swarm/agent.env` is now mode 640, root and group `agent` (was 644).
- **macOS worker (optional).** `scripts/setup-macos.sh`: one user, Homebrew, launchd agents for the runner, a secrets sync every 10 minutes, daily cleanup
  and optionally the backend. No `sudo`. Less isolated than Ubuntu: no separate users, no per-job limits. The runner reads `vm_stat` for free memory and
  tasks get Homebrew on their PATH (Linux behavior is unchanged). New: [docs/macos-worker.md](docs/macos-worker.md); `docs/multi-worker.md` has a Mac example.
- **CLIProxyAPI panel (optional, advanced).** `scripts/setup-cliproxy.sh` installs the proxy on `127.0.0.1` with the management key from a file, and a small gate
  that lets only the panel and its API through, for `tailscale serve`. Add several Codex and Claude accounts in the browser, with the paste-the-callback step.
  Never restart the proxy while tasks run: `swarmctl restart-when-idle --also cliproxyapi`.
- **Tested and not.** Tests use stand-ins for the provider, Tailscale, systemd and launchd. Run for real: the sign-in helper's background flow with a
  pseudo-terminal, a single-user backend, runner and script task through the Mac launchd wrapper, the real CLIProxyAPI 8.0.79 with the panel in a browser
  (through the gate). **Not run:** creating a real server, the real first boot, joining Tailscale, automatic lockdown, `tailscale serve`, a real Mac, and a full
  sign-in with a real Codex, Claude or GitHub account.

## 0.4.0 (2026-11-20)

- Set up SOPS and age with every worker. Keep separate runtime and admin bundles, back up a recovery key,
  and sync the selected runtime bundles at worker startup. `with-secrets` loads them for one command.
- Optional: name extra workers, pin tasks, or give each worker a project allowlist. Expired claims cannot
  finish or upload results after another worker takes over.
- Escalate an unresolved failed task once. Dismiss it or queue a follow-up to keep it from escalating.
- Use Codex turn events to tell a failed run from an item-level warning. Tell the agent when a follow-up
  or retry starts a new model session instead of resuming one.

## 0.3.1 (2026-11-18)

Fix release from the fresh-server install test of 0.3.0. **0.3.0 passed a clean install on a fresh
Ubuntu 24.04 server on 2026-11-18**: setup, smoke test, admin user, firewall and SSH hardening, lockdown
refusal checks, an upgrade by running setup again, the project log, time limits and chains (with a
stand-in for the model), and the task and cleanup flow. That run found five small defects, none blocking.
0.3.1 fixes them. Still not tested on a real server: joining Tailscale (real lockdown, Serve, Tailscale
SSH), real Claude and Codex logins and model tasks, GitHub login and repo pushes, and bot notify delivery.
Upgrade steps: docs/updating-the-kit.md ("Upgrading from 0.3.0 to 0.3.1").

Fixed:
- `--max-cycles 0` was quietly turned into 20 (both `swarmctl` and the backend treated 0 as "not given").
  It is now refused with a clear error: a chain always has a real limit, and the agent ends it early by
  leaving no checkpoint.
- Expired throwaway log items still showed in `swarmctl log list` and the Markdown export until the daily
  cleanup. They now disappear from every view the moment they expire.
- `--chain` with `--command` was accepted but could never chain, since a script writes no checkpoint. It
  is now refused with a clear error.
- A task cancelled while running left its handoff folder (outbox) for 7 days. The runner now removes it
  at once whenever the backend has ended the claim, since those results can never be saved.
- Three setup tests failed when run by a normal user other than `agent`. They now pass for any user.
- The root-only setup test (`tests/setup_root_check.sh`, used in Docker or with
  `SWARM_TEST_DESTRUCTIVE_OK=1`) now puts everything back when it ends, pass or fail: the stand-ins for
  ufw, sshd, systemctl, tailscale and apt-get, root's SSH keys, sshd_config and its test user. On a box
  that already has a usable admin it changes nothing and the test is skipped with the reason, instead of
  failing.

Changed:
- Docs and the setup script header now say 0.3.0 passed a fresh-server install, and list what is still
  untested.
- docs/project-log.md says the first weekly export on a new install comes the following week, and how to
  export right away.
- Backend, worker, `agent-exec` and `swarmctl` report version 0.3.1.
- README has a new "What you need" section near the top, linked from a new contents line: a table of
  every tool and service the kit uses (Essential or Nice to have, and why), and "Where to run the
  worker" comparing cloud servers (with prices checked 2026-11-18, plus boat, and Google Cloud and AWS EC2
  as advanced options) and a computer at home. It says
  plainly that macOS is not supported. docs/worker-setup.md points to it.

## 0.3.0 (2026-11-18)

Easier to start and easier to keep going. The README is friendlier, your Grok Bot can now set the whole
kit up for you, bots get weekly goals and a daily check, and every project keeps a log. The backend
changes (the project log and long-running jobs) are covered by automated tests; 0.3.0 has not had its own fresh-server install
run yet (0.2.1 has, see docs/worker-setup.md "Tested on").
Upgrade steps: docs/updating-the-kit.md ("Upgrading from 0.2.1 to 0.3.0").

Added:
- `SETUP-WITH-GROKBOT.md`: tell your Grok Bot "set me up with this repo" and it walks you through
  everything, one question at a time. Every choice (worker or not, where and how big, model logins,
  backend, project bots, connectors, treg, Executor, approvals, goals meeting time) is explained in plain
  words with a recommended default. It never asks for secrets in chat and confirms anything that costs
  money.
- Project log: one append-only, typed history per project in the backend (new `log_entries` table,
  created automatically on upgrade). Docs: docs/project-log.md, with a marketing example.
  - Records: items (deliverables with status draft, needs-review, approved, changes or rejected, and a
    version), decisions (`by`, answer, source, date), feedback (`by` required), learnings (pending or
    standing), rule changes, activity and results. Items, learnings and rules have a stable `ref`; a
    change is a new entry, never an edit.
  - A new version of an item voids older verdicts; a verdict on an old version is refused.
  - Feedback becomes a pending learning automatically. Rule changes are proposals that store the text
    before and after, and only the owner (admin or Chief of Staff) approves them. Each project has one
    short rules doc (at most 25 rules, `SWARM_RULES_CAP`) with a changelog.
  - Bots read the standing rules plus a 14-day feedback digest before drafting: `swarmctl log brief`,
    `GET /log/brief`.
  - Bad records are refused: unknown kind, status or bot, missing `by`, unknown `ref`.
  - Throwaway items take `expires_days`; once expired they leave every view and are deleted.
  - `swarmctl log add|list|export|items|rules|brief`; `POST /log`, `GET /log`, `/log/items`,
    `/log/rules`, `/log/brief`, `/log/export`. Plain Markdown and JSON, readable by agents with their
    own token.
  - Weekly Markdown export per project to `SWARM_LOG_EXPORT_DIR`, and an optional local git repo per
    project (`swarmctl project set NAME --log-git`) so changes show as diffs.
  - Automatic entries: finished and failed tasks add a `result`; answering or dropping a Waiting on you
    item, or answering a task's question for the human, adds a `decision`. Repeated requests never log
    twice. Project bots read and write only projects they are bound to; a running task writes only to
    its own project; the worker token has no access.
- Waiting on you shows counts and age: open, snoozed, oldest in days, and how many have been open 14
  days or more. The weekly export lists those old items under "Consider dropping these", and the goals
  meeting prompt asks the Chief of Staff to suggest drops.
- Long-running jobs (docs/long-running-jobs.md):
  - Chains, the recommended way: `swarmctl enqueue --chain --max-cycles N`. Each cycle is a short, fresh
    run that writes `CHECKPOINT.md`; the backend queues the next cycle as a follow-up with the checkpoint
    attached. One project log entry per cycle; the owner hears only when the chain ends. Cancelling any
    cycle cancels the whole chain.
  - Per-task time limit: `--timeout-minutes`, up to the admin maximum `SWARM_TASK_TIMEOUT_MAX`
    (default one day), and a `long-running` task kind that uses that maximum and keeps a checkpoint in
    its folder. The task token and the watchdog follow the task's own limit.
- Weekly goals meeting (Chief of Staff) and daily work heartbeat (each project bot): prompts 6 to 8 in
  templates/routine-prompts.md, a goals file template (templates/goals.md), and
  docs/goals-and-heartbeat.md, which also explains how this differs from the worker's 30 second machine
  heartbeat.
- docs/which-tool-when.md: Grok Bot connectors, treg (paid data, plus private tools like Search Console
  and Google Ads), the worker (all coding of any size, long jobs, design, deep research, bulk scraping,
  specific models), Executor (later), with examples and when a connector beats treg for the same
  account.
- Four illustrated pictures in docs/images/ for the README.

Changed:
- README: two-sentence opening, the four pictures, "Set it up with your Grok Bot" as the main path, a
  short "which tool when" table, and manual setup as the second path.
- All Mermaid flow charts moved out of the README into docs/how-it-works-technical.md, plus a new chart
  for the project log and the weekly loop.
- Chief of Staff and project bot templates: goals meeting, work heartbeat, project log and the feedback
  loop (bots read recent feedback and learnings before drafting), setup choices, and "all coding goes to
  the worker". The model routing template says the same.
- Docs say plainly that the worker's 30 second machine heartbeat is a network call with no model, so it
  uses no tokens.
- README "which tool when" has a row for chains, linking docs/long-running-jobs.md.
- Logging feedback or a decision through the API now needs `by`. Routine prompt 8 shows the new call.
- Backend, worker, `agent-exec` and `swarmctl` report version 0.3.0.

## 0.2.1 (2026-11-18)

Fix release from the first install test of 0.2.0 on a fresh Ubuntu 24.04 server. That test found that
no task could finish on a new install, that Claude Code did not install, and that the lockdown step could
lock out a server that has only root. 0.2.1 fixes these. **0.2.1 passed a clean install on a fresh Ubuntu
24.04 server on 2026-11-18** (setup, smoke test, admin user, firewall and SSH hardening, lockdown refusal
checks, task and cleanup flow). Still not tested on a real server: joining Tailscale (so the real lockdown,
Tailscale Serve and Tailscale SSH), Claude and Codex login, running a model task, GitHub login and repo
pushes, and bot notify delivery.
Upgrade steps: docs/updating-the-kit.md ("Upgrading from 0.2.0 to 0.2.1").

Why 0.2.1 and not a changed 0.2.0: 0.2.0 was already installed on a test server, and a version that
changes behavior needs its own number so `swarmctl status` and `/health` show which code is running.

Fixed (numbers are the install-test defects):

1. Claude Code now installs. The setup script ran `sudo -iu agent bash -c '...'`, and `sudo -i` escapes the
   command line, so `$f` and `$?` were lost and curl failed. Agent commands now run as
   `sudo -u agent -H bash -lc '...'`. A failed install is reported as "Claude Code did not install" with
   exit code 3, instead of the misleading "claude does not run as agent". The smoke test had the same
   pattern and is fixed too.
2. and 3. Tasks no longer fail while being prepared. The runner and `agent-exec` used `chmod` on new task
   folders in the shared inbox and outbox. Linux clears the setgid bit (the bit that makes new files take
   the folder's group) when someone outside the folder's group runs chmod, so files got the wrong group and
   the other user could not read them. The task folders are no longer chmodded, and both sides now check
   the group and setgid bit after making a folder.
4. `--lockdown` refuses unless a usable non-root admin exists: a normal user with a login shell, sudo
   (group `sudo` or a sudo rule), a password or a passwordless sudo rule, and a non-empty
   `authorized_keys` only that user can change. `--admin-user NAME` makes one: adds it to `sudo`, copies
   root's plain key lines, and asks for a sudo password when run in a terminal. The lockdown message and
   the Tailscale SSH sample now name that user. Lockdown also refuses, with a hint, when Tailscale is not
   installed or not joined.
5. Install-time hardening: the firewall (ufw) is turned on at install, allowing SSH from anywhere (so you
   cannot be locked out) plus SSH over Tailscale and Tailscale's port. Root and password login over SSH are
   turned off at install only when an admin user is confirmed (the script was run through sudo as that
   user, or you answered "yes" after testing `ssh` and `sudo -v` as that user). The end of the run says
   what is and is not hardened. `--harden` runs just this step later.
6. `swarmctl status` no longer warns "newest database backup is older than 36 hours" on a new install. The
   backend records when its database was made and does not warn about a missing backup in the first 36
   hours, and setup makes a first backup. A missing backup after that gives "There is no database backup
   yet".
7. apt waits for other package installs instead of failing at once. Before each apt call the script waits,
   up to 10 minutes with a progress line every 30 seconds, until the server's first-boot setup (cloud-init,
   checked with `cloud-init status --wait` and a time limit) has finished and no process holds the apt or
   dpkg locks (`/var/lib/apt/lists/lock`, `/var/lib/dpkg/lock-frontend`, `/var/lib/dpkg/lock`,
   `/var/cache/apt/archives/lock`, checked with fuser, lsof or /proc). If apt still fails on a lock it runs
   again, up to 4 times, 20 seconds apart, and then stops with a clear message. `apt-get update` also
   retries network errors. The dpkg lock wait inside apt (`DPkg::Lock::Timeout=120`, also written to
   `/etc/apt/apt.conf.d/90-agent-swarm-lock-wait`) still covers the Node.js and Tailscale installers. The
   first version of this fix only set `DPkg::Lock::Timeout`, which does not cover the lock that
   `apt-get update` takes, so the 0.2.1 retest failed at once on a new DigitalOcean server while
   cloud-init was installing droplet-agent.
8. Quieter output: apt, Node.js, Tailscale, Claude Code and Codex installer output goes to
   `/var/log/agent-swarm-setup.log` (mode 600). The screen keeps the `==>` lines and shows the last log
   lines if a step fails. The "apt does not have a stable CLI interface" warning now goes only to the log.
9. Each run removes old release folders, keeping only `current` and `previous`.
10. `--tailscale later` installs Tailscale without joining, so nothing waits for a browser; `--lockdown`
    works once you join with `sudo tailscale up --ssh`. The default join now gives up after 15 minutes
    instead of waiting forever, and a run with no terminal and no auth key joins later on its own.
    `--no-tailscale` (also `--tailscale off`) still works and the docs say what it costs.

Also (from the 0.2.1 retest):

- Outbox task folders no longer pile up. `agent-exec` has a new narrow action, `cleanup TASK`: it removes
  only `/srv/agent-swarm/outbox/TASK`, checks the task id, refuses links and any path outside the outbox,
  and runs as the agent through the same single sudo rule (the rule still allows only `agent-exec`). The
  runner calls it only after the results were uploaded and the backend acknowledged the finish; otherwise
  the folder stays. The runner removes its own inbox folder as before. The daily cleanup now also backs up
  and removes outbox folders of tasks that finished more than `SWARM_KEEP_DAYS` (7) days ago.
- `agent-exec` task and folder names must match in full: a name with a trailing newline was accepted
  before.

Also:

- The smoke test shows "public SSH is still open" as a NOTE, not a WARN, so a new box ends with
  0 failed and 2 warnings (GitHub login, Tailscale).
- Docs: honest "Tested on" notes, how to get the kit onto a server without your own GitHub login (release
  file, `git archive` plus `scp`, or a read-only deploy key or token), a "Make your admin user" step, the
  Tailscale pause and how to skip it, the expected `swarmctl status` warning and smoke-test output on a new
  box, why `sudo -iu agent` is needed for the model CLIs, and a "Setup problems" section in
  troubleshooting.
- Tests: 78 before, 124 now. New: the inbox and outbox handoff run as two real OS users in a throwaway
  container (catches defects 2 and 3), the setup script's lockdown refusal on a root-only box,
  `--admin-user`, `--harden`, release pruning, the Tailscale modes, the Claude Code install path, the
  apt lock option, and the backup warning. Tests that need root or two users run in Docker and skip
  with a loud message when Docker is not there (`SWARM_REQUIRE_USER_TESTS=1` makes that a failure).

## 0.2.0 (2026-11-18)

Security and reliability release, from two reviews of 0.1.0. **Not yet run end to end on a fresh server.**
The setup script passes shellcheck and its parts are tested one by one; run it on a server you can rebuild,
then run `scripts/smoke-test.sh`. Upgrade steps: docs/updating-the-kit.md ("Upgrading from 0.1.0 to 0.2.0").

What changed:

- Each task now gets its own task token when it is claimed. It works only for that task and that run, and
  stops when the task stops running, when its lease runs out, or after 8 hours. The worker token can only
  take tasks, read task states and send alerts. It can no longer read any task, report or file.
- Every run has a claim id. A runner that lost its task (cancelled, answered, given to another run) gets
  HTTP 409 and can never finish or publish it. Repeating a finish gives back the first answer. Cancel is
  immediate, and the runner stops the agent and publishes nothing.
- Three users on the worker: `swarm` (backend), `swarm-runner` (runner, worker token, GitHub login) and
  `agent` (model CLIs, no tokens, no GitHub login). Code moves between them as git bundles. The runner
  checks new lines for secrets and pushes only to the task's own branch.
- Bots see only their own things: duplicate checks, Waiting on you items, projects and follow-ups are all
  per bot. Repo tasks need a project that lists the bot (`swarmctl project set NAME --repo ... --bot ...`).
- A route permission table lives in the code, is printed by `swarm_backend.py --permissions`, and is in
  docs/backend.md. A test keeps them the same.
- Bot trigger keys move out of the database into one file per bot (`/etc/agent-swarm/bot-keys/`). The
  first start moves old keys and wipes them from the database. Changing a bot's URL or key never sends the
  new key to the old URL.
- Tailscale Serve, with Tailscale on the bot's computer, is now the recommended way for bots to reach the
  backend, with a sample access rule set. Funnel is option 2, with warnings.
- The backend checks the token before reading a request, rejects bad lengths, has timeouts, a connection
  cap and a limit on failed logins. Notify URLs must be public https addresses (checked again at every
  delivery) and redirects are not followed.
- Delivery is described honestly as "at least once". A receiver answering 409 or `{"duplicate": true}`
  counts as delivered. Notices carry `event` (what happened) and `reason` (why this bot got it).
- `swarmctl enqueue --follow-up TASK_ID` continues a finished task, including unpushed work saved as a
  bundle. Follow-ups of one parent run one at a time (`held_behind`).
- A task whose saved folder is gone starts fresh with the full brief and every answer, and says so. If
  unpushed work was lost, it asks first.
- Waiting tasks get one reminder after 12 hours and go on Waiting on you after 24. A watchdog alerts when
  no runner checks in or a task runs far past its time limit. Old files are deleted after 90 days, and each
  bot can have at most 100 open tasks.
- Review tasks: if findings lack a `path:line` reference, the worker asks once for the format to be fixed.
- Script tasks have the same time limit as model runs. A failed or timed-out agent is never marked done.
- Pushing to the main branch is off unless a project allows it (`--allow-push-main`) and the task asks for
  it (`--push-main`). Default and common protected branch names are refused otherwise.
- `with-secrets NAME -- command` gives one command a shared key, with clear refusals and no printing.
- Cleanup saves untracked and binary files before deleting a folder, skips repos a live git worktree points
  into, and deletes as the `agent` user only after checking the saved copy.
- `swarmctl drain`, `undrain` and `restart-when-idle` restart the worker without killing tasks. Upgrades
  drain and wait first, install into a new release folder, and roll back if the backend does not start.
- Setup: `--auth-key FILE` and `--tags`, checks the `agent` account has no sudo, SSH hardening that wins
  over cloud defaults and is verified, limits for all tasks together, honest exit code 3 when something is
  left to do. New `scripts/smoke-test.sh` and `scripts/restore-backup.sh` (checks the backup before
  touching anything). Backups are checked, timestamped and private.
- `swarmctl` works through the installed link, `project set|list|show` (the old form still works), token
  files are written with mode 600 and never through a link, and `enqueue` warns about local file paths in a
  brief.
- `check-secrets.sh` prints the file, line and pattern number, never the secret, and a scan error is exit 2.
- README rewritten for non-technical readers, with product-language charts. Technical charts moved to
  docs/how-it-works-technical.md.
- Tests: 13 before, now a full suite covering tokens, permissions, delivery, the runner with a real git
  repo, and the tools.

Open items (not fixed in this release):

- All tasks share the `agent` user: the separation keeps tasks out of each other's backend data, but not
  out of each other's files or the shared CLI logins. Per-task users or containers are a bigger change.
- Outgoing network access from tasks is not limited. It needs a firewall design that does not break the
  model CLIs.
- The backend reads its admin and worker tokens from files at start; root can read them. Expected on a
  single-owner box.
- Development mode (`SWARM_AGENT_USER` empty) runs the runner and agent as one user. Used by the tests only.
- The setup script, systemd units and Tailscale steps are not verified on a real fresh server yet.
- Cloudflare Workers backend: design only, not built.

## 0.1.0 (2026-11-18)

First version.

- On-worker backend (`server/swarm_backend.py`): tasks, owners, questions and answers, messages to running
  tasks, reports and artifacts, bot registry with per-bot tokens, Waiting on you list, status command,
  direct results routing to the owning bot with Chief of Staff fallback, idempotent delivery.
  Python standard library and SQLite only.
- Worker (`server/swarm_worker.py`, `server/run-agent`): runs Claude Code or Codex on your own subscriptions,
  resumes the same session after an answer, falls back to the other provider on usage limits, script tasks
  with no model, pushes `agent/<task-id>` and opens draft pull requests when asked.
- Command line (`server/swarmctl`).
- Daily cleanup of finished task folders, nightly database backup, hourly disk alert, systemd units.
- `scripts/setup-worker.sh` for a fresh Ubuntu LTS server, with a separate lockdown step.
- Docs for both paths (Grok Bot only, and Grok Bot plus worker) and templates for bots and routines.
