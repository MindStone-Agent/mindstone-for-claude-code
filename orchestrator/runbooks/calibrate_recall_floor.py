#!/usr/bin/env python3
"""Calibrate the semantic-recall similarity floor against a measured noise band.

WHY THIS EXISTS
---------------
`user_prompt_submit.py` filters recall results with a hard-coded `MIN_SIMILARITY`.
A threshold that was never measured against the corpus it filters is decoration: it
reads, in config and in review, as a solved problem while admitting everything.

Vector search has no natural zero. Ask it a question with no answer and it returns
the nearest neighbours anyway, with confident-looking scores. The only defence is a
floor placed above the score that *unanswerable* queries actually achieve — and that
number is a property of a specific store, so it must be measured per store, not
inherited from another install.

WHAT IT MEASURES
----------------
Four query classes. The two negative classes are the point.

  verbatim      sentences lifted from memory bodies. Upper bound. INFLATED by
                construction — the text is literally in the index. Reported for
                scale, never used to set the floor.
  descriptive   each memory's `description:` frontmatter, targeting that memory.
                The closest available stand-in for a real user prompt on a topic
                the store genuinely covers.
  gibberish     random token salad. The EASY negative.
  absent        fluent, grammatical English about subjects the store does not
                contain. The HARD negative, and the one that decides the floor.

`absent` matters more than `gibberish`. Embeddings place coherent text near other
coherent text, so a well-formed question about marine biology scores higher against
a devops corpus than `zzqx kumquat manifold` does. Calibrating on gibberish alone
sets the floor beneath the real failure mode and certifies it as safe.

WHAT IT DECIDES
---------------
A floor is safe only if it sits above every negative score and below the positive
scores it must not suppress. If those bands overlap there is NO safe floor, and that
is a finding about the store — reported, not tuned away. Exits non-zero so a caller
can gate on it.

A NOTE ON TRUSTING THIS SCRIPT
------------------------------
Its output became `MIN_SIMILARITY = 0.50`, and for a while it had no test of any
kind — a calibration tool that could not itself be calibrated. `decide()` is now
pure and `--self-test` exercises it on synthetic bands with known answers,
including the case that must never read as clean: overlapping bands. The
self-test also gates a real run, so a green calibration cannot come from broken
verdict logic.

Usage:
    python3 calibrate_recall_floor.py
    python3 calibrate_recall_floor.py --k 6 --json report.json
    python3 calibrate_recall_floor.py --self-test
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))

MEMORY_DIR = ORCHESTRATOR_DIR / "memory"

# Deterministic: same store + same seed => same report. A calibration you cannot
# reproduce is an anecdote.
SEED = 20260826

# Fluent English on subjects a technical operations store has no reason to hold.
# These are the hard negatives. Kept deliberately mundane and generic so the set is
# portable to any install and carries no local detail.
ABSENT_TOPIC_QUERIES = [
    "What is the correct water temperature for proofing bread dough?",
    "How do humpback whales coordinate bubble-net feeding?",
    "Which pigments were most common in Renaissance fresco painting?",
    "What causes the seasonal migration of monarch butterflies?",
    "How is a violin bow rehaired and what horsehair is preferred?",
    "What are the rules for castling in chess and when is it illegal?",
    "How does a diesel-electric locomotive transmit power to the wheels?",
    "What distinguishes a stratovolcano from a shield volcano?",
    "How do you prune an apple tree for an open-centre shape?",
    "What is the offside rule in association football?",
    "Which grape varieties are permitted in traditional Chianti?",
    "How did medieval scribes prepare vellum for manuscript work?",
]

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")

POSITIVE_CLASSES = ("verbatim", "descriptive")
NEGATIVE_CLASSES = ("gibberish", "absent")


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm: dict = {}
    key = None
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            fm[key] = val
        elif key and line.startswith(("  ", "\t")) and fm.get(key) in ("", ">", "|", ">-", "|-"):
            # folded/literal block scalar continuation
            fm[key] = (fm[key] if fm[key] not in (">", "|", ">-", "|-") else "") + " " + line.strip()
    return fm, text[end + 4:]


def load_memories() -> list[tuple[Path, dict, str]]:
    out = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, body = _frontmatter(raw)
        out.append((p, fm, body))
    return out


def build_queries(mems, rng, n_gibberish: int):
    """Return {class_name: [(query, expected_path_or_None), ...]}."""
    verbatim, descriptive = [], []

    for path, fm, body in mems:
        desc = (fm.get("description") or "").strip()
        if len(desc) >= 40:
            descriptive.append((desc, path))

        # A distinctive prose sentence from the body: long enough to be specific,
        # not a heading, not a code/table line.
        for sent in re.split(r"(?<=[.!?])\s+", body):
            s = " ".join(sent.split())
            if (
                80 <= len(s) <= 240
                and not s.startswith(("#", "-", "*", "|", ">", "`"))
                and "```" not in s
            ):
                verbatim.append((s, path))
                break

    # Gibberish: real morphology, no corpus meaning. Built from a fixed syllable
    # inventory rather than sampling the corpus, so it cannot accidentally hit.
    syll = ["zqx", "vru", "plog", "meth", "kar", "dwin", "farn", "quilb", "trex",
            "nulk", "shiv", "gorm", "yalt", "brint", "occ", "vesh", "lunt", "krad"]
    gibberish = []
    for _ in range(n_gibberish):
        n = rng.randint(4, 8)
        q = " ".join("".join(rng.choice(syll) for _ in range(rng.randint(1, 2)))
                     for _ in range(n))
        gibberish.append((q, None))

    absent = [(q, None) for q in ABSENT_TOPIC_QUERIES]

    return {
        "verbatim": verbatim,
        "descriptive": descriptive,
        "gibberish": gibberish,
        "absent": absent,
    }


def score_class(recall_fn, queries, k: int, positive: bool):
    """For positives: score of the CORRECT file's best chunk (0.0 if not retrieved).
    For negatives: the single highest score returned — the false-positive ceiling."""
    scores, misses = [], 0
    for q, expected in queries:
        try:
            res = recall_fn(q, k=k, source_types=["memory"], mmr=False)
        except Exception as e:  # noqa: BLE001 — a broken store must not look like a clean run
            print(f"  ! recall failed on {q[:50]!r}: {e}", file=sys.stderr)
            continue
        if not res:
            if positive:
                misses += 1
                scores.append(0.0)
            continue
        if positive:
            want = str(expected)
            hit = next((r for r in res if str(r.get("source_path", "")).endswith(Path(want).name)), None)
            if hit is None:
                misses += 1
                scores.append(0.0)
            else:
                scores.append(float(hit["similarity"]))
        else:
            scores.append(max(float(r["similarity"]) for r in res))
    return scores, misses


def band(scores):
    live = [s for s in scores if s > 0]
    if not live:
        return None
    return {
        "n": len(live),
        "min": min(live),
        "p05": statistics.quantiles(live, n=20)[0] if len(live) >= 20 else min(live),
        "median": statistics.median(live),
        "p95": statistics.quantiles(live, n=20)[18] if len(live) >= 20 else max(live),
        "max": max(live),
    }


MARGINAL_REL = 0.05          # <5% of the observed score range = not separation


def decide(bands: dict) -> dict | None:
    """Turn measured bands into a verdict. PURE — no printing, no I/O.

    Extracted from main() so it can be exercised on synthetic bands. It was
    previously inline, which meant the single most consequential piece of logic
    in this repo's calibration path — the one whose output became
    MIN_SIMILARITY = 0.50 — had no test of any kind. A calibration tool that
    cannot be calibrated is the same species of problem as a check that cannot
    fail, and this one had already been acted on.

    Returns None when a required band is empty (INCONCLUSIVE).
    """
    if not bands.get("absent") or not any(bands.get(c) for c in POSITIVE_CLASSES):
        return None

    # The floor must clear EVERY negative and suppress NO positive, so it is
    # bounded by the worst case on each side — the highest false positive and the
    # WEAKEST true positive, across all classes.
    #
    # An earlier version set the ceiling from `descriptive` alone, on the
    # reasoning that `verbatim` was inflated. That was backwards: verbatim scores
    # LOWER (a lone sentence must match its own file against 500+ competing
    # chunks, where a description is a dense summary of the whole file), so
    # excluding it discarded the hardest positive case and produced a floor that
    # would have suppressed real matches. Choosing which positives to measure
    # against is how a harness flatters itself.
    neg = {c: b for c, b in bands.items() if c in NEGATIVE_CLASSES and b}
    neg_ceiling = max(b["max"] for b in neg.values())
    neg_min = min(b["min"] for b in neg.values())
    pos_floor, binding_class = min(
        ((bands[c]["min"], c) for c in POSITIVE_CLASSES if bands.get(c)), key=lambda t: t[0])
    margin = pos_floor - neg_ceiling

    span = (max(b["max"] for b in bands.values() if b)
            - min(b["min"] for b in bands.values() if b))
    rel = margin / span if span else 0.0

    # A positive margin is not the same as a usable one. The score range here
    # spans roughly 0.48-0.77, so a margin of a few thousandths is a rounding
    # artefact dressed as a decision. Three outcomes, not two.
    if margin <= 0:
        verdict, rc = "no_safe_floor", 1
    elif rel < MARGINAL_REL:
        verdict, rc = "marginal", 1
    else:
        verdict, rc = "viable", 0

    return {
        "neg_ceiling": neg_ceiling, "neg_min": neg_min,
        "pos_floor": pos_floor, "binding_class": binding_class,
        "margin": margin, "span": span, "rel": rel,
        "recommended": neg_ceiling + margin * 0.5,
        "verdict": verdict, "rc": rc,
    }


def _self_test() -> int:
    """Prove the verdict logic discriminates. Synthetic bands, known answers."""
    def B(lo, hi):
        return {"n": 10, "min": lo, "p05": lo, "median": (lo + hi) / 2, "p95": hi, "max": hi}

    checks: list[tuple[str, bool]] = []

    def ck(label, got, want=True):
        checks.append((label, got == want))

    # --- clean separation -> viable
    d = decide({"verbatim": B(0.70, 0.80), "descriptive": B(0.75, 0.85),
                "gibberish": B(0.30, 0.40), "absent": B(0.35, 0.45)})
    ck("well-separated bands -> viable", d["verdict"], "viable")
    ck("  recommended floor sits between the bands",
       d["neg_ceiling"] < d["recommended"] < d["pos_floor"])
    ck("  exit code is success", d["rc"], 0)

    # --- OVERLAP -> no safe floor. The case that must never read as clean.
    d = decide({"verbatim": B(0.40, 0.80), "descriptive": B(0.75, 0.85),
                "gibberish": B(0.30, 0.40), "absent": B(0.35, 0.55)})
    ck("overlapping bands -> NO SAFE FLOOR", d["verdict"], "no_safe_floor")
    ck("  margin is negative", d["margin"] < 0)
    ck("  exit code is failure", d["rc"], 1)

    # --- technically disjoint, practically not -> marginal
    d = decide({"verbatim": B(0.5010, 0.80), "descriptive": B(0.75, 0.85),
                "gibberish": B(0.30, 0.40), "absent": B(0.35, 0.50)})
    ck("a hair's-breadth gap -> MARGINAL, not viable", d["verdict"], "marginal")
    ck("  and it does NOT report success", d["rc"], 1)

    # --- the binding positive must be the WEAKEST class, not a chosen one.
    d = decide({"verbatim": B(0.60, 0.70), "descriptive": B(0.80, 0.90),
                "gibberish": B(0.30, 0.40), "absent": B(0.35, 0.45)})
    ck("the weakest positive class binds the floor", d["binding_class"], "verbatim")
    ck("  the floor is NOT taken from the flattering class", d["pos_floor"], 0.60)

    # --- the hardest negative must set the ceiling.
    d = decide({"verbatim": B(0.70, 0.80), "descriptive": B(0.75, 0.85),
                "gibberish": B(0.30, 0.40), "absent": B(0.35, 0.58)})
    ck("the strongest false positive sets the ceiling", d["neg_ceiling"], 0.58)

    # --- INCONCLUSIVE rather than a confident number from missing data.
    ck("an empty negative band is inconclusive, not viable",
       decide({"verbatim": B(0.7, 0.8), "descriptive": B(0.7, 0.8),
               "gibberish": None, "absent": None}), None)
    ck("an empty positive band is inconclusive, not viable",
       decide({"verbatim": None, "descriptive": None,
               "gibberish": B(0.3, 0.4), "absent": B(0.3, 0.4)}), None)

    # --- band() itself
    ck("band() returns None when every score is a miss", band([0.0, 0.0, 0.0]), None)
    b = band([0.0, 0.5, 0.7])
    ck("band() ignores misses when summarising", (b["n"], b["min"], b["max"]), (2, 0.5, 0.7))

    # --- score_class against an injected recall, both directions.
    def fake(hit_name):
        def _r(_q, k=4, source_types=None, mmr=False):
            return [{"source_path": f"/x/{hit_name}", "similarity": 0.42}]
        return _r
    s, m = score_class(fake("right.md"), [("q", Path("/mem/right.md"))], 4, positive=True)
    ck("score_class credits a correct retrieval", (s, m), ([0.42], 0))
    s, m = score_class(fake("wrong.md"), [("q", Path("/mem/right.md"))], 4, positive=True)
    ck("CONTROL score_class records a MISS when the wrong file comes back", (s, m), ([0.0], 1))

    failed = [l for l, ok in checks if not ok]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    print()
    if failed:
        print(f"SELF-TEST FAILED — {len(failed)} assertion(s). Do not trust this calibration.")
        return 1
    print(f"SELF-TEST PASSED — {len(checks)} assertions; the verdict distinguishes "
          f"separated, marginal and overlapping bands.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                    help="prove the verdict logic discriminates, then exit")
    ap.add_argument("--k", type=int, default=4, help="top-k, match the caller you are calibrating (default 4)")
    ap.add_argument("--gibberish", type=int, default=12, help="number of token-salad queries")
    ap.add_argument("--json", type=Path, help="write the full report as JSON")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    # A green run means nothing if the verdict logic itself is broken, so the
    # self-test gates the real calibration rather than sitting beside it.
    if _self_test() != 0:
        return 2
    print()

    try:
        from recall import recall
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: recall unavailable ({e})", file=sys.stderr)
        return 2

    rng = random.Random(SEED)
    mems = load_memories()
    if not mems:
        print(f"FATAL: no memory files under {MEMORY_DIR}", file=sys.stderr)
        return 2

    qs = build_queries(mems, rng, args.gibberish)
    print(f"store: {len(mems)} memory files   k={args.k}   seed={SEED}\n")

    results, bands = {}, {}
    for name in ("verbatim", "descriptive", "gibberish", "absent"):
        positive = name in ("verbatim", "descriptive")
        scores, misses = score_class(recall, qs[name], args.k, positive)
        results[name] = {"scores": scores, "misses": misses, "n_queries": len(qs[name])}
        bands[name] = band(scores)

    hdr = f"{'class':<14}{'n':>5}{'min':>9}{'median':>9}{'max':>9}   note"
    print(hdr)
    print("-" * len(hdr))
    NOTE = {
        "verbatim": "HARDEST positive — a lone sentence vs the whole store",
        "descriptive": "positive — a dense summary of its own file, scores high",
        "gibberish": "easy negative",
        "absent": "HARD negative — coherent English, absent subject",
    }
    for name in ("verbatim", "descriptive", "gibberish", "absent"):
        b = bands[name]
        if not b:
            print(f"{name:<14}{results[name]['n_queries']:>5}{'—':>9}{'—':>9}{'—':>9}   no results at all")
            continue
        print(f"{name:<14}{b['n']:>5}{b['min']:>9.3f}{b['median']:>9.3f}{b['max']:>9.3f}   {NOTE[name]}")

    for name in ("verbatim", "descriptive"):
        m = results[name]["misses"]
        if m:
            print(f"\n  note: {name} — correct file NOT in top-{args.k} for {m}/{results[name]['n_queries']} queries")

    # ---- the decision -------------------------------------------------------
    print("\n" + "=" * 72)
    d = decide(bands)
    if d is None:
        print("VERDICT: INCONCLUSIVE — a required band is empty.")
        return 2

    neg_ceiling = d["neg_ceiling"]
    pos_floor, binding_class = d["pos_floor"], d["binding_class"]
    margin = d["margin"]

    print(f"negative ceiling (worst false positive) : {neg_ceiling:.4f}")
    print(f"positive floor   (weakest true positive): {pos_floor:.4f}   [binding class: {binding_class}]")
    print(f"margin                                  : {margin:+.4f}")

    if bands["gibberish"] and bands["absent"]:
        gap = bands["absent"]["max"] - bands["gibberish"]["max"]
        print(f"\nhard-vs-easy negative gap               : {gap:+.4f}"
              f"   ({'coherent absent text scores HIGHER — calibrating on gibberish alone would set the floor too low'
                    if gap > 0 else 'gibberish scored at least as high'})")

    span, rel = d["span"], d["rel"]
    rc = d["rc"]
    rec = d["recommended"]
    if d["verdict"] == "no_safe_floor":
        print("\nVERDICT: NO SAFE FLOOR — the bands OVERLAP.")
        print("  Some unanswerable queries outscore some genuine matches. No single threshold")
        print("  separates them. This is a finding about the store, not a tuning failure:")
        print("  narrative must NOT be deferred to recall alone on this branch.")
        rc = 1
    elif d["verdict"] == "marginal":
        print(f"\nVERDICT: MARGINAL — the bands are technically disjoint and practically not.")
        print(f"  margin {margin:.4f} is {rel*100:.1f}% of the observed score range ({span:.3f}).")
        print(f"  A floor at {rec:.3f} would sit {margin/2:.4f} from BOTH the weakest true")
        print(f"  positive and the strongest false one — a coin flip on anything borderline,")
        print(f"  and it will move with the next ingest.")
        print(f"  USE IT TO SUPPRESS OBVIOUS NOISE, NOT AS A CORRECTNESS MECHANISM.")
        print(f"  Recall must not be the only path to a memory on this branch. It is one of")
        print(f"  three by design; this is the measurement that says why it cannot be the one.")
        rc = 1
    else:
        print(f"\nVERDICT: floor is viable.  RECOMMENDED MIN_SIMILARITY = {rec:.3f}")
        print(f"  margin {margin:.4f} = {rel*100:.1f}% of the score range; {margin/2:.4f} either side.")
        if rel < 0.15:
            print(f"  CAUTION: re-run after any large ingest rather than trusting the number.")

    try:
        from user_prompt_submit import MIN_SIMILARITY as CURRENT
        neg_min = min(b["min"] for c, b in bands.items() if c in NEGATIVE_CLASSES and b)
        print(f"\ncurrent MIN_SIMILARITY in user_prompt_submit.py: {CURRENT}")
        print(f"  measured negative band: {neg_min:.4f} – {neg_ceiling:.4f}")

        # Three distinct states. Collapsing them (\"is it below the ceiling?\") reports
        # a deliberately-conservative floor as though it were the inert 0.30 — which
        # would read to a future maintainer as a bug rather than a decision.
        if CURRENT < neg_min:
            print(f"  *** INERT: {CURRENT} is below the ENTIRE negative band. It cannot fire. ***")
            print(f"  *** Every measured false positive is admitted, and so is everything else. ***")
            rc = rc or 1
        elif CURRENT <= neg_ceiling:
            frac = (CURRENT - neg_min) / (neg_ceiling - neg_min) if neg_ceiling > neg_min else 0.0
            print(f"  PARTIAL: fires against roughly the lower {frac*100:.0f}% of the negative band,")
            print(f"  and still admits false positives up to {neg_ceiling:.4f}.")
            print(f"  Correct ONLY as a deliberate choice — i.e. recall is one of several paths")
            print(f"  and a spurious hit costs less than a suppressed real one. If recall is the")
            print(f"  ONLY path to a memory on this branch, this floor is too low.")
        else:
            print(f"  Above the negative ceiling: suppresses every measured false positive.")
            if CURRENT > pos_floor:
                print(f"  *** BUT it is ALSO above the weakest true positive ({pos_floor:.4f}) — ***")
                print(f"  *** it is suppressing real matches. Lower it. ***")
                rc = rc or 1
    except Exception:
        pass

    if args.json:
        args.json.write_text(json.dumps(
            {"k": args.k, "seed": SEED, "n_memories": len(mems), "bands": bands,
             "negative_ceiling": neg_ceiling, "positive_floor": pos_floor, "margin": margin},
            indent=2))
        print(f"\nreport -> {args.json}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
