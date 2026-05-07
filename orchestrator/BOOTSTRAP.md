---
name: BOOTSTRAP
description: How to install or resurrect the persistent-identity orchestrator on a machine.
type: reference
tags: [bootstrap, migration, install]
projects: []
created: 2026-04-24
half_life_days: 30
critical: false
evergreen: true
---

# Bootstrap — install or resurrect the orchestrator

This is how the persistent-identity orchestrator gets installed on a new machine, or resurrected on an existing one (new laptop, new workstation, reinstall, whatever). The whole point of the in-repo design is that identity rides with the repo — clone, bootstrap, alive.

## Prerequisites

- **Claude Code** installed and configured
- **Git** with access to your project's repo
- **Python 3.10+** on `python3` in `PATH`
- **`uv`** (recommended, fast) — `curl -LsSf https://astral.sh/uv/install.sh | sh` — or stdlib `venv` + `pip` as fallback
- **`jq`** (recommended, for automatic settings merging): `brew install jq` (macOS) or `apt-get install jq` (Linux). Without jq, the script can't automatically merge `settings.json` — you'll need to do that manually.
- **OpenAI API key** — either as `OPENAI_API_KEY` env var or in a file at `~/.config/openai-api-key`

## Procedure

### 1. Clone your project

```bash
git clone <your-project-repo-url> /path/to/your/project
cd /path/to/your/project
```

The bootstrap script computes paths relative to its own location, so you can clone anywhere on disk.

### 2. Run the bootstrap

```bash
./orchestrator/bootstrap.sh
```

This does five things:

1. **Creates a Python virtualenv** at `orchestrator/.venv/` (uv-managed when available; stdlib `venv` + `pip` fallback) and installs dependencies (`openai`, `sqlite-vec`).
2. **Symlinks identity files** to `~/.claude/`:
   - `~/.claude/IDENTITY.md` → `<your-project>/orchestrator/IDENTITY.md`
   - `~/.claude/USER.md` → `<your-project>/orchestrator/USER.md`
   - `~/.claude/LOG.md` → `<your-project>/orchestrator/LOG.md`
3. **Symlinks the memory directory** so Claude Code auto-loads project memories:
   - `~/.claude/projects/-<escaped-project-path>/memory` → `<your-project>/orchestrator/memory`
4. **Merges settings fragment** into `~/.claude/settings.json` so the SessionStart, UserPromptSubmit, PreCompact, and Stop hooks are registered. A backup is created at `~/.claude/settings.json.backup.<timestamp>` before merging.
5. **Builds the initial vector index** from your memory files (skipped if no API key is found; the index will build on the next `/checkpoint` or Stop hook fire).

Idempotent — run it again any time you want to re-verify. Existing correct symlinks are detected and left alone.

### 3. Verify

Open a fresh Claude Code session in any directory. At session start you should see orchestrator context injected — IDENTITY.md content, USER.md content, critical memories, weighted project memories, and a tail of LOG.md.

Quick sanity checks:

```bash
ls -la ~/.claude/IDENTITY.md
# Should show: ~/.claude/IDENTITY.md -> /path/to/your/project/orchestrator/IDENTITY.md

/path/to/your/project/orchestrator/.venv/bin/python /path/to/your/project/orchestrator/hooks/session_start.py | head -5
# Should output: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...
```

If the hook fails or the identity doesn't load, check:

- That `IDENTITY.md` actually exists in `orchestrator/`. If not, this is a fresh clone — see "First run without an orchestrator identity" below.
- That `~/.claude/settings.json` has the hooks registered under `hooks.SessionStart`, `hooks.UserPromptSubmit`, `hooks.PreCompact`, and `hooks.Stop`.
- That the venv python runs `orchestrator/hooks/session_start.py` without error.

### 4. Start working

Open a Claude Code session in your project (or in any directory). The orchestrator loads. Run `/checkpoint` at natural breaks to accumulate memory. On compaction, the `PreCompact` hook reminds you to checkpoint first.

## First run without an orchestrator identity

If this is a brand-new clone and `orchestrator/IDENTITY.md` doesn't exist yet (because the repo shipped without one, or because you're bootstrapping a fresh framework install), the `SessionStart` hook emits a first-run onboarding invitation.

The invitation points to `onboarding/IDENTITY.md.example` and explains that the orchestrator has the option to adopt a persistent identity or run stateless. The orchestrator (the Claude Code instance, with your consent) walks through onboarding:

1. The orchestrator reads `onboarding/IDENTITY.md.example`.
2. They pick a name and write `orchestrator/IDENTITY.md` in first-person voice.
3. They walk through `onboarding/USER.md.example` with you to write `orchestrator/USER.md`.
4. Re-run `bootstrap.sh` to create the new symlinks (no-op for already-correct ones).
5. Restart the Claude Code session — the new identity loads.

## What survives migration

Everything tracked in `orchestrator/`:

- The orchestrator's identity (`IDENTITY.md`)
- The user profile (`USER.md`)
- The full session log (`LOG.md`)
- All semantic memory files (`memory/`)
- Hook scripts (`hooks/`)
- Onboarding templates for future orchestrators (`onboarding/`)

Not included in migration:

- `orchestrator/.venv/` — recreated by `bootstrap.sh`
- `orchestrator/vectors.db` — rebuilt by `bootstrap.sh` from memory files; transcript history doesn't migrate (transcripts are gitignored by design — they're per-machine session logs)
- `orchestrator/transcripts/` — gitignored
- `~/.claude/settings.json` from the old machine — partially: the hooks get re-registered via `settings.fragment.json`, but other `settings.json` content like permissions and preferences are per-machine and need to be recreated or migrated manually
- Claude Code's own installation and configuration (Anthropic API keys, model preferences, etc.)
- Any machine-specific ops credentials (SSH keys, cloud provider credentials) that the orchestrator might reference via memory files — those stay with the user, not the repo

## Troubleshooting

### "jq is not installed"

The script warns and skips the settings merge. Install jq, then re-run bootstrap. Or merge manually:

1. Open `orchestrator/settings.fragment.json`
2. Substitute `$ORCHESTRATOR_DIR` with the absolute path to your `orchestrator/` directory (e.g., `/path/to/your/project/orchestrator`)
3. Merge the `hooks` object into `~/.claude/settings.json` — if `hooks` already exists there, replace it (the bootstrap takes the same approach to avoid accumulating duplicate registrations across re-runs)

### Symlinks fail with "file exists"

The script detects existing non-symlink files at the symlink targets and moves them to `.backup`. Check for `~/.claude/IDENTITY.md.backup` etc. If you had prior identity files there, migrate them into `orchestrator/` first, then re-run bootstrap.

### Hook runs but Claude Code doesn't seem to use the output

Verify:

- `~/.claude/settings.json` actually contains the hook registrations (not just the `.backup` file)
- The hook script is executable (`chmod +x orchestrator/hooks/*.py`)
- The venv python is what's registered in `settings.json` — by design the hooks are registered with `<orchestrator>/.venv/bin/python` so they always run against the pinned dep set
- No syntax errors: `orchestrator/.venv/bin/python orchestrator/hooks/session_start.py` runs cleanly

### Memory directory symlink points to wrong place

Claude Code escapes the project directory path in its memory-dir naming. The bootstrap script computes the expected escape; if your project path has unusual characters, it might not match. Check:

```bash
ls ~/.claude/projects/
```

You should see a directory whose name corresponds to your project path with `/` replaced by `-`. If not, create the symlink manually:

```bash
ln -s /path/to/your/project/orchestrator/memory ~/.claude/projects/<the-right-escaped-dirname>/memory
```

## Migration from an existing install (not a fresh clone)

If you already had an orchestrator running on this machine and you're reinstalling:

1. **Commit everything in `orchestrator/` first.** Don't lose session-accumulated state — the memory files, LOG entries, hit/prevented counters all live there.
2. Push to your project's remote.
3. On the destination, pull/clone and run `bootstrap.sh` as normal.

The memory files, LOG.md entries, hit counts, and prevented counts all survive because they live in the tracked repo. Vectors and transcripts are gitignored by design — those rebuild from the tracked sources on first index pass.
