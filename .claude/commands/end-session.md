---
description: Mechanical session-end archive. Runs orchestrator/hooks/session_end.py to archive the current session's JSONL, vectorize new chunks, and auto-increment memory hits — work that the Stop hook normally does but that gets skipped when /exit fires without a completed turn.
---

# End-session — mechanical archive before exit

Use this before `/exit` to ensure the current session's texture is captured in the vector store and memory hit counters are updated. The Stop hook fires per-turn-completion, not on session end — so sessions that end without a final completed turn (`/exit` after an error, abrupt termination, image-dimension errors, etc.) skip the auto-archive. This command is the user-actionable workaround until the v3 watchdog ships.

## When to invoke

- **Before `/exit`** — anytime you're closing a session you want preserved
- **When a session is ending on an error** and the Stop hook may not fire reliably
- **Anytime the user signals "wrap up"** without explicitly running `/checkpoint`

This is additive — running it after `/checkpoint` is fine (idempotent), and running it without `/checkpoint` is also fine (it just does the mechanical layer).

## When NOT to invoke

- Trivial conversational sessions where no work happened (the Stop hook from any prior assistant turn already captured the texture)
- Mid-session — only run before exit/end. Repeated mid-session calls aren't harmful but aren't useful either.

## Relationship to `/checkpoint`

`/checkpoint` (the reflective dream-cycle) **invokes `/end-session` as its final step**. So if the session warrants reflection — decisions made, memories cited, work shipped — run `/checkpoint` instead; it does both layers.

Use `/end-session` standalone when:
- The session was trivial (no decisions, no memory citations, no produced artifacts)
- The session is ending on an error and `/checkpoint` can't run (model calls blocked)
- You've already done the reflective work elsewhere and just need the mechanical archive

**Rule of thumb before `/exit`:** ran `/checkpoint`? Done. Skipping `/checkpoint` because it's not warranted? Run `/end-session` and you're done.

## Protocol

### Step 1 — Find the current session JSONL

The current session's JSONL lives at `~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl`. CWD escape rule: replace `/` with `-`. Find the most recent JSONL in that directory:

```bash
ls -t ~/.claude/projects/$(pwd | sed 's|/|-|g')/*.jsonl 2>/dev/null | head -1
```

The filename is `<session-uuid>.jsonl` — extract the UUID.

### Step 2 — Invoke session_end.py with the resolved session ID

```bash
echo '{"session_id": "<UUID>", "cwd": "<absolute-path-to-cwd>"}' | \
  orchestrator/.venv/bin/python orchestrator/hooks/session_end.py
```

(The hook's fallback path will work even without `session_id` if you pass only `cwd`, since I fixed the Python precedence bug on 2026-04-26 — but explicit is better than implicit. Pass the UUID.)

### Step 3 — Verify the archive

Two checks:

1. New file in `orchestrator/transcripts/`:
```bash
ls -t orchestrator/transcripts/*.jsonl | head -1
```
The most recent should be today's date + the session UUID.

2. New auto-archive entry in `LOG.md`:
```bash
tail -5 orchestrator/LOG.md
```
Should show `### Auto-archive — <ISO timestamp>` with chunk + hit counts.

### Step 4 — Confirm to user

Brief one-line confirmation: *"Session archived. <N> chunks vectorized, <M> hits incremented. Safe to /exit."*

If anything failed (no archive file, no LOG entry, error in step 2 output), report the failure clearly and suggest manual recovery via the procedure documented in `feedback_exit_does_not_fire_stop_hook.md`.

## Idempotency

Running `/end-session` twice in a row is safe — the hook checks if the archive's mtime is already at or beyond the source JSONL's mtime, and skips re-archiving if so. Running it after `/checkpoint` is also safe for the same reason. No double-vectorization, no double hit-incrementing within the same session window.

## Relationship to other hooks/commands

- **Stop hook** (`session_end.py` invoked automatically) — fires after every assistant turn completion. `/end-session` does the same work the Stop hook would have done, just on demand.
- **`/checkpoint`** — reflective layer (LOG synthesis, drift, Option D, new memories). `/end-session` is the mechanical layer underneath. Both can run in the same session.
- **PreCompact hook** — reminder before manual `/compact`. If you're running `/compact`, run `/end-session` after to ensure post-compact state gets archived.

## Why this exists

Empirical finding 2026-04-26: another Cairn instance got stuck on an image-dimension API error. `/exit` worked but didn't fire the Stop hook (Stop fires per-turn-completion, not on session end). The session JSONL had 36 minutes / 83KB of texture not in the archive. Manual `session_end.py` invocation recovered it cleanly. This command formalizes that recovery into a repeatable user-facing workflow.

The eventual proper fix is the v3 archive watchdog (calling `session_end.py` from UserPromptSubmit every N turns) — see `ROADMAP.md`. Until that ships, `/end-session` is the user-actionable bandaid.
