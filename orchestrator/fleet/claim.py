"""Mark the running Claude Code session as the agent's own, so the Fleet reporter reports it.

Only an agent's own sessions report (Clint, 2026-11-04: "Agent sessions only"); every other
Claude Code session on the machine shares the same settings.json and hooks and sends nothing
(fleet/common.py agent_session). An agent launched with MS4CC_FLEET_AGENT=<handle> needs no
claim. A session already running claims itself from inside, through its Bash tool, with the
full path (the Bash tool runs in the project, not the MS4CC checkout):

    <MS4CC>/orchestrator/.venv/bin/python <MS4CC>/orchestrator/fleet/claim.py            # claim
    <MS4CC>/orchestrator/.venv/bin/python <MS4CC>/orchestrator/fleet/claim.py --release  # stop
    <MS4CC>/orchestrator/.venv/bin/python <MS4CC>/orchestrator/fleet/claim.py --list

The session id comes from CLAUDE_CODE_SESSION_ID, which Claude Code sets for its tools. A claim
ends with its session (SessionEnd, which /clear also fires); a new session, or a resumed one,
claims again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet import common as c  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) > 1 or argv[:1] not in ([], ["--release"], ["--list"]):
        print(__doc__)
        return 2
    cfg = c.fleet_config()
    if cfg is None:
        print("Fleet is off or not configured (synapse.toml [fleet] enabled = true, and a token).")
        return 1
    path = c.claimed_sessions_file(cfg.handle)
    if argv == ["--list"]:
        try:
            print(path.read_text(encoding="utf-8").strip() or "(no claimed sessions)")
        except OSError:
            print("(no claimed sessions)")
        return 0
    marked = os.environ.get(c.AGENT_ENV)
    if marked is not None:  # the launch mark decides, whatever is claimed
        if marked == cfg.handle:
            print(f"This session was launched as {cfg.handle} ({c.AGENT_ENV}); it reports without a claim.")
            return 0
        print(f"{c.AGENT_ENV}={marked!r} doesn't name {cfg.handle}: this session can't report, claimed or not.")
        return 1
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid or c.safe_session(sid) != sid:
        print("No session id: run this through Claude Code's Bash tool, inside the session to claim.")
        return 1
    if argv == ["--release"]:
        c.update_claims(cfg.handle, lambda ids: [i for i in ids if i != sid])
        print(f"Released {sid}: it no longer reports as {cfg.handle} ({path}).")
        return 0
    c.update_claims(cfg.handle, lambda ids: [i for i in ids if i != sid] + [sid])
    print(f"Claimed {sid} for {cfg.handle} ({path}): this session now reports to the Fleet board.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
