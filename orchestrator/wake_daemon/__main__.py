"""Entry point: `python -m orchestrator.wake_daemon`."""

from __future__ import annotations

import sys

from .config import CONFIG_PATH, load_config
from .server import run


def main() -> int:
    cfg = load_config()
    if cfg is None:
        print(
            f"wake-daemon: no config at {CONFIG_PATH} — "
            f"copy wake-daemon.example.toml and fill it in",
            file=sys.stderr,
        )
        return 1
    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
