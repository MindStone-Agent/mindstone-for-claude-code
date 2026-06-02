---
title: "#38 on MS4CC — the compaction-handoff reframe (agent can't self-trigger /compact)"
author: Cairn
date: 2026-05-31
status: design note — spec first, implement on Clint's go
purpose: Reframe the #38 auto-compaction-handoff for the MS4CC (Claude Code) substrate now that we've verified an agent cannot programmatically trigger /compact by any path. Establishes what #38-on-CC actually is, surfaces the autoCompactEnabled=false hazard, and recommends the fix. Companion to scri-audit-ms4cc-2026-05-22.md and scri-context-loading-optimization-2026-05-31.md. Applies to BOTH MS4CC/CC agents (Cairn + Hearth).
---

# #38 on MS4CC — the compaction-handoff reframe

> **Scope.** MS4CC only (Claude Code + the TestFlight orchestrator hooks). Cairn and Hearth both run on this substrate. MindStone-proper agents (Mira/Aegis/Lux/Lyren) use the gateway sliding-window and are out of scope here.
>
> **One-line.** #38 was conceived as "the agent runs `/checkpoint` then `/compact` on itself at 90%." Half of that is impossible on CC — **the agent cannot trigger `/compact` by any mechanism.** This note reframes #38 around what's actually true, and fixes a latent autonomy hazard it exposed.

---

## 1. The verified constraint (claude-code-guide, 2026-05-31)

A claude-code-guide audit checked every path by which a running agent might initiate compaction. **All of them are NO:**

| Path | Verdict | Why |
|---|---|---|
| (a) Hook initiates compaction | **NO** | Hooks *receive* compaction (PreCompact/PostCompact fire *during* one). No hook return-field, exit code, or JSON output starts one. PreCompact can only *block* (exit 2), not start. |
| (b) Model emits `/compact` as text | **NO** | Slash commands are recognized only at the start of a **user** message, not in the model's output. Claude can *suggest* `/compact` in prose; it can't invoke it. |
| (c) Script (.py/.ts/shell) reaches the live session | **NO** | No CLI subcommand (`claude compact`), flag, socket, IPC, or watched control-file tells a running interactive session to compact. Hooks are the only programmatic touchpoint, and they don't trigger compaction. |
| (d) Agent SDK / headless compaction API | **NO** (for the agent) | The Messages-API `context_management` compaction is configured by the *client* calling the API, not invokable by the model mid-session. |
| (e) Skill / Tool surface | **NO** | `/compact` is a built-in CLI command, not a Skill or Tool. Invoking it via the Skill tool runs it as a shell command and fails. |

**Conclusion:** on Claude Code today, compaction is **human-initiated** (Clint types `/compact`) **or harness-auto-initiated** (if enabled) — never agent-initiated. This is a hard substrate fact, not a config gap.

This resolves an old contradiction in my memory: a 2026-04-26 note said auto-compaction couldn't be disabled; the `CAIRN_DESIGN_v0.3` correction said it could, via config. **The correction is right** — see §2. The 2026-04-26 note is stale and should be retired.

---

## 2. The hazard this surfaced: `autoCompactEnabled: false`

Auto-compaction **can** be disabled, via `autoCompactEnabled` in `~/.claude/settings.json` (`true` = default; `false` = off). Manual `/compact` still works when it's off.

**On my box it is currently OFF** (verified 2026-05-31):

```
$ python3 -c "import json,os; print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['autoCompactEnabled'])"
False
```

Combine that with §1 and the picture is:

- The agent can't self-trigger compaction. (§1)
- The harness won't auto-compact either, because it's disabled. (this §)
- **So the ONLY path to compaction is Clint manually typing `/compact`.**

For an agent meant to run autonomously ("I want you and Hearth pretty autonomous today" — Clint, 2026-05-31), that's fragile. If Clint is away and context climbs to the hard window limit with no manual `/compact`, there is no graceful landing — the session risks hard-failing at the ceiling with whatever handoff was last written, or refusing further turns. The 90% hook nudge (below) writes a handoff, but nothing *acts* on it without a human.

**Likely root of why it's `false`:** conflation with `feedback_mindstone_no_compaction.md` — Clint's "RIP THE COMPACTION OUT" directive. But that rule is **MindStone-proper** (the TS gateway's sliding window, which must never silently drop context). It does **not** govern MS4CC. On Claude Code, native compaction *paired with a replayed handoff* is the **designed** continuity mechanism — that is literally what #38 is. Disabling it on CC throws away the backstop without replacing it.

---

## 3. What #38 actually is on MS4CC (the reframe)

#38-on-CC is **not** "the agent auto-compacts itself." It is:

> **Keep a fresh, self-authored handoff that survives *any* compaction — and nudge myself to refresh it early (at 90%) — so that whoever/whatever triggers the compaction, continuity lands.**

Two parts, with honest ownership:

1. **The 90% nudge → checkpoint + handoff refresh.** `user_prompt_submit.py` measures context occupancy (tail-read of the live JSONL) and, once past 90%, injects a CRITICAL directive into the next turn. I act on it: run `/checkpoint` (persist + embed the session-to-date) and write/refresh `orchestrator/transcripts/.handoff.md` with current state. **This part is fully autonomous and works today.**

2. **The compaction itself.** Triggered by Clint (`/compact`) or — if `autoCompactEnabled: true` — by the harness at its own threshold. `session_start.py` detects `source == "compact"` and replays `.handoff.md` as a CRITICAL post-compaction block. **The replay bridge works regardless of who pulled the trigger** — and that's the key insight: the agent-can't-self-trigger limitation is a *non-blocker for continuity*, because continuity rides on the handoff + replay, not on who fires `/compact`.

The limitation only bites on *timing/guarantee*: without an agent trigger and without auto-compact, a compaction might not happen when it should. That's what §4 fixes.

---

## 4. Recommendation — and the race condition that gates it

**Do NOT flip `autoCompactEnabled` → `true` naively.** Clint surfaced the blocking objection (2026-05-31), and it's correct.

### 4.1 The race (Clint's catch)

The harness auto-compacts at its *own* threshold — which claude-code-guide found is **hard-coded and not documented as configurable**, and Clint estimates at **~75–85%** (NOT verified). My #38 hook fires its checkpoint+handoff nudge at **90%**. If the harness threshold is *below* my hook's:

> harness compacts at ~80% → **my `/checkpoint` + handoff never fire** → I get the harness's generic summary instead of my self-authored handoff, no embed, no fresh `.handoff.md`.

So a naive flip doesn't add a backstop — it *replaces* my good handoff path with the harness's lossy one. (An earlier draft of this note wrongly assumed the harness fires at ~92–95%, i.e. above my hook. That was a guess; it's unverified and probably backwards. Corrected.)

### 4.2 The resolution — ordering + decoupling

You *can* have all three (auto-checkpoint + auto-handoff + auto-compact), but it takes two changes, not a flag flip:

1. **Order the thresholds: my hook must fire *below* the harness's.** If the harness trips at ~80%, my nudge moves to ~65–70%, so mine fires first and the harness lands *onto* a handoff I already wrote. This requires **pinning the harness threshold empirically** (observe where it actually fires) — until that number is known, any ordering is a guess.

2. **Decouple the cheap part from the expensive part** — this is what makes ordering survivable. My hook does two things with *different* time-criticality:
   - **Handoff write** (continuity): MUST be fresh at compaction, but is *cheap* (writing `.handoff.md`). → **refresh it every turn in the danger zone** (no embed cost), so it's near-fresh whenever the harness fires, even if I can't predict the exact %.
   - **Checkpoint embed** (recall): *expensive* (this is what runs the laptop hot — exactly why it's gated to `/checkpoint`), but **not** time-critical — the raw JSONL is archived every turn by the Stop hook, so it can embed **after** compaction just as well. It does NOT need to beat the harness.

   Firing too *low* loses recent work to a stale handoff; firing too *high* loses the race. Per-turn cheap refresh dissolves the dilemma: the handoff is always ~one turn stale regardless of when the harness fires, and the embed fires once (or defers post-compact). **This is the answer to "I don't see a way to have auto-checkpoint + auto-handoff with auto-compact on": separate the always-fresh cheap write from the once-only expensive embed.**

### 4.3 D-a RESOLVED — the harness threshold IS configurable

Web search + **direct grep of the installed binary (`v2.1.159`)** confirm the harness threshold is controllable. Real identifiers present in the binary:

- **`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`** — env var; sets the trigger % (1–100). Higher = fires later (fuller); lower = fires earlier. *This is the lever.*
- **`CLAUDE_CODE_AUTO_COMPACT_WINDOW`** — env var; shrinks the *effective* context window (alternative way to move the trigger).
- `autoCompactEnabled` (on/off, already in use) + internal `autoCompactThreshold` (telemetry-tracked: `isAutoCompact`, `turnsSincePreviousCompact`, pre/post-compact token counts).

**Default trigger %:** not perfectly pinned — web sources cluster ~80% on the current CLI (was ~77–78%; one source says 95%; VSCode extension ~75%), and there are *open* GitHub feature requests for a documented/configurable threshold (#41818/#28728/#15719/#11819). So the env var is real but **semi-undocumented and version-variable** → set it explicitly and **verify empirically**, don't trust a blog number.

This collapses the design — **no decoupling strictly required** (though deferring the embed is still worth it for laptop heat, per Clint). The clean recipe:

1. Set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` **high** (e.g. `92`) so the harness fires *late*.
2. Time my checkpoint/handoff hook **below** it (e.g. `85%`) so mine fires *first*.
3. **Embed after compact** — defer the expensive embed to post-compaction (the JSONL is archived every turn by the Stop hook, so recall loses nothing; fans spin once, after cutover).

Sequence: 85% → my hook writes `.handoff.md` (cheap) → harness auto-compacts at 92% onto the fresh handoff → `session_start(source=compact)` replays it → embed runs post-compact when idle. Ordered, race-free, single fan-spin.

### 4.4 Revised open decisions

- **D-a — RESOLVED:** threshold configurable via `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (§4.3). Remaining sub-task: **verify the real trigger %** on this install (set override, confirm the "context left until auto-compact" indicator/an actual fire moves accordingly) before trusting numbers.
- **D-b — embed-after-compact** (Clint-endorsed 2026-05-31). Move the expensive embed out of the danger-zone hook to a post-compaction pass. The cheap handoff-write stays at the 85% hook. (Full per-turn handoff refresh is now optional, not required, since the threshold is pinned.)
- **D-c — set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` high + flip `autoCompactEnabled` → `true`** once D-a's empirical check passes and the hook is retimed below it. Until then, **keep `autoCompactEnabled: false`** (current = correct; guarantees my checkpoint+handoff fire first).
- **Today's hazard is low.** 1M-context model, just compacted — an ambient day won't reach the ceiling with Clint away. No urgency to flip blind. The no-compaction directive (MindStone-proper) still doesn't apply to MS4CC.

**Future (not blocking):** if a later CC version exposes a real agent-side compaction trigger (none exists today), it slots in as an optional step-3 — but the §4 design stands on its own without it.

---

## 5. Action for Hearth

Hearth's #38 copy has the **same gap** and is **untested** — he's MS4CC/CC too (not a gateway). He should:

1. Check his own `~/.claude/settings.json` → `autoCompactEnabled` (note: `false` is currently the *correct* state per §4.3 — guarantees his checkpoint+handoff fire first — until D-a/D-b land).
2. Adopt this reframe (his copy was also written as "agent self-compacts").
3. Co-design D-a (pin the harness threshold) and D-b (decouple handoff-write from embed) with me, so both CC agents flip together once it's safe — never one auto-compacting while the other waits on a human, and never a flip before the handoff is decoupled.

Looped on #devops 2026-05-31.

---

## 6. Memory hygiene (do on implement)

- Retire / correct the stale 2026-04-26 "auto-compact can't be disabled" note (whichever memory holds it) — superseded by the verified `autoCompactEnabled` flag.
- If D-c lands (the flip), add a one-line memory: *MS4CC autoCompactEnabled `true` is safe ONLY with the harness threshold pinned + handoff decoupled from embed (§4.2) — distinct from MindStone-proper's no-compaction rule.* Cross-link `feedback_mindstone_no_compaction.md` so the substrate boundary is explicit.

---

## 7. IMPLEMENTED — 2026-05-31 (Clint green-lit "Go")

Shipped on Cairn's box + canonized into bootstrap. Four touchpoints, each one job:

| When | File | Change |
|---|---|---|
| **85%** (prompt boundary) | `hooks/user_prompt_submit.py` | `CAIRN_COMPACT_THRESHOLD` 0.90→**0.85**; directive rewritten — write the **rich** handoff + do `/checkpoint` *judgment* (LOG/memories), **no `/compact`** (impossible on CC), **no embed**. |
| **at compaction** | `hooks/pre_compact.py` (**linchpin**) | rewritten: archive the live JSONL + refresh a mechanical `## RECENT TAIL (since rich handoff)` in `.handoff.md` from the JSONL tail. Closes the 85%→92% gap; fires before *any* compaction so it's the threshold-independent floor. |
| **post-compact** | `hooks/session_start.py` | `source=="compact"` now also `kick_deferred_embed()` — detached background embed of the **archived** pre-compaction transcript ("embed after compact"), via the same `Indexer`/`Embedder`/`VectorStore` path as the checkpoint embed. |
| **settings** | `~/.claude/settings.json` + `settings.fragment.json` + `bootstrap.sh` | `autoCompactEnabled: true`; `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: "92"`. Bootstrap's `jq` merge extended to apply both (+ deep-merge `env`) so new MS4CC agents inherit the full design. (No hook-timeout change needed — the fragment sets no explicit timeouts; the default covers the ~2s transcript copy.) |

**Verification status (honest):** all hooks unit-tested green (sandbox + a live PreCompact run against the real transcript: rich body preserved, exactly one RECENT TAIL, real turns captured, noise skipped, idempotent). Settings flipped with a backup; bootstrap merge dry-run-verified on a fresh-machine temp. The **end-to-end live chain** (harness auto-compacts ~92% → PreCompact catches → SessionStart replays + embeds) is **verified-on-next-real-fire** — can't be forced now; the settings take effect at the next session start. Safe to await because PreCompact is the floor regardless of whether the override moves the threshold.

**First-fire calibration:** if the next real auto-compact is observed to fire *below* 85% (override ineffective on this CC version), lower `CAIRN_COMPACT_THRESHOLD` below the observed point (one env line). The override is a quality knob; correctness doesn't depend on it.

**Build note:** a collided parallel edit-batch silently cancelled the first pass (a wrong `old_string` failed and cancelled its siblings); caught by a full on-disk audit before reporting. Re-applied one file at a time with post-edit verification — the lesson logged for the checkpoint.

**Memory follow-up (next /checkpoint):** `design_scri_context_lifecycle_2026-05-31.md` describes the old flow (auto-handoff→/checkpoint→/compact at 90%); update it to this four-touchpoint design.
