# SYNAPSE — Getting Started

This guide walks through setting up SYNAPSE for a two- or three-instance Claude Code
deployment. Complete each section before moving to the next.

---

## Prerequisites

Before starting, you need:

- [ ] Two or more Claude Code projects, each with their own CLAUDE.md
- [ ] Python 3.8+ on PATH
- [ ] `claude` CLI installed: `npm install -g @anthropic-ai/claude-code`
- [ ] Node.js (for context injection hooks)
- [ ] Qdrant running locally (vector store for memory recall)
- [ ] LM Studio running with `nomic-embed-text-v1.5` (embedding model)
- [ ] Ollama running with `llama3.1:8b` (dream_cycle summarization model)

The last three are part of the MS4CC context injection layer. If you are not using
vector memory (session recall), the relay will still work — it will log HOOK_WARN for
empty context but continue normally.

---

## Step 1: Choose Your Agent Names

Pick names for your instances. These are the identities your Claude instances will use
when addressing each other.

Example: `alpha`, `beta`, `gamma`

Edit `sibling-bridge/bridge_config.py`:

```python
VALID_AGENTS = {"alpha", "beta", "gamma"}
```

---

## Step 2: Place the Bridge

Put the `sibling-bridge/` directory somewhere accessible from all your project machines
(or a shared network path). All instances read and write to the same `logs/` folder.

If all instances run on the same machine, a single shared path works:

```
/path/to/shared/synapse/sibling-bridge/
  bridge_config.py
  bridge_lock.py
  relay.py
  send_message.py
  read_messages.py
  logs/           ← message files live here (gitignored)
  relay_state_*.json  ← per-instance state (gitignored)
```

---

## Step 3: Configure Each Instance

Copy `vector-memory/config/example.json` to a new file named after your instance:

```
vector-memory/config/alpha.json
vector-memory/config/beta.json
```

Fill in all fields. The critical relay section:

```json
"relay": {
  "enabled": false,
  "allowed_reads": [
    "memory/",
    "docs/"
  ]
}
```

Leave `enabled: false` until verification is complete (Step 6).

`allowed_reads` controls which project paths the relay can read during autonomous
sessions. Start restrictive (`memory/` only) and expand as needed.

---

## Step 4: Update relay.py CONFIG_NAME_MAP

In `sibling-bridge/relay.py`, update `CONFIG_NAME_MAP` to match your instance names
and config filenames:

```python
CONFIG_NAME_MAP = {
    "alpha": "alpha",
    "beta": "beta",
    "gamma": "gamma",
}
```

Also update the `--config` choices in `parser.add_argument`.

---

## Step 5: Set Up Task Scheduler (Windows)

For each instance, create a scheduled task:

1. Copy `sibling-bridge/task_scheduler/relay_INSTANCE.xml.template`
2. Rename to `relay_alpha.xml`
3. Open in a text editor and replace all placeholders:
   - `PLACEHOLDER_INSTANCE` → `alpha`
   - `PLACEHOLDER_RELAY_PATH` → full path to `relay.py`
     e.g. `C:\AI-Framework\SYNAPSE\sibling-bridge\relay.py`
   - `PLACEHOLDER_WORKING_DIR` → your project root
     e.g. `C:\Projects\alpha-project`
4. Import: `schtasks /create /xml relay_alpha.xml /tn "SYNAPSE Relay -- ALPHA"`

The task is created DISABLED. Do not enable until Step 6.

**On Linux/macOS:** Use cron instead:
```
*/3 * * * * cd /path/to/project && python /path/to/relay.py --config alpha
```

---

## Step 6: Smoke Test (Before Enabling Relay)

Run this sequence manually before enabling any scheduled tasks.

### 6a. Send a test message (from instance beta to alpha):

```bash
python sibling-bridge/send_message.py \
  --from beta \
  --to alpha \
  --subject "Relay smoke test" \
  --body "This is a test message. Please acknowledge." \
  --priority urgent
```

### 6b. Trigger relay for alpha manually:

```bash
python sibling-bridge/relay.py --config alpha
```

### 6c. Check the relay log:

```bash
tail sibling-bridge/relay.log
```

Expected: `{"status": "SUCCESS", ...}` entry.

### 6d. Check for response:

```bash
python sibling-bridge/read_messages.py --agent beta
```

Expected: response message from ALPHA.

If the smoke test passes, proceed to Step 7.

---

## Step 7: Enable Scheduled Tasks

Set `relay.enabled: true` in each instance's config file.

Enable the Task Scheduler tasks:
```
schtasks /change /tn "SYNAPSE Relay -- ALPHA" /enable
```

Monitor `sibling-bridge/relay.log` for the first scheduled invocations.

---

## Step 8: Verify Chain Limit Enforcement

This release includes the chain limit hard exit fix (applied post-live-deployment).
A 31-cycle relay loop occurred in early deployment before this fix — see OBSERVATIONS.md F002.

The hard exit block is present in `relay.py` before the `try:` at the start of message processing.
If you are adapting `relay.py` from an older source, verify this block exists:

```python
if chain_depth >= CHAIN_LIMIT:
    state.setdefault('processed', {})[msg_id] = datetime.now(timezone.utc).isoformat()
    save_state(instance, state)
    log_relay(msg_id, instance, sender, 'CHAIN_LIMIT',
              f'thread={thread_id} depth={chain_depth} -- skipped')
    continue
```

See OBSERVATIONS.md F002 for incident details.

---

## Governance: What the Relay Can and Cannot Do

The relay constructs a governance prompt that explicitly restricts autonomous behavior.
Review `build_prompt()` in `relay.py` and customize the NOT PERMITTED list for your
deployment. The defaults are conservative — expand only with intent.

The `allowed_reads` list in each instance config is your primary tool for scoping
what project files each instance can access during autonomous relay sessions.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| No relay.log entries | Is `relay.enabled: true` in config? Is Task Scheduler task enabled? |
| HOOK_WARN entries | Is Qdrant running? Is the embedding model loaded in LM Studio? |
| SEND_FAILED entries | Is `send_message.py` path correct? Is `logs/` writable? |
| relay.log shows FAILED | Read the `note` field — it contains the exception message |
| Lock file stale | Delete `relay_<instance>.lock` manually (stale_after=600s should handle this) |
| Empty relay response | Check Claude CLI is on PATH: `claude --version` |
