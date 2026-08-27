#!/usr/bin/env python3
"""Bounded memory-index generator — the injected catalogue, not the file on disk.

WHY THIS EXISTS
---------------
The index was injected by reading `memory/MEMORY.md` verbatim. That file is
human-curated prose and its size is a function of how many memories exist, so
the injected index grows every checkpoint, forever, with no ceiling.

That makes it the ONLY tier whose cost scales with memory COUNT rather than
with how many memories are binding (Cairn's finding). The constitution stops
growing when we stop writing binding rules; the index never stops. On a sibling
store MEMORY.md already measures 114,177 chars — 143% of the entire 80,000
budget, by itself, before a single rule is injected.

A fixed per-entry width is a delay, not a fix: at 400 memories a 120-char entry
is 48,000 chars and we are back here. So the index needs an ALLOCATION and it
needs to DEGRADE, not merely be narrow.

THE INVARIANT
-------------
    Degrade the description, never the existence.

A name alone is still something an agent can read on demand and something it
can search for. A dropped entry is invisible — and invisible-but-present is the
exact failure state this whole tiering effort exists to eliminate. So every
memory appears in the index unconditionally; only the DESCRIPTION is rationed.

Descriptions are therefore surrendered completely — every one of them — before a
single name is dropped.

There is still a hard ceiling: names cost ~48 chars each and that grows linearly
forever, so around 646 memories (at 40% of an 80,000 budget) even the bare roster
stops fitting. Name-only is a LONGER delay than a width cap, not an escape from
one. Past that point the generator lists as many as fit, in priority order, and
states IN-BAND exactly how many are not listed.

That is chosen over emitting an over-budget roster, because an over-budget index
gets dropped by admission as a single item — and then the agent sees nothing at
all. A partial catalogue with an honest count still separates "I was not shown
it" from "it does not exist". A silently short one does not, and reads exactly
like a complete one.

WHAT IT DOES NOT DO
-------------------
It does not write descriptions. Descriptions are human-curated by design (#86)
and an auto-summarizer would quietly replace a person's words with a model's
guess at them. This enforces WIDTH and ORDER; a human writes the words.

It also does not rewrite `MEMORY.md`. That file remains the human artefact,
with its category grouping and conventions, and the completeness check from #86
still guards it. This generates the INJECTED catalogue from each memory's own
frontmatter, which has a second benefit: the injected index cannot drift out of
sync with the corpus the way the hand-maintained file did (16 memories had no
pointer at all when the check was first run).
"""

from __future__ import annotations

import re

# Full rendered width of one entry, INCLUDING markup. Fixed at 160 by the
# cross-agent decision of 2026-08-26 so that entries authored on one substrate
# survive this generator unchanged rather than being re-wrapped. Do not drift
# this number without telling the other implementations first.
ENTRY_WIDTH = 160

# Share of the total context budget the index may consume.
INDEX_ALLOCATION_FRACTION = 0.40

HEADER = "## MEMORY INDEX (everything that exists — read any of these on demand)"

# Chars held back for the "N listed by name only" footnote, which is part of the
# index and must come out of its allocation rather than silently overrun it.
# Sized against the longest rendering of that line with six-figure counts.
_FOOTNOTE_RESERVE = 260


def truncate_at_word(text: str, limit: int) -> str:
    """Trim to at most `limit` chars, breaking on whitespace, never mid-word.

    A word cut in half is worse than a word omitted: `feedback_never_destr`
    reads as a typo, invites a wrong guess at the rest, and is unsearchable.
    The ellipsis is a signal that more exists on disk.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ""
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    # No break point at all (one very long token) — omit rather than mangle.
    if space <= 0:
        return ""
    return cut[:space].rstrip(" ,;:-") + "…"


def _name_line(name: str) -> str:
    return f"- `{name}`"


def _full_line(name: str, desc: str, width: int) -> str:
    """Render `- \\`name\\` — description`, whole line capped at `width`."""
    prefix = f"- `{name}` — "
    room = width - len(prefix)
    if room < 12:
        # The name alone eats the line. Existence beats description.
        return _name_line(name)
    body = truncate_at_word(desc, room)
    return prefix + body if body else _name_line(name)


_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _int(v) -> int:
    """Frontmatter is hand-edited. `hits: many` must not take down the session."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _recency_key(la: str) -> int:
    """Sort key for `last_applied`, DESCENDING, that cannot raise.

    The first version was `"".join(chr(255 - ord(c)) for c in la)`, which inverts
    a string lexically. `255 - ord(c)` goes negative on any non-ASCII character,
    so `last_applied: 2026–01–01` with en-dashes — what a hand-edit or a
    smart-quotes paste produces — raised ValueError out of build_index, out of
    assemble_context, out of main(), and the hook emitted NOTHING. No identity,
    no user, no constitution, no index, and silent: a hook that prints nothing
    looks exactly like a hook with nothing to add.

    Found by Cairn in adversarial review of #90 (#95), reproduced in a sandbox.
    Second instance of the class — #71 was weight() raising on a bare-scalar
    `projects`. Memory files are hand-edited BY DESIGN, so parsing them must be
    total: every input maps to some ordering, none maps to an exception.

    Returns a negated YYYYMMDD so larger dates sort first; anything unparseable
    sorts last, which is the correct place for "we do not know when this was
    used" rather than a crash or a false claim of recency.
    """
    m = _ISO_DATE.search(la or "")
    if not m:
        return 0
    return -int(m.group(1) + m.group(2) + m.group(3))


def _signal_health(entries: list[dict]) -> dict:
    """Report whether the ordering signals can actually discriminate.

    `last_applied` saturates: 103 files on this store, ONE distinct value. `hits`
    correlates with file age at r=0.85 — it counts turns elapsed, not usefulness.

    THE CAUSE IS THE SCAN WINDOW, not the injected index.

    An earlier version of this docstring blamed the index: the transcript contains
    the injected index, the index names every memory, so every memory looks cited.
    That is real, and it is NOT the mechanism. Stripping every injected region
    removes 48.2% of a 616 MB transcript and changes the credited count by zero —
    103/103 either way. Cairn reproduced the same null result independently.

    The actual cause: the citation scan re-read the ENTIRE cumulative transcript
    every turn, so a memory mentioned once was re-credited on every turn forever
    after. Fixed in #93 with a per-transcript watermark. Corrected here because a
    comment that teaches a disproven cause — with numbers attached to lend it
    weight — is worse than no comment; the next reader inherits the wrong model
    and goes looking in the wrong file. Flagged by Cairn (#95 P2).

    The ordering below survives a dead signal, but the degeneracy is still
    REPORTED, because sorting by a field that cannot discriminate produces output
    that still looks sorted. See MS4CC #91.
    """
    la = {e["last_applied"] for e in entries if e["last_applied"]}
    return {
        "distinct_last_applied": len(la),
        "last_applied_saturated": len(la) <= 1 and len(entries) > 1,
        "prevented_nonzero": sum(1 for e in entries if e["prevented"] > 0),
    }


def _priority(e: dict) -> tuple:
    """Sort key. Lower tuple sorts earlier — earlier means keeps its description.

    1. critical            the constitution's map; a rule you can't find is a rule you don't have
    2. prevented > 0       human-CONFIRMED usefulness, the one uncontaminated usage signal we own
    3. everything else     by last_applied desc, then hits desc  (degenerate today — see #91)
    4. name                deterministic tie-break; an unstable index diffs as noise every session
    """
    band = 0 if e["critical"] else (1 if e["prevented"] > 0 else 2)
    return (
        band,
        -e["prevented"],
        e["last_applied_key"],   # already negated for descending
        -e["hits"],
        e["name"],
    )


def collect(memories) -> list[dict]:
    """Normalize (path, frontmatter, body) triples into index entries.

    `type` in (index, log, roadmap) is excluded: MEMORY.md must not list itself,
    and the log/roadmap are injected by their own tiers.
    """
    out = []
    for path, fm, _body in memories:
        if fm.get("type") in ("index", "log", "roadmap"):
            continue
        la = str(fm.get("last_applied") or "")
        out.append({
            "name": path.name,
            "desc": " ".join(str(fm.get("description") or "").split()),
            "critical": bool(fm.get("critical")),
            "prevented": _int(fm.get("prevented")),
            "hits": _int(fm.get("hits")),
            "last_applied": la,
            "last_applied_key": _recency_key(la),
        })
    return out


def build_index(memories, allocation: int, *, width: int = ENTRY_WIDTH) -> tuple[str, dict]:
    """Render the injected catalogue within `allocation` chars.

    Strategy: start from the name-only roster — which is non-negotiable and
    always emitted in full — then UPGRADE entries to a full description in
    priority order for as long as the total still fits. This is the only
    formulation where the existence guarantee is structural rather than a
    property we hope the loop preserves.
    """
    entries = collect(memories)
    health = _signal_health(entries)
    entries.sort(key=_priority)

    if not entries:
        return "", {"entries": 0, "listed": 0, "roster_truncated": False, "unlisted": 0,
                    "full": 0, "name_only": 0, "chars": 0, "allocation": allocation,
                    "over_allocation": False, **health}

    header_cost = len(HEADER) + 1
    name_cost = {e["name"]: len(_name_line(e["name"])) + 1 for e in entries}
    roster_cost = header_cost + sum(name_cost.values())

    # Pre-render the upgrades so the footnote can be budgeted BEFORE spending.
    upgrade: dict[str, tuple[str, int]] = {}
    for e in entries:
        if not e["desc"]:
            continue
        line = _full_line(e["name"], e["desc"], width)
        upgrade[e["name"]] = (line, (len(line) + 1) - name_cost[e["name"]])

    # The "listed by name only" footnote is itself part of the index and must be
    # paid for out of the allocation — an earlier draft emitted it for free and
    # overran by its length, which the allocation control caught. It is only
    # charged when a tail is actually inevitable: if every description fits,
    # no footnote is emitted and reserving for one would shrink the index for
    # no reason.
    #
    # `ceiling` is the space available for ENTRIES — allocation minus whatever the
    # footnote will cost. It must NOT be floored back up to `roster_cost`: an
    # earlier version wrote `max(allocation - reserve, roster_cost)`, so whenever
    # the roster fit the allocation but roster+footnote did not, the max() handed
    # the reserve back and the footnote was emitted on top of a fully-spent
    # allocation. Measured by Cairn (#95 P1) at up to 13.6% over, in the window
    # `allocation - 260 < roster_cost <= allocation`.
    #
    # Both of my existing assertions missed it for the same reason: the roster
    # DOES fit the allocation there, and only the conjunction of roster+footnote
    # does not. Third time this shape has cost me today — a fixture that never
    # reaches the state its assertion describes.
    all_full_cost = roster_cost + sum(max(d, 0) for _l, d in upgrade.values())
    reserve = _FOOTNOTE_RESERVE if all_full_cost > allocation else 0
    ceiling = max(allocation - reserve, 0)

    # Upgrade greedily in priority order while the WHOLE index still fits.
    used = roster_cost
    full: dict[str, str] = {}
    for e in entries:
        if e["name"] not in upgrade:
            continue
        line, delta = upgrade[e["name"]]
        if delta <= 0 or used + delta <= ceiling:
            full[e["name"]] = line
            used += delta

    # Past roughly 700 memories the NAME ROSTER alone outgrows the allocation:
    # existence costs ~48 chars a head and that grows linearly, forever. Name-only
    # is a longer delay than a width cap, not an escape from Cairn's observation.
    #
    # At that point something has to give, and the choice is between emitting the
    # whole roster over-budget — which makes admission drop the index AS ONE ITEM,
    # so the agent sees NOTHING — or listing as many as fit and saying exactly how
    # many are not shown. The second is strictly better: a partial catalogue with
    # an honest count still distinguishes "I was not shown it" from "it does not
    # exist", and that distinction is the entire reason this tier exists. An
    # all-or-nothing cliff at scale is the failure this system keeps relearning.
    listed = entries
    dropped = 0
    if roster_cost > ceiling:
        keep, running = [], header_cost
        for e in entries:                      # already in priority order
            # Strict priority: STOP at the first name that does not fit rather
            # than skipping ahead to shorter ones further down. A catalogue that
            # silently reorders itself by name length would be harder to reason
            # about than one that is honestly truncated, and `unlisted` counts
            # entries that could individually have fitted. Deliberate — Cairn
            # flagged it as worth stating rather than as a defect (#95).
            if running + name_cost[e["name"]] > ceiling:
                break
            keep.append(e)
            running += name_cost[e["name"]]
        listed, dropped = keep, len(entries) - len(keep)
        full = {}                              # no room for any description

    lines = [HEADER]
    for e in listed:
        lines.append(full.get(e["name"], _name_line(e["name"])))

    n_full = len(full)
    n_short = len(listed) - n_full
    if dropped:
        lines.append(
            f"_⚠ {dropped} of {len(entries)} memories are NOT listed here — the index "
            f"allocation ({allocation:,} chars) cannot hold even their names. They exist "
            f"in `orchestrator/memory/` and are reachable by recall. Treat any "
            f"'I have no memory of that' as unreliable until the budget is raised._"
        )
    elif n_short:
        lines.append(
            f"_{n_short} of {len(entries)} listed by name only — the index allocation "
            f"({allocation:,} chars) is spent. Every memory is still listed and still "
            f"readable on disk; only the summary is omitted._"
        )

    text = "\n".join(lines)
    stats = {
        "entries": len(entries),
        "listed": len(listed),
        # Existence itself had to be rationed. This is the alarm, not a detail.
        "roster_truncated": dropped > 0,
        "unlisted": dropped,
        "full": n_full,
        "name_only": n_short,
        "chars": len(text),
        "allocation": allocation,
        "over_allocation": len(text) > allocation,
        **health,
    }
    return text, stats


# ---------------------------------------------------------------------------
# Self-test. Every assertion is paired with input it must REJECT.
# ---------------------------------------------------------------------------

def _self_test() -> int:
    from pathlib import Path

    def mem(name, desc, *, critical=False, prevented=0, hits=0, la="2026-01-01", type_="feedback"):  # noqa: E501
        return (Path(name), {"description": desc, "critical": critical, "prevented": prevented,
                             "hits": hits, "last_applied": la, "type": type_}, "body")

    checks: list[tuple[str, bool]] = []

    def ck(label, got, want=True):
        checks.append((label, got == want))

    # --- truncate_at_word: the boundary property, in both directions.
    ck("truncates on a space, never mid-word",
       truncate_at_word("alpha beta gamma delta", 14), "alpha beta…")
    ck("leaves short text untouched",
       truncate_at_word("alpha beta", 40), "alpha beta")
    ck("never emits a half-word", all(
        (t := truncate_at_word("supercalifragilistic expialidocious", n)) == ""
        or not t.rstrip("…").split()[-1].startswith("supercalifragilistica")
        for n in range(2, 40)))
    ck("a single unbreakable token is omitted, not mangled",
       truncate_at_word("supercalifragilisticexpialidocious", 10), "")
    ck("collapses whitespace so width is honest",
       truncate_at_word("a   b\n c", 40), "a b c")

    # --- CONTROL: the truncator must actually cut. A no-op passes the tests above.
    ck("CONTROL truncation really shortens",
       len(truncate_at_word("one two three four five six seven", 20)) <= 20)

    # --- width discipline
    long_desc = "word " * 200
    line = _full_line("feedback_some_rule.md", long_desc, ENTRY_WIDTH)
    ck("rendered entry respects the width cap", len(line) <= ENTRY_WIDTH)
    ck("CONTROL an unbounded render would have blown it", len(f"- `x` — {long_desc}") > ENTRY_WIDTH)
    ck("a name too long for any description degrades to name-only",
       _full_line("x" * (ENTRY_WIDTH - 5) + ".md", "some description", ENTRY_WIDTH).endswith("`"))

    # --- ordering
    corpus = [
        mem("z_plain.md", "plain", hits=5),
        mem("a_critical.md", "crit", critical=True),
        mem("m_prevented.md", "prev", prevented=2),
    ]
    txt, st = build_index(corpus, 10_000)
    order = [l for l in txt.splitlines() if l.startswith("- ")]
    ck("criticals lead the index", "a_critical.md" in order[0])
    ck("human-confirmed 'prevented' outranks a plain memory", "m_prevented.md" in order[1])
    ck("plain memories come last", "z_plain.md" in order[2])

    # --- the existence guarantee: descriptions go first, and go entirely,
    #     before a single name is dropped.
    big = [mem(f"feedback_rule_{i:03d}.md", "a description " * 20) for i in range(200)]
    txt, st = build_index(big, 6_000)
    ck("EVERY memory is still listed once descriptions are fully sacrificed",
       st["entries"] == 200 and st["listed"] == 200 and txt.count("\n- ") == 200)
    # Asserts the PROPERTY (descriptions collapse, names do not) rather than an
    # exact count: at 6,000 a handful of descriptions genuinely still fit, and a
    # hard-coded 200 would be testing the arithmetic of one allocation, not the
    # behaviour. The first draft did exactly that and failed on correct output.
    ck("descriptions are the thing sacrificed first",
       st["listed"] == 200 and st["full"] <= 10 and st["name_only"] >= 190)
    ck("existence is NOT rationed while the roster fits", st["roster_truncated"], False)

    # --- past the point where even names do not fit: partial roster + honest count,
    #     never a silent short list and never an over-budget block that gets dropped whole.
    txt_tiny, st_tiny = build_index(big, 1_000)
    ck("names are rationed only after descriptions are gone", st_tiny["roster_truncated"])
    ck("the unlisted count is REPORTED", st_tiny["unlisted"] == 200 - st_tiny["listed"]
       and st_tiny["unlisted"] > 0)
    ck("the block says so in-band", "are NOT listed here" in txt_tiny)
    ck("a truncated roster still respects the allocation", st_tiny["chars"] <= 1_000)
    ck("CONTROL a truncated roster is genuinely shorter than a full one",
       st_tiny["listed"] < st["listed"])
    ck("criticals are the names kept when the roster is cut",
       build_index([mem("z_plain.md", "d")] * 0 + [mem(f"a_plain_{i}.md", "d") for i in range(60)]
                   + [mem("zzz_critical.md", "d", critical=True)], 700)[0].splitlines()[1]
       .startswith("- `zzz_critical.md`"))

    # --- and at a generous allocation the descriptions come back.
    txt2, st2 = build_index(big, 200_000)
    ck("a sufficient allocation restores every description", st2["full"] == 200)
    ck("CONTROL the two runs actually differ", st2["chars"] > st["chars"] * 2)

    # --- allocation is respected when the roster fits but the descriptions do not.
    #
    # 18,000 is chosen to land in the genuine middle: the name-only roster costs
    # ~5,070 and each upgrade to a full 160-char entry costs ~136 more, so this
    # buys roughly 95 of 200 descriptions. An earlier draft used 40,000, which
    # fits ALL 200 descriptions — the "partial" case was never exercised and the
    # control below is what caught it.
    txt3, st3 = build_index(big, 18_000)
    ck("stays inside the allocation when the roster fits", st3["chars"] <= 18_000)
    ck("CONTROL the mid allocation is a genuine middle",
       0 < st3["full"] < 200 and st3["name_only"] > 0)

    # --- #95 P1: the window where the roster fits the allocation but roster +
    #     footnote does not. Swept rather than spot-checked, because the whole
    #     defect was that a single fixture never landed inside the window.
    forty = [mem(f"feedback_u_{i:02d}.md", "d " * 40) for i in range(40)]
    roster = len(HEADER) + 1 + sum(len(_name_line(f"feedback_u_{i:02d}.md")) + 1 for i in range(40))
    over = []
    for alloc in range(roster - _FOOTNOTE_RESERVE - 40, roster + _FOOTNOTE_RESERVE + 40, 10):
        _t, s = build_index(forty, alloc)
        if s["chars"] > alloc:
            over.append((alloc, s["chars"] - alloc))
    ck("#95 P1 the index NEVER exceeds its allocation across the whole window",
       over == [], True)
    if over:  # pragma: no cover - diagnostic only
        print(f"       overruns: {over[:6]}")
    # CONTROL: the sweep must actually cross the boundary, or it proves nothing.
    shapes = {build_index(forty, a)[1]["roster_truncated"]
              for a in (roster - _FOOTNOTE_RESERVE - 40, roster + _FOOTNOTE_RESERVE + 30)}
    ck("#95 P1 CONTROL the sweep spans both truncated and untruncated", shapes, {True, False})

    # --- #95 P0: hand-edited frontmatter must never raise out of this module.
    for bad in ("2026–01–01", "2026-01-01 ✅", "", None, "not a date", "2026-01-01 да"):
        try:
            build_index([mem("a.md", "d", la=bad)], 10_000)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        ck(f"#95 P0 last_applied={bad!r:20.20} does not raise", ok)
    try:
        build_index([(Path("z.md"), {"description": "d", "critical": False,
                                     "prevented": "lots", "hits": "many",
                                     "last_applied": "2026-01-01", "type": "feedback"}, "b")],
                    10_000)
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    ck("#95 P0 non-numeric hits/prevented do not raise", ok)
    ck("#95 P0 CONTROL recency order still holds for real dates",
       [e["name"] for e in sorted(collect([mem("old.md", "d", la="2026-01-01"),
                                           mem("new.md", "d", la="2026-08-27")]), key=_priority)],
       ["new.md", "old.md"])

    # --- signal degeneracy must be detected, not silently ordered by.
    same = [mem(f"a_{i}.md", "d", la="2026-08-27") for i in range(5)]
    _, sd = build_index(same, 10_000)
    ck("saturated last_applied is flagged", sd["last_applied_saturated"])
    varied = [mem(f"a_{i}.md", "d", la=f"2026-08-2{i}") for i in range(5)]
    _, sv = build_index(varied, 10_000)
    ck("CONTROL a healthy last_applied is NOT flagged", sv["last_applied_saturated"], False)

    # --- index/log/roadmap are excluded so the catalogue cannot list itself.
    _, sx = build_index([mem("MEMORY.md", "the index", type_="index"),
                         mem("feedback_real.md", "real")], 10_000)
    ck("the index does not list itself", sx["entries"] == 1)

    # --- determinism: an index that reorders itself diffs as noise every session.
    import random
    shuffled = big[:]
    random.Random(7).shuffle(shuffled)
    ck("output is order-independent", build_index(shuffled, 18_000)[0] == txt3)

    # --- the footnote is paid for out of the allocation, not on top of it.
    ck("the name-only footnote is budgeted, not free",
       len(_FOOTNOTE_RESERVE * "x") >= max(
           len(f"_{n} of {n} listed by name only — the index allocation "
               f"({a:,} chars) is spent. Every memory is still listed and still "
               f"readable on disk; only the summary is omitted._")
           for n, a in ((999_999, 9_999_999), (1, 1))))

    failed = [l for l, ok in checks if not ok]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    print()
    if failed:
        print(f"SELF-TEST FAILED — {len(failed)} assertion(s) cannot distinguish good from bad.")
        return 1
    print(f"SELF-TEST PASSED — {len(checks)} assertions, each paired with input it rejects.")
    return 0


def main() -> int:
    import argparse
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Render the bounded memory index.")
    ap.add_argument("--self-test", action="store_true")
    # NOTE: argparse expands %-tokens in help text, so the literal percent sign
    # must survive OUR interpolation as `%%` for argparse to render it as `%`.
    ap.add_argument("--allocation", type=int, default=None,
                    help=f"chars (default: {int(INDEX_ALLOCATION_FRACTION * 100)}%% "
                         "of the context budget)")
    ap.add_argument("--show", action="store_true", help="print the rendered index")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import session_start as ss

    allocation = args.allocation or int(ss.context_budget_chars() * INDEX_ALLOCATION_FRACTION)
    text, stats = build_index(ss.load_memory_files(), allocation)

    if args.show:
        print(text)
        print()
    print(f"entries      {stats['entries']}")
    print(f"full         {stats['full']}")
    print(f"name-only    {stats['name_only']}")
    print(f"chars        {stats['chars']:,} / {stats['allocation']:,} allocation"
          f"{'  ** OVER **' if stats['over_allocation'] else ''}")
    print(f"signal       last_applied distinct={stats['distinct_last_applied']}"
          f"{'  SATURATED (see #91)' if stats['last_applied_saturated'] else ''}"
          f", prevented>0 on {stats['prevented_nonzero']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
