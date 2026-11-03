"""Fleet Phase 0 status line (synapse#31 section 5).

Claude Code runs this as the `statusLine` command and shows what it prints. It does two
things, in this order:

1. **Prints the bar.** Bootstrap installs this only while Fleet is on. If the user already
   had a status line, bootstrap wrapped it rather than replacing it: its command arrives here
   base64-encoded after `--wrap-b64`, runs under /bin/sh (as Claude Code runs it) with the
   same stdin and inherited stderr, and its stdout bytes and exit code pass through
   unchanged. With nothing to wrap, or if the wrapped command runs past WRAP_TIMEOUT_S (it is
   then killed with its whole process group), it prints a short bar of its own (model,
   context %, cost). Nothing outside the standard library
   is imported before the bar is out, so a Fleet module that can't load (no tomllib on Python
   3.10, say) can't blank the user's bar. A missing or broken MS4CC venv still can: the command
   runs on the venv's python.
2. **Reports `status`** to Synapse, only when `[fleet] enabled = true`: at most once every
   10 s per session, from a detached grandchild. An update that falls inside the window is
   kept and sent when the window ends, so the last update of a turn isn't lost.

Privacy: the status payload is built from the allow-listed fields below only.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STATUS_EVERY_S = 10.0
WRAP_TIMEOUT_S = 5.0


def _num(v: object) -> float | None:
    """A finite float, or None (a bool, a string, NaN, infinity, an int too big for a float)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


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
        out = _run_wrapped(wrap, raw)
        if out is not None:
            return out
    try:
        return own_bar(inp), 0
    except Exception:
        return b"", 0


def _run_wrapped(wrap: str, raw: bytes) -> tuple[bytes, int] | None:
    """Run the user's command in a process group of its own, so a timeout kills everything it
    started, not just /bin/sh. None if it can't run or runs past WRAP_TIMEOUT_S."""
    try:
        p = subprocess.Popen(wrap, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = p.communicate(raw, timeout=WRAP_TIMEOUT_S)
        return out, p.returncode
    except (OSError, subprocess.SubprocessError):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=1)
        except subprocess.SubprocessError:
            pass
        for f in (p.stdin, p.stdout):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        return None


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
        c.mark_sent(sid, event["ts"])
        c.emit(cfg, [event])
        return
    # Inside the window: keep the newest snapshot, and make sure one sender is waiting to
    # send it when the window ends. The flusher claim is a file created exclusively; a claim
    # older than the window plus the send deadline is stale (its process died) and is taken.
    if not c._ensure_state_dir():
        return
    tmp = c._state_file(sid, f"status.pending.{os.getpid()}.tmp")  # one per writer: no interleaving
    try:
        tmp.write_text(json.dumps(event))
        tmp.replace(pending)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
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

    def release() -> None:
        try:
            claim.unlink()
        except OSError:
            pass

    def flush() -> None:
        """Wait out the window, then send the newest pending snapshot. The claim is released
        before the snapshot is taken, so any update that lands after that point gets a flusher
        of its own. If a direct send opened a new window first, the snapshot goes back (unless a
        newer one is waiting, or it is older than what was sent), and this flusher waits again
        unless another flusher holds the claim, in which case that one sends it."""
        for _ in range(6):
            time.sleep(c.rate_remaining(sid, "status", STATUS_EVERY_S) + 0.05)
            release()  # from here on, an update that lands starts a flusher of its own
            mine = c._state_file(sid, f"status.flushing.{os.getpid()}")
            try:  # take the snapshot by renaming it, so a newer one can't be unlinked unseen
                os.rename(pending, mine)
            except OSError:
                return  # nothing waiting: a direct send already carried it
            try:
                ev = json.loads(mine.read_text())
            except (OSError, ValueError):
                ev = None
            try:
                mine.unlink()
            except OSError:
                pass
            if not isinstance(ev, dict):
                return
            if ev.get("ts", "") <= c.last_sent(sid):
                return  # something newer already went out
            if c.rate_ok(sid, "status", STATUS_EVERY_S):
                c.mark_sent(sid, ev.get("ts", ""))
                c.send_now(cfg, [ev])
                return
            try:  # put it back, unless a newer snapshot is already pending
                fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, json.dumps(ev).encode())
                os.close(fd)
            except OSError:
                pass
            try:  # then wait again, unless another flusher holds the claim: it hasn't read
                os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except OSError:  # yet (holders release before reading), so it sends what is pending now
                return
        release()
    c.detach(flush)


def main(argv: list[str]) -> int:
    raw = sys.stdin.buffer.read()
    try:
        inp = json.loads(raw or b"{}")
    except Exception:  # ValueError, and RecursionError for deeply nested input
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
