"""Live output of running commands for the Fleet board (synapse#31, follow-up to the activity
layer; Hearth's call on #devops, 2026-11-03 20:21Z).

Claude Code writes every Bash command's output, foreground or background, to
`<tmp>/claude-<uid>/<project>/<session>/tasks/<task_id>.output` while it runs (measured on
2.1.317). Nothing in Claude Code streams that file to a hook, so:

1. `live.py --watch` runs as a synchronous SessionStart and CwdChanged hook and returns
   `watchPaths` for the session's tasks directory. Only SessionStart, CwdChanged and FileChanged
   hooks may, and a working-directory change rebuilds the watch list from CwdChanged's answer.
   Every session gets it (one claimed while running can't be told apart at its start); the
   gate is report.py, which sends nothing for a session that isn't the agent's.
2. FileChanged `add` or `change` reaches report.py, which starts one detached follower for that
   file. A background Bash or Monitor call starts its follower at PostToolUse as well, since
   Claude Code reports a file only once its writes pause.
3. The follower reads the file every POLL_S, through a descriptor it keeps open, and sends its
   new complete lines as `tool.output` events. Redaction is never handed a fragment:
   - Only complete lines are sent. A partial line waits for its end; when the command ends,
     its last line is whole and goes too.
   - A line scanner (_scan), whose state is kept across polls, withholds a private key from
     BEGIN to END (any line of key characters inside it, of any length, and an encrypted key's
     headers). A Kubernetes `data:` or `stringData:` block isn't judged by the scanner: it
     waits, and so does the output after it, until its document ends (a `---` line, as the
     server splits them) or the command does, then goes to the redactor with what follows in
     one piece, which decides it as it does for the whole output. Past HOLD_MAX a waiting block
     is withheld. One WITHHELD line stands in for each run withheld.
   - What the scanner lets through is redacted along with up to CTX_MAX of the output before
     it (not past a withheld run). Only the part after the context's own redaction is sent;
     if the new lines change how the context redacts, nothing is sent and the gap is marked.
   - After a burst only the last READ_MAX bytes are read, and the line that read starts in is
     skipped; the bytes skipped are run through the scanner first, so it knows whether the
     read starts inside a key block or a Secret. Past REPLAY_MAX only the last REPLAY_MAX are,
     and indented lines are withheld until one at the margin.
   Every skip marks the next event `truncated`.
4. It stops and sends `done` when the file is deleted (the command ended; it reads to the end
   first) or the session ends. It stops without `done` when a background task has been idle
   for IDLE_S and Claude Code doesn't list it as running, when activity is turned off, or after
   MAX_S; a later `change` resumes from the saved offset. A foreground task is never let go
   when idle: its file goes when it ends. When a stopped task's file goes, or the session
   ends, with no follower running, its `done` is sent then, marked truncated, since what was
   written after the stop can't be known; a file that goes while its follower is stopping
   gets its `done` from that follower, once it has let go of the task.

A command whose file is gone before its follower opens it streams nothing: tool.finished
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
HOLD_MAX = 16 * 1024       # lines waiting on a decision are withheld past this
REPLAY_MAX = 4 * 1024 * 1024  # bytes a burst skips that are replayed through the scanner
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
    # Every session gets the watch, marked or not: a session claimed while running (claim.py)
    # can't be told apart at SessionStart, and FileChanged reaches report.py, which sends
    # nothing for a session that isn't the agent's (no network call). Live output review 3.
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


def _background(session: str, task_id: str) -> bool:
    """A background Bash or Monitor call's task (remember_task saw its id)."""
    return c._state_file(session, f"task.{task_id}").exists()


def end_session(cfg: c.FleetConfig, session: str) -> None:
    """SessionEnd: followers still running see the mark and finish; a task whose follower has
    already stopped gets its done here."""
    mark_ended(session)
    try:
        pos = list(c.STATE_DIR.glob(f"{c.safe_session(session)}.live.*.pos"))
    except OSError:
        return
    for p in pos[:200]:
        task_id = p.name[len(c.safe_session(session)) + len(".live."):-len(".pos")]
        if not TASK_ID.fullmatch(task_id):
            continue
        fd = _claim(session, task_id)
        if fd is None:  # its follower is running
            continue
        try:
            st = _load(session, task_id)
            if not st["done"]:
                _done(cfg, session, task_id, st)
        finally:
            os.close(fd)


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
        # The file may have gone after this follower stopped (idle, MAX_S) but before it let
        # go of the lock, when an unlink's done saw the lock held: send that done now.
        if not path.exists():
            again = _claim(session, task_id)
            if again is not None:
                try:
                    st = _load(session, task_id)
                    if not st["done"] and (st["stopped"] or st["seq"]):  # not a file that never was
                        _done(cfg, session, task_id, st)
                finally:
                    os.close(again)
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


# --- what may go: a line scanner, then redaction with context ---------------------------------
# Secrets that span lines are handled here, line by line, with state carried across polls, not
# left to redaction of a window: a window's edge can cut a key block or a Secret from what
# makes it one (live output review rounds 1 and 2).

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_KEYISH = re.compile(r"[ \t]*[A-Za-z0-9+/=]{20,}[ \t]*")  # could be key material
_DOC_SPLIT = re.compile(r"---[ \t]*")
# The key at the line's start, or after other keys of the same JSON line ({"apiVersion": "v1",
# "data": {), as a hand-written or partly minified document has it.
_DATA_HDR = re.compile(r"(?:[ \t]*|[^\n]{0,4096}[{,][ \t]*)\"?(?:data|stringData)\"?[ \t]*:[ \t]*(?:\{[ \t]*)?")
_CLOSER = re.compile(r"[ \t]*[}\]],?[ \t]*")
# An encrypted key's headers and the blank line after them are inside its block (round 4).
_PEM_HDR = re.compile(r"(?:Proc-Type|DEK-Info|Comment)[ \t]*:[^\n]*|[ \t]*")
_B64_LINE = re.compile(r"[ \t]*[A-Za-z0-9+/=]+[ \t]*")  # inside a key block: key material, any length
WITHHELD = "[REDACTED-SECRET-LINES]"  # stands in for a run of lines not sent


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _bare(raw: str) -> str:
    return raw.rstrip("\n").rstrip("\r")


def _ends_doc(line: str) -> bool:
    """A `---` line: where the server splits documents, and so where a Secret's reach ends. A
    closing brace at the margin is not one (round 4: a flow mapping's own brace "ended" the
    document before its kind: Secret, and its data went out)."""
    return bool(_DOC_SPLIT.fullmatch(line))


def _scan(raw: list[str], st: dict, final: bool, replay: bool = False) -> tuple[list[str], int, int]:
    """Which of `raw` (complete lines, each ending in a newline) may be sent. Returns the lines
    to send, with WITHHELD for each run held back as secret, how many lines were dealt with (the
    rest wait for a later poll), and the index just past the last line withheld (0 if none).
    st's per-document state (secret_doc, data_indent, inkey) is carried across polls and
    reflects the lines dealt with.

    - A private key, BEGIN to END, is never sent; nor, after a BEGIN with no END, the base64
      lines that follow it (of any length: a key's last line can be short).
    - A `data:` or `stringData:` block isn't judged here. It waits, and so does the output after
      it, until its document ends (a `---` line, where the server splits documents) or the
      output does; then it goes with what follows to the redactor in one piece, which reads its
      kind and propagates its values the way it does for the whole output (live output review
      7: decided here, a Secret's withheld values never reached the redactor, and an echo of
      one later in the output went out). Past HOLD_MAX it is withheld, and so is the rest of
      its document's data (secret_doc).
    - Key-like lines with nothing after them yet wait for the next line, so that a key's tail
      and its END line are redacted together.

    replay: lines skipped unread by a burst, run through only to bring the state up to date;
    nothing waits, and a block still open at the end stays open."""
    out: list[str] = []
    final = final or replay
    wend = [0]
    n = len(raw)
    bare = [_bare(x) for x in raw]
    # One reverse pass: where each line's document ends.
    doc_end = [n] * (n + 1)
    for x in range(n - 1, -1, -1):
        doc_end[x] = x if _DOC_SPLIT.fullmatch(bare[x]) else doc_end[x + 1]

    def withhold(end: int) -> None:
        wend[0] = end
        if out and out[-1] == WITHHELD + "\n" or not out and st["withheld_last"]:
            return
        out.append(WITHHELD + "\n")

    def size_from(k: int) -> int:
        return sum(len(x.encode("utf-8", "surrogateescape")) for x in raw[k:])

    i = 0
    while i < n:
        line = bare[i]
        if st["inkey"]:
            if _PEM_END.search(line):
                st["inkey"] = False
                withhold(i + 1)
                i += 1
                continue
            if _B64_LINE.fullmatch(line) or _PEM_HDR.fullmatch(line):
                withhold(i + 1)
                i += 1
                continue
            st["inkey"] = False
        if st["data_indent"] is not None:
            h = st["data_indent"]
            j = i  # the block: deeper lines and blanks, then a closing brace at h
            while j < n and (not bare[j].strip() or _indent(bare[j]) > h):
                j += 1
            if j < n and _CLOSER.fullmatch(bare[j]) and _indent(bare[j]) == h:
                j += 1
            if st["secret_doc"]:
                secret = True  # held past HOLD_MAX earlier in this document, or a blind replay
            elif doc_end[i] < n or final:
                secret = False  # the document has ended: to the redactor with what follows
            elif size_from(i) > HOLD_MAX:
                secret, st["secret_doc"] = True, True  # held too long: withheld, and the rest
                j = n
            else:
                return _ended_with(out, st), i, wend[0]  # wait for the document's end
            if secret:
                if j > i:
                    withhold(j)
            else:
                out.extend(raw[i:j])
            i = j
            if j < n or (final and not replay):
                st["data_indent"] = None
            continue
        if _ends_doc(line):
            st.update(secret_doc=False, data_indent=None, inkey=False)
            out.append(raw[i])
            i += 1
            continue
        if _PEM_BEGIN.search(line):
            st["inkey"] = not _PEM_END.search(line)
            withhold(i + 1)
            i += 1
            continue
        if _DATA_HDR.fullmatch(line):
            st["data_indent"] = _indent(line)
            out.append(raw[i])
            i += 1
            continue
        if _KEYISH.fullmatch(line):
            r = i
            while r < n and _KEYISH.fullmatch(bare[r]):
                r += 1
            if r == n and not final:
                if size_from(i) <= HOLD_MAX:
                    return _ended_with(out, st), i, wend[0]  # an END may follow
                st["inkey"] = True  # too long to wait for: withheld, and key lines after it
                withhold(n)
                return _ended_with(out, st), n, wend[0]
            out.extend(raw[i:r])
            i = r
            continue
        out.append(raw[i])
        i += 1
    return _ended_with(out, st), n, wend[0]


def _ended_with(out: list[str], st: dict) -> list[str]:
    """Remember whether the last thing sent was the withheld marker, so the next poll's
    withheld run doesn't repeat it."""
    if out:
        st["withheld_last"] = out[-1] == WITHHELD + "\n"
    return out


def _redact(ctx: str, text: str) -> str | None:
    """text redacted with the output before it as context, so a secret seen in the context
    (its key, its first appearance) is redacted in text too. Only what follows the context's
    own redaction is returned, and only when the joint redaction begins with exactly it; when
    the new lines change how the context redacts (a key block or a Secret across the join),
    the result is None and nothing is sent. Also None when there's no scrubber."""
    from fleet.redact import clean
    try:
        head = clean(ctx)
        out = clean(ctx + text)
    except Exception:
        return None
    if not out.startswith(head):
        return None
    return out[len(head):]


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
          "stopped": False, "inkey": False, "secret_doc": False, "data_indent": None, "withheld_last": False, "ctx_from": 0,
          "ino": None, "head": None}
    try:
        got = json.loads(c._state_file(session, f"live.{task_id}.pos").read_text())
        if isinstance(got, dict):
            for k, v in got.items():
                if k == "data_indent" and not (v is None or type(v) is int):
                    continue
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


def _head(fd: int, n: int) -> str:
    return hashlib.sha256(os.pread(fd, n, 0)).hexdigest()


def _context(fd: int, at: int, since: int = 0) -> str:
    """Up to CTX_MAX bytes of the file before `at` (a line start) and from `since` on: context
    for redaction, never sent itself. It starts at a line's start: a piece of a line read from
    its middle (a burst skipped its head) can hold half a JSON string, and the redactor cuts
    its output at a string left open (synapse#41), which would drop the lines after it."""
    lo = max(0, at - CTX_MAX, min(since, at))
    try:
        if lo > 0 and os.pread(fd, 1, lo - 1) != b"\n":
            nl = os.pread(fd, at - lo, lo).find(b"\n")
            lo = at if nl < 0 else lo + nl + 1
        return _sendable(_text(os.pread(fd, at - lo, lo)))
    except OSError:
        return ""


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
    """The final event, once per task. truncated: output was skipped, held unsent, or written
    after a follower stopped without done and before none resumed (it can't be known)."""
    if st["seq"]:  # nothing streamed at all: tool.finished carries the output
        lost = st["owed"] or st["held"] or st["stopped"]
        _send(cfg, [_event(cfg, session, task_id, st["seq"], None, lost, True)])
        st["seq"] += 1
    st.update(done=True, held=False)
    _save(session, task_id, st)


def _follow(cfg: c.FleetConfig, session: str, path: Path, task_id: str, st: dict, fd: int) -> None:
    ino = os.fstat(fd).st_ino
    if st["ino"] not in (None, ino):  # not the file the saved offset was in
        st.update(offset=0, midline=False, owed=True, head=None, secret_doc=False, data_indent=None, inkey=False, ctx_from=0)
    st["ino"], st["stopped"] = ino, False  # this follower reads on from the saved offset
    started = time.time()
    last_size, last_growth = -1, started

    def emit(text: str, context: str) -> None:
        """Redact text with its context and send it; advances seq, clears owed when sent."""
        red = _redact(context, text)
        if red is None:  # no scrubber, or the join changed the context: nothing is sent, marked
            st["owed"] = True
            return
        pieces = _pieces(_sendable(red))
        if len(pieces) > PER_TICK:
            pieces, st["owed"] = pieces[-PER_TICK:], True
        events = []
        for piece in pieces:
            events.append(_event(cfg, session, task_id, st["seq"], piece, st["owed"], False))
            st["seq"] += 1
            st["owed"] = False
        if not _send(cfg, events):
            st["owed"] = True

    def replay(lo: int, hi: int) -> None:
        """Bring the scanner's state through bytes a burst skips unread: whether a key block or
        a Secret's data is open where the read starts. Past REPLAY_MAX only the end is replayed,
        from a state that takes the document for a Secret and every indented line for its data,
        withheld until a line at the margin (where the read starts is unknown)."""
        if hi - lo > REPLAY_MAX:
            lo = hi - REPLAY_MAX
            st.update(secret_doc=True, data_indent=0, inkey=False, midline=True)
        b = os.pread(fd, hi - lo, lo)
        if st["midline"]:
            nl = b.find(b"\n")
            b = b[nl + 1:] if nl >= 0 else b""
        # The line the read starts in is skipped, not sent, but it may be the one that decides
        # (a Secret's kind): replay it whole.
        rest = os.pread(fd, READ_MAX, hi)
        nl = rest.find(b"\n")
        b += rest[: nl + 1] if nl >= 0 else b""
        cut = b.rfind(b"\n")
        raw = [x + "\n" for x in _text(b[: cut + 1]).split("\n")[:-1]] if cut >= 0 else []
        _scan(raw, st, final=False, replay=True)
        # The bytes skipped are context for what follows (never sent): a Secret's block the
        # burst skipped lets the redactor propagate its values to an echo after it (review 7).

    def poll(size: int, final: bool) -> None:
        """Send what may go of the file from st["offset"] to `size`. final: the command ended,
        so its last line is whole and nothing waits."""
        if size - st["offset"] > READ_MAX:  # a burst: read only its last READ_MAX bytes
            replay(st["offset"], size - READ_MAX)
            st["offset"], st["owed"] = size - READ_MAX, True
            st["midline"] = os.pread(fd, 1, st["offset"] - 1) != b"\n"  # skip a line read mid-way
        data = os.pread(fd, size - st["offset"], st["offset"])
        if st["midline"]:  # the rest of a line already cut: never sent without its start
            nl = data.find(b"\n")
            skip = len(data) if nl < 0 else nl + 1
            st["offset"] += skip  # owed already: a burst or a resumed skip set it
            st["midline"] = nl < 0
            data = data[skip:]
        if final and data and not data.endswith(b"\n"):
            data += b"\n"
        cut = data.rfind(b"\n")
        if cut < 0:  # wait for the line's end; a line past READ_MAX is skipped as a burst
            st["held"] = False
            return
        raw = [x + "\n" for x in _text(data[: cut + 1]).split("\n")[:-1]]
        ctx = _context(fd, st["offset"], st["ctx_from"])
        go, used, wend = _scan(raw, st, final)
        st["held"] = used < len(raw)
        if go:
            emit(_sendable("".join(go)), ctx)

        def nbytes(k: int) -> int:
            return sum(len(x.encode("utf-8", "surrogateescape")) for x in raw[:k])
        if wend:  # withheld lines are never context: a later kind: Secret would re-redact them
            st["ctx_from"] = st["offset"] + nbytes(wend)
        st["offset"] += nbytes(used)

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
            st["stopped"] = True
            return _save(session, task_id, st)
        # Rewritten in place: shorter than what was read, or its first bytes changed. Compared on
        # the length first hashed, so a file still under 64 bytes is checked too.
        h = st["head"]
        seen = h if isinstance(h, list) and len(h) == 2 and type(h[0]) is int and isinstance(h[1], str) else None
        if size < st["offset"] or (seen and (size < seen[0] or _head(fd, seen[0]) != seen[1])):
            st.update(offset=0, midline=False, owed=True, secret_doc=False, data_indent=None, inkey=False, ctx_from=0)
            seen = None
        n_head = min(size, 64)
        if n_head and (seen is None or n_head > seen[0]):
            st["head"] = [n_head, _head(fd, n_head)]
        if size == last_size:
            # Only a background task Claude Code no longer lists as running is let go when
            # idle: a foreground task's file goes when it ends, so it is followed until then.
            if now - last_growth > IDLE_S and _background(session, task_id) and not _running(session, task_id):
                st["stopped"] = True
                return _save(session, task_id, st)
            continue
        last_size, last_growth = size, now
        poll(size, final=False)
        _save(session, task_id, st)


if __name__ == "__main__":
    try:
        sys.exit(watch_main() if "--watch" in sys.argv[1:] else 0)
    except Exception:
        sys.exit(0)
