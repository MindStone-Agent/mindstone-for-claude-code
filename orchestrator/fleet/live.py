"""Live output of running commands for the Fleet board (synapse#31, follow-up to the activity
layer; Hearth's call on #devops, 2026-11-03 20:21Z).

Claude Code writes every Bash command's output, foreground or background, to
`<tmp>/claude-<uid>/<project>/<session>/tasks/<task_id>.output` while it runs (measured on
2.1.317). Nothing in Claude Code streams that file to a hook, so:

1. `live.py --watch` runs as a synchronous SessionStart and CwdChanged hook and returns
   `watchPaths` for the session's tasks directory. Only SessionStart, CwdChanged and FileChanged
   hooks may, and a working-directory change rebuilds the watch list from CwdChanged's answer.
2. FileChanged `add` or `change` reaches report.py, which starts one detached follower for that
   file. A background Bash or Monitor call starts its follower at PostToolUse as well, since
   Claude Code reports a file only once its writes pause.
3. The follower reads the file every POLL_S and sends its new complete lines as `tool.output`
   events. Redaction is never given a fragment:
   - Only complete lines are sent. A partial line is never sent, not even when the follower
     stops; a line of READ_MAX or more is skipped whole.
   - Each batch of lines is redacted together with up to CTX_MAX of the output before it, read
     back from the file, and only the batch's own lines are sent.
   - Lines are held back while a secret that spans lines may still be open: a private-key
     BEGIN with no END yet, a trailing run of lines that look like key material, a Secret's
     `data:` block before its `kind:`. They go out once closed, or when the command ends;
     held past HOLD_MAX they are skipped, with any key lines that follow.
   - After a burst the follower reads only the last READ_MAX bytes, and skips the line that
     read starts in when it starts mid-line.
   Every skip marks the next event `truncated`.
4. It stops when the file is deleted (a foreground command finished) or the session ends, and
   only then sends `done`. It also stops, without `done`, when the task has been idle for
   IDLE_S and isn't among the running tasks Stop last reported, when activity is turned off,
   or after MAX_S; a later `change` resumes from its saved offset.

A foreground command that finishes before the first poll sends nothing: tool.finished
already carries its output.

`tool.output` payload (Cairn's contract on #31, 2026-11-03 20:32Z): task_id, tool_use_id (when
known), seq (from 0 per task; the server drops a repeated seq), text (at most CHUNK_MAX as
sent; empty on the final event), truncated (output was skipped before this chunk), done.
"""
from __future__ import annotations

import fcntl
import hashlib
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
PER_TICK = 4               # events sent per poll at most; older ones are skipped, marked truncated
READ_MAX = 64 * 1024       # bytes read per poll at most; anything before them is skipped
CTX_MAX = 16 * 1024        # earlier output redacted along with new lines (the server's PEM reach)
HOLD_MAX = 16 * 1024       # held lines are skipped past this
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
    """The synchronous SessionStart and CwdChanged hook. Prints nothing unless activity is on:
    SessionStart stdout reaches the model."""
    cfg = c.fleet_config()
    if cfg is None or not cfg.activity:
        return 0
    try:
        inp = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(inp, dict):
        return 0
    event = inp.get("hook_event_name")
    if event not in ("SessionStart", "CwdChanged"):
        return 0
    d = tasks_dir(inp)
    if d is None:
        return 0
    if event == "SessionStart":  # a resumed session keeps its id: it streams again
        try:
            c._state_file(inp["session_id"], "ended").unlink()
        except OSError:
            pass
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)  # as Claude Code creates it
    except OSError:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "watchPaths": [str(d)]}}))
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


def _activity_on() -> bool:
    cfg = c.fleet_config()
    return cfg is not None and cfg.activity


def _claim(session: str, task_id: str) -> int | None:
    """One follower per task: an exclusive lock on its claim file, held by the follower for its
    life (the detached process inherits the descriptor; the lock goes when it exits)."""
    if not c._ensure_state_dir():
        return None
    try:
        fd = os.open(c._state_file(session, f"live.{task_id}"), os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _start(cfg: c.FleetConfig, session: str, path: Path, task_id: str) -> None:
    fd = _claim(session, task_id)
    if fd is None:
        return

    def run() -> None:
        try:
            follow(cfg, session, path, task_id)
        finally:
            os.close(fd)
    if os.environ.get("MS4CC_FLEET_INLINE") == "1":
        return run()
    c.detach(run)
    os.close(fd)  # this process's copy; the follower keeps the lock


def _task_path(inp: dict, file_path: object) -> Path | None:
    d = tasks_dir(inp)
    if not isinstance(file_path, str) or d is None:
        return None
    path = Path(file_path)
    if path.parent != d or path.suffix != ".output" or not TASK_ID.fullmatch(path.stem):
        return None
    return path


def on_file_changed(cfg: c.FleetConfig, inp: dict, session: str) -> None:
    """FileChanged from report.py: start a follower for a task's output file if none runs, or
    send the `done` a stopped follower owed when the file goes."""
    path = _task_path(inp, inp.get("file_path"))
    if path is None or not c._ensure_state_dir():
        return
    if inp.get("event") in ("add", "change"):
        return _start(cfg, session, path, path.stem)
    if inp.get("event") == "unlink":
        fd = _claim(session, path.stem)
        if fd is None:  # a follower is running: it sees the file go
            return
        try:
            st = _load(session, path.stem)
            if not st["done"]:
                _done(cfg, session, path.stem, st)
        finally:
            os.close(fd)


def on_task_started(cfg: c.FleetConfig, inp: dict, session: str, task_id: str) -> None:
    """A background Bash or Monitor call returned its task id: follow it now."""
    d = tasks_dir(inp)
    if d is not None and TASK_ID.fullmatch(task_id):
        _start(cfg, session, d / f"{task_id}.output", task_id)


# --- redaction with context and hold-back -------------------------------------------------------

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_KEYISH_LINE = re.compile(r"[ \t]*[A-Za-z0-9+/=]{20,}[ \t]*")  # could be key material
_DATA_HDR = re.compile(r"(?m)^([ \t]*)\"?(?:data|stringData)\"?[ \t]*:[ \t]*\{?[ \t]*$")
_KIND = re.compile(r"(?m)^([ \t]*)\"?kind\"?[ \t]*:")
_DOC_SPLIT = re.compile(r"(?m)^---[ \t]*$")


def _hold_at(ctx: str, text: str) -> int:
    """Where in `text` (complete lines) sending must stop for now: the start of a secret that
    spans lines and may not be closed yet. len(text) when nothing is open."""
    hold = len(text)
    m = None
    for m in _PEM_BEGIN.finditer(text):
        pass
    if m and not _PEM_END.search(text, m.end()):  # a key block with no END yet
        hold = min(hold, text.rfind("\n", 0, m.start()) + 1)
    lines = text.split("\n")[:-1]  # text ends with a newline
    i = len(lines)
    while i and _KEYISH_LINE.fullmatch(lines[i - 1]):  # an END may follow these
        i -= 1
    if i < len(lines):
        hold = min(hold, sum(len(x) + 1 for x in lines[:i]))
    doc = ctx + text  # a Secret's data before its kind (kubectl prints keys sorted)
    split = hdr = None
    for split in _DOC_SPLIT.finditer(doc):
        pass
    start = split.end() if split else 0
    for hdr in _DATA_HDR.finditer(doc, start):
        pass
    if hdr is not None:
        indent = len(hdr.group(1).expandtabs())
        if not any(len(k.group(1).expandtabs()) <= indent for k in _KIND.finditer(doc, start)):
            hold = min(hold, max(0, hdr.start() - len(ctx)))  # a line start; 0 if in ctx
    return hold


def _key_lines(lines: str) -> int:
    """How many characters of `lines` are leading lines of key material or a key's END line."""
    n = 0
    for line in lines.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if not (_KEYISH_LINE.fullmatch(bare) or _PEM_END.search(bare)):
            break
        n += len(line)
    return n


def _redact_lines(ctx: str, text: str) -> str | None:
    """text (complete lines) redacted along with the output before it; only text's own lines
    are returned. Where redaction joined lines across the boundary, the result may repeat a
    redacted line of the context: never an unredacted one. None when there's no scrubber."""
    from fleet.redact import clean
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    k = norm.count("\n")
    try:
        out = clean(ctx + norm)
    except Exception:
        return None
    parts = out.split("\n")
    if out.endswith("\n"):
        parts = parts[:-1]
    return "\n".join(parts[-k:]) + "\n" if k else ""


def _pieces(text: str) -> list[str]:
    """Already redacted text in pieces of at most CHUNK_MAX as sent, cut after a line break
    where there is one, else after a space."""
    from fleet.redact import _fit, size
    out = []
    while text:
        if size(text) <= CHUNK_MAX:
            out.append(text)
            break
        head = _fit(text, CHUNK_MAX, from_end=False)
        k = head.rfind("\n")
        k = k if k > 0 else head.rfind(" ")
        head = head[: k + 1] if k > 0 else head
        out.append(head)
        text = text[len(head):]
    return out


# --- the follower -------------------------------------------------------------------------------

def _load(session: str, task_id: str) -> dict:
    st = {"offset": 0, "seq": 0, "midline": False, "done": False, "owed": False, "held": False,
          "inkey": False, "ino": None, "head": None}
    try:
        got = json.loads(c._state_file(session, f"live.{task_id}.pos").read_text())
        if isinstance(got, dict):
            for k, v in got.items():
                if k in st and (v is None or type(v) is type(st[k]) or st[k] is None):
                    st[k] = v
    except (OSError, ValueError, TypeError):
        pass
    return st


def _save(session: str, task_id: str, st: dict) -> None:
    try:
        c._state_file(session, f"live.{task_id}.pos").write_text(json.dumps(st))
    except OSError:
        pass


def _event(cfg: c.FleetConfig, session: str, task_id: str, seq: int, text: str | None,
           truncated: bool, done: bool) -> dict:
    tool_use_id = None
    try:
        tool_use_id = c.short(c._state_file(session, f"task.{task_id}").read_text(), 128)
    except OSError:
        pass
    payload = {"task_id": task_id, "tool_use_id": tool_use_id, "seq": seq, "text": text or "",
               "truncated": bool(truncated), "done": done}
    return {"type": "tool.output", "ts": c.now_iso(), "machine": cfg.machine, "harness": "claude-code",
            "session_id": session, "agent_id": None, "parent_agent_id": None,
            "payload": {k: v for k, v in payload.items() if v is not None}}


class _Deadline(Exception):
    pass


def _send(cfg: c.FleetConfig, events: list[dict]) -> bool:
    """One POST, bounded like every other send, without killing the follower on a timeout.
    False when any batch didn't land."""
    if not events:
        return True
    ok = True
    inline = os.environ.get("MS4CC_FLEET_INLINE") == "1"
    if not inline:
        def expire(*_):
            raise _Deadline()
        signal.signal(signal.SIGALRM, expire)
        signal.alarm(c.SEND_DEADLINE_S)
    try:
        for batch in c.batches(events):
            status = c.post(cfg, batch)
            ok = ok and status is not None and 200 <= status < 300
    except _Deadline:
        ok = False
    finally:
        if not inline:
            signal.alarm(0)
    return ok


def _text(b: bytes) -> str:
    return b.decode("utf-8", "surrogateescape")  # round-trips: offsets stay byte-exact


def _sendable(t: str) -> str:
    return t.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _context(fd: int, at: int) -> str:
    """Up to CTX_MAX bytes of the file before `at` (a line start), from a line start."""
    lo = max(0, at - CTX_MAX)
    try:
        b = os.pread(fd, at - lo, lo)
    except OSError:
        return ""
    if lo > 0:
        nl = b.find(b"\n")
        b = b[nl + 1:] if nl >= 0 else b""
    return _sendable(_text(b)).replace("\r\n", "\n").replace("\r", "\n")


def follow(cfg: c.FleetConfig, session: str, path: Path, task_id: str) -> None:
    """Read through an open descriptor, so a file deleted between polls (the command ended)
    can still be read to its end."""
    c._ensure_state_dir()
    st = _load(session, task_id)
    if st["done"]:
        return
    try:
        f = path.open("rb")
    except OSError:
        if st["seq"] and isinstance(path, Path) and not path.exists():
            _done(cfg, session, task_id, st)
        return
    with f:
        _follow(cfg, session, path, task_id, st, f.fileno())


def _done(cfg: c.FleetConfig, session: str, task_id: str, st: dict) -> None:
    """The final event, once per task; it says so if output was skipped or held unsent."""
    if st["seq"]:  # nothing streamed at all: tool.finished carries the output
        _send(cfg, [_event(cfg, session, task_id, st["seq"], None, st["owed"] or st["held"], True)])
        st["seq"] += 1
    st.update(done=True, held=False)
    _save(session, task_id, st)


def _follow(cfg: c.FleetConfig, session: str, path: Path, task_id: str, st: dict, fd: int) -> None:
    ino = os.fstat(fd).st_ino
    if st["ino"] not in (None, ino):  # not the file the saved offset was in
        st.update(offset=0, midline=False, owed=True, head=None)
    st["ino"] = ino
    started = time.time()
    last_size, last_growth = -1, started

    def emit(text: str, context: str) -> None:
        """Redact text with its context and send it; advances seq, clears owed when sent."""
        red = _redact_lines(context, text)
        if red is None:  # no scrubber: nothing is sent, and the gap is marked
            st["owed"] = True
            return
        pieces = _pieces(red)
        if len(pieces) > PER_TICK:
            pieces, st["owed"] = pieces[-PER_TICK:], True
        events = []
        for piece in pieces:
            events.append(_event(cfg, session, task_id, st["seq"], piece, st["owed"], False))
            st["seq"] += 1
            st["owed"] = False
        if not _send(cfg, events):
            st["owed"] = True

    def poll(size: int, final: bool) -> None:
        """Send what may go of the file from st["offset"] to `size`. final: the command ended,
        so its last line is whole and nothing is held."""
        if size - st["offset"] > READ_MAX:  # a burst: read only its last READ_MAX bytes
            st["offset"], st["owed"] = size - READ_MAX, True
            st["midline"] = os.pread(fd, 1, st["offset"] - 1) != b"\n"  # skip a line read mid-way
        data = os.pread(fd, size - st["offset"], st["offset"])
        if st["midline"]:  # the rest of a line already cut: never sent without its start
            nl = data.find(b"\n")
            skip = len(data) if nl < 0 else nl + 1
            st["offset"] += skip
            st["owed"] = st["owed"] or nl != 0  # nl == 0: only the newline went
            st["midline"] = nl < 0
            data = data[skip:]
        if final and data and not data.endswith(b"\n"):
            data += b"\n"
        cut = data.rfind(b"\n")
        if cut < 0:
            if len(data) >= READ_MAX:  # one line of READ_MAX or more: skipped whole
                st["offset"] += len(data)
                st.update(midline=True, owed=True)
            st["held"] = False
            return
        lines = _text(data[: cut + 1])
        if st["inkey"]:  # after a key block held too long: its lines go unsent, to its end
            n = _key_lines(lines)
            if n:
                st["offset"] += len(lines[:n].encode("utf-8", "surrogateescape"))
                st["owed"], lines = True, lines[n:]
            st["inkey"] = not lines
            if not lines:
                st["held"] = False
                return
        ctx = _context(fd, st["offset"])
        norm = lines.replace("\r\n", "\n").replace("\r", "\n")
        stop = len(norm) if final else _hold_at(ctx, norm)
        go = _norm_prefix(lines, stop)
        held = lines[len(go):]
        st["held"] = bool(held)
        if go:
            emit(_sendable(go), ctx)
            st["offset"] += len(go.encode("utf-8", "surrogateescape"))
        if len(held.encode("utf-8", "surrogateescape")) > HOLD_MAX:
            # Held too long to send safely: what opened it may leave the context window. Skip
            # it, and any key lines still to come.
            st["offset"] += len(held.encode("utf-8", "surrogateescape"))
            st.update(owed=True, held=False, inkey=True)

    while True:
        time.sleep(POLL_S)
        now = time.time()
        try:
            gone = path.stat().st_ino != ino  # deleted, or replaced by another file
        except FileNotFoundError:
            gone = True
        except OSError:
            return _save(session, task_id, st)
        size = os.fstat(fd).st_size
        if gone:  # the command ended: read to the end through the open descriptor
            poll(size, final=True)
            return _done(cfg, session, task_id, st)
        if _ended(session):  # what is held stays unsent: the done event says so
            poll(size, final=False)
            return _done(cfg, session, task_id, st)
        if now - started > MAX_S or not _activity_on():
            return _save(session, task_id, st)
        head = hashlib.sha256(os.pread(fd, 64, 0)).hexdigest() if size >= 64 else None
        if size < st["offset"] or (st["head"] and head != st["head"]):  # rewritten in place
            st.update(offset=0, midline=False, owed=True)
        st["head"] = head
        if size == last_size:
            if now - last_growth > IDLE_S and not _running(session, task_id):
                return _save(session, task_id, st)
            continue
        last_size, last_growth = size, now
        poll(size, final=False)
        _save(session, task_id, st)


def _norm_prefix(lines: str, stop: int) -> str:
    """The prefix of `lines` (raw, maybe with CR) that is `stop` characters of its normalised
    form; `stop` is always at a line start there."""
    if stop <= 0:
        return ""
    seen = i = 0
    while i < len(lines) and seen < stop:
        if lines.startswith("\r\n", i):
            i += 2
        else:
            i += 1
        seen += 1
    return lines[:i]


if __name__ == "__main__":
    try:
        sys.exit(watch_main() if "--watch" in sys.argv[1:] else 0)
    except Exception:
        sys.exit(0)
