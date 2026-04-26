---
description: Dream-cycle checkpoint — synthesize this session, update LOG.md, increment memory weights, flag drift.
---

# Checkpoint — persist this session to the orchestrator's memory

The dream-cycle moment where session experience becomes persistent memory. Run at natural breaks, pre-compaction, session end, or when Clint asks.

**Reminder: most mechanical work is now automatic.** The Stop hook (`orchestrator/hooks/session_end.py`) handles transcript archival + vectorization + auto-increment of `hits` counters on cited memories every session end. `/checkpoint` is for the *judgment* parts: synthesis, new-memory proposals, drift detection, prevented-confirmation.

## Protocol

### 1. Synthesize the session

Draft an entry for `testflight/orchestrator/LOG.md` in this format:

```markdown
## YYYY-MM-DD — short title

**Project(s):** [list]
**Scope:** one-line summary

### What happened
- bullet summary

### Decisions made
- bullet

### Memories cited (auto-tracked by Stop hook, verify and annotate)
- filename.md — why it was useful

### Prevented confirmations (Option D — ask if anything genuinely prevented a mistake)
- filename.md — confirmed by Clint

### New memories proposed
- (from step 3)

### Drift flagged
- (from step 4)

### Lint
- (from step 5)
```

Show Clint the draft. Let him edit or approve.

### 2. Prevented confirmations (Option D, terse)

Pull the list of memories that were `hits` in this session (the Stop hook writes them to LOG automatically once it fires; until then, enumerate from your citations in the session). Ask Clint:

> *"Which of these prevented a mistake? ('1, 3, 5' works, or 'none'.)"*

Increment `prevented` by 1 for each confirmed. Note the confirmations in the LOG entry.

**When to skip this step:** If no memories were obviously cited, or the session was low-stakes. Don't force the ritual.

### 3. Propose new memories — with semantic check first

Before drafting a new memory, **check if similar memory already exists**:

```bash
orchestrator/.venv/bin/python orchestrator/hooks/recall.py "<concept to check>" --source memory --k 5
```

If a similar memory exists (similarity > ~0.55), propose *updating* the existing one rather than creating a duplicate. If nothing matches, draft the new memory:

- File at `testflight/orchestrator/memory/<type>_<short_name>.md`
- v0.2 frontmatter schema (see `CAIRN_DESIGN_v0.2.md` §6)
- `type` ∈ {feedback, project, reference, design}
- `tags` and `projects` — **infer from content and filename; do NOT ask Clint** (he doesn't tag)
- Critical flag only for load-bearing rules (rarely)

Show Clint the draft before writing. On accept: write file, add pointer to `MEMORY.md`. The Stop hook will vectorize it on next session end (or you can manually index it now via `orchestrator/.venv/bin/python orchestrator/hooks/indexer.py backfill`).

### 4. Drift detection

Review the session for:

- **Role-shaped work without `/act-as` declaration.** Did I implement, debug, write tests, or produce artifacts that would normally go to a subagent without calling `/act-as`? Flag it.
- **Non-trivial decisions without canonical attribution.** Framework, pattern, or doc-structure choices without citing `canonical-models-reference`, `technology-stack-reference`, `doc-standards`, or equivalent? Flag the specific decision.
- **Memory contradictions.** Two memories that conflict? Name them.
- **Artifact skips.** Shipped work without updating `TASK_STATUS.md` or equivalent? Flag.

Write findings under "Drift flagged" in the LOG entry. Accumulated drift warrants a review.

### 5. Lint — semantic-assisted

Use the vector store to surface issues:

- **Stale memories:** `weight` decayed near zero (created >90 days ago AND `hits: 0`). Note for potential archiving.
- **Duplicate/redundant content:** After a new memory is proposed, run a semantic search for it; if top-3 matches are all existing memories with high sim, consider merging rather than adding.
- **Outdated claims:** A project sprint memo from months ago no longer reflecting current state — note for refresh.

Observations, not actions. Clint can act on them later.

### 6. Append to LOG.md

Once Clint approves the entry, append to the end of `testflight/orchestrator/LOG.md`. Preserve chronological order.

## Automation — what the Stop hook handles

The Stop hook (`orchestrator/hooks/session_end.py`) runs at every session end and:

- Copies the session's JSONL from `~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl` to `orchestrator/transcripts/YYYY-MM-DD__<uuid>.jsonl`
- Chunks + embeds + stores transcript chunks in `orchestrator/vectors.db`
- Scans the transcript for memory-filename citations and auto-increments `hits` on matching memory files (also sets `last_applied`)
- Appends a one-line `### Auto-archive` note to `LOG.md`

This happens regardless of whether `/checkpoint` is invoked. The manual `/checkpoint` is additive: it does the judgment parts the Stop hook can't.

## When NOT to run /checkpoint

Skip if:
- Session was purely transactional (trivial Q&A)
- No decisions made, no memories cited obviously
- Clint says "don't checkpoint this"

The Stop hook will still auto-archive, so nothing is lost even if `/checkpoint` is skipped.

## Relationship to other hooks/commands

- **SessionStart hook** — loads identity, user, critical memories, memory index, recent LOG tail
- **UserPromptSubmit hook** — semantic recall per user turn based on prompt content
- **PreCompact hook** — reminder to run `/checkpoint` before compaction
- **Stop hook** — auto-archives transcript + vectorizes + auto-increments hits
- **`/act-as` and `/end-role`** — produce role-span LOG entries that `/checkpoint` rolls up

The full stack: `/checkpoint` is the reflective ritual on top of the mechanical persistence layer.
