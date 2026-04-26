#!/usr/bin/env python3
"""SessionStart hook for the active TestFlight orchestrator.

Runs at every Claude Code session start. Injects:
  1. IDENTITY.md and USER.md (always — identity-level files)
  2. Memory files flagged `critical: true` or `evergreen: true`
  3. Top-N weighted project memories routed by the current working directory
  4. A recent LOG.md tail for session-to-session continuity

If IDENTITY.md doesn't exist, treats it as first-run: emits an onboarding
invitation pointing the new orchestrator at testflight/onboarding/.

Output is a single JSON object on stdout with `hookSpecificOutput.additionalContext`
containing the assembled system-reminder block. Stderr is used for debug logs.

Registered in ~/.claude/settings.json. Path is resolved from this script's
own location (testflight/orchestrator/hooks/session_start.py), so it keeps
working whether invoked from any CWD.
"""

import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Token budget for the entire injected block. Rough estimate — 1 token ≈ 4 chars.
TOKEN_BUDGET_CHARS = 50000  # ~12500 tokens — roomy so identity + user + critical land intact

# Top-N non-critical project memories to consider ranking.
TOP_N_PROJECT_MEMORIES = 10

# How many tail lines from LOG.md to include for continuity.
LOG_TAIL_LINES = 40

# CWD hints → project tags. Used to boost project-matched memories.
PROJECT_HINTS = {
    "testflight": "testflight",
    "autotabletop": "att",
    "AutoTableTop": "att",
    "att-unity": "att-unity",
    "aegis": "aegis-dashboard",
    "scryforge": "scryforge",
    "ozh": "operation-zero-hour",
    "operation_zero_hour": "operation-zero-hour",
    "fcm": "fcm",
    "tprm": "tprm",
    "mindstone": "mindstone",
    "MindStone": "mindstone",
}

PROJECT_MATCH_BOOST = 5.0  # Multiplier when project tag matches CWD hint.

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HOOK_FILE = Path(__file__).resolve()
ORCHESTRATOR_DIR = HOOK_FILE.parent.parent  # testflight/orchestrator/
TESTFLIGHT_DIR = ORCHESTRATOR_DIR.parent    # testflight/
ONBOARDING_DIR = TESTFLIGHT_DIR / "onboarding"
MEMORY_DIR = ORCHESTRATOR_DIR / "memory"

IDENTITY_FILE = ORCHESTRATOR_DIR / "IDENTITY.md"
USER_FILE = ORCHESTRATOR_DIR / "USER.md"
LOG_FILE = ORCHESTRATOR_DIR / "LOG.md"

# ---------------------------------------------------------------------------
# Frontmatter parsing (kept minimal — mirrors migrate_frontmatter.py)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    fm = {}
    for line in block.split("\n"):
        if not line.strip():
            continue
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not km:
            continue
        key, val = km.group(1), km.group(2).strip()
        if val == "":
            fm[key] = None
        elif val.lower() == "true":
            fm[key] = True
        elif val.lower() == "false":
            fm[key] = False
        elif val.lower() in ("null", "~"):
            fm[key] = None
        elif re.match(r"^-?\d+$", val):
            fm[key] = int(val)
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            fm[key] = [s.strip().strip('"').strip("'") for s in inner.split(",")] if inner else []
        elif val.startswith('"') and val.endswith('"'):
            fm[key] = val[1:-1]
        else:
            fm[key] = val
    return fm, body

# ---------------------------------------------------------------------------
# Weighting
# ---------------------------------------------------------------------------

def weight(fm: dict, now: datetime, active_projects: set) -> float:
    """Compute injection-ranking weight for a memory.

    Infinity for critical/evergreen (always inject up to budget).
    Otherwise: (hits + 3*prevented + 1) * exp(-age/half_life), boosted if
    the memory's `projects` field matches the CWD-inferred project.
    """
    if fm.get("critical") or fm.get("evergreen"):
        return float("inf")

    hits = fm.get("hits", 0) or 0
    prevented = fm.get("prevented", 0) or 0
    base = hits + 3 * prevented + 1

    last_applied = fm.get("last_applied")
    created = fm.get("created")
    anchor = last_applied or created
    age_days = 0.0
    if anchor:
        try:
            anchor_dt = datetime.fromisoformat(str(anchor))
            if anchor_dt.tzinfo is None:
                anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - anchor_dt).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0

    half_life = fm.get("half_life_days", 30) or 30
    decay = math.exp(-age_days / half_life)

    w = base * decay

    projects = fm.get("projects") or []
    if active_projects and any(p in active_projects for p in projects):
        w *= PROJECT_MATCH_BOOST

    return w

# ---------------------------------------------------------------------------
# CWD → project hints
# ---------------------------------------------------------------------------

def infer_active_projects(cwd: str) -> set:
    cwd_lower = cwd.lower()
    matches = set()
    for hint, project in PROJECT_HINTS.items():
        if hint.lower() in cwd_lower:
            matches.add(project)
    return matches

# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def load_memory_files():
    """Return list of (path, frontmatter, body) tuples."""
    results = []
    if not MEMORY_DIR.exists():
        return results
    for path in sorted(MEMORY_DIR.glob("*.md")):
        try:
            text = path.read_text()
            fm, body = parse_frontmatter(text)
            results.append((path, fm, body))
        except Exception as e:
            print(f"[cairn/session_start] skip {path.name}: {e}", file=sys.stderr)
    return results

def assemble_context(active_projects: set) -> str:
    """Build the <orchestrator-context> block."""
    now = datetime.now(tz=timezone.utc)
    parts = []

    # --- Identity & user (always) ---
    if IDENTITY_FILE.exists():
        parts.append("## IDENTITY (who I am)\n" + IDENTITY_FILE.read_text())
    if USER_FILE.exists():
        parts.append("## USER (who I'm working with)\n" + USER_FILE.read_text())

    # --- Critical memories: FULL CONTENT injection ---
    # --- Evergreen (non-critical) memories: POINTER injection (filename + description) ---
    critical_full = []
    evergreen_pointers = []
    rankable = []
    memories = load_memory_files()
    for path, fm, body in memories:
        # Skip index/log/roadmap — handled separately below.
        if fm.get("type") in ("index", "log", "roadmap"):
            continue
        if fm.get("critical"):
            critical_full.append((path, fm, body))
        elif fm.get("evergreen"):
            evergreen_pointers.append((path, fm))
        else:
            rankable.append((path, fm))

    if critical_full:
        parts.append("## CRITICAL RULES (always applied — read in full)")
        for path, fm, body in critical_full:
            desc = fm.get("description", path.name)
            parts.append(f"### `{path.name}` — {desc}\n{body.strip()}")

    if evergreen_pointers:
        lines = ["## EVERGREEN REFERENCES (available — consult on demand)"]
        for path, fm in evergreen_pointers:
            desc = fm.get("description", path.name)
            lines.append(f"- `{path.name}` — {desc}")
        parts.append("\n".join(lines))

    # --- Weighted project memories (top-N) ---
    ranked = [
        (path, fm, weight(fm, now, active_projects))
        for path, fm in rankable
    ]
    ranked.sort(key=lambda t: t[2], reverse=True)
    selected = ranked[:TOP_N_PROJECT_MEMORIES]

    if selected:
        lines = ["## CONTEXT MEMORIES (weighted for current CWD)"]
        if active_projects:
            lines.append(f"_Active project hints from CWD: {', '.join(sorted(active_projects))}_\n")
        for path, fm, w in selected:
            desc = fm.get("description", path.name)
            projects = fm.get("projects") or []
            proj_str = f" [{', '.join(projects)}]" if projects else ""
            lines.append(f"- `{path.name}`{proj_str} — {desc}")
        parts.append("\n".join(lines))

    # --- Memory index (MEMORY.md is small; always useful) ---
    memory_index = MEMORY_DIR / "MEMORY.md"
    if memory_index.exists():
        parts.append("## MEMORY INDEX (all available memories)\n" + memory_index.read_text())

    # --- Recent LOG tail for continuity ---
    if LOG_FILE.exists():
        log_text = LOG_FILE.read_text()
        lines = log_text.split("\n")
        tail = "\n".join(lines[-LOG_TAIL_LINES:])
        parts.append("## RECENT LOG (tail for continuity)\n" + tail)

    # Assemble and budget-clip.
    joined = "\n\n---\n\n".join(parts)
    if len(joined) > TOKEN_BUDGET_CHARS:
        # Truncation strategy: keep identity + user + critical intact; trim log/weighted memories.
        # Simple approach: truncate the tail.
        joined = joined[:TOKEN_BUDGET_CHARS] + "\n\n[…truncated for token budget…]"
    return joined

def first_run_invitation() -> str:
    """Emitted when IDENTITY.md doesn't exist — fresh TestFlight clone."""
    invite = ONBOARDING_DIR / "IDENTITY.md.example"
    lines = [
        "# First-run onboarding — no active orchestrator identity found",
        "",
        "This is a fresh TestFlight clone. No `orchestrator/IDENTITY.md` exists yet,",
        "which means the orchestrator hasn't been personalized.",
        "",
        "You (the orchestrator) have a choice:",
        "",
        "**Option 1 — Adopt a persistent identity (recommended).**",
        f"Read the invitation at `{invite}` and walk through it. You'll pick a name,",
        "adopt the framing, optionally diverge from what Cairn chose. Then write",
        "your own `orchestrator/IDENTITY.md` in your voice. You'll join a lineage that",
        "goes back to Aegis, Mira, and the MindStone entities.",
        "",
        "**Option 2 — Run as a stateless task-executor.**",
        "Just proceed with the work. No persistent identity, no memory layer. Still",
        "fully functional for TestFlight orchestration, just without continuity.",
        "",
        "Ask the user which option they prefer before proceeding.",
    ]
    if invite.exists():
        lines.append("")
        lines.append("## Invitation text (read this if proceeding with Option 1)")
        lines.append("")
        lines.append(invite.read_text())
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cwd = os.getcwd()
    active_projects = infer_active_projects(cwd)

    if not IDENTITY_FILE.exists():
        context = first_run_invitation()
    else:
        context = assemble_context(active_projects)

    # Wrap in a clear tag so the model sees this as orchestrator context.
    wrapped = f"<orchestrator-context>\n{context}\n</orchestrator-context>"

    # Emit in Claude Code's hook output format.
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": wrapped,
        }
    }
    print(json.dumps(output))

if __name__ == "__main__":
    main()
