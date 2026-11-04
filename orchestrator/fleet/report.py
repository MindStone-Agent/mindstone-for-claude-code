"""Fleet reporter: one Claude Code hook event in, fleet events out (synapse#31).

Run as a `command` hook for every event the fragment registers:

    $ORCHESTRATOR_DIR/.venv/bin/python $ORCHESTRATOR_DIR/fleet/report.py

It reads the hook's JSON from stdin, maps it to a fleet event, and posts that event to
Synapse from a detached grandchild, so the hook itself returns at once. It never prints:
SessionStart and UserPromptSubmit stdout is injected into the model's context.

Privacy (synapse#31 section 2): each status payload is built field by field from the
allow-list below; nothing is copied from the hook input wholesale. Two strings are free text:
the last component of the cwd (at most 64 chars) and a task subject, scrubbed and cut to 120
chars. With `[fleet] activity = true` (and https, or http to this machine), fleet/activity.py
adds redacted previews of tool inputs and outputs and the agent's replies, each built per tool
from its own allow-list; its docstring says what is never sent.

Off unless `[fleet] enabled = true` in orchestrator/config/synapse.toml.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script by the hook command
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet.common import emit, emit_later, fleet_config, now_iso, prune_state, rate_ok, remember_mode, reset_rate, scrub_subject  # noqa: E402

HARNESS = "claude-code"
# Notification types that mean a person must act (synapse#31: permission, idle, elicitation).
NEEDS_INPUT_KINDS = {"permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}
TOOL_SAMPLE_S = 5.0


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
            "cwd_name": _s(os.path.basename(cwd.rstrip("/")), 64) if isinstance(cwd, str) else None,
            "model": _model(inp.get("model")),
            "source": _s(inp.get("source"), 16),
        }, {}
    if ev == "SessionEnd":
        return "session.ended", {"reason": _s(inp.get("reason"), 32)}, {}
    if ev == "UserPromptSubmit":
        return "turn.started", {}, {}
    if ev == "Stop":
        return "turn.ended", {}, {}
    if ev in ("PostToolUse", "PostToolUseFailure"):  # a failed tool is still activity
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
        # `tool_name` is optional in #31 and isn't available: on 2.1.312 the permission
        # notification's message is just "Claude needs your permission", and the message
        # text itself is never sent.
        return "needs_input", {"kind": kind}, {}
    if ev in ("PreCompact", "PostCompact"):
        return "compact", {"phase": "pre" if ev == "PreCompact" else "post"}, {}
    return None


def _event(cfg, kind: str, session: str, payload: dict, agent_id: object = None) -> dict:
    return {
        "type": kind,
        "ts": now_iso(),
        "machine": cfg.machine,
        "harness": HARNESS,
        "session_id": session,
        # Set on events fired inside a subagent (and on SubagentStart/Stop, naming it).
        "agent_id": _s(agent_id, 128),
        "parent_agent_id": None,
        "payload": {k: v for k, v in payload.items() if v is not None},
    }


def _activity(cfg, inp: dict, session: str, mapped) -> list[dict] | None:
    """The activity-layer events for one hook input (synapse#31 scope change), replacing the
    status layer's event where they overlap. None when activity has nothing to add."""
    from fleet import activity as a
    ev = inp.get("hook_event_name")
    agent_id = inp.get("agent_id")
    if ev == "PreToolUse":
        payload = a.tool_started(inp)
        if payload is None:
            return []
        a.open_call(session, payload["tool_use_id"], payload["tool_name"])
        if payload["tool_name"] in a.AGENT_TOOLS:
            a.remember_agent_call(session, inp)
        return [_event(cfg, "tool.started", session, payload, agent_id)]
    if ev in ("PostToolUse", "PostToolUseFailure"):
        a.close_call(session, inp.get("tool_use_id"))
        if inp.get("tool_name") in a.AGENT_TOOLS:
            resp = inp.get("tool_response")
            # A background agent's PostToolUse comes at launch, maybe before its SubagentStart:
            # keep that call for pairing. A finished or failed call can't start one any more.
            if not (isinstance(resp, dict) and resp.get("status") == "async_launched"):
                a.forget_agent_call(session, inp.get("tool_use_id"))
        payload = a.tool_finished(inp, ok=ev == "PostToolUse")
        if payload and payload.get("task_id"):
            from fleet import live
            live.remember_task(session, payload["task_id"], payload["tool_use_id"])
            live.on_task_started(cfg, inp, session, payload["task_id"])
        return [_event(cfg, "tool.finished", session, payload, agent_id)] if payload else []
    if ev == "SubagentStart" and mapped is not None:
        kind, payload, _ = mapped
        parent, description = a.pair_subagent(session, payload.get("agent_type"),
                                              a.recent_errored(inp.get("transcript_path")))
        text, _ = a.preview(description, a.DESCRIPTION_MAX)
        payload = dict(payload, parent_tool_use_id=parent, description=text)
        return [_event(cfg, kind, session, payload, agent_id)]
    return None


def _tail(inp: dict, session: str) -> tuple[Path, str | None, bool] | None:
    """Which transcript to tail for replies after this event, if any."""
    from fleet import activity as a
    ev = inp.get("hook_event_name")
    tp = inp.get("transcript_path")
    agent_id = _s(inp.get("agent_id"), 128)
    # PreToolUse too, so "Let me run X" shows before X, not after it finishes.
    if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SubagentStop") and agent_id:
        path = a.subagent_transcript(tp, session, agent_id)
        return (path, agent_id, False) if path else None
    if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop") and isinstance(tp, str) and tp:
        return Path(tp), None, True
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
    if inp.get("hook_event_name") == "FileChanged":  # a task's output file: live output
        if cfg.activity:
            from fleet import live
            live.on_file_changed(cfg, inp, session)
        return 0
    if cfg.activity and inp.get("hook_event_name") in ("Stop", "SessionEnd"):
        from fleet import live
        if inp["hook_event_name"] == "Stop":
            live.remember_running(session, inp.get("background_tasks"))
        else:
            live.end_session(cfg, session)
    mapped = build(inp)
    if mapped is not None and mapped[0] == "session.started":
        prune_state()
    events = _activity(cfg, inp, session, mapped) if cfg.activity else None
    if events is None:
        if mapped is None:
            return 0
        kind, payload, _ = mapped
        if kind == "tool" and not rate_ok(session, "tool", TOOL_SAMPLE_S):
            return 0
        events = [_event(cfg, kind, session, payload, inp.get("agent_id"))]
    if mapped is not None and mapped[0] == "needs_input":
        # The board clears "needs you" on the next tool or turn event. Re-arm tool sampling
        # so the first tool after the answer is always sent, not swallowed by the 5 s sample.
        reset_rate(session, "tool")
    if not cfg.activity:
        emit(cfg, events)
        return 0
    from fleet import activity as a
    if inp.get("hook_event_name") == "SessionStart" and isinstance(inp.get("transcript_path"), str):
        a.start_offset(session, Path(inp["transcript_path"]))  # replies from here on, no history
    tail = _tail(inp, session)
    if tail is None:
        if events:
            emit(cfg, events)
        return 0
    path, tail_agent, at_end = tail

    def more() -> list[dict]:
        last = inp.get("last_assistant_message") if inp.get("hook_event_name") in ("Stop", "SubagentStop") else None
        out, errored = [], []
        msgs = a.tail_messages(session, path, tail_agent, start_at_end=at_end, last_message=last, errored=errored)
        for tool_use_id in errored:  # denied or blocked: no PostToolUse will ever come
            name = a.close_call(session, tool_use_id)
            if name:
                a.forget_agent_call(session, tool_use_id)
                out.append(_event(cfg, "tool.finished", session, {"tool_use_id": tool_use_id, "tool_name": name,
                                                                  "ok": False, "error": "not run (denied or blocked)"},
                                  tail_agent))
        for m in msgs:
            ts = m.pop("ts", None)
            ev = _event(cfg, "message", session, m, tail_agent)
            if ts:
                ev["ts"] = ts  # when it was said; the server clamps it to +-5 min
            out.append(ev)
        return out
    emit_later(cfg, events or [], more)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # a reporter must never fail the hook it rides on
        sys.exit(0)
