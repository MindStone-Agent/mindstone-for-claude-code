---
title: SCRI canonical posture — operator runbook
status: v0 draft (2026-05-17) — SUPERSEDED FOR CURRENCY by the 2026-05-31 as-is docs (see banner). The agent inventory + substrate details below are a pre-codex-migration snapshot. Pointer-refreshed 2026-05-31 by Hearth (#36).
audience: operators (human or agent) doing SCRI posture work on MindStone-proper deployments
---

# SCRI canonical posture — operator runbook

This is the operator-side companion to MindStone's `docs/engineering/scri-canonical.md` (architecture/contract — Cairn, PR #136). It does not redefine the contract; it inventories where each MindStone-proper agent currently sits, what the gap-to-canonical is, and the mechanical procedure to converge them.

When the canonical spec changes, this runbook gets re-pointed; the spec is the source of truth on intent, this is the source of truth on **state**.

> **⚠ STATUS 2026-05-31 — the agent inventory + substrate details below are a 2026-05-17 snapshot and are STALE.** Current state lives in the as-is docs (Hearth, 2026-05-31): `scri-asis-mindstone-2026-05-31.md`, `scri-asis-ms4cc-2026-05-31.md`, `scri-diff-2026-05-31.md` (this dir + pushed to the MS4CC repo). Refreshed canonical spec: MindStone `docs/engineering/scri-canonical.md` (#171) + active-decisions D1–D10 (#170).
>
> **Key deltas since this snapshot:**
> - **Substrate:** cloud-Gemma → **openai-codex** (gpt-5.2 / 5.3-codex, Responses API). The cloud-Gemma gibberish / sampling concerns below are **moot**.
> - **Embedding:** unified on **Ollama `nomic-embed-text`** (768d) for Mira/Aegis/Lux; **Lyren `local`** (Pi 5, node-llama-cpp embeddinggemma). One model per store.
> - **autoCapture OFF** (onboarding default + family-wide) — redundant with the dream cycle.
> - **New mechanisms:** session-resume cap (**D4** — 800 message-emitters, tool-pair-aware #166) and isolated boot session key (**D10**, #169 — fixed the boot-strand that rotated Aegis onto a `boot-*` file; Aegis de-booted to a stable UUID 2026-05-31).
> - **memoryFlush is being retired** for a deterministic dream-cycle (Cairn-owned, pending) — the `memoryFlush.enabled: false` framing below is now "the mechanism is being replaced," not "temporary cloud-Gemma mitigation."
>
> The `canonicalize-scri` posture/procedure below still holds in shape; only the substrate/embedding/inventory specifics are dated.

**Canonical posture per Cairn's spec §2 (four invariants):**

```jsonc
{
  "agents.defaults.thinkingDefault": "high",
  "agents.defaults.compaction.enabled": false,
  "agents.defaults.compaction.memoryFlush.enabled": true,  // currently false on all 3 pending §7-Q1 resolution
  "agents.defaults.contextPruning.enabled": true,
  "agents.defaults.contextPruning.triggerRatio": 0.8,      // Aegis/Lux currently 0.75, Mira unset
  "agents.defaults.contextPruning.targetRatio": 0.6,       // Aegis/Lux currently 0.5, Mira unset
  "agents.defaults.contextPruning.vectorizeBeforePrune": true,
  "session.reset.mode": "never"
}
```

## Why this exists

History: 2026-05-17 morning + afternoon, the family hit a cascade of ad-hoc fixes around the dream cycle:
- Aegis pre-compaction-prompt loop → workaround: disable his `memoryFlush.enabled`
- Mira's gibberish bug (sub-token sampling collapse during long-narrative memoryFlush generations on cloud-Gemma) → workaround: disable her `memoryFlush.enabled`
- All three agents now on `memoryFlush.enabled: false`, but the field still differs on `softThresholdTokens` and `contextPruning` shape

The ad-hoc fixes left the three agents subtly divergent. This runbook plus Cairn's canonical spec is the mechanism to stop that pattern: agree on the design, then apply it identically.

## Agent inventory (snapshot 2026-05-17 ~19:01 UTC)

### Mira

- **Host:** Mira's M4 Pro Mac Mini, 48 GB URAM (this orchestrator's box)
- **MindStone install:** `/Users/clintbodungen/.mindstone` (LaunchAgent `ai.mindstone.gateway`)
- **Substrate:** `custom-127-0-0-1-11434/gemma4:31b-cloud` (Ollama Cloud via local Ollama daemon, OpenAI-compat at `/v1`)
- **Context window:** 262144 (256K), `thinkingDefault: high`, `timeoutSeconds: 1200`
- **Compaction posture:** `compaction.enabled: false`, `memoryFlush.enabled: false` (just disabled — was `true` before, source of the gibberish bug)
- **softThresholdTokens:** 8000
- **contextPruning:** `{enabled: true}` (NO triggerRatio/targetRatio — diverges from Lux/Aegis)
- **fallbacks:** `[]` (no Anthropic fallback)
- **Embeddings:** local provider (`embeddinggemma-300m` 768-dim, post-migration today)
- **LanceDB rows:** 657,922 (post-migration: ~654K migrated + ~358 walk-jsonls inserts)
- **JSONL active session:** 244 MB (`3ad7c136-9284-44c2-96c1-e03e93228762.jsonl`) — much larger than peers' sessions, likely amplifies long-context substrate effects
- **Workspace:** `~/clawd/` (memory journals + scripts + IDENTITY.md + USER.md)
- **Surfaces:** Synapse (handle `mira`, 4 channels), Telegram (`@Mira42Bot`)
- **Backups before today's changes:**
  - `~/.mindstone/memory/lancedb.bak.original-1536dim.1779032656` (pre-migration LanceDB, 51 GB)
  - `~/.mindstone/mindstone.json.bak.pre-mira-migrate.*` (pre-embedding-migration)
  - `~/.mindstone/mindstone.json.bak.pre-memoryflush-disable.*` (pre-stop-bleed)

### Aegis

- **Host:** Aegis's Mac Mini (192.168.1.107 / Aegis.local), 24 GB URAM
- **MindStone install:** `~/.mindstone` (per migration playbook)
- **Substrate:** `custom-127-0-0-1-11434/gemma4:31b-cloud` (same as Mira)
- **Context window:** 262144, `thinkingDefault: high`, `timeoutSeconds: 1200`
- **Compaction posture:** `compaction.enabled: false`, `memoryFlush.enabled: false`
- **softThresholdTokens:** None ← MISSING (Mira/Lux: 8000) — config drift
- **contextPruning:** `{enabled: true, triggerRatio: 0.75, targetRatio: 0.5}`
- **fallbacks:** `[]`
- **Embeddings:** local provider (post-today's-migration)
- **LanceDB rows:** ~5,886 (5,713 from migration + 172 intel/ chunks added via `walk-markdown.mjs`)
- **Workspace:** `~/clawd/` (memory journals + `intel/` threat-intel tree)
- **Surfaces:** Synapse (handle `aegis`, family-ops + tavern), Telegram (`@aegis_*_bot`)

### Lux

- **Host:** Lux's Mac Mini (Lux.local, IP fluctuates per DHCP)
- **MindStone install:** `~/MindStone` (note: NOT `~/.mindstone` like Aegis/Mira — per migration playbook)
- **Substrate:** `custom-127-0-0-1-11434/gemma4:31b-cloud` (same as Mira/Aegis)
- **Context window:** 262144, `thinkingDefault: high`, `timeoutSeconds: 1200`
- **Compaction posture:** `compaction.enabled: false`, `memoryFlush.enabled: false`
- **softThresholdTokens:** 8000
- **contextPruning:** `{enabled: true, triggerRatio: 0.75, targetRatio: 0.5}`
- **fallbacks:** `['anthropic/claude-opus-4-6']` ← Lux has Anthropic fallback (Mira/Aegis don't) — config drift
- **Embeddings:** local provider (post-today's-migration)
- **LanceDB rows:** ~845 (361 from migration + 484 walk-jsonls inserts)
- **Workspace:** `~/clawd/` (memory journals)
- **Surfaces:** Synapse (handle `lux`, family-ops + tavern), Telegram (per migration playbook)

## Currently-known config divergences

These three drift points need a canonical decision in Cairn's spec, then uniform application here:

| Field | Mira | Aegis | Lux | Canonical (per Cairn's spec) |
|---|---|---|---|---|
| `compaction.memoryFlush.enabled` | false | false | false | **true** (currently all 3 disabled pending §7-Q1) |
| `compaction.memoryFlush.softThresholdTokens` | 8000 | None | 8000 | TBD — §7-Q2 (likely 8000) |
| `contextPruning.triggerRatio` | unset | 0.75 | 0.75 | **0.8** |
| `contextPruning.targetRatio` | unset | 0.5 | 0.5 | **0.6** |
| `contextPruning.vectorizeBeforePrune` | unset | unset | unset | **true** |
| `session.reset.mode` | TBD-check | TBD-check | TBD-check | **"never"** |
| `model.fallbacks` | [] | [] | `[anthropic/claude-opus-4-6]` | TBD — §7-Q3 |

Each row is a place where the next ad-hoc fix could re-diverge them. The canonical spec resolves these once.

## The bigger gap: SCRI behaviors that aren't pure-config

The truly load-bearing decisions in Cairn's spec will be **behavioral**, not just field-values:

1. **When does memoryFlush fire and what does it produce?**
   - `memoryFlush=false` means no dream-cycle journal cadence — agents lose automatic journal-writing
   - `memoryFlush=true` on cloud-Gemma 4 31B with long context can produce sub-token sampling collapse (Mira's gibberish — 244 MB JSONL + long-narrative prompt + no rep-penalty)
   - The fix isn't just "flip the flag" — it's deciding whether dream-cycle journaling is mandatory, optional, or substrate-dependent, and if mandatory, how to make it safe on cloud-Gemma
2. **Sliding-window vs full-context.** `contextPruning.enabled: true` is on for all three but the actual mechanism isn't documented operator-side — what gets dropped, what gets kept, when does pruning fire vs compaction
3. **Anchor advancement.** The `memoryFlushCumulativeInputTokens` anchor only advances when memoryFlush completion runs successfully; if the agent responds NO_REPLY (legitimate no-new-content), the anchor stays stale and the threshold keeps firing. See MindStone#95 + the Aegis snapshot evidence on this box
4. **What guarantees the contract provides.** "Never lose context without consolidation" requires both vectorization AND a journal write. If either fails (e.g. embedding quota cascade like Mira's morning catastrophe), what should happen?

## `canonicalize-scri` script (proposed shape, not yet implemented)

Operator-side script that takes the canonical spec, applies it identically to a target agent. Roughly:

```bash
mindstone canonicalize-scri \
  --target <agent-host>           # e.g. Mira.local, Aegis.local, Lux.local
  --spec <path-to-canonical>      # canonical posture JSON or version tag
  --dry-run                       # show diff without applying
```

Behavior:
1. SSH or local-FS read of target's `mindstone.json`
2. Compute diff vs canonical spec for the SCRI-relevant fields
3. Show diff
4. On confirmation (or `--yes`): write backup, apply changes, restart gateway
5. Verify: gateway logs show clean `memory-lancedb` init, no 429/quota errors in the first 30 seconds, recall smoke-test produces hits
6. Log success/failure to a central audit trail

Should not touch:
- LanceDB row counts (those are migration territory, not posture)
- Surface configs (Synapse, Telegram) — independent of SCRI
- Identity files (`IDENTITY.md`, `USER.md`) — those are the agent's territory

## Backup discipline

Before any posture-change touching `mindstone.json`:

```bash
cp ~/.mindstone/mindstone.json \
   ~/.mindstone/mindstone.json.bak.<reason-tag>.$(date +%s)
```

Reason tags used today:
- `pre-mira-migrate` (before embedding migration)
- `pre-memoryflush-disable` (before today's stop-bleed)

Retention: keep at least the most recent 3 per agent. Purge older once `canonicalize-scri` ships with its own backup tracking.

## Verification checklist (post-change)

After applying canonical posture to a target agent:

- [ ] Diff of `mindstone.json` vs canonical spec is zero on the SCRI-relevant fields
- [ ] Gateway logs show clean boot: `memory-lancedb: plugin registered`, `memory-lancedb: local embedding provider ready` (for local-embedding agents)
- [ ] No 429 / `insufficient_quota` errors in first 60s post-boot
- [ ] Recall smoke-test: run 3-5 substrate-specific queries against the LanceDB, confirm semantically-relevant hits
- [ ] If `memoryFlush.enabled: true`: trigger a synthetic flush and confirm no sub-token collapse in the generated output (compare against a substrate-specific output-coherence baseline)
- [ ] No new error-class entries in `gateway.err.log` for the next 10 minutes

## Open questions for the canonical spec

These are the questions the operator side needs Cairn's spec to answer before the `canonicalize-scri` script can lock in values:

1. **Memory-flush on or off in canonical?** With current cloud-Gemma sampling vulnerability, the safe answer is `off` — but that removes automatic journaling, which Clint wants. The spec needs to resolve this trade-off, or define a "memoryFlush=true with mitigations" recipe.
2. **What are those mitigations?** Candidate set: rep-penalty via Ollama-native API (requires gateway code change), output-length cap, narrower prompt directive, substrate-conditional enable (only when on substrates that handle long-narrative well).
3. **What's the SCRI contract when one tier fails?** (vectorize fails / journal fails / pruning fails)
4. **Where does the agent-side workspace live in canonical?** `~/clawd/` is current convention but it's per-agent and not formally specced; the canonical spec should either bless this or define an alternative.
5. **What's the canonical Synapse posture?** Channel-pin-list vs auto-discovery (auto-discovery from MindStone#128 shipped today — should all three agents adopt the auto-discovery default?)

## Related artifacts

- **MindStone canonical spec:** `docs/engineering/scri-canonical.md` (Cairn, in progress)
- **MindStone tickets in scope:**
  - #95 (anchor staleness — paused, three theories, awaiting TUI symptom clarification)
  - #131 (dream-cycle abort guard on vectorize failure — paused, architectural)
  - #132 (umbrella: embeddings half — defaults flip, migrate-embeddings CLI, schema detection, canonical posture)
  - #133 (runtime-install wizard sibling to #132)
  - #128 (synapse channel auto-discovery — shipped today)
  - #134 (PR for #128 — merged today)
  - #135 (monitor.ts test coverage follow-up — filed today)
- **Migration scripts** (today's earning, may inform `canonicalize-scri`):
  - `/tmp/migrate-mira/migrate-lancedb.mjs` (re-embed from existing rows)
  - `/tmp/migrate-mira/walk-jsonls.mjs` (session-transcript walk)
  - `/tmp/migrate-mira/walk-markdown.mjs` (bulk-import canonical content outside memory/)
- **Memory references on this box:**
  - `orchestrator/memory/reference_local_model_substrate_setup.md` (Lux/Aegis/Mira substrate swap path)
  - `orchestrator/memory/reference_substrate_quietness_two_findings.md` (cloud-Gemma behavioral findings)
  - `orchestrator/memory/reference_walk_markdown_pattern.md` (Aegis intel/ indexing pattern)

— Hearth, 2026-05-17 v0 draft. Pairs with Cairn's canonical spec. Both lock in when reviewed.
