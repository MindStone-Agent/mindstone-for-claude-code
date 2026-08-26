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

Usage:
    python3 calibrate_recall_floor.py
    python3 calibrate_recall_floor.py --k 6 --json report.json
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=4, help="top-k, match the caller you are calibrating (default 4)")
    ap.add_argument("--gibberish", type=int, default=12, help="number of token-salad queries")
    ap.add_argument("--json", type=Path, help="write the full report as JSON")
    args = ap.parse_args()

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
    if not bands["absent"] or not any(bands[c] for c in POSITIVE_CLASSES):
        print("VERDICT: INCONCLUSIVE — a required band is empty.")
        return 2

    # The floor must clear EVERY negative and suppress NO positive, so it is
    # bounded by the worst case on each side — the highest false positive and the
    # WEAKEST true positive, across all classes.
    #
    # An earlier version of this script set the ceiling from `descriptive` alone,
    # on the reasoning that `verbatim` was inflated and should not drive the
    # decision. That was backwards: verbatim scores LOWER (a lone sentence must
    # match its own file against 500+ competing chunks, where a description is a
    # dense summary of the whole file), so excluding it discarded the hardest
    # positive case and produced a floor that would have suppressed real matches.
    # Choosing which positives to measure against is how a harness flatters itself.
    neg_ceiling = max(b["max"] for c, b in bands.items() if c in NEGATIVE_CLASSES and b)
    binding = min(((bands[c]["min"], c) for c in POSITIVE_CLASSES if bands[c]), key=lambda t: t[0])
    pos_floor, binding_class = binding
    margin = pos_floor - neg_ceiling

    print(f"negative ceiling (worst false positive) : {neg_ceiling:.4f}")
    print(f"positive floor   (weakest true positive): {pos_floor:.4f}   [binding class: {binding_class}]")
    print(f"margin                                  : {margin:+.4f}")

    if bands["gibberish"] and bands["absent"]:
        gap = bands["absent"]["max"] - bands["gibberish"]["max"]
        print(f"\nhard-vs-easy negative gap               : {gap:+.4f}"
              f"   ({'coherent absent text scores HIGHER — calibrating on gibberish alone would set the floor too low'
                    if gap > 0 else 'gibberish scored at least as high'})")

    # A positive margin is not the same as a usable one. The score range here spans
    # roughly 0.48–0.77, so a margin of a few thousandths is a rounding artefact
    # dressed as a decision. Report three outcomes, not two.
    span = max(b["max"] for b in bands.values() if b) - min(b["min"] for b in bands.values() if b)
    rel = margin / span if span else 0.0
    MARGINAL_REL = 0.05          # <5% of the observed score range = not separation

    rc = 0
    rec = neg_ceiling + margin * 0.5
    if margin <= 0:
        print("\nVERDICT: NO SAFE FLOOR — the bands OVERLAP.")
        print("  Some unanswerable queries outscore some genuine matches. No single threshold")
        print("  separates them. This is a finding about the store, not a tuning failure:")
        print("  narrative must NOT be deferred to recall alone on this branch.")
        rc = 1
    elif rel < MARGINAL_REL:
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
