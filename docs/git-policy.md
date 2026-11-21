# Git policy: submodule pins are never auto-committed

The stock kit worker never commits leftover work: it publishes only the commits the agent made itself, and warns
about uncommitted files. The one place the kit commits for someone is the project-log export folder (`git_commit`
in `server/swarm_backend.py`). That commit goes through `commit_leftover` in `server/swarm_gitlink.py`, which never
stages submodule gitlinks. The export always passes `allow_gitlink=False`; no task setting changes that.

`commit_leftover` works in a copy of the index. It runs `git add -A`, then unstages every staged entry whose index
mode is 160000 on either side (added, modified, deleted), found from `git diff --cached --raw -z`, so it does not
depend on `.gitmodules`. The pin stays modified in the worktree. If only pins remain, no commit is made. The
caller's real index is replaced only after a successful commit, so any failure leaves it exactly as it was.

It fails closed (raises, commits nothing, leaves the index alone) when:

- a pin was replaced by a directory of files (`engine` becomes `engine/new.txt`): unstaging the pin would unstage
  those files too, so commit that change explicitly;
- unstaging changed any other staged path, or a gitlink is still staged on the re-scan right before the commit;
- any git command fails or runs longer than 60 seconds.

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

- stores the list and the count on the task (`gitlink_skipped`, `gitlink_skipped_count` in `GET /tasks/ID`); the
  latest finish replaces an earlier observation;
- adds a `publish` event;
- appends one note ("gitlink pin not committed") to the task's stored REPORT.md, creating it if the run left none,
  on success and on publish failures alike;
- adds `gitlink_skipped` (at most 10 entries) and `gitlink_skipped_count` (the true total) to finish notices, on the
  bus and on the legacy webhook. A bus notice also gets a `WARNING:` line ahead of the report excerpt.

## Intentional pin bumps

Commit an intentional submodule pin bump explicitly, in a commit the agent (or you) makes; published commits are
never filtered.

`swarmctl enqueue --allow-gitlink` stores `allow_gitlink: true` on the task (follow-ups inherit it). The API takes a
JSON boolean, or the strings true/false/1/0/yes/no; anything else is refused, so `"false"` never opts in. It is
metadata for a worker that auto-commits on a task's behalf; the stock kit has no such worker, so the flag does not by
itself commit anything. `AGENT_ALLOW_GITLINK=1` is read only by callers of `commit_leftover` that pass no explicit
setting; the stock export passes `False`.
