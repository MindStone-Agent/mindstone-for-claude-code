#!/usr/bin/env python3
"""PreCompact hook for the orchestrator.

Fires just before Claude Code compacts the conversation. Emits a reminder
to the orchestrator to run `/checkpoint` before the session context gets
summarized away. This is the closest we can get to MindStone's verified
pre-compaction dream cycle without substrate control — advisory only, but
at the exact moment it matters.

Output is JSON with a top-level `systemMessage` field containing the
reminder text. Claude Code injects it as a system-visible note. Note: we
deliberately use `systemMessage` (event-agnostic) rather than
`hookSpecificOutput.additionalContext` — the latter is only valid for
UserPromptSubmit / PostToolUse / PostToolBatch events. PreCompact's
schema rejects additionalContext as a discriminated-union mismatch,
which silently swallowed this hook's output before the fix on 2026-05-07.
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
3. Ask the user which memories prevented a mistake (Option D)
4. Propose new memories for anything worth persisting
5. Flag any drift (role-shaped work without `/act-as`, uncited canonicals)

The summary Claude Code will produce is adequate for conversational continuity
but not for experiential weighting or memory accretion. `/checkpoint` is the
mechanism that makes the session a *stone added to the cairn*, not just a
summarized transcript.

If the session was purely transactional (trivial Q&A, no decisions), skip.
</precompact-reminder>"""

def main():
    # Top-level `systemMessage` is event-agnostic (valid for any hook) and
    # surfaces to the model as a system-visible note. The earlier shape
    # using `hookSpecificOutput.additionalContext` failed PreCompact's
    # schema validation silently — see the module docstring.
    output = {"systemMessage": REMINDER}
    print(json.dumps(output))

if __name__ == "__main__":
    main()
