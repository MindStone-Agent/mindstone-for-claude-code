#!/usr/bin/env python3
"""UserPromptSubmit hook — surface new Synapse mentions per turn.

Fires before each user turn alongside the semantic-recall hook. If
Synapse is active and there are new `@`-mentions since the persisted
cursor, emit a small digest as additionalContext. Otherwise, exit
silently (no output ⇒ no injection, leaving the semantic-recall block
untouched).

Cursor is advanced after a successful fetch so already-seen mentions
don't repeat on the next prompt.

Failsafe: missing config / missing token / inactive flag / network
trouble all → exit 0 with no output. Never break the prompt.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HOOK_FILE = Path(__file__).resolve()
ORCHESTRATOR_DIR = HOOK_FILE.parent.parent

sys.path.insert(0, str(ORCHESTRATOR_DIR.parent))


def _emit(context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )


def main() -> int:
    try:
        from orchestrator.integrations.synapse import (  # type: ignore
            SynapseClient,
            SynapseError,
            is_active,
            load_config,
            read_cursor,
            write_cursor,
        )

        cfg = load_config()
        if cfg is None or not is_active(cfg):
            return 0
        token = cfg.read_token()
        if not token or not cfg.channels:
            return 0

        client = SynapseClient(cfg.base_url, token, timeout=cfg.http_timeout)

        per_channel_blocks: list[str] = []
        for slug in cfg.channels:
            cursor = read_cursor(cfg, slug)
            try:
                page = client.list_messages(
                    slug,
                    since=cursor,
                    mentions_me=True,
                    limit=cfg.limit_per_channel,
                    order="asc",
                )
            except SynapseError as e:
                print(f"[synapse_user_prompt_submit] {slug}: {e}", file=sys.stderr)
                continue

            if not page.messages:
                continue

            block_lines: list[str] = [f"## #{slug}"]
            for m in page.messages:
                block_lines.append(
                    f"- [{m.created_at}] **{m.sender_handle}**: {m.body}"
                )
            per_channel_blocks.append("\n".join(block_lines))

            if page.head_cursor:
                write_cursor(cfg, slug, page.head_cursor)

        if not per_channel_blocks:
            return 0

        body = "\n\n".join(per_channel_blocks)
        wrapped = (
            "<synapse-digest>\n"
            "# Synapse — new mentions since last turn\n\n"
            f"{body}\n"
            "</synapse-digest>"
        )
        _emit(wrapped)
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
