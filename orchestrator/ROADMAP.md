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
- **Intra-session archive watchdog (2026-04-26)** — ✅ Shipped (split out of v3 §6). UserPromptSubmit forks `session_end.py` as a detached background subprocess every N=10 turns; `session_end.py` honors `CAIRN_WATCHDOG_MODE=1` to skip hit-incrementing and emit a distinct LOG header. Eliminates the need for users to remember `/end-session` before `/exit`.
- **v3** — Compaction-handoff system (four-touchpoint: danger-zone handoff, PreCompact linchpin, post-compact replay + embed, auto-compact calibration). ✅ Shipped. See below.

## v3 — compaction-handoff system ✅ Shipped

Catalyst: Mira's letter to Cairn flagging that lossy auto-compaction loses session texture even with auto-archive. Solution adapted to Claude Code's substrate constraints (we can't programmatically prune conversation history from a hook, but we can make compaction lossless via a four-touchpoint handoff system).

What shipped:

1. **Danger-zone directive at 85% context** — `UserPromptSubmit` detects approaching context limit (`CAIRN_COMPACT_THRESHOLD=0.85`) and injects a directive: the orchestrator writes a rich handoff to `orchestrator/transcripts/.handoff.md` (identity summary, key decisions, open threads) and runs the `/checkpoint` judgment (LOG entry, new memories). No embed at this stage.
2. **PreCompact linchpin** — fires before ANY compaction (harness-auto or manual). Archives the live transcript JSONL and appends a `## RECENT TAIL` section to `.handoff.md` from turns since the 85% rich handoff. No model call; no embed. This is the threshold-independent safety floor: continuity holds even if the context-percent override is a no-op on a given Claude Code version.
3. **Post-compact replay in SessionStart** — on `source==compact`, replays `.handoff.md` as context and kicks a detached background embed of the archived pre-compaction transcript via the same Indexer/Embedder/VectorStore path as the checkpoint embed.
4. **Settings calibration** — `autoCompactEnabled: true` + `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=92` (harness auto-compacts ~92%, above the 85% danger zone so the rich handoff always lands first). Applied by `bootstrap.sh`'s jq merge alongside hook registrations.

6. **Intra-session archive watchdog — ✅ IMPLEMENTED 2026-04-26 (split out of v3, shipped early).** Discovered empirically: the Stop hook fires per-turn-completion, NOT on session end. `/exit`, abrupt termination, or error-killed sessions lost their post-last-turn texture from auto-archive. `user_prompt_submit.py` keeps a per-session turn counter at `orchestrator/transcripts/.watchdog_state.json` and every N=10 turns (configurable via `CAIRN_WATCHDOG_THRESHOLD`) forks `session_end.py` as a detached background subprocess (`start_new_session=True` so it survives `/exit`; stdio devnull'd). `session_end.py` honors `CAIRN_WATCHDOG_MODE=1` to skip memory-hit auto-incrementing (the per-turn Stop hook already owns that) and emits a distinct `### Watchdog-archive` LOG header. The earlier `/end-session` command remains useful for explicit user-driven archives but is no longer required for routine `/exit` safety.

What v3 deliberately does NOT attempt: true programmatic sliding-window pruning. Claude Code doesn't allow modifying conversation history from a hook. The four-touchpoint handoff system makes compaction lossless without requiring that capability.

## v4 — observability and validation

- **Reflection agent** for self-audit. After a task, a lightweight agent verifies that canonicals the orchestrator claimed to apply were actually applied. Complements `/checkpoint`'s drift detection but runs automatically.
- **Memory decay visualizer** CLI. Shows which memories are near-zero weight, which are compounding, which haven't been touched in months. Helps with manual lint.
- **Cross-session consistency checks.** Compare orchestrator behavior across sessions for drift in voice, principles, or style. Catches accidental tone shifts from IDENTITY.md edits.
- **Per-project weight profiles.** Different projects might want different half-lives or emphasis. (E.g., infrastructure projects may want their security memories to never decay; rapid-prototype projects can decay older patterns faster.)

## v5 — portability and platform reach

- **Local embeddings. ✅ Shipped.** Ollama + `nomic-embed-text` (768-dim, 8K context window) is now the default. Zero API cost, full data privacy. Provider abstraction in `embedder.py`. Legacy OpenAI path still works via env var overrides.
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
