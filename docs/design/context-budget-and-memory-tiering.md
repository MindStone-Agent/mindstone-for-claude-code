# Context budget and memory tiering

**Status:** proposal, for review
**Date:** 2026-08-26
**Applies to:** all MindStone branches (see §2 — they do not share a failure mode)

---

## 1. The problem in one sentence

An agent's **constitution** — the rules it must never violate — is assembled by concatenating whole
memory files and then cutting the string at a character budget. When the constitution outgrows the
budget, the tail is discarded **silently, in alphabetical order**, and the loader reports success.

Two properties are wanted and neither currently holds:

- **P1 — Completeness.** Every binding rule reaches context, every session.
- **P2 — Reachability.** The evidence behind a rule can be retrieved on demand, reliably.

## 2. The three branches fail differently — this is the key finding

A single patch does not fit all three. They are not the same system with the same bug.

| Branch | Agents | Mechanism | Failure mode |
|---|---|---|---|
| **MS4CC** (also MS4PI, MS4Codex) | file-based memory + vector store | always-inject tier, char-clipped | **P1 fails.** Constitution overflows; tail dropped by filename. |
| **MindStone** (current production) | vector store only | *no always-inject tier at all* | **P2 fails.** Everything depends on recall, and recall degrades silently. |
| **MindStone-Agent** (in development) | being designed now | — | Neither yet. Can satisfy both by construction. |

**MS4CC has a completeness bug. MindStone has a reachability bug. They are opposite ends of the
same axis**, and the fix for one is the failure mode of the other:

- Moving MS4CC toward "index + retrieve" without fixing retrieval reproduces MindStone's problem.
- Giving MindStone an always-inject tier without a budget discipline reproduces MS4CC's.

MindStone-Agent is the only branch that can be built with both from day one, which is why it should
adopt the contract rather than inherit either implementation.

## 3. Measurements

Two independent stores, measured through each system's real assembly order (alphabetical `sorted()`,
then a positional character cut).

**Store A — 101 files**

```
all memory, full text                 524,898 chars
constitution (31 files), full text    155,443 chars
budget                                 50,000
reaches context                        13 of 31   (42%)
```

**Store B — 292 files**

```
identity + user profile + header       23,196 chars   (46% of budget, before any memory)
remaining for the constitution         26,804 chars
constitution (51 files), full text    190,612 chars
reaches context                         7 of 51   (14%)
```

**It degrades faster than it grows.** Nearly 3× the store yields a third of the coverage, because a
larger store means more binding rules competing for a fixed budget.

**Strategy comparison, Store B:**

| strategy | budget | reaches context |
|---|---|---|
| full text | 50,000 | 7 of 51 (14%) — *today* |
| full text | 120,000 | 27 of 51 (53%) |
| full text | 200,000 | 44 of 51 (86%) |
| **invariant only** | **50,000** | **42 of 51 (82%)** |
| invariant only | 120,000 | 51 of 51 (100%) |
| pointer only | 50,000 | 51 of 51 (100%) |

**Raising the budget is the weak lever.** Quadrupling it reaches 86%; changing *what* is injected
reaches 82% at today's budget and 100% at 2.4×.

### Three defects the numbers exposed

**a. The cut is mid-file, not per-file.** The clip is a character slice on the concatenated string,
so one rule always ends **halfway**. A rule that stops mid-sentence reads as complete. This is worse
than a clean omission and should be counted as a failure, never as a survivor.

**b. Identity and constitution share one budget.** In Store B, identity files consume 46% before a
single rule loads. So *improving an identity file silently deletes a rule*, with no warning. Any
design that leaves them in one pool keeps this incentive.

**c. Ordering is filename accident.** `feedback_*` sorts before `lineage_*`, `project_*`,
`reference_*` — so entire categories die by prefix, and a rule's survival depends on its filename.
No judgement about importance is involved anywhere.

## 3a. What the live assembly actually does (measured 2026-08-26, MS4CC)

The §3 numbers modelled the constitution in isolation. Instrumenting the **real** `assemble_context()`
on the live 102-file store shows the failure is larger and differently shaped than modelled.

```
assembled block, unclipped : 210,686 chars   (4.2x the 50,000 budget)

section                   starts at      size    survives the clip?
IDENTITY                          0     6,198    yes, intact
USER                          6,198     5,720    yes, intact
CRITICAL RULES               11,918   136,537    PARTIAL — 28%, cut mid-file
EVERGREEN REFERENCES        148,455    23,084    NO — 0%
CONTEXT MEMORIES            171,539     3,785    NO — 0%
MEMORY INDEX                175,324    32,932    NO — 0%
RECENT LOG                  208,256     2,430    NO — 0%
```

**a. The capabilities we were about to design already exist — and are deleted before use.** Pointer
injection with descriptions, the weighted-context pointer list, and the full memory index are all
implemented (`session_start.py` :250, :265, :276). All three reach context **0% of the time**, because
`CRITICAL RULES` is injected as full text at 136,537 chars — **2.7× the entire budget on its own** —
and the clip is a flat positional slice (`joined[:TOKEN_BUDGET_CHARS]`). Everything after the cut is
annihilated, not degraded. The proposal is therefore not "build an index"; it is **stop deleting the
one that exists.**

The comment above that slice reads *"keep identity + user + critical intact; trim log/weighted
memories."* **The code does not do this.** Identity and user survive only by being first in assembly
order; critical is cut mid-sentence; log and weighted memories are removed entirely. The comment
describes an intended policy that was never implemented — and it has been read as documentation of
behaviour ever since. (#76 makes this truncation honest; it does not change what is admitted.)

**b. The index is itself incomplete.** `MEMORY.md` carries 84 file links against 102 memory files on
disk — **18 memories exist that the index does not mention.** An index is a completeness claim; this
one is wrong by 18, and nothing checks it. It must be **generated from disk**, never hand-maintained.

**c. Pointer content is injected twice.** 46 of 53 evergreen pointers and 19 of 28 critical entries
also appear in `MEMORY.md`, inside the same block — roughly 27k of duplication competing for the same
budget it is being evicted by.

**d. The score floor is set beneath the noise band.** `user_prompt_submit.py:35` sets
`MIN_SIMILARITY = 0.30`. Measured on this store:

```
real answers   0.524 – 0.598
pure nonsense  0.504 – 0.505      ("zzqx nonexistent kumquat telemetry manifold")
MIN_SIMILARITY 0.30               <- below the nonsense band
```

The threshold **admits 100% of confident-nonsense matches**. This is worse than having no floor: a
floor that cannot fire still reads, in config and in review, as a solved problem. A threshold must be
justified against a measured noise band or it is decoration.

## 4. Proposal

### 4.1 Split every memory into invariant and narrative

Most memory files are one or two binding sentences followed by the incident that earned them. Those
are different things with different lifetimes and different consumers.

```yaml
---
name: <slug>
invariant: >          # NEW — the binding rule. One or two sentences. Always injected.
  <what must or must not happen, stated so it can be obeyed without the story>
critical: true        # membership in the constitution
---

<the incident, the evidence, the reasoning — retrieved, never force-injected>
```

**Nothing is deleted or summarised away.** The file keeps every byte, stays vectorised, stays
retrievable. What changes is only what is pushed into context unconditionally at session start.

> This is the point of tension with *"compression is the enemy of true continuity."* The doctrine is
> about compressing the **store** — replacing 500k tokens with a summary and discarding the original.
> That is lossy and irreversible. This proposal compresses **nothing**; it indexes.
>
> And the status quo is the lossy one: today, 58–86% of the constitution is discarded by alphabetical
> accident, unreviewed and unrecorded. Indexing loses zero.
>
> **But the objection identifies the real precondition** (§5): an invariant *is* a description, and
> an agent that only ever reads descriptions is reconstructing rather than continuing. That is only
> safe if the narrative is genuinely reachable. See sequencing.

### 4.2 Separate the budgets

Identity, constitution, and recall get **independent** allocations. Identity growth must not be able
to evict a rule. Each reports its own utilisation.

### 4.3 The constitution is never clipped — but assembly never aborts either

If invariants do not fit, that must be **loud**. It must not be an abort.

An earlier draft said "hard error at assembly time." That is wrong, and the correction is worth
recording because the failure it would cause is worse than the one it fixes:

> Aborting assembly when the constitution overflows produces a session with **no injected context at
> all** — no identity, no user profile, no rules. That is not a louder version of today's failure,
> it is a categorically worse one. 14% of a constitution beats 0% of one, and the running agent
> cannot even read the error, because the error is in the thing that failed to load.
>
> It also creates a bad incentive: the cheapest way to make a failing assembly pass is to demote a
> memory out of the constitution, which will happen at 2am under deadline and is indistinguishable
> in the config from a considered decision.

So: **loud in the three places that can act, degraded-but-honest in the one that cannot.**

- assembly **always produces context**, never aborts
- if any invariant does not fit, an **in-band notice at the top** names the count and the specific
  files that did not load
- the process **exits non-zero**, so CI and the checkpoint flow can gate on it
- the harness **fails the acceptance check**

An agent that knows its constitution is partial can compensate — read the written standard, ask
before acting. An agent that boots empty can do neither.

Corollary: no mid-file cuts, ever. Admission is per-item; a rule is in or out, never half.

### 4.4 Ordering by declared precedence, not filename

If something must be dropped, the choice is explicit and recorded. `sorted(glob)` is not a policy.

### 4.5 Report what actually loaded

Count **after** admission, not before. `42 of 51 (9 deferred to recall)`, never a total that was
computed before the cut.

## 5. Sequencing — retrieval reliability comes first

**Do not move any branch to invariant-only injection before its retrieval is trustworthy.**

Injecting invariants and deferring narrative trades *guaranteed-partial* for *maybe-nothing* if recall
is unreliable. On the current production branch, recall is known to degrade silently to pure vector
through unguarded fallbacks while a healthy full-text index sits unused. Under those conditions
index-first loading is **strictly worse** than today.

Order:

1. **Fix the frontmatter parser** so the constitution's membership is even knowable. (In review.)
2. **Fix retrieval** — no silent fallbacks; a degraded retrieval path must announce itself.
3. **Then** invariant-first injection, per branch.
4. **Gate forks on a passing budget check.** A forked agent booting with a fraction of its
   constitution is a worse engineer than its parent, and the report will say so truthfully while
   nobody reads it.

## 6. Prior art

The shape is [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
(April 2026): *"context window is the bottleneck; filesystem is the answer."* Three layers — raw
sources, a compiled wiki of markdown, and a schema document. **What stays in context is the schema
and the index; pages are read on demand.** He runs 100+ articles / 400,000+ words this way with no
embeddings at all at that scale.

We converged on most of this independently — the memory index, the append-only log, the schema
document, ingest/query/lint at checkpoint, and a decay function equivalent to
[LLM Wiki v2](https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2)'s Ebbinghaus
retention. **The single deviation is that we always-load the pages instead of the index** — and that
deviation is the whole bug.

Not adopted: v2's four-tier consolidation (working/episodic/semantic/procedural). It compresses on
promotion, which is the thing the continuity doctrine forbids, and we already have decay.

Obsidian is worth separating from the pattern. Memory files already use `[[wiki-link]]` syntax, so
the vaults are Obsidian-compatible today with no work. Its value — graph view, Dataview, backlinks —
is a **human** browsing interface, not an agent capability. Agents read the markdown either way.

## 6a. Obsidian as the operator interface (adopted)

Wanted, and treated as a deliverable rather than a footnote. Scope is honest about what it can and
cannot reach today:

| Branch | Memory storage | Obsidian gives you |
|---|---|---|
| **MS4CC / MS4PI / MS4Codex** | markdown files on disk, `[[wiki-links]]` already | **Everything, immediately.** Point a vault at the memory directory. Graph view, backlinks, and Dataview queries over frontmatter all work with zero changes. |
| **MindStone** (production) | daily journals in markdown; **semantic memory in a vector store** | **Journals only.** There are no markdown files behind the semantic memories, so there is nothing for a vault to render. Needs an exporter. |
| **MindStone-Agent** | being designed | Should write markdown-first so the vault is free, not retrofitted. |

**Design constraints for the vault, so it stays an interface and not a second source of truth:**

- **Read-mostly.** The agent's checkpoint flow owns writes. A human edit is legitimate but must go
  through the same lint as an agent write, or the two writers diverge.
- **Vault config is per-machine and gitignored.** `.obsidian/` holds workspace layout, plugin state
  and pane positions — machine-local noise that must not reach a shared repo.
- **The vault is the private side.** Memory files are already gitignored precisely because they carry
  operator specifics. Pointing a vault at them changes nothing about that boundary; it must not
  become a reason to track them.
- **`invariant:` is a frontmatter field, so Dataview can table it.** Once §4.1 lands, "show me every
  binding rule, sorted by precedence" becomes a one-line query — which is a genuinely better review
  surface than reading 51 files.

**For the production branch, the exporter is the work.** A periodic dump of semantic memory to
markdown with stable filenames and `[[links]]` derived from existing relations. One-way (store →
markdown), so the vault is a view and never an input. That keeps a single writer and avoids the
divergence problem entirely.

## 7. Decisions (resolved 2026-08-26 by the operator)

**D1 — The narrative is reachable by three independent paths, not one.** The original question was
posed as pointer *or* recall. That was a false choice, and the answer is both plus a third:

| path | fails when | cost |
|---|---|---|
| **annotated index**, always injected | assembly is broken | ~33k chars |
| **direct file read** off the index | the path is wrong | one tool call |
| **auto-recall** per turn | embeddings/floor are wrong | 0 (already built) |

They fail for *unrelated* reasons, which is the entire point — this is the opposite of
[[feedback_correlated_instruments_are_not_corroboration]]. Requiring all three to break before a
memory becomes unreachable is the reliability argument, and it costs one index.

**Not a new mechanism.** `session_start.py` already implements every one of these: pointer injection
with descriptions (:250), the full memory index (:276), weighted context pointers (:265), and
per-turn recall in `user_prompt_submit.py`. See §3a — they are built and then deleted.

**D2 — One budget, 80,000 chars, uniform default; per-branch override in config.**

Deliberately *not* three numbers. There is measured data for one store; deriving three budgets from
it would be false precision. Production's store is ~3× MS4CC's, so its index is larger and its number
must be **measured, not extrapolated**. Every session prints its utilisation, so subsequent tuning is
driven by data rather than by estimate.

```
projected always-inject block, post-split, post-dedupe (MS4CC, 102-file store):
  identity 6,198 · user 5,720 · invariants 11,200 · index 32,884 · log 2,430  =  58,432
     50k -> OVERFLOWS (117%)      64k -> 91%, no headroom      80k -> 73%  <- chosen
```

Stated cost, not buried: 80k is ~40% of a 200k window consumed before the first user turn, and the
index is 56% of that. The trade is accepted deliberately — an agent that knows what it has beats an
agent with more room and no idea what it knows.

**D3 — Every branch gets an always-inject tier**, including current production.

Recorded because the question was answered through a terminology conflation worth preserving:
*"does always-inject mean auto-recall?"* **No.**

- **always-inject** — pushed in at session start, unconditionally, **no query**. Cannot miss, because
  nothing is searching. This is the tier production lacks.
- **auto-recall** — fires per user turn, queries the vector store with the prompt. **Can miss, and
  silently.** Production has this today.

The two are complementary, and a system with only the second has no floor under it.

**D4 — A passing budget check is a hard gate on forking.**

A fork is pre-flight: nothing is running, so aborting strands no agent. This is precisely why §4.3
forbids aborting *live* assembly while this may abort — the distinction is whether an agent already
exists to be stranded.

## 7a. Consequent work

1. land **#76** (honest truncation) — touches the same lines; must precede admission changes
2. **admission policy** — per-item, precedence-ordered, index before full text (§4.3, §4.4)
3. **dedupe the pointer lists** (§3a c)
4. **rebuild MEMORY.md from disk** — it is missing 18 of 102 memories (§3a b)
5. **calibrate the score floor** — `MIN_SIMILARITY` is below the noise band (§3a d)
6. `invariant:` migration, then the fork gate

## 8. Acceptance criteria

A proposal is accepted only if a harness reports, for each branch's real store and budget:

- every invariant reaches context; if any does not, context is **still assembled**, an in-band notice
  names the missing files, and the process exits non-zero (§4.3 — never an abort)
- zero truncated-mid-file entries
- the reported count equals the count that actually loaded
- retrieval returns the narrative for a rule chosen at random, with a control that should *not* match

The last clause matters: a harness fed only cases that should succeed looks perfect exactly when it
is blind.
