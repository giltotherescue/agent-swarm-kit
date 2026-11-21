# Orchestrators: one agent session that stays up for days

Most long jobs should be a **chain** of short cycles ([long-running-jobs.md](long-running-jobs.md)). A chain
is cheaper when something goes wrong, starts each cycle with fresh context, and lets everything else in the
kit work as usual. Use a chain first.

An **orchestrator** is for the rare product-level agent that must keep one session going for days, for
example an agent that keeps building, testing and steering one product and that other bots talk to by name.
It is one Claude Code session in a tmux session on **one pinned worker**, with no time limit. A small
supervisor keeps it running, restarts it after a crash, reports a heartbeat, and stops it gracefully when
you ask.

| | Chain (default) | Orchestrator |
| --- | --- | --- |
| Session | Fresh each cycle; a checkpoint carries state | One session for days, resumed after a crash |
| Time limit | Each cycle has one | None |
| Uses a task slot | Yes, one per running cycle | No. It runs beside the runner, not inside it |
| Worker updates | Happen between cycles | Never stop it; its supervisor adopts the running session |
| Publishing | The runner pushes and opens PRs, after the secret scan | The agent has no push access of its own (see [What it can do](#what-it-can-do)) |
| Messages | `swarmctl message` into the next cycle's messages file | Typed into the session when the agent is idle, by the [Agent Bus](agent-bus.md), or by Agent Relay |
| Cost of a stuck agent | One short cycle | Until someone notices (heartbeat and crash-loop notices help) |

## How it works

```
swarmctl orchestrator ...  ──>  backend (orchestrators table, events, one pending message)
                                   ▲  heartbeat every 30 s: state, pid, tmux session, restarts, last exit, terminal tail
                                   │  reply: desired state, settings, one message
                       swarm-orchestrator@NAME (supervisor, user swarm-runner, its own systemd unit)
                                   │  agent-exec orch-*  (the same single sudoers rule the runner uses)
                                   ▼
                   tmux socket and session orch-NAME, user agent, scope swarm-orch-NAME-*.scope
                   folder /home/agent/work/orchestrators/NAME  ->  claude (interactive, bypass permissions)
```

- **Separate from the runner.** The supervisor is its own systemd unit, `swarm-orchestrator@NAME`, not a child
  of `swarm-worker`. It takes no task slot and does not count toward `SWARM_MAX_PARALLEL`. Restarting or
  updating the runner (including `swarmctl restart-when-idle` and the stop-all a runner does at start) only
  stops `swarm-task-*` scopes, never `swarm-orch-*`. The daily cleanup skips the `orchestrators` folder.
- **The session survives its supervisor.** tmux runs as the agent user in its own systemd user scope. The unit
  uses `KillMode=process`, so a supervisor restart (or a kit upgrade, which restarts supervisors) leaves the
  session running, and the new supervisor adopts it ("adopt" event).
- **One pinned worker.** The record names the worker (`worker_id`). The backend only answers heartbeats from
  that worker's `SWARM_WORKER_NAME`, so a supervisor anywhere else starts nothing. A file lock stops two
  supervisors for one name on one machine, and the backend refuses a second supervisor for 90 seconds after
  the first one last reported.
- **Restarts.** If Claude exits or the tmux session ends while it should run, the supervisor restarts it after
  5 seconds, then 10, 20 and so on up to 5 minutes. After 10 healthy minutes the pause is back to 5 seconds.
  It resumes the saved session (`claude --resume SESSION_ID`). If there is no session to resume, or a resumed
  one dies within a minute, it starts fresh with a first prompt that says to read `CHECKPOINT.md` and
  `HANDOVER.md`. **After 5 crashes in 30 minutes it stops restarting**, reports `crash-loop`, and the owner bot
  is told. An admin restarts it with `swarmctl orchestrator restart NAME`.
- **Heartbeat.** Every 30 seconds the supervisor reports to the backend. This runs no model and uses no
  tokens. If nothing arrives for 10 minutes while it should run, the backend marks it `unreachable` and
  notifies the owner bot once (the Chief of Staff when there is no owner bot).
- **Sign-in and first-run screens (0.7.1).** Before a start the supervisor checks that Claude has a sign-in
  for the agent user (`CLAUDE_CODE_OAUTH_TOKEN` or another key or proxy in `agent.env`, an `apiKeyHelper`, or a
  saved `/login`). Without one it does not start Claude. While the session runs it reads the bottom of the
  screen. Claude Code's first-run screens (the theme picker, the security notes, the folder trust question)
  get Enter, which accepts the default, at most 3 times each ("first-run" event), and only when the whole dialog is
  the bottom of the pane (its key hint the last line, no input box on screen: text in a draft or in history is never
  answered). The folder trust screen, in both wordings, is answered from its selection: with the `❯` on "No, exit" it
  gets the arrow key towards Yes (Down for the current "Quick safety check", Up for the older "Do you trust the files
  in this folder"), and Enter only once the one `❯` is on the Yes option (0.8.1). A sign-in screen
  ("Select login method", "Paste code here", "Please run /login", an expired token) or the bypass-permissions
  warning that stays up for 10 seconds ends the session at once. Nothing is ever typed into those screens:
  queued messages and the stop wrap-up wait. The state becomes `needs-login`, `last_exit` says what to run, and
  the owner bot is told once. It does not restart, even after a supervisor restart, until you sign in
  (`sudo swarmctl login claude` on the worker) and run `swarmctl orchestrator start NAME`.
- **Messages during a long busy turn (0.8.1).** Queued messages are typed in at once when the pane is idle at an
  empty prompt. If the session stays busy, the oldest message is pasted once it is 90 seconds old (15 when the first
  line of the message says `URGENT`, which the bus puts on urgent messages), counted from when the backend queued it;
  Claude Code queues it for the agent's next step. The rest of a continuous backlog follows 5 seconds apart. The guard
  fails closed, like agent-control PR 67: right before every paste and every Enter, idle or busy, and before the stop
  wrap-up, the supervisor captures the screen again, and only a confirmed empty input box (read from the bottom: 1 to 8
  rows made only of whole status items Claude Code draws there, then rule, prompt line, identical rule, with no other
  prompt right under a rule higher up) takes it. `agent-exec` holds the one pane parser and the supervisor loads it, so
  both read a capture the same way. Enter after a paste needs exactly the pasted text in the box (whitespace runs
  compared as one space). Claude Code's collapsed `[Pasted text #N +K lines]` marker cannot be tied to the message, so
  it never gets Enter: the message is reported as `reason=not_verifiable` and waits for you (look at the session; press
  Enter yourself if the paste is the message, then release it as below). A choice menu or permission prompt, typed text
  (a draft, including a multiline one or one holding a pasted rule), an unrecognized footer, or a screen it cannot read
  holds the message; a wrap-up it cannot send safely is skipped, and the session ends at the stop deadline. Only the
  verified hint `Press up to edit queued messages` counts as empty; `Try "..."` is treated as typed text. A message held
  for 2 minutes is reported once per reason as a `delivery-deferred` event (`reason=unknown_footer|draft|menu|unreadable`,
  or `paste_failed`/`submit_failed`/`submit_unknown`/`not_verifiable`; never pane contents); it is never pasted because of a timeout. Each busy paste is recorded once as a
  `message-delivered-busy` event. A message is never pasted twice: if Enter failed only Enter
  is retried, and only while the pasted text is still in the box; if only the receipt failed only the receipt is
  retried. When the outcome of a paste is unknown and the box is empty again, the message is left unacknowledged and
  reported once as `delivery-unknown`. These states are kept in the supervisor's state file, so a restarted supervisor
  does not paste it again either. Check the session, then run `swarmctl orchestrator release NAME ID` if the message
  arrived (marked delivered, nothing typed) or `swarmctl orchestrator release NAME ID --resend` to have it pasted once
  more. The queue behind a held message waits until then. The only exception to the box rule is the kit's tests: with
  `SWARM_ORCH_PANE_ADAPTER=plain` a stand-in program with no box parts at all gets idle messages and the wrap-up.
- **Checkpoint.** The first prompt and the standing rules tell the agent to keep `CHECKPOINT.md` in its folder
  current, so even a crash with no session to resume loses little.
- **Events.** Every start, stop, crash, restart, crash loop, adoption, Relay registration, message delivery and
  setting change is kept as an event, shown by `swarmctl orchestrator status NAME`.

States: `stopped`, `starting`, `running`, `restarting` (waiting out the pause), `stopping` (wrap-up),
`crash-loop`, `blocked` (this worker may not run it, for example its project is not in
`SWARM_WORKER_PROJECTS`), `needs-login` (Claude has no sign-in, or showed a sign-in or first-run screen it
could not get past; see above), and `unreachable` (set by the backend when the heartbeat stops).

## Set one up

1. **Check the basics.** The worker runs kit 0.7.0 or later and has checked in (`swarmctl status` lists it).
   The project exists and its bots are bound (`swarmctl project set app --repo you/app --bot app-bot`). If the
   worker has `SWARM_WORKER_PROJECTS`, the project must be in it. The agent user has a working Claude login
   (`swarmctl login claude`, or your proxy in `/etc/agent-swarm/agent.env`).
2. **Write the brief.** Standing instructions in a file: what the product is, what to work on, how to test,
   when to ask. No keys or passwords: the backend refuses a brief that looks like it holds one.
3. **Create it** (admin token):

   ```bash
   swarmctl orchestrator create app-steward --worker build-box -p app -r you/app --owner app-bot \
       --brief steward.md --secret-bundle app-runtime --relay-name app-steward
   ```

   `--secret-bundle` takes **names** of runtime bundles ([secrets.md](secrets.md)), never values. `--relay-name`
   is optional (see [Agent Relay](#agent-relay-optional)). `-m` picks a model; the project's default model is used otherwise.
4. **Install its supervisor once**, on that worker:

   ```bash
   sudo systemctl enable --now swarm-orchestrator@app-steward
   ```

   On a Mac worker: `bash scripts/setup-macos.sh --orchestrator app-steward` (a launchd agent;
   `--remove-orchestrator app-steward` removes it).
5. **Start it:** `swarmctl orchestrator start app-steward`. Within about 30 seconds,
   `swarmctl orchestrator status app-steward` shows `running`, the worker, the pid, the tmux session, uptime
   and the last heartbeat.

## Day to day

| Command | What it does |
| --- | --- |
| `swarmctl orchestrator list` | Every orchestrator: state, desired state, worker, uptime, restarts, last heartbeat |
| `swarmctl orchestrator status NAME` | Settings, pid, tmux session, session id, uptime, last exit and the last events |
| `swarmctl orchestrator logs NAME --lines 200` | The last lines of its terminal (up to 500), uploaded with each heartbeat |
| `swarmctl orchestrator message NAME "text"` | One message, typed into the session when the agent is idle |
| `swarmctl orchestrator release NAME ID [--resend]` | After a `delivery-unknown` event: the message arrived (marked delivered), or `--resend` to paste it once more (0.8.1) |
| `swarmctl orchestrator stop NAME` | Graceful stop: a wrap-up message, then the session ends when the agent goes quiet, or after 2 minutes |
| `swarmctl orchestrator start NAME` | Set it to run. Also clears a crash loop and `needs-login` |
| `swarmctl orchestrator restart NAME` | Graceful stop, then start. Also clears a crash loop and `needs-login` |
| `swarmctl orchestrator set NAME --brief new.md` | Change settings. They apply at the next start or restart |
| `swarmctl orchestrator delete NAME` | Remove a stopped one. Refused while it still runs or is `stopping`: wait for `stopped`. A deleted record never starts again. Then disable its unit (below) |

Creating, changing, starting, stopping, restarting and deleting need the **admin token**. The Chief of Staff
and the owner bot can read status and logs and send messages; another project bot sees nothing.
`swarmctl status` lists orchestrators and warns about any that are `unreachable`, `crash-loop`, `blocked` or
`needs-login`.

**Removing one for good.** `swarmctl orchestrator stop NAME`, wait until `status` shows `stopped`, then
`swarmctl orchestrator delete NAME`, then on the worker:
`sudo systemctl disable --now swarm-orchestrator@NAME` (on a Mac: `setup-macos.sh --remove-orchestrator NAME`).
The supervisor cannot disable its own unit. If it is still running when the record is deleted, it ends the
session, and its log says the line to run. A supervisor that is stopped while its orchestrator should not run
ends the session too, so disabling the unit never leaves an unseen Claude behind.

Watching it live on the worker (read only, do not type into it unless you mean to):
`sudo -u agent tmux -L swarm-orch-NAME attach -r -t orch-NAME`.

**Messages.** One at a time: a second message is refused until the first is typed in. Up to 4,000
characters. The supervisor types it when the terminal has been quiet for 20 seconds and does not show
"esc to interrupt". It arrives with a header saying it is a request, not permission to push, merge or deploy.

**Logs.** The supervisor sends the last 200 lines of the terminal with each heartbeat. Every line that
matches the secret patterns the runner uses before a push (`config/secret-patterns.txt`) is replaced by
`[line hidden: it looks like a secret]`, by the supervisor and again by the backend. Sign-in links (OAuth
`authorize` links, device-login links) are replaced by `[sign-in link hidden]`, so a pending sign-in is never
uploaded.

## What it can do

An orchestrator runs with the project's rules and the credentials the worker already gives tasks, nothing
more. In the kit's standard setup the agent user has **no GitHub login**: the runner pushes for tasks. So an
orchestrator works in its own folder (a clone of the repo when `--repo` is set) and cannot push, merge or
deploy by itself. To ship, give it a bot token in a runtime bundle and let it queue normal worker tasks
(`with-secrets app-bot-token -- swarmctl enqueue ...`); those publish through the runner with the usual
project rules and secret scan. Never give an orchestrator rights to branches it does not own.

Secrets: the supervisor loads the named runtime bundles through `with-secrets` when it starts the session.
Only bundles synced as runtime bundles on that worker exist there; admin-only bundles never do, so naming
one stops the start with "a secret bundle is not available", and nothing runs. The settings in
`/etc/agent-swarm/agent.env` (for example `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, or your proxy address) apply
to the session as they do to tasks. No value from that file or from a bundle is printed, logged or sent.

## Agent Relay (optional)

If your worker runs Agent Relay, an orchestrator can have a fixed name there so other bots can message it.
Set `--relay-name` on the record and, in `/etc/agent-swarm/swarm.env`, the command that registers a name:

```dotenv
SWARM_RELAY_REGISTER_CMD=relay agent register {name}
```

The supervisor runs it as the agent user before every start and restart (so the name comes back after a
crash), with `{name}` replaced. Its output is thrown away, since it may hold a token: no Relay key or token
goes into the record, the API, the logs or the events, only "registered" or "failed". Keys come from the agent
user's own Relay setup or `agent.env`, never from the backend. The session also gets
`SWARM_RELAY_AGENT_NAME` in its environment, for a Relay session hook that needs it. Without the setting,
orchestrators work the same and only take messages through `swarmctl orchestrator message`.

## Settings

In `/etc/agent-swarm/swarm.env` (all optional):

| Setting | Default | What it does |
| --- | --- | --- |
| `SWARM_RELAY_REGISTER_CMD` | empty (no Relay) | Command that registers `{name}` on Agent Relay |
| `SWARM_ORCH_CLAUDE` | `claude` | The Claude Code command |
| `SWARM_ORCH_STOP_GRACE` | 120 | Seconds a graceful stop waits for the wrap-up |
| `SWARM_ORCH_DEFER_REPORT_SECONDS` | 120 | A message the pane holds back this long is reported once per reason as `delivery-deferred` (0.8.1) |
| `SWARM_ORCH_PANE_ADAPTER` | claude | Tests only: `plain` marks a box-less stand-in program instead of Claude Code (0.8.1) |
| `SWARM_ORCH_IDLE_SECONDS` | 20 | Quiet seconds before a message is typed in |
| `SWARM_ORCH_BACKOFF_START`, `SWARM_ORCH_BACKOFF_MAX` | 5, 300 | First and longest pause before a restart |
| `SWARM_ORCH_CRASH_LIMIT`, `SWARM_ORCH_CRASH_WINDOW` | 5, 1800 | Crashes within this many seconds that make a crash loop |
| `SWARM_ORCH_SCOPE` | `SWARM_TASK_SCOPE` (1) | Run the session in its own systemd user scope |
| `SWARM_ORCH_POLL_SECONDS` | 10 | How often the supervisor looks at the session (was 5 before 0.7.1) |
| `SWARM_ORCH_MESSAGE_POLL_SECONDS` | 10 | While no message is held, how often the supervisor asks the backend for the next one, idle or busy (0.8.0, busy too since 0.8.1; see [agent-bus.md](agent-bus.md)) |
| `SWARM_ORCH_BUSY_MAX_WAIT_SECONDS`, `SWARM_ORCH_BUSY_URGENT_WAIT_SECONDS`, `SWARM_ORCH_BUSY_GAP_SECONDS` | 90, 15, 5 | While the session is busy: how long a queued message waits before it is pasted (urgent: the shorter wait), and the pause between two busy pastes (0.8.1) |
| `SWARM_ORCH_SCREEN_SECONDS` | 10 | How long a sign-in screen may stay up before the session is ended |
| `SWARM_ORCH_LOGIN_MARKERS`, `SWARM_ORCH_BLOCK_MARKERS` | see `server/swarm_orchestrator.py` | Text (several separated by a vertical bar) that marks a sign-in screen or a screen it does not answer |
| `SWARM_ORCH_LOGIN_CHECK` | 1 | 0 skips the sign-in check before a start (the screen check still runs) |

API: `GET /orchestrators`, `POST /orchestrators`, `GET` and `PUT /orchestrators/NAME`,
`POST /orchestrators/NAME/start|stop|restart|delete`, `GET /orchestrators/NAME/logs`,
`POST /orchestrators/NAME/messages`, and for the supervisor only `POST /worker/orchestrators/NAME/heartbeat`.
Who may call what is in [backend.md](backend.md#who-can-call-what).

## Honest limits

- **Tested with stand-ins.** The tests run the real supervisor, agent-exec and tmux with a stand-in for Claude
  Code, for `with-secrets` and for the Relay command. They have not run a real Claude session for days, a
  real Relay registration, the systemd unit and user scope on a real server, or the launchd agent on a real Mac.
- **First start on a new worker.** Claude Code in a terminal (not `-p`) shows one-time screens. The supervisor
  answers the theme picker, the security notes and the older folder trust question with Enter, and the current one
  ("Quick safety check") with Down, then Enter once "Yes, I trust this folder" is selected. It does not accept the
  bypass-permissions warning by itself (Enter there means "No, exit"): if `needs-login` says it stopped at that
  screen, accept it once by hand as the agent user (`sudo -iu agent claude --permission-mode bypassPermissions`,
  pick "Yes, I accept", then `/exit`), then `swarmctl orchestrator start NAME`. The screen texts are matched as
  Claude Code 2.1 shows them; they were checked against a real install for the theme picker, the sign-in
  menu and the current folder trust screen.
- **Sign-in screens are matched by text.** A sign-in phrase that the agent itself prints and leaves at the
  bottom of an idle screen for 10 seconds would also stop it. Change the list with `SWARM_ORCH_LOGIN_MARKERS`.
- **Idle detection is a guess.** "Quiet terminal and no `esc to interrupt`" is how Claude Code looks today. If
  that changes, set `SWARM_ORCH_BUSY_MARKERS` (separated by `|`).
- **Workers share one token.** Pinning is by worker name, as for tasks ([multi-worker.md](multi-worker.md)):
  a routing rule between trusted workers, not a security boundary.
- **A long session costs more.** It grows over days. Auto-compact (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`) and the
  checkpoint help; a chain avoids the problem.
