# Git policy: submodule pins are never auto-committed

The stock kit worker never commits leftover work: it publishes only the commits the agent made itself, and warns
about uncommitted files. The only places the kit commits for someone are the project-log export folders: the
backend's weekly export (`git_commit` in `server/swarm_backend.py`) and `swarmctl log export --dir DIR --git`
(`git_commit` in `server/swarmctl`). Both go through `commit_leftover` in `server/swarm_gitlink.py`, which never
stages submodule gitlinks, and both always pass `allow_gitlink=False`; no task setting changes that. git's own
output is captured; the CLI prints only the redacted skip note, and on a failure a redacted error and exit code 1.

`commit_leftover` works in a copy of the index. It runs `git add -A`, then unstages every staged entry whose index
mode is 160000 on either side (added, modified, deleted), found from `git diff --cached --raw -z`, so it does not
depend on `.gitmodules`. The pin stays modified in the worktree. If only pins remain, no commit is made. The
caller's real index is replaced only after a successful commit, so any failure leaves it exactly as it was.

It fails closed (raises, commits nothing, leaves the index alone) when:

- a pin was replaced by a directory of files (`engine` becomes `engine/new.txt`): unstaging the pin would unstage
  those files too, so commit that change explicitly;
- a pin and an ordinary file or symlink share a path (the pin `engine` replaced by a staged file `engine`, or the
  other way round): resetting it would silently unstage the file, so commit that change explicitly;
- unstaging changed any other staged path, or a gitlink is still staged on the re-scan right before the commit;
- any git command fails or runs longer than 60 seconds. Its stderr is redacted in full first and only then cut to
  the last 300 characters.

The scan forces `--ignore-submodules=none` (a `diff.ignoreSubmodules=all` or `submodule.<name>.ignore=all` cannot
hide a pin), reads raw filename bytes and uses literal pathspecs.

## Redaction

Every gitlink field (path, old sha, new sha) and every log or error line that names one is redacted at its source
and again in the backend, on the full value before it is cut: the secret patterns in `config/secret-patterns.txt`
become `[redacted]`, and key=value secrets (agent-control's rule: a key containing API_KEY, TOKEN, SECRET, PASSWORD
or PASSWD, then `=` or `:`) become `KEY=[redacted]`. A value whose redacted form does not fit its limit (200
characters for a path, 12 for a sha) becomes `[redacted]` whole. A token-shaped path never gets a notice rejected.

## Reporting a skip

A worker (or other caller) that skips pins reports them on `POST /tasks/ID/finish` as
`gitlink_skipped: [{path, old, new}]` (at most 200 entries) and `gitlink_skipped_count` (the true total). The
stock worker forwards these only if its collector supplies them, sanitized first. The backend sanitizes again, then:

- stores the list and the count on the task (`gitlink_skipped`, `gitlink_skipped_count` in `GET /tasks/ID`). An
  explicit observation replaces the earlier one, including on a resumed claim (waiting, answer, finish): a list
  replaces it, and an explicit `[]` clears the list, the count and the note. Omitting `gitlink_skipped` keeps what
  was stored. A replayed finish of the same claim changes nothing;
- adds a `publish` event;
- keeps exactly one note ("gitlink pin not committed") in the task's stored REPORT.md, creating it if the run left
  none, on success and on publish failures alike. The note sits between `<!-- swarm:gitlink-skip-note -->` and
  `<!-- /swarm:gitlink-skip-note -->`; a later observation replaces it (or removes it, and removes a report that
  held only the note). Only the markers, never the note's wording in a report, identify it;
- adds `gitlink_skipped` (at most 10 entries) and `gitlink_skipped_count` (the true total) to finish notices, on the
  bus and on the legacy webhook. A bus notice also gets a `WARNING:` line ahead of the report excerpt.

## Intentional pin bumps

Commit an intentional submodule pin bump explicitly, in a commit the agent (or you) makes; published commits are
never filtered.

`swarmctl enqueue --allow-gitlink` stores `allow_gitlink: true` on the task. The API takes a JSON boolean, 0/1, or
the strings true/false/1/0/yes/no (any case): `true`, `1`, `"true"`, `"1"` and `"yes"` opt in, `false`, `0`,
`"false"`, `"0"`, `"no"` and `""` do not, and anything else is refused with a 400. A follow-up inherits the parent's
value only when it omits the field; an explicit value, JSON `false` included, overrides it. An explicit JSON `null`
is refused with a 400, on a new task and on a follow-up. It is
metadata for a worker that auto-commits on a task's behalf; the stock kit has no such worker, so the flag does not by
itself commit anything. `AGENT_ALLOW_GITLINK=1` is read only by callers of `commit_leftover` that pass no explicit
setting; the stock export passes `False`.
