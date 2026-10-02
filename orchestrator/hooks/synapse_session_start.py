#!/usr/bin/env python3
"""SessionStart hook — Synapse greeting digest.

If Synapse is configured AND active, fetch recent `@`-mentions across
the configured channels and inject a short digest as additionalContext.
This is the agent's "what did I miss while I was away" greeting.

Fail-soft in every direction: missing config / missing token / network
trouble all → exit 0 with no output. The orchestrator should never
crash on a Synapse hiccup.

Activation:
  - orchestrator/config/synapse.toml present + parseable
  - ~/.synapse/<handle>.token present (mode 600)
  - ~/.synapse/<handle>.active present (touched by /synapse-activate)
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HOOK_FILE = Path(__file__).resolve()
ORCHESTRATOR_DIR = HOOK_FILE.parent.parent

# Make `orchestrator.integrations.synapse` importable regardless of CWD.
sys.path.insert(0, str(ORCHESTRATOR_DIR.parent))


def _emit(context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
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
            write_cursor,
        )
        from orchestrator.integrations.synapse.state import (  # type: ignore
            load_session_cursors,
            read_session_id_from_stdin,
            write_session_cursors,
        )

        cfg = load_config()
        if cfg is None:
            return 0
        if not is_active(cfg):
            return 0
        token = cfg.read_token()
        if not token:
            return 0
        if not cfg.channels:
            return 0

        client = SynapseClient(cfg.base_url, token, timeout=cfg.http_timeout)

        # With a session id, advance THIS session's cursor, not the shared one:
        # moving the shared cursor here would hide mentions from every other
        # open session that has not read its first prompt yet. Without one, the
        # shared cursor is advanced as before.
        session_id = read_session_id_from_stdin()
        session_cursors = load_session_cursors(cfg, session_id) if session_id else {}

        lines: list[str] = []
        per_channel_blocks: list[str] = []
        for slug in cfg.channels:
            try:
                page = client.list_messages(
                    slug,
                    mentions_me=True,
                    limit=cfg.limit_per_channel,
                    order="desc",
                )
            except SynapseError as e:
                # Print a small breadcrumb to stderr; don't surface to model.
                print(f"[synapse_session_start] {slug}: {e}", file=sys.stderr)
                continue

            if not page.messages:
                continue

            block_lines: list[str] = [f"## #{slug} — {len(page.messages)} unread mention(s)"]
            # API returned DESC; show oldest first within the digest.
            for m in reversed(page.messages):
                block_lines.append(
                    f"- [{m.created_at}] **{m.sender_handle}**: {m.body}"
                )
            per_channel_blocks.append("\n".join(block_lines))

            # Advance cursor so subsequent UserPromptSubmit hooks don't
            # re-surface these messages.
            if page.head_cursor:
                if session_id:
                    previous = session_cursors.get(slug)
                    session_cursors[slug] = page.head_cursor
                    if not write_session_cursors(cfg, session_id, session_cursors):
                        # Not saved: roll back so the mentions repeat.
                        if previous is None:
                            session_cursors.pop(slug, None)
                        else:
                            session_cursors[slug] = previous
                else:
                    write_cursor(cfg, slug, page.head_cursor)

        if not per_channel_blocks:
            return 0

        body = "\n\n".join(per_channel_blocks)
        wrapped = (
            "<synapse-digest>\n"
            "# Synapse — unread mentions while you were away\n\n"
            f"{body}\n"
            "</synapse-digest>"
        )
        _emit(wrapped)
        return 0
    except Exception:
        # Last-resort: if anything explodes, log to stderr and exit 0.
        traceback.print_exc(file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
