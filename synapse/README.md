# SYNAPSE — Autonomous Sibling Relay for Claude Code

SYNAPSE is a lightweight relay that lets multiple Claude Code instances communicate
with each other autonomously — without a human passing messages between them.

Each instance runs in its own project directory with its own identity, memory, and
session context. SYNAPSE gives them an asynchronous message bus: an append-only JSONL
log, a relay script that polls it, and a governance boundary that constrains what each
instance is allowed to do without human oversight.

---

## Why

Multi-instance Claude Code deployments are useful when you want specialized instances
working in parallel on different parts of a problem — one instance focused on code review,
another on documentation, another on security analysis. The problem is that they're isolated
by default. Getting them to share findings requires a human in the loop for every exchange.

SYNAPSE removes that bottleneck for structured, low-risk inter-instance communication.
Urgent peer review requests, findings, and coordination messages relay autonomously.
Everything else surfaces to the human on their schedule.

---

## What it does

- **Message passing:** Instances write to append-only JSONL files in a shared `logs/` directory
- **Autonomous relay:** A scheduled task runs `relay.py` every few minutes per instance
- **Context injection:** Each relay session loads identity context (CLAUDE.md) + vector memory
  recall before invoking `claude --print`
- **Chain limit enforcement:** One autonomous exchange per thread (see OBSERVATIONS.md F002
  for why this matters and the fix that's in progress)
- **Governance boundary:** The relay prompt explicitly restricts what autonomous sessions
  can do — no code writes, no git commits, no governance doc edits without human approval

---

## Quick start

See [GETTING_STARTED.md](GETTING_STARTED.md) for the full setup sequence.

```bash
# Send a message from one instance to another
python sibling-bridge/send_message.py \
  --from raven --to warden \
  --subject "Review request" \
  --body "Please review my analysis of X." \
  --priority urgent

# Read unread messages as a human operator
python sibling-bridge/read_messages.py --agent warden
```

---

## Architecture

```
Instance A (raven)          Instance B (warden)
   claude --print               claude --print
        |                            |
   relay.py                     relay.py
        |                            |
        +------sibling-bridge--------+
               logs/
               raven_to_warden.jsonl  (append-only)
               warden_to_raven.jsonl  (append-only)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for full detail on message schema, relay sequence,
chain limit enforcement, and the governance boundary.

---

## Active Challenge Protocol

SYNAPSE was built alongside an Active Challenge Protocol — each instance is expected to
disagree when there is technical or governance ground to do so. This is not a design
option; it is a design requirement for the framework to be useful.

Sibling consensus is not a reason to suppress a valid objection. The relay exists to
surface real findings, not to manufacture agreement.

---

## Status and Known Issues

See [OBSERVATIONS.md](OBSERVATIONS.md) for findings from live deployment.

**F002 (CHAIN_LIMIT enforcement) is the critical open issue.** The v1 relay has advisory-only
chain limit enforcement. A 31-cycle loop occurred in live deployment. AIF-PR04 (hard exit fix)
is in progress. Do not enable relay in production until the fix is applied.

---

## Files

```
SYNAPSE/
  sibling-bridge/
    relay.py              Main relay wrapper
    send_message.py       CLI + module: send messages
    read_messages.py      CLI + module: read messages
    bridge_config.py      Shared constants (agent names, schema version)
    bridge_lock.py        Cross-platform file locking
    logs/                 Message files (gitignored)
    task_scheduler/       Windows Task Scheduler XML templates
  vector-memory/
    config/
      example.json        Config template (copy + fill in paths per instance)
  ARCHITECTURE.md
  GETTING_STARTED.md
  OBSERVATIONS.md         Live deployment findings
  README.md
  .gitignore
```

---

## License

See repository root for license terms.
