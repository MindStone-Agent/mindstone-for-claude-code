"""Active flag + per-channel cursor persistence.

Files in ~/.synapse/<handle>.{active,cursor.json}:
  - <handle>.active     — touch to enable; missing means disabled.
  - <handle>.cursor.json — { "<channel_slug>": "<opaque_cursor>", ... }

The active flag exists for the same reason channel slugs exist in the
Synapse API: it lets the user toggle the integration mid-session
without restarting Claude Code or editing config. Slash commands flip
the flag; hooks read it on every event.
"""

from __future__ import annotations

import json
import os
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
