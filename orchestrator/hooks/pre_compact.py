#!/usr/bin/env python3
"""PreCompact hook — the compaction-handoff LINCHPIN.

Fires immediately before Claude Code compacts (manual OR auto). This is the ONE
hook guaranteed to run right before any compaction, so it's where we make the
handoff bullet-proof regardless of WHO triggered the compaction or WHEN:

  1. Archive the live transcript JSONL into transcripts/ (so the freshest
     pre-compaction texture is on disk before compaction rewrites the live file;
     the post-compaction deferred embed reads this archive).
  2. Refresh a mechanical `## RECENT TAIL (since rich handoff)` section in
     `.handoff.md` from the JSONL tail — capturing any work done AFTER the rich,
     model-authored handoff was written (the 85%->compaction gap Clint flagged).
     No model call, no embed; cheap and always-fresh at the cliff edge.
  3. Emit a systemMessage noting handoff state.

Why this design: the rich handoff is written by the model at the 85% danger
threshold (user_prompt_submit.py). The harness may auto-compact later (~92%), so
the rich handoff can be stale by then — up to ~70K tokens of work it never saw.
PreCompact tops it up with the raw recent tail at the moment of compaction, so
post-compaction continuity loses nothing. It is also the SAFETY FLOOR: because
PreCompact fires before *any* compaction, continuity holds even if the
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE threshold knob is a no-op on this version and the
harness fires at its own default. The expensive embed is deferred to
post-compaction (session_start.py, source=="compact").

Output uses top-level `systemMessage` — PreCompact's schema rejects
hookSpecificOutput.additionalContext (that silently swallowed this hook's output
before the 2026-05-07 fix). See `feedback_hook_schema_per_event.md`.
"""

import json
import os
import sys
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

HOOK_FILE = Path(__file__).resolve()
ORCHESTRATOR_DIR = HOOK_FILE.parent.parent
TRANSCRIPTS_DIR = ORCHESTRATOR_DIR / "transcripts"
HANDOFF_PATH = TRANSCRIPTS_DIR / ".handoff.md"

FRESH_SECS = 1800          # a rich handoff written within 30 min is "this compaction's"
TAIL_BYTES = 1_500_000     # read the last ~1.5MB of JSONL for the mechanical tail
TAIL_MESSAGES = 16         # how many recent text messages to capture
TAIL_CHARS = 400           # per-message char cap in the mechanical tail
RECENT_TAIL_MARKER = "## RECENT TAIL (since rich handoff)"

# Pure-injection turns to skip so the mechanical tail stays signal, not hook noise.
NOISE_PREFIXES = (
    "<synapse-digest", "<semantic-recall", "<system-reminder",
    "<post-compaction-handoff", "<context-capacity-handoff", "<precompact",
    "<local-command", "<command-", "<persisted-output",
)


def read_hook_input() -> dict:
    try:
        if sys.stdin.isatty():
            return {}
        data = sys.stdin.read().strip()
        return json.loads(data) if data else {}
    except Exception:
        return {}


def _resolve_transcript_path(hook_input: dict) -> Path | None:
    tp = hook_input.get("transcript_path") or hook_input.get("transcriptPath")
    if tp and Path(tp).exists():
        return Path(tp)
    cwd = hook_input.get("cwd") or os.getcwd()
    escaped = "-" + str(cwd).strip("/").replace("/", "-")
    projdir = Path.home() / ".claude" / "projects" / escaped
    if projdir.is_dir():
        jsonls = sorted(projdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if jsonls:
            return jsonls[0]
    return None


def _archive_transcript(tp: Path) -> Path | None:
    """Copy the live JSONL into transcripts/ as YYYY-MM-DD__<uuid>.jsonl.
    Mirrors session_end.py._archive_transcript. Idempotent on mtime."""
    try:
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dest = TRANSCRIPTS_DIR / f"{stamp}__{tp.stem}.jsonl"
        if dest.exists() and dest.stat().st_mtime >= tp.stat().st_mtime:
            return dest
        shutil.copy2(tp, dest)
        return dest
    except Exception as e:
        print(f"[pre_compact] archive failed ({e})", file=sys.stderr)
        return None


def _extract_text(content) -> str:
    """Pull readable text out of a message content field (str or block list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text"):
                out.append(b["text"].strip())
            elif t == "tool_use":
                out.append(f"[tool: {b.get('name', '?')}]")
            elif t == "tool_result":
                out.append("[tool-result]")
        return "\n".join(x for x in out if x).strip()
    return ""


def _recent_tail(tp: Path) -> str:
    """Mechanical digest of the last TAIL_MESSAGES user/assistant text messages
    from the JSONL tail. Cheap, no model, no embed."""
    try:
        size = tp.stat().st_size
        with tp.open("rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # discard partial line at the seek boundary
            raw_lines = f.read().splitlines()
    except Exception as e:
        print(f"[pre_compact] tail read failed ({e})", file=sys.stderr)
        return ""

    msgs = []
    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        typ = o.get("type")
        if typ not in ("user", "assistant"):
            continue
        msg = o.get("message") or {}
        role = msg.get("role") or typ
        text = _extract_text(msg.get("content"))
        if not text:
            continue
        if any(text.startswith(p) for p in NOISE_PREFIXES):
            continue
        msgs.append((role, text))

    if not msgs:
        return ""
    tail = msgs[-TAIL_MESSAGES:]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        RECENT_TAIL_MARKER,
        f"_Mechanical capture by PreCompact at {stamp} — the raw recent exchange "
        "AFTER the rich handoff above. Authoritative for anything the rich handoff predates._",
        "",
    ]
    for role, text in tail:
        snippet = text if len(text) <= TAIL_CHARS else text[:TAIL_CHARS] + "…"
        snippet = " ".join(snippet.split())  # collapse whitespace/newlines
        lines.append(f"- **{role}:** {snippet}")
    return "\n".join(lines)


def _refresh_handoff_tail(tp: Path) -> bool:
    """Append/replace the RECENT TAIL section in .handoff.md without clobbering
    the rich, model-authored body. If no handoff exists, write a minimal one."""
    tail = _recent_tail(tp)
    if not tail:
        return False
    try:
        existing = HANDOFF_PATH.read_text() if HANDOFF_PATH.exists() else ""
    except Exception:
        existing = ""
    # strip any prior RECENT TAIL section so it doesn't accumulate across compactions
    if RECENT_TAIL_MARKER in existing:
        existing = existing.split(RECENT_TAIL_MARKER, 1)[0].rstrip()
    if not existing.strip():
        existing = (
            "# HANDOFF (mechanical — no rich handoff was written this session)\n\n"
            "PreCompact captured the recent tail below as a fallback continuity bridge.\n"
        )
    new = existing.rstrip() + "\n\n" + tail + "\n"
    try:
        HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
        HANDOFF_PATH.write_text(new)
        return True
    except Exception as e:
        print(f"[pre_compact] handoff tail write failed ({e})", file=sys.stderr)
        return False


def main():
    hook_input = read_hook_input()
    tp = _resolve_transcript_path(hook_input)

    archived = None
    tail_written = False
    if tp:
        archived = _archive_transcript(tp)
        tail_written = _refresh_handoff_tail(tp)

    bits = []
    if tail_written:
        bits.append("recent-tail refreshed")
    if archived:
        bits.append("transcript archived")
    status = "; ".join(bits) if bits else "no transcript resolved — continuity falls back to existing handoff/LOG"

    msg = (
        "<precompact-note>\n"
        f"Compaction starting. PreCompact: {status}. `.handoff.md` is in place — SessionStart "
        "will replay it post-compaction and the transcript embed runs then (deferred, off the "
        "critical path). Continuity bridge active; nothing to do.\n"
        "</precompact-note>"
    )
    print(json.dumps({"systemMessage": msg}))


if __name__ == "__main__":
    main()
