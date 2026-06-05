#!/usr/bin/env python3
"""PostToolUse hook — context-capacity sampling DURING autonomous work.

The 85% danger-zone handoff trigger historically lived ONLY in
`user_prompt_submit.py`, which samples context occupancy on the UserPromptSubmit
hook — i.e. only when Clint sends a prompt. During long autonomous runs (no user
prompts), context climbs past the 85% threshold straight through to the harness
auto-compact (~92%) BETWEEN prompts, so the trigger was never sampled and the
rich, model-authored handoff + `/checkpoint` synthesis never ran. The 2026-06-05
incident: a full day-job session compacted with `.handoff_state.json` `fired:
false` — the directive was never injected. The rule it enforces: the danger-zone handoff is never deferred for in-flight work.

This hook closes that gap. PostToolUse fires after every tool call — i.e. DURING
a turn, including fully autonomous turns — so it samples occupancy even when no
user prompt is forthcoming. It REUSES `user_prompt_submit.maybe_handoff_directive`
verbatim, which means:

  - Same COMPACT_THRESHOLD (0.85) and same CONTEXT_WINDOW.
  - Same `.handoff_state.json` per-session fire-once state. Whichever sampling
    point (UserPromptSubmit OR this hook) crosses 85% first fires the directive
    once and sets `fired: true`; the other then sees `fired: true` and stays
    silent. Multiple sampling points, ONE trigger → NO duplication.
  - Same re-arm logic (re-arms when occupancy falls below REARM_RATIO after a
    compaction).

To keep this cheap under heavy tool volume, occupancy is sampled at most once
per THROTTLE_SECS (the latest `usage` record only moves between turns, and a
single turn cannot climb the ~7 points from 85%→92% in a few seconds, so a short
throttle never misses the crossing). All errors are swallowed — this hook must
never block a tool call.

Output uses `hookSpecificOutput.additionalContext` (valid for PostToolUse per the
event-scoped schema; see `feedback_hook_schema_per_event.md`) — the same
injection path UserPromptSubmit uses for the directive.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_DIR = HOOK_DIR.parent
THROTTLE_STATE_FILE = ORCHESTRATOR_DIR / "transcripts" / ".handoff_posttool_ts.json"
THROTTLE_SECS = float(os.environ.get("CAIRN_POSTTOOL_THROTTLE_SECS", "12"))

# Reuse the canonical handoff-trigger logic so threshold + fire-once state stay
# single-sourced. user_prompt_submit's module level is side-effect-free (all work
# is under its __main__ guard), so importing it is safe.
sys.path.insert(0, str(HOOK_DIR))
try:
    import user_prompt_submit as ups  # noqa: E402
except Exception as e:  # pragma: no cover - import guard
    print(f"[post_tool_handoff] cannot import user_prompt_submit ({e})", file=sys.stderr)
    ups = None


def _read_hook_input() -> dict:
    try:
        if sys.stdin.isatty():
            return {}
        data = sys.stdin.read().strip()
        return json.loads(data) if data else {}
    except Exception:
        return {}


def _resolve_session_id(hook_input: dict) -> str:
    sid = hook_input.get("session_id") or hook_input.get("sessionId")
    if sid:
        return sid
    sess = hook_input.get("session")
    if isinstance(sess, dict) and sess.get("id"):
        return sess["id"]
    return "default"


def _throttled(sid: str) -> bool:
    """True if we sampled for this session within the last THROTTLE_SECS.
    Updates the timestamp when it returns False (i.e. when we proceed)."""
    now = time.time()
    state: dict = {}
    try:
        if THROTTLE_STATE_FILE.exists():
            state = json.loads(THROTTLE_STATE_FILE.read_text())
            if not isinstance(state, dict):
                state = {}
    except Exception:
        state = {}

    last = state.get(sid)
    if isinstance(last, (int, float)) and (now - last) < THROTTLE_SECS:
        return True

    state[sid] = now
    try:
        THROTTLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        THROTTLE_STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        print(f"[post_tool_handoff] throttle state write failed ({e})", file=sys.stderr)
    return False


def main() -> None:
    if ups is None:
        return
    hook_input = _read_hook_input()
    try:
        sid = _resolve_session_id(hook_input)
        if _throttled(sid):
            return
        directive = ups.maybe_handoff_directive(hook_input)
        if directive:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": directive,
                }
            }))
    except Exception as e:
        print(f"[post_tool_handoff] error ({e})", file=sys.stderr)


if __name__ == "__main__":
    main()
