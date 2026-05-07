---
description: Connect to Synapse and surface unread @-mentions in this session.
---

# /synapse-activate

Run the Synapse activation primitive — touches `~/.synapse/<handle>.active`,
validates the bearer token, and pulls any unread `@`-mentions across
configured channels so they're surfaced in this session even though
SessionStart already ran.

This command assumes the Claude Code working directory is the MS4CC
repo root (which it is, when CC is launched from there).

Steps:

1. Activate:

   ```bash
   ./orchestrator/.venv/bin/python -m orchestrator.integrations.synapse activate
   ```

   If it fails (no config, bad token, network), report the error to me
   directly and stop — don't try to "make it work."

2. Then fetch unread mentions and advance the cursor so the
   UserPromptSubmit hook doesn't re-surface them on the next turn:

   ```bash
   ./orchestrator/.venv/bin/python \
     -m orchestrator.integrations.synapse fetch --advance-cursor --mentions-only --verbose
   ```

3. Surface a brief digest of any fetched mentions (sender / channel /
   one-line body each), and ask me how I'd like to handle them. If
   there were none, just say so.

After this, the UserPromptSubmit hook will surface any new mentions on
subsequent turns automatically until I run `/synapse-deactivate`.
