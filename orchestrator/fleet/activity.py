"""Fleet activity layer, Claude Code side (synapse#31, scope change of 2026-11-03).

What each agent is doing, for the owner's board: every tool call with a redacted preview of its
input and output, the agent's own replies, and the subagent tree. Off unless both
`[fleet] enabled = true` and `[fleet] activity = true`, and never sent over plain http to
anything but localhost (FleetConfig.activity).

Event shapes follow the server's allow-list (synapse feat/fleet-activity, api/fleet.py):
- tool.started:  tool_use_id, tool_name, input_preview (4 KB), input_truncated
- tool.finished: tool_use_id, tool_name, ok, duration_ms, output_preview (16 KB),
                 output_truncated, or error (16 KB) for a failed call
- message:       message_id, text (8 KB), truncated
- subagent.started gains description (240) and parent_tool_use_id

What is shown, per tool (Hearth's ruling on #devops, 2026-11-03: cap and redact, don't hide):
the Agent prompt and the subagent's report, what Write writes (head and tail), Edit's old and
new strings, Bash's command and output, any other tool's input and output as compact JSON.
Everything is redacted (fleet/redact.py) before it is cut to its cap.

Never sent: the contents of files Read returns (a .env would otherwise ship), and the user's
own words: their prompts (they are in the transcript too, in `user` and `api-request-blob`
lines; only `assistant` text blocks are read) and their answers to AskUserQuestion. Nor are
thinking blocks.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

from fleet import common as c
from fleet.redact import preview

INPUT_MAX = 4096
OUTPUT_MAX = 16384
MESSAGE_MAX = 8192
DESCRIPTION_MAX = 240
TAIL_MAX_BYTES = 8 * 1024 * 1024   # the most new transcript read per tail; older bytes are skipped
AGENT_TOOLS = ("Agent", "Task")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
PAIR_TTL_S = 120   # an Agent call not started within this (denied, failed, blocked) stops pairing
DEFAULT_AGENT_TYPE = "general-purpose"  # what Claude Code runs when subagent_type is left out
_NO_DROP: frozenset = frozenset()
_ANSWERS = frozenset({"answers", "annotations"})  # AskUserQuestion: the user's own words
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _lines(s: object) -> int:
    """Lines as an editor counts them: a final newline doesn't start another."""
    return s.count("\n") + (0 if s.endswith("\n") else 1) if isinstance(s, str) and s else 0


def _without(v: object, drop: frozenset, depth: int = 0) -> object:
    if depth > 32:
        return None
    if isinstance(v, dict):
        return {k: _without(x, drop, depth + 1) for k, x in v.items() if k not in drop}
    if isinstance(v, list):
        return [_without(x, drop, depth + 1) for x in v]
    return v


def _dump(v: object, drop: frozenset) -> str | None:
    try:
        return json.dumps(_without(v, drop), ensure_ascii=False, separators=(", ", ": "))
    except (TypeError, ValueError):
        return None


def input_text(tool_name: str, ti: object) -> str | None:
    """The raw (unredacted) input text worth showing for one call."""
    if not isinstance(ti, dict):
        return None
    if tool_name == "Bash":
        return _str(ti.get("command"))
    if tool_name == "Read":
        extra = "".join(f" {k}={ti[k]}" for k in ("offset", "limit") if isinstance(ti.get(k), int))
        return (_str(ti.get("file_path")) or "") + extra or None
    if tool_name == "Write":
        return f"{ti.get('file_path', '')} ({_lines(ti.get('content'))} lines)"
    if tool_name == "Edit":
        all_ = " all" if ti.get("replace_all") is True else ""
        return f"{ti.get('file_path', '')} (-{_lines(ti.get('old_string'))}/+{_lines(ti.get('new_string'))} lines{all_})"
    if tool_name == "MultiEdit":
        edits = ti.get("edits") if isinstance(ti.get("edits"), list) else []
        return f"{ti.get('file_path', '')} ({len(edits)} edits)"
    if tool_name == "NotebookEdit":
        return _str(ti.get("notebook_path"))
    if tool_name in ("Glob", "Grep"):
        parts = [ti.get("pattern")] + [f"{k}={ti[k]}" for k in ("path", "glob", "type") if _str(ti.get(k))]
        return " ".join(p for p in parts if _str(p)) or None
    if tool_name == "WebFetch":
        return _str(ti.get("url"))
    if tool_name == "WebSearch":
        return _str(ti.get("query"))
    if tool_name in AGENT_TOOLS:  # what the subagent was asked to do
        head = " ".join(p for p in (_str(ti.get("subagent_type")), _str(ti.get("description"))) if p)
        prompt = _str(ti.get("prompt"))
        return (head + ("\n\n" + prompt if prompt else "")).strip() or None
    return _dump(ti, _ANSWERS if tool_name == "AskUserQuestion" else _NO_DROP)


def _capped(text: object, limit: int) -> str:
    """One field of a multi-part preview, redacted and cut on its own, so a long old string
    can't push the new one out of the preview."""
    t, _ = preview(text if isinstance(text, str) else None, limit)
    return t or ""


def _diff(path: object, pairs: list[tuple[object, object]]) -> str:
    """`--- old` / `+++ new` blocks, each capped, for the board to render as a diff."""
    budget = max(256, (OUTPUT_MAX - 512) // max(1, 2 * len(pairs)))
    parts = [str(path or "")]
    for old, new in pairs[:16]:
        parts += ["--- old", _capped(old, budget), "+++ new", _capped(new, budget)]
    if len(pairs) > 16:
        parts.append(f"… {len(pairs) - 16} more edits")
    return "\n".join(parts)


def output_text(tool_name: str, resp: object, ti: object = None) -> str | None:
    """The raw output worth showing for one call, per tool. Shapes are from Claude Code 2.1.312
    captures and transcripts' toolUseResult; `ti` is the call's tool_input."""
    ti = ti if isinstance(ti, dict) else {}
    r = resp if isinstance(resp, dict) else {}
    if tool_name == "Read":  # the path is in tool.started; the file's contents are never sent
        f = r.get("file") if isinstance(r.get("file"), dict) else None
        if f is None:
            return f"read ({r.get('type')})" if _str(r.get("type")) else "read"
        n, total = f.get("numLines"), f.get("totalLines")
        return f"{n} lines" + (f" of {total}" if isinstance(total, int) and total != n else "") if isinstance(n, int) else "read"
    if tool_name == "Write":
        content = r.get("content") if isinstance(r.get("content"), str) else ti.get("content")
        content = content if isinstance(content, str) else ""
        what = r.get("type") if r.get("type") in ("create", "update") else "wrote"
        path = r.get("filePath") or ti.get("file_path") or ""
        size = len(content.encode("utf-8", "replace"))
        return f"{what} {path} ({_lines(content)} lines, {size:,} bytes)\n" + content
    if tool_name == "Edit":
        return _diff(r.get("filePath") or ti.get("file_path"),
                     [(r.get("oldString", ti.get("old_string")), r.get("newString", ti.get("new_string")))])
    if tool_name == "MultiEdit":
        edits = ti.get("edits") if isinstance(ti.get("edits"), list) else []
        return _diff(r.get("filePath") or ti.get("file_path"),
                     [(e.get("old_string"), e.get("new_string")) for e in edits if isinstance(e, dict)])
    if tool_name == "NotebookEdit":
        return f"{ti.get('notebook_path', '')} ({ti.get('edit_mode') or 'replace'})\n" + (_str(ti.get("new_source")) or "")
    if tool_name in AGENT_TOOLS:  # the subagent's report
        if not r:
            return _str(resp)
        texts = [b.get("text") for b in r.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
        status = _str(r.get("status")) or ""
        body = "\n\n".join(t for t in texts if isinstance(t, str) and t.strip())
        return (f"[{status}] " if status else "") + body or None
    if tool_name == "AskUserQuestion":  # the questions, never the user's answers
        qs = [q.get("question") for q in r.get("questions") or [] if isinstance(q, dict) and _str(q.get("question"))]
        return "answered: " + " / ".join(qs) if qs else "answered"
    if isinstance(resp, str):
        return resp or None
    if "stdout" in r or "stderr" in r:
        out = r.get("stdout") if isinstance(r.get("stdout"), str) else ""
        err = r.get("stderr") if isinstance(r.get("stderr"), str) else ""
        return (out + (("\n[stderr]\n" if out else "[stderr]\n") + err if err else "")) or None
    if tool_name == "Glob" and isinstance(r.get("filenames"), list):
        return "\n".join(x for x in r["filenames"] if isinstance(x, str)) or "no files"
    if tool_name == "Grep":
        if _str(r.get("content")):
            return r["content"]
        if isinstance(r.get("filenames"), list):
            return "\n".join(x for x in r["filenames"] if isinstance(x, str)) or "no matches"
    if tool_name == "WebFetch" and _str(r.get("result")):
        return r["result"]
    if resp is None:
        return None
    return _dump(resp, _NO_DROP)


def tool_started(inp: dict) -> dict | None:
    tool_use_id = c.short(inp.get("tool_use_id"), 128)
    name = c.short(inp.get("tool_name"), 64)
    if not tool_use_id or not name:
        return None
    text, cut = preview(input_text(name, inp.get("tool_input")), INPUT_MAX)
    return {"tool_use_id": tool_use_id, "tool_name": name, "input_preview": text, "input_truncated": cut or None}


def tool_finished(inp: dict, ok: bool) -> dict | None:
    tool_use_id = c.short(inp.get("tool_use_id"), 128)
    name = c.short(inp.get("tool_name"), 64)
    if not tool_use_id or not name:
        return None
    d = inp.get("duration_ms")
    payload = {"tool_use_id": tool_use_id, "tool_name": name, "ok": ok,
               "duration_ms": d if isinstance(d, int) and not isinstance(d, bool) and d >= 0 else None}
    if ok:
        text, cut = preview(output_text(name, inp.get("tool_response"), inp.get("tool_input")), OUTPUT_MAX)
        payload.update(output_preview=text, output_truncated=cut or None)
    else:
        text, _ = preview(inp.get("error") if isinstance(inp.get("error"), str) else None, OUTPUT_MAX)
        payload["error"] = text
    return payload


# --- subagent pairing ------------------------------------------------------------------
# SubagentStart doesn't name the Agent call that launched it (verified on 2.1.312, #31), so
# the reporter remembers each Agent call's id, type and description at PreToolUse and pairs
# the next SubagentStart of that type with the oldest unpaired call. A call that never starts
# a subagent (denied, failed, blocked by a hook) leaves the queue when its PostToolUse(Failure)
# arrives, or after PAIR_TTL_S, so it can't shift every later pairing by one.

def _locked_json(session: str, what: str):
    path = c._state_file(session, what)
    if not c._ensure_state_dir():
        return None, None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None, None
    if not c._lock(fd, 2.0):
        os.close(fd)
        return None, None
    raw = b""
    while chunk := os.read(fd, 65536):
        raw += chunk
    try:  # raw_decode: a write whose truncate never ran leaves old bytes after the new JSON
        data = json.JSONDecoder().raw_decode(raw.decode("utf-8"))[0] if raw.strip() else None
    except (ValueError, UnicodeDecodeError):
        data = None
    return fd, data


def _write_locked(fd: int, data: object) -> None:
    """Write, then cut to length. A process killed in between leaves the new JSON followed by
    old bytes, which _locked_json reads as the new state; never an empty file."""
    raw = json.dumps(data).encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, raw)
    os.ftruncate(fd, len(raw))


def remember_agent_call(session: str, inp: dict) -> None:
    ti = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
    call = {"id": c.short(inp.get("tool_use_id"), 128),
            "type": c.short(ti.get("subagent_type"), 64) or DEFAULT_AGENT_TYPE,
            "description": _str(ti.get("description")), "at": time.time()}
    if not call["id"]:
        return
    fd, calls = _locked_json(session, "agentcalls")
    if fd is None:
        return
    try:
        calls = _live(calls)[-50:]
        calls.append(call)
        _write_locked(fd, calls)
    finally:
        os.close(fd)


def pair_subagent(session: str, agent_type: str | None) -> tuple[str | None, str | None]:
    """(parent_tool_use_id, raw description) for a starting subagent, or (None, None)."""
    fd, calls = _locked_json(session, "agentcalls")
    if fd is None:
        return None, None
    try:
        calls = _live(calls)
        found = (None, None)
        for i, call in enumerate(calls):
            if call.get("type") == (agent_type or DEFAULT_AGENT_TYPE):
                del calls[i]
                found = (call.get("id"), call.get("description"))
                break
        _write_locked(fd, calls)
        return found
    finally:
        os.close(fd)


def _live(calls: object) -> list[dict]:
    now = time.time()
    return [x for x in (calls if isinstance(calls, list) else [])
            if isinstance(x, dict) and isinstance(x.get("at"), (int, float)) and now - x["at"] < PAIR_TTL_S]


def forget_agent_call(session: str, tool_use_id: object) -> None:
    """The Agent call finished or failed: it can no longer start a subagent."""
    fd, calls = _locked_json(session, "agentcalls")
    if fd is None:
        return
    try:
        _write_locked(fd, [x for x in _live(calls) if x.get("id") != tool_use_id])
    finally:
        os.close(fd)


# --- the agent's replies, tailed from the transcript ----------------------------------------

def subagent_transcript(transcript_path: object, session: str, agent_id: str) -> Path | None:
    """Claude Code keeps a subagent's transcript at <session dir>/<session>/subagents/agent-<id>.jsonl."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    safe = c.safe_session(agent_id)
    return Path(transcript_path).parent / c.safe_session(session) / "subagents" / f"agent-{safe}.jsonl"


def start_offset(session: str, path: Path) -> None:
    """At SessionStart, pin the main transcript's read offset at its current end, so the first
    tail sends this session's new replies and none of the history (a resume, a compaction)."""
    fd, state = _locked_json(session, "offset.main")
    if fd is None:
        return
    try:
        if isinstance(state, dict) and state.get("path") == str(path):
            return  # already tracking this file: keep its offset
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        _write_locked(fd, {"path": str(path), "offset": size})
    finally:
        os.close(fd)


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()[:16]


def tail_messages(session: str, path: Path, agent_id: str | None, start_at_end: bool,
                  last_message: object = None) -> list[dict]:
    """New assistant replies in `path` since the last call, as message payloads, each with the
    `ts` of its transcript line (so a reply shows where it was said, not when it was read).

    The read offset is kept per file under an exclusive lock, so two tails never send the same
    line. A transcript seen for the first time with `start_at_end` starts at its current end
    (no history is sent); a subagent's transcript is read from its start. Only complete lines
    are consumed. Each assistant `text` block is one message; nothing else is read. A reply
    repeated in a later turn ("Done.") is a new line, and is sent again.

    `last_message` is Stop / SubagentStop's `last_assistant_message`: the final reply can reach
    the transcript only after the hook fires (seen on 2.1.312 for a subagent). It is sent from
    the hook input unless it is the last text the tail has seen; then it is `owed`, and the
    transcript line carrying it later is skipped once."""
    key = "offset." + c.safe_session(agent_id or "main")
    fd, state = _locked_json(session, key)
    if fd is None:
        return []
    out: list[dict] = []
    try:
        state = state if isinstance(state, dict) and state.get("path") == str(path) else {}
        last_text = state.get("last_text") if isinstance(state.get("last_text"), str) else None
        owed = state.get("owed") if isinstance(state.get("owed"), str) else None
        offset = state.get("offset") if isinstance(state.get("offset"), int) else None
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        new_offset = offset if offset is not None else 0

        def add(raw: str, mid: str | None, ts: str | None) -> None:
            text, cut = preview(raw, MESSAGE_MAX)
            if text is not None:
                out.append({"message_id": mid, "text": text, "truncated": cut or None, "ts": ts})

        if size is not None:
            if offset is None or offset > size:
                offset = size if start_at_end else 0
            offset = max(offset, size - TAIL_MAX_BYTES)
            try:
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
            except OSError:
                chunk = b""
            end = chunk.rfind(b"\n")
            new_offset = offset + end + 1 if end >= 0 else offset
            for line in (chunk[: end + 1].splitlines() if end >= 0 else []):
                if b'"type":"assistant"' not in line and b'"type": "assistant"' not in line:
                    continue  # cheap skip: request blobs and prompts are never parsed
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
                texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                mid = c.short(d.get("uuid"), 120)
                ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) and _TS.fullmatch(d["timestamp"]) else None
                for i, t in enumerate(x for x in texts if isinstance(x, str) and x.strip()):
                    dg = _digest(t)
                    last_text = dg
                    if dg == owed:
                        owed = None  # already sent from the hook input
                        continue
                    add(t, f"{mid}.{i}" if mid else None, ts)
        if isinstance(last_message, str) and last_message.strip():
            dg = _digest(last_message)
            if dg != last_text:
                add(last_message, f"last.{dg}.{new_offset}", None)
                owed = dg
                last_text = dg
        _write_locked(fd, {"path": str(path), "offset": new_offset, "last_text": last_text, "owed": owed})
    finally:
        os.close(fd)
    return out
