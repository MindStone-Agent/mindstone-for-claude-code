---
description: Run the adversarial review loop on a deliverable with an independent fresh-context reviewer, apply the findings, and re-attack the edits until a round comes back clean.
---

# Adversarial review — independent verification, in rounds

Run this on anything whose outcome is critical: a deploy, a customer-facing change, a data migration, security-adjacent code, and any document a third party will act on (proposal, SOW, status report, review findings, a drafted message). Authorization is standing; there is no permission to wait for. `$ARGUMENTS` names the deliverable (paths) and, optionally, the ask it answers.

**If a TestFlight checkout is installed, follow its skill instead of the rest of this file.** Resolve the path with Bash, because the Read tool does not expand variables: `echo "$TESTFLIGHT_HOME"`; if that prints nothing, `grep -m1 '^TESTFLIGHT_HOME = ' ~/.claude/CLAUDE.md 2>/dev/null | cut -d' ' -f3-`; if that prints nothing too, `git rev-parse --show-toplevel` when working inside a git checkout (the existence check below decides whether it is TestFlight). If `<path>/.claude/skills/adversarial-review/SKILL.md` exists, read it in full and follow it in place of the loop, briefs, transports and receipt below. Two rules of this file still apply: "Independence is the active ingredient" (the next paragraph) and "Never report the mechanism as blocked" (the last paragraph). In the receipt, record whether you used the TestFlight skill or this file, and for the skill also record the path, the output of `git -C <path> log -1 --format=%h -- .claude/skills/adversarial-review/SKILL.md` (if it prints nothing, record "uncommitted copy"; if `<path>` is not a git checkout, say so and treat the skill as unreviewed), and whether `git -C <path> status --porcelain -- .claude/skills/adversarial-review` prints anything (uncommitted edits are unreviewed). If no path resolves, or the file is missing (a moved checkout, or one older than the skill), use this file and say so.

**Independence is the active ingredient.** The reviewer is always a separate context: a fresh subagent, or a persistent QA peer reached over Synapse. Self-review does not count, a second pass in your own context does not count, and a persona switch inside your own session does not count. The context is the contamination.

## The loop

1. **Round 1.** Launch a fresh `general-purpose` subagent (background) with the round-1 brief below. It inherits none of your conversation and re-reads everything. It does load the CLAUDE.md hierarchy and a git-status snapshot, and has been observed on at least one installation to receive your auto-memory index (MEMORY.md), so it is fresh-context, not recall-clean.
2. **Apply.** Apply findings as exact-match replacements that assert exactly one match per edit, re-run any mechanical scrub (style banlist, em dashes, spelling), and record what was applied and what was rejected with a reason.
3. **Round N.** Launch a new fresh subagent with the round-N brief: the numbered previous findings, the edited passages, and the instruction to re-attack only what changed plus anything it now contradicts.
4. **Converge.** Once fixes start adding mechanism (usually round 3 or 4), switch to the closing brief, which limits findings to text that is wrong, contradicts the document, or cannot work as stated. Fixes spawn mechanisms and mechanisms have defects; without the scope rule the loop grows the specification instead of finishing it.
5. **Stop** when a round returns no severity-1 or severity-2 findings. Apply remaining nits without another round.
6. **Receipt.** In the commit, PR, board comment, or handoff: rounds run, severity-1 and severity-2 counts per round, the last verdict, and where the transcripts are.

Expect several rounds. A 6,800-word product design took seven (severity-1 per round: 7, 0, 1, 1, 2, 1, 0; round 7 still had one severity-2, which was applied without the further round step 5 requires), and every severity-1 after round one was in text a previous round's fix had introduced.

## Round-1 brief

```
You are an independent adversarial reviewer. Your job is to REFUTE, not confirm. Do not soften findings. Rank them by severity with evidence.

Documents under review (read in full): [paths]
Audience and purpose: [who reads it, what they do with it, what happens next]
The ask, verbatim: "[the requester's words]"

Attack on these axes, in order:
1. Factual claims about [the subject]. Fetch [primary sources] yourself and check EVERY number, quote, label, and claim verbatim.
2. Technical claims. Verify each against vendor docs or standards: [list]. Report anything wrong or overstated.
3. Scope discipline. [The phase rule that applies.] Flag over-specification.
4. Coverage of the ask. Missing elements; invented scope presented as required.
5. Internal consistency. Contradictions between sections, diagrams and prose, tables and phases, this document and its companions.
6. Voice and style. [Rules.] Quote each violation with its line.
7. Anything else a skeptical [role] would tear apart: unfalsifiable acceptance criteria, guardrails that cannot be enforced as described, unstated assumptions.

Out of bounds: [customer systems, live forms, bulk requests, destructive probes]. Do not edit the files.

Output: a ranked list. For each finding: severity (1 = must fix before it leaves, 2 = should fix, 3 = nit), file and line, the claim as written, the evidence with the URL or source, and the corrected text ready to paste. Then a list of claims you verified as CORRECT. Then a process note: what you touched and did not. "Nothing real found" is an acceptable answer.
```

## Round-N brief

```
You are an independent adversarial reviewer running pass [N] on documents revised after pass [N-1]. Re-attack the EDITS: confirm each previous finding was applied correctly, and find anything the edits introduced. Refute, do not confirm.

Files: [paths]
Context: [two sentences]
Previous findings that were supposed to be applied: [numbered list with the corrected fact]

1. For each previous finding: APPLIED / PARTIALLY / NOT APPLIED with the line, and whether the applied text is itself correct. Spot-verify new claims the fixes introduced against [sources].
2. Re-check every quoted string and number against [primary sources]. [Out-of-bounds list.]
3. Internal consistency after the edits.
4. Style scrub. [Rules.]
5. Anything new a skeptical [role] would attack in the revised text.

Output: ranked findings, then the applied/not-applied table, then the verified-correct list. If nothing rises to severity 1 or 2, say "ROUND CLEAN" on the first line.
```

## Closing brief

```
Closing pass. Re-attack ONLY the sentences changed in the last round: [list with section names].

Scope rule: report a finding ONLY when the text as written is factually wrong (cite the source), contradicts another passage of the same document, or promises a mechanism that cannot work as stated. No elaboration, no extra mechanisms, no nice-to-haves.

Output: first line "ROUND CLEAN" or "NOT CLEAN: n severity-1, n severity-2, n nits", then findings with line, text as written, evidence, paste-ready replacement, then a one-line verdict per changed sentence.
```

## Transports

- **Ephemeral fresh-context reviewer (default):** the fresh subagent above (fresh context, not recall-clean; see step 1).
- **Persistent QA peer over Synapse:** for deliverables crossing a boundary (third party, production, security), when a different model or substrate is wanted, or when the reviewer must run the product rather than read it. Post the brief to the QA channel with an @mention, announce the review window, and wait for the report. The peer's memory holds the review protocol and a defect-class catalog, never the author's project context. One review window at a time; the worktree is frozen for its duration; QA re-runs only after an explicit handoff.

Never report the mechanism as blocked. Silently substituting self-review is the failure this command exists to prevent.
