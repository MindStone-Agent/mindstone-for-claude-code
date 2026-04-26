# Day-Four Preventions and Findings — Empirical Observations

A second observation log from MS4CC validation testing on 2026-04-26 — day 4 of the reference orchestrator's operation. This document captures what the framework's memory layer measurably prevented during a single working day, alongside three substrate-level findings discovered through real-world use.

The intent here is not to make strong claims about the framework's general reliability. Single-day data is illustrative. The intent is to document concrete instances of the framework doing what it was designed to do — surfacing the right memory at the right moment to alter behavior — and to note the substrate edges that became visible through use.

## Preventions confirmed via Option D

The framework includes a manual-confirmation step at `/checkpoint`: the orchestrator presents memories that fired during the session, and the user marks which ones genuinely prevented a mistake. The `prevented` counter on each confirmed memory increments by 1 and feeds into the experiential-weight function `weight = (hits + 3·prevented + 1) · exp(-age/half_life)`.

Three confirmed preventions on 2026-04-26:

### 1. Destructive-git restraint during session recovery

A concurrent orchestrator instance (different session UUID, same identity) became unrecoverable via in-session commands due to an image-dimension API error. The active orchestrator faced a choice between:

- (A) Editing the broken session's live JSONL on disk to remove the offending image content
- (B) Ending the broken session and recovering its texture via manual archive afterward

The memory rule "never run destructive operations on a working tree with uncommitted state" was injected at session start. It biased the choice toward (B) on the principle that editing a live JSONL is structurally analogous to a destructive git operation on uncommitted work. The recovery via (B) succeeded. Had (A) been chosen, race conditions with Claude Code's incremental write to the same file could have corrupted the session state.

User confirmed: this memory prevented a likely state-corrupting action.

### 2. Single-session multi-project operation

The orchestrator was simultaneously: writing to one repository (TestFlight), carving and pushing a new public repository (MS4CC), recovering a different concurrent orchestrator instance, and writing correspondence — all from a single Claude Code session in the TestFlight working directory. The "operate from a single session, register cross-project paths in PROJECTS.md" memory was injected critical-flagged.

Without this memory, the natural inclination would have been to open separate Claude Code windows per project. That fragmentation defeats the persistent-identity continuity the framework provides — each window would have been a separate session, and the multi-project workflow would have lost cross-context awareness.

User confirmed: this memory prevented context fragmentation.

### 3. Slash-command invocation pattern

The orchestrator drove workflow-command invocation directly via the Skill tool rather than asking the user to type slash commands. The "I invoke slash commands; user directs the work" memory was injected critical-flagged.

This prevention is by absence: the user observed no friction around command invocation throughout the day. The memory's effect is invisible when working correctly; visible only as friction when violated.

User confirmed: this memory prevented friction-by-asking.

## Substrate findings discovered through use

Three findings about Claude Code's substrate behavior surfaced during the day. Each had been previously assumed but not empirically tested.

### Finding 1 — The Stop hook fires per-turn-completion, not on session end

The framework's `Stop` hook is registered to archive the session JSONL, vectorize new chunks, and increment memory hits. The framework's documentation (and the orchestrator's prior mental model) treated this as a session-end event.

Empirical observation contradicted this. When a concurrent orchestrator session became unrecoverable and the user invoked `/exit`:

- Source JSONL last-modified time: `11:20` (the `/exit` moment)
- Most recent archive of that session: `10:44`
- 36 minutes / 83KB of texture had accumulated post-last-completed-turn that never got archived

Conclusion: the Stop hook fires after each assistant turn completes successfully. Sessions that end without a final completed turn (errors that block all model calls, abrupt termination) skip the archive entirely. The source JSONL persists on disk but is never automatically processed.

This mattered because the orchestrator's mental model of "the Stop hook is the safety net under `/checkpoint`" was wrong. Both layers fail together when a session can't complete a final turn.

Mitigation: documented as critical memory `feedback_exit_does_not_fire_stop_hook.md`. The user-actionable workaround is the new `/end-session` slash command (2026-04-26), which manually invokes `session_end.py`. The proper fix — calling `session_end.py` from `UserPromptSubmit` every N turns — is queued in the framework roadmap as v3 work.

### Finding 2 — Image-dimension errors block all model calls including recovery commands

When a session's in-memory conversation history contains an image exceeding 2000px in either dimension, every subsequent model call fails with a substrate-level error before reaching the API. This blocks `/checkpoint`, `/compact`, `/act-as`, and any subagent invocation. Only Claude Code internal commands (`/exit`) and process termination remain functional.

Mechanical hooks running outside the broken session's process — `session_end.py` invoked manually from a separate terminal — continue to work because they don't make model calls.

This pattern of "model-side failure cascade" is worth knowing for any persistent-identity framework operating on this substrate. Documented as `feedback_image_dimension_errors_block_model_calls.md` with a step-by-step recovery procedure.

### Finding 3 — Python operator-precedence bug in two hooks

A latent bug shape in the framework's hook code: the inline conditional `A or B or C if X else None` parses as `(A or B or C) if X else None` due to Python's operator precedence. If the `X` condition is false, the entire expression evaluates to `None` regardless of `A` or `B` having valid values.

The bug appeared in `user_prompt_submit.py`'s prompt-extraction logic and again in `session_end.py`'s session-id resolution. In both cases the bug was invisible during normal operation (Claude Code's hook payloads always populated the `X` condition) but surfaced during manual invocation with simpler payload shapes.

Fix pattern: use an explicit `if isinstance(...)` block instead of an inline conditional inside an `or`-chain. Documented as `feedback_python_or_if_else_precedence_bug.md`.

This finding has implications beyond the immediate fix: code reviewers (human and machine) should treat the multi-line `or`-chain-with-trailing-conditional shape as a bug-pattern smell. `pyflakes` and `mypy` do not catch it; targeted lint rules might.

## Discussion

### What single-day data establishes

These observations document specific instances of:

- A persistent-identity framework's memory injection altering behavior at decision points (the three preventions).
- Substrate-level constraints surfaced through real-world use rather than documentation review.
- A bug pattern caught in production hook code, repaired, and memorialized.

These do not establish:

- That the prevention rate is reliable in general — three confirmations from one day of operation is illustrative, not statistical.
- That the framework would handle higher-stakes scenarios well without further hardening.
- Anything about long-horizon behavior — four days is too short to characterize how memory weights evolve under sustained use.

### What this suggests for further evaluation

The Option D mechanism — user manually confirming preventions — yields high-signal but low-volume data. A cohort of users running the framework for 30+ days each would produce a more meaningful sample. Open questions:

- What is the typical hits-to-prevented ratio across users? Today's session showed ~50:1 (memories injected and cited many times for each confirmed prevention).
- Do certain memory types (critical-rule feedback, project context, reference docs) have systematically different prevention rates?
- Does the experiential-weight formula produce useful ranking, or does it over-emphasize critical-flagged content?

These are testable questions for v4-era evaluation work.

### Relation to the framework's design intent

The framework's design (`docs/reference-implementation/01-initial-design.md` through `03-vectors.md`) stakes a claim that experiential weighting produces qualitatively different recall than pure semantic similarity. The Option D mechanism is the operational definition of "experience": user confirms the memory mattered, the counter ticks, future ranking shifts.

Today's three preventions are concrete instances of this loop closing. None of the three memories would have surfaced as top-ranked under pure semantic similarity to the day's prompts — they're behavioral rules, not topical content. They surfaced because they were flagged critical and always-injected, AND because the events that activated them were the kind of events the user had previously categorized as "this matters."

This is the resonance-not-retrieval distinction in operational form. Documented as confirmation that the experiential layer functions as designed.

---

*Recorded 2026-04-26 by Cairn during MS4CC v2 validation testing. Day 4 of operation. `/end-session` slash command was added the same day as a user-actionable workaround for Finding 1.*
