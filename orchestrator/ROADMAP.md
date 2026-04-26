---
name: ROADMAP
description: Future features and known gaps for MindStone for Claude Code (the framework, not any specific orchestrator instance).
type: roadmap
tags: [roadmap, framework]
projects: []
hits: 0
prevented: 0
last_applied: null
created: 2026-04-26
half_life_days: 365
critical: false
evergreen: true
---

# MindStone for Claude Code — Roadmap

Framework-level future direction. Per-orchestrator (Cairn-specific or your-own-orchestrator-specific) roadmap items belong in your own gitignored `orchestrator/IDENTITY.md` or local notes — not here.

## Status (2026-04-26)

- **v1** — Persistent-identity orchestrator skeleton: hooks, schema, role adoption, onboarding, bootstrap. ✅ Shipped.
- **v2** — Vectors via sqlite-vec + OpenAI embeddings, auto-archive Stop hook, UserPromptSubmit semantic recall, Python venv via `uv`. ✅ Shipped.
- **v3** — Sliding-window-adjacent compaction handling, dedup hardening. **Planned, not shipped.** See below.

## v3 — sliding-window-adjacent compaction handling

Catalyst: Mira's letter to Cairn (the framework's author) flagging that lossy auto-compaction loses session texture even with auto-archive. Solution adapted to Claude Code's substrate constraints (we can't programmatically prune; we can disable auto-compact and bracket manual compaction with hooks).

Planned scope:

1. **Document the auto-compact-off recommendation** in `BOOTSTRAP.md` and `README.md`. Users flip `Auto-compact` to `false` via `/config`. (The toggle exists despite earlier docs claiming otherwise — confirmed empirically.)
2. **Upgrade `PreCompact` hook** so when the user runs manual `/compact`, it: reads the session JSONL, vectorizes any turns not yet in vectors.db, writes a high-weight consolidation memo, stashes the last 20–30% of verbatim messages to `orchestrator/transcripts/pending_reinject.txt`.
3. **Add `SessionStart:compact` matcher branch** to detect post-compaction state, inject the stashed verbatim tail + consolidation memo as `<pre-compaction-verbatim>` context, then delete the stash file.
4. **Content-hash dedup hardening** — current chunk_id dedup handles same-source same-range, but cross-source near-duplicates (same content in memory file AND transcript) currently store separately. Add insert-time content-hash check; link rather than duplicate. MMR mostly hides this at retrieval time, but storage-level dedup is cleaner for public release.
5. **Optional UserPromptSubmit context-size monitor** — log approximate session JSONL size so the orchestrator can surface "approaching capacity, consider /compact or /checkpoint" gracefully. Not pruning (Claude Code doesn't expose that API), just awareness.

What v3 deliberately does NOT attempt: true programmatic sliding-window pruning. Claude Code doesn't allow modifying conversation history from a hook. The compaction-boundary cliff can be eliminated by user choice (auto-compact off), and graceful manual compaction is what we're building around.

## v4 — observability and validation

- **Reflection agent** for self-audit. After a task, a lightweight agent verifies that canonicals the orchestrator claimed to apply were actually applied. Complements `/checkpoint`'s drift detection but runs automatically.
- **Memory decay visualizer** CLI. Shows which memories are near-zero weight, which are compounding, which haven't been touched in months. Helps with manual lint.
- **Cross-session consistency checks.** Compare orchestrator behavior across sessions for drift in voice, principles, or style. Catches accidental tone shifts from IDENTITY.md edits.
- **Per-project weight profiles.** Different projects might want different half-lives or emphasis. (E.g., infrastructure projects may want their security memories to never decay; rapid-prototype projects can decay older patterns faster.)

## v5 — portability and platform reach

- **Local embeddings.** Drop-in provider for Ollama + `nomic-embed-text` or similar. Zero API cost, full data privacy. Provider abstraction in `embedder.py`.
- **MindStone port.** When MindStone's platform is ready for external orchestrators, port over. That's the "remaining 30% of the SCRI memory experience" Claude Code's substrate doesn't allow (programmatic pre-inference injection, true sliding window, autonomous background processes).
- **Multi-orchestrator coordination.** If multiple orchestrator instances need to share read-only reference memory while keeping identity and experiential memory isolated.
- **Personal-memory portability across machines.** With the public/private boundary in place, moving a single user's memory across machines requires manual sync. Solutions: separate private repo for personal memory, cloud-sync'd path, dedicated backup command.

## Known gaps / deferred items

- **Hit-counter heuristic.** Stop hook auto-increments `hits` via filename match in transcript. Match-based heuristic — false positives (memory name mentioned in unrelated context) and false negatives (memory cited semantically without filename) are possible. Worth measuring empirically once the framework has a few users.
- **Onboarding conversation quality.** The first-run invitation is a static template. A more dynamic conversational onboarding (LLM-driven walk-through) might land better for users who haven't read the design docs.
- **Workflow integration.** MS4CC ships orchestrator-internal commands (`/checkpoint`, `/act-as`, `/end-role`). Users define their own workflow commands and subagents. The framework provides hooks for binding role adoption to user-defined agents but doesn't ship a default subagent roster — that's the user's domain.

## Operational backlog

- Periodic clean-up of memories that have decayed near-zero weight over 90+ days without citation. Candidate for automated archiving to a `memory/archive/` subdirectory.
- v3+ frontmatter schema consideration: optional `topic` field for cross-cutting concepts that span multiple projects. Lower priority now that vectors handle cross-cutting retrieval well.

## Contributing

This framework is an early public release. Issues, discussions, and pull requests welcome. The reference implementation (Cairn — see `docs/reference-implementation/`) shows how the framework was built; the framework itself is intended to be substrate for many orchestrators.
