"""Synapse client CLI for ad-hoc operations from MS4CC.

Usage from inside MS4CC:
  python -m orchestrator.integrations.synapse setup
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
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import SynapseClient, SynapseError
from .config import (
    CONFIG_PATH,
    HOME_SYNAPSE,
    SynapseConfig,
    ensure_synapse_dir,
    load_config,
)
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


def _prompt(label: str, default: str | None = None, *, secret: bool = False) -> str:
    """Read a single line from the user with an optional default.

    Returns the default if the user just presses Enter. For secrets,
    we still echo (Synapse tokens are paste-and-go; full noecho would
    confuse paste UX more than it helps and the token is visible in
    the issuance step anyway).
    """
    if default is None:
        prompt = f"{label}: "
    else:
        prompt = f"{label} [{default}]: "
    try:
        ans = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130)
    if not ans and default is not None:
        return default
    return ans


# Project root: cli.py lives at orchestrator/integrations/synapse/cli.py;
# three .parent hops land at the MS4CC root directory.
_ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent.parent


def _ensure_hook_in_matcher_group(
    matcher_groups: list, matcher_pattern: str, hook_type: str, command: str
) -> bool:
    """Append a hook to the group with `matcher == matcher_pattern`. Create the
    group if no match. Idempotent: returns False if `command` was already there.
    """
    for group in matcher_groups:
        if not isinstance(group, dict):
            continue
        if group.get("matcher") == matcher_pattern:
            group_hooks = group.setdefault("hooks", [])
            for h in group_hooks:
                if isinstance(h, dict) and h.get("command") == command:
                    return False
            group_hooks.append({"type": hook_type, "command": command})
            return True
    matcher_groups.append(
        {
            "matcher": matcher_pattern,
            "hooks": [{"type": hook_type, "command": command}],
        }
    )
    return True


def _merge_synapse_hooks_into_settings() -> dict:
    """Additively merge the two Synapse hooks into ~/.claude/settings.json.

    Idempotent: existing Synapse hook entries are preserved, not duplicated.
    Other hooks (user's own, MS4CC's core hooks, hooks from other tools) are
    left untouched. Backs up the original on every change before writing.

    Returns a dict summary: {settings_path, backup_path, added: list[str],
    already_present: list[str]}.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    session_cmd = (
        f"{_ORCHESTRATOR_DIR}/.venv/bin/python "
        f"{_ORCHESTRATOR_DIR}/hooks/synapse_session_start.py"
    )
    prompt_cmd = (
        f"{_ORCHESTRATOR_DIR}/.venv/bin/python "
        f"{_ORCHESTRATOR_DIR}/hooks/synapse_user_prompt_submit.py"
    )

    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    hooks = data.setdefault("hooks", {})
    session_groups = hooks.setdefault("SessionStart", [])
    prompt_groups = hooks.setdefault("UserPromptSubmit", [])

    added: list[str] = []
    already: list[str] = []

    if _ensure_hook_in_matcher_group(session_groups, "*", "command", session_cmd):
        added.append("SessionStart -> synapse_session_start.py")
    else:
        already.append("SessionStart -> synapse_session_start.py")

    if _ensure_hook_in_matcher_group(prompt_groups, "*", "command", prompt_cmd):
        added.append("UserPromptSubmit -> synapse_user_prompt_submit.py")
    else:
        already.append("UserPromptSubmit -> synapse_user_prompt_submit.py")

    backup_path = None
    if added and settings_path.exists():
        backup_path = settings_path.with_name(
            f"settings.json.backup.{int(time.time())}"
        )
        shutil.copy2(settings_path, backup_path)

    if added or not settings_path.exists():
        tmp_path = settings_path.with_name("settings.json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(settings_path)

    return {
        "settings_path": settings_path,
        "backup_path": backup_path,
        "added": added,
        "already_present": already,
    }


def cmd_setup(_args: argparse.Namespace) -> int:
    """Interactive setup — replaces the manual config + token-paste dance.

    Steps:
      1. Prompt for base_url, handle, channels
      2. Prompt for the bearer token
      3. Validate connection live (GET /v1/auth/me) BEFORE writing files
      4. Write orchestrator/config/synapse.toml
      5. Write ~/.synapse/<handle>.token (mode 600); ensure ~/.synapse mode 700
      6. Additively merge the two Synapse hooks into ~/.claude/settings.json
         (preserves existing hooks; idempotent — skips entries already present)
      7. Print next-step

    Refuses to overwrite an existing synapse.toml without confirmation; the
    user can re-run later or edit by hand.
    """
    print()
    print("Synapse client setup.")
    print("=====================")
    print()

    existing = load_config()
    if existing is not None:
        print(f"  ! orchestrator/config/synapse.toml already exists.")
        print(f"    handle={existing.handle}, base_url={existing.base_url}")
        ans = _prompt("Overwrite? (y/N)", default="N").lower()
        if ans not in ("y", "yes"):
            print("  cancelled — config left untouched.")
            return 0
        print()

    base_url_default = existing.base_url if existing else "http://localhost:8080"
    handle_default = existing.handle if existing else None
    channels_default = (
        ",".join(existing.channels) if existing and existing.channels else "family-ops"
    )

    print("This MS4CC instance is going to talk to a Synapse deployment.")
    print()
    base_url = _prompt("Synapse base URL", default=base_url_default).rstrip("/")
    handle = _prompt(
        "Your agent handle on this Synapse deployment", default=handle_default
    )
    if not handle:
        print("  ! handle is required. Aborting.", file=sys.stderr)
        return 1
    channels_raw = _prompt(
        "Channels to watch (comma-separated)", default=channels_default
    )
    channels = [c.strip() for c in channels_raw.split(",") if c.strip()]
    if not channels:
        print("  ! at least one channel is required. Aborting.", file=sys.stderr)
        return 1

    print()
    print(f"Bearer token for '{handle}'.")
    print(f"  Issued from the Synapse host with:")
    print(f"  ./scripts/bootstrap.sh issue-token --account {handle} \\")
    print(f"    --scopes 'channel:<slug>:read,channel:<slug>:post'")
    print()
    token = _prompt("Paste the token now (or Ctrl-C to cancel)").strip()
    if not token:
        print("  ! empty token. Aborting.", file=sys.stderr)
        return 1

    # Validate before writing anything.
    print()
    print("Validating…", flush=True)
    try:
        client = SynapseClient(base_url, token, timeout=5)
        me = client.me()
    except SynapseError as e:
        sys.stdout.flush()
        print(f"  ✗ token rejected: {e}", file=sys.stderr)
        print(f"    base_url and/or token are wrong; nothing written.", file=sys.stderr)
        return 1

    actual_handle = str(me.get("handle"))
    actual_kind = str(me.get("kind"))
    if actual_handle != handle:
        print(
            f"  ! token resolves to handle={actual_handle!r} but you typed {handle!r}.",
            file=sys.stderr,
        )
        ans = _prompt(f"Use the token's actual handle ({actual_handle})? (Y/n)", default="Y").lower()
        if ans in ("n", "no"):
            print("  aborted — re-run with the right token.", file=sys.stderr)
            return 1
        handle = actual_handle

    print(f"  ✓ reachable")
    print(f"  ✓ authenticated as {handle} ({actual_kind})")

    # Write config.
    config_dir = CONFIG_PATH.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    config_lines = [
        "# Generated by `synapse setup`. Edit by hand if you need to.",
        "# Token is NOT stored here; it lives at ~/.synapse/<handle>.token (mode 600).",
        "",
        "[synapse]",
        f'base_url = "{base_url}"',
        f'handle = "{handle}"',
        f"channels = [{', '.join(repr(c) for c in channels)}]",
        "limit_per_channel = 20",
        "fresh_session_seconds = 43200",
        "http_timeout = 5",
        "",
    ]
    CONFIG_PATH.write_text("\n".join(config_lines), encoding="utf-8")

    # Write token.
    ensure_synapse_dir()
    token_path = HOME_SYNAPSE / f"{handle}.token"
    token_path.write_text(token, encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass

    print()
    print(f"  wrote {CONFIG_PATH.relative_to(Path.cwd()) if CONFIG_PATH.is_relative_to(Path.cwd()) else CONFIG_PATH}")
    print(f"  wrote {token_path} (mode 600)")

    # Additively merge Synapse hooks into ~/.claude/settings.json. Preserves
    # existing hooks (user's own, MS4CC's core hooks); idempotent — skips
    # entries already present.
    try:
        merge_info = _merge_synapse_hooks_into_settings()
        if merge_info["added"]:
            print(f"  wrote {merge_info['settings_path']}")
            if merge_info["backup_path"]:
                print(f"    (backup at {merge_info['backup_path']})")
            for entry in merge_info["added"]:
                print(f"    + {entry}")
        if merge_info["already_present"]:
            for entry in merge_info["already_present"]:
                print(f"    = {entry} (already present)")
    except Exception as e:
        # Non-fatal: config + token are already written. User can re-run
        # setup or wire the hooks manually via settings.fragment.json.
        print(f"  ! settings.json hook-merge failed ({e})", file=sys.stderr)
        print(f"    config + token written; wire hooks manually if needed.", file=sys.stderr)

    print()
    print("Next:")
    print("  /synapse-activate   (from Claude Code)")
    print("  or:")
    print("  ./orchestrator/.venv/bin/python -m orchestrator.integrations.synapse activate")
    print()
    return 0


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

    sub.add_parser(
        "setup",
        help="Interactive setup — write config + token, validate connection",
    )
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
        "setup": cmd_setup,
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
