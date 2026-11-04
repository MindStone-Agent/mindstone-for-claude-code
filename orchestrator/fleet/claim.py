"""Mark the running Claude Code session as the agent's own, so the Fleet reporter reports it.

Only an agent's own sessions report (Clint, 2026-11-04: "Agent sessions only"); every other
Claude Code session on the machine shares the same settings.json and hooks and sends nothing
(fleet/common.py agent_session). An agent launched with MS4CC_FLEET_AGENT=<handle> needs no
claim. A session already running claims itself from inside, through its Bash tool:

    $ORCHESTRATOR_DIR/.venv/bin/python $ORCHESTRATOR_DIR/fleet/claim.py            # claim
    $ORCHESTRATOR_DIR/.venv/bin/python $ORCHESTRATOR_DIR/fleet/claim.py --release  # stop
    $ORCHESTRATOR_DIR/.venv/bin/python $ORCHESTRATOR_DIR/fleet/claim.py --list

The session id comes from CLAUDE_CODE_SESSION_ID, which Claude Code sets for its tools. A new
session (/clear, a fork) has a new id and must claim again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet import common as c  # noqa: E402

KEEP = 50  # claimed ids kept per handle; the oldest go first


def _ids(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").split()
    except OSError:
        return []


def _write(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(i + "\n" for i in ids))
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    cfg = c.fleet_config()
    if cfg is None:
        print("Fleet is off or not configured (synapse.toml [fleet] enabled = true, and a token).")
        return 1
    path = c.claimed_sessions_file(cfg.handle)
    if argv[:1] == ["--list"]:
        print("\n".join(_ids(path)) or "(no claimed sessions)")
        return 0
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid or c.safe_session(sid) != sid:
        print("No session id: run this through Claude Code's Bash tool, inside the session to claim.")
        return 1
    ids = [i for i in _ids(path) if i != sid]
    if argv[:1] == ["--release"]:
        _write(path, ids)
        print(f"Released {sid}: it no longer reports as {cfg.handle}.")
        return 0
    if argv:
        print(__doc__)
        return 2
    _write(path, (ids + [sid])[-KEEP:])
    print(f"Claimed {sid} for {cfg.handle}: this session now reports to the Fleet board.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
