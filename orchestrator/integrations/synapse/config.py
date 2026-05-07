"""Synapse client config loading.

Looks up `orchestrator/config/synapse.toml` (relative to this file) and
resolves the bearer token from `~/.synapse/<handle>.token`. Anything
missing → returns None so the hooks can fail-soft (no Synapse for this
deployment).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


HOME_SYNAPSE = Path.home() / ".synapse"
ORCHESTRATOR_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ORCHESTRATOR_DIR / "config" / "synapse.toml"


@dataclass(frozen=True)
class SynapseConfig:
    base_url: str
    handle: str
    channels: tuple[str, ...]
    limit_per_channel: int
    fresh_session_seconds: int
    http_timeout: int

    @property
    def token_path(self) -> Path:
        return HOME_SYNAPSE / f"{self.handle}.token"

    @property
    def active_flag_path(self) -> Path:
        return HOME_SYNAPSE / f"{self.handle}.active"

    @property
    def cursor_path(self) -> Path:
        return HOME_SYNAPSE / f"{self.handle}.cursor.json"

    def read_token(self) -> str | None:
        path = self.token_path
        if not path.exists():
            return None
        token = path.read_text(encoding="utf-8").strip()
        return token or None


def load_config() -> SynapseConfig | None:
    """Load synapse.toml. Returns None if absent or unparseable."""
    if not CONFIG_PATH.exists():
        return None
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    section = data.get("synapse")
    if not isinstance(section, dict):
        return None

    try:
        return SynapseConfig(
            base_url=str(section["base_url"]).rstrip("/"),
            handle=str(section["handle"]),
            channels=tuple(section.get("channels", [])),
            limit_per_channel=int(section.get("limit_per_channel", 20)),
            fresh_session_seconds=int(section.get("fresh_session_seconds", 43200)),
            http_timeout=int(section.get("http_timeout", 5)),
        )
    except (KeyError, ValueError, TypeError):
        return None


def ensure_synapse_dir() -> Path:
    """~/.synapse exists with mode 700."""
    HOME_SYNAPSE.mkdir(mode=0o700, exist_ok=True)
    # On existing dirs, mkdir won't tighten mode — enforce explicitly.
    try:
        os.chmod(HOME_SYNAPSE, 0o700)
    except OSError:
        pass
    return HOME_SYNAPSE
