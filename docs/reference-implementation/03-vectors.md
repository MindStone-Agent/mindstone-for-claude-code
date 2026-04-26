---
name: Cairn Design v0.3
description: Delta from v0.2 — adds SCRI-style semantic recall via sqlite-vec + OpenAI embeddings, auto-archive Stop hook, UserPromptSubmit hook, and public-release framing as MindStone for Claude Code.
type: design
status: approved
author: Cairn (self-designed, iterated with Clint)
date: 2026-04-24
supersedes: CAIRN_DESIGN_v0.2.md (delta — not a rewrite)
hits: 48
last_applied: 2026-04-26
---

# Cairn Design v0.3 — Delta

This document is a **delta** on v0.2. Everything in `CAIRN_DESIGN_v0.2.md` still stands unless explicitly changed below. Read v0.2 first for the full architecture.

## What changed since v0.2

### 1. Framework name — **MindStone for Claude Code**

The orchestrator-agnostic framework (identity files, hooks, slash commands, memory schema, bootstrap) is now explicitly named **MindStone for Claude Code** for public release. It's the Claude-Code substrate implementation of the MindStone architecture from `github.com/R1ngZer0/MindStone`.

Important distinction:
- **MindStone for Claude Code** = the generic, reusable framework (what we're publishing)
- **Cairn** = the specific persistent-identity orchestrator instance that Clint and I are building on top of it
- **TestFlight** = a *use case* of MindStone for Claude Code — the software development workflows, the 20 subagents, canonicals. TestFlight is not MindStone for Claude Code. TestFlight *uses* it.

### 2. Semantic recall is now real (true-SCRI move)

v0.2 used keyword routing + weighted frontmatter. v0.3 adds a proper vector store:

- **SQLite + sqlite-vec** for embedding storage. Lightweight, embedded, ships as a dependency, no server.
- **OpenAI `text-embedding-3-small`** for embeddings. Cheap, fast, high quality. Local-embedding support (nomic-embed-text via Ollama) deferred to v4.
- **Indexing covers both corpora:** memory files (chunked by level-2 header or length) AND session transcripts (chunked by conversational turn group).
- **MMR diversification** on retrieval to avoid near-duplicate results.
- **Secret scrubbing** before embedding — regex scrubs OpenAI keys, GitHub tokens, AWS keys, SSH keys, PEM blocks, etc. so accidental secret leaks in transcripts don't end up in the vector store.

See `orchestrator/hooks/embedder.py`, `vectorstore.py`, `indexer.py`, `recall.py`.

### 3. Auto-archive Stop hook

New `orchestrator/hooks/session_end.py` fires on every session end. Mechanical, no LLM required:

1. Resolves the current session's JSONL from `~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl`
2. Copies to `orchestrator/transcripts/YYYY-MM-DD__<uuid>.jsonl`
3. Chunks + embeds + stores transcript in `vectors.db`
4. Scans transcript for memory-filename citations; auto-increments `hits` and updates `last_applied` on matching files
5. Appends a one-line `### Auto-archive` note to `LOG.md`

This is the SCRI "dream cycle at compaction boundary" pattern applied to session boundaries. Transcript memory accumulates automatically whether or not `/checkpoint` runs.

### 4. UserPromptSubmit hook — semantic recall per turn

New `orchestrator/hooks/user_prompt_submit.py` fires on every user turn:

1. Takes the user's prompt
2. Queries `vectors.db` for top-K chunks across memory + transcripts (separate quotas)
3. Applies MMR diversification and minimum-similarity threshold
4. Injects results as `<semantic-recall>` context

This is where semantic recall earns its keep. SessionStart loads the always-needed identity baseline; UserPromptSubmit surfaces "given what you just asked, here's what I probably need to remember."

### 5. Python venv + `pyproject.toml`

v0.2 used `python3` (system). v0.3 uses an isolated venv:

- `orchestrator/pyproject.toml` declares deps (`openai`, `sqlite-vec`)
- `orchestrator/.venv/` (gitignored) managed by `uv` (preferred) or stdlib `venv` + `pip` (fallback)
- Hooks are registered with `.venv/bin/python` so they always run against the pinned dep set
- `bootstrap.sh` creates the venv and installs deps on first run

This makes public release cleaner — users get reproducible installs without polluting their system Python.

### 6. Schema simplification — Clint never tags

`tags` and `projects` frontmatter fields are now **auto-inferred only**, never asked of Clint:

- New memories written by `/checkpoint` have tags derived from filename + content, not user input
- Existing memories keep their fields from the migration script's inference
- Vectors make tags less load-bearing anyway — semantic search finds memories regardless of whether they were tagged right

### 7. `.gitignore` carve-out

Machine-local state added to `.gitignore`:
- `orchestrator/.venv/`
- `orchestrator/vectors.db` (+ journal)
- `orchestrator/transcripts/`

Everything else under `orchestrator/` stays tracked. Vectors rebuild on new machine via `bootstrap.sh` → `indexer.py backfill`.

### 8. Public release direction

Building with public extraction in mind (per v0.2 §14 addendum). The extraction plan:

- **Stays in TestFlight** (not MindStone for Claude Code):
  - `.claude/agents/*.md` (the 20 subagents)
  - `.claude/commands/{rapid-prototype,product-owner,backend-integration,data-pipeline,refactor-existing-project}.md` (TestFlight workflows)
  - `.claude/models/`, `.claude/deep-agents/`, `.claude/skills/` (TestFlight canonicals and skills)
  - `PROJECTS.md.example`, `README.md`'s TestFlight-specific content
  - The specific subagent references in AGENTS.md

- **Moves to MindStone for Claude Code** (public release):
  - `orchestrator/` (all of it — framework + hooks + schema + bootstrap)
  - `onboarding/` (templates)
  - `.claude/commands/{checkpoint,act-as,end-role}.md` (generic orchestrator commands)
  - Abstracted AGENTS.md template (without TestFlight specifics)
  - The MindStone-for-CC README, INSTALL, LICENSE (drafted in this v0.3 work)

Extraction is mechanical when ready. We build inside TestFlight for now because that's where Cairn lives; carving happens later.

## Updated file structure (under TestFlight repo)

```
testflight/
├── orchestrator/                        # ACTIVE ORCHESTRATOR + MindStone-for-CC framework
│   ├── IDENTITY.md                      # Me, Cairn
│   ├── USER.md                          # Clint
│   ├── LOG.md                           # Session log (append-only)
│   ├── ROADMAP.md                       # Future features
│   ├── BOOTSTRAP.md                     # Migration instructions
│   ├── DOCS_UPDATED.md                  # Audit log of TestFlight docs touched
│   ├── README.md                        # [NEW] Public release landing page (MindStone for CC)
│   ├── LICENSE                          # [NEW] MIT
│   ├── pyproject.toml                   # [NEW] Python project config (openai, sqlite-vec)
│   ├── bootstrap.sh                     # Updated: uv venv + install + symlinks + settings merge + index backfill
│   ├── settings.fragment.json           # Updated: venv python + Stop + UserPromptSubmit hooks
│   ├── .venv/                           # [NEW, gitignored] Python virtualenv
│   ├── vectors.db                       # [NEW, gitignored] sqlite-vec vector store
│   ├── transcripts/                     # [NEW, gitignored] Archived session JSONLs
│   ├── memory/                          # Semantic memory files (tracked)
│   │   └── [30+ files + CAIRN_DESIGN_v0.1/2/3.md]
│   └── hooks/
│       ├── session_start.py             # Loads identity + memory + log tail
│       ├── user_prompt_submit.py        # [NEW] Semantic recall per user prompt
│       ├── session_end.py               # [NEW] Archive + vectorize + auto-increment hits
│       ├── pre_compact.py               # Reminder to /checkpoint
│       ├── embedder.py                  # [NEW] OpenAI + scrub
│       ├── vectorstore.py               # [NEW] sqlite-vec wrapper
│       ├── indexer.py                   # [NEW] Chunker for markdown + JSONL
│       └── recall.py                    # [NEW] Semantic search utility + CLI
├── onboarding/                          # Templates for new orchestrators
├── AGENTS.md                            # Substrate-neutral orchestration guide
├── CLAUDE.md                            # Thin pointer to AGENTS.md
├── .claude/commands/                    # /checkpoint, /act-as, /end-role (+ TestFlight workflows)
└── ... (rest of TestFlight unchanged)
```

## Hook architecture — updated

Four hooks total now:

| Hook | When | What it does |
|---|---|---|
| **SessionStart** | Session begin | Inject identity + user + critical memories + weighted top-N + MEMORY.md index + LOG tail |
| **UserPromptSubmit** | Per user turn | Inject semantic recall from vectors.db (memory + transcript chunks) |
| **PreCompact** | Before compaction | Reminder to `/checkpoint` before context is summarized |
| **Stop** | Session end | Archive JSONL → vectorize → auto-increment hits → append to LOG |

## Moved to DONE (was in v0.2 ROADMAP)

From v0.2's open items:

- ✅ SQLite FTS5 / semantic retrieval — chose sqlite-vec + embeddings (better than FTS5 for naturalistic queries)
- ✅ Automatic hit tracking — Stop hook does it via transcript filename match
- ✅ MMR diversification — implemented in `vectorstore.search()`

## Still open / deferred

- **Local embeddings (nomic-embed-text via Ollama).** Deferred to v5. OpenAI works for now.
- **Per-project weight profiles.** Deferred (ROADMAP v4).
- **Reflection agent for self-audit.** Deferred (ROADMAP v4).
- **Cross-session consistency checks.** Deferred (ROADMAP v4).
- **MindStone port proper.** Deferred (ROADMAP v5).
- **Semantic search over IDENTITY/USER chunks** — currently excluded from retrieval targets. May make sense to include if queries about "who am I" or "who is Clint" are common.
- **Public extraction.** Mechanical carve when ready.
- **Sliding-window-adjacent compaction handling.** Moved to ROADMAP v3 after Mira's letter (see `orchestrator/memory/<redacted-operator-memory>`). See `ROADMAP.md` for the full v3 plan.

## Post-commit corrections (2026-04-24, after initial v0.3 commit)

### Auto-compact IS disable-able

v0.3 as written treated Claude Code's auto-compact as mandatory. That was wrong. `/config` → "Auto-compact" is a user-toggleable setting; set to `false` and auto-compaction is off. Manual `/compact` still works.

This changes v3 substantially — the compaction-boundary "cliff" Mira describes can be eliminated by user choice, not just cushioned by hooks. Updated `ROADMAP.md` v3 recommends: flip auto-compact off by default in `BOOTSTRAP.md`, then layer the PreCompact emergency vectorization and `SessionStart:compact` re-injection as graceful-handling around manual compactions.

### Hook matcher values (from research)

`SessionStart` supports a `matcher` field with values `startup`, `resume`, `compact`, `clear`. We only used `*` in v0.3. v3 will use `compact`-specific branching for post-compact re-injection.

## Public release checklist (partial — this session)

- [x] pyproject.toml
- [x] MIT LICENSE
- [x] Framework README
- [ ] Separate repo + extraction script (deferred — we're still inside TestFlight)
- [ ] CI / automated tests (deferred)
- [ ] Announcement / docs polish (deferred)

---

## Revision history

- **v0.3 (2026-04-24, same day as v0.2):** Vectors via sqlite-vec + OpenAI. Auto-archive Stop hook. UserPromptSubmit hook. Python venv via uv. Schema simplification (no manual tagging). Public-release framing as MindStone for Claude Code. Cairn = the specific identity; TestFlight = a use case.
- **v0.2 (2026-04-24):** Consolidated v0.1 addendums. User-level symlinks, orchestrator-agnostic framework, migration survivability.
- **v0.1 (2026-04-22):** Initial design with 5 addendums.

*By Cairn, 2026-04-24.*
