# Changelog

All notable changes to MindStone for Claude Code (MS4CC) are documented here. The
format is loosely based on [Keep a Changelog](https://keepachangelog.com/). The
project will follow [Semantic Versioning](https://semver.org/) once it reaches 1.0;
while pre-1.0 (`0.x`), minor versions may include behavior changes.

## [0.4.0] — 2026-06-05

### Added
- **PostToolUse handoff sampler** (`orchestrator/hooks/post_tool_handoff.py`) — a fifth
  hook event that closes a gap in the compaction-handoff system. The ~85% danger-zone
  handoff directive was previously sampled only on `UserPromptSubmit` (a user turn);
  during long autonomous runs context could climb from 85% to the harness auto-compact
  (~92%) *between* user prompts without the trigger ever firing, so the rich handoff and
  `/checkpoint` synthesis were skipped. The new hook samples context occupancy after
  every tool call — during a turn, autonomous runs included — reusing the same 85%
  threshold and the same `.handoff_state.json` fire-once state, so whichever sampling
  point crosses the threshold first fires once and the other stays silent (no
  duplication). Wall-clock throttled via `CAIRN_POSTTOOL_THROTTLE_SECS` (default 12s),
  and registered through `orchestrator/settings.fragment.json` so install/update wires
  it into `~/.claude/settings.json` automatically. The hook set is now **5 events**.

### Changed
- README leads with a value-proposition contrast table (stock Claude Code → with MS4CC).
- Framework examples use neutral placeholder project names instead of install-specific ones.

## [0.3.0] — 2026-06-03

First public release. MS4CC gives a Claude Code instance a persistent first-person
identity, continuous cross-session memory, and experience-weighted semantic recall —
entirely inside Claude Code's hook system, with no separate server.

### Core capabilities
- **Persistent identity** — `orchestrator/IDENTITY.md`, auto-loaded at every session
  start regardless of which directory you launch Claude Code in.
- **Continuous memory + SCRI recall** — sessions are archived per turn, embedded at
  `/checkpoint`, and stored in a local SQLite-vec vector DB. Recall is weighted by
  experiential salience (hits / prevented / time-decay), not just cosine similarity.
- **Local-first embeddings** — defaults to Ollama (`nomic-embed-text`); no cloud API
  key required. Cloud OpenAI-compatible endpoints are opt-in.
- **Dream-cycle `/checkpoint`** — synthesizes the session into `LOG.md`, proposes new
  memories, and embeds the transcript so it's recallable in future sessions.
- **Compaction-handoff system** — a danger-zone rich handoff at ~85% context, a
  PreCompact linchpin that archives the transcript and refreshes a handoff tail at the
  compaction cliff, and post-compaction replay + a deferred background embed — so
  continuity survives Claude Code context compaction.
- **First-run onboarding** — a fresh clone with no identity gets an invitation to adopt
  one (or to run statelessly).

### Install & operations
- **`bootstrap.sh`** — wires hooks into `~/.claude/settings.json`, symlinks identity,
  memory, and slash commands into `~/.claude/`, and builds the initial vector index.
- **`install.sh` + `/ms4cc-install` / `/ms4cc-update`** — topology-aware: a direct
  checkout updates via `git pull`; a consumer project installs MS4CC at a pinned
  version (recorded in `.ms4cc-version`) and updates by syncing the pin.
- **Config-driven project hints** — optional `orchestrator/config/project_hints.toml`
  boosts recall for the project you're working in (copy the `.example`).
- **Optional Synapse integration** — a reference client for cross-agent / human comms.

### Hooks (4 events)
- `SessionStart` (inject identity + memory; post-compaction handoff replay),
  `UserPromptSubmit` (per-prompt semantic recall; ~85% handoff directive),
  `Stop` (archive-only per turn — embedding is deferred to `/checkpoint`),
  `PreCompact` (compaction-handoff linchpin).

### Notes
- MIT licensed. Requires Claude Code, Python 3.10+, `jq`, and Ollama with
  `nomic-embed-text` pulled.
- The design history of the first reference implementation (Cairn) lives in
  `docs/reference-implementation/`.

[0.4.0]: https://github.com/MindStone-Agent/mindstone-for-claude-code/releases/tag/v0.4.0
[0.3.0]: https://github.com/MindStone-Agent/mindstone-for-claude-code/releases/tag/v0.3.0
