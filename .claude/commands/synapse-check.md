---
description: Show recent messages on a Synapse channel.
argument-hint: [channel]
---

# /synapse-check

Pull the most recent ~20 messages on a channel for a quick read-without-
posting view. Defaults to the first configured channel if no argument
is given.

Run from the MS4CC repo root (with `--channel <slug>` if `$ARGUMENTS` is
non-empty, else no flag):

```bash
./orchestrator/.venv/bin/python \
  -m orchestrator.integrations.synapse check $ARGS_AS_FLAGS
```

Report the output verbatim — the CLI already formats it for reading.

Arguments: $ARGUMENTS
