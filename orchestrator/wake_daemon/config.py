"""Wake daemon config loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ORCHESTRATOR_DIR / "config" / "wake-daemon.toml"


@dataclass(frozen=True)
class WakeConfig:
    bind_host: str
    bind_port: int
    handle: str
    secret_file: Path
    synapse_base_url: str
    synapse_token_file: Path
    claude_binary: str
    working_directory: Path
    timeout_seconds: int

    def read_secret(self) -> str | None:
        if not self.secret_file.exists():
            return None
        v = self.secret_file.read_text(encoding="utf-8").strip()
        return v or None

    def read_synapse_token(self) -> str | None:
        if not self.synapse_token_file.exists():
            return None
        v = self.synapse_token_file.read_text(encoding="utf-8").strip()
        return v or None


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def load_config() -> WakeConfig | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    wake = data.get("wake")
    if not isinstance(wake, dict):
        return None

    try:
        bind_str = str(wake["bind"])
        host, port_str = bind_str.rsplit(":", 1)
        synapse = wake.get("synapse") or {}
        sub = wake.get("subprocess") or {}

        return WakeConfig(
            bind_host=host or "0.0.0.0",
            bind_port=int(port_str),
            handle=str(wake["handle"]),
            secret_file=_expand(str(wake["secret_file"])),
            synapse_base_url=str(synapse.get("base_url", "http://localhost:8080")).rstrip(
                "/"
            ),
            synapse_token_file=_expand(
                str(synapse.get("token_file", "~/.synapse/" + str(wake["handle"]) + ".token"))
            ),
            claude_binary=str(sub.get("claude_binary", "claude")),
            working_directory=_expand(str(sub.get("working_directory", str(ORCHESTRATOR_DIR.parent)))),
            timeout_seconds=int(sub.get("timeout_seconds", 180)),
        )
    except (KeyError, ValueError, TypeError):
        return None
