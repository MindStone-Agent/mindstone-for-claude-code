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

Never sent: the user's prompts (they are in the transcript too, in `user` and
`api-request-blob` lines; only `assistant` text blocks are read), the Agent tool's `prompt`,
thinking blocks.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

from fleet import common as c
from fleet.redact import preview

INPUT_MAX = 4096
OUTPUT_MAX = 16384
MESSAGE_MAX = 8192
DESCRIPTION_MAX = 240
TAIL_MAX_BYTES = 8 * 1024 * 1024   # the most new transcript read per tail; older bytes are skipped
AGENT_TOOLS = ("Agent", "Task")


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _lines(s: object) -> int:
    return s.count("\n") + 1 if isinstance(s, str) and s else 0


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
    if tool_name in AGENT_TOOLS:  # never the prompt: it's the parent's instruction text
        return " ".join(p for p in (_str(ti.get("subagent_type")), _str(ti.get("description"))) if p) or None
    try:
        return json.dumps(ti, ensure_ascii=False, separators=(", ", ": "))
    except (TypeError, ValueError):
        return None


def output_text(resp: object) -> str | None:
    if isinstance(resp, str):
        return resp or None
    if isinstance(resp, dict) and ("stdout" in resp or "stderr" in resp):
        out = resp.get("stdout") if isinstance(resp.get("stdout"), str) else ""
        err = resp.get("stderr") if isinstance(resp.get("stderr"), str) else ""
        return (out + (("\n[stderr]\n" if out else "[stderr]\n") + err if err else "")) or None
    if resp is None:
        return None
    try:
        return json.dumps(resp, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


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
        text, cut = preview(output_text(inp.get("tool_response")), OUTPUT_MAX)
        payload.update(output_preview=text, output_truncated=cut or None)
    else:
        text, _ = preview(inp.get("error") if isinstance(inp.get("error"), str) else None, OUTPUT_MAX)
        payload["error"] = text
    return payload


# --- subagent pairing ------------------------------------------------------------------
# SubagentStart doesn't name the Agent call that launched it (verified on 2.1.312, #31), so
# the reporter remembers each Agent call's id, type and description at PreToolUse and pairs
# the next SubagentStart of that type with the oldest unpaired call.

def _locked_json(session: str, what: str):
    path = c._state_file(session, what)
    if not c._ensure_state_dir():
        return None, None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None, None
    fcntl.flock(fd, fcntl.LOCK_EX)
    raw = b""
    while chunk := os.read(fd, 65536):
        raw += chunk
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    return fd, data


def _write_locked(fd: int, data: object) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(data).encode())


def remember_agent_call(session: str, inp: dict) -> None:
    ti = inp.get("tool_input") if isinstance(inp.get("tool_input"), dict) else {}
    call = {"id": c.short(inp.get("tool_use_id"), 128), "type": c.short(ti.get("subagent_type"), 64),
            "description": _str(ti.get("description"))}
    if not call["id"]:
        return
    fd, calls = _locked_json(session, "agentcalls")
    if fd is None:
        return
    try:
        calls = [x for x in (calls or []) if isinstance(x, dict)][-50:]
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
        calls = [x for x in (calls or []) if isinstance(x, dict)]
        for i, call in enumerate(calls):
            if call.get("type") == agent_type or call.get("type") is None:
                del calls[i]
                _write_locked(fd, calls)
                return call.get("id"), call.get("description")
        return None, None
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
    """New assistant replies in `path` since the last call, as message payloads.

    The read offset is kept per file under an exclusive lock, so two tails never send the same
    reply. A transcript seen for the first time with `start_at_end` starts at its current end
    (no history is sent); a subagent's transcript is read from its start. Only complete lines
    are consumed. Each assistant `text` block is one message; nothing else is read.

    `last_message` is Stop / SubagentStop's `last_assistant_message`: the final reply can reach
    the transcript only after the hook fires (seen on 2.1.312 for a subagent), so if the tail
    didn't already carry it, it's sent from the hook input. A digest of every text sent is kept
    with the offset, so the line arriving later in the transcript isn't sent a second time."""
    key = "offset." + c.safe_session(agent_id or "main")
    fd, state = _locked_json(session, key)
    if fd is None:
        return []
    out: list[dict] = []
    try:
        same = isinstance(state, dict) and state.get("path") == str(path)
        sent = [x for x in (state.get("sent") if same and isinstance(state.get("sent"), list) else []) if isinstance(x, str)]
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        offset = state.get("offset") if same else None
        new_offset = offset if isinstance(offset, int) else 0

        def add(raw: str, mid: str | None) -> None:
            dg = _digest(raw)
            if dg in sent:
                return
            text, cut = preview(raw, MESSAGE_MAX)
            if text is None:
                return
            sent.append(dg)
            out.append({"message_id": mid, "text": text, "truncated": cut or None})

        if size is not None:
            if not isinstance(offset, int) or offset > size:
                offset = size if start_at_end else 0
            offset = max(offset, size - TAIL_MAX_BYTES)
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
            end = chunk.rfind(b"\n")
            new_offset = offset + end + 1 if end >= 0 else offset
            for line in (chunk[: end + 1].splitlines() if end >= 0 else []):
                if b'"type":"assistant"' not in line and b'"type": "assistant"' not in line:
                    continue  # cheap skip: request blobs and prompts are never parsed
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
                texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                mid = c.short(d.get("uuid"), 120)
                for i, t in enumerate(x for x in texts if isinstance(x, str) and x.strip()):
                    add(t, f"{mid}.{i}" if mid else None)
        if isinstance(last_message, str) and last_message.strip():
            add(last_message, f"last.{_digest(last_message)}")
        _write_locked(fd, {"path": str(path), "offset": new_offset, "sent": sent[-40:]})
    finally:
        os.close(fd)
    return out
