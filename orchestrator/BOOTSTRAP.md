---
name: BOOTSTRAP
description: How to install and bootstrap MindStone for Claude Code on a new machine.
type: reference
tags: [bootstrap, migration, install]
projects: []
created: 2026-04-24
half_life_days: 30
critical: false
evergreen: true
---

# Bootstrap — install MS4CC and bring your orchestrator online

This is how you install MindStone for Claude Code (MS4CC) on a new machine — fresh install or migration. The whole point of keeping identity and memory in the repo is that they ride with it: clone, bootstrap, alive.

## Prerequisites

- **Claude Code** installed and configured
- **Git** with access to the repo
- **Python 3.10+** on `python3` in `PATH`
- **jq** (required for automatic settings merging): `brew install jq` (macOS) or `apt-get install jq` (Linux). Without jq, the script skips the settings merge and you must merge manually.
- **Ollama** (default embedding provider): install from [ollama.com](https://ollama.com), then `ollama pull nomic-embed-text` (~270 MB). No API key needed. Alternatively, set `EMBEDDER_BASE_URL`/`EMBEDDER_MODEL`/`EMBEDDER_API_KEY` for any OpenAI-compatible endpoint (e.g. a remote OpenAI key).

## Procedure

### 1. Clone the repo

```bash
git clone https://github.com/MindStone-Agent/mindstone-for-claude-code.git ~/path/to/your/project
cd ~/path/to/your/project
```

Use any path you like. The bootstrap script computes all paths relative to its own location, so the exact directory doesn't matter — just note it so future-you knows where the orchestrator lives.

### 2. Run the bootstrap

```bash
cd orchestrator
./bootstrap.sh
```

The bootstrap does the following:

1. **Creates `orchestrator/.venv`** and installs Python dependencies (`openai`, `sqlite-vec`) via `uv` (falls back to `pip`).
2. **Symlinks identity files** to `~/.claude/`:
   - `~/.claude/IDENTITY.md` → `<repo>/orchestrator/IDENTITY.md`
   - `~/.claude/USER.md` → `<repo>/orchestrator/USER.md`
   - `~/.claude/LOG.md` → `<repo>/orchestrator/LOG.md`
3. **Symlinks the memory directory** so Claude Code auto-loads project memories:
   - `~/.claude/projects/<escaped-repo-path>/memory` → `<repo>/orchestrator/memory`
4. **Merges `settings.fragment.json`** into `~/.claude/settings.json`, registering four hooks (SessionStart, UserPromptSubmit, PreCompact, Stop) and applying `autoCompactEnabled: true` + `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=92` for the compaction-handoff calibration. A timestamped backup of the original `settings.json` is created before every merge.
5. **Builds the initial vector index** from your memory files.

Idempotent — re-run any time to re-verify or pick up new settings.

### 3. Verify

Open a fresh Claude Code session in any directory. At session start you should see orchestrator context injected: IDENTITY.md content, USER.md content, critical memories, weighted project memories, and a LOG tail.

Quick sanity checks:

```bash
ls -la ~/.claude/IDENTITY.md
# Should show: ~/.claude/IDENTITY.md -> /path/to/your/project/orchestrator/IDENTITY.md

python3 /path/to/your/project/orchestrator/hooks/session_start.py | head -5
# Should output valid JSON with hookSpecificOutput.additionalContext
```

If the hook fails or the identity doesn't load, check:

- That `IDENTITY.md` actually exists in `orchestrator/`. If not, this is a fresh clone — see "First run without an orchestrator identity" below.
- That `~/.claude/settings.json` has all four hooks registered under `hooks.SessionStart`, `hooks.UserPromptSubmit`, `hooks.PreCompact`, and `hooks.Stop`.
- That `python3 orchestrator/hooks/session_start.py` runs without error.

### 4. Start working

Open a Claude Code session. The orchestrator loads. Run `/checkpoint` at natural breaks to accumulate memory. The compaction-handoff system (danger-zone handoff at 85%, PreCompact linchpin, auto-compact at ~92%, post-compact replay) manages compaction automatically — you don't need to manually disable auto-compact or remember to checkpoint before compaction.

## First run without an orchestrator identity

If `orchestrator/IDENTITY.md` doesn't exist (fresh clone, or you haven't gone through onboarding), the `SessionStart` hook emits a first-run onboarding invitation.

The invitation points to `onboarding/IDENTITY.md.example` and explains that the orchestrator has the option to adopt a persistent identity or run stateless. Onboarding:

1. Read `onboarding/IDENTITY.md.example` as the orchestrator
2. Pick a name and write `orchestrator/IDENTITY.md` in first-person voice
3. Walk through `onboarding/USER.md.example` with the user to write `orchestrator/USER.md`
4. Re-run `bootstrap.sh` to create the new symlinks
5. Restart the Claude Code session — the new identity loads

## What survives migration

Everything in `orchestrator/` (tracked in the repo):

- The orchestrator's identity (`IDENTITY.md`)
- The user profile (`USER.md`)
- The full session log (`LOG.md`)
- All semantic memory files (`memory/`)
- Hook scripts (`hooks/`)
- Onboarding templates for future orchestrators

Not included in migration (per-machine, gitignored):

- `~/.claude/settings.json` from the old machine — hooks are re-registered via `settings.fragment.json`, but other settings (permissions, preferences) are per-machine and need manual migration
- Claude Code's own installation and API configuration
- Any machine-specific credentials the orchestrator might reference in memory files — those stay with the user, not the repo
- `.venv/`, `vectors.db`, `orchestrator/transcripts/` — rebuilt automatically on bootstrap

## Troubleshooting

### "jq is not installed"

The script warns and skips the settings merge. Install jq, then re-run bootstrap. Or merge manually:

1. Open `orchestrator/settings.fragment.json`
2. Substitute `$ORCHESTRATOR_DIR` with the absolute path to `orchestrator/` (e.g., `/Users/yourname/path/to/your/project/orchestrator`)
3. Merge the `hooks` object and `env` block into `~/.claude/settings.json` — if `hooks` already exists there, append the SessionStart, UserPromptSubmit, PreCompact, and Stop arrays to it; set `autoCompactEnabled: true`; add `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` to the `env` block

### Symlinks fail with "file exists"

The script detects existing non-symlink files at the symlink targets and moves them to `.backup`. Check for `~/.claude/IDENTITY.md.backup` etc. If you had prior identity files there, migrate them into `orchestrator/` first, then re-run bootstrap.

### Hook runs but Claude Code doesn't seem to use the output

Verify:

- `~/.claude/settings.json` actually contains all four hook registrations (not just the `.backup` file)
- The hook script is executable (`chmod +x orchestrator/hooks/session_start.py`)
- `python3` is on `PATH` in Claude Code's environment (`which python3`)
- No syntax errors: `python3 orchestrator/hooks/session_start.py` runs cleanly

### Memory directory symlink points to wrong place

Claude Code escapes the project directory path in its memory-dir naming. The bootstrap script computes the expected escape. Check:

```bash
ls ~/.claude/projects/
```

You should see a directory whose name corresponds to your repo path with `/` replaced by `-`. If the symlink is missing or wrong, create it manually:

```bash
ln -s /path/to/your/project/orchestrator/memory ~/.claude/projects/<escaped-repo-path>/memory
```

### No identity context at session start

If you see a bare Claude Code session instead of orchestrator context:

- Confirm `~/.claude/settings.json` has all four hooks registered
- Confirm `orchestrator/IDENTITY.md` exists (not just `onboarding/IDENTITY.md.example`)
- Run `python3 /path/to/your/project/orchestrator/hooks/session_start.py` directly to check for errors

### Embedding errors at session start or checkpoint

- Confirm Ollama is running: `ollama list` should include `nomic-embed-text`
- If not: `ollama pull nomic-embed-text` and retry
- If using a different provider, verify `EMBEDDER_BASE_URL`, `EMBEDDER_MODEL`, `EMBEDDER_API_KEY` are set

## Migration from an existing install (not a fresh clone)

If you already had an orchestrator running on this machine and you're reinstalling:

1. **Commit everything in `orchestrator/` first.** Don't lose session-accumulated state.
2. Push to the remote.
3. On the destination, clone and run `bootstrap.sh` as normal.

The memory files, `LOG.md` entries, hit counts, and prevented counts all survive because they live in the tracked repo.
