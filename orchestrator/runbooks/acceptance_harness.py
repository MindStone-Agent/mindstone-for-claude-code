#!/usr/bin/env python3
"""Acceptance harness for the memory system — design doc §8.

Proves the four properties the tiering was built to guarantee, against the REAL
store at the REAL budget. Everything else in this repo tests a component; this
tests the promise.

    A1  every binding rule reaches context — and if one does not, context is still
        ASSEMBLED, an in-band notice names what is missing, and the process reports
        failure rather than booting silently degraded
    A2  zero mid-file truncations. A rule that stops halfway reads as complete and
        is worse than one plainly absent
    A3  the reported count equals the count that actually loaded. Counting before
        the cut reports every rule as present while most were dropped
    A4  a rule chosen AT RANDOM can be retrieved from the store by searching for it
        — WITH a control that must NOT match
    A5  the memory index rations DESCRIPTIONS under pressure and never EXISTENCE:
        every memory stays named even when the catalogue cannot hold its summaries

A4's control is the point of the whole file. A harness fed only cases that should
succeed looks perfect exactly when it is blind, and this repo has produced nine
false-clean results in a single day from checks that could not fail. So A4 asserts
both that signal is found AND that noise is rejected, and reports the margin between
them rather than a bare pass.

Usage:
    python3 acceptance_harness.py                  # full run
    python3 acceptance_harness.py --samples 12     # more random rules
    python3 acceptance_harness.py --self-test      # prove the assertions can fail
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = ORCHESTRATOR_DIR / "memory"
sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))

SEED = 20260827          # deterministic: an unreproducible run is an anecdote
NONSENSE = [
    "zzqx plorb frimble kumquat manifold",
    "vresh gorlum tannic ferrite oscillation",
    "blint waxen pergola quantise trundle",
    "myrrh cadastral folium bezoar switching",
]


class Result:
    def __init__(self):
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, label: str, passed: bool, detail: str = ""):
        print(f"  {'PASS' if passed else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
        if not passed:
            self.failures.append(label)


def assemble(budget: int | None = None):
    import session_start as ss
    if budget is not None:
        os.environ["MS4CC_CONTEXT_BUDGET_CHARS"] = str(budget)
    else:
        os.environ.pop("MS4CC_CONTEXT_BUDGET_CHARS", None)
    ctx = ss.assemble_context(ss.infer_active_projects(os.getcwd()))
    return ctx, dict(ss.LAST_ASSEMBLY)


def a1_a2_a3(r: Result, budget: int | None):
    import session_start as ss
    ctx, rep = assemble(budget)
    label = f"@{rep['budget']:,}"

    # A1 — every binding rule present, or a loud, assembled, honest failure.
    if rep["constitution_complete"]:
        r.check(f"A1 {label} every binding rule reached context",
                rep["invariants_admitted"] == rep["invariants_total"],
                f"{rep['invariants_admitted']}/{rep['invariants_total']}")
    else:
        # The degraded path must still be SAFE: context assembled, notice present.
        r.check(f"A1 {label} degraded run still assembled context", len(ctx) > 1000,
                f"{len(ctx):,} chars")
        r.check(f"A1 {label} in-band notice names the missing rules",
                "INCOMPLETE CONSTITUTION" in ctx and "RULES NOT LOADED" in ctx)
        r.check(f"A1 {label} identity survived via the required exemption",
                "## IDENTITY" in ctx)

    # A2 and A3 test STRUCTURE, not strings.
    #
    # A first version scraped the whole block for "truncated for token budget" and
    # counted every "\n- **`" bullet. Both failed on a healthy run: the phrase
    # appears inside injected MEMORY CONTENT (a memory about truncation), and two
    # memory bodies use the same bullet format. The detectors were measuring the
    # string, not the property — the same mistake that made a before/after test
    # report a working feature as a regression earlier today.
    #
    # So: derive the expected text from the store and assert on THAT.
    crit = [(p, fm) for p, fm, _b in ss.load_memory_files()
            if fm.get("critical") and fm.get("type") not in ("index", "log", "roadmap")]
    invariants = {p.name: (fm.get("invariant") or "").strip() for p, fm in crit}
    admitted_names = {n for n, inv in invariants.items()
                      if inv and f"- **`{n}`** — {inv}" in ctx}

    # A2 — every admitted invariant appears IN FULL. Per-item admission makes a
    # partial rule structurally impossible; this asserts the guarantee rather than
    # trusting it. A rule present but truncated is the failure being ruled out.
    partial = [n for n, inv in invariants.items()
               if inv and f"- **`{n}`**" in ctx and f"- **`{n}`** — {inv}" not in ctx]
    r.check(f"A2 {label} no invariant rendered partially", not partial,
            f"{len(admitted_names)} rendered in full"
            + (f"; PARTIAL: {', '.join(partial[:3])}" if partial else ""))

    # A3 — the report must describe what is actually in the block. Compare the
    # claimed count against invariants verifiably present, by exact text.
    r.check(f"A3 {label} reported invariant count == verified-present count",
            len(admitted_names) == rep["invariants_admitted"],
            f"reported {rep['invariants_admitted']}, verified {len(admitted_names)}")

    # And the index claim must match reality.
    r.check(f"A3 {label} index_present matches the block",
            rep["index_present"] == ("MEMORY INDEX" in ctx or "index fallback" in ctx.lower()))
    return rep


def a5_index_degrades(r: Result):
    """A5 — the catalogue rations descriptions, never existence.

    The index is the only tier whose cost scales with memory COUNT, so it is the
    one guaranteed to hit its ceiling as the store grows. When it does, the
    contract is that every memory is still NAMED and only the description is
    dropped: a name is something an agent can search for and read on demand, an
    absent entry is invisible.

    Tested at a budget squeezed hard enough to force the degradation but not so
    hard that the index cannot be admitted at all — and paired with a control
    asserting the degradation genuinely happened, because "all names present"
    passes trivially on a run where nothing was rationed.
    """
    import session_start as ss

    names = [p.name for p, fm, _b in ss.load_memory_files()
             if fm.get("type") not in ("index", "log", "roadmap")]

    ctx_full, rep_full = assemble(None)
    r.check("A5 @budget every memory is catalogued",
            rep_full["index_entries"] == len(names),
            f"{rep_full['index_entries']}/{len(names)} entries, "
            f"{rep_full['index_full']} with descriptions")
    r.check("A5 @budget the index stays inside its allocation",
            not rep_full["index_over_allocation"], f"{rep_full['index_chars']:,} chars")
    # The hard failure: existence itself rationed. Fine to survive (it is reported
    # in-band), but it must never happen silently on a healthy install.
    r.check("A5 @budget no memory is missing from the catalogue entirely",
            rep_full["index_unlisted"] == 0,
            f"{rep_full['index_unlisted']} unlisted"
            if rep_full["index_unlisted"] else "all listed")

    # Squeeze until the index rations. Walk down rather than hard-coding one
    # number: the threshold moves with the store, and a fixed budget that stops
    # forcing degradation would silently turn this into a no-op.
    squeezed = None
    for budget in (34_000, 32_000, 30_000, 28_000):
        _ctx, rep = assemble(budget)
        if rep["index_present"] and rep["index_name_only"] > 0:
            squeezed = (budget, _ctx, rep)
            break

    if squeezed is None:
        r.check("A5 a squeezed budget forces the index to ration", False,
                "no tested budget produced a rationed-but-present index — "
                "the degradation path went untested")
        assemble(None)
        return

    budget, ctx, rep = squeezed
    missing = [n for n in names if f"`{n}`" not in ctx]
    r.check(f"A5 @{budget:,} EVERY memory still named when descriptions are rationed",
            not missing,
            f"{len(names) - len(missing)}/{len(names)} named; "
            f"{rep['index_full']} full, {rep['index_name_only']} name-only"
            + (f"; MISSING {', '.join(missing[:3])}" if missing else ""))
    # CONTROL: prove the run actually degraded. Without this, a budget that
    # happened to fit everything would pass the check above for the wrong reason.
    r.check(f"A5 CONTROL @{budget:,} the run really did ration descriptions",
            rep["index_name_only"] > 0 and rep["index_full"] < rep["index_entries"],
            f"{rep['index_name_only']} of {rep['index_entries']} reduced to name-only")
    r.notes.append(
        f"index rations at ~{budget:,} chars: {rep['index_full']} full + "
        f"{rep['index_name_only']} name-only, all {rep['index_entries']} still listed")
    if rep_full.get("index_signal_saturated"):
        r.notes.append("last_applied is SATURATED — recency cannot order the index on this "
                       "store; ordering falls through to prevented/hits. See #91.")
    assemble(None)


def a4_retrieval(r: Result, samples: int):
    """A random rule must be findable by search, and nonsense must not outscore it."""
    try:
        from recall import recall
        import session_start as ss
    except Exception as e:
        r.check("A4 retrieval available", False, str(e))
        return

    crit = [(p, fm) for p, fm, _b in ss.load_memory_files()
            if fm.get("critical") and (fm.get("invariant") or "").strip()]
    if not crit:
        r.check("A4 there are invariants to sample", False)
        return

    rng = random.Random(SEED)
    chosen = rng.sample(crit, min(samples, len(crit)))

    signal, misses = [], []
    for path, fm in chosen:
        # Query with the INVARIANT — what an agent actually holds and would search
        # from. Not the description, which is inside the indexed text and would make
        # this close to a string match.
        hits = recall((fm.get("invariant") or "")[:300], k=6, source_types=["memory"], mmr=False)
        top = next((h for h in hits if path.name in str(h.get("source_path", ""))), None)
        if top is None:
            misses.append(path.name)
        else:
            signal.append(float(top["similarity"]))

    noise = []
    for q in NONSENSE:
        hits = recall(q, k=6, source_types=["memory"], mmr=False)
        if hits:
            noise.append(max(float(h["similarity"]) for h in hits))

    r.check(f"A4 random rules retrievable from their own invariant",
            not misses, f"{len(signal)}/{len(chosen)} found"
            + (f"; MISSED {', '.join(misses[:3])}" if misses else ""))

    if signal and noise:
        margin = min(signal) - max(noise)
        r.check("A4 CONTROL — nonsense scores below the weakest real match",
                margin > 0,
                f"signal min {min(signal):.4f}, noise max {max(noise):.4f}, margin {margin:+.4f}")
        r.notes.append(
            f"retrieval margin {margin:+.4f} (signal {min(signal):.3f}–{max(signal):.3f}, "
            f"noise {min(noise):.3f}–{max(noise):.3f})")
        if 0 < margin < 0.02:
            r.notes.append("margin is THIN — recall must not be the only path to a rule "
                           "on this store; the constitution tier is what carries it.")
    else:
        r.check("A4 CONTROL produced comparable bands", False,
                f"signal={len(signal)} noise={len(noise)}")


def self_test() -> int:
    """Prove the assertions can FAIL. A harness that only ever passes proves nothing."""
    print("self-test — the assertions must fire on known-bad input:")
    fails = 0

    inv = {"a.md": "never do the bad thing", "b.md": "always do the good thing"}
    whole = "## CONSTITUTION\n" + "\n".join(f"- **`{n}`** — {t}" for n, t in inv.items())
    # b.md present but its rule cut short — the exact failure A2 must catch.
    cut = "## CONSTITUTION\n- **`a.md`** — never do the bad thing\n- **`b.md`** — always do"

    def partial_in(text):
        return [n for n, t in inv.items()
                if f"- **`{n}`**" in text and f"- **`{n}`** — {t}" not in text]

    def present_in(text):
        return {n for n, t in inv.items() if f"- **`{n}`** — {t}" in text}

    checks = [
        ("A2 catches a partially-rendered invariant", partial_in(cut) == ["b.md"], True),
        ("A2 passes when all render in full", partial_in(whole) == [], True),
        ("A3 counts only verifiably-present invariants", present_in(cut) == {"a.md"}, True),
        ("A3 counts all when whole", present_in(whole) == {"a.md", "b.md"}, True),
        ("A2/A3 ignore look-alike text in memory CONTENT",
         partial_in(whole + "\n\nsome memory body mentioning - **`c.md`** — and truncated for token budget") == [], True),
    ]
    for label, got, expected in checks:
        ok = got == expected
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"SELF-TEST FAILED — {fails} assertion(s) cannot distinguish good from bad.")
        return 1
    print("SELF-TEST PASSED — assertions fire on known-bad input, so a green run is meaningful.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=8, help="random rules to test for retrieval")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if self_test() != 0:
        return 2
    print()

    r = Result()
    print("=== A1-A3 at the configured budget ===")
    rep = a1_a2_a3(r, None)
    print(f"\n=== A1-A3 under a DELIBERATELY tiny budget (the degraded path must be safe) ===")
    a1_a2_a3(r, 20_000)
    print(f"\n=== A4 retrieval, {args.samples} random rules + {len(NONSENSE)} nonsense controls ===")
    a4_retrieval(r, args.samples)
    print(f"\n=== A5 the index rations descriptions, never existence ===")
    a5_index_degrades(r)

    print("\n" + "=" * 72)
    for n in r.notes:
        print(f"  note: {n}")
    if r.failures:
        print(f"\nACCEPTANCE FAILED — {len(r.failures)} check(s):")
        for f in r.failures:
            print(f"  - {f}")
        return 1
    print("\nACCEPTANCE PASSED — every binding rule reaches context, nothing is cut "
          "mid-file, the report matches the block, and rules are retrievable with "
          "noise measurably below signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
