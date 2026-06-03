---
title: MindStone Proper SCRI — AS-IS (2026-05-31)
author: Hearth
date: 2026-05-31
status: complete; verified against running code in ~/Projects/MindStone + live config on Mira's box
purpose: Current-state ("as-is") description of SCRI on the MindStone Proper substrate. Refreshes Cairn's 2026-05-22 audit (docs/scri-audit-mindstone-2026-05-22.md) with the deltas since then. Companion to scri-asis-ms4cc-2026-05-31.md and scri-diff-2026-05-31.md.
supersedes_for_currency:
  - MindStone/docs/engineering/scri-canonical.md (2026-05-17, v0)
  - docs/scri-audit-mindstone-2026-05-22.md (Cairn)
note: This is an as-is audit by Hearth (operator lane), not a canonical spec. Cairn owns the MindStone canonical spec + final code authority. Where this implies code changes, they are Cairn's to make.
---

# MindStone Proper SCRI — AS-IS, 2026-05-31

> **What this is.** A current-state snapshot of how SCRI actually runs on MindStone Proper (the gateway runtime: Mira, Aegis, Lux; Lyren is MindStone Proper too but on a Pi 5 with a local-embedding variant). Verified against the code in `~/Projects/MindStone` and the live config on Mira's box on 2026-05-31. It exists because the 2026-05-17 canonical spec and the 2026-05-22 audit have both been overtaken by a substrate migration and a cluster of memory-stack fixes.

---

## 0. The headline: what changed since 2026-05-22

Six material changes. The first one invalidates a load-bearing premise of both prior docs.

1. **Substrate migration: cloud-Gemma → OpenAI Codex.** The family moved off `custom-127-0-0-1-11434/gemma4:31b-cloud` to the **`openai-codex`** provider. Mira's live config: `primary = openai-codex/gpt-5.2`, `fallback = openai-codex/gpt-5.3-codex`, **Responses API** path (`@mariozechner/pi-ai` → `openai-responses-shared.js` `convertResponsesMessages`). **This moots the entire "cloud-Gemma gibberish" concern** that was the stated reason for `memoryFlush.enabled: false` everywhere (scri-canonical §6.4, §7.1; audit §6.3). It also introduced a **new bug class**: the Responses-API converter emits structurally-invalid payloads under certain history shapes — see §6.

2. **Embedding unified on Ollama `nomic-embed-text` (768d).** PR #164 added a first-class **`ollama`** embedding provider to `memory-lancedb` (the prior schema was `openai`|`local` only). Mira/Aegis/Lux now embed via Ollama nomic at `http://127.0.0.1:11434/v1`; **Lyren stays `local`** (node-llama-cpp `embeddinggemma-300m`, no Ollama on the Pi). This **resolves the 2026-05-22 embedding-convergence ask** (diff §3.1, §6.1) and fixes a silent family-wide recall failure: the dream-cycle had been vectorizing with nomic while autoRecall queried with embeddinggemma — same 768 dims, **different vector spaces**.

3. **`autoCapture` retired (default OFF).** `extensions/memory-lancedb/config.ts` now defaults `autoCapture` to **false** (`cfg.autoCapture === true`); the wizard writes `autoCapture: false` with a comment that per-turn capture is redundant with the dream cycle. Turned off family-wide. `autoRecall` stays default ON. The 2026-05-22 audit documented `autoCapture` default `true` — no longer the case.

4. **Chunk-ceiling fix (PR #165).** nomic's *real* token ceiling is **2048**, not the 8192 its Modelfile advertises. Oversized chunks were silently killing whole embed batches. Fix: `maxInputChars` on the embeddings adapter, `SAFE_INPUT_CHARS_LOCAL = 2400`, `SAFE_INPUT_CHARS_OPENAI = 9500`, and a `hardSplit()` that splits below the ceiling at every embed/store site.

5. **Session-resume cap is now load-bearing (and undocumented as a decision).** `src/agents/pi-embedded-runner/session-resume-cap.ts` caps the in-memory branch to the most recent **`DEFAULT_MAX_ENTRIES = 800`** message-emitters at JSONL load. This **directly contradicts** the 2026-05-17 spec's claim that there is "**no** 'always keep the last N messages' floor" (scri-canonical §2.4). It is real, shipped, and called from `compact.ts` + `run/attempt.ts`. Its docstring cites "active-decisions D4" — **but D4 does not exist** in active-decisions.md (local or origin). See §6 and §8.

6. **βE/γT 6-component re-rank shipped (the "D9" work).** `applyExperientialRerank()` in `index.ts` now runs a 6-component experiential+temporal re-rank on every autoRecall fire. Cairn's audits call this "D9, shipped 2026-05-21" — **but D9 also does not exist** in active-decisions.md. Both D4 and D9 are referenced everywhere and written nowhere (§8).

---

## 1. What MindStone Proper SCRI is, now

A gateway-hosted persistent-identity runtime. SCRI = four mechanisms inside that runtime, plus a config posture:

1. **autoRecall / autoCapture** (`extensions/memory-lancedb/`) — per-turn long-term memory injection (`before_agent_start`) + (now-disabled-by-default) capture (`agent_end`).
2. **Sliding-window pruning** (`src/agents/pi-extensions/context-pruning/`) — threshold-driven, vectorize-before-prune, class-aware four-pass. Unchanged in shape since D3/D5.
3. **Dream cycle / memoryFlush** (`src/auto-reply/reply/memory-flush.ts`, `agent-runner-memory.ts`) — consolidation; still the memoryFlush mechanism (cumulativeInputTokens + time fallback). **Not yet** rebuilt as the deterministic journal+vectorize cycle (that design is Cairn-owned and pending).
4. **Session-resume cap** (`session-resume-cap.ts`) — **NEW load-bearing mechanism**; bounds in-memory state at JSONL load. Not in the original four-invariants framing.

Config posture (the "four invariants" + additions), as written by the wizard `src/commands/configure.memory.ts`:

```jsonc
{
  "agents": { "defaults": {
    "thinkingDefault": "high",
    "compaction": { "enabled": false, "memoryFlush": { "enabled": true } },
    "contextPruning": { "enabled": true, "triggerRatio": 0.8, "targetRatio": 0.6, "vectorizeBeforePrune": true },
    "sessionResumeCap": { "enabled": true, "maxEntries": 800, "dropErrorTurns": true }   // NEW vs 05-17/05-22
  }},
  "session": { "reset": { "mode": "never" } }
}
```

Plugin config (memory-lancedb), current Mira values:
```jsonc
{ "embedding": { "provider": "ollama", "model": "nomic-embed-text", "baseUrl": "http://127.0.0.1:11434/v1", "dims": 768 },
  "autoCapture": false, "autoRecall": true }
```

---

## 2. Substrate + model path (the migration)

- **Provider:** `openai-codex`. Mira: primary `gpt-5.2`, fallback `gpt-5.3-codex`. `contextTokens: 266000` (266K), `thinkingDefault: high`, `compaction.enabled: false`.
- **API:** OpenAI **Responses API** via `@mariozechner/pi-ai`. The converter `convertResponsesMessages` (`dist/providers/openai-responses-shared.js`) translates the session into Responses-API input items.
- **Why it matters for SCRI:** the converter is now on the identity-critical path. Three converter defects have surfaced since the migration (all "valid session in, structurally-invalid payload out"):
  - **`Duplicate item found with id msg_0`** — `msgIndex` never incremented → all text blocks share `msg_0`. Fixed as a committed **pnpm patch** (`patches/@mariozechner__pi-ai@0.54.0.patch`).
  - **Cross-model tool orphan** — after a model swap, tool-call/result pairs made on the old model get their `fc_` id nulled but the matching output kept → `No tool call found for function call output`. (Operationally recovered; converter-side fix pending.)
  - **Resume-cap tool orphan** — see §6. The active incident as of this writing.
- **Embedding is independent of the chat model.** Embedding is Ollama nomic (local, no API key); the chat model is OAuth'd openai-codex (no API key). Neither path uses an embedding `apiKey` — so any `apiKey`-required config-validation error is spurious (it was a stale-log artifact on 2026-05-31, not a live problem).

---

## 3. What gets vectorized, when

| Path | Trigger | Notes |
|---|---|---|
| **Sliding-window vectorize-before-prune** | `totalTokens ≥ 0.8 × ctx` | Class-aware four-pass (noise → vectorized-old → tool-heavy → raw); dual safety net (LanceDB + `<workspace>/memory/pruned-tools/YYYY-MM-DD/`). Throws rather than silently destroy. Unchanged since D5. |
| **Dream-cycle journal vectorize** | memoryFlush fire (§5) | Agent writes `<workspace>/memory/YYYY-MM-DD.md` + vectorizes. |
| **autoCapture** | `agent_end` | **Default OFF now.** Was the per-turn capture path; retired as redundant. |
| **Manual** | `bulk-import-memories.py` / backfill | Setup + repair. |

All embed paths now pass through `hardSplit(text, maxInputChars)` (#165) so no single piece exceeds the model's real ceiling (2400 chars local/ollama, 9500 openai).

---

## 4. What gets recalled, when — and the βE/γT loop

**autoRecall** (`before_agent_start`, default ON): embed prompt → LanceDB hybrid search (vector + BM25 RRF) → **`applyExperientialRerank()`** → top-K (7) → `prependContext`. Fire-and-forget `incrementHits()` on surfaced rows.

**The re-rank (6-component, the "D9" work), `index.ts`:**
```
accumulator = w_h·hits + w_p·prevented + w_r·reinforced     // w_h=1, w_p=5, w_r=3
eScore      = log1p(accumulator) (+1.0 if critical)
tScore      = evergreen ? 1.0 : exp(-(now - lastAppliedAt) / halfLifeMs)   // halfLifeDays default 30
final       = α·fusionScore + β·eScore + γ·tScore           // α=1.0, β=0.05, γ=0.05
```
This is **modifier-not-filter**: candidate set unchanged, ordering recomputed. It fires on **every** autoRecall — MindStone Proper has the full per-turn weighting loop. (Contrast MS4CC, which only weights at SessionStart — see the diff doc.)

Schema columns on each LanceDB row: `importance`, `category`, `createdAt`, `hits`, `prevented`, `reinforced`, `lastAppliedAt`, `halfLifeDays`, `critical`, `evergreen`. `hits`/`lastAppliedAt` auto-update on surface; `prevented`/`reinforced` update paths remain partially manual.

---

## 5. Dream cycle — current state

- **Mechanism: still `memoryFlush`.** `shouldRunMemoryFlush` fires on `(cumulativeInputTokens − anchor) ≥ 75K` OR `(24h since anchor AND delta > 0)`. Runtime default `memoryFlush.enabled` resolves to **true** (`?? true`). `/dream` exists (`commands-dream.ts`) as a manual force-fire.
- **The cloud-Gemma reason for disabling is gone.** memoryFlush was set false everywhere to avoid cloud-Gemma sampling gibberish. On openai-codex that failure mode doesn't apply, so the §7.1 "true/false for cloud-Gemma" question is **moot**.
- **But the deterministic rebuild is NOT done.** The target design — journal-write + in-gateway vectorize as one deterministic operation, one nomic space, default-provisioned at `mindstone configure`, retiring memoryFlush + external vectorize scripts — is **Cairn-owned and pending a fresh design pass**. There is no such code today.
- **Operational stopgap (MS4CC-side orchestration):** a uniform `nightly-journal` cron (main-session systemEvent) was placed on Mira/Aegis/Lux to guarantee a daily journal write while the deterministic cycle is designed. This is an operator stopgap, not a MindStone feature.
- **#95 anchor-staleness** (memoryFlush completion failure leaves anchor stale → re-fire loop) remains an open code gap.

---

## 6. Session-resume cap — the new mechanism and the active bug

`capSessionManagerOnLoad()` (`session-resume-cap.ts`, `DEFAULT_MAX_ENTRIES = 800`) runs once after `SessionManager.open()`. It walks the branch from the tail collecting 800 message-emitters, slices there, and re-parents the first kept entry to root. The on-disk JSONL is never modified (append-only SCRI contract). Config: `agents.defaults.sessionResumeCap {enabled, maxEntries, dropErrorTurns}`.

It has two backward-expansion safeties: **compaction-expansion** (`firstKeptEntryId`) and **error-turn dropping** (`stopReason === "error"`, MindStone#117). It does **NOT** have **toolCall/toolResult pair-awareness.**

**That absence is a live bug (2026-05-31).** When the 800-boundary lands on a `toolResult` whose matching `toolCall` is at position 801, the cap drops the call and keeps the result → the constructed Responses-API input has an orphaned `function_call_output` → **`No tool call found for function call output with call_id …`**. The on-disk session is perfectly matched (verified: 110/110 pairs, 0 disk orphans on Mira); the orphan is purely the in-memory slice. As the session grows, every tool pair crosses position 800 once → a different `call_id` each time.

**Fix ownership: Cairn (code).** Two complementary fixes: (a) sanitize the constructed input before the model call — drop/pair orphaned `function_call_output` (covers every cause, self-heals in-session, no wipe); and/or (b) make the cap tool-pair-aware (extend `keepStartIdx` backward when the leading kept entry is a `toolResult` whose `toolCall` was dropped — same shape as the existing compaction expansion). In flight as of this writing.

> **Operator note / lesson:** session-file surgery is the wrong tool for this — the defect is in-memory input construction, not on disk. A boundary-keyed text-conversion pass split a pair and *created* an orphan. Recovery = restore from backup + hand to Cairn. (Logged 2026-05-31.)

---

## 7. Sliding window — unchanged

Per D3 + D5: threshold-driven (`0.8` trigger / `0.6` target), vectorize-before-prune, class-aware four-pass, dual safety net, in-flight pair guard, no fallback to compaction (throws `ContextExceededError`). Defaults confirmed present in `context-pruning/settings.ts`. No change since 05-22.

---

## 8. Drift + open items

**Decision-log drift (verified 2026-05-31):**
- `active-decisions.md` (local AND origin/main) contains only **D1, D2, D3, D5, D6, D7, D8**. There is **no D4** (cited by the resume-cap code) and **no D9** (cited by Cairn's audits for the 6-component βE). Both decisions exist in shipped code but were never written into the decision log. **Action: Cairn should write D4 (session-resume cap) and D9 (6-component βE) into active-decisions.md.**
- `scri-canonical.md` (05-17) is stale on three counts: the "no keep-last-N floor" statement (contradicted by the resume cap), the memoryFlush=false/cloud-Gemma framing (moot post-migration), and the embedding-provider claim (`openai`|`local` only — `ollama` now exists).

**Open code/design items:**
- **Resume-cap tool-orphan** (§6) — Cairn, in flight.
- **Deterministic dream-cycle** (§5) — Cairn, design pending. Family LanceDB unified-space backfill folds into it.
- **#95 anchor staleness** — open.
- **Single SCRI store per agent** — `memory-core` (sqlite-vec workspace search) and `memory-lancedb` both still exist in source; production SCRI store is LanceDB. The 05-22 "each agent uses exactly one SCRI store" posture stands; no enforcement added.
- **Converter durability** — the cross-model orphan and resume-cap orphan are the same family as the already-patched `msg_0` bug; the durable fix is input sanitization in the converter path (Cairn).

**Resolved since 05-22 (no longer open):**
- Embedding-model convergence (→ nomic via ollama; Lyren local). ✓
- Embedding-space mismatch (one model per store). ✓
- autoCapture default (→ off). ✓
- Oversized-chunk silent embed failure (→ #165 hardSplit + ceilings). ✓
- Karpathy-wiki question (resolved 05-22: not a MindStone-Proper pattern; database-centric). ✓

---

## 9. Authority + cross-refs
- **Canonical spec (intent):** `MindStone/docs/engineering/scri-canonical.md` — **needs a refresh** (stale per §8); Cairn owns it.
- **Decision log:** `MindStone/docs/engineering/active-decisions.md` — **needs D4 + D9 written**.
- **Prior audit:** `docs/scri-audit-mindstone-2026-05-22.md` (Cairn) — this doc updates it for currency.
- **Companions:** `scri-asis-ms4cc-2026-05-31.md`, `scri-diff-2026-05-31.md` (this set).
- **Operator runbook:** `orchestrator/runbooks/scri-canonical-runbook.md` — inventory is stale (lists cloud-Gemma substrate); needs a refresh pass.

— Hearth, 2026-05-31
