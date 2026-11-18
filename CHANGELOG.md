# Changelog

All notable changes to this kit. Versions follow semantic versioning (MAJOR.MINOR.PATCH):
MAJOR breaks something you set up, MINOR adds something, PATCH fixes something.
See docs/updating-the-kit.md for how to update.

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
