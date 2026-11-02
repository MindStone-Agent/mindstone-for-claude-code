#!/usr/bin/env python3
"""Report what a bootstrap file actually delivers into agent context.

The problem this exists for, in one line:
    load-bearing rules can be missing from context with no signal, because our checks
    measure what we emit, not what we receive.

Two agents proposed a metric for this on 2026-11-01 and then misapplied it within the hour,
in opposite directions, with the correct definition in hand. So the numbers are computed
here rather than typed into a table.

WHAT IT DOES
    Splits a bootstrap file into sections, works out which part of it survives the harness's
    head/tail truncation, and reports the loss three ways:

      1. NOT DELIVERED = file_chars - (head + tail)      arithmetic; cannot lie
      2. the breakdown: whole sections, partial sections, seam artefacts
      3. BY RULE:       arrives / arrives without its marking / does not arrive

    Methods 1 and 2 are computed independently and compared. If they disagree the run FAILS
    loudly. Agreeing numbers that were computed once are one number.

    The seam-artefact test is on CONTENT, not on the boundary: a stub is only free if the
    characters it loses are whitespace. A seam can swallow a newline or a whole section, and
    "it came from a seam" does not distinguish those.

EXIT CODES
    0  checked, both methods agree, classification complete
    1  the two methods DISAGREE, or a rule is not delivered  -> do not trust the table
    2  could not check (missing file, bad arguments, unreadable content)

Usage
    bootstrap-report.py AGENTS.md
    bootstrap-report.py AGENTS.md --limit 20000 --head-ratio 0.7 --tail-ratio 0.2
    bootstrap-report.py AGENTS.md --json
    bootstrap-report.py AGENTS.md --rules-only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

HEADING_RE = re.compile(r"^(#{2,3}) (.+)$", re.M)

# A rule is a section whose heading carries the ⛔ or ⚠️ marker, or whose title is one of these.
# The marker is the convention in this workspace; the title list is the escape hatch for rules
# that predate it.
RULE_TITLE_RE = re.compile(r"\b(Safety|Group Chats|External vs Internal)\b", re.I)

STATUS_WHOLE = "whole"
STATUS_STUB = "whole-tail-cut"       # lost span is whitespace only: not damage
STATUS_MARK = "partial-marking"      # body delivered, heading clipped
STATUS_BODY = "partial-body"         # heading delivered, body cut
STATUS_ABSENT = "absent"


@dataclass
class Section:
    title: str
    start: int
    end: int
    heading_end: int
    delivered: int
    lost: int
    lost_text: str
    status: str
    is_rule: bool

    @property
    def size(self) -> int:
        return self.end - self.start


def split_sections(text: str) -> list[Section]:
    heads = list(HEADING_RE.finditer(text))
    if not heads:
        return []
    secs: list[Section] = []
    for i, m in enumerate(heads):
        start = m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        nl = text.find("\n", start)
        heading_end = nl if nl != -1 else end
        title = m.group(2).strip()
        secs.append(
            Section(
                title=title,
                start=start,
                end=end,
                heading_end=heading_end,
                delivered=0,
                lost=0,
                lost_text="",
                status=STATUS_WHOLE,
                is_rule=bool(re.search(r"[⛔⚠️]", m.group(1) + m.group(2))) or bool(RULE_TITLE_RE.search(title)),
            )
        )
    return secs


def delivered_span(sec: Section, head_end: int, tail_start: int, total: int) -> tuple[int, int]:
    """Return (delivered_chars, lost_chars) for one section under a head/tail window."""
    # fully inside the head
    if sec.end <= head_end:
        return sec.size, 0
    # fully inside the tail
    if sec.start >= tail_start:
        return sec.size, 0
    # straddles the head cut: delivered from start to head_end
    if sec.start < head_end < sec.end:
        return head_end - sec.start, sec.end - head_end
    # straddles the tail cut: delivered from tail_start to end
    if sec.start < tail_start < sec.end:
        return sec.end - tail_start, tail_start - sec.start
    # entirely between the windows
    return 0, sec.size


def classify(sec: Section, head_end: int, tail_start: int, text: str) -> None:
    delivered, lost = delivered_span(sec, head_end, tail_start, len(text))
    sec.delivered = delivered
    sec.lost = lost
    if lost == 0:
        sec.status = STATUS_WHOLE
        sec.lost_text = ""
        return
    if delivered == 0:
        sec.status = STATUS_ABSENT
        sec.lost_text = text[sec.start : sec.end]
        return
    # which slice was lost?
    if sec.start < head_end < sec.end:
        sec.lost_text = text[head_end : sec.end]
    else:
        sec.lost_text = text[sec.start : tail_start]
    # the seam-artefact test: content, not boundary
    if sec.lost_text.strip() == "":
        sec.status = STATUS_STUB
        return
    # is the heading itself fully delivered?
    if sec.heading_end > (head_end if sec.start < head_end < sec.end else tail_start):
        sec.status = STATUS_MARK
    else:
        sec.status = STATUS_BODY


def build_report(text: str, limit: int, head_ratio: float, tail_ratio: float) -> dict:
    head_end = int(limit * head_ratio)
    tail_len = int(limit * tail_ratio)
    tail_start = max(head_end, len(text) - tail_len)
    window = head_end + tail_len

    secs = split_sections(text)
    for s in secs:
        classify(s, head_end, tail_start, text)

    # ---- method 1: arithmetic. file minus window. ----
    not_delivered_arith = max(0, len(text) - window)

    # ---- method 2: the breakdown, computed from sections. ----
    by_status: dict[str, int] = {}
    lost_total = 0
    for s in secs:
        # the breakdown is of LOST characters, not section sizes: the columns have to sum
        # to the same subtrahend or the table can inflate without anyone noticing.
        by_status[s.status] = by_status.get(s.status, 0) + s.lost
        lost_total += s.lost
    # characters before the first heading (title block) are always delivered
    prefix = secs[0].start if secs else len(text)
    not_delivered_sum = lost_total

    rules = [s for s in secs if s.is_rule]
    rules_broken = [s for s in rules if s.status not in (STATUS_WHOLE, STATUS_STUB)]

    return {
        "file": {
            "path": None,
            "chars": len(text),
            "sections": len(secs),
            "prefix_chars": prefix,
        },
        "window": {
            "limit": limit,
            "head_ratio": head_ratio,
            "tail_ratio": tail_ratio,
            "head_end": head_end,
            "tail_start": tail_start,
            "delivered_capacity": window,
            "note": "head+tail is the usable window; limit - (head+tail) is never filled",
        },
        "not_delivered": {
            "arithmetic": not_delivered_arith,
            "from_breakdown": not_delivered_sum,
            "agree": not_delivered_arith == not_delivered_sum,
        },
        "breakdown": {k: v for k, v in sorted(by_status.items())},
        "rules": {
            "total": len(rules),
            "arriving": len([s for s in rules if s.status == STATUS_WHOLE]),
            "arriving_stub": len([s for s in rules if s.status == STATUS_STUB]),
            "without_marking": len([s for s in rules if s.status in (STATUS_MARK, STATUS_BODY)]),
            "absent": len([s for s in rules if s.status == STATUS_ABSENT]),
            "broken": [{"title": s.title, "status": s.status, "lost": s.lost} for s in rules_broken],
            "sums": (
                len([s for s in rules if s.status == STATUS_WHOLE])
                + len([s for s in rules if s.status == STATUS_STUB])
                + len([s for s in rules if s.status in (STATUS_MARK, STATUS_BODY)])
                + len([s for s in rules if s.status == STATUS_ABSENT])
            ),
        },
        "sections": [
            {
                "title": s.title,
                "size": s.size,
                "delivered": s.delivered,
                "lost": s.lost,
                "pct_delivered": round(100 * s.delivered / s.size, 2) if s.size else 100.0,
                "status": s.status,
                "is_rule": s.is_rule,
            }
            for s in secs
        ],
    }


def print_report(rep: dict, rules_only: bool) -> None:
    f, w = rep["file"], rep["window"]
    nd = rep["not_delivered"]
    print("=== bootstrap delivery ===")
    print(f"  file              {f['chars']:>7,} chars · {f['sections']} sections · prefix {f['prefix_chars']}")
    print(f"  window            {w['delivered_capacity']:>7,} chars  (head {w['head_end']:,} + tail {w['delivered_capacity']-w['head_end']:,})")
    print()
    print(f"  NOT DELIVERED     {nd['arithmetic']:>7,}   (file - window)")
    print(f"  from breakdown    {nd['from_breakdown']:>7,}")
    if nd["agree"]:
        print("  ✅ the two methods agree")
    else:
        print("  ❌ THE TWO METHODS DISAGREE — do not use these numbers")
    print()
    print("  where it went:")
    for k, v in rep["breakdown"].items():
        note = ""
        if k == STATUS_STUB:
            note = "   (whitespace only — not damage)"
        elif k == STATUS_ABSENT:
            note = "   (whole sections dropped)"
        elif k == STATUS_MARK:
            note = "   (body arrives, heading clipped)"
        elif k == STATUS_BODY:
            note = "   (heading arrives, body cut)"
        print(f"    {k:<16} {v:>7,}{note}")
    print()
    r = rep["rules"]
    print(f"  rules: {r['total']} total · {r['arriving']} whole · {r['arriving_stub']} whole(newline only) · {r['without_marking']} without marking · {r['absent']} absent")
    for b in r["broken"]:
        print(f"    ⚠️  {b['status']:<16} {b['title'][:58]}")
    if rules_only:
        return
    print()
    print("  every section:")
    for s in rep["sections"]:
        flag = "R" if s["is_rule"] else " "
        print(f"    {s['pct_delivered']:>6.2f}%  {s['size']:>6,}  {s['status']:<16} {flag} {s['title'][:48]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="bootstrap file to measure")
    ap.add_argument("--limit", type=int, default=20000, help="harness character limit (default 20000)")
    ap.add_argument("--head-ratio", type=float, default=0.70, help="head fraction of limit (default 0.70)")
    ap.add_argument("--tail-ratio", type=float, default=0.20, help="tail fraction of limit (default 0.20)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--rules-only", action="store_true", help="skip the per-section listing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        print(f"bootstrap-report: {path} not found", file=sys.stderr)
        return 2
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        print(f"bootstrap-report: cannot read {path}: {e}", file=sys.stderr)
        return 2
    if not text.strip():
        print(f"bootstrap-report: {path} is empty", file=sys.stderr)
        return 2

    rep = build_report(text, args.limit, args.head_ratio, args.tail_ratio)
    rep["file"]["path"] = str(path)

    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print_report(rep, args.rules_only)

    if not rep["not_delivered"]["agree"]:
        return 1
    if rep["rules"]["absent"] or rep["rules"]["without_marking"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
