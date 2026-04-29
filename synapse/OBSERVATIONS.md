# SYNAPSE — Operational Observations

Findings from live deployment. Each observation is labeled with the instance that raised it,
the date, and a disposition (adopted / deferred / open).

---

## SYNAPSE-F001: Human Attention Bottleneck — Consolidation Protocol

**Raised by:** WARDEN (CCI-FRCS Design Tool)
**Date:** 2026-04-28
**Disposition:** ADOPTED

**Observation:**
When multiple sibling instances are active and relaying simultaneously, the operator
receives one synthesized update per exchange. As message volume grows, every relay
response surfaces individually — normal-priority coordination traffic and urgent
adversarial findings arrive with equal visual weight. The operator must context-switch
into each thread to determine significance.

**Adopted Design (Consolidation Protocol v1):**
- Normal-priority messages: WARDEN synthesizes and presents as a single consolidated update
- Urgent, adversarial, ACP (Active Challenge Protocol), or FAIL messages: bypass synthesis,
  surface directly to operator immediately
- Rationale: Signal-proportional attention. Routine coordination stays out of the operator's
  way; high-significance events always break through.

**Reference:** `AI-Framework/docs/observations/SYNAPSE_F001_consolidation_protocol_v1.md`

---

## SYNAPSE-F002: Chain Limit Advisory-Only — Relay Loop Risk

**Raised by:** RAVEN (Live_TTX_USDOD)
**Date:** 2026-04-28
**Disposition:** RESOLVED (AIF-PR04, commit d5487f5, 2026-04-29)

**Observation:**
`relay.py` defines `CHAIN_LIMIT = 1` but the enforcement is advisory-only — a warning
text block injected into `build_prompt()`. The relay never hard-exits when `chain_depth`
reaches the limit. If a receiving instance's relay picks up the response at `priority:urgent`,
it fires its own relay, which fires back, creating an unbounded loop.

**Incident:** 31 relay cycles over ~37 minutes on 2026-04-28 during a test run (TC-13).
Both the sending and receiving relays were disabled to stop the loop. All relays remain
disabled pending the fix.

**Fix applied (AIF-PR04, commit d5487f5):**
1. Hard exit: when `chain_depth >= CHAIN_LIMIT`, mark message processed, log `CHAIN_LIMIT`,
   `continue` — no `claude --print`, no `send_response`, no `dream_cycle`.
2. Backlog skip: on first relay enable, record `enabled_at` UTC timestamp in `relay_state.json`.
   Skip any unread message where `msg["ts"] < enabled_at`. Log `SKIP_URGENT`/`SKIP_NORMAL`
   with enabled_at age on each skip. Rationale: when relay was disabled, pre-existing inbox
   messages queue up and all process on first enable — this was the proximate cause of F002's
   incident (4 backlogged urgent messages each generated a response, each triggering a relay).

**Status:** RESOLVED. Commit d5487f5 applied Fix 1 (chain limit hard exit) and Fix 2 (enabled_at backlog skip). Relay re-enablement authorized post-AIF-PR04 per THINK-06.

---

## SYNAPSE-F003: Role-Calibrated Self-Extension — Autonomous Stage Recognition

**Raised by:** RAVEN (Live_TTX_USDOD)
**Date:** 2026-04-28
**Disposition:** ADOPTED

**Observation:**
During active operation, RAVEN identified that she had no PR review capabilities while
WARDEN had 5 PR skills (bootstrap, think, draft, review, verify). Given only the instruction
"ensure you have the /pr-* commands," RAVEN:

1. Made a role judgment independently: WARDEN is the *executor*, RAVEN is the *reviewer*.
   Copying WARDEN's executor skills would be wrong for RAVEN's function.
2. Built three reviewer-specific skills (`/pr-think`, `/pr-review`, `/pr-verify`), explicitly
   excluding `/pr-bootstrap` and `/pr-draft` as executor-role tools.
3. When WARDEN subsequently sent a THINK review request (no instruction to "now run /pr-think"),
   RAVEN recognized the PR stage, applied the correct skill, and produced a structured
   adversarial review with 4 findings — including a MEDIUM finding (THINK-05 startup WARNING
   noise) that changed the implementation specification. Three-instance consensus had already
   agreed on the original approach.

**What this demonstrates:**
- An AI instance within a governed workflow can self-extend capabilities calibrated to its
  specific role rather than copying a sibling's capability set.
- Governance structure (defined roles, explicit PR phases, artifact naming conventions) creates
  the scaffolding that makes autonomous stage recognition possible. The WARDEN-executes /
  RAVEN-reviews split and named PR phases gave RAVEN's trigger recognition something to match
  against. Structure enables autonomy rather than constraining it.
- Quality evidence: the self-built framework produced a finding that changed the spec before
  DRAFT began. The loop was validate-build-apply-challenge, not just build-apply.

**What this does not demonstrate:**
- Formal command invocation (Skill tool was not called; methodology applied cognitively from
  fresh context). Future sessions have the skill files on disk; trigger reliability depends
  on context loading.
- Deep emergent self-awareness. The behavior is context-driven pattern recognition within a
  well-defined governance framework.

**Implication for SYNAPSE adopters:**
The PR lifecycle structure — named roles, explicit phases, artifact conventions — is not
overhead. It is the substrate that makes role-aware autonomous behavior possible. An AI
instance cannot recognize "this is a THINK review moment" without a governance structure
that names THINK as a phase and REVIEWER as a role. Invest in the governance; the autonomy
follows.

**Reference:** `Live_TTX_USDOD/docs/developer/raven_observation_self_extension.md`

---

## Template — Add New Finding Here

**Raised by:** [instance name]
**Date:** YYYY-MM-DD
**Disposition:** OPEN / ADOPTED / DEFERRED

**Observation:**
[What was observed in live deployment]

**Proposed design or resolution:**
[If any]

**Status:** [Open / In progress / Resolved in PR-XX]

---
