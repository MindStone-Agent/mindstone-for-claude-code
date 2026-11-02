#!/usr/bin/env python3
"""What actually reaches context in a Claude Code session (MS4CC #64, #108).

Our checks measured what the hooks EMIT. This reports what the harness DELIVERED, read from the
session transcript, where Claude Code records each hook's additionalContext either in full or as a
"<persisted-output> ... Preview (first 2KB)" stub, and each instruction file (CLAUDE.md, MEMORY.md)
as the text it actually injected.

Usage:
  python3 orchestrator/runbooks/context_report.py [--project-dir DIR] [--memory-dir DIR] [--transcript FILE]
                                         [--rule "text of a load-bearing rule"]...

Defaults: the project dir is ~/.claude/projects/<cwd with / replaced by ->; the transcript is the
newest *.jsonl there; the memory dir is <project dir>/memory. Standard library only; reads files,
writes nothing, prints no environment. Section 5 measures duplication; it never edits a file.
This covers Claude Code sessions. For MindStone bootstrap files, see scripts/bootstrap-report.py (PR #141).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PERSISTED = re.compile(r"<persisted-output>\s*Output too large \(([\d.]+)\s*(KB|MB|B)\)")
CUT = re.compile(r"Only part of it was loaded: (\d+) of (\d+) lines were cut off, starting at line (\d+)")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---", re.S)


def kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def flag(fm: str, name: str) -> bool:
    # Top-level or nested (e.g. under `metadata:`), as session_start.py now reads it.
    return re.search(rf"^\s*{name}:\s*true\s*$", fm, re.M | re.I) is not None


def memory_files(mem: Path) -> None:
    files = [p for p in sorted(mem.glob("*.md")) if p.name != "MEMORY.md"]
    crit, ever, crit_bytes = 0, 0, 0
    for p in files:
        text = p.read_text(errors="replace")
        m = FRONTMATTER.match(text)
        fm = m.group(1) if m else ""
        if flag(fm, "critical"):
            crit += 1
            crit_bytes += len(text.encode())
        if flag(fm, "evergreen"):
            ever += 1
    print("1. Memory files")
    print(f"   total {len(files)}, critical {crit}, evergreen {ever}, critical set {kb(crit_bytes)}")


def load(transcript: Path) -> list[dict]:
    out = []
    with transcript.open(errors="replace") as f:
        for line in f:
            if '"attachment"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o.get("attachment"), dict):
                out.append(o)
    return out


def delivered_size(content: object) -> tuple[bool, int, str]:
    """(persisted?, original size in bytes, text the model actually saw)."""
    if isinstance(content, list):
        text = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in content)
    else:
        text = content if isinstance(content, str) else json.dumps(content)
    m = PERSISTED.search(text)
    if m:
        mult = {"B": 1, "KB": 1024, "MB": 1024 * 1024}[m.group(2)]
        return True, int(float(m.group(1)) * mult), text
    return False, len(text.encode()), text


def instruction_files(atts: list[dict]) -> list[str]:
    """The text of every instruction file (CLAUDE.md, MEMORY.md, ...) in the latest instructions attachment."""
    last: list[str] = []
    for o in atts:
        a = o["attachment"]
        if a.get("type") == "instructions":
            last = [str(fi.get("content", "")) for fi in a.get("files", [])]
    return last


def memory_index(mem: Path, atts: list[dict]) -> str:
    """The index in --memory-dir: its size on disk, and how (and whether) it reached the model.

    Claude Code injects its own auto-memory MEMORY.md as an instruction file (cut at about 25 KB). An
    MS4CC index elsewhere only reaches the model inside the SessionStart hook output. Both are
    reported, each with the path it was measured on.
    """
    idx = mem / "MEMORY.md"
    print("2. MEMORY.md")
    instr: list[tuple[str, str]] = []
    for o in atts:
        a = o["attachment"]
        if a.get("type") == "instructions":
            instr = [(str(fi.get("path", "")), str(fi.get("content", ""))) for fi in a.get("files", []) if str(fi.get("path", "")).endswith("/MEMORY.md")]
    delivered_text = ""
    if not idx.exists():
        print(f"   {idx}: none")
    else:
        disk = idx.read_text(errors="replace")
        print(f"   {idx}: {disk.count(chr(10)) + 1} lines, {kb(len(disk.encode()))} on disk")
        same = [(pth, c) for pth, c in instr if os.path.realpath(pth) == os.path.realpath(idx)]
        if same:
            delivered_text = same[-1][1]
            cut = CUT.search(delivered_text)
            how = (f"lines 1-{int(cut.group(3)) - 1} of {cut.group(2)} ({cut.group(1)} cut off)" if cut else "whole file")
            print(f"     delivered as a Claude Code instruction file: {how}, {kb(len(delivered_text.encode()))} injected")
        else:
            # Look for the index's first substantial line in the SessionStart output the model saw.
            probe = next((l.strip() for l in disk.splitlines() if len(l.strip()) > 40 and not l.startswith("---")), "")
            starts = [o["attachment"] for o in atts if o["attachment"].get("type") == "hook_additional_context" and o["attachment"].get("hookEvent") == "SessionStart"]
            if not starts:
                print("     not delivered as an instruction file, and no SessionStart output in this transcript")
            else:
                p_, size, text = delivered_size(starts[-1].get("content"))
                seen = probe and probe in text
                if p_:
                    print(f"     not an instruction file; the latest SessionStart output (where MS4CC sends the index) was SAVED to a file "
                          f"({kb(size)}), so the model saw a 2 KB preview; the index's first line {'is' if seen else 'is not'} in it")
                else:
                    print(f"     {'found' if seen else 'NOT found'} in the latest SessionStart output (inline, {kb(size)})")
                if seen:
                    delivered_text = text
    for pth, c in instr:
        if not idx.exists() or os.path.realpath(pth) != os.path.realpath(idx):
            cut = CUT.search(c)
            how = (f"lines 1-{int(cut.group(3)) - 1} of {cut.group(2)} cut" if cut else "whole file")
            print(f"   {pth} (Claude Code auto-memory, a different file): {how}, {kb(len(c.encode()))} injected")
    return delivered_text


def hooks(atts: list[dict]) -> list[str]:
    print("3. Hook output (additionalContext)")
    by_event: dict[str, list[tuple[bool, int]]] = {}
    for o in atts:
        a = o["attachment"]
        if a.get("type") != "hook_additional_context":
            continue
        p, size, _ = delivered_size(a.get("content"))
        by_event.setdefault(str(a.get("hookEvent", "?")), []).append((p, size))
    for ev, xs in sorted(by_event.items()):
        saved = [s for p, s in xs if p]
        inline = [s for p, s in xs if not p]
        print(f"   {ev}: {len(xs)} outputs; inline {len(inline)} (largest {kb(max(inline, default=0))}); "
              f"saved to a file {len(saved)} (largest {kb(max(saved, default=0))})")
    # The latest session start: SessionStart outputs within 60 s of the last one.
    starts = [o for o in atts if o["attachment"].get("type") == "hook_additional_context" and o["attachment"].get("hookEvent") == "SessionStart"]
    seen: list[str] = []
    if starts:
        t_last = starts[-1].get("timestamp", "")
        def ts(o: dict) -> float:
            try:
                return datetime.fromisoformat(str(o.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        latest = [o for o in starts if ts(starts[-1]) - ts(o) <= 60]
        print(f"   latest session start ({t_last}):")
        for o in latest:
            p, size, text = delivered_size(o["attachment"].get("content"))
            print(f"     {'SAVED, 2 KB preview shown' if p else 'inline'}: {kb(size)}")
            seen.append(text)
    return seen


def canary(rules: list[str], seen: list[str], index: str, instructions: list[str]) -> None:
    if not rules:
        print("4. Load-bearing rule: pass --rule \"...\" to check one")
        return
    print("4. Load-bearing rules: present in what the harness recorded as delivered?")
    norm = lambda s: re.sub(r"\s+", " ", s).lower()
    sources = [("session-start hook output (as shown)", norm("\n".join(seen))),
               ("MEMORY.md as delivered", norm(index)),
               ("instruction files re-read in the transcript", norm("\n".join(instructions)))]
    for r in rules:
        hit = [name for name, h in sources if norm(r) in h]
        print(f"   {'YES' if hit else 'NO '}  {r[:80]}{'  <- ' + ', '.join(hit) if hit else ''}")
    print("   NO means: not in the session-start hook output the model saw, nor in the delivered memory index.")
    print("   CLAUDE.md files loaded with the system prompt aren't recorded in the transcript; check those by hand.")


def duplicates(mem: Path) -> None:
    """Agenda item 1: duplicated sections (same header twice) and index lines naming the same file twice."""
    idx = mem / "MEMORY.md"
    print("5. Duplication in MEMORY.md (measured, not fixed)")
    if not idx.exists():
        print("   none in the memory dir")
        return
    lines = idx.read_text(errors="replace").splitlines()
    heads = [l.strip() for l in lines if l.startswith("#")]
    dup_heads = sorted({h for h in heads if heads.count(h) > 1})
    links = re.findall(r"\]\(([^)]+\.md)\)", "\n".join(lines))
    dup_links = sorted({x for x in links if links.count(x) > 1})
    print(f"   headers {len(heads)}, repeated {len(dup_heads)}" + (f": {', '.join(dup_heads[:8])}" if dup_heads else ""))
    print(f"   index entries {len(links)}, files listed more than once {len(dup_links)}" + (f": {', '.join(dup_links[:8])}" if dup_links else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project-dir")
    ap.add_argument("--memory-dir")
    ap.add_argument("--transcript")
    ap.add_argument("--rule", action="append", default=[])
    a = ap.parse_args()
    proj = Path(a.project_dir) if a.project_dir else Path.home() / ".claude" / "projects" / os.getcwd().replace("/", "-")
    mem = Path(a.memory_dir) if a.memory_dir else proj / "memory"
    if a.transcript:
        tr = Path(a.transcript)
    else:
        cands = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not cands:
            # Sessions started from another directory (the home dir, say) live under another project
            # dir: fall back to the newest transcript anywhere, and say so.
            cands = sorted((Path.home() / ".claude" / "projects").glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
            if not cands:
                print(f"no transcript in {proj} or any other project dir", file=sys.stderr)
                return 2
            print(f"(no transcript in {proj}; using the newest one anywhere: pass --transcript to choose)")
        tr = cands[-1]
    print(f"transcript: {tr.name}\nmemory dir: {mem}\n")
    memory_files(mem)
    atts = load(tr)
    index = memory_index(mem, atts)
    seen = hooks(atts)
    canary(a.rule, seen, index, instruction_files(atts))
    duplicates(mem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
