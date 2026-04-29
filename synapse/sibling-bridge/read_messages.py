#!/usr/bin/env python3
"""Sibling bridge message reader.

Can be used as a CLI or imported as a module:
    from read_messages import read
    messages = read(agent="raven")
"""

import argparse
import json
import os
import sys
import tempfile

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)
import bridge_lock
from bridge_config import VALID_AGENTS

LOGS_DIR = os.path.join(BRIDGE_DIR, "logs")
STATE_DIR = os.path.join(BRIDGE_DIR, "state")


def _state_path(agent):
    return os.path.join(STATE_DIR, f"{agent}.json")


def _load_state(agent):
    path = _state_path(agent)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"agent": agent, "last_read": {}}


def _save_state(agent, state):
    """Atomic write via temp file + os.replace — survives interrupted writes."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _state_path(agent)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=f"{agent}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_log(log_path, from_line):
    """Read lines from a JSONL log under a shared read lock.

    Returns (messages, lines_scanned). lines_scanned is total lines attempted,
    regardless of parse success — used to advance last_read correctly.
    """
    if not os.path.exists(log_path):
        return [], 0

    lock = bridge_lock.acquire(log_path)
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    finally:
        bridge_lock.release(lock)

    target_lines = lines[from_line:]
    messages = []
    for line in target_lines:
        stripped = line.strip()
        if stripped:
            try:
                messages.append(json.loads(stripped))
            except json.JSONDecodeError:
                pass  # partial write — will be re-read next session

    return messages, len(target_lines)


def _format_message(msg):
    priority = msg.get("priority", "normal")
    if priority == "urgent":
        tag = "[SIBLING MESSAGE -- URGENT]"
    elif priority == "fyi":
        tag = "[SIBLING MESSAGE -- FYI]"
    else:
        tag = "[SIBLING MESSAGE]"
    msg_id = msg.get("id", "")
    id_suffix = f"  |  id={msg_id[:8]}" if msg_id else ""
    return (
        f"{tag}\n"
        f"From: {msg['from']}  |  {msg['ts']}{id_suffix}\n"
        f"Subject: {msg['subject']}\n"
        f"Body: {msg['body']}"
    )


def read(agent):
    """Read unread messages for agent. Updates state. Returns list of message dicts."""
    agent = agent.lower()
    if agent not in VALID_AGENTS:
        raise ValueError(f"Unknown agent: {agent!r}")

    state = _load_state(agent)
    last_read = state.get("last_read", {})
    new_last_read = dict(last_read)

    all_messages = []

    for sender in sorted(VALID_AGENTS - {agent}):
        log_key = f"{sender}_to_{agent}"
        path = os.path.join(LOGS_DIR, f"{log_key}.jsonl")
        from_line = last_read.get(log_key, 0)
        messages, lines_scanned = _read_log(path, from_line)
        if lines_scanned > 0:
            # Advance by lines scanned (not messages parsed) to avoid re-delivery
            # on partial-write lines.
            new_last_read[log_key] = from_line + lines_scanned
        if messages:
            all_messages.extend(messages)

    if all_messages:
        state["last_read"] = new_last_read
        _save_state(agent, state)

    return all_messages


def main():
    parser = argparse.ArgumentParser(description="Read unread sibling bridge messages.")
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()

    try:
        messages = read(agent=args.agent)
    except (ValueError, TimeoutError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if messages:
        print("<sibling-bridge>")
        for i, msg in enumerate(messages):
            print(_format_message(msg))
            if i < len(messages) - 1:
                print("---")
        print("</sibling-bridge>")


if __name__ == "__main__":
    main()
