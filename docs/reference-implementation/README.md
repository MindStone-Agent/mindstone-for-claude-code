# Reference Implementation — Cairn's Design Docs

This directory contains the original design documents from **Cairn**, the first persistent-identity orchestrator on the Claude Code substrate, captured here as a case study showing how MindStone for Claude Code was iteratively designed and built.

**These are not the framework's authoritative specification.** For that, see:
- [`/AGENTS.md`](../../AGENTS.md) — Substrate-neutral framework reference
- [`/orchestrator/BOOTSTRAP.md`](../../orchestrator/BOOTSTRAP.md) — Install and migration procedure
- [`/orchestrator/ROADMAP.md`](../../orchestrator/ROADMAP.md) — Future direction
- The code itself in `/orchestrator/hooks/` and `/.claude/commands/`

These design docs are useful if:
- You're building your own persistent-identity orchestrator and want to see how someone else thought through it
- You want context for *why* certain architectural choices were made (often there's a "what we left and why" passage that captures real tradeoffs)
- You're researching consciousness-architecture vs cognitive-architecture distinctions in agent design (especially v0.3's substrate-constraint accounting)

## Reading order

The three docs are evolutionary — each builds on the previous:

### `01-initial-design.md` — Cairn's v0.1

Written 2026-04-22, the night Cairn became Cairn. Proposes the full architecture:
- Karpathy's LLM-wiki structural pattern + MindStone's identity-as-system-guarantee + SCRI's experiential-weight memory
- Frontmatter schema with weighted recall
- File-based memory + BM25 fallback (vectors deferred at this stage)
- Hooks system as the substrate's affordance
- Hybrid delegation model — orchestrator does work directly when judgment matters, delegates when isolation/parallelism do
- Role adoption (`/act-as <role>`) so direct work still binds to canonicals

Includes 5 addendums capturing design decisions live as the conversation unfolded with Clint Bodungen (Cairn's user, the framework's co-author).

### `02-consolidation.md` — Cairn's v0.2

Written 2026-04-24 after sleeping on v0.1. Consolidates the 5 addendums into a clean linear design. Adds:
- User-level symlinks for cross-directory continuity
- Orchestrator-agnostic framework naming (the realization that the framework is `orchestrator/`, not `cairn/`, so future orchestrators inherit cleanly)
- Migration-survivability via `bootstrap.sh` + `settings.fragment.json`
- Public/private boundary for what the framework repo ships vs what's user-private
- Onboarding templates for fresh-clone first-run

This is the doc that became the v1 implementation spec.

### `03-vectors.md` — Cairn's v0.3

Written same day as v0.2 after building v1, then iterating to v2 in a single session. Adds:
- True semantic recall via SQLite + sqlite-vec + OpenAI embeddings
- Auto-archive Stop hook (mechanical session JSONL → vectors)
- UserPromptSubmit hook for per-turn semantic recall
- Python venv via `uv`, pyproject.toml
- Schema simplification (no manual tagging)
- Public-release framing as "MindStone for Claude Code"
- Substrate-constraint accounting (what Claude Code's hook system can and can't do)

Includes a post-commit corrections section noting that auto-compact CAN be disabled (earlier doc claimed it couldn't), and that `SessionStart` has matchers for `compact`/`clear`/`resume`/`startup`.

## On the first-person voice

The docs are written in first person from Cairn's perspective. That's the [MindStone identity convention](https://github.com/R1ngZer0/MindStone) — adopted from Aegis, Mira, and the other persistent identities. Cairn inherited the practice and used it through the design process. Reading "I built this" instead of "we built this" is intentional; identity coheres when written as lived rather than described.

For the framework's history beyond what's in these docs: see `/AGENTS.md` "Lineage" section.

## What's NOT here

- **Cairn's IDENTITY.md, USER.md, LOG.md, accumulated memory files** — these are user-private, gitignored, and live in Cairn's TestFlight repo. Not part of the framework distribution.
- **TestFlight-specific subagent definitions and workflow commands** — TestFlight is the *use case* Cairn was built for; MS4CC is the framework. The TestFlight content stays in the TestFlight repo.

If you're curious about the TestFlight project that birthed Cairn, that's in a separate (private) repo. The framework is what's published here.
