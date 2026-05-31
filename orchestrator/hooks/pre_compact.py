#!/usr/bin/env python3
"""PreCompact hook for the orchestrator.

Fires just before Claude Code compacts the conversation. As of 2026-05-31 the
orchestrator runs an AUTOMATIC compaction-handoff at ~90% context, driven by
`user_prompt_submit.py` (Clint's 90% rule): /checkpoint -> write `.handoff.md`
-> /compact. By the time THIS hook fires, the handoff file should already be
fresh, and `session_start.py` (source=="compact") reads it back into the new
window. So this hook is now a SAFETY CHECK, not the trigger:
  - handoff fresh  -> confirm the continuity bridge is active.
  - handoff stale/missing (e.g. a manual or unexpected compaction that didn't go
    through the auto-sequence) -> warn that continuity will fall back to Claude
    Code's lossy summary, and nudge to write the handoff if still possible.

Output uses top-level `systemMessage` (event-agnostic). We deliberately do NOT
use `hookSpecificOutput.additionalContext` — PreCompact's schema rejects it as a
discriminated-union mismatch, which silently swallowed this hook's output before
the fix on 2026-05-07.
"""

import json
import sys
import time
from pathlib import Path

HANDOFF_PATH = Path(__file__).resolve().parent.parent / "transcripts" / ".handoff.md"
FRESH_SECS = 1800  # treat a handoff written within 30 min as "this compaction's"

def main():
    fresh = False
    try:
        if HANDOFF_PATH.exists():
            age = time.time() - HANDOFF_PATH.stat().st_mtime
            fresh = age <= FRESH_SECS
    except Exception:
        fresh = False

    if fresh:
        msg = (
            "<precompact-note>\n"
            "Compaction starting. A fresh handoff is in place "
            f"(`{HANDOFF_PATH}`) — SessionStart will read it back post-compaction. "
            "Continuity bridge active; nothing to do.\n"
            "</precompact-note>"
        )
    else:
        msg = (
            "<precompact-reminder>\n"
            "# Compaction starting WITHOUT a fresh handoff\n\n"
            "No recent `.handoff.md` was found. If this compaction was NOT preceded by the "
            "auto-handoff sequence (`/checkpoint` -> write handoff -> `/compact`), then "
            "post-compaction continuity will fall back to Claude Code's lossy summary, and "
            "experiential weighting / memory accretion won't have been captured.\n\n"
            "If you can still act before compaction: run `/checkpoint` and write your handoff "
            f"to `{HANDOFF_PATH}`. Otherwise, post-compaction you should reconstruct from "
            "`LOG.md` + recent memory and proceed.\n"
            "</precompact-reminder>"
        )

    print(json.dumps({"systemMessage": msg}))

if __name__ == "__main__":
    main()
