"""Fleet Phase 0 status line (synapse#31 section 5).

Claude Code runs this as the `statusLine` command and shows what it prints. It does two
things, in this order:

1. **Prints the bar.** If the user already had a status line, bootstrap wrapped it rather
   than replacing it: its command arrives here base64-encoded after `--wrap-b64`, runs
   with the same stdin, and its output is printed unchanged. With nothing to wrap, it
   prints a short bar of its own (model, context %, cost).
2. **Reports `status`** to Synapse, at most once every 10 s per session, from a detached
   grandchild, only when `[fleet] enabled = true`. The bar never waits on the network.

Privacy: the status payload is built from the allow-listed fields below only.
"""
from __future__ import annotations

import base64
import binascii
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet.common import emit, fleet_config, now_iso, rate_ok, read_mode  # noqa: E402

STATUS_EVERY_S = 10.0
WRAP_TIMEOUT_S = 5.0


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _get(d: object, *path: str) -> object:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def wrapped_command(argv: list[str]) -> str | None:
    if "--wrap-b64" not in argv:
        return None
    i = argv.index("--wrap-b64")
    try:
        return base64.b64decode(argv[i + 1], validate=True).decode("utf-8") or None
    except (IndexError, binascii.Error, UnicodeDecodeError):
        return None


def own_bar(inp: dict) -> str:
    parts = []
    name = _get(inp, "model", "display_name")
    if isinstance(name, str) and name:
        parts.append(name)
    pct = _num(_get(inp, "context_window", "used_percentage"))
    if pct is not None:
        parts.append(f"ctx {pct:.0f}%")
    cost = _num(_get(inp, "cost", "total_cost_usd"))
    if cost is not None:
        parts.append(f"${cost:.2f}")
    return " · ".join(parts)


def bar(raw: bytes, inp: dict, wrap: str | None) -> str:
    """The text to show. A wrapped command's stdout passes through byte for byte; if it
    fails, times out or prints nothing, our own bar stands in rather than a blank line."""
    if wrap:
        try:
            r = subprocess.run(wrap, shell=True, input=raw, capture_output=True, timeout=WRAP_TIMEOUT_S)
            out = r.stdout.decode("utf-8", errors="replace")
            if out.strip():
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    return own_bar(inp)


def status_event(inp: dict, machine: str) -> dict | None:
    session = inp.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    payload = {
        "context_pct": _num(_get(inp, "context_window", "used_percentage")),
        "context_window": _num(_get(inp, "context_window", "context_window_size")),
        "cost_usd": _num(_get(inp, "cost", "total_cost_usd")),
        "model": _get(inp, "model", "id") if isinstance(_get(inp, "model", "id"), str) else None,
        "rate_5h_pct": _num(_get(inp, "rate_limits", "five_hour", "used_percentage")),
        "rate_7d_pct": _num(_get(inp, "rate_limits", "seven_day", "used_percentage")),
        "permission_mode": read_mode(session[:128]),
    }
    if payload["context_window"] is not None:
        payload["context_window"] = int(payload["context_window"])
    if isinstance(payload["model"], str):
        payload["model"] = payload["model"][:128]
    return {
        "type": "status",
        "ts": now_iso(),
        "machine": machine,
        "harness": "claude-code",
        "session_id": session[:128],
        "agent_id": None,
        "parent_agent_id": None,
        "payload": {k: v for k, v in payload.items() if v is not None},
    }


def main(argv: list[str]) -> int:
    raw = sys.stdin.buffer.read()
    try:
        inp = json.loads(raw or b"{}")
    except ValueError:
        inp = {}
    if not isinstance(inp, dict):
        inp = {}
    sys.stdout.write(bar(raw, inp, wrapped_command(argv)))
    sys.stdout.flush()  # before any fork, so a child can't re-flush the bar
    cfg = fleet_config()
    if cfg is None:
        return 0
    event = status_event(inp, cfg.machine)
    if event and rate_ok(cfg, event["session_id"], "status", STATUS_EVERY_S):
        emit(cfg, [event])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)
