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
import traceback
import sys
from datetime import datetime, timezone
from pathlib import Path

# Canonical authority base (log1p(hits) + 3*prevented) lives in memory_weight.py so
# every substrate shares ONE hardened formula. Guarded: if the import fails we fall
# back to a safe inline copy in weight(), so session start never hard-depends on it.
try:
    from memory_weight import base as _authority_base
except Exception:
    _authority_base = None

# The bounded index generator. Guarded the same way — but note the fallback is NOT
# "no index". An agent without the index cannot see what exists and therefore cannot
# tell "I have no memory of that" from "I was never shown it", which is the single
# most misleading state this system can boot into. So the fallback is the OLD
# behaviour (MEMORY.md verbatim, unbounded) and it announces itself on stderr.
try:
    import memory_index as memory_index_mod
except Exception as _e:  # pragma: no cover - import guard
    memory_index_mod = None
    print(f"[session_start] memory_index unavailable ({_e}); "
          f"falling back to UNBOUNDED MEMORY.md injection", file=sys.stderr)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Token budget for the entire injected block. Rough estimate — 1 token ≈ 4 chars.
#
# 80,000 (~20k tokens) per design decision D2. Chosen by measurement, not feel: on a
# 102-file store the always-inject block totals ~58,400 chars once pointer lists are
# deduped, which overflows 50k (117%) and fits 80k at 73% — leaving headroom, which
# matters because this degrades FASTER than it grows (a bigger store means more rules
# competing for a fixed budget, so coverage falls as the store rises).
#
# Deliberately ONE number with a per-install override rather than a table of tuned
# per-branch values: the measurement covers one store, and three numbers extrapolated
# from it would be false precision. Any other install — especially a larger one —
# should set MS4CC_CONTEXT_BUDGET_CHARS from its OWN measured utilisation, which
# assemble_context() reports every run.
TOKEN_BUDGET_CHARS = 80000

# ADMISSION TIERS — declared precedence, lowest admitted first.
#
# Ordering is a POLICY, stated here, not an accident of `sorted(glob)`. Under the
# old positional clip a rule's survival depended on its filename: `feedback_*` sorts
# before `lineage_*`, `project_*`, `reference_*`, so whole categories died by prefix
# with no judgement about importance involved anywhere.
#
# The index outranks full critical bodies on purpose (design D1). Knowing WHAT
# EXISTS is worth more per character than any single rule's full text, because the
# index is what makes a partial constitution recoverable: an agent that can see a
# rule's name and description can go and read the file. An agent missing the index
# does not know the rule exists to be read.
TIER_IDENTITY = 10   # required — an agent without identity is a different agent
TIER_USER = 20       # required
TIER_INVARIANT = 25  # the constitution itself: the binding rule of every critical
TIER_INDEX = 30      # what exists at all
TIER_CRITICAL = 40   # full narrative bodies — admitted ONLY if budget remains
TIER_EVERGREEN = 50  # pointers
TIER_CONTEXT = 60    # weighted pointers
TIER_LOG = 70        # continuity tail

# Populated by assemble_context() on every run: a machine-readable account of what
# was admitted, so `--check`, CI and the checkpoint flow can gate on a result rather
# than scraping the human-readable notice.
LAST_ASSEMBLY: dict = {}


def context_budget_chars() -> int:
    """Effective budget: env override wins, else the measured default."""
    raw = os.environ.get("MS4CC_CONTEXT_BUDGET_CHARS")
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
            print(f"[session_start] MS4CC_CONTEXT_BUDGET_CHARS={raw!r} is not positive; "
                  f"using {TOKEN_BUDGET_CHARS}.", file=sys.stderr)
        except ValueError:
            print(f"[session_start] MS4CC_CONTEXT_BUDGET_CHARS={raw!r} is not an integer; "
                  f"using {TOKEN_BUDGET_CHARS}.", file=sys.stderr)
    return TOKEN_BUDGET_CHARS

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

# Auto-handoff bridge (Clint's 90% rule, 2026-05-31; generalized 2026-07-01).
# SessionStart injects the handoff the previous self wrote on source in
# {compact, resume, startup} so continuity survives compaction AND a fresh
# relaunch/resume — not just the compaction cliff. (Deferred embed stays
# compact-only.) Path MUST match user_prompt_submit.py's HANDOFF_PATH.
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
# including the rule making independent adversarial QA mandatory, and a memory
# holding sensitive personal context. MEMORY.md listed them under "always
# injected", so the index asserted a delivery that was not happening and nothing
# distinguished the two cases from the outside.
#
# Memory FILENAMES are operator data, not framework data. They are derived from
# the memory's subject, so naming one in a tracked file publishes what the
# operator keeps memories ABOUT even when the content itself never leaves the
# machine. Describe the category ("a memory holding sensitive personal context"),
# never the filename. See feedback_no_project_names_in_public_repos.
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
    # Open folded/literal block scalar: (key, style, [lines]).
    current_block: tuple[str, str, list] | None = None

    # `>` folds newlines to spaces, `|` keeps them; a trailing `-` strips the final
    # newline. Without this, `invariant: >` parsed to the literal string ">" -- a
    # value that reports as PRESENT while carrying no rule, which is the exact
    # false-positive shape the invariant tier exists to eliminate.
    BLOCK_STYLES = (">", "|", ">-", "|-", ">+", "|+")

    def _close_block():
        nonlocal current_block
        if current_block is None:
            return
        key, style, lines = current_block
        # Strip the common indent, then fold or keep newlines per style.
        stripped = [ln.strip() for ln in lines]
        while stripped and not stripped[-1]:
            stripped.pop()
        joined = ("\n".join(stripped) if style.startswith("|")
                  else " ".join(s for s in stripped if s))
        fm[key] = joined.strip()
        current_block = None

    for line in block.split("\n"):
        indented = line[:1].isspace()

        # An open block scalar consumes every indented line, blank ones included.
        if current_block is not None:
            if indented or not line.strip():
                current_block[2].append(line)
                continue
            _close_block()

        if not line.strip():
            continue

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

        if val in BLOCK_STYLES:
            current_block = (key, val, [])
            current_nested = None
            continue

        fm[key] = _coerce_scalar(val)
        # An empty value opens a candidate nested block (`metadata:`), which the
        # next indented lines fill. A non-empty value closes any open block.
        if val == "":
            current_nested = key
            nested.setdefault(key, {})
        else:
            current_nested = None

    _close_block()

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

def _safe_num(v):
    """Finite-float coercion for the inline weight() fallback (mirrors memory_weight._num)."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _as_str_list(v) -> list[str]:
    """Frontmatter list fields, coerced. Total: no input raises.

    `projects: [a, b]` -> ["a","b"];  `projects: a` -> ["a"];  `projects: 5` ->
    ["5"];  absent/None -> []. A scalar becomes a one-element list rather than
    being iterated character-by-character, which is what the old `or []` did to
    a bare string — `"threatgen"` silently matched the project `"t"`.
    """
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return [str(v)]


def weight(fm: dict, now: datetime, active_projects: set) -> float:
    """Compute injection-ranking weight for a memory.

    Infinity for critical/evergreen (always inject up to budget).
    Otherwise: (log1p(hits) + 3*prevented + 1) * exp(-age/half_life), boosted if
    the memory's `projects` field matches the CWD-inferred project.
    """
    if fm.get("critical") or fm.get("evergreen"):
        return float("inf")

    # Authority-forward base: `hits` is an age-odometer (accumulates with a memory's
    # presence, not its usefulness), so it enters ranking only as log1p — dampened so
    # the human-confirmed `prevented` signal isn't numerically swamped (MS4CC #63).
    # Prefer the shared canonical formula (memory_weight.base); fall back to a safe
    # inline copy. BOTH clamp hits/prevented >=0 and coerce garbage/inf to finite, so a
    # corrupt frontmatter value can never crash this SessionStart critical path
    # (log1p domain error / overflow). Wrapped so it degrades to recency-only, never raises.
    try:
        if _authority_base is not None:
            base = _authority_base(fm) + 1.0
        else:
            hits = max(0.0, _safe_num(fm.get("hits")))
            prevented = max(0.0, _safe_num(fm.get("prevented")))
            base = math.log1p(hits) + 3 * prevented + 1.0
    except Exception:
        base = 1.0

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

    # Clamp half-life to a positive value: a corrupt non-positive half_life_days would
    # flip the exponent positive and blow decay up to ~10^100+ (or OverflowError at
    # extreme age), letting one bad frontmatter value dominate the whole ranking.
    # Mirrors memory_weight.decay() — keep the two in sync.
    half_life = _safe_num(fm.get("half_life_days")) or 30
    if half_life <= 0:
        half_life = 30
    decay = math.exp(-age_days / half_life)

    w = base * decay

    # Coerce to a list of strings. `projects: threatgen` (a bare scalar) and
    # `projects: 5` are both things a hand-edit produces, and the second still
    # raised `TypeError: 'int' object is not iterable` — #71, the FIRST instance
    # of the class that #95 was the second of. The outer guard in main() now keeps
    # either from zeroing the session, but a memory should not lose its project
    # boost because someone typed a value without brackets.
    projects = _as_str_list(fm.get("projects"))
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
    """Build the <orchestrator-context> block.

    Admission is PER ITEM and ordered by declared precedence — see ADMISSION TIERS.
    Nothing is ever cut mid-item: a memory is injected whole or not at all, and
    whatever does not fit is NAMED in an in-band notice rather than vanishing.

    Assembly NEVER aborts. A session that boots with a partial constitution can
    compensate (read the file, ask before acting); one that boots empty cannot,
    and cannot even read the error explaining why. See §4.3 of
    docs/design/context-budget-and-memory-tiering.md.
    """
    now = datetime.now(tz=timezone.utc)
    items: list[dict] = []
    # Resolved once, up front: the index tier needs it to size its allocation
    # BEFORE admission runs, not just at the admission step.
    budget_for_index = budget = context_budget_chars()

    def add(tier: int, text: str, *, label: str = "", required: bool = False):
        if text and text.strip():
            items.append({"tier": tier, "text": text, "label": label, "required": required})

    # --- Identity & user: REQUIRED. Admitted even if they exceed the budget on
    # their own — an agent without its identity is not a degraded agent, it is a
    # different one. The overage is reported rather than silently absorbed.
    if IDENTITY_FILE.exists():
        add(TIER_IDENTITY, "## IDENTITY (who I am)\n" + IDENTITY_FILE.read_text(),
            label="IDENTITY", required=True)
    if USER_FILE.exists():
        add(TIER_USER, "## USER (who I'm working with)\n" + USER_FILE.read_text(),
            label="USER", required=True)

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

    # Critical memories inject in TWO tiers.
    #
    # The INVARIANT is the binding rule and it always goes in — 32 of them cost
    # ~9k chars where their full bodies cost 146,913 (184% of an 80k budget). That
    # is the whole point of the split: ordering alone cannot make full text fit,
    # and no budget large enough exists at store scale.
    #
    # The BODY is the narrative that earned the rule. It is admitted only if room
    # remains after identity, the constitution and the index — and it is always
    # reachable on disk and through recall, so losing it costs context, not access.
    #
    # A file with no invariant falls back to full body at the invariant tier: it is
    # unmigrated, not exempt, and dropping it silently would be the failure this
    # design exists to prevent. The audit names it.
    missing_invariant = []
    for path, fm, body in critical_full:
        desc = fm.get("description", path.name)
        inv = (fm.get("invariant") or "").strip()
        if inv:
            add(TIER_INVARIANT, f"- **`{path.name}`** — {inv}", label=f"invariant:{path.name}")
            add(TIER_CRITICAL, f"### `{path.name}` — {desc}\n{body.strip()}", label=path.name)
        else:
            missing_invariant.append(path.name)
            add(TIER_INVARIANT, f"### `{path.name}` — {desc}\n{body.strip()}", label=path.name)

    # NOTE: there is no longer an "evergreen pointer list".
    #
    # It was a SUBSET of the memory index — 54 of 55 entries duplicated verbatim,
    # 23,021 chars of the block spent saying the same thing twice (#87) — kept only
    # as a fallback for installs with no MEMORY.md on disk. The index is now
    # GENERATED from frontmatter rather than read from that file, so it exists
    # whenever memories exist and covers every evergreen memory by construction.
    # The fallback would therefore fire never, or fire and re-create #87. Removed
    # rather than left as a comforting no-op.
    _ = evergreen_pointers  # retained above for the tier split; no longer injected

    # --- Weighted project memories (top-N) ---
    # Belt-and-suspenders: weight() is itself crash-proof now, but the whole ranking is
    # still wrapped so any unforeseen raise degrades to "skip the weighted section"
    # (identity/critical/evergreen/index/log still inject) rather than aborting the
    # entire SessionStart context.
    try:
        ranked = [
            (path, fm, weight(fm, now, active_projects))
            for path, fm in rankable
        ]
        ranked.sort(key=lambda t: t[2], reverse=True)
        selected = ranked[:TOP_N_PROJECT_MEMORIES]
    except Exception as e:
        print(f"[session_start] weighted-memory ranking skipped ({e})", file=sys.stderr)
        selected = []

    if selected:
        lines = ["## CONTEXT MEMORIES (weighted for current CWD)"]
        if active_projects:
            lines.append(f"_Active project hints from CWD: {', '.join(sorted(active_projects))}_\n")
        for path, fm, w in selected:
            desc = fm.get("description", path.name)
            projects = fm.get("projects") or []
            proj_str = f" [{', '.join(projects)}]" if projects else ""
            lines.append(f"- `{path.name}`{proj_str} — {desc}")
        add(TIER_CONTEXT, "\n".join(lines), label="CONTEXT")

    # --- Memory index: admitted BEFORE full critical bodies (design D1) ---
    # Knowing WHAT EXISTS is worth more per char than any single rule's full text.
    # The index is the one artefact that makes a partial constitution recoverable:
    # an agent that can see a rule's name and description can go read it. An agent
    # missing the index does not know the rule exists to be read.
    #
    # Before this change the index was assembled LAST and the clip was a positional
    # slice, so it reached context 0% of the time on this store — the capability
    # existed and was deleted, every session, unreported.
    # The index is GENERATED from frontmatter and BOUNDED to a share of the budget.
    # It used to be `MEMORY.md` read verbatim, which has two defects:
    #
    #   1. Unbounded. It is the only tier whose cost scales with memory COUNT rather
    #      than with how many memories are binding. The constitution stops growing;
    #      the index grows every checkpoint, forever. On a sibling store that file is
    #      already 114,177 chars — 143% of the whole budget by itself.
    #   2. Drift. A hand-maintained pointer file silently falls out of sync with the
    #      corpus; #86 found 16 memories with no pointer at all. Generating from each
    #      memory's own frontmatter makes that class of gap structurally impossible.
    #
    # Descriptions are still human-written — the generator enforces width and order,
    # it does not summarise. `MEMORY.md` remains the human artefact on disk, with its
    # categories and conventions, and #86's completeness check still guards it.
    index_stats: dict = {}
    if memory_index_mod is not None:
        # The allocation is the SMALLER of its budget share and what is actually
        # left after everything that outranks it (identity, user, the constitution).
        #
        # A fixed 40% of the TOTAL is wrong when the budget is squeezed: the index
        # gets built to a size that cannot be admitted, then admission drops it
        # WHOLE, and the agent loses all 102 names instead of shrinking to a
        # name-only roster that would have fitted. Sizing against the real
        # remainder is what makes "degrade the description, never the existence"
        # hold at small budgets as well as large ones.
        spent_above = sum(len(i["text"]) + len("\n\n---\n\n")
                          for i in items if i["tier"] < TIER_INDEX)
        allocation = max(0, min(int(budget_for_index * memory_index_mod.INDEX_ALLOCATION_FRACTION),
                                budget_for_index - spent_above - len("\n\n---\n\n")))
        index_text, index_stats = memory_index_mod.build_index(memories, allocation)
        if index_text:
            add(TIER_INDEX, index_text, label="MEMORY INDEX")
    else:
        legacy_index = MEMORY_DIR / "MEMORY.md"
        if legacy_index.exists():
            add(TIER_INDEX, "## MEMORY INDEX (all available memories)\n" + legacy_index.read_text(),
                label="MEMORY INDEX")

    # --- Recent LOG tail for continuity ---
    if LOG_FILE.exists():
        log_text = LOG_FILE.read_text()
        lines = log_text.split("\n")
        tail = "\n".join(lines[-LOG_TAIL_LINES:])
        add(TIER_LOG, "## RECENT LOG (tail for continuity)\n" + tail, label="RECENT LOG")

    # ------------------------------------------------------------------
    # Admission. Per item, in precedence order, whole-or-not-at-all.
    # ------------------------------------------------------------------
    # This replaces `joined[:BUDGET]`. That slice cut wherever the character
    # count landed, which meant (a) one memory always ended mid-sentence and
    # read as complete, and (b) everything assembled after the cut vanished
    # regardless of importance — on this store the memory index, the pointer
    # lists and the log tail reached context 0% of the time.
    #
    # Ordering is DECLARED (the TIER_* constants), not an accident of
    # alphabetical filenames. If something has to be dropped, the choice is
    # explicit and recorded rather than decided by a leading underscore.
    SEP = "\n\n---\n\n"

    admitted: list[dict] = []
    omitted: list[dict] = []
    used = 0
    for item in sorted(items, key=lambda i: i["tier"]):
        cost = len(item["text"]) + (len(SEP) if admitted else 0)
        if item["required"] or used + cost <= budget:
            admitted.append(item)
            used += cost
        else:
            omitted.append(item)

    # Account for the two tiers SEPARATELY. Counting only bodies reports "6 of 32
    # critical" while all 32 binding rules are in fact loaded -- an alarm raised by
    # the design working as intended, which is how a useful signal gets ignored.
    #
    # Constitution completeness is the property that matters and must be total.
    # Narrative coverage is best-effort by construction: the body is deferred to
    # disk and recall, so its absence costs context, never access.
    n_inv_total = sum(1 for i in items if i["tier"] == TIER_INVARIANT)
    n_inv_ok = sum(1 for i in admitted if i["tier"] == TIER_INVARIANT)
    n_crit_total = sum(1 for i in items if i["tier"] == TIER_CRITICAL)
    n_crit_ok = sum(1 for i in admitted if i["tier"] == TIER_CRITICAL)
    missing = [i["label"] for i in omitted]
    missing_invariants = [i["label"] for i in omitted if i["tier"] == TIER_INVARIANT]
    index_dropped = any(i["tier"] == TIER_INDEX for i in omitted)

    # Report BEFORE the notice is built, so a caller (`--check`, the checkpoint
    # flow, CI) can gate on a machine-readable result instead of scraping prose.
    global LAST_ASSEMBLY
    LAST_ASSEMBLY = {
        "budget": budget,
        "used": used,
        "utilisation": (used / budget) if budget else 0.0,
        "over_budget": used > budget,
        "items_total": len(items),
        "items_admitted": len(admitted),
        # The property that must hold: every binding rule reached context.
        "invariants_total": n_inv_total,
        "invariants_admitted": n_inv_ok,
        "missing_invariants": missing_invariants,
        "constitution_complete": not missing_invariants,
        "index_present": not index_dropped,
        # The index is now bounded, so "present" is no longer the whole story:
        # it can be present and rationed. Report the shape so a caller can tell
        # a full catalogue from a name-only one instead of inferring from size.
        "index_entries": index_stats.get("entries", 0),
        "index_listed": index_stats.get("listed", 0),
        "index_full": index_stats.get("full", 0),
        "index_name_only": index_stats.get("name_only", 0),
        "index_chars": index_stats.get("chars", 0),
        "index_over_allocation": index_stats.get("over_allocation", False),
        # The hard alarm: the catalogue could not hold even the NAMES of this many
        # memories. Existence rationed, not just description. Must be 0 in a healthy
        # install; non-zero means the agent cannot see part of its own store.
        "index_unlisted": index_stats.get("unlisted", 0),
        "index_roster_truncated": index_stats.get("roster_truncated", False),
        "index_signal_saturated": index_stats.get("last_applied_saturated", False),
        # Best-effort by design: narrative deferred to disk + recall.
        "bodies_total": n_crit_total,
        "bodies_admitted": n_crit_ok,
        "omitted": missing,
        # `complete` now means "nothing that MUST be present is missing", not
        # "nothing at all was deferred" -- deferring narrative is the design.
        "complete": (not missing_invariants) and (not index_dropped),
    }

    HEADINGS = {
        TIER_INVARIANT: ("## CONSTITUTION (binding rules — always applied)\n"
                         "_Each line is the rule itself. The incident that earned it lives in the "
                         "named file and is retrievable; read it before acting on anything subtle._"),
        TIER_CRITICAL: ("## CRITICAL MEMORIES (full text — the narrative behind the rules above)\n"
                        "_Present only as budget allowed. Absence here is not absence of the rule; "
                        "the rule is in the constitution above._"),
    }
    # Invariants are one-line bullets and must render as a LIST. Admission is
    # per-item so each can be dropped independently, but joining 32 bullets with a
    # horizontal rule turns the constitution into 32 separated blocks — harder to
    # read as a single body of rules, which is exactly how it should read.
    body_parts, group = [], []
    seen_tiers = set()

    def _flush():
        if group:
            body_parts.append("\n".join(group))
            group.clear()

    for item in admitted:
        if item["tier"] in HEADINGS and item["tier"] not in seen_tiers:
            _flush()
            body_parts.append(HEADINGS[item["tier"]])
        seen_tiers.add(item["tier"])
        if item["tier"] == TIER_INVARIANT and item["text"].startswith("- "):
            group.append(item["text"])          # bullet: keep in the running list
        else:
            _flush()
            body_parts.append(item["text"])
    _flush()
    joined = SEP.join(body_parts)

    # In-band notice at the TOP. At the bottom it competes with the recency the
    # rest of the block is fighting for, and it is precisely the line that must
    # not be skimmed: it is the difference between "I have my rules" and "I have
    # most of my rules and here are the names of the ones I do not."
    if missing_invariants or index_dropped:
        # The serious case: a BINDING RULE or the index did not reach context.
        #
        # The heading must name what ACTUALLY failed. An earlier version always
        # said "INCOMPLETE CONSTITUTION", so a run with all 32 rules loaded and
        # only the index dropped announced "0 binding rule(s) did not fit" — a
        # warning that contradicts itself, which is how a real warning gets
        # trained away. Same defect class as counting bodies and reporting 6/32.
        names = ", ".join(f"`{m}`" for m in missing_invariants[:12])
        more = f" (+{len(missing_invariants) - 12} more)" if len(missing_invariants) > 12 else ""
        if missing_invariants:
            heading = (f"## ⚠ INCOMPLETE CONSTITUTION — {len(missing_invariants)} binding rule(s) "
                       f"did not fit the {budget:,}-char budget")
        else:
            heading = (f"## ⚠ MEMORY INDEX NOT LOADED — the constitution is complete, but the "
                       f"catalogue did not fit the {budget:,}-char budget")
        lines = [heading]
        if missing_invariants:
            lines.append(f"**RULES NOT LOADED:** {names}{more}")
            lines.append("Read them from `orchestrator/memory/` before acting on anything they "
                         "cover, and say so rather than guessing.")
        if index_dropped:
            lines.append("**The memory index did not load** — I cannot see what else exists. "
                         "Treat any 'I have no memory of that' as unreliable.")
        joined = "\n".join(lines) + "\n" + SEP + joined
        if missing_invariants:
            print(f"[session_start] CONSTITUTION INCOMPLETE: {len(missing_invariants)} invariant(s) "
                  f"omitted at {budget:,} chars ({used:,} used)"
                  + ("; index also dropped" if index_dropped else "")
                  + f". Missing: {', '.join(missing_invariants[:8])}", file=sys.stderr)
        else:
            print(f"[session_start] INDEX DROPPED at {budget:,} chars ({used:,} used): "
                  f"constitution COMPLETE ({n_inv_ok}/{n_inv_total}), but no room remained for the "
                  f"{index_stats.get('entries', 0)}-entry catalogue. Raise "
                  f"MS4CC_CONTEXT_BUDGET_CHARS — at this budget the agent cannot see what exists.",
                  file=sys.stderr)
    elif omitted:
        # The ordinary case: the constitution is whole, narrative was deferred.
        # Deliberately NOT phrased as a failure — this is the tiering working.
        joined = (
            f"## Context note — constitution complete, {len(omitted)} narrative item(s) deferred\n"
            f"All {n_inv_total} binding rules are loaded above. "
            f"{n_crit_ok} of {n_crit_total} full narratives fit; the rest live in "
            f"`orchestrator/memory/` and are retrievable by name or recall. "
            f"Nothing was cut mid-file.\n"
            + SEP + joined
        )
        print(f"[session_start] budget {used:,}/{budget:,} ({used/budget*100:.0f}%): "
              f"constitution COMPLETE ({n_inv_ok}/{n_inv_total} invariants); "
              f"{n_crit_ok}/{n_crit_total} narratives loaded, {len(omitted)} deferred.",
              file=sys.stderr)
    elif LAST_ASSEMBLY["over_budget"]:
        # Everything fitted only because required items are exempt.
        print(
            f"[session_start] BUDGET: required items alone exceed the budget "
            f"({used:,} > {budget:,}). Nothing dropped, but there is no headroom.",
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


def handoff_block(source: str) -> str:
    """Surface the handoff the previous session left in .handoff.md so continuity
    survives ANY context boundary — a compaction, a fresh relaunch (startup), or a
    --resume. Framing adapts to the source: a compaction produces a lossy summary,
    so "read this FIRST, it's more complete"; a deliberate (re)launch or resume is
    not lossy, so "resume from it IF you're continuing this thread, otherwise just
    register where things stood." Returns "" if no handoff exists.

    Why this fires on startup/resume too (2026-07-01, Clint): the handoff was
    originally compaction-only, but running in bypassPermissions mode means Clint
    now relaunches fresh routinely (the flag/default only re-applies on a fresh
    launch), and he exits+resumes often — both of which are startup/resume, not
    compact. Gating the handoff to compact alone silently dropped continuity on
    exactly those paths."""
    try:
        if not HANDOFF_PATH.exists():
            return ""
        content = HANDOFF_PATH.read_text().strip()
        if not content:
            return ""
        if source == "compact":
            intro = [
                "You just compacted. Your pre-compaction self wrote the handoff below so you can resume",
                "exactly where you left off. Read it FIRST and continue from it — it is more current and",
                f"more complete than the compaction summary. Full file: `{HANDOFF_PATH}`.",
            ]
        else:  # startup / resume — a deliberate (re)launch, not a lossy compaction
            intro = [
                "This is the handoff your previous session left behind. If you're continuing that work,",
                "read it FIRST and resume from it. If you're starting something unrelated, just register",
                f"where things stood and proceed — don't force it. Full file: `{HANDOFF_PATH}`.",
            ]
        return "\n".join([
            '<session-handoff priority="CRITICAL">',
            *intro,
            "",
            content,
            "</session-handoff>",
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

def check_mode() -> int:
    """`--check`: assemble, report, and EXIT NON-ZERO if anything did not fit.

    Deliberately separate from the live hook path. §4.3 of the design doc says the
    process should exit non-zero on an incomplete constitution — correct for CI, the
    checkpoint flow, and the fork gate, all of which run before an agent exists.

    It is WRONG for the live SessionStart hook. That path prints the context as JSON
    on stdout; a non-zero exit risks the harness discarding it, which would boot a
    session with NO context at all. That is not a louder version of a partial
    constitution, it is the categorically worse failure the design forbids — and the
    running agent could not even read the error, because the error is in the thing
    that failed to load. So: loud here, degraded-but-honest there.
    """
    if not IDENTITY_FILE.exists():
        print("[check] no IDENTITY.md — first-run install, nothing to verify.")
        return 0

    assemble_context(infer_active_projects(os.getcwd()))
    r = LAST_ASSEMBLY
    print(f"budget          {r['budget']:,} chars")
    print(f"used            {r['used']:,} ({r['utilisation']*100:.1f}%)")
    print(f"items           {r['items_admitted']}/{r['items_total']} admitted")
    print(f"CONSTITUTION    {r['invariants_admitted']}/{r['invariants_total']} binding rules  "
          f"<- must be total")
    print(f"index           {'present' if r['index_present'] else 'DROPPED'}")
    print(f"narratives      {r['bodies_admitted']}/{r['bodies_total']} full bodies  "
          f"(best-effort; deferred to disk + recall)")

    # Gate on the constitution, NOT on narrative coverage. Failing because a body
    # was deferred would fire on every healthy run at store scale and train
    # everyone to ignore it -- the alarm that cries wolf is worse than no alarm.
    if r["missing_invariants"]:
        print(f"\nFAIL — {len(r['missing_invariants'])} binding rule(s) did not reach context:")
        for name in r["missing_invariants"]:
            print(f"  - {name}")
        return 1
    if not r["index_present"]:
        print("\nFAIL — the memory index did not load; the agent cannot see what exists.")
        return 1
    if r["over_budget"]:
        print("\nFAIL — required items alone exceed the budget. Nothing dropped, no headroom.")
        return 1

    deferred = len(r["omitted"])
    print(f"\nOK — constitution complete and index present."
          + (f" {deferred} narrative item(s) deferred, which is the tiering working."
             if deferred else " Everything fit."))
    return 0


def minimal_context(err: Exception) -> str:
    """The floor. Identity, user, and every binding rule we can read off disk.

    Deliberately primitive: direct file reads, a line-scan for `invariant:`, no
    ranking, no weighting, no budget arithmetic, no index generator. Every one of
    those is a component that could have been what raised, so none of them may be
    on the recovery path. Each file is guarded individually, so one unreadable
    memory cannot take the fallback down the way it just took assembly down.

    The notice is IN-BAND because a diagnostic on stderr does not reach the model,
    and an agent running on a partial constitution needs to know it is.
    """
    parts = [
        "## ⚠ DEGRADED BOOT — context assembly failed\n"
        f"`{type(err).__name__}: {err}`\n\n"
        "Identity and the binding rules below were read directly from disk. "
        "**The memory index did not load, so I cannot see what else exists** — "
        "treat any 'I have no memory of that' as unreliable, and read "
        "`orchestrator/memory/` directly before acting on anything it might cover. "
        "Most likely a malformed frontmatter field in one memory file; "
        "`orchestrator/hooks/session_start.py --check` will name it."
    ]
    for label, path in (("IDENTITY (who I am)", IDENTITY_FILE), ("USER (who I'm working with)", USER_FILE)):
        try:
            if path.exists():
                parts.append(f"## {label}\n{path.read_text()}")
        except Exception:  # noqa: BLE001
            parts.append(f"## {label}\n_unreadable_")

    rules, unreadable = [], 0
    try:
        for p in sorted(MEMORY_DIR.glob("*.md")):
            try:
                text = p.read_text()
            except Exception:  # noqa: BLE001
                unreadable += 1
                continue
            # Line-scan rather than parse: the parser is a suspect too. But it
            # MUST understand block scalars — all 32 invariants on this store are
            # written `invariant: >` with the rule on following indented lines,
            # and the first version of this reader took only inline values and so
            # recovered ZERO rules from the only store it exists to protect. The
            # sandbox missed it because the fixture used a simpler shape than the
            # real files. Same defect Cairn hit in his harness the same night.
            lines = text.splitlines()
            for i, line in enumerate(lines[:80]):
                if not line.startswith("invariant:"):
                    continue
                rule = line.split(":", 1)[1].strip()
                if rule in (">", "|", ">-", "|-", ""):
                    folded = []
                    for cont in lines[i + 1:]:
                        if cont.strip() and not cont.startswith((" ", "\t")):
                            break            # next key, or the closing ---
                        if cont.strip():
                            folded.append(cont.strip())
                    rule = " ".join(folded)
                if rule:
                    rules.append(f"- **`{p.name}`** — {rule}")
                break
    except Exception:  # noqa: BLE001
        pass

    if rules:
        parts.append("## CONSTITUTION (binding rules — read directly from disk)\n"
                     + "\n".join(rules))
    parts.append(f"_Degraded boot: {len(rules)} binding rule(s) recovered"
                 + (f", {unreadable} file(s) unreadable" if unreadable else "") + "._")
    return "\n\n---\n\n".join(parts)


def _self_test() -> int:
    """Prove the crash-proofing and the degraded-boot floor actually work.

    `minimal_context()` is a safety net, and until now it had no test in-repo —
    only a sandbox run. An untested safety net is the thing this whole effort
    exists to remove, so it gets assertions like everything else.
    """
    checks: list[tuple[str, bool]] = []

    def ck(label, got, want=True):
        checks.append((label, got == want))

    # --- #71: frontmatter list coercion is total, and matches correctly.
    ck("a bare scalar becomes a one-element list", _as_str_list("threatgen"), ["threatgen"])
    ck("a real list passes through", _as_str_list(["a", "b"]), ["a", "b"])
    ck("a number does not raise", _as_str_list(5), ["5"])
    ck("absent is empty", _as_str_list(None), [])
    # CONTROL: the old `or []` iterated a bare string CHARACTER BY CHARACTER, so
    # `projects: threatgen` matched the project "t" and missed "threatgen".
    ck("CONTROL a bare string no longer matches a single character",
       any(p in {"t"} for p in _as_str_list("threatgen")), False)
    ck("CONTROL and it DOES match its own project",
       any(p in {"threatgen"} for p in _as_str_list("threatgen")))

    now = datetime.now(tz=timezone.utc)
    raised = []
    for v in (["a"], "a", 5, None, "", ("a", "b"), {"a": 1}, 3.5):
        try:
            weight({"projects": v, "hits": 1, "created": "2026-01-01"}, now, {"a"})
        except Exception as e:  # noqa: BLE001
            raised.append((v, type(e).__name__))
    ck("weight() survives every shape of `projects`", raised, [])

    # --- the degraded-boot floor.
    ctx = minimal_context(ValueError("simulated"))
    ck("degraded boot names the failure", "simulated" in ctx)
    ck("degraded boot carries the in-band warning", "DEGRADED BOOT" in ctx)
    ck("degraded boot says the index is missing", "cannot see what else exists" in ctx)
    ck("degraded boot recovers identity when it exists",
       ("## IDENTITY" in ctx) if IDENTITY_FILE.exists() else True)
    # Count the rules the store actually holds and require the floor to recover
    # EVERY one. "## CONSTITUTION is present" passed while zero rules were
    # recovered, because the heading is emitted from a non-empty list — and the
    # list was non-empty for a store with inline invariants and empty for this
    # one. Assert the number, not the heading.
    on_disk = sum(1 for p in MEMORY_DIR.glob("*.md")
                  if any(l.startswith("invariant:") for l in p.read_text(errors="ignore").splitlines()[:80]))
    recovered = ctx.count("\n- **`")
    ck(f"degraded boot recovers ALL {on_disk} binding rules from disk",
       recovered, on_disk)
    # CONTROL: block scalars are the real shape here, so a reader that only
    # handles inline values must fail this, not pass it vacuously.
    ck("CONTROL the store really does use block scalars (so this is not vacuous)",
       on_disk > 0 and any(
           p.read_text(errors="ignore").find("invariant: >") >= 0
           for p in MEMORY_DIR.glob("*.md")))
    # CONTROL: it must be materially smaller than a healthy assembly, or it is
    # not a floor — it is the same code path wearing a different label.
    try:
        full = assemble_context(infer_active_projects(os.getcwd()))
        ck("CONTROL the floor is smaller than a healthy assembly", len(ctx) < len(full))
    except Exception:  # noqa: BLE001
        ck("CONTROL healthy assembly available for comparison", False)

    failed = [l for l, ok in checks if not ok]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    print()
    if failed:
        print(f"SELF-TEST FAILED — {len(failed)} assertion(s).")
        return 1
    print(f"SELF-TEST PASSED — {len(checks)} assertions covering frontmatter "
          f"coercion and the degraded-boot floor.")
    return 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    if "--check" in sys.argv:
        sys.exit(check_mode())
    hook_input = read_hook_input()
    source = str(hook_input.get("source") or hook_input.get("matcher") or "").lower()
    cwd = os.getcwd()
    active_projects = infer_active_projects(cwd)

    if not IDENTITY_FILE.exists():
        context = first_run_invitation()
    else:
        try:
            context = assemble_context(active_projects)
        except Exception as e:  # noqa: BLE001
            # LAST-RESORT GUARD. Assembly must never take the session to zero.
            #
            # Memory files are hand-edited by design, so a malformed one is a
            # normal input, not an exceptional one. Twice now a single bad field
            # has raised out of assembly and made the hook emit NOTHING — no
            # identity, no user, no constitution — which is silent from inside
            # the session, because a hook that prints nothing is indistinguishable
            # from a hook with nothing to add:
            #
            #   #71  weight() raised TypeError on a bare-scalar `projects`
            #   #95  memory_index built a sort key with chr(255 - ord(c)), which
            #        goes negative on any non-ASCII char — one en-dash in one
            #        date zeroed the entire injection
            #
            # Both were one-line bugs; the CLASS is what needs the guard. Cairn
            # argued this from my own words and he is right: a partial catalogue
            # with an honest count beats nothing at all, and that reasoning holds
            # for the whole injection, not just the index.
            #
            # So: fall back to identity + user + a directly-read constitution,
            # bypassing ranking, weighting and the index generator entirely —
            # every component that could have been the thing that raised.
            print(f"[session_start] ASSEMBLY FAILED ({type(e).__name__}: {e}) — "
                  f"falling back to identity + constitution only", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            context = minimal_context(e)

    # Wrap in a clear tag so the model sees this as orchestrator context.
    wrapped = f"<orchestrator-context>\n{context}\n</orchestrator-context>"

    # Prepend the previous session's handoff so continuity survives the context
    # boundary — whether that's a compaction (lossy summary), a fresh relaunch
    # (startup), or a --resume. All three benefit from the pre-boundary self's
    # "first action on resume" pointer; only the framing differs (see handoff_block).
    # `clear` is intentionally excluded: /clear means "give me a clean slate."
    if source in ("compact", "resume", "startup"):
        handoff = handoff_block(source)
        if handoff:
            wrapped = handoff + "\n\n" + wrapped

    # Deferred "embed after compact" stays COMPACT-ONLY: it re-embeds the archived
    # pre-compaction transcript (post-compaction the live JSONL is the lossy summary,
    # so the archive is the only full copy). On startup/resume the normal
    # /checkpoint + session_end path owns embedding, so kicking it here would be
    # redundant work on the critical path.
    if source == "compact":
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
