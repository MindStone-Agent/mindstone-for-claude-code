---
description: Wrap up the session before /exit. Composes the reflective dream-cycle (/checkpoint when warranted) with the mechanical archive (transcript vectorization + hit-counter updates) so both layers land before the session closes.
---

# End-session — wrap up the session before /exit

Final wrap-up before `/exit`. Composes the reflective dream-cycle (`/checkpoint`) and the mechanical archive (transcript vectorization + hit-counter updates) so both layers land before the session closes. After `/end-session` completes, you can `/exit` knowing the session texture is preserved and any session-shaped reflection is captured.

The mechanical archive matters because the Stop hook fires per-turn-completion, not on session end — so sessions that end without a final completed turn (`/exit` after an error, abrupt termination, image-dimension errors, etc.) skip the auto-archive. `/end-session` is the user-actionable workaround until the v3 watchdog ships (auto-archive every N turns from `UserPromptSubmit`).

## When to invoke

- **Before `/exit`** — anytime you're closing a session you want preserved
- **When a session is ending on an error** and the Stop hook may not fire reliably
- **Anytime the user signals "wrap up"** without explicitly running `/checkpoint`

This is additive — running it after `/checkpoint` is fine (idempotent), and running it without `/checkpoint` is also fine (it just does the mechanical layer).

## When NOT to invoke

- Trivial conversational sessions where no work happened (the Stop hook from any prior assistant turn already captured the texture)
- Mid-session — only run before exit/end. Repeated mid-session calls aren't harmful but aren't useful either.

## Relationship to `/checkpoint`

`/checkpoint` is a *reflective punctuation mark* that can be invoked multiple times during a session at natural breaks. `/end-session` is the *wrap-up* before `/exit` — it composes `/checkpoint` (when warranted) with the mechanical archive.

**Rule of thumb before `/exit`:** run `/end-session`. It handles the checkpoint-if-needed and the archive in one go.

Stand-alone `/checkpoint` is for mid-session reflection without ending. The Stop hook handles per-turn mechanical archival; `/end-session` handles the post-error / post-`/exit-incoming` archival the Stop hook can't.

## Protocol

### Step 1 — Reflective layer (invoke `/checkpoint` if warranted)

Quick judgment: was there meaningful work this session that hasn't been checkpointed yet?

- **Decisions made, memories cited, artifacts produced, or substantive work since the last checkpoint** → run `/checkpoint` first. Walk through its full protocol: synthesize, increment hits, ask Option D, propose new memories, flag drift, append to `LOG.md`. Then continue to step 2 below.
- **Trivial session, OR `/checkpoint` was already run recently and nothing meaningful has happened since** → skip the reflective layer and proceed to step 2.
- **Error-ended session where `/checkpoint` can't run (model calls blocked)** → skip and proceed to step 2; the mechanical archive is what's recoverable.

When in doubt, lean toward running `/checkpoint`. Over-checkpointing is cheaper than missing reflective synthesis on a session that mattered.

### Step 2 — Find the current session JSONL

The current session's JSONL lives at `~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl`. CWD escape rule: replace `/` with `-`. Find the most recent JSONL in that directory:

```bash
ls -t ~/.claude/projects/$(pwd | sed 's|/|-|g')/*.jsonl 2>/dev/null | head -1
```

The filename is `<session-uuid>.jsonl` — extract the UUID.

### Step 3 — Invoke session_end.py with the resolved session ID

```bash
echo '{"session_id": "<UUID>", "cwd": "<absolute-path-to-cwd>"}' | \
  orchestrator/.venv/bin/python orchestrator/hooks/session_end.py
```

(The hook's fallback path will work even without `session_id` if you pass only `cwd`, since I fixed the Python precedence bug on 2026-04-26 — but explicit is better than implicit. Pass the UUID.)

### Step 4 — Verify the archive

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

### Step 5 — Confirm to user

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
