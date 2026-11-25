Recheck pull request #{{PR}} in {{REPO}}. This is review round {{ROUND}}, a recheck after fixes, not a fresh review.
{{TIER_LINE}}

The pull request is checked out in this folder. Compare it with the base branch (for example `git diff origin/main...HEAD`).

The earlier review (task {{PRIOR}}) listed these must-fix items:

{{MUST_FIX}}

Your scope:
1. For each item above, say whether it is fixed, with the path:line that shows it.
2. Report a new issue as must fix only if it itself meets the blocking bar: security or auth, data loss or corruption,
   wrong behavior on a common path, money or entitlements, or red CI, with the realistic trigger in one or two sentences.
3. Do not raise new edge cases, races or hardening as must fix. List them as follow-ups (non-blocking) if they are worth
   keeping.

Every finding needs a file path and line (`path/to/file.ext:42`). Do not rewrite the code. Do not commit or push.

Write REPORT.md. In "Summary" say "Ready to merge" when every prior must-fix item is fixed and nothing new blocks, else
"Do not merge". Then put findings under these two headings, one list item each, and write "- None" under an empty one:

## Must-fix findings
## Follow-up findings

The worker files the follow-ups as one GitHub issue labeled `review-followup`, linked from the PR. Then write
AGENT_STATUS (done).
