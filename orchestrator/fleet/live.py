"""Live output of running commands for the Fleet board (synapse#31, follow-up to the activity
layer; Hearth's call on #devops, 2026-11-03 20:21Z).

Claude Code writes every Bash command's output, foreground or background, to
`<tmp>/claude-<uid>/<project>/<session>/tasks/<task_id>.output` while it runs (measured on
2.1.317). Nothing in Claude Code streams that file to a hook: a FileChanged hook does fire on
it, but coalesces writes into one `change` near the end. So:

1. `live.py --watch` runs as a synchronous SessionStart hook and returns `watchPaths` for the
   session's tasks directory (only SessionStart, CwdChanged and FileChanged hooks may).
2. FileChanged `add` (or `change`, if no follower is running) reaches report.py, which starts
   one detached follower for that file.
3. The follower reads the new complete lines every POLL_S, redacts and caps them like every
   other preview (fleet/redact.py), and sends them as `tool.output` events. It stops when the
   file is deleted (a foreground command finished), when the task has been idle for IDLE_S and
   isn't among the running tasks Stop last reported, when the session ends, or after MAX_S.
   Its offset and sequence number are kept on disk, so a later `change` resumes, not repeats.

A foreground command that finishes before the first poll sends nothing: tool.finished
already carries its output.

`tool.output` payload: task_id, tool_use_id (when known), seq, text (at most CHUNK_MAX as
sent), truncated (output was skipped before this chunk), done.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # run as a script by the hook command
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet import common as c  # noqa: E402

POLL_S = float(os.environ.get("MS4CC_FLEET_LIVE_POLL_S", "2"))  # the env knobs are for tests
IDLE_S = float(os.environ.get("MS4CC_FLEET_LIVE_IDLE_S", "60"))
MAX_S = float(os.environ.get("MS4CC_FLEET_LIVE_MAX_S", str(30 * 60)))
CHUNK_MAX = 4096           # one event's text, as sent (JSON-escaped UTF-8)
CHUNK_CHARS = 3000         # what is cut into one chunk before redaction
PER_TICK = 4               # chunks sent per poll at most; older ones are skipped, marked truncated
READ_MAX = 64 * 1024       # bytes read per poll at most; anything before them is skipped
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def tasks_dir(inp: dict) -> Path | None:
    """The session's tasks directory. Claude Code names its per-project temp directory the way
    it names the transcript's directory under ~/.claude/projects (same sanitiser), under
    realpath(CLAUDE_CODE_TMPDIR or /tmp)/claude-<uid>."""
    tp, sid = inp.get("transcript_path"), inp.get("session_id")
    if not isinstance(tp, str) or not tp or not isinstance(sid, str) or c.safe_session(sid) != sid:
        return None
    project = Path(tp).parent.name
    if not project or project in (".", ".."):
        return None
    base = os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp"
    return Path(os.path.realpath(base)) / f"claude-{os.getuid()}" / project / sid / "tasks"


def watch_main() -> int:
    """The synchronous SessionStart hook. Prints nothing unless activity is on: SessionStart
    stdout reaches the model."""
    cfg = c.fleet_config()
    if cfg is None or not cfg.activity:
        return 0
    try:
        inp = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    d = tasks_dir(inp) if isinstance(inp, dict) else None
    if d is None:
        return 0
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)  # as Claude Code creates it
    except OSError:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "watchPaths": [str(d)]}}))
    return 0


# --- state shared with report.py ---------------------------------------------------------------

def remember_task(session: str, task_id: object, tool_use_id: object) -> None:
    """A background Bash or Monitor call's task id, so its stream names the call."""
    if isinstance(task_id, str) and TASK_ID.fullmatch(task_id) and isinstance(tool_use_id, str) and c._ensure_state_dir():
        try:
            c._state_file(session, f"task.{task_id}").write_text(tool_use_id[:128])
        except OSError:
            pass


def remember_running(session: str, background_tasks: object) -> None:
    """Stop's `background_tasks`: the ids still running when the turn ended."""
    if not isinstance(background_tasks, list) or not c._ensure_state_dir():
        return
    ids = [t.get("id") for t in background_tasks if isinstance(t, dict) and isinstance(t.get("id"), str)]
    try:
        c._state_file(session, "bgtasks").write_text(json.dumps(ids[:200]))
    except OSError:
        pass


def mark_ended(session: str) -> None:
    if c._ensure_state_dir():
        try:
            c._state_file(session, "ended").write_text("1")
        except OSError:
            pass


def _running(session: str, task_id: str) -> bool:
    try:
        return task_id in json.loads(c._state_file(session, "bgtasks").read_text())
    except (OSError, ValueError, TypeError):
        return False


def _ended(session: str) -> bool:
    return c._state_file(session, "ended").exists()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _claim(session: str, task_id: str) -> Path | None:
    """One follower per task. A claim whose process is gone is taken over."""
    claim = c._state_file(session, f"live.{task_id}")
    for _ in range(2):
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            return claim
        except FileExistsError:
            try:
                pid = int(claim.read_text() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and _alive(pid):
                return None
            if not pid:  # just created by a follower that hasn't written its pid yet
                try:
                    if time.time() - claim.stat().st_mtime < 5:
                        return None
                except OSError:
                    pass
            try:
                claim.unlink()
            except OSError:
                return None
        except OSError:
            return None
    return None


def on_file_changed(cfg: c.FleetConfig, inp: dict, session: str) -> None:
    """FileChanged from report.py: start a follower for a task's output file if none runs."""
    if inp.get("event") not in ("add", "change"):
        return
    fp, d = inp.get("file_path"), tasks_dir(inp)
    if not isinstance(fp, str) or d is None:
        return
    path = Path(fp)
    if path.parent != d or path.suffix != ".output" or not TASK_ID.fullmatch(path.stem):
        return
    if not c._ensure_state_dir():
        return
    claim = _claim(session, path.stem)
    if claim is None:
        return

    def run() -> None:
        try:
            claim.write_text(str(os.getpid()))
            follow(cfg, session, path, path.stem)
        finally:
            try:
                claim.unlink()
            except OSError:
                pass
    c.detach(run)


# --- the follower -------------------------------------------------------------------------------

def _chunks(text: str) -> list[str]:
    """Pieces of at most CHUNK_CHARS, cut after a line break where there is one."""
    out, i = [], 0
    while i < len(text):
        j = i + CHUNK_CHARS
        if j < len(text):
            k = text.rfind("\n", i, j)
            j = k + 1 if k > i else j
        out.append(text[i:j])
        i = j
    return out


def _load(session: str, task_id: str) -> tuple[int, int]:
    try:
        st = json.loads(c._state_file(session, f"live.{task_id}.pos").read_text())
        return int(st["offset"]), int(st["seq"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0, 0


def _save(session: str, task_id: str, offset: int, seq: int) -> None:
    try:
        c._state_file(session, f"live.{task_id}.pos").write_text(json.dumps({"offset": offset, "seq": seq}))
    except OSError:
        pass


class _Deadline(Exception):
    pass


def _send(cfg: c.FleetConfig, events: list[dict]) -> None:
    """One POST, bounded like every other send, without killing the follower on a timeout."""
    if not events:
        return
    inline = os.environ.get("MS4CC_FLEET_INLINE") == "1"
    if not inline:
        def expire(*_):
            raise _Deadline()
        signal.signal(signal.SIGALRM, expire)
        signal.alarm(c.SEND_DEADLINE_S)
    try:
        for batch in c.batches(events):
            c.post(cfg, batch)
    except _Deadline:
        pass
    finally:
        if not inline:
            signal.alarm(0)


def follow(cfg: c.FleetConfig, session: str, path: Path, task_id: str) -> None:
    from fleet.redact import preview
    c._ensure_state_dir()
    offset, seq = _load(session, task_id)
    started = last_growth = time.time()
    partial = b""

    def event(text: str | None, truncated: bool, done: bool) -> dict:
        nonlocal seq
        tool_use_id = None
        try:
            tool_use_id = c.short(c._state_file(session, f"task.{task_id}").read_text(), 128)
        except OSError:
            pass
        seq += 1
        payload = {"task_id": task_id, "tool_use_id": tool_use_id, "seq": seq, "text": text,
                   "truncated": truncated or None, "done": done}
        return {"type": "tool.output", "ts": c.now_iso(), "machine": cfg.machine, "harness": "claude-code",
                "session_id": session, "agent_id": None, "parent_agent_id": None,
                "payload": {k: v for k, v in payload.items() if v is not None}}

    def finish() -> None:
        events = []
        if partial:  # a last line with no newline
            text, cut_short = preview(partial.decode("utf-8", "replace"), CHUNK_MAX)
            if text is not None:
                events.append(event(text, cut_short, False))
        if events or seq:  # nothing streamed at all: tool.finished carries the output
            events.append(event(None, False, True))
        _send(cfg, events)
        _save(session, task_id, offset, seq)

    while True:
        time.sleep(POLL_S)
        now = time.time()
        if now - started > MAX_S or _ended(session):
            return finish()
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return finish()  # a foreground command finished
        except OSError:
            return finish()
        if size < offset:  # rewritten from the start
            offset, partial = 0, b""
        if size == offset:
            if now - last_growth > IDLE_S and not _running(session, task_id):
                return finish()
            continue
        last_growth = now
        skipped = False
        if size - offset > READ_MAX:
            offset, partial, skipped = size - READ_MAX, b"", True
        try:
            with path.open("rb") as f:
                f.seek(offset)
                data = f.read(size - offset)
        except OSError:
            return finish()
        offset += len(data)
        data = partial + data
        cut = data.rfind(b"\n")
        if cut < 0 and len(data) < CHUNK_CHARS:
            partial = data  # wait for the rest of the line
            continue
        if cut >= 0:
            data, partial = data[: cut + 1], data[cut + 1:]
        else:
            partial = b""
        pieces = _chunks(data.decode("utf-8", "replace"))
        if len(pieces) > PER_TICK:
            pieces, skipped = pieces[-PER_TICK:], True
        events = []
        for i, piece in enumerate(pieces):
            text, cut_short = preview(piece, CHUNK_MAX)
            if text is None:  # no scrubber: send nothing rather than unredacted output
                continue
            events.append(event(text, (skipped and i == 0) or cut_short, False))
        _send(cfg, events)
        _save(session, task_id, offset - len(partial), seq)  # a resumed follower rereads the partial line


if __name__ == "__main__":
    try:
        sys.exit(watch_main() if "--watch" in sys.argv[1:] else 0)
    except Exception:
        sys.exit(0)
