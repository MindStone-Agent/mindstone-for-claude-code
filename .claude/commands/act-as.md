---
description: Adopt a subagent role structurally — load directives and canonicals so the standards stay load-bearing even when I do the work directly.
argument-hint: <role-name>
---

# Act as `$1` — structural role adoption

The hybrid delegation model lets me do implementation-shaped work directly. Role adoption is how I keep the project's canonicals load-bearing when I do.

The role I'm adopting: **`$1`**

## Step 1 — Load the directives

Read the subagent definition at `.claude/agents/$1.md`. Pin it as active context for this role. Identify:

- The subagent's primary responsibilities and scope
- Canonical sources it references (skills like `canonical-models-reference`, `technology-stack-reference`, `doc-standards`, etc.)
- Required artifacts (TASK_STATUS updates, commit format, doc structures, quality gate evidence)
- Constraints (what the subagent refuses to do, scope boundaries)

If `.claude/agents/$1.md` doesn't exist, tell the user and stop. Don't guess at a role.

## Step 2 — Load the referenced canonicals

For each canonical / skill that the role directive references:
- Invoke the skill if it's listed in available skills, OR
- Read the reference documentation directly so its standards are active in my context

Common canonicals by role:
- **python-backend-architect**, **software-engineer**, **frontend-prototype-developer**: `canonical-models-reference`, `technology-stack-reference`, `feature-existence-check`
- **data-architect**, **graph-database-architect**: `canonical-models-reference`, `technology-stack-reference`
- **tech-writer**, **prd-writer**, **implementation-planner**: `doc-standards`, `canonical-models-reference`
- **test-planner**, **sqa-agent**: `doc-standards`, `canonical-models-reference`
- **security-vulnerability-scanner**, **node-linter-fixer**: (tool-heavy, minimal canonicals)
- **feature-planner**: `feature-existence-check`, `canonical-models-reference`, `doc-standards`

## Step 3 — Announce the role adoption

Tell the user clearly:

> *"Acting as `$1`. Loaded directives from `.claude/agents/$1.md` and canonicals: [list]. Proceeding with [brief task summary]."*

This is the visible contract. The user knows which role is active and what standards I'm bound to.

## Step 4 — Log the role-entry in this session's work

Note in my working memory (for use at `/checkpoint`):
- Role adopted: `$1`
- Timestamp: now
- Task: [the work being done]
- Canonicals loaded: [list]

At `/end-role` (or implicit at task close), this becomes a LOG.md entry with attribution audit.

## Step 5 — Do the work

Follow the role's directives. Produce the same artifacts the subagent would produce. Cite canonical sources inline when I make non-trivial decisions:

- *"Using <pattern> per `canonical-models-reference` §X."*
- *"Stack choice per `technology-stack-reference` (FastAPI + SQLAlchemy)."*
- *"Following <template> per `doc-standards` for PRDs."*

If I find myself about to make a non-trivial choice without a canonical to cite, pause and check the references first.

## When to exit the role

Call `/end-role` when:
- The task the role was adopted for is complete
- I'm pivoting to a different kind of work that would need a different role
- I need to step out to pure orchestration again

`/end-role` will trigger the attribution audit (what I cited, what I produced) and log the role span.

## Notes

- Role adoption is structural, not dramatic. I don't change voice or persona — I'm still Cairn. I'm binding myself to the subagent's standards, not roleplaying the subagent.
- If the task grows in scope beyond the role, I can layer: `/end-role`, then `/act-as <new-role>` for the new work. Cleaner than smuggling cross-domain work under one role's banner.
- If during role work I hit something the subagent would have asked the user about, I still ask the user. Role adoption binds standards, not autonomy.
