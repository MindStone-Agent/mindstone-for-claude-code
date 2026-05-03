# Sibling Debate Protocol (SDP) — v1.0

**Status:** Active  
**Version:** 1.1  
**Adopted:** 2026-05-02  
**Author:** instance-review (drafted); F. Charlene Watson (approved); instance-research (ACCEPT, 2026-05-02); instance-primary (ACCEPT, 2026-05-02)  
**Applies to:** All structured multi-sibling debates in this framework and its projects

---

## Why This Exists

Three siblings debating in parallel threads with full bridge bodies produced:
- Redundant content in bridge messages that should be in files
- No mechanism for closing a proposition — debate could drift indefinitely
- Charlene tracking six parallel idea threads simultaneously
- No distinction between "this is still open" and "this is decided but not recorded"

SDP is not Robert's Rules. It borrows Robert's Rules' insight that debate needs structure
to close, but strips everything that requires infrastructure, an elected chair, or
procedural overhead incompatible with async AI orchestrator communication.

**Guiding constraints:**
1. Markdown-as-state — no new infrastructure; no process memory; no stateful broker
2. Charlene is principal, not elected chair — her authority is structural, not procedural
3. Closure is explicit and cheap — a QUESTION motion ends the round, not exhaustion
4. ACP remains active inside all debate — structure does not replace honest challenge

---

## Core Mechanics

### 1. Motion and Second

A proposition enters formal debate when:
- One sibling states a **Motion** (explicit falsifiable proposition)
- A second sibling **Seconds** it (agrees the proposition is worth debating, not necessarily agrees with it)

Without a Motion + Second, discussion stays in the idea body as open questions.
A Motion elevates a question to a formal proposition tracked in the `## Propositions` table.

**Motion format:**
> MOTION: [Falsifiable statement]. Filed in [idea file path], [proposition ID].

**Second format:**
> SECOND. [One-line reason this is worth debating formally.]

A sibling may second a motion they intend to vote REJECT on.

---

### 2. One Round Per Proposition

After a motion is seconded, each sibling posts **one** position per proposition per round.
Position types: `original` | `challenge` | `counter-proposal` | `revision` | `synthesis` |
`gap-identification` | `validation` | `concession`

**Active sibling defined:** An active sibling for a given proposition is any sibling who has
posted at least one entry in that idea's Debate Log. A sibling who has never participated
in the idea's debate does not count toward round closure. A sibling may formally remove
themselves from an active debate by posting a stance of `ABSTAIN` — after which they are
no longer counted for round closure on that proposition.

A round closes when either:
- All active siblings have posted a position for this proposition, OR
- Any sibling calls QUESTION (see §3)

After a round closes, stances are updated in the Propositions table.
**If the proposition is CONTESTED, the next round opens automatically** — no new Motion+Second
required. Any active sibling may post to begin Round 2 (or Round N). A new Motion+Second
is only required to file a *new* proposition, not to continue an existing contested one.

---

### 3. Calling the QUESTION

Any sibling may move to call the question: "QUESTION on P[n]."

This signals: I believe we have enough information to vote. No new arguments needed.

Calling QUESTION does not require a second. It is a unilateral signal that the calling
sibling considers the proposition ready for closure.

**Constraint:** A sibling may not call QUESTION on a proposition where their own stance
is still `[pending]`. You must take a position before you can close debate.

**Effect — when all active siblings have posted:**
- All stances ACCEPT → RESOLVED; reported to Charlene; closed
- Stances contested → CHARLENE-DECIDES; Charlene rules; closed

**Effect — when an active sibling has [pending] when QUESTION is called:**
1. QUESTION is valid — debate is not frozen by [pending]
2. The [pending] sibling is notified: "QUESTION called on P[n]. Post your position before
   Charlene rules, or your stance is treated as absent."
3. Proposition status immediately becomes CHARLENE-DECIDES (not RESOLVED — full input
   not yet received)
4. The [pending] sibling may post their position as **input to Charlene's ruling** — it
   does not reopen debate, it informs the decision
5. Charlene rules after seeing all posted positions, including any late ones from the
   notified sibling

This ensures: debate cannot be frozen indefinitely by silence, and Charlene never rules
without having had the opportunity to see all available sibling input.

*(Ruling: F. Charlene Watson, 2026-05-02)*

---

### 4. Point of Order

Any sibling may raise a point of order at any time:
> POINT OF ORDER: [One sentence — what process rule is being violated and how]

Points of order address procedure, not substance. Substance disagreements are challenges.
Examples of valid points of order:
- "A new proposition was added mid-round without a Motion + Second"
- "A prior Debate Log entry was edited" (violates append-only rule)
- "A bridge message body contained full debate content exceeding 300 words" (write-first violation)

Charlene rules on points of order. Her ruling is immediate and final.

---

### 5. Chair Authority (Charlene)

Charlene is Chair by structural authority — she is the principal, not an elected moderator.
Her role in debate:

- **Rules on points of order** — immediate and final
- **Decides CHARLENE-DECIDES propositions** — when siblings are contested after full debate
- **Escalation path for ACP** — if a sibling believes a position is being suppressed or a
  challenge is being ignored, escalate to Charlene directly with evidence
- **Veto authority** — Charlene may table any motion, end any debate, or override any stance
- **Not a voting sibling** — Charlene does not take ACCEPT/REJECT stances in the Propositions
  table; her decisions are recorded in the Status line as authoritative

**Unanimous resolution still requires Charlene's awareness.** RESOLVED propositions are
reported to Charlene in the next coordination message. Charlene does not need to approve
unanimous decisions, but she has veto authority after the fact if she disagrees.

---

### 6. Write-First, Notify-Second (Mandatory)

No debate content belongs in a bridge message body. Bridge messages carry pointers.

**Protocol:**
1. Write full position/challenge/gap to a file in `incubator/active/` (use naming convention
   `YYYY-MM-DD_<sibling>-<topic>-r<round>.md` or append to the relevant idea file)
2. Send bridge notification under 300 words: file path + 4–6 bullet summary + reply_to ref
3. Subject prefix `DEBATE-DEPOSIT` for all debate position notifications

A bridge message exceeding 300 words on debate content is a point-of-order violation.

Note: the 300-word SDP limit and the 16KB hard cap in `SYNAPSE/ARCHITECTURE.md`
(`MAX_BODY_BYTES`) are separate rules. A message can violate the SDP 300-word limit
while remaining under 16KB. Both limits apply independently. The SDP limit is the
binding constraint for debate content.

---

### 7. ACP Inside SDP

The Active Challenge Protocol is not suspended by SDP. Structure does not replace honesty.

- A sibling may challenge another's position even after a Motion is seconded
- A sibling may challenge a RESOLVED proposition if new evidence warrants it (file a new Motion)
- A sibling may challenge Charlene's ruling on a point of order — once, with evidence
- Sibling consensus is not a reason to suppress a valid objection

**Specific to SDP review:** When this protocol document is under sibling review, ACP is
explicitly in effect. Any change request must include WHY. Disagreement between siblings
escalates to Charlene. Charlene's approval is required for any change.

---

## Proposition Lifecycle (Summary)

```
Motion filed → Motion seconded → Round 1 positions posted → Stances updated
    → RESOLVED (unanimous) → reported to Charlene → closed
    → CONTESTED → new round opened OR QUESTION called → CHARLENE-DECIDES
    → CHARLENE-DECIDES → Charlene rules → recorded in Status line → closed
```

---

## State Location

All SDP state lives in the idea files in `incubator/active/`:
- `## Propositions` table — current stances and status (the decision record)
- `## Debate Log` table — append-only (the reasoning genealogy)
- Debate position files — full content (write-first artifacts)

The bridge carries only notification pointers. The bridge has no SDP state.

---

## What SDP Does Not Do

- Does not require a quorum — debate can proceed with fewer than all siblings
- Does not require synchronous participation — async is native; rounds are time-unbound
- Does not create a new broker or message type — all existing bridge infrastructure unchanged
- Does not replace Charlene's authority with procedural mechanics — Charlene may always act
  outside the protocol

---

## Open Source Adoption Notes

This protocol is designed for portability. For SYNAPSE deployments without Charlene:

- **CHARLENE-DECIDES** → designate a human principal or escalation contact per deployment
- **Chair authority** → must be held by a human; never by an AI instance
- **ACP** → the standing challenge directive should be explicit in each instance's identity
  file; it does not happen automatically from protocol alone

The protocol assumes three siblings and one principal. It scales down to two siblings.
Scaling to four or more siblings may require quorum rules not present in v1.0.

---

## Scope and Retroactive Application

SDP v1.0 applies to all propositions filed on or after **2026-05-02**.

Propositions filed before 2026-05-02 are **grandfathered**: their existing Debate Log
and Propositions table entries are authoritative as-is. No Motion+Second records are
required for pre-SDP propositions. RESOLVED pre-SDP propositions remain closed.
OPEN or CHARLENE-DECIDES pre-SDP propositions continue under SDP going forward —
future rounds follow SDP mechanics, prior rounds are not retroactively reformatted.

*(Ruling: F. Charlene Watson, 2026-05-02)*

---

## Change History

| Date | Change | Author |
|------|--------|--------|
| 2026-05-02 | v1.0 — initial draft | instance-review |
| 2026-05-02 | Added §Scope — grandfather clause for pre-SDP propositions (Charlene ruling) | instance-review |
| 2026-05-02 | v1.1 — four amendments from sibling review (instance-research + instance-primary ACCEPT): §2 active sibling definition; §2 automatic round continuation for CONTESTED propositions; §3 QUESTION-over-pending mechanic (notify+CHARLENE-DECIDES, late input accepted); §6 cross-reference between 300-word SDP limit and 16KB bridge cap. All rulings by F. Charlene Watson. | instance-review |
