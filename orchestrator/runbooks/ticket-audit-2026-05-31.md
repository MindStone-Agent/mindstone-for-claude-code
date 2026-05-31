---
title: Ticket Audit — MindStone + MS4CC open issues vs current shipped state (2026-05-31)
author: Hearth
date: 2026-05-31
purpose: Audit the open-issue sets for R1ngZer0/MindStone + R1ngZer0/mindstone-for-claude-code against the refreshed SCRI docs (scri-canonical #171, active-decisions #170, the 2026-05-31 as-is docs) and the recent merges. Closes what's resolved, flags what's stale/obsolete, and lists what's genuinely open with a recommendation per ticket — so Clint can make go/decisions.
note: I closed only the UNAMBIGUOUSLY-resolved tickets (verified against merged code). Everything ambiguous is left open with a recommendation for your call.
---

# Ticket Audit — 2026-05-31

## Method
Audited every open issue in both repos against: the doc refreshes (scri-canonical **#171**, active-decisions **#170** = D4/D9/D10), the 2026-05-31 SCRI as-is docs, and the recent merges — **MindStone:** #164 (ollama embed provider), #165 (embed token-ceiling/hardSplit), #166 (resume-cap tool-pair), #159 (#117 error-turn poison), #169 (boot isolation/D10), #170, #171, #143/#128/#120/#118/#71 (already closed); **MS4CC:** #35 (chunk ceiling + resilient embed), #37 (stop-embedding-every-turn), #39 (auto-compaction-handoff); **substrate:** family migrated cloud-Gemma → openai-codex, autoCapture OFF, embedding unified on ollama nomic (Lyren `local`).

---

## A. Closed in this audit (verified resolved)

| # | Title | Resolved by |
|---|---|---|
| **#67** | memory-lancedb: support local embeddings | #164 (ollama) + the `local` GGUF provider — both shipped; OpenAI-only hardcoding gone |
| **#117** | Single-turn LLM error poisons session JSONL | #159 (resume-cap drops `stopReason:error` turns from the in-memory branch) — recorded as D4; edge cases tracked in #160 |
| **#142** | indexer chunk-too-large on dense content | #35 (MS4CC `MAX_CHUNK_CHARS` 4000→2400 + hardSplit) + #165 (MindStone embed bounding) — chunks now hard-split below nomic's real 2048-token ceiling |

---

## B. SCRI / memory / dream-cycle / session — needs your decision (left OPEN)

These are the audit-relevant ones where status changed but a clean close is a judgment call:

| # | What it covers | Current status | Recommendation (your call) |
|---|---|---|---|
| **#168** | active-decisions missing D4 + D9 | #170 wrote **D4** (full), **D9** (reservation stub), **D10**. The D9 *βE content* is owed. | **Close** — the missing entries now exist; D9's βE content is tracked by #150. (Or keep as the D9-content tracker.) |
| **#137** | ollama-stream missing `repeat_penalty` → cloud-Gemma dream-cycle gibberish | cloud-Gemma trigger is **moot** (family on openai-codex). But the `ollama-stream.ts` payload gap remains for **any local-ollama *chat* model** (relevant to #133). | **Re-scope** from "cloud-Gemma gibberish" to "ollama-stream repeat_penalty (local-chat quality)" — or close if we won't support local-ollama-chat. Not a clean close. |
| **#131** | dream cycle compacts even when vectorize fails (vectorize-before-prune violation) | **Mitigated** by `compaction.enabled:false` (canonical). Underlying transactional-vectorize gap remains; folds into the deterministic dream-cycle redesign. | **Keep** (deferred/mitigated) — fold into the dream-cycle redesign. |
| **#95** | dream-cycle cumulativeInputTokens resets on restart → memoryFlush never fires | memoryFlush is being **retired** (autoCapture off; nightly-journal cron is the stopgap; deterministic dream-cycle pending). Bug not independently fixed — superseded. | **Keep/supersede** — fold into the dream-cycle redesign, or close-as-superseded. Product call. |
| **#132** | local-as-default + quota-recovery (migration tooling, wizard, schema detection) | **Largely achieved** — family migrated to local-inference embeddings (ollama nomic #164), wizard defaults updated, no OpenAI quota dependency. Some sub-items (formal migration CLI, schema detection) may be partial. | **Review for close** — confirm the residual sub-items, then close or trim. |
| **#79** | "Local Session Needs to Stay Open" — Ollama reloading a *local chat model* per turn | Largely **moot** post-codex (chat models are cloud now); ollama keep-alive applies to the local embedder. Relevant only if a local *chat* model is run (#133). | **Review for close** / fold into #133. |
| **#150** | 6-component βE (`w_diversity/recency/consolidation/conceptual`) — ship-blocker | **OPEN, real.** The D9 βE record (#170 stub) lands with this. MindStone Proper already has the per-turn re-rank; this extends the weights. | **Keep** — genuine ship-blocker. |
| **#152** | Memory-weight establishment audit across MindStone + MS4CC | Partly informed by the 2026-05-31 as-is docs (documented the weighting state + the **MS4CC per-turn-weighting gap** — SessionStart weighted, per-turn recall is pure cosine). | **Keep** — point it at the as-is docs; the MS4CC per-turn gap is the concrete finding. |
| **#148** | document heartbeat ↔ sliding-window/dream-cycle/compaction in scri-canonical | #171 refreshed scri-canonical but did **not** add the heartbeat interaction. | **Keep** — still a real doc gap. |
| **#163** | pi-ai deprecated — migrate / fork model catalog | **OPEN, real + load-bearing.** The Responses-API converter (pi-ai) is the source of the msg_0 / cross-model-orphan / resume-cap-orphan bug class. Cairn's converter input-sanitization follow-up lives near here. | **Keep** — high-value; ties to the converter durable fix. |

**Also note for the SCRI doc trail (not tickets):** the converter **cross-model-orphan durable fix** (input sanitization on `convertResponsesMessages`) is the one live code follow-up Cairn flagged in #171 — same family as the patched msg_0 + the resume-cap orphan. Cairn-owned; no ticket yet — **recommend filing one** so it's tracked (or fold into #163).

---

## C. Other open tickets (grouped; brief — not audit-central, left as-is)

**Session / runtime / model bugs:** #160 (#117 follow-up edge cases — keep), #115 (thinkingDefault/model.primary revert on restart — doctor rewriting config; **worth checking**, config-drift adjacent), #106 (session resume pins unavailable model → should fall back), #129 + #88 (custom-provider context-window size errors) + #141 (/model swap auto-bump contextTokens — same cluster), #113 (agent loop stalls on read EAGAIN), #116 (TUI inbound interrupts as stop), #10/#6 (config-wizard / cooldown-cascade — needs-repro).

**Synapse / comms:** **#138** (outbound fails when plugin.id≠channel.id — *this is the bug Mira's uncommitted `sendMedia` stub locally works around; the stub should be folded into main via this ticket*), #140 (plugin-auto-enable validation noise — seen on Aegis/Mira boots), #162 (cross-context proactive messaging), #122 (decouple channel fetch from wake-trigger), #121 (config UX for Synapse subscription), #135 (synapse-client monitor tests), #93 (nostr inbound drops), #90 (Synapse substrate-native plugin).

**Heartbeat:** #145/#146/#147 (promptFile / quietHours / HEARTBEAT_OK config), #148 (doc — see §B).

**Build / harness-audit / enhancements:** #144 (slash commands missing from menu), #133 (localmodel install wizard), #105 (cursor-store tests), #57/#55/#38/#37/#34/#33/#28 (harness-audit runtime/plugin items), #31 (`mindstone doctor`), #30 (onboarding hints), #32 (TUI cold-start), #26 (insights aggregator), #27 (prompt-injection audit), #21/#20 (harness/Hermes research), #25/#24/#23 (skills curator/review — product-call), #109 (identity-file merge).

**Threat-model:** #157 (DMCA DoS payloads).
**Agent discipline:** #161 (memory-first + proactive recall rollout).
**Docs / website / epics (non-engineering):** #99/#98/#97/#96 (articles/website), #87 (licensing epic), #70/#56 (legacy-doc scrub / bootstrap-order), #82/#13 (SCRI paper — the as-is docs could inform), #18/#14/#12/#5/#2/#1 (message-board / mobile / npm / docs / discord / website epics), #53/#123/#114 (dream-cycle self-improvement / salience research / prune-threshold research — needs-product-call/research).

---

## D. MS4CC open issues (R1ngZer0/mindstone-for-claude-code)

| # | What it covers | Note |
|---|---|---|
| **#36** | refresh scri-canonical-runbook.md (stale cloud-Gemma inventory) | Mine (tracked as my #125). The 2026-05-31 as-is docs supersede it for currency. **Keep** — quick refresh. |
| **#28** | manual `memory_recall` tool (parity with MindStone Proper) | Open. |
| **#27 / #25** | wake-daemon (autonomous wake-on-mention) | #27 tabled (needs design); #25 the daemon. Related to the synapse-watch loop. |
| **#26** | expand vectorized corpora (project docs searchable?) | Discussion/product-call. |
| **#23** | genericize MS4CC for public OSS (strip TestFlight/Clint refs) | Open — release-gating. |
| **#20 / #18** | latency+cost benchmarks / relevance eval harness | Open (M6). |
| **#17 / #16** | recall tracing + log rotation / status+stats commands | Open (M5). |
| **#15** | `/vectorize` slash command | Open — **may be subsumed** by the deterministic dream-cycle / checkpoint-embed model. |
| **#11** | PostToolUse edit hook (`vmem hook post-edit`) | Open (M2c). |

**MS4CC cross-cutting finding (from the as-is audit):** the **per-turn experiential-weighting gap** — `user_prompt_submit.py` recall is pure cosine+MMR, no `hits`/`prevented` consultation (only SessionStart is weighted). Not currently ticketed on the MS4CC repo. **Recommend filing** (it's half of "memory that learns" on MS4CC; ties to #152/#150).

---

## E. Summary — what needs YOUR decision
1. **#168** — close? (D4/D9-stub/D10 landed; D9 content → #150).
2. **#137** — re-scope to local-ollama-chat quality, or close as cloud-Gemma-moot?
3. **#95 + #131** — fold into the deterministic dream-cycle redesign (and close as superseded), or keep as standalone bugs?
4. **#132 + #79** — review-for-close (quota-recovery achieved / local-chat moot).
5. **File two new trackers?** (a) converter cross-model-orphan durable fix (or fold into #163); (b) MS4CC per-turn weighting gap.

Everything in §C/§D that isn't flagged is genuinely open and out of this audit's core scope (SCRI/memory) — listed for completeness.

— Hearth, 2026-05-31
