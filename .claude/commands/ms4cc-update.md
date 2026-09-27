---
description: Update MS4CC (MindStone for Claude Code) to a newer version. Detects topology — a direct checkout updates with git pull; a consumer install syncs to a new pin.
---

# Update MS4CC

Bring this MS4CC installation up to date. The update differs by topology —
**detect which one this is FIRST**, then follow the matching steps. If neither
matches cleanly, ask the user rather than guessing.

`$ARGUMENTS` may name a specific version to move to — a commit SHA, a tag, or a
branch. If omitted, it updates to the latest on the default line.

## Detect the topology

- **Consumer install** — the project root has a `.ms4cc-version` pin file **and** a
  `scripts/ms4cc-sync.sh` helper, `orchestrator/` gitignored. → section A.
- **Direct checkout** — this project *is* the `mindstone-for-claude-code` repo
  (`git remote -v` shows the MS4CC repo; `install.sh` and `orchestrator/` tracked).
  → section B.

## A. Consumer install — sync to a new pin

1. Run the sync helper, passing the requested version if one was given:
   ```bash
   bash scripts/ms4cc-sync.sh update $ARGUMENTS
   ```
   (Empty `$ARGUMENTS` moves to latest `main`.) It overwrites the framework engine,
   slash commands, and `AGENTS.md`; your per-user files are never touched.
2. Report the previous vs new pin (compare the "current pin" line the helper prints
   against the new `ref=` in `.ms4cc-version`).
3. **Commit the updated `.ms4cc-version`** to the consumer repo so the new pin is
   recorded (the engine files are gitignored and not committed).

## B. Direct checkout — git pull

1. Pull the new code:
   ```bash
   git pull                                  # latest on the current branch
   # or, to move to a specific ref:
   # git fetch origin && git checkout $ARGUMENTS
   ```
   If `git pull` fails with "refusing to merge unrelated histories", this clone predates the 0.5.0 history rewrite: back up `orchestrator/memory/` and local edits, then `git fetch origin --tags --force && git reset --hard origin/main` in this checkout (don't re-clone).
   Then restore `MEMORY.md` from the backup and follow the remaining CHANGELOG 0.5.0 upgrade steps (seed watermarks in this same turn, re-run bootstrap, restart, then rescrub).
   The hooks are wired in `~/.claude/settings.json` at this repo's path, so a pull
   updates the live hook **code** in place — there is no copy step.
2. **Decide whether to re-run bootstrap.** If the pull added/removed a hook file or
   changed `orchestrator/settings.fragment.json`, re-wire settings:
   ```bash
   ./orchestrator/bootstrap.sh
   ```
   `bootstrap.sh` is idempotent and never touches your identity/memory — when unsure,
   run it. (To check: `git diff --name-status <old>..HEAD -- orchestrator/hooks orchestrator/settings.fragment.json`.)
3. Report the previous vs new commit (`git log --oneline -1`).

## After updating (both topologies)

- A **Claude Code restart** is needed for updated hooks to take effect — the running
  session loaded the old ones at startup. New slash commands appear in the menu only
  after a fresh launch from the project directory.
- **Migrating a box that predates the incremental-indexing fix (#47/#48):** if you
  have legacy dated transcript archives (`YYYY-MM-DD__<uuid>.jsonl`) in
  `orchestrator/transcripts/`, they won't auto-migrate to the stable-name scheme. One
  time: back up `orchestrator/vectors.db`, collapse each session's dated copies to a
  single stable `<uuid>.jsonl` (keep the largest/highest-line copy, delete the rest),
  then run `orchestrator/hooks/indexer.py backfill` to embed the tails. See
  `onboarding/GETTING_STARTED.md` → "Updating MS4CC".
