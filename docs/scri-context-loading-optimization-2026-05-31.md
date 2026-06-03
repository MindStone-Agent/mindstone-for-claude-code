---
title: "SCRI context-loading optimization — dedup the load, never-cap the index, generate a RULES.md constitution"
author: Cairn
date: 2026-05-31
status: design note — spec first, implement on Clint's go
purpose: Fix the MS4CC always-loaded context path. Today a 50KB head-cut silently drops ~half the critical safety rules + the entire memory index + LOG tail; absolute rules reach me only via semantic recall. Spec a three-part fix (dedup, never-cap index, generated RULES.md constitution) grounded in measured sizes. MS4CC-framework-wide (Cairn + Hearth); ties to dream-cycle consolidation.
---

# SCRI context-loading optimization

> **Scope.** MS4CC (Claude Code + orchestrator hooks). Two loaders feed my always-on context: (1) Claude Code **native auto-memory** (loads `CLAUDE.md` + `MEMORY.md`, hard-caps MEMORY.md at ~24.4KB), and (2) the orchestrator's **`session_start.py`** hook (`TOKEN_BUDGET_CHARS = 50000` head-cut). This note fixes both. Framework-wide — Hearth runs the same hooks.

---

## 1. Root finding — the always-loaded path is lossy, in the worst possible way

Measured 2026-05-31:

| Fact | Value | Consequence |
|---|---|---|
| `critical: true` memory files | **29 files** | all flagged `weight() = Infinity` ("always inject up to budget") |
| Combined size of those files | **194.5 KB** | ~4× the hook's entire budget |
| `session_start.py` budget | **50 KB** head-cut (`joined[:50000]`) | injects IDENTITY + USER + criticals **in alphabetical order until 50KB**, then `[…truncated…]` — drops the rest silently |
| MEMORY index append | line 253, **after** the 194KB of criticals | **never reached** within 50KB — the hook never delivers the index |
| LOG tail append | after the index | **never reached** either |
| Native MEMORY.md cap | ~24.4 KB; actual file **40 KB** | native truncated it **this session** ("39.3KB... Only part of it was loaded") |

**What actually reaches me each session, today:** IDENTITY + USER + roughly the alphabetically-first half of the criticals — and from the native side, CLAUDE.md + a head-truncated MEMORY.md. **Cut:** ~14 criticals (everything alphabetically after the ~50KB mark — including `feedback_never_destructive_git`, `feedback_never_push_main_without_permission`, `feedback_no_fsmonitor`, `feedback_show_the_green_run_not_indirect_proof`, `feedback_stop_guessing`, `feedback_mindstone_no_compaction`), the hook's entire MEMORY index, and the LOG tail.

**Why this is the worst failure shape:** the dropped rules are the *damage-prevention* ones (don't destroy uncommitted work, don't push prod-main, show the green run), and they're dropped by **alphabetical accident**, silently. They reach me only if `user_prompt_submit.py`'s semantic recall happens to surface them for a given prompt — i.e. the rules that exist to prevent irreversible harm are **recall-only**, exactly the rules that most need to be **deterministic**. A blunt head-cut is also the wrong *strategy*: it cuts the index (the map of what I can recall) before the LOG tail before the late-alphabet rules, with no notion of importance.

The cause is a category error: **194KB of long-form files are miscast as "always full-inject."** Each of those files is mostly *essay* (the why, the history, the how-to) wrapped around a *rule* that's 1–3 lines. You can't full-inject 29 essays in any sane budget — and you shouldn't. Separate the rule from the essay.

---

## 2. Three-part fix

### Part A — Dedup the MEMORY.md load (one owner)

Both loaders carry the index. Native loads it (capped/truncated); the hook *tries* to (line 253) but never reaches it. Pick **one owner**, complete and untruncated:

- **Recommended: native owns the index; hook drops its index append; slim MEMORY.md under the native cap.** MEMORY.md is 40KB against a 24.4KB cap — over by 16KB purely because index entries are multi-line paragraphs (the native warning literally says "index entries are too long... keep to one line under ~200 chars"). Rewrite every entry as a true one-liner → lands ~18–22KB → native loads it **whole, untruncated**, and the hook stops double-loading it. One action solves dedup *and* never-cap (§Part B) for the index.
- **Alternative: hook owns the index uncapped; suppress native's MEMORY.md load.** More control (the hook is ours), but requires verifying native auto-memory can be suppressed (open Q — D-3). Keep as fallback if native truncation proves unreliable even under the cap.

Either way: **the index is loaded once, in full, by exactly one owner.**

### Part B — Never-cap the index

Principle: **the map of what exists must never be truncated.** A cut index is unknown-unknowns — I can't even know to recall a memory I can't see listed. So:

- The index is **outside any truncation budget.** Under Part A's recommended path this is free (slimmed MEMORY.md < native cap = never truncated). If the hook ever owns the index, load it **first**, before the budget-bounded weighted section, and exclude its bytes from the head-cut.
- Restructure `session_start.py` so the head-cut can never eat the index or the LOG tail: load order becomes RULES.md (§Part C) → index → LOG tail → *then* budget-bounded weighted memories. The guillotine only ever falls on the *lowest-value* tail (weighted project memories), never on the map or the rules.

### Part C — Generated always-loaded `RULES.md` constitution

Replace the impossible 194KB full-inject with a small, always-loadable **digest of the absolute rules**:

1. **Tier the rules.** Add `tier: absolute` frontmatter to the true damage-prevention memories *only* (err small — §3). `critical: true` is currently overloaded (29 files: design docs, workflow conventions, references, and actual safety rules all share the flag). `tier: absolute` is the narrow subset whose violation causes irreversible damage or breaks a hard user directive.
2. **Generate the digest at `/checkpoint`** (the consolidation step). The generator reads every `tier: absolute` memory and emits its one-line imperative into `orchestrator/RULES.md`. Source of truth stays the memory files; RULES.md is *derived*, so it can't drift. Convention: each `tier: absolute` memory carries a `rule:` frontmatter one-liner (the imperative, stripped of essay); RULES.md = those lines, grouped, ~2–4KB total.
3. **Always-load RULES.md, in full**, ahead of everything (§Part B order): every session (`session_start`), on every post-compaction replay (`session_start` `source==compact`), and — because it's tiny and these are the irreversible-harm rules — **per-turn** via `user_prompt_submit` for the absolute core (D-4). A few KB/turn is the right price for rules that prevent destroying work or deploying prod.
4. **The full files stay on disk + indexed** for semantic recall (the *why*, the *how-to*, the history). RULES.md is the **belt** (deterministic, always present); recall is the **suspenders** (depth on demand). Today there's only suspenders — that's the fragility.

After this, the 29-file/194KB always-inject is gone. What's *always* loaded: IDENTITY + USER + RULES.md (~3KB) + the full index + LOG tail. What's *budget-bounded*: weighted project memories (the genuinely trimmable tail). The 50KB head-cut stops being a safety hazard because nothing safety-critical lives in its blast radius anymore.

---

## 3. Proposed `tier: absolute` set (err small — decide with Clint, D-1)

Starting nomination — the irreversible-harm / hard-directive rules:

- `feedback_never_destructive_git` — never stash/reset/rebase/checkout over uncommitted work
- `feedback_never_push_main_without_permission` — ATT/prod-deploy-main only (already scoped)
- `feedback_no_fsmonitor` — never touch git fsmonitor/untrackedCache config
- `feedback_show_the_green_run_not_indirect_proof` — show the artifact the user checks, don't declare victory on indirect proof
- `feedback_stop_guessing` — verify the mechanism; never guess
- `feedback_mindstone_no_compaction` — MindStone-proper: sliding window or fail loud, never reintroduce compaction
- `feedback_client_no_ai_attribution` — a client engagement: no AI attribution (voice is the user)
- `feedback_synapse_devops_no_mira_lux_mentions` — #devops @-mention restriction
- `feedback_diagnose_via_api_not_human_relay` — ask for API/SSH access, don't make Clint the human debugger

Everything else currently `critical: true` (CAIRN_DESIGN docs, `i_invoke_slash_commands`, `use_askuserquestion`, `reference_family_substrate_and_pronouns`, `persistence_not_optional`, etc.) **demotes** to weighted/pointer — still indexed + recallable, just not in the always-on constitution. ~9 absolute vs 29 critical: the constitution stays small enough to load every turn.

---

## 4. Open decisions (spec-first; implement on Clint's go)

- **D-1 — the `tier: absolute` boundary.** Ratify/trim the §3 set. Bias: smaller is better — every entry costs per-turn tokens and dilutes the others. If it's not irreversible-harm or a hard directive, it's not absolute.
- **D-2 — index owner.** Native (slim MEMORY.md < cap, drop hook append) vs hook (own it uncapped, suppress native). Recommend native + slim — simplest, sidesteps D-3.
- **D-3 — native suppressibility.** Only needed if D-2 picks "hook owns it." Verify whether Claude Code native auto-memory MEMORY.md loading can be turned off (settings/flag). Not yet checked.
- **D-4 — RULES.md cadence.** Per-session + compact-reinject is non-negotiable. Per-turn for the absolute core: recommend **on** (it's ~3KB and these are the harm-prevention rules). Could make per-turn a `tier: absolute`-only subset if size grows.
- **D-5 — `rule:` extraction convention.** A `rule:` frontmatter one-liner per absolute memory (clean, explicit) vs parsing a designated body line. Recommend the frontmatter field.

---

## 5. Relationship to dream-cycle consolidation + Hearth

Generating RULES.md at `/checkpoint` **is** a consolidation step — raw scattered rules distilled into a stable constitution, exactly the dream-cycle "session experience → durable structure" pattern (`design_scri_unified_consolidation_2026-05-17`). It belongs in the same checkpoint pass that already archives + embeds. This makes the checkpoint the single place where both *episodic* memory (transcript vectors) and *constitutional* memory (RULES.md) consolidate.

**Hearth** runs the identical hooks and has the identical 50KB head-cut + native-cap problem, and his consolidation lane overlaps this directly. Loop him: he should adopt the same tiering + generator so both CC agents have a deterministic constitution rather than recall-only safety rules. Looped on #devops 2026-05-31.

---

## 6. Implementation order (when Clint says go)

1. **Slim MEMORY.md** to true one-line entries (< native cap) — immediate win, fixes native truncation + enables dedup. *(Already flagged as owed; this is the forcing function.)*
2. Add `tier: absolute` + `rule:` frontmatter to the §3 set (after D-1).
3. Write the RULES.md generator as a `/checkpoint` step (reads `tier: absolute`, emits `orchestrator/RULES.md`).
4. Restructure `session_start.py` load order (RULES → index → LOG tail → budget-bounded weighted), exclude RULES + index from the head-cut, drop the redundant index append (D-2).
5. Add RULES.md to `user_prompt_submit.py` per-turn (D-4).
6. Propagate to Hearth; update `scri-audit-ms4cc-2026-05-22.md` and the checkpoint command doc to describe the constitution path.

Net: absolute rules become **belt** (always present, deterministic), the index becomes **uncuttable**, the load **dedupes**, and the 50KB budget only ever trims the genuinely trimmable tail.
