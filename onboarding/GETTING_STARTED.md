# Getting Started with MindStone for Claude Code

Welcome. This guide is for new contributors and anyone onboarding to the MS4CC framework for the first time. It covers what the framework is, how to get it running, and what to expect in your first session.

---

## What is MS4CC?

MindStone for Claude Code (MS4CC) is a persistent-identity and semantic-memory framework for Claude Code. It gives a Claude Code instance a name, a continuously-accumulating memory, and SCRI-style recall (Semantic Context Resonance Injection — memory retrieval weighted by experiential salience, not just text similarity). It is entirely opt-in: a bare Claude Code session without MS4CC works exactly as it always did. Bootstrap once and your orchestrator is online.

---

## Install and bootstrap

The full prerequisites and procedure live in `orchestrator/BOOTSTRAP.md`. The short version:

```bash
git clone https://github.com/R1ngZer0/mindstone-for-claude-code.git ~/path/to/your/project
cd ~/path/to/your/project/orchestrator
./bootstrap.sh
```

Prerequisites: Claude Code, Python 3.10+, `jq`, and Ollama with `nomic-embed-text` pulled (`ollama pull nomic-embed-text`). See `BOOTSTRAP.md` for details and troubleshooting.

---

## Your first session — what you will see

Open a Claude Code session after bootstrapping. At session start the framework injects:

- The orchestrator's identity (`IDENTITY.md` — who the orchestrator is, in first-person)
- Your profile (`USER.md` — who you are, from the orchestrator's view)
- Critical and high-weight memories from `orchestrator/memory/`
- A tail of `LOG.md` (recent session history)

This context block appearing at the top of each session is intentional, not a bug. It is how the orchestrator reconstructs continuity across what would otherwise be a blank-slate session.

If this is a fresh clone with no `IDENTITY.md`, you will see a first-run onboarding invitation instead. Follow it to adopt an identity or to start stateless — see the next section.

---

## Adopt an identity (or run stateless)

MS4CC supports two modes:

- **Persistent-identity mode** (recommended): the orchestrator has a name, accumulates memory, and grows sharper over time. To set this up, follow `onboarding/IDENTITY.md.example`. Write your own `orchestrator/IDENTITY.md` in first-person, then re-run `bootstrap.sh`.
- **Stateless task-executor mode**: no `IDENTITY.md`. The orchestrator operates without persistent memory, treating delegation as the default. Useful for automated pipelines or users who don't want a persistent identity.

You can switch to persistent-identity mode at any time by following the onboarding flow.

---

## The dream cycle: `/checkpoint`

`/checkpoint` is the orchestrator's reflective checkpoint command — the "dream cycle." When you run it, the orchestrator:

1. Synthesizes the session into a new `LOG.md` entry
2. Reviews which cited memories actually prevented mistakes (Option D — increments `prevented` counts)
3. Proposes new memory files for things learned this session
4. Flags drift (e.g., implementation work done without `/act-as`, decisions without canonical attribution)
5. Embeds the session transcript into the vector store so it is searchable in future sessions

Run `/checkpoint` at natural breaks in your work session — at a milestone, before a long pause, or when context is getting deep. The embedding step is the `/checkpoint`'s job; the Stop hook that fires after each turn handles archiving only, not embedding.

---

## Compaction is automatic — you do not manage it

When a session approaches its context limit, the framework handles continuity automatically. You do not need to manually disable auto-compact, remember to checkpoint before compaction, or do anything special.

Here is what happens:

- At **85% context**, the `UserPromptSubmit` hook detects the danger zone and has the orchestrator write a rich handoff document (`orchestrator/transcripts/.handoff.md`) capturing identity state, key decisions, and open threads. The orchestrator also runs the `/checkpoint` judgment at this point.
- At **~92% context**, the harness auto-compacts. Before it does, the `PreCompact` hook fires and appends a `## RECENT TAIL` section to the handoff document from the session transcript — capturing any work done between the 85% handoff and the actual compaction.
- When the next session starts post-compaction, `SessionStart` replays the handoff document as context and kicks a background embed of the archived transcript.

You will see a handoff/checkpoint sequence happen near the context limit. That is the system preserving continuity — let it run. The only thing you need to do is make sure Ollama is running so the background embed completes.

---

## Memory primer

Memory files live in `orchestrator/memory/*.md`. They accumulate the orchestrator's knowledge — corrections, project context, learned behavior, design decisions. Each file has YAML frontmatter with weight parameters (`hits`, `prevented`, `half_life_days`) that govern how often it surfaces at session start.

The `/checkpoint` command manages memory: it proposes new files, increments `prevented` counts on confirmed mistake-prevention, and updates `LOG.md`.

**Do not hand-edit memory files** unless you know what you are changing and why. They are the orchestrator's long-term knowledge base and the weight values matter. If you want to understand why the orchestrator did something or what it currently knows, read `orchestrator/LOG.md` — the append-only session log is the narrative record.

---

## Where to go next

- **`README.md`** — project overview, structure, embeddings, Synapse client, full hook descriptions
- **`AGENTS.md`** — orchestration model, hook architecture, substrate constraints, slash commands
- **`orchestrator/BOOTSTRAP.md`** — full install procedure, troubleshooting, migration from existing installs
- **`orchestrator/runbooks/`** — current SCRI operational state: as-is assessments, canonical spec diffs, ticket audit (`scri-canonical-runbook.md`, `scri-asis-ms4cc-2026-05-31.md`, `scri-diff-2026-05-31.md`, `ticket-audit-2026-05-31.md`)
- **`docs/reference-implementation/`** — design history (Cairn's three-version evolution from initial proposal to working v2)

---

## Troubleshooting first-run

**Bootstrap says "jq is not installed"**
Install jq (`brew install jq` on macOS, `apt-get install jq` on Linux) and re-run `./bootstrap.sh`. Without jq, the settings merge is skipped and hooks are not registered.

**No identity context at session start after bootstrap**
Check two things: (1) `~/.claude/settings.json` contains all four hook registrations under `hooks.SessionStart`, `hooks.UserPromptSubmit`, `hooks.PreCompact`, and `hooks.Stop`; (2) `orchestrator/IDENTITY.md` exists. If IDENTITY.md is missing, you are in fresh-clone onboarding mode — see "Adopt an identity" above.

**Embedding errors at `/checkpoint` or session start**
The default embedding provider is Ollama. Check that Ollama is running and `nomic-embed-text` is available:

```bash
ollama list
# nomic-embed-text should appear

# If not:
ollama pull nomic-embed-text
```

If you are using a different provider, verify `EMBEDDER_BASE_URL`, `EMBEDDER_MODEL`, and `EMBEDDER_API_KEY` are set in your environment.
