---
description: Post a message to a Synapse channel.
argument-hint: <channel> <body>
---

# /synapse-post

Post a message via the Synapse REST API. Arguments come in as `$ARGUMENTS`
(first whitespace-separated token = channel slug, remainder = body).

Steps:

1. Parse `$ARGUMENTS`. If empty, ask me which channel and what to say —
   don't guess.
2. Run from the MS4CC repo root:

   ```bash
   ./orchestrator/.venv/bin/python \
     -m orchestrator.integrations.synapse post --channel "<slug>" --body "<body>"
   ```

3. Report the success line (id / channel / time) or the error verbatim.

Arguments: $ARGUMENTS
