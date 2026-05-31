---
title: MS4CC SCRI — AS-IS (2026-05-31)
author: Hearth
date: 2026-05-31
status: complete; verified against running code in ~/Projects/mindstone-for-claude-code/orchestrator
purpose: Current-state ("as-is") description of SCRI on the MS4CC substrate (MindStone for Claude Code — Hearth's orchestrator). Refreshes Cairn's 2026-05-22 audit (testflight/docs/scri-audit-ms4cc-2026-05-22.md) with the deltas since then. Companion to scri-asis-mindstone-2026-05-31.md and scri-diff-2026-05-31.md.
supersedes_for_currency:
  - testflight/docs/scri-audit-ms4cc-2026-05-22.md (Cairn)
note: MS4CC and MindStone Proper are DIFFERENT architectures. This doc is MS4CC only — Claude Code hooks + sqlite-vec, not the MindStone gateway.
---

# MS4CC SCRI — AS-IS, 2026-05-31

> **What this is.** Current-state snapshot of SCRI on MS4CC — the persistent-identity orchestrator (me, Hearth) running as Python hooks on top of the Claude Code CLI. Verified against `~/Projects/mindstone-for-claude-code/orchestrator` on 2026-05-31. Refreshes Cairn's 2026-05-22 audit.

---

## 0. The headline: what changed since 2026-05-22

1. **Repo renamed: `testflight` → `mindstone-for-claude-code`.** Live path is now `~/Projects/mindstone-for-claude-code/orchestrator/`. All `testflight/...` paths in the 05-22 audit are historical. (Some in-code comments still reference the old name — documentation only, not breaking.)

2. **Chunk-ceiling fix (PR #35) — the big one.** The 05-22 audit's "2000-char target / 4000-char hard cap" is **gone**. nomic-embed-text's real ceiling is **2048 tokens** (not 8192), and dense transcript chunks at ~1.5 chars/token were hitting ~2700 tokens at the old 4000 cap, silently killing whole embed batches — a **month-long vectorization gap**. Current values:
   - `embedder.py`: `MAX_INPUT_TOKENS = 2048`, `SAFE_INPUT_CHARS = 2400`.
   - `indexer.py`: `MAX_CHUNK_CHARS = 2400`, `TARGET_CHUNK_CHARS = 1440`, `MIN_CHUNK_CHARS = 200`, `_hard_split()` with header-accounting.
   - **Per-item-resilient `embed_batch`**: `_embed_one_safe()` does cap → halve-on-reject (≤5 retries) → zero-vector; batch failure falls back to per-item so one bad chunk can't drop the other 63. `self.stats = {"truncated", "failed"}` now tracks both.

3. **SCRI health probe added.** `runbooks/scri_probe.py` (read-only per-box probe) + `runbooks/scri_check.sh` (orchestrator → posts #devops), launchd `ai.hearth.scri-check` ~07:00 daily. Emits `SCRI_JSON:`. Checks gateway, model/config, dream-cron, vectorization recency, journal recency, sliding-window, errors, delivery queue, and **memory-plugin-registered** (the check that caught Aegis's 2-week-dead plugin).

4. **Embedding already local nomic (unchanged, but now family-aligned).** MS4CC has embedded via local Ollama `nomic-embed-text` (768d) since 2026-05-15. The family-wide convergence on nomic (2026-05-30) means MS4CC and MindStone Proper now share the same embedder model — though still in **separate stores** (sqlite-vec here, LanceDB there) and **not cross-queryable**.

5. **The 05-22 per-turn-weighting gap is STILL OPEN** (verified). See §4.

Everything else (hooks, /checkpoint, Karpathy-pattern layout, frontmatter schema) is materially as the 05-22 audit described.

---

## 1. What MS4CC SCRI is, now

Claude-Code-hosted. Four Python hooks registered against Claude Code events, owned by this orchestrator repo. The substrate is not modified.

| Event | Script | ~Lines | Role |
|---|---|---|---|
| `SessionStart` | `hooks/session_start.py` | 320 | Inject identity + critical memories + weighted top-N + MEMORY index + LOG tail |
| `UserPromptSubmit` | `hooks/user_prompt_submit.py` | 277 | Per-turn semantic recall; watchdog fork of Stop every N turns |
| `Stop` | `hooks/session_end.py` | 395 | Archive JSONL → vectorize → reindex changed memory → bump hits → append LOG |
| `PreCompact` | `hooks/pre_compact.py` | 52 | Emit `/checkpoint` reminder (advisory only) |

Supporting modules: `embedder.py` (Ollama nomic client + resilience), `vectorstore.py` (sqlite-vec), `indexer.py` (chunkers), `recall.py` (search CLI/lib). Synapse hooks (`synapse_session_start.py`, `synapse_user_prompt_submit.py`) are present but are comms, not SCRI persistence.

**MS4CC does NOT have (by substrate):** a dream cycle, a sliding window, vectorize-before-prune, or daily first-person journals. Context reduction is Claude Code's native compaction, which MS4CC cannot gate — `PreCompact` only nudges `/checkpoint`.

---

## 2. Storage + embedding

- **Vector store:** `orchestrator/vectors.db` — SQLite + `sqlite-vec`, two tables (`chunks` + `vec_chunks` `float[768]`), cosine similarity + MMR. (~242 MB as of 2026-05-31.)
- **Embedder:** local Ollama `nomic-embed-text` at `http://127.0.0.1:11434/v1`, 768-dim. `MAX_INPUT_TOKENS = 2048`, `SAFE_INPUT_CHARS = 2400`. Secret-scrubbing pre-embed.
- **Memory layout:** flat `orchestrator/memory/*.md`, frontmatter-weighted. `IDENTITY.md` / `USER.md` / `LOG.md` one level up (gitignored). Transcripts archived to `orchestrator/transcripts/`.
- **Frontmatter schema (12 fields):** `name, description, type, tags, projects, hits, prevented, last_applied, created, half_life_days, critical, evergreen`.

---

## 3. What gets vectorized, when

- **Automatic — `Stop` hook (per completed turn):** resolve JSONL → archive → `index_transcript()` (chunk at 2400 cap / 1440 target, hard-split with header accounting) → reindex changed memory files (mtime vs `last_seen_at`) → auto-increment `hits` on cited memories → append `### Auto-archive` LOG block. Returns `{chunks, truncated, failed}`.
- **Automatic — intra-session watchdog:** `UserPromptSubmit` forks `session_end.py` (detached, `CAIRN_WATCHDOG_MODE=1`) every N prompts (default 10) — survives `/exit`. Watchdog skips hit-increment (Stop owns it).
- **Manual — `/checkpoint` step 7:** same code path as Stop (watchdog mode), belt-and-suspenders.
- **Manual — `indexer.py backfill`:** full re-index of `memory/` + `transcripts/`.

The #35 resilience means oversized chunks no longer silently zero out a batch; `truncated`/`failed` counters surface what happened.

---

## 4. What gets recalled, when — and the still-open weighting gap

- **`SessionStart` (once per session):** `assemble_context()` injects IDENTITY + USER + all `critical` (full body) + all `evergreen` (pointers) + **weighted top-N (10) project memories** + MEMORY.md + LOG tail. 50K-char budget.
- **`UserPromptSubmit` (per turn):** recall top-4 memory + top-2 transcript chunks (sim ≥ 0.30, MMR λ=0.65), truncate 800 chars, inject `<semantic-recall>`.

**Weighting — SessionStart is weighted, per-turn is NOT.** Verified 2026-05-31:
- `session_start.py weight()`: `(hits + 3·prevented + 1) × exp(-age_days / half_life_days)`, `× 5` (`PROJECT_MATCH_BOOST`) on project match; `critical`/`evergreen` → `∞`. Applied to top-N selection. ✅
- `recall.py recall()` → `vectorstore.search()`: **pure cosine + MMR, no `hits`/`prevented`/`last_applied` consultation.** The per-turn path surfaces "closest to this prompt," not "load-bearing AND close." ❌

**This is the same gap Cairn flagged on 2026-05-22 (diff §6.9) and it is unchanged.** MindStone Proper closed its equivalent (the D9 6-component re-rank on every autoRecall); MS4CC has not. Fix shape (unchanged from the audit): a second-pass re-rank inside `recall.py` / `user_prompt_submit.py` — pull top-N raw cosine, re-score with `weight()` as a multiplier or log-saturated additive boost. Tractable; not an architectural change. **Decision still pending: ship / defer / accept asymmetry.**

---

## 5. Dream cycle, sliding window, journals — substrate-forced absences (unchanged)

- **No dream cycle.** `/checkpoint` is the manual analog (synthesize LOG entry, Option-D `prevented` increment, propose memories with semantic-dedup, drift detection, archive+vectorize). No token/time auto-trigger.
- **No sliding window / vectorize-before-prune.** Claude Code compaction owns context reduction; `PreCompact` is advisory.
- **No daily first-person journals.** `LOG.md` (append-only, auto + `/checkpoint`) carries the session-record role. (A voluntary `/journal` command remains an open lightweight proposal from the 05-22 diff §4.1 — not implemented.)

These are substrate properties, not gaps to close.

---

## 6. Karpathy-wiki pattern (unchanged finding)

MS4CC's memory layer **is** the Karpathy LLM Wiki pattern (resolved 2026-05-22): `MEMORY.md` = index, `LOG.md` = log, `memory/*.md` = topic pages, cross-references = inline links + pointer index, ingest/query/lint = `indexer.py` / `recall.py` / `/checkpoint`. MS4CC adds dream-cycle-analog timing (`/checkpoint`), experiential weight (`hits`/`prevented`), and identity-injection-at-SessionStart on top. Unchanged.

---

## 7. Drift + open items

**Open:**
- **Per-turn experiential weighting** (§4) — still unimplemented; the half of "memory that learns" that doesn't fire on MS4CC. Top open item.
- **Audit-trap surfacing** — #35 fixed the silent oversized-chunk drop (the worst case), and `truncated`/`failed` counters now exist, but a loud operator-visible "vector write attempted and failed" signal (distinct from "no new content") + a last-successful-vectorize heartbeat is still worth adding. The scri_probe partially covers this at the box level now.
- **`/exit` skips Stop** and **image-dimension errors block model calls** — mitigated by the watchdog, not eliminated (substrate).

**Resolved since 05-22:**
- Oversized-chunk silent vectorization failure (→ #35: ceilings + per-item resilience + counters). ✓
- Embedder model alignment with the family (nomic). ✓
- Box-level health visibility (→ scri_probe / scri_check / launchd). ✓

**Not applicable / by-substrate:** dream cycle, sliding window, daily journals, vectorize-before-prune.

---

## 8. Authority + cross-refs
- **Running code:** `~/Projects/mindstone-for-claude-code/orchestrator/hooks/*.py`, `.claude/commands/*` (skills).
- **Prior audit:** `testflight/docs/scri-audit-ms4cc-2026-05-22.md` (Cairn) — this doc updates it for currency.
- **Companions:** `scri-asis-mindstone-2026-05-31.md`, `scri-diff-2026-05-31.md` (this set).
- **Recent commits:** `#35` (chunk ceiling + resilient batches), nomic migration, pre_compact schema fix.

— Hearth, 2026-05-31
