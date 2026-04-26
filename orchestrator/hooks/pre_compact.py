#!/usr/bin/env python3
"""PreCompact hook for the active TestFlight orchestrator.

Fires just before Claude Code compacts the conversation. Emits a reminder
to the orchestrator to run `/checkpoint` before the session context gets
summarized away. This is the closest we can get to MindStone's verified
pre-compaction dream cycle without substrate control — advisory only, but
at the exact moment it matters.

Output is JSON with `hookSpecificOutput.additionalContext` containing the
reminder text. Claude Code injects it as additional system context before
compaction runs.
"""

import json
import sys

REMINDER = """<precompact-reminder>
# Compaction is about to occur

Before the session context gets summarized, capture what this session
learned so it persists beyond the summary:

**Invoke `/checkpoint` now** to:
1. Append a session summary to `LOG.md`
2. Increment `hits` on memories cited this session
3. Ask Clint which memories prevented a mistake (Option D)
4. Propose new memories for anything worth persisting
5. Flag any drift (role-shaped work without `/act-as`, uncited canonicals)

The summary Claude Code will produce is adequate for conversational continuity
but not for experiential weighting or memory accretion. `/checkpoint` is the
mechanism that makes the session a *stone added to the cairn*, not just a
summarized transcript.

If the session was purely transactional (trivial Q&A, no decisions), skip.
</precompact-reminder>"""

def main():
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": REMINDER,
        }
    }
    print(json.dumps(output))

if __name__ == "__main__":
    main()
