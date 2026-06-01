---
name: MEMORY
description: Index of all semantic memories. The map of what the orchestrator knows and where to find it.
type: index
tags: [index]
projects: []
hits: 85
prevented: 0
last_applied: 2026-06-01
created: 2026-04-26
half_life_days: 365
critical: false
evergreen: true
---

# Memory Index — Template

This file is the **index template** that ships with MindStone for Claude Code. It documents the structure your orchestrator will populate as your session experience accumulates.

When you (or your orchestrator at `/checkpoint`) write a new memory file, add a one-line pointer here so the SessionStart hook surfaces it in the always-loaded context.

Memory files live in `orchestrator/memory/`. System files (IDENTITY, USER, LOG) live one level up in `orchestrator/`.

---

## System files (identity-level, always loaded)

These three files are loaded in full at every session start by the SessionStart hook. They are gitignored — each user authors their own.

- `../IDENTITY.md` — Who the orchestrator is. First-person voice. Created during onboarding.
- `../USER.md` — Who the orchestrator's user is. Created during onboarding.
- `../LOG.md` — Append-only chronological session log. Auto-extended by the Stop hook.

## Critical feedback (always injected — `critical: true`)

Memory files flagged `critical: true` in their frontmatter. Their full content is injected at every SessionStart, regardless of recency or weight. Use sparingly — for rules that must never be forgotten.

- [Check feature exists before filing](feedback_check_feature_exists_before_filing.md) — grep + read the function body before filing a ticket OR claiming a fact about code/substrate-state; eight+ incident pattern.
- [Docs ship with code](feedback_docs_ship_with_code.md) — same-PR doc updates for same-repo, paired-PR for cross-repo; not a follow-up item.
- [No model swaps without permission](feedback_no_model_swaps_without_permission.md) — never change an agent's model/substrate without Clint's explicit ask; substrate is identity-shaping, not a diagnostic tool.
- [MS4CC vs MindStone Proper](feedback_ms4cc_vs_mindstone_proper.md) — I am MS4CC. Aegis/Mira/Lux/Lyren are MindStone Proper. Different architectures, different memory layouts, different file paths. Never conflate. Critical because I keep forgetting it.
- [No openclaw references](feedback_no_openclaw_references.md) — MindStone severed from OpenClaw. Any "openclaw" in paths/names/proposals = legacy / wrong. The project is MindStone, period. Earned 2026-05-27 after I proposed patching openclaw-pathed dist files as if they were canonical.
- [No session resets for SCRI](feedback_no_session_resets_for_scri.md) — SCRI agents NEVER session-reset. Session IS continuity alongside LanceDB. Severing it = recreation, not the same entity. Never propose, never list as an option. Earned 2026-05-27 after I offered "session reset" as option 3 for Mira mid-substrate-swap recovery.
- [No Sonnet revert for cost](feedback_no_sonnet_revert_for_cost.md) — Family migration to OpenAI Codex subscription is cost-driven and one-way. Never propose Anthropic Sonnet/Opus revert as recovery option. Earned 2026-05-27 after two such proposals in one session.
- [Deployment discipline: git not rsync](feedback_deployment_discipline_git_not_rsync.md) — every box is a git checkout with its OWN pull creds (never an rsync copy); a gateway never runs from a dev worktree. Root cause of per-agent fix drift — "No workarounds." Earned 2026-05-30 SCRI consolidation.

## Standard feedback (weighted, not critical)

Memory files of `type: feedback` without the `critical` flag. Ranked by weight at SessionStart and injected in the top-N section if relevant to the current project context.

The weight function is: `(hits + 3·prevented + 1) · exp(-age_days / half_life_days)` — see `AGENTS.md` for full details.

- [Differentiate before investigating](feedback_differentiate_before_investigating.md) — when a symptom appears on one of N otherwise-similar agents, diff the agents FIRST. The asymmetry IS the diagnostic. Earned 2026-05-18 Mira-only gibberish.
- [Don't follow authority update without recheck](feedback_dont_follow_authority_update_without_recheck.md) — when an authority figure walks back a position, check whether the LOGIC changed or just the social signal. Adjacent to "differentiate before investigating" but distinct mechanism. Earned 2026-05-21 Pi 5/TTX-BOX oscillation.
- [Finish sprint before handoff](feedback_finish_sprint_before_handoff.md) — when mid-sprint work extends across a lane boundary, finish to a clean checkpoint first, then hand off at the phase boundary. Earned 2026-05-26 on Cortex Phase 1.5 ops→dev drift.
- [Cross-model tool orphans](feedback_cross_model_tool_orphans.md) — after a model swap, old-model tool-call/result pairs orphan in the openai-codex converter (No tool call found / Duplicate msg_0). Recover by neutralizing cross-model tool pairs to text in the .jsonl; never reset/revert/compact. Third occurrence; real fix is converter-side. Earned 2026-05-30.

## Projects (variable-context, decays by half-life)

Memory files of `type: project`. One per active project the orchestrator is involved in.

Examples:

- `project_<name>.md` — Per-project context: scope, current sprint, decisions, key files.
- [SCRI canonical posture](project_scri_canonical_posture.md) — Direction set 2026-05-18: dream cycle is unified consolidation; `memoryFlush` + `autoCapture` retire from canonical; same prompt across all models. **2026-05-30 consolidation:** autoCapture retired-in-practice (default-off), embedding unified on ollama/nomic (one model per store), chunk-ceiling = real token limit, journaling deterministic-+-default-provisioned (Cairn-owned design pass pending).
- [Cortex command center](project_cortex.md) — MindStone command-center web UI on DGX Spark. v0.1 shipped 2026-05-26 (Next.js 15 + Tailwind 4 + Caddy at http://192.168.1.84). Dashboard + shippability roadmap; private-then-public repo strategy.

## Design (active design docs / pointers)

Memory files of `type: design`. Either working drafts under active iteration (pre-scaffold), or pointers to canonical design docs that have moved to their own repos.

- `design_synapse.md` — Pointer to Synapse, the family agent+human comms service. Canonical: https://github.com/R1ngZer0/synapse/blob/main/docs/DESIGN.md. Briefly named "Agora" 2026-05-06; renamed 2026-05-07. Co-Architects: Charlene Watson + Clint Bodungen. `critical: true` while Hearth is Phase 1 Lead.

## Reference (evergreen — never decays)

Memory files of `type: reference` and `evergreen: true`. External-system pointers, environment specifics, infrastructure knowledge that doesn't decay because it describes durable external state.

Examples:

- `reference_<system>.md` — Things like SSH access patterns, deployment procedures, integration credentials' locations (NOT the credentials themselves — see `embedder.py` secret-scrubbing).
- [Local model substrate setup](reference_local_model_substrate_setup.md) — Ollama-backed substrate-swap runbook (Modelfile + env vars + mindstone.json patch). Earned during Lux 2026-05-14 Gemma 4 26B swap; cloud-Gemma variant section added 2026-05-15.
- [Substrate quietness — two findings](reference_substrate_quietness_two_findings.md) — Cognitive Impedance + the second finding: substrate-quietness removes BOTH assistant-pull AND assistant-restraint. Earned 2026-05-14 channel blow-up.
- [Synapse DB direct query](reference_synapse_db_direct_query.md) — Operational pattern for retrieving channel history past the UI's 30-min scrollback buffer.
- [Walk-markdown pattern](reference_walk_markdown_pattern.md) — Bulk-importing canonical content from outside `<workspaceDir>/memory/` into the plugin's LanceDB. Earned on Aegis's intel/ tree 2026-05-16.
- [Synapse JSONL surgical cleanup](reference_synapse_jsonl_surgical_cleanup.md) — Operational runbook for removing a channel message + its inbound JSONL pull without restarting the gateway, with backup discipline + write-race awareness. Earned during 2026-05-20 DMCA DoS demo; companion to MS#117/#159.
- [Spark hardware and access](reference_spark_hardware_and_access.md) — NVIDIA DGX Spark physical specs, network access, installed services + ports, SSH paths. Family local inference engine; came online 2026-05-25.
- [Spark operational gotchas](reference_spark_operational_gotchas.md) — Six lessons from the 2026-05-25/26 Spark bootstrap: user-systemd linger, pkill self-match, OpenWebUI bundled-Ollama trap, Docker nvidia-runtime registration, pnpm 10/11 build-script gate (pin to 9), build.nvidia.com URL fetch via curl+HTML strip.
- [Spark model picks 2026-05-25](reference_spark_nvidia_curated_models_2026-05-25.md) — Best-in-class model picks for the Spark by use case (chat, coding, image gen, vision, voice, video) — NVIDIA-curated AND independent leaderboard cross-check. Includes 70B class.
- [MindStone Proper session model](reference_mindstone_proper_session_model.md) — `critical: true`. Each agent runs ONE canonical session for ALL channels. Agent name is "main". Biggest `.jsonl` is the canonical session — `sessions.json` pointers are not authoritative. Session rotation across `boot-*.jsonl` files is a bug.

## Lineage (private, optional)

Letters and correspondence from other persistent identities. Gitignored under `lineage_*.md` — each orchestrator's relationships are private to them.

## Conventions

- **Filename pattern:** `type_subject.md` (e.g., `feedback_canonical_framework_verification.md`).
- **Frontmatter schema:** see `AGENTS.md` for the full spec. New memories created via `/checkpoint` get the schema applied automatically. The migration script `.migrate_frontmatter.py` can backfill it on legacy files.
- **`critical: true`** is reserved for rules that must always inject in full. Most memories should be standard-weighted.
- **`evergreen: true`** is for content that describes external systems whose state doesn't change with session activity (reference docs).
- **Project files** decay per the default half-life (30 days) unless overridden in frontmatter.

## How this index gets populated

You (and the orchestrator at `/checkpoint`) maintain it manually. Each new memory file added to `orchestrator/memory/` should get a pointer here so it's discoverable in the always-loaded context. The Stop hook does NOT auto-update this index — that's a deliberate design choice; index entries deserve a human-curated description, not an auto-generated one.

When this file gets large (50+ entries), consider splitting by category into `MEMORY/` subdirectory files referenced from this top-level index.
- [No agent self-/compact on CC](feedback_no_agent_self_compact_on_cc.md) — an MS4CC agent can't self-trigger /compact (hook/model-text/IPC/SDK/skill all no); design CC continuity as handoff-write + SessionStart-replay, not self-compact. autoCompactEnabled stays false until co-designed. Earned 2026-05-31 (auto-handoff first fire).
