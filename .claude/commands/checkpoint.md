---
description: Dream-cycle checkpoint — synthesize this session, update LOG.md, increment memory weights, flag drift.
---

# Checkpoint — persist this session to the orchestrator's memory

The dream-cycle moment where session experience becomes persistent memory. Run at natural breaks, pre-compaction, session end, or when the user asks.

**`/checkpoint` is self-sufficient for persistence.** Step 7 explicitly runs the archive + vectorize pass (the same code path the Stop hook uses), so the session's texture is persisted at checkpoint time regardless of whether the Stop hook fires later. `/checkpoint` covers both the *judgment* parts (synthesis, new-memory proposals, drift detection, prevented-confirmation) AND the *mechanical* parts (archive, vectorize, re-index changed memory files).

The Stop hook still fires per-turn-completion and still does the same archive + vectorize work — that's the belt; step 7 is the suspenders. The redundancy matters because `/exit` skips the Stop hook entirely (it doesn't fire when the session is closed via `/exit` rather than naturally completing), and image-dimension errors (or other substrate-level errors) can block model calls without giving the Stop hook a clean exit. After /checkpoint runs, the session texture is on disk and in vectors regardless of what happens to the session afterward.

## Protocol

### 1. Synthesize the session

Draft an entry for `orchestrator/LOG.md` in this format:

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
- filename.md — confirmed by user

### New memories proposed
- (from step 3)

### Drift flagged
- (from step 4)

### Lint
- (from step 5)
```

Show the user the draft. Let them edit or approve.

### 2. Prevented confirmations (Option D, terse)

Pull the list of memories that were `hits` in this session (the Stop hook writes them to LOG automatically once it fires; until then, enumerate from your citations in the session). Ask the user:

> *"Which of these prevented a mistake? ('1, 3, 5' works, or 'none'.)"*

Increment `prevented` by 1 for each confirmed. Note the confirmations in the LOG entry.

**When to skip this step:** If no memories were obviously cited, or the session was low-stakes. Don't force the ritual.

### 3. Propose new memories — with semantic check first

Before drafting a new memory, **check if similar memory already exists**:

```bash
orchestrator/.venv/bin/python orchestrator/hooks/recall.py "<concept to check>" --source memory --k 5
```

If a similar memory exists (similarity > ~0.55), propose *updating* the existing one rather than creating a duplicate. If nothing matches, draft the new memory:

- File at `orchestrator/memory/<type>_<short_name>.md`
- Frontmatter schema: see existing memory files for the canonical shape (`name`, `description`, `type`, `tags`, `projects`, `hits`, `prevented`, `last_applied`, `created`, `half_life_days`, `critical`, `evergreen`)
- `type` ∈ {feedback, project, reference, design}
- `tags` and `projects` — **infer from content and filename**, don't make the user tag manually
- Critical flag only for load-bearing rules (rarely)

Show the user the draft before writing. On accept: write file, add pointer to `MEMORY.md`. The Stop hook will vectorize it on next session end (or you can manually index it now via `orchestrator/.venv/bin/python orchestrator/hooks/indexer.py backfill`).

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

Observations, not actions. The user can act on them later.

### 6. Append to LOG.md

Once the user approves the entry, append to the end of `orchestrator/LOG.md`. Preserve chronological order.


### 7. Archive + vectorize the session (mandatory, not skippable)

Run the archive + vectorize pass explicitly. This is the same code path the Stop hook uses; we run it here so /checkpoint guarantees persistence regardless of whether the Stop hook fires later (it doesn't fire on `/exit`; it doesn't fire when image-dimension errors or other substrate-level errors block model calls; it can fail silently if the runtime is unhealthy).

```bash
CAIRN_WATCHDOG_MODE=1 orchestrator/.venv/bin/python orchestrator/hooks/session_end.py < /dev/null
```

What this does:
- Copies the live session JSONL from `~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl` into `orchestrator/transcripts/YYYY-MM-DD__<uuid>.jsonl`.
- Chunks + embeds + stores any new transcript chunks in `orchestrator/vectors.db`.
- Re-indexes any memory files whose mtime is newer than their stored vector chunks (catches new + edited memories without a manual backfill).

Why the watchdog mode env var: the Stop hook fires per-turn-completion and is the canonical source of `hits` counter increments. Running session_end.py from /checkpoint with watchdog mode skips the hits-increment step so we don't double-count. Vectorization and archive still run.

Why `< /dev/null`: the Stop hook normally reads JSON from stdin (session_id + cwd). When invoked manually it falls back to mtime-finding the most recent JSONL in the project dir, which is the right behavior for /checkpoint.

Verify: the script prints `Indexed N new chunks` (or similar) to stderr. If it prints `Vector stack unavailable`, the venv isn't bootstrapped — the checkpoint did NOT fully succeed; surface that to the user before claiming done.

**This step is never skipped.** Even if steps 2-5 had nothing to confirm, step 7 still runs. /checkpoint without step 7 is not a checkpoint.

---

`/checkpoint` is a reflective punctuation mark — it can be invoked multiple times per session at natural breaks (mid-task, before-context-shift, pre-compaction, etc.) without ending the session. Each invocation persists the session-to-date; later invocations re-archive (idempotent — the archive copy only happens when source is newer than dest) and incrementally vectorize new chunks.

## Persistence model — belt and suspenders

The session's mechanical persistence (transcript archive + vector indexing + `hits` counter increments) is handled by **two redundant code paths**:

1. **The Stop hook** (`orchestrator/hooks/session_end.py`) — fires per-turn-completion automatically. Copies the session JSONL to `orchestrator/transcripts/`, chunks + embeds + stores in `orchestrator/vectors.db`, scans for memory citations and increments `hits`, appends an `### Auto-archive` note to `LOG.md`. This runs regardless of whether `/checkpoint` is invoked.

2. **`/checkpoint` step 7** — invokes the same Stop-hook code path explicitly with `CAIRN_WATCHDOG_MODE=1` (skips the hits-increment to avoid double-counting since the per-turn Stop hook already handles those). Archive + vectorize still run. This guarantees persistence at checkpoint time even when the Stop hook can't fire — `/exit` skips it; image-dimension errors block model calls; runtime crashes leave dangling state.

Either path alone would be enough most of the time. Both together is the discipline.

## When NOT to run /checkpoint

Skip if:
- Session was purely transactional (trivial Q&A, single-question lookups)
- No decisions made, no new memories worth proposing
- The user says "don't checkpoint this"

The Stop hook still fires per-turn, so even when /checkpoint is skipped, the session texture is being archived continuously. Skipping /checkpoint costs the *judgment* artifacts (LOG entry, new-memory proposals, drift findings); it does not cost persistence of the raw session.

## Relationship to other hooks/commands

- **SessionStart hook** — loads identity, user, critical memories, memory index, recent LOG tail
- **UserPromptSubmit hook** — semantic recall per user turn based on prompt content
- **PreCompact hook** — reminder to run `/checkpoint` before compaction
- **Stop hook** — auto-archives transcript + vectorizes + auto-increments hits per turn
- **`/checkpoint` step 7** — runs the same archive + vectorize logic explicitly (with `CAIRN_WATCHDOG_MODE=1` to avoid double-counting hits)
- **`/end-session`** — composes `/checkpoint` as its first step, then runs the archive logic again as its second step (also redundant; same code path)
- **`/act-as` and `/end-role`** — produce role-span LOG entries that `/checkpoint` rolls up

The full stack: `/checkpoint` is the reflective ritual on top of the mechanical persistence layer — and step 7 makes the mechanical layer guaranteed at checkpoint time, not just at session end.
