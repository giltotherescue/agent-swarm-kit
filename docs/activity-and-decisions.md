# Activity and decisions

Each project bot and the Chief of Staff write one short line when something ships, changes, gets
blocked, or is decided. Write for the affected project. For swarm-wide work use the project
`cross-project`; any bot can write there and only the Chief of Staff and admin can read it. Expect zero to five lines a day per project. A check that finds nothing new writes
nothing. Never log secrets or personal data, even in links. Use a short, plain summary, not a transcript.

```text
- YYYY-MM-DD HH:MM CT | bot | kind | summary | link
```

`CT` means Central Time. The backend sets the clock and bot ID. Use `-` when there is no link. Links
must use HTTPS (secure web links), without credentials or query strings. A summary is one line of up
to 600 characters. Use a stable key on retries: the same key from the same bot within 24 hours returns
the first entry instead of adding a second.

| Kind | When to write it |
| --- | --- |
| `activity` | A real change worth tracking |
| `shipped` | Something went live or was delivered, not merely queued |
| `blocked` | A new blocker that stops work |
| `decision` | A call was made. Needs `--decider`, see below |

| Decider | Who made the call |
| --- | --- |
| `owner` | The owner actually said yes or made the call. Only the Chief of Staff or admin records it |
| `delegated` | The Chief of Staff, for the owner, under the [delegation rule](delegated-decisions.md) |
| `bot` | A bot, within its own brief |

```bash
swarmctl activity-log add PROJECT --kind shipped "Published the help page" --key HELP-PAGE-V2
swarmctl activity-log add PROJECT --kind decision --decider bot "Kept the short checklist" --key CHECKLIST
swarmctl activity-log list PROJECT --limit 10 --kind decision
swarmctl activity-log export PROJECT --month YYYY-MM
```

The list command is the recent feed. There is no web dashboard. The short feed lives in the backend's
SQLite `activity_log` table. The existing [project log](project-log.md) still holds task results, item
versions, feedback and rules. Its automatic entries do not replace a deliberate feed entry.
See [the backend](backend.md#who-can-call-what) for the application programming interface (API).

## When a worker task may log

Put `Activity log: yes` on a line by itself in a trusted task brief. On `done` or `blocked`, if there
is a real update, the task writes one object to `.task-context/activity-log.json`:

```json
{"kind":"blocked","summary":"Waiting for the approved copy"}
```

The runner posts it with the task's current
claim. The task never gets a bot or worker credential. The entry belongs to the task's project and
owner bot. A task can write `activity`, `shipped` or `blocked`, never a decision. Do not opt in a task from untrusted input.
The owning bot should not log the same event again. Small, bot-only swarms can keep the same lines and
decisions in memory notes instead.

## Two homes for decisions

Every decision goes in the feed. Add `--lasting` when it changes how the project works, such as a
standing workflow or a confirmed change in goals. The nightly snapshot also puts that entry into
`<project>/decisions.md` under its date and `Owner decided`, `Delegated by Chief of Staff`, or
`Project bot calls` for a bot's own lasting choices. The owner heading is only for what the human
actually decided. The Chief of Staff's calls are always overrulable by the owner.

```bash
swarmctl activity-log add PROJECT --kind decision --decider owner --overrules ENTRY-ID "Use the full checklist"
```

Only a decision can overrule, and only an owner decision can overrule an owner decision. The new entry names the old ID. If the original was lasting, its overrule is lasting too. Never edit or
delete the old line. The new owner entry is what overrules it. Tell the affected bot what changed.

## Nightly export to the memory repo

The memory repo is a separate, private, docs-only repo with `main` as its default branch. A project
can have `README.md` for its brief, `tasks.md` for open work, `decisions.md` for lasting choices, and `log/` for the monthly feed. To start,
put `# Project brief` in `README.md`, `# Open work` in `tasks.md`, and `# Decisions` in `decisions.md`.
The export adds the date sections and headings. An empty `log/` need not be committed.

The owner sets up a dedicated backend project for that **one repo**. Do not add any code repo to it.
Grant main-branch publishing on this project only. The Chief of Staff and bound project bots can then
queue docs updates whenever needed, without a pull request (PR). The example binds one project bot.

```bash
swarmctl project set MEMORY-PROJECT --repo ORG/MEMORY-REPO --bot PROJECT-BOT --allow-push-main
```

Each night the Chief of Staff runs the first command below for each project. The backend remembers
the last published entry per project, so the snapshot holds only what is new, and empty output means
nothing changed: queue nothing and send nothing. Otherwise put the nonempty snapshots in one
`nightly.json` array and queue one docs-only task with it attached (second command), on a low-cost
model, with at most one export in flight. The task brief runs the offline apply (third command) from
the memory checkout and commits only the changed docs. The worker pushes to `main` without a pull
request and reports `done` only after the push succeeded. Then, and only then, the Chief of Staff
saves the position with the fourth command, using each snapshot's `through`. On failure it saves
nothing, and the next night's snapshot covers the same entries again. Do not log the export or send
success messages. Never add secrets or personal data to memory docs.

```bash
swarmctl activity-log export PROJECT --nightly
swarmctl enqueue --project MEMORY-PROJECT --owner CHIEF-BOT --push-main --attach nightly.json brief.md
swarmctl activity-log apply .task-context/attachments/nightly.json --dir .
swarmctl activity-log published PROJECT THROUGH
```

The snapshot includes full Markdown for each changed `<project>/log/YYYY-MM.md`, including any months
missed while the routine was off. It includes the generated `decisions.md` block only when a lasting
decision changed. The apply command replaces only the block between `<!-- activity-log:begin -->` and
`<!-- activity-log:end -->`; it appends it if there are no markers. It refuses mismatched markers and
keeps anything outside them untouched. It refuses snapshots that would drop old entries, and applying
the same or an older snapshot again changes nothing. Do not hand edit the generated block or monthly
files. `--after ID` on the export overrides the saved position, for example `--after 0` to rebuild.
