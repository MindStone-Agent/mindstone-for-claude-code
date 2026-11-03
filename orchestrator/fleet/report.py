"""Fleet Phase 0 reporter: one Claude Code hook event in, at most one fleet event out (synapse#31).

Run as a `command` hook for every event the fragment registers:

    $ORCHESTRATOR_DIR/.venv/bin/python $ORCHESTRATOR_DIR/fleet/report.py

It reads the hook's JSON from stdin, maps it to a fleet event, and posts that event to
Synapse from a detached grandchild, so the hook itself returns at once. It never prints:
SessionStart and UserPromptSubmit stdout is injected into the model's context.

Privacy (synapse#31 section 2): each payload is built field by field from the allow-list
below. Nothing is copied from the hook input wholesale, so a prompt, a tool input or a
tool output cannot leave the machine through this file, whatever Claude Code adds to the
hook JSON later. The only free text sent is a task subject, scrubbed and cut to 120 chars.

Off unless `[fleet] enabled = true` in orchestrator/config/synapse.toml.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # run as a script by the hook command
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet.common import emit, fleet_config, now_iso, rate_ok, remember_mode, reset_rate, scrub_subject  # noqa: E402

HARNESS = "claude-code"
NEEDS_INPUT_KINDS = {"permission_prompt", "idle_prompt", "elicitation_dialog"}
TOOL_SAMPLE_S = 5.0
_TOOL_IN_MESSAGE = re.compile(r"permission to use ([A-Za-z0-9_.:-]{1,64})")


def _s(v: object, limit: int = 64) -> str | None:
    """A short identifier-like string, or None. Never free text."""
    if not isinstance(v, str) or not v:
        return None
    return v[:limit]


def _model(v: object) -> str | None:
    """SessionStart's optional `model`: a string, or an object with an `id`."""
    if isinstance(v, dict):
        v = v.get("id")
    return _s(v, 128)


def build(inp: dict) -> tuple[str, dict, dict] | None:
    """(type, payload, top-level extras) for one hook input, or None when it maps to nothing.

    Every payload value is named here explicitly; this is the allow-list."""
    ev = inp.get("hook_event_name")
    if ev == "SessionStart":
        cwd = inp.get("cwd")
        return "session.started", {
            "cwd_name": os.path.basename(cwd.rstrip("/")) if isinstance(cwd, str) and cwd else None,
            "model": _model(inp.get("model")),
            "source": _s(inp.get("source"), 16),
        }, {}
    if ev == "SessionEnd":
        return "session.ended", {"reason": _s(inp.get("reason"), 32)}, {}
    if ev == "UserPromptSubmit":
        return "turn.started", {}, {}
    if ev == "Stop":
        return "turn.ended", {}, {}
    if ev == "PostToolUse":
        return "tool", {"tool_name": _s(inp.get("tool_name"), 128)}, {}
    if ev in ("SubagentStart", "SubagentStop"):
        # Claude Code's own internal agents fire SubagentStop with no agent_type and no
        # matching SubagentStart (seen on 2.1.312); reporting them would unbalance the
        # board's open-subagent count, so only typed subagents are reported.
        agent_type = _s(inp.get("agent_type"), 64)
        if agent_type is None:
            return None
        kind = "subagent.started" if ev == "SubagentStart" else "subagent.stopped"
        return kind, {"agent_type": agent_type}, {}
    if ev in ("TaskCreated", "TaskCompleted"):
        kind = "task.created" if ev == "TaskCreated" else "task.completed"
        return kind, {"task_id": _s(inp.get("task_id"), 128), "subject": scrub_subject(inp.get("task_subject", inp.get("subject")))}, {}
    if ev == "Notification":
        kind = inp.get("notification_type")
        if kind not in NEEDS_INPUT_KINDS:
            return None
        payload: dict = {"kind": kind}
        m = _TOOL_IN_MESSAGE.search(inp.get("message") or "") if kind == "permission_prompt" else None
        if m:
            payload["tool_name"] = m.group(1)
        return "needs_input", payload, {}
    if ev in ("PreCompact", "PostCompact"):
        return "compact", {"phase": "pre" if ev == "PreCompact" else "post"}, {}
    return None


def main() -> int:
    cfg = fleet_config()
    if cfg is None:  # off, unconfigured or no token: the cheapest path, nothing forked
        return 0
    try:
        inp = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(inp, dict):
        return 0
    session = _s(inp.get("session_id"), 128)
    if not session:
        return 0
    remember_mode(session, inp.get("permission_mode"))  # the status line has no mode field
    mapped = build(inp)
    if mapped is None:
        return 0
    kind, payload, extras = mapped
    if kind == "tool" and not rate_ok(cfg, session, "tool", TOOL_SAMPLE_S):
        return 0
    if kind == "needs_input":
        # The board clears "needs you" on the next tool or turn event. Re-arm tool sampling
        # so the first tool after the answer is always sent, not swallowed by the 5 s sample.
        reset_rate(session, "tool")
    event = {
        "type": kind,
        "ts": now_iso(),
        "machine": cfg.machine,
        "harness": HARNESS,
        "session_id": session,
        # Set on events fired inside a subagent (and on SubagentStart/Stop, naming it).
        "agent_id": _s(inp.get("agent_id"), 128),
        "parent_agent_id": None,
        "payload": {k: v for k, v in payload.items() if v is not None},
    }
    emit(cfg, [event])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # a reporter must never fail the hook it rides on
        sys.exit(0)
