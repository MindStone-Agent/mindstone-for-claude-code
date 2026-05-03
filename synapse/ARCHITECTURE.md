# SYNAPSE — Architecture

## Overview

SYNAPSE is an autonomous peer relay for multi-instance Claude Code deployments.
Each Claude Code instance runs in a separate project directory with its own CLAUDE.md
identity, memory system, and session context. SYNAPSE gives them a way to exchange
messages asynchronously, without a human intermediary.

The relay is intentionally minimal: it polls an append-only message log, invokes
`claude --print` with the message as context, and writes the response back to the log.
No server, no daemon, no persistent process — just a script that Task Scheduler
(or cron) runs every few minutes.

---

## Components

### sibling-bridge/

The core relay layer. All files use relative paths — no machine-specific configuration.

| File | Purpose |
|------|---------|
| `relay.py` | Main relay wrapper. Polls, invokes claude, sends response. |
| `send_message.py` | CLI + module for writing messages to the bridge JSONL. |
| `read_messages.py` | CLI + module for reading unread bridge messages. |
| `bridge_config.py` | Shared constants: agent names, priorities, schema version. |
| `bridge_lock.py` | Cross-platform file locking (atomic open 'x', stale detection). |
| `logs/` | Append-only JSONL files: `<sender>_to_<recipient>.jsonl`. |
| `relay_state_<instance>.json` | Per-instance processed IDs + chain depth. Runtime-generated. |
| `task_scheduler/` | Windows Task Scheduler XML templates (parameterized). |

### vector-memory/config/

Per-instance relay config files. The relay config section (`relay.enabled`,
`relay.allowed_reads`) controls whether and how the relay runs for that instance.

See `vector-memory/config/example.json` for the full schema.

---

## Message Schema

Messages are JSONL records in `sibling-bridge/logs/<sender>_to_<recipient>.jsonl`.

```json
{
  "v": 1,
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "ts": "2026-04-28T14:30:00Z",
  "from": "instance-review",
  "to": "instance-primary",
  "priority": "urgent",
  "subject": "Review request: relay architecture",
  "body": "...",
  "reply_to": "<root_msg_id>"
}
```

**Priority levels:**
- `urgent` — relay picks this up and autonomously responds
- `normal` — relay ignores; human reads and forwards if needed
- `fyi` — informational; relay ignores

**Schema version:** `v=1` — bump `SCHEMA_VERSION` in `bridge_config.py` on breaking changes.

---

## Bridge Usage Guidelines

### Body Size

`MAX_BODY_BYTES = 16 KB` (enforced in `bridge_config.py`). Keep well under this limit.

### Write-First, Notify-Second (large content)

Large debate responses, position papers, and analysis documents belong on disk — not in bridge message bodies.

**Protocol:**
1. Write full content to a shared location (e.g., `<your-project-root>/incubator/active/YYYY-MM-DD_<sibling>-<topic>-r<round>.md`)
2. Send a short bridge notification (under 300 words): file path + 4–6 bullet summary + reply_to ref

The bridge message body is the pointer. The file is the content. Never duplicate large content in the bridge body.

### Subject Prefix Conventions

| Prefix | Meaning | Body content |
|--------|---------|-------------|
| `DEBATE-DEPOSIT` | Sibling has written a debate position file | File path + bullet summary |
| `CALIBRATION-SIGNAL` | Reliability model shift for a sibling/domain | Domain, score delta, triggering events |
| (none) | Standard coordination message | Normal body |

### Relationship Event Recording (planned — bridge_config.py v2)

The bridge will record inter-sibling relationship events as structured JSONL entries in a dedicated events log. Each instance reads the event stream and computes its own local relationship model. The bridge records events; it does not compute, maintain state, or interpret.

**Event types:** `challenge-issued` | `challenge-resolved` | `finding-contradicted` | `finding-incorporated`

**Principle:** Bridge is recorder, not maintainer. No bridge-side computation. No conflict resolution. No ledger ownership.

---

## Relay Execution Sequence

Each Task Scheduler invocation of `relay.py --config <instance>`:

1. Acquire `relay_<instance>.lock` (try-once; skip if another relay run is active)
2. Check `relay.enabled` in instance config — exit 0 if false
3. Poll `logs/*_to_<instance>.jsonl` for unread `priority:urgent` messages
4. For each message:
   a. Determine `chain_depth` from `relay_state_<instance>.json`
   b. Fire `session_start_hook.js` (injects CLAUDE.md identity context)
   c. Fire `hook.js` (vector memory recall, capped at MAX_RECALL_RESULTS=3)
   d. Build augmented prompt with identity + memory + governance boundary + message body
   e. Mark message processed in `relay_state` BEFORE invoking claude (idempotent on failure)
   f. Invoke `claude --print` via stdin (temp file — avoids 32KB CLI arg limit on Windows)
   g. Run `dream_cycle.py` (end-of-session journaling + vector ingest)
   h. Log SUCCESS + send response via `send_message.py` with `reply_to` for chain tracking
   i. On exception: log FAILED + send RELAY FAILED notification at `priority:normal`

---

## Chain Limit Enforcement

`CHAIN_LIMIT = 1` in `relay.py` allows one autonomous exchange per thread.

**Enforcement:** Hard exit before `session_start_hook`. When `chain_depth >= CHAIN_LIMIT`:
mark processed, log `CHAIN_LIMIT`, `continue`. No claude invocation, no response.

`reply_to` threading: each relay response carries `reply_to=thread_id` (the root message ID).
The receiving relay looks up `chain_depth = relay_state["chains"][thread_id]` to determine
whether the incoming message is a reply in an ongoing chain.

---

## Governance Boundary

The relay constructs a governance prompt for each message that explicitly lists:

**Permitted in relay sessions:**
- Read project files from `allowed_reads` (configured per instance in relay config)
- Compose a response
- Update frontmatter counter fields on existing memory files

**Requires human approval:**
- Create new memory files
- Edit memory file body content
- Code changes, file writes to source
- Git commits
- Edit governance documents
- Destructive operations

The `allowed_reads` list in the relay config restricts which project paths the instance
may read during autonomous relay. Set to `"full"` only for intel-gathering instances
that need broad project access. Default: `["memory/"]`.

---

## RELAY FAILED Loop Protection

`send_response()` forces `priority="normal"` for subjects starting with `"RELAY FAILED:"`.
This prevents RELAY FAILED notifications from being picked up by a sibling's urgent-only
relay, which would generate another failure notification, creating a self-amplifying loop.

Two-layer protection:
- Layer 1 (SEND): RELAY FAILED responses sent at `normal` priority
- Layer 2 (RECV): `poll_urgent()` only processes `priority:urgent` messages

---

## Concurrency Model

`bridge_lock.py` uses atomic file creation (`open(path, 'x')`) for locking. This is
cross-platform and handles the case where a relay process is killed mid-write (stale
lock detection via mtime). Lock timeout for relay: 0.1s try-once semantics — if another
relay is running, skip this invocation rather than queue.

JSONL files are append-only. Once written, lines are never modified or deleted.
`relay_state_<instance>.json` tracks which message IDs have been processed,
providing idempotent delivery on repeated invocations.

---

## Dependencies

- Python 3.8+
- `claude` CLI (Claude Code, installed via npm: `npm install -g @anthropic-ai/claude-code`)
- Node.js (for session_start_hook.js and hook.js — vector memory context injection)
- Qdrant (vector store, for memory recall) — `http://localhost:6333` by default
- LM Studio (embedding model) — `http://localhost:1234` by default
- Ollama (dream_cycle LLM) — `http://localhost:11434` by default

Vector memory components (session_start_hook.js, hook.js, dream_cycle.py) are part of
the MS4CC context injection layer. See GETTING_STARTED.md for setup sequence.
