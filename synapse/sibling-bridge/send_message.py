#!/usr/bin/env python3
"""Sibling bridge message sender.

Can be used as a CLI or imported as a module:
    from send_message import send
    send(sender="raven", recipient="warden", subject="...", body="...")
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)
import bridge_lock
from bridge_config import MAX_BODY_BYTES, SCHEMA_VERSION, VALID_AGENTS, VALID_PRIORITIES

LOGS_DIR = os.path.join(BRIDGE_DIR, "logs")


def _log_path(sender, recipient):
    return os.path.join(LOGS_DIR, f"{sender}_to_{recipient}.jsonl")


def _append_message(path, message):
    """Append one JSONL line under an exclusive lock."""
    lock = bridge_lock.acquire(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
    finally:
        bridge_lock.release(lock)


def send(sender, recipient, subject, body, priority="normal", reply_to=None):
    """Send a message. recipient may be 'all' to broadcast to every other agent."""
    sender = sender.lower()
    priority = priority.lower()

    if sender not in VALID_AGENTS:
        raise ValueError(f"Unknown sender: {sender!r}")
    if recipient != "all" and recipient.lower() not in VALID_AGENTS:
        raise ValueError(f"Unknown recipient: {recipient!r}")
    if recipient.lower() == sender:
        raise ValueError("sender and recipient must be different")
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"Unknown priority: {priority!r}")

    body_bytes = body.encode("utf-8")
    if len(body_bytes) > MAX_BODY_BYTES:
        raise ValueError(
            f"Body exceeds {MAX_BODY_BYTES // 1024}KB limit "
            f"({len(body_bytes)} bytes). Summarize before sending."
        )

    os.makedirs(LOGS_DIR, exist_ok=True)

    recipients = (VALID_AGENTS - {sender}) if recipient == "all" else {recipient.lower()}
    msg_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for dest in sorted(recipients):
        message = {
            "v": SCHEMA_VERSION,
            "id": msg_id,
            "ts": ts,
            "from": sender.upper(),
            "to": dest.upper() if recipient != "all" else "ALL",
            "priority": priority,
            "subject": subject,
            "body": body,
        }
        if reply_to:
            message["reply_to"] = reply_to
        _append_message(_log_path(sender, dest), message)

    label = "ALL" if recipient == "all" else recipient.upper()
    print(f"[bridge] sent: {sender.upper()} -> {label} [{priority}] {subject!r} (id={msg_id[:8]})")
    return msg_id


def main():
    parser = argparse.ArgumentParser(description="Send a message via the sibling bridge.")
    parser.add_argument("--from", dest="sender", required=True)
    parser.add_argument("--to", dest="recipient", required=True,
                        help="Agent name or 'all' to broadcast")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--priority", default="normal", choices=sorted(VALID_PRIORITIES))
    parser.add_argument("--reply-to", dest="reply_to", default=None)
    args = parser.parse_args()

    try:
        send(
            sender=args.sender,
            recipient=args.recipient,
            subject=args.subject,
            body=args.body,
            priority=args.priority,
            reply_to=args.reply_to,
        )
    except (ValueError, TimeoutError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
