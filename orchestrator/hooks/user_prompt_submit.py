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


def main():
    hook_input = read_hook_input()
    prompt = extract_prompt(hook_input)

    # Watchdog: every N turns, fork session_end.py in the background to keep
    # the archive + vector index fresh in case this session ends via /exit
    # or an error before the next Stop hook fires.
    maybe_run_watchdog(hook_input)

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
