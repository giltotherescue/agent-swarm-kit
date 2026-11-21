# Git policy: submodule pins are never auto-committed

The kit publishes only commits the agent made itself. The worker never commits leftover work (it warns about
uncommitted files). The one place the kit commits for someone is the project-log export folder
(`git_commit` in `server/swarm_backend.py`). Every such commit goes through `commit_leftover` in
`server/swarm_gitlink.py`, which never stages submodule gitlinks.

After `git add -A` it unstages every staged entry whose index mode is 160000 on either side (added, modified,
deleted), found from `git diff --cached --raw -z`, so it does not depend on `.gitmodules`. The pin stays modified
in the worktree. If only pins remain, no commit is made. Commits the agent makes itself are untouched.

The scan forces `--ignore-submodules=none` (a `diff.ignoreSubmodules=all` or `submodule.<name>.ignore=all` cannot
hide a pin), reads raw filename bytes, uses literal pathspecs, and re-scans before committing. Any git error or
leftover staged gitlink aborts the commit (fails closed).

## Reporting a skip

A worker that skips pins reports them on `POST /tasks/ID/finish` as `gitlink_skipped: [{path, old, new}]`.
The backend redacts every field with the secret patterns (before truncating; a secret-looking value becomes
`[redacted]`), stores the list on the task (`gitlink_skipped` in `GET /tasks/ID`), and adds a `publish` event.
Finish notices carry `gitlink_skipped` (at most 10 entries, paths cut at 200 characters) and `gitlink_skipped_count`
(all of them), on the bus and on the legacy webhook. A bus notice also gets a `WARNING:` line ahead of the report excerpt.

## Opting in

For an intentional pin bump: `swarmctl enqueue --allow-gitlink` (stores `allow_gitlink` on the task; follow-ups
inherit it), or `AGENT_ALLOW_GITLINK=1` in the worker's environment for every task.
