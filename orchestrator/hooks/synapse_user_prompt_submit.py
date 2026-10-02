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

# Per-session cursors: see integrations/synapse/state.py. Each Claude Code
# session keeps its own cursor so one session consuming a mention does not hide
# it from the others. This hook never writes the shared cursor when a session id
# is present; a prompt whose stdin yields no session id falls back to the shared
# cursor for that prompt and writes it.


def _read_session_id() -> str | None:
    """The Claude Code session id from this hook's stdin JSON, or None (see state.py)."""
    from orchestrator.integrations.synapse.state import read_session_id_from_stdin  # type: ignore

    return read_session_id_from_stdin()


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
        from orchestrator.integrations.synapse.state import (  # type: ignore
            load_session_cursors,
            prune_session_cursors,
            touch_session_cursors,
            write_session_cursors,
        )

        cfg = load_config()
        if cfg is None or not is_active(cfg):
            return 0
        token = cfg.read_token()
        if not token:
            return 0

        client = SynapseClient(cfg.base_url, token, timeout=cfg.http_timeout)

        session_id = _read_session_id()
        session_cursors: dict[str, str] = {}
        if session_id:
            session_cursors = load_session_cursors(cfg, session_id)
            # Touch this session's file on every run so only sessions that have
            # actually gone unused for SESSION_CURSOR_MAX_AGE_S are pruned, not
            # a live session that simply received no mentions.
            touch_session_cursors(cfg, session_id)
            prune_session_cursors(cfg)

        # Channel discovery: ask the server which channels this account is
        # a member of, rather than relying on a hand-maintained `channels`
        # list. If `cfg.channels` is set in synapse.toml, it acts as a
        # filter (intersection with memberships). If empty/unset, the
        # digest covers every channel the account belongs to.
        #
        # Lift of MS4CC #30: removes the manual-toml-edit step from
        # "admin adds me to a new channel" so the digest picks the new
        # channel up on the next prompt automatically.
        try:
            membership_rows = client.list_channels()
        except SynapseError as e:
            print(f"[synapse_user_prompt_submit] list_channels: {e}", file=sys.stderr)
            return 0

        membership_slugs = tuple(
            row["slug"]
            for row in membership_rows
            if isinstance(row, dict) and row.get("slug")
        )
        if cfg.channels:
            # Filter mode: keep only configured channels we're actually a member of.
            configured = {slug.lower() for slug in cfg.channels}
            slugs_to_poll: tuple[str, ...] = tuple(
                s for s in membership_slugs if s.lower() in configured
            )
        else:
            slugs_to_poll = membership_slugs

        if not slugs_to_poll:
            return 0

        per_channel_blocks: list[str] = []
        seeded = False
        for slug in slugs_to_poll:
            # This session's own cursor if it has one. Otherwise seed from the
            # shared cursor and PIN the seed into the session's file, so a later
            # move of the shared cursor by another writer cannot make this
            # session skip mentions. How recent the seed is depends on whatever
            # last advanced the shared cursor (SessionStart, CLI fetch); with no
            # shared entry the fetch has no lower bound.
            cursor = session_cursors.get(slug) if session_id else None
            if cursor is None:
                cursor = read_cursor(cfg, slug)
                if session_id and cursor is not None:
                    session_cursors[slug] = cursor
                    seeded = True
            try:
                # Digest scope is controlled by `digest_mentions_only` in
                # synapse.toml (default True). When True, only messages that
                # name this agent directly or via a broadcast the agent
                # belongs to (`@family`, `@channel`, etc.) surface. When
                # False, all recent channel traffic since the cursor surfaces,
                # bounded by `limit_per_channel`. The False mode is useful
                # when peer coordination context (acks, status updates
                # between other agents) is operationally relevant.
                page = client.list_messages(
                    slug,
                    since=cursor,
                    mentions_me=cfg.digest_mentions_only,
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
                if session_id:
                    previous = session_cursors.get(slug)
                    session_cursors[slug] = page.head_cursor
                    if not write_session_cursors(cfg, session_id, session_cursors):
                        # Not saved: roll back so a later channel's write cannot
                        # persist this channel's advance, and the mentions repeat.
                        if previous is None:
                            session_cursors.pop(slug, None)
                        else:
                            session_cursors[slug] = previous
                else:
                    write_cursor(cfg, slug, page.head_cursor)

        if session_id and seeded:
            write_session_cursors(cfg, session_id, session_cursors)

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
