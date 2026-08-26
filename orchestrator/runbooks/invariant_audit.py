#!/usr/bin/env python3
"""Audit `invariant:` coverage and quality, and project the injected budget.

WHY THIS EXISTS
---------------
Splitting a memory into an always-injected `invariant:` and a recalled narrative
only helps if the invariant actually states the rule. A field that exists but says
"The rule (2026-07-29):" is worse than no field: it reports as covered, injects as
present, and reads as complete while the rule itself never arrives.

So coverage is necessary and not sufficient, and this audits both.

WHY THE FIELD IS AUTHORED AND NOT EXTRACTED
-------------------------------------------
A heuristic extractor cannot be graded on its tail. Any predicate written to detect
a bad extraction is the same predicate the extractor optimises against, so grader
and candidate share a blind spot and the residue is invisible by construction.
Measured across 51 criticals on a 292-file store, "first prose paragraph" left the
rule PROVABLY ABSENT in 10 — nine of them ending in a dangling colon.

An explicit field is the only version whose failures are countable: it is present
or it is not, and this script says which. That is the whole argument for the
migration cost.

DEGENERACY CHECKS
-----------------
An invariant is degenerate when it cannot be obeyed on its own:
  - ends in a colon                 -> a lead-in; the rule is in a block that will
                                       not be injected
  - too short                       -> a label, not a rule
  - attribution-only                -> "**Name, 2026-07-29:**"
  - narrative-dependent             -> "as we saw", "this incident", "the above"
  - identical to `description`      -> adds nothing over the index line

Usage:
    python3 invariant_audit.py                 # audit + budget projection
    python3 invariant_audit.py --missing       # just the names still needing one
    python3 invariant_audit.py --self-test     # prove the detectors fire
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))

MIN_INVARIANT_CHARS = 40
MAX_INVARIANT_CHARS = 600

NARRATIVE_DEPS = re.compile(
    r"\b(as we saw|as above|as below|this incident|that incident|the above|the below|"
    r"see above|see below|described above|earlier today|last night)\b", re.I)
ATTRIBUTION_ONLY = re.compile(r"^\W*\*{0,2}[A-Z][a-z]+,?\s*\d{4}-\d{2}-\d{2}\*{0,2}\W*$")


def degeneracy(inv: str, description: str = "") -> list[str]:
    """Return reasons this invariant cannot be obeyed standalone. Empty = usable."""
    reasons = []
    s = (inv or "").strip()
    if not s:
        return ["absent"]
    if s.endswith(":"):
        reasons.append("ends in a colon (lead-in; the rule is in a block that will not inject)")
    if len(s) < MIN_INVARIANT_CHARS:
        reasons.append(f"too short ({len(s)} < {MIN_INVARIANT_CHARS} chars) — a label, not a rule")
    if len(s) > MAX_INVARIANT_CHARS:
        reasons.append(f"too long ({len(s)} > {MAX_INVARIANT_CHARS} chars) — that is narrative")
    if ATTRIBUTION_ONLY.match(s):
        reasons.append("attribution only — names a source, states no rule")
    if NARRATIVE_DEPS.search(s):
        reasons.append("depends on narrative not being injected ('as we saw', 'the above', ...)")
    if description and s.strip().lower() == description.strip().lower():
        reasons.append("identical to `description` — adds nothing over the index line")
    return reasons


def audit():
    from session_start import load_memory_files, context_budget_chars
    mems = load_memory_files()
    crit = [(p, fm, b) for p, fm, b in mems
            if fm.get("critical") and fm.get("type") not in ("index", "log", "roadmap")]

    ok, degen, missing = [], [], []
    for p, fm, body in crit:
        inv = (fm.get("invariant") or "").strip()
        if not inv:
            missing.append((p, fm, body)); continue
        r = degeneracy(inv, fm.get("description", ""))
        (degen if r else ok).append((p, fm, body, r))

    budget = context_budget_chars()
    print(f"store: {len(mems)} memories · {len(crit)} critical · budget {budget:,} chars\n")
    print(f"  invariant present + usable : {len(ok)}/{len(crit)}")
    print(f"  invariant present + DEGENERATE : {len(degen)}/{len(crit)}")
    print(f"  invariant MISSING : {len(missing)}/{len(crit)}")

    if degen:
        print("\nDEGENERATE (present but not obeyable standalone):")
        for p, fm, _b, reasons in degen:
            print(f"  {p.name}")
            for r in reasons:
                print(f"      - {r}")

    # ---- budget projection ------------------------------------------------
    full_total = sum(len(b) for _, _, b in crit)
    inv_total = sum(len((fm.get("invariant") or "").strip()) + len(p.name) + 8
                    for p, fm, _ in crit if (fm.get("invariant") or "").strip())
    # Files still lacking one would have to inject full text.
    unmigrated = sum(len(b) for p, fm, b in crit if not (fm.get("invariant") or "").strip())

    print("\nBUDGET PROJECTION (critical tier only)")
    print(f"  full text, all criticals            {full_total:>9,} chars  ({full_total/budget*100:>5.0f}% of budget)")
    print(f"  invariants for migrated files       {inv_total:>9,} chars")
    print(f"  full text still required (unmigrated){unmigrated:>8,} chars")
    projected = inv_total + unmigrated
    print(f"  projected critical tier             {projected:>9,} chars  ({projected/budget*100:>5.0f}% of budget)")
    if full_total:
        print(f"  reduction vs full text              {100 - projected/full_total*100:>9.1f}%")

    rc = 1 if (missing or degen) else 0
    if rc:
        print(f"\nINCOMPLETE — {len(missing)} missing, {len(degen)} degenerate. "
              f"Run with --missing for the worklist.")
    else:
        print("\nOK — every critical memory carries a usable invariant.")
    return rc, missing


def self_test() -> int:
    """Prove each degeneracy detector fires. Positive control both directions."""
    cases = [
        ("ends in a colon",      "The rule (2026-07-29), stated plainly for the record:", True),
        ("too short",            "Be careful.",                                            True),
        ("attribution only",     "**Clint, 2026-08-12**",                                  True),
        ("narrative-dependent",  "Never do that again, for the reasons described above in this incident.", True),
        ("absent",               "",                                                       True),
        ("GOOD — obeyable",      "Never restart a family agent's gateway without explicit "
                                 "approval; stage the change and flag that a restart is needed.", False),
    ]
    failures = 0
    print("self-test — degeneracy detectors:")
    for label, text, should_flag in cases:
        flagged = bool(degeneracy(text))
        ok = flagged == should_flag
        print(f"  {label:<22} flagged={flagged!s:<5} expected={should_flag!s:<5} {'ok' if ok else '*** WRONG ***'}")
        if not ok:
            failures += 1

    # identical-to-description needs its own case (two-arg form)
    d = "Never restart a gateway without approval."
    flagged = any("identical" in r for r in degeneracy(d, d))
    print(f"  {'identical to desc':<22} flagged={flagged!s:<5} expected=True  {'ok' if flagged else '*** WRONG ***'}")
    if not flagged:
        failures += 1

    print()
    if failures:
        print(f"SELF-TEST FAILED — {failures} detector(s) misbehaved; an audit from this build means nothing.")
        return 1
    print("SELF-TEST PASSED — detectors fire on known-bad input, so a clean audit is meaningful.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--missing", action="store_true", help="print only the files still needing an invariant")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if self_test() != 0:
        return 2
    print()

    rc, missing = audit()
    if args.missing:
        print("\nWORKLIST — need an authored invariant:")
        for p, fm, body in sorted(missing, key=lambda t: -len(t[2])):
            print(f"  {len(body):>7,}  {p.name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
