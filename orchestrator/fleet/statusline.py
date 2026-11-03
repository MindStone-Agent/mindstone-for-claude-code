"""Fleet Phase 0 status line (synapse#31 section 5).

Claude Code runs this as the `statusLine` command and shows what it prints. It does two
things, in this order:

1. **Prints the bar.** If the user already had a status line, bootstrap wrapped it rather
   than replacing it: its command arrives here base64-encoded after `--wrap-b64`, runs with
   the same stdin and inherited stderr, and its stdout bytes and exit code pass through
   unchanged. With nothing to wrap, or if the wrapped command can't start or runs past
   WRAP_TIMEOUT_S, it prints a short bar of its own (model, context %, cost). Nothing outside
   the standard library is imported before the bar is out, so a broken MS4CC install can't
   blank the user's bar.
2. **Reports `status`** to Synapse, only when `[fleet] enabled = true`: at most once every
   10 s per session, from a detached grandchild. An update that falls inside the window is
   kept and sent when the window ends, so the last update of a turn isn't lost.

Privacy: the status payload is built from the allow-listed fields below only.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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


def own_bar(inp: dict) -> bytes:
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
    return " · ".join(parts).encode("utf-8")


def bar(raw: bytes, inp: dict, wrap: str | None) -> tuple[bytes, int]:
    """(bytes to show, exit code). A wrapped command's stdout and exit code pass through as
    they are, so Claude Code treats them exactly as it did before the wrap."""
    if wrap:
        try:
            r = subprocess.run(wrap, shell=True, input=raw, stdout=subprocess.PIPE, timeout=WRAP_TIMEOUT_S)
            return r.stdout, r.returncode
        except (OSError, subprocess.SubprocessError):
            pass
    return own_bar(inp), 0


def status_event(inp: dict, machine: str, now_iso, read_mode) -> dict | None:
    session = inp.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    model = _get(inp, "model", "id")
    payload = {
        "context_pct": _num(_get(inp, "context_window", "used_percentage")),
        "context_window": _num(_get(inp, "context_window", "context_window_size")),
        "cost_usd": _num(_get(inp, "cost", "total_cost_usd")),
        "model": model[:128] if isinstance(model, str) and model else None,
        "rate_5h_pct": _num(_get(inp, "rate_limits", "five_hour", "used_percentage")),
        "rate_7d_pct": _num(_get(inp, "rate_limits", "seven_day", "used_percentage")),
        "permission_mode": read_mode(session[:128]),
    }
    if payload["cost_usd"] is not None:
        payload["cost_usd"] = round(payload["cost_usd"], 4)
    if payload["context_window"] is not None:
        payload["context_window"] = int(payload["context_window"])
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


def report(inp: dict) -> None:
    """Send `status`, or keep it for the end of the window. Imported lazily, after the bar."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fleet import common as c

    cfg = c.fleet_config()
    if cfg is None:
        return
    event = status_event(inp, cfg.machine, c.now_iso, c.read_mode)
    if event is None:
        return
    sid = event["session_id"]
    pending = c._state_file(sid, "status.pending")
    if c.rate_ok(sid, "status", STATUS_EVERY_S):
        try:
            pending.unlink()
        except OSError:
            pass
        c.emit(cfg, [event])
        return
    # Inside the window: keep the newest snapshot, and make sure one sender is waiting to
    # send it when the window ends. The flusher claim is a file created exclusively; a claim
    # older than the window plus the send deadline is stale (its process died) and is taken.
    if not c._ensure_state_dir():
        return
    tmp = c._state_file(sid, "status.pending.tmp")
    try:
        tmp.write_text(json.dumps(event))
        tmp.replace(pending)
    except OSError:
        return
    claim = c._state_file(sid, "status.flusher")
    try:
        if time.time() - claim.stat().st_mtime > STATUS_EVERY_S + c.SEND_DEADLINE_S + 5:
            claim.unlink()
    except OSError:
        pass
    try:
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return  # a flusher is already waiting; it sends the newest pending snapshot
    delay = c.rate_remaining(sid, "status", STATUS_EVERY_S)

    def flush() -> None:
        try:
            time.sleep(delay + 0.05)
            try:
                ev = json.loads(pending.read_text())
                pending.unlink()
            except (OSError, ValueError):
                return  # a direct send already went out and cleared it
            if c.rate_ok(sid, "status", STATUS_EVERY_S):
                c.send_now(cfg, [ev])
        finally:
            try:
                claim.unlink()
            except OSError:
                pass
    c.detach(flush)


def main(argv: list[str]) -> int:
    raw = sys.stdin.buffer.read()
    try:
        inp = json.loads(raw or b"{}")
    except ValueError:
        inp = {}
    if not isinstance(inp, dict):
        inp = {}
    out, code = bar(raw, inp, wrapped_command(argv))
    try:
        sys.stdout.buffer.write(out)
        sys.stdout.flush()  # before any fork, so a child can't re-flush the bar
    except (OSError, ValueError):
        pass
    try:
        report(inp)
    except Exception:
        pass
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)
