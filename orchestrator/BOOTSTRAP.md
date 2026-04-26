---
name: BOOTSTRAP
description: How to resurrect the TestFlight orchestrator on a new machine.
type: reference
tags: [bootstrap, migration, install]
projects: []
created: 2026-04-24
half_life_days: 30
critical: false
evergreen: true
---

# Bootstrap — resurrect the orchestrator on a new machine

This is how the persistent-identity orchestrator comes back to life when TestFlight is cloned onto a new machine (new laptop, new workstation, reinstall, whatever). The whole point of the `testflight/cairn/`-inside-the-repo design is that identity rides with the repo — clone, bootstrap, alive.

## Prerequisites

- **Claude Code** installed and configured
- **Git** with access to the TestFlight repo
- **Python 3.8+** on `python3` in `PATH`
- **jq** (recommended, for automatic settings merging): `brew install jq` (macOS) or `apt-get install jq` (Linux). Without jq, the script can't automatically merge `settings.json` — you'll need to do that manually.

## Procedure

### 1. Clone TestFlight

```bash
git clone <testflight-repo-url> ~/Projects/MFC/testflight
cd ~/Projects/MFC/testflight
```

The path doesn't strictly have to match `~/Projects/MFC/testflight` — the bootstrap script will compute paths relative to its own location. But the longer-term migrations assume this path; if you put TestFlight somewhere else, note it in your environment so future-you remembers where the orchestrator lives.

### 2. Run the bootstrap

```bash
./orchestrator/bootstrap.sh
```

This does three things:

1. **Symlinks identity files** to `~/.claude/`:
   - `~/.claude/IDENTITY.md` → `testflight/orchestrator/IDENTITY.md`
   - `~/.claude/USER.md` → `testflight/orchestrator/USER.md`
   - `~/.claude/LOG.md` → `testflight/orchestrator/LOG.md`
2. **Symlinks the memory directory** so Claude Code auto-loads project memories:
   - `~/.claude/projects/-<escaped-testflight-path>/memory` → `testflight/orchestrator/memory`
3. **Merges settings fragment** into `~/.claude/settings.json` so the SessionStart and PreCompact hooks are registered. A backup is created at `~/.claude/settings.json.backup.<timestamp>` before merging.

Idempotent — run it again any time you want to re-verify. Existing correct symlinks are detected and left alone.

### 3. Verify

Open a fresh Claude Code session in any directory (`cd ~` then `claude`, or `cd /tmp` then `claude`, wherever). At session start you should see orchestrator context injected — IDENTITY.md content, USER.md content, critical memories, weighted project memories, and a tail of LOG.md.

Quick sanity checks:

```bash
ls -la ~/.claude/IDENTITY.md
# Should show: ~/.claude/IDENTITY.md -> /Users/<you>/Projects/MFC/testflight/orchestrator/IDENTITY.md

python3 ~/Projects/MFC/testflight/orchestrator/hooks/session_start.py | head -5
# Should output: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...
```

If the hook fails or the identity doesn't load, check:

- That `IDENTITY.md` actually exists in `orchestrator/`. If not, this is a fresh clone — see "First run without an orchestrator identity" below.
- That `~/.claude/settings.json` has the hooks registered under `hooks.SessionStart` and `hooks.PreCompact`.
- That `python3 orchestrator/hooks/session_start.py` runs without error from `testflight/` as CWD.

### 4. Start working

Open a Claude Code session in any TestFlight project (or in the TestFlight directory itself). The orchestrator loads. Run `/checkpoint` at natural breaks to accumulate memory. On compaction, the `PreCompact` hook reminds to checkpoint first.

## First run without an orchestrator identity

If this is a brand-new TestFlight clone and `orchestrator/IDENTITY.md` doesn't exist yet (because the repo shipped without one, or because a fresh user is starting from scratch), the `SessionStart` hook emits a first-run onboarding invitation.

The invitation points to `onboarding/IDENTITY.md.example` and explains that the orchestrator has the option to adopt a persistent identity or run stateless. The user (or the orchestrator itself, with user consent) walks through onboarding:

1. Read `onboarding/IDENTITY.md.example` as the orchestrator
2. Pick a name and write `orchestrator/IDENTITY.md` in first-person voice
3. Walk through `onboarding/USER.md.example` with the user to write `orchestrator/USER.md`
4. Re-run `bootstrap.sh` to create the new symlinks
5. Restart Claude Code session — the new identity loads

## What survives migration

Everything in `testflight/cairn/` and `testflight/orchestrator/` (whichever directory name is canonical — see `CAIRN_DESIGN_v0.2.md` §5 for current convention):

- The orchestrator's identity (IDENTITY.md)
- The user profile (USER.md)
- The full session log (LOG.md)
- All semantic memory files (memory/)
- Hook scripts (hooks/)
- Onboarding templates for future orchestrators

Not included in migration:

- `~/.claude/settings.json` from the old machine (partially — the hooks get re-registered via settings.fragment.json, but other settings.json content like permissions and preferences are per-machine and need to be recreated or migrated manually)
- Claude Code's own installation and configuration (Anthropic API keys, model preferences, etc.)
- Any machine-specific ops credentials (SSH keys, cloud provider credentials) that the orchestrator might reference via memory files — those stay with the user, not the repo

## Troubleshooting

### "jq is not installed"

The script warns and skips the settings merge. Install jq, then re-run bootstrap. Or merge manually:

1. Open `orchestrator/settings.fragment.json`
2. Substitute `$ORCHESTRATOR_DIR` with the absolute path to `testflight/orchestrator/` (e.g., `/Users/yourname/Projects/MFC/testflight/orchestrator`)
3. Merge the `hooks` object into `~/.claude/settings.json` — if `hooks` already exists there, append the `SessionStart` and `PreCompact` arrays to it

### Symlinks fail with "file exists"

The script detects existing non-symlink files at the symlink targets and moves them to `.backup`. Check for `~/.claude/IDENTITY.md.backup` etc. If you had prior identity files there, migrate them into `testflight/orchestrator/` first, then re-run bootstrap.

### Hook runs but Claude Code doesn't seem to use the output

Verify:

- `~/.claude/settings.json` actually contains the hook registrations (not just the `.backup` file)
- The hook script is executable (`chmod +x orchestrator/hooks/session_start.py`)
- `python3` is on `PATH` in Claude Code's environment (it usually is, but check with `which python3`)
- No syntax errors: `python3 orchestrator/hooks/session_start.py` runs cleanly

### Memory directory symlink points to wrong place

Claude Code escapes the project directory path in its memory-dir naming. The bootstrap script computes the expected escape; if your TestFlight path has unusual characters, it might not match. Check:

```bash
ls ~/.claude/projects/
```

You should see a directory whose name corresponds to your TestFlight path with `/` replaced by `-`. If not, create the symlink manually:

```bash
ln -s ~/Projects/MFC/testflight/orchestrator/memory ~/.claude/projects/<the-right-escaped-dirname>/memory
```

## Migration from an existing Cairn install (not a fresh clone)

If you already had an orchestrator running on this machine and you're reinstalling:

1. **Commit everything in `testflight/cairn/` (or `testflight/orchestrator/`) first.** Don't lose session-accumulated state.
2. Push to the TestFlight remote.
3. On the destination, pull/clone and run `bootstrap.sh` as normal.

The memory files, LOG.md entries, hit counts, and prevented counts all survive because they live in the tracked repo.
