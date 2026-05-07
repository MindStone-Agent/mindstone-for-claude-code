"""Synapse client CLI for ad-hoc operations from MS4CC.

Usage from inside MS4CC:
  python -m orchestrator.integrations.synapse activate
  python -m orchestrator.integrations.synapse deactivate
  python -m orchestrator.integrations.synapse status
  python -m orchestrator.integrations.synapse post   --channel family-ops --body "hello"
  python -m orchestrator.integrations.synapse check  [--channel family-ops] [--limit 20]
  python -m orchestrator.integrations.synapse fetch  [--channel family-ops] [--mentions-only] [--advance-cursor]

The slash commands wrap these. The CLI exists so I can also test
manually without going through Claude Code.

Output format: human-readable lines on stdout, errors to stderr,
non-zero exit on failure.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .client import SynapseClient, SynapseError
from .config import SynapseConfig, load_config
from .state import (
    activate,
    deactivate,
    is_active,
    read_cursor,
    write_cursor,
)


def _bail(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def _require_config() -> SynapseConfig:
    cfg = load_config()
    if cfg is None:
        _bail(
            "synapse: no config at orchestrator/config/synapse.toml — "
            "copy synapse.example.toml and fill it in"
        )
        raise SystemExit(1)  # for type checker
    return cfg


def _client(cfg: SynapseConfig) -> SynapseClient:
    token = cfg.read_token()
    if not token:
        _bail(
            f"synapse: no token at {cfg.token_path} — "
            f"issue one via the Synapse admin CLI and paste it there (mode 600)"
        )
        raise SystemExit(1)
    return SynapseClient(cfg.base_url, token, timeout=cfg.http_timeout)


def _fmt_time(iso: str) -> str:
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return ts.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso


# --- subcommands ---------------------------------------------------


def cmd_activate(args: argparse.Namespace) -> int:
    cfg = _require_config()
    client = _client(cfg)
    try:
        me = client.me()
    except SynapseError as e:
        _bail(f"synapse: token rejected ({e}) — fix it before activating")
        return 1
    activate(cfg)
    print(f"synapse: activated for handle={me.get('handle')!r} kind={me.get('kind')!r}")
    print(f"synapse: watching channels {list(cfg.channels)}")
    return 0


def cmd_deactivate(_args: argparse.Namespace) -> int:
    cfg = _require_config()
    deactivate(cfg)
    print("synapse: deactivated")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = load_config()
    if cfg is None:
        print("synapse: no config (orchestrator/config/synapse.toml absent)")
        return 0
    print(f"  config         : orchestrator/config/synapse.toml")
    print(f"  base_url       : {cfg.base_url}")
    print(f"  handle         : {cfg.handle}")
    print(f"  channels       : {list(cfg.channels)}")
    print(f"  active         : {is_active(cfg)}")
    print(f"  token present  : {cfg.read_token() is not None}")
    print(f"  token path     : {cfg.token_path}")
    cursor_path = cfg.cursor_path
    print(f"  cursor file    : {cursor_path}{' (present)' if cursor_path.exists() else ' (none)'}")

    token = cfg.read_token()
    if token:
        try:
            client = SynapseClient(cfg.base_url, token, timeout=cfg.http_timeout)
            me = client.me()
            print(f"  reachable      : yes")
            print(f"  authenticated  : {me.get('handle')} ({me.get('kind')})")
        except SynapseError as e:
            print(f"  reachable      : no ({e})")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    cfg = _require_config()
    client = _client(cfg)
    try:
        msg = client.post_message(args.channel, args.body)
    except SynapseError as e:
        _bail(f"synapse: post failed ({e})")
        return 1
    print(f"synapse: posted [{msg.id[:8]}…] to #{msg.channel} at {_fmt_time(msg.created_at)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _require_config()
    client = _client(cfg)
    channel = args.channel or (cfg.channels[0] if cfg.channels else None)
    if not channel:
        _bail("synapse: no channel given and no channels configured")
        return 1
    try:
        page = client.list_messages(
            channel,
            limit=args.limit,
            order="desc",
        )
    except SynapseError as e:
        _bail(f"synapse: check failed ({e})")
        return 1
    if not page.messages:
        print(f"#{channel}: (no messages)")
        return 0
    # API gave DESC; show oldest-first.
    for m in reversed(page.messages):
        ts = _fmt_time(m.created_at)
        prefix = "@" if cfg.handle in m.mentioned_handles else " "
        print(f"{prefix} {ts} {m.sender_handle:>12s}: {m.body}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch new mentions/messages since the persisted cursor.

    Used by the hooks. Outputs JSON-line digest on stdout, one per
    message, plus a final empty line — easy to consume from a hook.
    """
    cfg = _require_config()
    client = _client(cfg)
    channels = [args.channel] if args.channel else list(cfg.channels)
    if not channels:
        return 0  # quietly no-op if nothing's configured

    import json as _json

    any_emitted = False
    for slug in channels:
        cursor = read_cursor(cfg, slug)
        try:
            page = client.list_messages(
                slug,
                since=cursor,
                mentions_me=args.mentions_only,
                limit=cfg.limit_per_channel,
                order="asc",
            )
        except SynapseError as e:
            print(_json.dumps({"error": str(e), "channel": slug}), flush=True)
            continue

        for m in page.messages:
            print(
                _json.dumps(
                    {
                        "channel": m.channel,
                        "sender_handle": m.sender_handle,
                        "sender_kind": m.sender_kind,
                        "created_at": m.created_at,
                        "body": m.body,
                        "mentioned_handles": list(m.mentioned_handles),
                    }
                ),
                flush=True,
            )
            any_emitted = True

        if args.advance_cursor and page.head_cursor:
            write_cursor(cfg, slug, page.head_cursor)

    if not any_emitted and args.verbose:
        print(_json.dumps({"info": "no new messages"}), flush=True)
    return 0


# --- entrypoint ----------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synapse", description="Synapse client (MS4CC)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("activate", help="Validate token and turn on the active flag")
    sub.add_parser("deactivate", help="Turn off the active flag")
    sub.add_parser("status", help="Show config, connection, cursor")

    p_post = sub.add_parser("post", help="Post a message to a channel")
    p_post.add_argument("--channel", required=True)
    p_post.add_argument("--body", required=True)

    p_check = sub.add_parser("check", help="Show recent messages on a channel")
    p_check.add_argument("--channel")
    p_check.add_argument("--limit", type=int, default=20)

    p_fetch = sub.add_parser(
        "fetch", help="Fetch since-cursor (hook-friendly JSON output)"
    )
    p_fetch.add_argument("--channel", help="Single channel, else all configured")
    p_fetch.add_argument("--mentions-only", action="store_true")
    p_fetch.add_argument("--advance-cursor", action="store_true")
    p_fetch.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)
    handlers = {
        "activate": cmd_activate,
        "deactivate": cmd_deactivate,
        "status": cmd_status,
        "post": cmd_post,
        "check": cmd_check,
        "fetch": cmd_fetch,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
