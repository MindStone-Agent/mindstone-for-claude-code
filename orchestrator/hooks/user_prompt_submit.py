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

def main():
    hook_input = read_hook_input()
    prompt = extract_prompt(hook_input)

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
