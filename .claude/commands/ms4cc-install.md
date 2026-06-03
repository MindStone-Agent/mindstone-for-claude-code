---
description: Install / set up MS4CC (MindStone for Claude Code). Detects whether this is a direct MS4CC checkout (runs bootstrap) or a project that installs MS4CC as a dependency (runs the pinned installer).
---

# Install MS4CC

Set up MS4CC in this project. MS4CC runs in one of two topologies and the install
differs — **detect which one this is FIRST**, then follow the matching steps. If
neither matches cleanly, ask the user rather than guessing.

## Detect the topology

- **Consumer install** — MS4CC is a dependency of another project (e.g. TestFlight):
  the project root has a `.ms4cc-version` pin file **and** a `scripts/ms4cc-sync.sh`
  helper, and `orchestrator/` is gitignored (the engine is installed, not checked
  out). → section A.
- **Direct checkout** — this project *is* the `mindstone-for-claude-code` repo
  (`git remote -v` shows the MS4CC repo; `install.sh`, `orchestrator/`, `AGENTS.md`
  are tracked at the root). → section B.

## A. Consumer — install the pinned engine

1. Run the consumer's installer helper:
   ```bash
   bash scripts/ms4cc-sync.sh install
   ```
   With a `.ms4cc-version` pin present it reproduces that exact version; with no pin
   it installs latest `main`. It lays the engine into the gitignored `orchestrator/`
   and **never** overwrites your per-user files (`IDENTITY.md`, `USER.md`, `LOG.md`,
   `orchestrator/memory/`).
2. Report the resolved version it pinned to (the `ref=` line of `.ms4cc-version`) and
   confirm the wire-up (`bootstrap.sh`) ran.

## B. Direct checkout — wire up

You already have the code (you cloned the repo). "Install" here means wiring it into
Claude Code:

1. Run the bootstrapper:
   ```bash
   ./orchestrator/bootstrap.sh
   ```
   It creates the Python venv, symlinks `IDENTITY.md` / `USER.md` / `LOG.md` and the
   memory dir into `~/.claude/`, merges the hook registrations into
   `~/.claude/settings.json`, and backfills the vector index. It does **not** touch
   your identity or memory content.
2. If there's no `orchestrator/IDENTITY.md` yet, the SessionStart first-run
   invitation walks you through adopting an identity (`onboarding/IDENTITY.md.example`).

## After installing (both topologies)

A **Claude Code restart** loads the hooks and surfaces the MS4CC slash commands in
the menu (the menu is built at process launch). See `onboarding/GETTING_STARTED.md`
for the update process and where slash commands live.
