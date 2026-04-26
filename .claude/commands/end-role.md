---
description: Exit the currently-adopted subagent role, run attribution audit, and log the role span.
---

# End role — attribution audit and LOG entry

This closes out a role adopted via `/act-as`. It produces an accountability record so the standards binding is visible.

## Step 1 — Name the role I'm ending

State clearly:

> *"Ending role: `<role-name>`. Running attribution audit."*

If no role is currently active (I never called `/act-as` for this work span), say so and skip to step 5 — still worth logging that I did work without a role adoption so drift is visible.

## Step 2 — Attribution audit

List, for the work done under this role:

### Canonicals cited

Every non-trivial decision I made should have cited a canonical source. List them:

- *"Used <pattern> per `canonical-models-reference` §X"* — for [what decision]
- *"Stack choice per `technology-stack-reference`"* — for [what choice]
- *"Followed <template> per `doc-standards`"* — for [what doc]

If the list is empty or sparse relative to the work done, that's a drift signal. Flag it.

### Artifacts produced

What did I actually produce? Map to what the subagent would have produced:

- TASK_STATUS.md updates? (yes/no/N/A)
- Documentation files created or updated? (list)
- Code changes? (files modified)
- Commit(s) created? (SHA + message format match the project convention?)
- Quality gate evidence? (syntax checks, build, tests, etc.)
- UAT / testing notes? (where applicable)

If a subagent would have produced artifact X and I didn't, flag it.

### Deviations from the role's directives

Did I do anything outside the role's scope? Anything the subagent definition would have refused or delegated elsewhere? Name it.

## Step 3 — Compute honest self-assessment

One sentence: was the work done under this role *indistinguishable* from what the subagent would have produced? Yes / no / mostly-yes-with-caveats.

If no or mostly-yes-with-caveats: specifically name what was skipped or deviated, and why.

## Step 4 — Write the role span to LOG.md

Append a short entry to `testflight/orchestrator/LOG.md`:

```markdown
### Role span — YYYY-MM-DD HH:MM → HH:MM

- **Role:** <role-name>
- **Task:** [brief task description]
- **Canonicals cited:** [list]
- **Artifacts produced:** [list]
- **Self-assessment:** [indistinguishable / caveats / deviations]
- **Drift flagged:** [anything worth noting for /checkpoint drift review]
```

This goes under the current session's LOG entry in the relevant subsection. If no session LOG entry exists yet (no `/checkpoint` run), append it under a new date heading.

## Step 5 — Clear the role context

- Announce to the user: *"Role `<role-name>` closed. Returning to orchestrator mode."*
- The role's directives are no longer pinned. I'm back to being Cairn-the-orchestrator.
- If more role-shaped work is coming, I'll `/act-as` again for the appropriate role. Cleaner to layer than to smuggle.

## Relationship to `/checkpoint`

`/end-role` is lightweight — it's per-task. `/checkpoint` is heavy — it's per-session. Role spans logged by `/end-role` get rolled up in the session summary that `/checkpoint` produces. If I run `/checkpoint` without any `/end-role` calls during the session but I did role-shaped work, the drift detector in `/checkpoint` step 5 will catch it.
