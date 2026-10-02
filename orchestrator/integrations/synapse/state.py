"""Active flag + per-channel cursor persistence.

Files in ~/.synapse/<handle>.{active,cursor.json}:
  - <handle>.active     — touch to enable; missing means disabled.
  - <handle>.cursor.json — { "<channel_slug>": "<opaque_cursor>", ... }
  - <handle>.cursor.<session_id>.json — same shape, one per Claude Code session

The active flag exists for the same reason channel slugs exist in the
Synapse API: it lets the user toggle the integration mid-session
without restarting Claude Code or editing config. Slash commands flip
the flag; hooks read it on every event.
"""

from __future__ import annotations

import glob
import json
import os
import re
import select
import sys
import time
from pathlib import Path

from .config import SynapseConfig, ensure_synapse_dir


def is_active(cfg: SynapseConfig) -> bool:
    return cfg.active_flag_path.exists()


def activate(cfg: SynapseConfig) -> None:
    ensure_synapse_dir()
    cfg.active_flag_path.touch(mode=0o600, exist_ok=True)


def deactivate(cfg: SynapseConfig) -> None:
    try:
        cfg.active_flag_path.unlink()
    except FileNotFoundError:
        pass


def read_cursor(cfg: SynapseConfig, channel: str) -> str | None:
    path = cfg.cursor_path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    val = data.get(channel)
    return val if isinstance(val, str) else None


def write_cursor(cfg: SynapseConfig, channel: str, cursor: str) -> None:
    ensure_synapse_dir()
    path = cfg.cursor_path
    data: dict[str, str] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = {k: v for k, v in loaded.items() if isinstance(v, str)}
        except (OSError, json.JSONDecodeError):
            data = {}
    data[channel] = cursor
    # Atomic write so concurrent hooks don't truncate each other.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --- Per-session cursors ----------------------------------------------------
#
# Several Claude Code sessions can be open at once. With a single shared cursor,
# whichever session reads a mention first advances it and every other session
# goes blind to that mention. Each session therefore keeps its own cursor file,
# <handle>.cursor.<session_id>.json, next to the shared one.
#
# The shared cursor stays the fallback when no session id is available, and the
# seed for a session's first read of a channel its SessionStart did not pin; the
# seed is pinned into the session's own file on the first write, so later moves
# of the shared cursor cannot change what that session sees. With a session id
# present, no caller in this package writes the shared cursor: SessionStart pins
# the session's own file too. A seed from the shared cursor can therefore be
# stale (older mentions are replayed once; with no shared entry the first fetch
# has no lower bound).
#
# Cursor files unused for SESSION_CURSOR_MAX_AGE_S are deleted.

SESSION_CURSOR_MAX_AGE_S = 14 * 24 * 3600
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_session_id(value: object) -> str | None:
    """Return value if it is a safe session id (it becomes part of a filename)."""
    if isinstance(value, str) and _SESSION_ID_RE.fullmatch(value):
        return value
    return None


def current_session_id() -> str | None:
    """The session id of the Claude Code session this process runs in, or None.

    Claude Code exports CLAUDE_CODE_SESSION_ID to the commands it runs. Hooks
    receive the same id as `session_id` in their stdin JSON.
    """
    return valid_session_id(os.environ.get("CLAUDE_CODE_SESSION_ID"))


def session_cursor_path(cfg: SynapseConfig, session_id: str) -> Path:
    return cfg.cursor_path.with_name(f"{cfg.handle}.cursor.{session_id}.json")


def load_session_cursors(cfg: SynapseConfig, session_id: str) -> dict[str, str]:
    try:
        data = json.loads(session_cursor_path(cfg, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def write_session_cursors(cfg: SynapseConfig, session_id: str, cursors: dict[str, str]) -> bool:
    """Atomically persist this session's cursors; return True on success.

    A failure is logged, not raised: a cursor write must never cost a digest.
    The caller decides what to do with a cursor that did not persist (the hook
    rolls it back so the mentions repeat on the next prompt)."""
    tmp = None
    try:
        ensure_synapse_dir()
        path = session_cursor_path(cfg, session_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # Create the temp file 0600 from the start (the umask can only tighten it).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)  # fdopen failed, so nothing owns the descriptor yet
            raise
        with f:
            f.write(json.dumps(cursors, sort_keys=True, indent=2))
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)  # best effort: the temp file was already 0600
        except OSError:
            pass
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[synapse state] session cursor write: {e}", file=sys.stderr)
        if tmp is not None:
            try:
                os.unlink(tmp)  # do not leave a stray .tmp behind
            except OSError:
                pass
        return False


def touch_session_cursors(cfg: SynapseConfig, session_id: str) -> None:
    """Mark this session's file as in use so only idle sessions get pruned."""
    try:
        os.utime(session_cursor_path(cfg, session_id))
    except OSError:
        pass


def prune_session_cursors(cfg: SynapseConfig) -> None:
    """Delete session cursor files (and leftover temp files) idle for 14 days."""
    cutoff = time.time() - SESSION_CURSOR_MAX_AGE_S
    prefix = glob.escape(cfg.handle)
    for pattern in (f"{prefix}.cursor.*.json", f"{prefix}.cursor.*.json.tmp"):
        for p in cfg.cursor_path.parent.glob(pattern):
            if p == cfg.cursor_path:
                continue  # defensive: the glob cannot match the shared cursor anyway
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


STDIN_WAIT_S = 0.5


def read_session_id_from_stdin(wait_s: float = STDIN_WAIT_S) -> str | None:
    """Return the Claude Code session id from a hook's stdin JSON, or None.

    None (terminal, empty, not JSON, an incomplete payload, or an unsafe id)
    makes the caller fall back to the shared cursor, which is the pre-existing
    behaviour. Reads what the harness has written for at most wait_s in total,
    and stops as soon as a whole JSON document has arrived, so a writer that
    never closes the pipe cannot stall the prompt; a payload still incomplete
    when the wait ends cannot be parsed and also falls back.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        fd = sys.stdin.fileno()
        deadline = time.monotonic() + wait_s
        chunks: list[bytes] = []
        data = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:  # EOF: the writer closed the pipe
                break
            chunks.append(chunk)
            try:
                data = json.loads(b"".join(chunks).decode("utf-8"))
                break
            except ValueError:  # incomplete so far (or a split multibyte character)
                continue
        if data is None:
            data = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except (OSError, ValueError):  # includes UnicodeDecodeError and JSONDecodeError
        return None
    if not isinstance(data, dict):
        return None
    # The other hooks accept both spellings.
    return valid_session_id(data.get("session_id")) or valid_session_id(data.get("sessionId"))
