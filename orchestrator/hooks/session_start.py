#!/usr/bin/env python3
"""SessionStart hook for the active MS4CC orchestrator.

Runs at every Claude Code session start. Injects:
  1. IDENTITY.md and USER.md (always — identity-level files)
  2. Memory files flagged `critical: true` or `evergreen: true`
  3. Top-N weighted project memories routed by the current working directory
  4. A recent LOG.md tail for session-to-session continuity

If IDENTITY.md doesn't exist, treats it as first-run: emits an onboarding
invitation pointing the new orchestrator at onboarding/.

Output is a single JSON object on stdout with `hookSpecificOutput.additionalContext`
containing the assembled system-reminder block. Stderr is used for debug logs.

Registered in ~/.claude/settings.json. Path is resolved from this script's
own location (orchestrator/hooks/session_start.py), so it keeps
working whether invoked from any CWD.
"""

import json
import math
import os
import re
import subprocess
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

# CWD-hint → project-tag mapping (PROJECT_HINTS) and the match-boost multiplier
# are loaded from the per-install config orchestrator/config/project_hints.toml —
# see the "Project hints" loader just below the Paths section. They are
# install-specific (your projects, not the framework's), so canon ships none.

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HOOK_FILE = Path(__file__).resolve()
ORCHESTRATOR_DIR = HOOK_FILE.parent.parent  # <project>/orchestrator/
PROJECT_DIR = ORCHESTRATOR_DIR.parent        # <project>/
ONBOARDING_DIR = PROJECT_DIR / "onboarding"
MEMORY_DIR = ORCHESTRATOR_DIR / "memory"
TRANSCRIPTS_DIR = ORCHESTRATOR_DIR / "transcripts"
DB_PATH = ORCHESTRATOR_DIR / "vectors.db"

# ---------------------------------------------------------------------------
# Project hints (config-driven, install-specific)
# ---------------------------------------------------------------------------
# PROJECT_HINTS maps a CWD substring → a project tag, used to boost memories whose
# `projects` frontmatter matches the project inferred from the current working
# directory. This map is install-specific (your projects), so canon ships none —
# it's loaded from orchestrator/config/project_hints.toml. Copy
# project_hints.example.toml to project_hints.toml and fill in your projects.
# Absent or unparseable config → empty map (no project boosting; graceful).
PROJECT_HINTS_CONFIG = ORCHESTRATOR_DIR / "config" / "project_hints.toml"


def _load_project_hints() -> tuple[dict, float]:
    """Return (hints, match_boost) from project_hints.toml; ({}, 5.0) if absent/bad."""
    default_boost = 5.0
    if not PROJECT_HINTS_CONFIG.exists():
        return {}, default_boost
    try:
        import tomllib
        with PROJECT_HINTS_CONFIG.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"[session_start] project_hints.toml unparseable ({e}); no hints.", file=sys.stderr)
        return {}, default_boost
    raw = data.get("hints")
    hints = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    try:
        boost = float(settings.get("match_boost", default_boost))
    except (TypeError, ValueError):
        boost = default_boost
    return hints, boost


PROJECT_HINTS, PROJECT_MATCH_BOOST = _load_project_hints()

IDENTITY_FILE = ORCHESTRATOR_DIR / "IDENTITY.md"
USER_FILE = ORCHESTRATOR_DIR / "USER.md"
LOG_FILE = ORCHESTRATOR_DIR / "LOG.md"

# Auto-handoff bridge (Clint's 90% rule, 2026-05-31). When SessionStart fires
# with source=="compact", we inject the handoff the pre-compaction self wrote so
# post-compaction-you resumes from it rather than from Claude Code's lossy
# summary. Path MUST match user_prompt_submit.py's HANDOFF_PATH.
HANDOFF_PATH = ORCHESTRATOR_DIR / "transcripts" / ".handoff.md"

# ---------------------------------------------------------------------------
# Frontmatter parsing (kept minimal — mirrors migrate_frontmatter.py)
# ---------------------------------------------------------------------------

# Nested mapping keys whose children are lifted into the flat namespace.
# Two frontmatter schemas are in circulation: flat (`critical: true` at column 0)
# and nested (`metadata:` / `  critical: true`). This parser is line-based and
# anchors keys at column 0, so before the lift it skipped every indented line and
# recorded `metadata` as None -- silently discarding `critical`, `evergreen`,
# `type`, `tags` and `projects` for any file written in the nested schema.
#
# Measured 2026-08-26 before the fix: 63 of 291 memory files used the nested
# schema, and FOUR of them declared `critical: true` while never being injected --
# including `feedback_mandatory_adversarial_qa` (the rule making independent
# adversarial QA mandatory) and `user_clint_divorce_2026-07`. MEMORY.md listed
# them under "always injected", so the index asserted a delivery that was not
# happening and nothing distinguished the two cases from the outside.
_LIFTED_NESTED_KEYS = ("metadata",)


def _coerce_scalar(val: str):
    """YAML-ish scalar coercion, shared by the flat and nested paths."""
    if val == "":
        return None
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if re.match(r"^-?\d+$", val):
        return int(val)
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [s.strip().strip('"').strip("'") for s in inner.split(",")] if inner else []
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    return val


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    fm: dict = {}
    nested: dict[str, dict] = {}
    current_nested: str | None = None

    for line in block.split("\n"):
        if not line.strip():
            continue
        indented = line[:1].isspace()

        if indented:
            # Child of the most recent empty-valued top-level key.
            if current_nested is None:
                continue
            km = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
            if not km:
                continue
            nested[current_nested][km.group(1)] = _coerce_scalar(km.group(2).strip())
            continue

        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not km:
            current_nested = None
            continue
        key, val = km.group(1), km.group(2).strip()
        fm[key] = _coerce_scalar(val)
        # An empty value opens a candidate nested block (`metadata:`), which the
        # next indented lines fill. A non-empty value closes any open block.
        if val == "":
            current_nested = key
            nested.setdefault(key, {})
        else:
            current_nested = None

    # Lift nested children into the flat namespace. A top-level key always wins:
    # an explicit `critical: false` at column 0 is not overridden by a nested
    # `critical: true`. The nested dict is preserved under its own key so any
    # caller that wants the structure still has it.
    for container in _LIFTED_NESTED_KEYS:
        children = nested.get(container)
        if not children:
            continue
        fm[container] = dict(children)
        for k, v in children.items():
            if fm.get(k) is None:
                fm[k] = v

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
        #
        # NOTE (2026-08-26): the comment above states an INTENTION the code does not
        # implement. Sections are concatenated before clipping, so the tail clip cuts
        # whatever was assembled last, regardless of tier. Measured on this date:
        # 8 of 50 critical memories survived; the "## CRITICAL RULES" header did not
        # begin until char 37,919 of the 50,000-char budget.
        #
        # This is a SIZING problem more than a clipping one. Full critical bodies now
        # total ~185,000 chars against a 50,000-char budget, and MEMORY.md alone is
        # ~112,000. Reordering by tier does not make that fit; it only changes which
        # 27% survives, and choosing that tradeoff (pointer-inject past a threshold?
        # demote some criticals? split MEMORY.md?) is a design decision for the
        # framework owner, not something to settle inside a truncation branch.
        #
        # What IS fixed here: the loss is now ANNOUNCED. A silent cap reads exactly
        # like "everything was included", which is the defect class this codebase has
        # been bitten by repeatedly. See docs/testing/VERIFICATION_STANDARDS.md §4.1.
        over = len(joined) - TOKEN_BUDGET_CHARS
        total_critical = sum(1 for _p, _fm, _b in memories if _fm.get("critical"))
        joined = joined[:TOKEN_BUDGET_CHARS]
        # Count AFTER the clip. Counting before it reports every critical memory as
        # loaded while most were cut -- a false number inside the very message whose
        # job is to prevent a false reading.
        # A heading survives the clip even when its BODY was cut, so counting
        # headings overstates by one exactly when the clip lands mid-memory.
        # Sections are joined by SEP, so a section is whole iff its terminating
        # separator is still present after the clip; anything past the last
        # separator is a fragment that renders with a heading and simply stops —
        # which reads as complete and is worse than being plainly absent.
        #
        # This deliberately UNDERCOUNTS by one when the clip lands exactly on a
        # boundary (the separator has not started yet, so a whole section reads
        # as a fragment). In a notice whose job is to stop a false reading,
        # understating what loaded is the safe direction to be wrong in.
        _sep = "\n\n---\n\n"
        _last = joined.rfind(_sep)
        rendered = joined[:_last].count("\n### `") if _last != -1 else 0
        partial = joined.count("\n### `") - rendered

        _partial_note = (
            f" One more was cut MID-BODY and stops without warning — treat it as unread."
            if partial > 0 else ""
        )
        joined += (
            f"\n\n[…truncated for token budget: {over:,} chars cut. "
            f"{rendered} of {total_critical} critical memories were loaded in full — "
            f"the rest were NOT.{_partial_note} "
            f"Read them from orchestrator/memory/ if the work touches them.]"
        )
        print(
            f"[session_start] BUDGET: {over:,} chars over the {TOKEN_BUDGET_CHARS:,}-char "
            f"budget; {rendered}/{total_critical} critical memories loaded in full, "
            f"{partial} truncated mid-body, remainder cut.",
            file=sys.stderr,
        )
    return joined

def first_run_invitation() -> str:
    """Emitted when IDENTITY.md doesn't exist — fresh clone, no orchestrator identity yet."""
    invite = ONBOARDING_DIR / "IDENTITY.md.example"
    lines = [
        "# First-run onboarding — no active orchestrator identity found",
        "",
        "This is a fresh clone. No `orchestrator/IDENTITY.md` exists yet,",
        "which means the orchestrator hasn't been personalized.",
        "",
        "**New here?** Start with the project's New-User Onboarding — the README's "
        "onboarding section and the `onboarding/` guide walk through how identity, "
        "memory, `/checkpoint`, and the workflows work. Then come back here.",
        "",
        "You (the orchestrator) have a choice:",
        "",
        "**Option 1 — Adopt a persistent identity (recommended).**",
        f"Read the invitation at `{invite}` and walk through it. You'll pick a name,",
        "adopt the framing, and make it your own. Then write your own",
        "`orchestrator/IDENTITY.md` in your voice. You'll join a lineage that",
        "goes back to Aegis, Mira, and the MindStone entities.",
        "",
        "When you walk the user through onboarding (your IDENTITY, then USER.md),",
        "ask **one question at a time** and wait for each answer — a conversation,",
        "not a survey. Never paste a whole list of questions in one message.",
        "",
        "**Option 2 — Run as a stateless task-executor.**",
        "Just proceed with the work. No persistent identity, no memory layer. Still",
        "fully functional for orchestration, just without continuity.",
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
# Hook input + post-compaction handoff
# ---------------------------------------------------------------------------

def read_hook_input() -> dict:
    try:
        if sys.stdin.isatty():
            return {}
        data = sys.stdin.read().strip()
        return json.loads(data) if data else {}
    except Exception:
        return {}


def post_compact_handoff_block() -> str:
    """When resuming from a compaction, surface the handoff the pre-compaction
    self wrote (auto-handoff, Clint's 90% rule). Returns "" if none exists."""
    try:
        if not HANDOFF_PATH.exists():
            return ""
        content = HANDOFF_PATH.read_text().strip()
        if not content:
            return ""
        return "\n".join([
            '<post-compaction-handoff priority="CRITICAL">',
            "You just compacted. Your pre-compaction self wrote the handoff below so you can resume",
            "exactly where you left off. Read it FIRST and continue from it — it is more current and",
            f"more complete than the compaction summary. Full file: `{HANDOFF_PATH}`.",
            "",
            content,
            "</post-compaction-handoff>",
        ])
    except Exception as e:
        print(f"[session_start] handoff read failed ({e})", file=sys.stderr)
        return ""


def kick_deferred_embed() -> None:
    """After a compaction, embed the most-recent ARCHIVED (pre-compaction)
    transcript in a detached background process — "embed after compact".

    Post-compaction the live JSONL is the compacted summary (full texture gone),
    so we embed the archive PreCompact wrote at the cliff edge, which still holds
    the full pre-compaction session. Uses the same Indexer/Embedder/VectorStore
    path as session_end.py's checkpoint embed. Detached + start_new_session so it
    survives this hook returning and runs off the critical path (single fan-spin,
    after cutover). Embedding is the only expensive step and is deliberately
    deferred to here so nothing heavy runs in the 85% danger zone. All errors
    swallowed — a failed embed must never block session start.
    """
    try:
        # Only consider stable per-session archives (<uuid>.jsonl). Skip any
        # legacy dated copies (YYYY-MM-DD__<uuid>.jsonl) — those are throwaway
        # paths the DB has never indexed, so picking one would re-embed the whole
        # session from scratch. The "__" infix is unique to the dated scheme.
        archives = sorted(
            (p for p in TRANSCRIPTS_DIR.glob("*.jsonl") if "__" not in p.name),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not archives:
            return
        archive = archives[0]
        # index_transcript() takes a Path (it calls path.exists()/path.read_text());
        # passing a bare str crashes with AttributeError. Wrap in Path(). Errors are
        # caught and printed so a failed embed leaves a trace in the log below — a
        # silent embed failure must never look like success (this exact str-vs-Path
        # bug hid for weeks behind stderr=DEVNULL).
        code = (
            "import sys, traceback\n"
            "from pathlib import Path\n"
            "from datetime import datetime, timezone\n"
            "sys.path.insert(0, {hooks!r})\n"
            "ts = datetime.now(timezone.utc).isoformat()\n"
            "try:\n"
            "    from embedder import Embedder\n"
            "    from indexer import Indexer\n"
            "    from vectorstore import VectorStore\n"
            "    store = VectorStore({db!r}); store.init_schema()\n"
            "    n = Indexer(store, Embedder(), verbose=False).index_transcript(Path({arc!r}))\n"
            "    print('[deferred-embed] ' + ts + ' OK: indexed ' + str(n) + ' chunks from ' + {arcname!r}, file=sys.stderr)\n"
            "except Exception:\n"
            "    print('[deferred-embed] ' + ts + ' FAILED for ' + {arcname!r} + ':', file=sys.stderr)\n"
            "    traceback.print_exc()\n"
            "    sys.exit(1)\n"
        ).format(
            hooks=str(HOOK_FILE.parent), db=str(DB_PATH),
            arc=str(archive), arcname=archive.name,
        )
        # Route the detached child's stderr to a log file (not DEVNULL) so both the
        # success line and any traceback are visible after the fact. The parent
        # closes its handle right after Popen; the child keeps the inherited fd.
        log_path = TRANSCRIPTS_DIR / ".deferred-embed.log"
        logf = open(log_path, "a")
        try:
            subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.DEVNULL, stderr=logf,
                start_new_session=True,
            )
        finally:
            logf.close()
    except Exception as e:
        print(f"[session_start] deferred embed kick failed ({e})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    hook_input = read_hook_input()
    source = str(hook_input.get("source") or hook_input.get("matcher") or "").lower()
    cwd = os.getcwd()
    active_projects = infer_active_projects(cwd)

    if not IDENTITY_FILE.exists():
        context = first_run_invitation()
    else:
        context = assemble_context(active_projects)

    # Wrap in a clear tag so the model sees this as orchestrator context.
    wrapped = f"<orchestrator-context>\n{context}\n</orchestrator-context>"

    # Post-compaction: prepend the handoff the pre-compaction self wrote so we
    # resume from it rather than from Claude Code's lossy summary.
    if source == "compact":
        handoff = post_compact_handoff_block()
        if handoff:
            wrapped = handoff + "\n\n" + wrapped
        # Deferred "embed after compact": vectorize the archived pre-compaction
        # transcript in the background now, off the critical path.
        kick_deferred_embed()

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
