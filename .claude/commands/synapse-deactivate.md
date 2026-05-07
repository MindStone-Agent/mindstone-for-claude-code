---
description: Stop surfacing Synapse mentions in this session.
---

# /synapse-deactivate

Removes `~/.synapse/<handle>.active`. The UserPromptSubmit hook will
no-op until reactivation.

Run from the MS4CC repo root:

```bash
./orchestrator/.venv/bin/python -m orchestrator.integrations.synapse deactivate
```

Confirm the result back to me in one short line.
