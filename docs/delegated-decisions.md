# Delegated decisions

The Chief of Staff can answer a small, reversible project question instead of adding it to Waiting on
you. The answer must fit the project's goals, brief and latest reported status. If any are missing or
the Chief of Staff is unsure, ask the owner instead.

## Limits

Do not delegate a call that:

- Spends money, publishes, or sends something outside the swarm.
- Merges or deploys beyond an existing grant. Delegation cannot create a grant.
- Touches production data or secrets.
- Changes direction or priorities, or hits anything marked `Needs owner` in the brief.

A delegated call is not approval for an owner-only action. Do not use it to answer a task marked
`--ask-human`. Keep the [approval rules](approvals.md) and any tighter project limits.

## Answer, log, tell the owner

1. Reply in the project bot's chat so it can keep working.
2. Add one decision with `--decider delegated` to the [activity feed](activity-and-decisions.md). Use the
   question ID as a stable key. Add `--lasting` only if the call changes how the project works.
3. Put the call in the owner's next morning note once, with the ID and a short reason. Send one note
   for both sections, omitting empty sections and sending nothing when both are empty.

```text
Needs owner
1. [PROJECT] Question. Recommend: answer. (link)

Delegated since the last note
- [PROJECT] #ENTRY-ID: answer. Why: small and reversible. (link)

Reply "overrule #ENTRY-ID: new answer" to replace a call.
```

When the owner overrules a call, add a new owner decision with `--overrules ENTRY-ID` and tell the
project bot. Leave the old line in place. An owner answer to a Waiting on you item is also a
owner decision when it makes a decision; record the owner's words, not your recommendation.

## Change the default

The owner can loosen this default or turn it off. Edit `Delegation` in the
[Chief of Staff instructions](../templates/chief-of-staff.md) and put project exceptions under `Needs
owner` or `Delegation` in the [project brief](../templates/project-bot-brief.md). Write exact grants and
limits, for example `Delegation: off` or `Allow reversible choices about internal checklist layout up
to one workday`. Owner-only actions still need separate approval. Do not infer a grant from silence.
