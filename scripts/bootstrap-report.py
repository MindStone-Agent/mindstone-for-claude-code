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
    0  the numbers are trustworthy AND every rule arrives
    3  the numbers are trustworthy, but a rule does not arrive fully
    1  the two methods DISAGREE -> do not use this table at all
    2  could not check (missing file, bad arguments, unreadable content)

    Aegis's point, and it is the right split: the exit code answers "can I trust these
    numbers", not "is delivery perfect". Those are different questions, and folding them
    together means a broken rule hides a broken measurement.

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

# only used to size the truncation marker; the real name is per-file but the length is what
# matters for budgeting and it is within a character or two either way.
_MARKER_NAME = "AGENTS.md"

# A rule is a section whose heading carries the ⛔ or ⚠️ marker, or whose title matches one of
# these words. The marker is this workspace's convention; the word list is the portability
# hatch, because boxes name their rule sections differently and a count that depends on one
# box's naming is not comparable across boxes. Override entirely with --rules-pattern.
DEFAULT_RULE_WORDS = (
    r"Safety|Group Chats|External vs Internal|Operating rules|Rules|Constraints|"
    r"Conventions|Protocol|Conduct|MANDATORY|NEVER|ALWAYS|\bMUST\b"
)
RULE_TITLE_RE = re.compile(r"\b(?:" + DEFAULT_RULE_WORDS + r")\b", re.I)

STATUS_WHOLE = "whole"
STATUS_STUB = "whole-tail-cut"       # lost span is whitespace only: not damage
STATUS_MARK = "partial-marking"      # body delivered, heading clipped
STATUS_BODY = "partial-body"         # heading delivered, body cut
STATUS_ABSENT = "absent"

# Cairn's four-step cause attribution. A count without a cause leaves the reader to guess, and
# a reader guesses the cause they already believed. Checked in this order, and the last step is
# what makes a zero trustworthy: if a rule should have arrived and did not, that is a bug in the
# probe, not a finding about the file.
CAUSE_NOT_WRITTEN = "not-in-file"      # never written to disk
CAUSE_ALLOWLIST = "allowlisted-out"    # file not offered to this run
CAUSE_TRIM = "trimmed"                 # rule offset outside the kept head/tail
CAUSE_UNKNOWN = "UNKNOWN"              # should be present; probe is wrong


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


def attribute_cause(
    sec: Section,
    head_end: int,
    tail_start: int,
    file_offered: bool,
    text: str,
) -> tuple[str, str]:
    """Cairn's four steps, in order. Returns (cause, explanation).

    The control is the file on disk: we know what was written, so the gap between that and what
    arrived is the cause. Step 4 exists so a zero can be trusted -- if the rule should have been
    delivered and was not, the probe is wrong and we say so rather than inventing a cause.
    """
    # 1. not in the file on disk: never written
    if sec.delivered == 0 and sec.lost == 0 and sec.size == 0:
        return CAUSE_NOT_WRITTEN, "zero-length section"
    if sec.status == STATUS_WHOLE:
        return "", "arrived whole"

    # 2. the file was not offered to this run at all
    if not file_offered:
        return CAUSE_ALLOWLIST, "file not in this session class's bootstrap set"

    # 3. the rule's offset falls outside the kept head and tail
    outside_head = sec.start >= head_end and sec.end <= tail_start
    spans_seam = (sec.start < head_end < sec.end) or (sec.start < tail_start < sec.end)
    if outside_head or spans_seam:
        return CAUSE_TRIM, f"offset {sec.start}-{sec.end} vs kept head<{head_end} tail>{tail_start}"

    # 4. it should have been there. The probe is wrong, and saying "trimmed" here would be a
    #    fabricated cause for a real bug.
    return CAUSE_UNKNOWN, (
        f"section sits inside the kept window (offset {sec.start}-{sec.end}, "
        f"head<{head_end}, tail>{tail_start}) but arrived as {sec.status}; probe bug"
    )


def build_report(
    text: str,
    limit: int,
    head_ratio: float = 0.70,
    tail_ratio: float = 0.20,
    head_chars: int | None = None,
    tail_chars: int | None = None,
    file_name: str | None = None,
    file_offered: bool = True,
) -> dict:
    # Absolute counts win when given: a harness that reports its own head and tail should be
    # measured, not approximated by a fraction that happens to work on two boxes.
    head_end = head_chars if head_chars is not None else int(limit * head_ratio)
    tail_len = tail_chars if tail_chars is not None else int(limit * tail_ratio)
    tail_start = max(head_end, len(text) - tail_len)
    window = head_end + tail_len

    # When the file is truncated the harness inserts a marker between head and tail. It is
    # spent from the limit and belongs to neither window, so the number actually injected is
    # window + marker, and the unused remainder is limit - that.
    truncated = len(text) > limit
    marker_len = 0
    if truncated:
        name = file_name or _MARKER_NAME
        # marker + the two newlines that join it to head and tail; it is spent from the limit
        # and belongs to neither window
        # Exactly what the harness does: marker = ["", line1, line2, ""].join("\n"), then the
        # content is [head, marker, tail].join("\n"). So the overhead beyond head+tail is the
        # joined marker plus the two newlines that attach it to head and tail.
        marker_block = "\n".join(
            [
                "",
                f"[...truncated, read {name} for full content...]",
                f"…(truncated {name}: kept {head_end}+{tail_len} chars of {len(text)})…",
                "",
            ]
        )
        marker_len = len(marker_block) + 2
    injected = window + marker_len

    s_causes: list[dict] = []
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
    # the marked set is what Cairn argues for: stable, script-and-person identical. It is also
    # narrower -- it excludes sections titled "Safety" or "Group Chats" because they carry no
    # marker. Report both, always, so the difference is visible rather than a choice.
    marked_rules = [s for s in secs if ("⛔" in s.title or "⚠️" in s.title)]
    rules_broken = [s for s in rules if s.status not in (STATUS_WHOLE, STATUS_STUB)]

    # cause attribution for every rule that did not arrive whole
    for s in rules_broken:
        cause, why = attribute_cause(s, head_end, tail_start, file_offered, text)
        s_causes.append(
            {
                "title": s.title,
                "status": s.status,
                "cause": cause,
                "why": why,
                "lost": s.lost,
            }
        )
    unknown = [c for c in s_causes if c["cause"] == CAUSE_UNKNOWN]

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
            "truncation_marker_chars": marker_len,
            "actually_injected": injected,
            # NOT clamped at zero. A negative value is the overflow condition: the marker is
            # spent from a limit that the head/tail split already exhausted, so the function
            # emits more than maxChars. Only reachable at small limits, but it is real and a
            # clamp would hide exactly the case worth knowing about.
            "unused_of_limit": limit - injected,
            "note": (
                "head+tail is the window; when truncated a marker is also spent from the limit, "
                "so unused = limit - (head+tail+marker). The shortfall is not headroom: it is a "
                "remainder that no code allocates."
            ),
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
            "broken": s_causes,
            "causes": {
                CAUSE_TRIM: len([c for c in s_causes if c["cause"] == CAUSE_TRIM]),
                CAUSE_ALLOWLIST: len([c for c in s_causes if c["cause"] == CAUSE_ALLOWLIST]),
                CAUSE_NOT_WRITTEN: len([c for c in s_causes if c["cause"] == CAUSE_NOT_WRITTEN]),
                CAUSE_UNKNOWN: len(unknown),
            },
            "probe_failed": bool(unknown),
            "marked_set": {
                "total": len(marked_rules),
                "whole": len([s for s in marked_rules if s.status == STATUS_WHOLE]),
                "tail_cut": len([s for s in marked_rules if s.status == STATUS_STUB]),
                "partial": len([s for s in marked_rules if s.status in (STATUS_MARK, STATUS_BODY)]),
                "absent": len([s for s in marked_rules if s.status == STATUS_ABSENT]),
            },
            # sections that are rule-class by title but carry no marker: the safety sections
            # live here, so neither set is complete on its own
            "unmarked_rule_class": [
                s.title for s in rules if s not in marked_rules
            ],
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
    absent_count = len([x for x in rep["sections"] if x["status"] == STATUS_ABSENT])
    ref_absent = [x for x in rep["sections"] if x["status"] == STATUS_ABSENT and not x["is_rule"]]
    print(
        f"    ({absent_count} sections absent in total: "
        f"{len(ref_absent)} unmarked reference, {absent_count - len(ref_absent)} rule-class)"
    )
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
    ms = r.get("marked_set") or {}
    print(f"  rules (rule-class): {r['total']} total · {r['arriving']} whole · {r['arriving_stub']} newline-cut · {r['without_marking']} partial · {r['absent']} absent")
    if ms:
        print(f"  rules (marked ⛔/⚠️):  {ms['total']} total · {ms['whole']} whole · {ms['tail_cut']} newline-cut · {ms['partial']} partial · {ms['absent']} absent")
        un = r.get("unmarked_rule_class") or []
        if un:
            print(f"  in rule-class but unmarked ({len(un)}): {', '.join(t[:34] for t in un)}")
    for b in r["broken"]:
        print(f"    ⚠️  {b['status']:<16} {b['title'][:52]}")
        print(f"        cause: {b['cause']}  ({b['why']})")
    if r.get("probe_failed"):
        cause_txt = ", ".join(f"{k}={v}" for k, v in r["causes"].items() if v)
        print(f"    ❌ UNKNOWN cause count > 0 ({cause_txt}) — the probe is wrong, not the file")
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
    ap.add_argument("--split", default=None, metavar="HEAD,TAIL",
                    help="head,tail as one argument, e.g. --split 0.8,0.1. "
                         "A ratio is a property of the harness, not a constant; a tool that can "
                         "only express one box's split measures the wrong window on the box that matters.")
    ap.add_argument("--head", type=int, default=None,
                    help="absolute head chars, overriding --head-ratio. Measure this, do not assume it.")
    ap.add_argument("--tail", type=int, default=None,
                    help="absolute tail chars, overriding --tail-ratio")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--rules-only", action="store_true", help="skip the per-section listing")
    ap.add_argument("--not-offered", action="store_true",
                    help="this file was NOT offered to the run (cron/subagent allowlist). "
                         "Changes the cause from 'trimmed' to 'allowlisted-out'.")
    ap.add_argument("--rules-pattern", default=None,
                    help="regex for rule-section titles (overrides the default word list)")
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

    if args.split:
        try:
            parts = [p.strip() for p in args.split.split(",")]
            if len(parts) != 2:
                raise ValueError("expected HEAD,TAIL")
            args.head_ratio, args.tail_ratio = float(parts[0]), float(parts[1])
        except ValueError as e:
            print(f"bootstrap-report: bad --split {args.split!r}: {e}", file=sys.stderr)
            return 2

    if args.rules_pattern:
        global RULE_TITLE_RE
        RULE_TITLE_RE = re.compile(args.rules_pattern, re.I)

    # Absolute values win over ratios. A ratio that is right on two boxes and wrong on the
    # fifth produces a table that reconciles internally and measures the wrong thing, which
    # is the failure mode this whole exercise exists to catch. If the harness reports its own
    # head and tail, pass those with --head and --tail rather than guessing a fraction.
    head_chars = args.head if args.head is not None else int(args.limit * args.head_ratio)
    tail_chars = args.tail if args.tail is not None else int(args.limit * args.tail_ratio)
    if head_chars + tail_chars > args.limit:
        print(
            f"bootstrap-report: --head {head_chars:,} + --tail {tail_chars:,} "
            f"exceeds --limit {args.limit:,}",
            file=sys.stderr,
        )
        return 2

    rep = build_report(
        text,
        args.limit,
        args.head_ratio,
        args.tail_ratio,
        head_chars=head_chars,
        tail_chars=tail_chars,
        file_name=path.name,
        file_offered=not args.not_offered,
    )
    rep["file"]["path"] = str(path)

    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print_report(rep, args.rules_only)

    # 1 is reserved for "these numbers cannot be trusted". A rule arriving badly is a real
    # finding, not a reason to distrust the arithmetic, so it gets its own code.
    if not rep["not_delivered"]["agree"]:
        return 1
    if rep["rules"].get("probe_failed"):
        return 1
    if rep["rules"]["absent"] or rep["rules"]["without_marking"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
