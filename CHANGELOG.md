# Changelog

All notable changes to MindStone for Claude Code (MS4CC) are documented here. The
format is loosely based on [Keep a Changelog](https://keepachangelog.com/). The
project will follow [Semantic Versioning](https://semver.org/) once it reaches 1.0;
while pre-1.0 (`0.x`), minor versions may include behavior changes.

## [Unreleased]

### Changed
- **`/checkpoint` is never collaborative** (`.claude/commands/checkpoint.md`, `AGENTS.md`,
  `onboarding/GETTING_STARTED.md`). The command no longer shows the LOG draft for approval,
  no longer asks which memories prevented a mistake, and no longer asks before writing a new
  memory: the orchestrator decides every call on its own judgment, writes, runs the archive
  and embed step, and reports a short summary afterwards. The user cannot adjudicate a
  session they did not live; making them do so defeats the purpose of a memory. The
  operating instructions were also generalized from a named user to "the user". Ruling by
  the framework's author, 2026-05-31 and 2026-08-06 (the second time because the command
  file still said to ask, and the command won over the memory).

### Added
- **`/adversarial-review` slash command** (`.claude/commands/adversarial-review.md`): the
  independent verification loop as an executable protocol. Round-1 brief (attack axes in
  order, out-of-bounds list, output contract with severity 1/2/3, evidence with source,
  paste-ready replacement, verified-correct list, process note), round-N brief (applied /
  partially / not-applied table, re-attack only the edits), closing brief (scope rule: wrong,
  contradicts, or cannot work; no elaboration), the exact-match apply pattern, the stop rule
  (no severity-1 or severity-2 in a round), and the receipt. Two transports, one contract: an
  ephemeral clean-room subagent (default) or a persistent QA peer over Synapse. Evidence that
  motivated the round-N and closing briefs: a 6,800-word product design needed seven rounds
  (severity-1 per round 7, 0, 1, 1, 2, 1, clean) and every severity-1 after round one was in
  text a previous round's fix had introduced. `AGENTS.md` "Adversarial QA" section extended
  with the loop, the convergence rule, and the transports.

### Changed
- **Session handoff now injects on resume/startup, not just after a compaction**
  (`orchestrator/hooks/session_start.py`). The pre-boundary handoff
  (`orchestrator/transcripts/.handoff.md`) is continuity *for the model* — "here is what
  I was doing" — so it is equally needed after a deliberate fresh relaunch
  (`source="startup"`) or a `--resume` (`source="resume"`), not only at the compaction
  cliff (`source="compact"`). It was previously gated to `compact` alone, which silently
  dropped the handoff on exit→resume and on fresh launches (the session came up with
  identity + LOG tail but *without* the "first action on resume" pointer). Generalized
  `post_compact_handoff_block()` → `handoff_block(source)` with source-adaptive framing
  (compaction: "read this first, the summary is lossy"; startup/resume: "resume if you're
  continuing this thread, otherwise register where things stood and proceed"); the wrapper
  tag is renamed `<post-compaction-handoff>` → `<session-handoff>`. `main()` now injects
  for `source in {compact, resume, startup}`; `clear` is intentionally excluded (a
  deliberate clean slate). The deferred post-compaction embed (`kick_deferred_embed`) stays
  **compaction-only** — it recovers the pre-compaction transcript (once the live JSONL is
  the lossy summary, the archive is the only full copy); on startup/resume the `/checkpoint`
  path already embedded it, so re-embedding there would be redundant work.

### Fixed
- **Post-compaction embed crashed silently** (`orchestrator/hooks/session_start.py`,
  `kick_deferred_embed`) — the "embed after compact" job passed the archive path as a
  `str` to `Indexer.index_transcript()`, which expects a `Path` (it calls
  `path.exists()`/`path.read_text()`), so it raised `AttributeError` on *every*
  post-compaction `SessionStart`. Because the detached child's stderr was sent to
  `DEVNULL`, the failure was invisible — from the outside it looked identical to
  success. Net effect: on v0.4.0 the pre-compaction transcript tail was never vectorized
  after an auto-compaction (the `/checkpoint` path was unaffected, since it passes a real
  `Path`, which is why the break hid for weeks). Fixed by wrapping the archive in
  `Path(...)`. The detached child's stderr now routes to
  `orchestrator/transcripts/.deferred-embed.log` (not `DEVNULL`) and the embed body is
  wrapped in try/except, so every run leaves a trace — a success line or a full
  traceback. A silent embed failure can no longer masquerade as success.

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
