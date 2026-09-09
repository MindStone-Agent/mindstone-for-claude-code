# Getting Started with MindStone for Claude Code

Welcome. This guide is for new contributors and anyone onboarding to the MS4CC framework for the first time. It covers what the framework is, how to get it running, and what to expect in your first session.

---

## What is MS4CC?

MindStone for Claude Code (MS4CC) is a persistent-identity and semantic-memory framework for Claude Code. It gives a Claude Code instance a name, a continuously-accumulating memory, and SCRI-style recall (Semantic Context Resonance Injection — memory retrieval weighted by experiential salience, not just text similarity). It is entirely opt-in: a bare Claude Code session without MS4CC works exactly as it always did. Bootstrap once and your orchestrator is online.

---

## Install and bootstrap

The full prerequisites and procedure live in `orchestrator/BOOTSTRAP.md`. The short version:

```bash
git clone https://github.com/MindStone-Agent/mindstone-for-claude-code.git ~/path/to/your/project
cd ~/path/to/your/project/orchestrator
./bootstrap.sh
```

Prerequisites: Claude Code, Python 3.10+, `jq`, and Ollama with `nomic-embed-text` pulled (`ollama pull nomic-embed-text`). See `BOOTSTRAP.md` for details and troubleshooting.

There is also a `/ms4cc-install` slash command that runs this for you (and the
right thing for the consumer topology below) once you're in a Claude Code session.

---

## Updating MS4CC

MS4CC runs in one of **two topologies**, and the update process is different for
each. Get this right and the "how do I get the latest code?" confusion goes away.
There is an `/ms4cc-update` slash command that detects the topology and does the
right thing; the manual steps are below.

### Topology 1 — Direct checkout (the normal case)

You cloned `mindstone-for-claude-code` and run Claude Code from it. `bootstrap.sh`
wired the hooks into `~/.claude/settings.json` **at this repo's path**, so the repo
*is* your live install.

- **Update:** `git pull` in the repo. Because the hooks point at the repo path, a
  pull updates the live hook code in place — there is no separate copy step.
- **Re-run `./orchestrator/bootstrap.sh` only when** the pull added/removed a hook
  file or changed `orchestrator/settings.fragment.json` (those change which hooks are
  *registered* in `settings.json`, which a code pull alone won't update). `bootstrap.sh`
  is idempotent and never touches your identity or memory — when unsure, run it.
  Check with: `git diff --name-status <old>..HEAD -- orchestrator/hooks orchestrator/settings.fragment.json`.
- **Restart Claude Code** after the pull — the running session loaded the old hook
  code at startup. (Hook *code* is picked up by new sessions automatically; only the
  hook *registration* in `settings.json` needs bootstrap.)

### Topology 2 — Consumer install (MS4CC as a dependency of another project)

Another project (the host project) installs MS4CC into a gitignored `orchestrator/`
at a pinned version recorded in `.ms4cc-version`, using `install.sh` via that
project's `scripts/ms4cc-sync.sh` helper. The MS4CC code is **not** the project's
git checkout.

- **Update:** `bash scripts/ms4cc-sync.sh update [REF]` (or `/ms4cc-update`), then
  commit the bumped `.ms4cc-version`. This re-runs the installer at the new version.
- The consumer project owns the pin and updates deliberately, so installs are
  reproducible across machines.

### Where slash commands live (a common gotcha)

Claude Code reads slash commands from two places: the **project-scoped**
`<project>/.claude/commands/` (only when you launch from that directory) and the
**user-global** `~/.claude/commands/` (always, regardless of launch directory). If you
launch Claude Code from `$HOME` rather than the project directory — common when your
hooks are absolute-path-wired so they work from anywhere — the project-scoped commands
are **not** loaded.

To make the commands work from any launch directory, **`bootstrap.sh` symlinks the
project's `.claude/commands/*.md` into `~/.claude/commands/`** (the same way it symlinks
your identity and memory). The project/repo stays the single source of truth: a `git
pull` (direct checkout) or `install.sh` (consumer) updates the files the symlinks point
at, so they stay current automatically. If you ever see stale or missing commands,
re-run `./orchestrator/bootstrap.sh` to refresh the symlinks — do **not** hand-copy
files into `~/.claude/commands/` (a hand-copy goes stale and shadows the real source).

### Migrating a box that predates the incremental-indexing fix (#47/#48)

If `orchestrator/transcripts/` has legacy **dated** archives
(`YYYY-MM-DD__<uuid>.jsonl`), the post-#47 incremental indexer won't auto-migrate
them. One time, after updating: back up `orchestrator/vectors.db`, collapse each
session's dated copies into a single stable `<uuid>.jsonl` (keep the largest /
highest-line copy, delete the rest), then run
`orchestrator/hooks/indexer.py backfill` to embed the tails. Verify recent content
returns from recall before deleting the backup.

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
4. Flags drift (e.g., decisions without canonical attribution, shipped work without a status update)
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

The `/checkpoint` command manages memory: it writes new files, increments `prevented` counts where its own judgment says a memory changed an action, and updates `LOG.md`. It never asks you to adjudicate its checkpoint; you get a short summary afterwards.

**Do not hand-edit memory files** unless you know what you are changing and why. They are the orchestrator's long-term knowledge base and the weight values matter. If you want to understand why the orchestrator did something or what it currently knows, read `orchestrator/LOG.md` — the append-only session log is the narrative record.

### Project hints (optional — tunes recall to your projects)

At session start the framework can boost memories that belong to the project you're
currently working in. It infers the "active project" from your current working
directory using a map you provide in `orchestrator/config/project_hints.toml`. The
map is **install-specific** (your projects, not the framework's), so it isn't shipped
— copy `orchestrator/config/project_hints.example.toml` to `project_hints.toml` and
fill in your own `"cwd-substring" = "project-tag"` entries (the tag matches the
`projects:` frontmatter on your memories). If you skip this, nothing breaks —
project boosting is simply off and recall still works via semantic match.

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
