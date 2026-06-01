#!/usr/bin/env python3
"""UserPromptSubmit hook — semantic recall based on what the user actually asked.

Fires before each user turn. Gets the user's prompt, queries the vector
store for the most relevant memory chunks and past transcript snippets,
and injects them into context.

This is where semantic recall earns its keep. SessionStart loads the
always-needed identity/memory baseline; UserPromptSubmit surfaces the
"given what you just asked, here's what I probably need to remember."

Output format: JSON with `hookSpecificOutput.additionalContext`.
Falls back to empty context gracefully if anything's unavailable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_FILE = Path(__file__).resolve()
ORCHESTRATOR_DIR = HOOK_FILE.parent.parent
DB_PATH = ORCHESTRATOR_DIR / "vectors.db"

# How many total chunks to surface and the per-source-type split.
TOP_K_TOTAL = 6
TOP_K_MEMORY = 4     # weight toward memory/feedback/project files
TOP_K_TRANSCRIPT = 2 # plus a couple from past transcripts
MMR_LAMBDA = 0.65    # favor relevance but allow some diversity

# Minimum similarity threshold — below this, don't bother injecting.
MIN_SIMILARITY = 0.30

# Per-chunk display budget (chars) — keep context lean
MAX_CHARS_PER_CHUNK = 800

# ---------------------------------------------------------------------------
# Intra-session archive watchdog
# ---------------------------------------------------------------------------
# The Stop hook fires per-turn-completion, so `/exit`, abrupt termination,
# or sessions that die on errors lose post-last-turn texture from auto-archive.
# Mitigation: every N user prompts, fork session_end.py in the background to
# refresh the archive + vector index. Worst-case loss becomes N turns instead
# of "everything since the last successful turn before the error."
#
# Threshold is configurable via env var; state is per-session in a JSON file
# under transcripts/ (already gitignored).
WATCHDOG_TURN_THRESHOLD = int(os.environ.get("CAIRN_WATCHDOG_THRESHOLD", "10"))
WATCHDOG_STATE_FILE = ORCHESTRATOR_DIR / "transcripts" / ".watchdog_state.json"
SESSION_END_HOOK = HOOK_FILE.parent / "session_end.py"

# ---------------------------------------------------------------------------
# Context-capacity auto-handoff trigger (Clint's 90% rule, 2026-05-31)
# ---------------------------------------------------------------------------
# Removes the need for Clint to babysit the token count. Each turn we read the
# latest `usage` record Claude Code writes into the transcript JSONL, compute
# context-window occupancy (input + cache_creation + cache_read), and at
# COMPACT_THRESHOLD inject a CRITICAL directive telling the orchestrator to run
# the compaction-handoff sequence autonomously:
#     /checkpoint  ->  write .handoff.md  ->  /compact
# Post-compaction, session_start.py (source == "compact") points back to the
# handoff file. Hysteresis: fires once on crossing up through COMPACT_THRESHOLD;
# re-arms only after occupancy falls back below REARM_RATIO (which it does after
# a compaction). CONTEXT_WINDOW defaults to the 1M-context model; tune via env if
# the figure here diverges from the TUI %.
CONTEXT_WINDOW = int(os.environ.get("CAIRN_CONTEXT_WINDOW", "1000000"))
# Danger-zone threshold. Fire BELOW the harness auto-compact threshold (set via
# CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, target ~92%) so the rich, model-authored
# handoff is written before the harness compacts. If a real fire is observed to
# compact BELOW this (override ineffective on this CC version), lower this env.
# PreCompact is the safety floor either way — see pre_compact.py / scri-38-cc.
COMPACT_THRESHOLD = float(os.environ.get("CAIRN_COMPACT_THRESHOLD", "0.85"))
REARM_RATIO = float(os.environ.get("CAIRN_COMPACT_REARM", "0.5"))
HANDOFF_PATH = ORCHESTRATOR_DIR / "transcripts" / ".handoff.md"
HANDOFF_STATE_FILE = ORCHESTRATOR_DIR / "transcripts" / ".handoff_state.json"

def read_hook_input() -> dict:
    try:
        if sys.stdin.isatty():
            return {}
        data = sys.stdin.read().strip()
        if not data:
            return {}
        return json.loads(data)
    except Exception:
        return {}

def extract_prompt(hook_input: dict) -> str:
    """Pull the user's prompt text out of the hook input JSON."""
    # Try common shapes in order
    p = hook_input.get("prompt") or hook_input.get("user_prompt")
    if not p:
        msg = hook_input.get("message")
        if isinstance(msg, dict):
            p = msg.get("content")
    if isinstance(p, str):
        return p.strip()
    if isinstance(p, list):
        texts = [
            item.get("text", "") for item in p
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(texts).strip()
    return ""

def truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "..."

# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def _resolve_session_id(hook_input: dict) -> str | None:
    sid = hook_input.get("session_id") or hook_input.get("sessionId")
    if sid:
        return sid
    sess = hook_input.get("session")
    if isinstance(sess, dict):
        return sess.get("id")
    return None


def _fork_session_end(hook_input: dict) -> None:
    """Spawn session_end.py as a detached background process in watchdog mode.

    Returns immediately; we do not wait for completion. The subprocess inherits
    no controlling terminal (start_new_session) so it survives `/exit`.
    """
    if not SESSION_END_HOOK.exists():
        return

    env = os.environ.copy()
    env["CAIRN_WATCHDOG_MODE"] = "1"

    forwarded_input = json.dumps({
        "session_id": _resolve_session_id(hook_input),
        "cwd": hook_input.get("cwd", os.getcwd()),
    }).encode()

    try:
        proc = subprocess.Popen(
            [sys.executable, str(SESSION_END_HOOK)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except Exception as e:
        print(f"[user_prompt_submit] watchdog fork failed ({e})", file=sys.stderr)
        return

    try:
        if proc.stdin is not None:
            proc.stdin.write(forwarded_input)
            proc.stdin.close()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def maybe_run_watchdog(hook_input: dict) -> None:
    """Increment per-session turn counter; fork session_end.py at threshold.

    State shape: { "<session_id>": <turns_since_last_archive> }
    Resets the counter to 0 on the firing prompt so the next firing is N more
    turns away. All errors are swallowed so the watchdog never blocks recall.
    """
    try:
        session_id = _resolve_session_id(hook_input)
        if not session_id:
            return

        WATCHDOG_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        state: dict = {}
        if WATCHDOG_STATE_FILE.exists():
            try:
                state = json.loads(WATCHDOG_STATE_FILE.read_text())
                if not isinstance(state, dict):
                    state = {}
            except Exception:
                state = {}

        count = int(state.get(session_id, 0)) + 1

        if count >= WATCHDOG_TURN_THRESHOLD:
            _fork_session_end(hook_input)
            state[session_id] = 0
        else:
            state[session_id] = count

        try:
            WATCHDOG_STATE_FILE.write_text(json.dumps(state))
        except Exception as e:
            print(f"[user_prompt_submit] watchdog state write failed ({e})", file=sys.stderr)
    except Exception as e:
        print(f"[user_prompt_submit] watchdog error ({e})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Auto-handoff trigger
# ---------------------------------------------------------------------------

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


_USAGE_TAIL_BYTES = 4_000_000  # last usage record lives at EOF; don't scan the whole growing transcript


def _last_usage_in(lines) -> dict | None:
    last = None
    for raw in lines:
        if b'"usage"' not in raw:
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        u = (o.get("message", {}) or {}).get("usage") or o.get("usage")
        if isinstance(u, dict) and ("input_tokens" in u or "cache_read_input_tokens" in u):
            last = u
    return last


def _context_tokens_from_transcript(tp: Path) -> int | None:
    """Sum the last usage record's prompt tokens = current context occupancy.

    Tail-reads the last few MB (the latest usage record sits at EOF), with a
    full-scan fallback. Per Cairn's #38 review: the transcript is append-only
    and grows unbounded (compaction shrinks context, not the file), so scanning
    the whole thing every turn was O(filesize) — ~0.87s on a 520MB file. The
    tail-read is ~138x faster with an identical result."""
    last = None
    try:
        size = tp.stat().st_size
        with tp.open("rb") as f:
            if size > _USAGE_TAIL_BYTES:
                f.seek(size - _USAGE_TAIL_BYTES)
                f.readline()  # drop the partial boundary line
            last = _last_usage_in(f.read().splitlines())
            if last is None and size > _USAGE_TAIL_BYTES:
                f.seek(0)
                last = _last_usage_in(f)
    except Exception:
        return None
    if not last:
        return None
    return (int(last.get("input_tokens", 0) or 0)
            + int(last.get("cache_creation_input_tokens", 0) or 0)
            + int(last.get("cache_read_input_tokens", 0) or 0))


def _build_handoff_directive(pct: float, ctx: int) -> str:
    return "\n".join([
        '<context-capacity-handoff priority="CRITICAL">',
        f"⚠ CONTEXT AT ~{pct*100:.0f}% ({ctx:,} / {CONTEXT_WINDOW:,} tokens). Danger-zone handoff trigger (2026-05-31 design).",
        "",
        "The harness will auto-compact on its own at its threshold (set above this one). You CANNOT trigger `/compact` yourself on Claude Code — do not try. Your job now is to make the handoff bullet-proof BEFORE the harness compacts. Do this autonomously; do NOT ask Clint to confirm any part of it:",
        "",
        f"1. Write your RICH handoff to `{HANDOFF_PATH}` (overwrite the body; leave any existing `## RECENT TAIL` section — PreCompact manages it). Post-compaction-you reads this FIRST. Capture concisely:",
        "   - What you're mid-task on + the exact next step",
        "   - Open threads / gated work / what you're waiting on and from whom (include any in-flight request from THIS turn so you address it after compaction)",
        "   - Key decisions + constraints established this session",
        "   - Anything post-compaction-you would otherwise lose and regret",
        "2. Run the `/checkpoint` JUDGMENT while you still have full context — synthesize the LOG entry, propose + write new memories, drift check. But do NOT run the embed (step 7 / `CAIRN_CHECKPOINT_MODE`): the post-compaction hook embeds the archived transcript automatically, off the critical path, so the fans stay quiet until after cutover.",
        "3. Do NOT run `/compact` and do NOT embed. Then just keep working normally.",
        "",
        "What happens automatically from here: PreCompact tops up this handoff with the recent tail at the moment of compaction (so work between now and then isn't lost), the harness compacts, and SessionStart replays the handoff + kicks the deferred embed. This replaces Clint watching your token count. Proceed without confirmation.",
        "</context-capacity-handoff>",
    ])


def maybe_handoff_directive(hook_input: dict) -> str | None:
    """Return the auto-handoff directive when context crosses COMPACT_THRESHOLD,
    else None. Hysteresis (per-session state file): fires once per crossing, and
    re-arms only after occupancy drops below REARM_RATIO (i.e. after a compaction).
    All errors swallowed so this never blocks the turn."""
    try:
        tp = _resolve_transcript_path(hook_input)
        if not tp:
            return None
        ctx = _context_tokens_from_transcript(tp)
        if not ctx or CONTEXT_WINDOW <= 0:
            return None
        pct = ctx / CONTEXT_WINDOW
        sid = _resolve_session_id(hook_input) or "default"

        state: dict = {}
        if HANDOFF_STATE_FILE.exists():
            try:
                state = json.loads(HANDOFF_STATE_FILE.read_text())
                if not isinstance(state, dict):
                    state = {}
            except Exception:
                state = {}
        entry = state.get(sid) if isinstance(state.get(sid), dict) else {}
        fired = bool(entry.get("fired"))

        def _save():
            try:
                HANDOFF_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                HANDOFF_STATE_FILE.write_text(json.dumps(state))
            except Exception as e:
                print(f"[user_prompt_submit] handoff state write failed ({e})", file=sys.stderr)

        # Re-arm once occupancy falls back down (e.g. right after a compaction).
        if fired and pct < REARM_RATIO:
            state[sid] = {"fired": False}
            _save()
            return None

        if pct >= COMPACT_THRESHOLD and not fired:
            state[sid] = {"fired": True}
            _save()
            print(f"[user_prompt_submit] auto-handoff fired at {pct*100:.0f}% ({ctx} tok)", file=sys.stderr)
            return _build_handoff_directive(pct, ctx)
        return None
    except Exception as e:
        print(f"[user_prompt_submit] handoff-trigger error ({e})", file=sys.stderr)
        return None


def main():
    hook_input = read_hook_input()
    prompt = extract_prompt(hook_input)

    # Watchdog: every N turns, fork session_end.py in the background to keep
    # the archive + vector index fresh in case this session ends via /exit
    # or an error before the next Stop hook fires.
    maybe_run_watchdog(hook_input)

    # Auto-handoff trigger (Clint's 90% rule): if context has crossed the
    # threshold, inject the CRITICAL handoff directive and skip normal recall
    # (we're about to checkpoint + compact anyway).
    directive = maybe_handoff_directive(hook_input)
    if directive:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": directive,
            }
        }))
        return

    # No prompt to work with, or too short to be worth embedding.
    if not prompt or len(prompt) < 8:
        return  # no output = no injection

    # Vector store must exist
    if not DB_PATH.exists():
        return

    # Lazy-import so the hook is cheap when idle
    try:
        from recall import recall
    except Exception as e:
        print(f"[user_prompt_submit] recall unavailable ({e})", file=sys.stderr)
        return

    try:
        memory_results = recall(
            prompt,
            k=TOP_K_MEMORY,
            source_types=["memory"],
            mmr=True,
            mmr_lambda=MMR_LAMBDA,
            db_path=DB_PATH,
        )
    except Exception as e:
        print(f"[user_prompt_submit] memory recall failed ({e})", file=sys.stderr)
        memory_results = []

    try:
        transcript_results = recall(
            prompt,
            k=TOP_K_TRANSCRIPT,
            source_types=["transcript"],
            mmr=True,
            mmr_lambda=MMR_LAMBDA,
            db_path=DB_PATH,
        )
    except Exception as e:
        print(f"[user_prompt_submit] transcript recall failed ({e})", file=sys.stderr)
        transcript_results = []

    # Filter by similarity threshold
    memory_results = [r for r in memory_results if r["similarity"] >= MIN_SIMILARITY]
    transcript_results = [r for r in transcript_results if r["similarity"] >= MIN_SIMILARITY]

    if not memory_results and not transcript_results:
        return  # Nothing relevant enough to surface

    parts = ["<semantic-recall>",
             "# Semantic recall for this prompt",
             "",
             "The prompt appears to relate to the following stored context. "
             "Use if relevant; ignore if not — recall is probabilistic, not authoritative.",
             ""]

    if memory_results:
        parts.append("## From memory files")
        for r in memory_results:
            path_display = Path(r["source_path"]).name
            snippet = truncate(r["text"].strip(), MAX_CHARS_PER_CHUNK)
            parts.append(f"### {path_display} (sim={r['similarity']:.2f})")
            parts.append(snippet)
            parts.append("")

    if transcript_results:
        parts.append("## From past session transcripts")
        for r in transcript_results:
            path_display = Path(r["source_path"]).name
            snippet = truncate(r["text"].strip(), MAX_CHARS_PER_CHUNK)
            parts.append(f"### {path_display} lines {r['start_line']}-{r['end_line']} (sim={r['similarity']:.2f})")
            parts.append(snippet)
            parts.append("")

    parts.append("</semantic-recall>")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(output))

if __name__ == "__main__":
    main()
