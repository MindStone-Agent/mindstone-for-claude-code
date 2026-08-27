#!/usr/bin/env python3
"""Audit the memory usage counters — and prove the citation scan can return ZERO.

WHY
---
`hits` and `last_applied` feed `memory_weight.base()` and the decay term in
`session_start.weight()`. They were measuring nothing.

The citation scan re-read the ENTIRE cumulative transcript on every turn, so a
memory mentioned once was re-credited on every turn thereafter, forever. Measured
on a 617 MB transcript: scanning the last 50 KB credits 1 memory, the last 500 KB
credits 4, the whole file credits all 103. So `hits` tracked turns-elapsed-since-
first-mention — file age, r=0.85 — and `last_applied` was stamped to today for
every file every turn, which pinned the decay term near 1.0 permanently. The
half-life mechanism had never once fired.

A counter with no input that produces zero is not a measurement. This file exists
to hold the fix to that standard: the self-test asserts the scan CAN decline to
credit, which is the property the old implementation could not satisfy on any
input at all.

Usage:
    python3 hit_counter_audit.py              # report the live signal's health
    python3 hit_counter_audit.py --self-test  # prove the scan can return zero
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
from datetime import date
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))

MEMORY_DIR = ORCHESTRATOR_DIR / "memory"

# `YYYY-MM-DD__<uuid>.jsonl` or a bare `<uuid>.jsonl` — the shapes
# archive_transcript writes. Anything else in transcripts/ is not a session.
_SESSION_JSONL = re.compile(
    r"^(\d{4}-\d{2}-\d{2}__)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$", re.I)

FM = """---
name: {stem}
description: a test memory
type: feedback
critical: false
hits: 0
prevented: 0
last_applied: null
created: 2026-01-01
---

body
"""


def _self_test() -> int:
    """Exercise the REAL auto_increment_hits, not a reimplementation of it.

    A detector written against the behaviour under test is how a working feature
    gets reported as a regression, and this repo has already paid for that once.
    So the module is imported and its globals redirected at a temp store.
    """
    import session_end as se

    checks: list[tuple[str, bool]] = []

    def ck(label, got, want=True):
        checks.append((label, got == want))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mem = tmp / "memory"
        mem.mkdir()
        for stem in ("feedback_alpha_rule", "feedback_beta_rule", "feedback_gamma_rule"):
            (mem / f"{stem}.md").write_text(FM.format(stem=stem))

        # Redirect the module at the temp store, restore afterwards.
        orig_mem, orig_state = se.MEMORY_DIR, se.STATE_PATH
        se.MEMORY_DIR = mem
        se.STATE_PATH = tmp / "state.json"
        try:
            transcript = tmp / "2026-01-01__test.jsonl"

            def hits(stem):
                for line in (mem / f"{stem}.md").read_text().splitlines():
                    if line.startswith("hits:"):
                        return int(line.split(":", 1)[1])
                return -1

            # Fixtures are real JSONL RECORDS, because that is what a transcript
            # is. An earlier version used bare text, which meant the injection
            # cases were never shaped like actual injections — the fixture could
            # not reach the state the assertion described. Same failure as #95 P1.
            def injected(*names):
                return json.dumps({"type": "attachment", "attachment": {
                    "type": "hook_additional_context",
                    "content": ["<orchestrator-context>\n## MEMORY INDEX\n"
                                + "\n".join(f"- `{n}` - a test memory" for n in names)
                                + "\n</orchestrator-context>"]}})

            def wrote(text):
                return json.dumps({"type": "assistant", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": text}]}})

            # --- 1. A name that appears ONLY inside an injected record is not a citation.
            transcript.write_text(injected("feedback_alpha_rule.md") + "\n")
            got = se.auto_increment_hits(transcript)
            ck("an injected index mention credits NOTHING", got, [])
            ck("  and the counter really did not move", hits("feedback_alpha_rule"), 0)

            # --- 2. The same name in authored text IS a citation.
            with transcript.open("a") as f:
                f.write(wrote("I applied feedback_alpha_rule.md before touching that config.") + "\n")
            got = se.auto_increment_hits(transcript)
            ck("an authored mention credits the memory", got, ["feedback_alpha_rule.md"])
            ck("  and the counter moved by exactly one", hits("feedback_alpha_rule"), 1)

            # --- 3. THE CORE PROPERTY: rescanning credits nothing.
            #     This is what the old implementation could never do. It re-read the
            #     whole file each turn, so this call returned the citation again —
            #     and again, every turn, forever.
            got = se.auto_increment_hits(transcript)
            ck("RESCANNING the same transcript credits nothing", got, [])
            ck("  the counter is still one, not two", hits("feedback_alpha_rule"), 1)

            # --- 4. New authored text after the watermark still counts.
            with transcript.open("a") as f:
                f.write(wrote("Then feedback_beta_rule.md came up and I followed it.") + "\n")
            got = se.auto_increment_hits(transcript)
            ck("NEW text after the watermark is still credited", got, ["feedback_beta_rule.md"])
            ck("  and the earlier memory was NOT re-credited", hits("feedback_alpha_rule"), 1)

            # --- 5. A memory nobody mentioned is never credited. The zero case.
            ck("an unmentioned memory stays at zero", hits("feedback_gamma_rule"), 0)

            # --- 6. Truncation/replacement must not seek past EOF and scan nothing.
            transcript.write_text(wrote("short file mentioning feedback_gamma_rule.md once.") + "\n")
            got = se.auto_increment_hits(transcript)
            ck("a REPLACED (shorter) transcript is rescanned from the start",
               got, ["feedback_gamma_rule.md"])

            # --- 7. The watermark advances even on an unproductive scan, so a quiet
            #        turn does not leave the window open to be re-counted later.
            state = json.loads((tmp / "state.json").read_text())
            ck("the watermark is persisted at EOF",
               state["hit_scan_offsets"]["2026-01-01__test.jsonl"] == transcript.stat().st_size)

            # --- #96: the filter must anchor on RECORD SHAPE, not on tag text.
            #
            # Cairn's reproduction: a docstring that merely NAMES the tag used to
            # open a DOTALL match, which then ran forward to the next real closing
            # tag and deleted every genuine citation in between. Preferentially
            # destroying citations from sessions that work on the memory system.
            inj = json.dumps({"type": "attachment", "attachment": {
                "type": "hook_additional_context",
                "content": ["<semantic-recall>\nfeedback_gamma_rule.md\n</semantic-recall>"]}})
            doc = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": 'def assemble_context(): """Build the '
                                         '<orchestrator-context> block."""'}]}})
            cite = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I applied feedback_alpha_rule.md here, "
                                         "and it stopped me."}]}})
            real = json.dumps({"type": "attachment", "attachment": {
                "type": "hook_additional_context",
                "content": ["<orchestrator-context>\nindex\n</orchestrator-context>"]}})
            kept = se.authored_text("\n".join([inj, doc, cite, real]))
            ck("#96 a citation between a tag-mention and a real region SURVIVES",
               "feedback_alpha_rule.md" in kept)
            ck("#96 the tag-naming docstring itself survives",
               "Build the" in kept)
            ck("#96 CONTROL hook-injected records are still dropped",
               "feedback_gamma_rule.md" not in kept and "\nindex\n" not in kept)
            ck("#96 CONTROL an injection-only window yields nothing to credit",
               se.authored_text(inj).strip(), "")

            # A torn/partial line is KEPT rather than discarded: dropping
            # unrecognised text is how this filter lost citations the first time.
            ck("#96 a torn JSON line is kept, not silently dropped",
               "feedback_beta_rule.md" in se.authored_text('{"type":"assist'
                                                           'feedback_beta_rule.md'))

            # --- the watermark must be LINE-ALIGNED so a torn final record is
            #     re-read when whole rather than half-scanned and skipped forever.
            torn = tmp / "torn.jsonl"
            torn.write_text(cite + "\n" + '{"type":"assistant","message":{"content":[{"typ')
            before = hits("feedback_alpha_rule")
            se.auto_increment_hits(torn)
            st = json.loads((tmp / "state.json").read_text())
            ck("the watermark stops at the last COMPLETE record",
               st["hit_scan_offsets"]["torn.jsonl"], len(cite) + 1)
            ck("  and the complete record before it was still credited",
               hits("feedback_alpha_rule"), before + 1)
            # Completing the torn record must then credit it — not skip it.
            torn.write_text(cite + "\n" + json.dumps(
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "and feedback_gamma_rule.md too"}]}}) + "\n")
            got = se.auto_increment_hits(torn)
            ck("CONTROL the once-torn record is credited once it is complete",
               got, ["feedback_gamma_rule.md"])
        finally:
            se.MEMORY_DIR, se.STATE_PATH = orig_mem, orig_state

    failed = [l for l, ok in checks if not ok]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    print()
    if failed:
        print(f"SELF-TEST FAILED — {len(failed)} assertion(s). The counter cannot be trusted.")
        return 1
    print(f"SELF-TEST PASSED — {len(checks)} assertions, including the one the old "
          f"implementation could not satisfy on any input: the scan can return ZERO.")
    return 0


def _report() -> int:
    """Describe the live signal. Historical values encode age, not use."""
    sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))
    import session_start as ss

    mem = [(p, fm) for p, fm, _b in ss.load_memory_files()]
    la = {}
    ages, hits = [], []
    for p, fm in mem:
        la[str(fm.get("last_applied"))] = la.get(str(fm.get("last_applied")), 0) + 1
        c = str(fm.get("created") or "")[:10]
        try:
            d = date.fromisoformat(c)
        except Exception:
            continue
        ages.append((date.today() - d).days)
        hits.append(int(fm.get("hits") or 0))

    print(f"memory files                {len(mem)}")
    print(f"distinct last_applied       {len(la)}"
          + ("   <- SATURATED: cannot order anything" if len(la) <= 1 else ""))
    if len(ages) > 2 and len(set(hits)) > 1:
        mx, my = statistics.mean(ages), statistics.mean(hits)
        num = sum((x - mx) * (y - my) for x, y in zip(ages, hits))
        den = (sum((x - mx) ** 2 for x in ages) * sum((y - my) ** 2 for y in hits)) ** 0.5
        r = num / den if den else 0.0
        print(f"r(file age, hits)           {r:+.4f}"
              + ("   <- hits is tracking AGE, not use" if abs(r) > 0.6 else ""))
    prevented = sum(1 for _p, fm in mem if int(fm.get("prevented") or 0) > 0)
    print(f"prevented > 0               {prevented}   (human-confirmed; the clean signal)")
    print()
    print("Values recorded before #91 encode age rather than use and do not become")
    print("correct by accumulating more of them. Resetting hits/last_applied to zero")
    print("is more honest than carrying them, and is a deliberate call, not a side effect.")
    return 0


def _seed_watermarks() -> int:
    """One-time migration: mark existing transcripts as already accounted for.

    The scan defaults to offset 0 for a transcript it has not seen, which is right
    for a NEW session. It is wrong exactly once — at the moment the fix lands —
    because the archived history has already been counted, many times over. Without
    this, the first post-fix run would scan 617 MB and hand every memory one last
    phantom credit, which is the bug's parting gift.

    Deliberately a separate, explicit step rather than a clever default: "skip
    everything the first time you see it" would silently discard the first turn of
    every future session, trading a visible one-off for an invisible forever.
    """
    sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))
    import session_end as se

    state = se._load_index_state()
    offsets = state.setdefault("hit_scan_offsets", {})
    seeded, already = 0, 0
    # Session transcripts only. `recall_usage.jsonl` also lives in this directory
    # and is append-only log data that names memories constantly — 283 of 296 stems
    # in one measurement — so anything that globs `*.jsonl` here and treats the
    # result as a transcript acquires the single worst file in the tree to scan for
    # citations. Cairn's note on #93; it was seeded harmlessly before, but the glob
    # is the hazard, not the seeding.
    for p in sorted((ORCHESTRATOR_DIR / "transcripts").glob("*.jsonl")):
        if not _SESSION_JSONL.match(p.name):
            print(f"  skipped {p.name}  (not a session transcript)")
            continue
        size = p.stat().st_size
        if offsets.get(p.name) == size:
            already += 1
            continue
        offsets[p.name] = size
        seeded += 1
        print(f"  seeded {p.name}  @ {size:,} bytes")
    se._save_index_state(state)
    print(f"\n{seeded} transcript(s) seeded, {already} already current.")
    print("Citations are now counted only from text written after this point.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--seed-watermarks", action="store_true",
                    help="one-time: treat existing archived transcripts as already counted")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.seed_watermarks:
        return _seed_watermarks()
    return _report()


if __name__ == "__main__":
    raise SystemExit(main())
