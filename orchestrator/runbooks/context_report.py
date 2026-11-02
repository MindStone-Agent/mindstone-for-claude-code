#!/usr/bin/env python3
"""What actually reaches context in a Claude Code session (MS4CC #64, #108).

Our checks measured what the hooks EMIT. This reports what the harness DELIVERED, read from the
session transcript, where Claude Code records each hook's additionalContext either in full or as a
"<persisted-output> ... Preview (first 2KB)" stub, and each instruction file (CLAUDE.md, MEMORY.md)
as the text it actually injected.

Usage:
  python3 orchestrator/runbooks/context_report.py [--project-dir DIR] [--memory-dir DIR] [--transcript FILE]
                                         [--rule "text of a load-bearing rule"]...

Defaults: the project dir is ~/.claude/projects/<cwd, each non-alphanumeric character replaced by ->; the transcript is the
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

PERSISTED = re.compile(r"\A\s*<persisted-output>\s*Output too large \(([\d.]+)\s*(KB|MB|B)\)")
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
            if '"attachment"' not in line and '"compact_boundary"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o.get("attachment"), dict):
                out.append(o)
            elif o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                # Kept as a pseudo-attachment: after a compaction the harness re-sends instruction files.
                out.append({"attachment": {"type": "compact_boundary"}, "timestamp": o.get("timestamp", "")})
    return out


def joined(content: object) -> str:
    if isinstance(content, list):
        return "\n".join(x if isinstance(x, str) else json.dumps(x) for x in content)
    return content if isinstance(content, str) else json.dumps(content)


def delivered_size(content: object) -> tuple[bool, int, str]:
    """For one hook output: (saved to a file?, original size in bytes, text the model actually saw).
    Saved only when the output IS the stub; an inline output that quotes a stub stays inline."""
    text = joined(content)
    m = PERSISTED.search(text)
    if m:
        mult = {"B": 1, "KB": 1024, "MB": 1024 * 1024}[m.group(2)]
        return True, int(float(m.group(1)) * mult), text
    return False, len(text.encode()), text


def outputs(content: object) -> list[tuple[bool, int, str]]:
    """One (persisted?, size, text) per hook output: an attachment can carry several, each saved separately."""
    if isinstance(content, list):
        return [delivered_size(x) for x in content]
    return [delivered_size(content)]


def unrecorded(atts: list[dict]) -> tuple[str, list[str]]:
    """Why an instruction-file NO can't be trusted, if it can't: ("", []) when it can.
    A compaction re-sends the instruction files; a re-read carries only the re-read file. Any file
    recorded before the last compaction and not recorded since has an unknown state, and so does
    everything when the transcript never recorded instruction files at all."""
    ever: set[str] = set()
    pending: set[str] = set()
    when = ""
    for o in atts:
        a = o["attachment"]
        if a.get("type") == "compact_boundary":
            pending, when = set(ever), str(o.get("timestamp", "")) or "?"
        elif a.get("type") == "instructions":
            for fi in a.get("files", []):
                path = str(fi.get("path", ""))
                ever.add(path)
                pending.discard(path)
    if not ever:
        return "no instruction files are recorded in this transcript", []
    if pending:
        return f"no re-send recorded since the compaction at {when} for", sorted(pending)
    return "", []


def latest_starts(atts: list[dict]) -> list[dict]:
    """The SessionStart attachments of the latest session start: those within 60 s of the last one."""
    starts = [o for o in atts if o["attachment"].get("type") == "hook_additional_context" and o["attachment"].get("hookEvent") == "SessionStart"]
    if not starts:
        return []
    def ts(o: dict) -> float:
        try:
            return datetime.fromisoformat(str(o.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return [o for o in starts if ts(starts[-1]) - ts(o) <= 60]


def instruction_files(atts: list[dict]) -> list[str]:
    """The instruction files in context now: the set re-sent after the last compaction (or at the start),
    updated by later re-reads, which carry only the re-read file."""
    latest: dict[str, str] = {}
    for o in atts:
        a = o["attachment"]
        if a.get("type") == "compact_boundary":
            latest = {}
        elif a.get("type") == "instructions":
            for fi in a.get("files", []):
                latest[str(fi.get("path", ""))] = str(fi.get("content", ""))
    return list(latest.values())


def memory_index(mem: Path, atts: list[dict]) -> str:
    """The index in --memory-dir: its size on disk, and how (and whether) it reached the model.

    Claude Code injects its own auto-memory MEMORY.md as an instruction file (cut at about 25 KB). An
    MS4CC index elsewhere only reaches the model inside the SessionStart hook output. Both are
    reported, each with the path it was measured on.
    """
    idx = mem / "MEMORY.md"
    print("2. MEMORY.md")
    instr: list[tuple[str, str]] = []
    instr_ts = ""
    for o in atts:
        a = o["attachment"]
        if a.get("type") == "compact_boundary":
            instr = []
        elif a.get("type") == "instructions":
            found = [(str(fi.get("path", "")), str(fi.get("content", ""))) for fi in a.get("files", []) if str(fi.get("path", "")).endswith("/MEMORY.md")]
            if found:
                instr, instr_ts = found, str(o.get("timestamp", ""))
    delivered_text = ""
    if not idx.exists():
        print(f"   {idx}: none")
    else:
        disk = idx.read_text(errors="replace")
        print(f"   {idx}: {len(disk.splitlines())} lines, {kb(len(disk.encode()))} on disk")
        same = [(pth, c) for pth, c in instr if os.path.realpath(pth) == os.path.realpath(idx)]
        if same:
            delivered_text = same[-1][1]
            cut = CUT.search(delivered_text)
            how = (f"lines 1-{int(cut.group(3)) - 1} of {cut.group(2)} ({cut.group(1)} cut off)" if cut else "whole file")
            print(f"     delivered as a Claude Code instruction file: {how}, {kb(len(delivered_text.encode()))} injected"
                  f"{' (the copy recorded at ' + instr_ts + ')' if instr_ts else ''}")
            # The harness drops the frontmatter and numbers what is left, so its line N is not file line N.
            fm = FRONTMATTER.match(disk)
            skip = disk[: fm.end()].count("\n") + 1 if fm else 0
            if skip:
                print(f"     the harness numbers lines after dropping the {skip}-line frontmatter: its line 1 is file line {skip + 1}")
            # The copy delivered after a compaction can predate edits made earlier in the session.
            shown = set(delivered_text.split("> WARNING:")[0].splitlines())
            body = disk.splitlines()[skip:]
            end = len(body)
            if cut:
                start = delivered_text[cut.end():].split('("', 1)[-1].split("\n", 1)[0][:40]
                end = next((i for i, l in enumerate(body) if start and l.startswith(start)), end)
            missing = [l for l in body[:end] if l.strip() and l not in shown]
            if missing:
                print(f"     the delivered copy is OLDER than the file, which was edited after the last recorded copy (a compaction "
                      f"usually re-sends that copy; a re-read refreshes it): {len(missing)} line(s) "
                      f"of the file are not in it; first: {missing[0][:80]}. A fresh copy is cut by size too, so these "
                      f"lines are not necessarily ones that would arrive")
        elif any(os.path.realpath(p_) == os.path.realpath(idx) for p_ in unrecorded(atts)[1]):
            print(f"     UNKNOWN: {unrecorded(atts)[0]} this file")
        else:
            # MS4CC builds its index in the SessionStart output from memory frontmatter rather than pasting
            # MEMORY.md, so look for the index's entries (the memory files it names), not its first line.
            fm = FRONTMATTER.match(disk)
            names = sorted(set(re.findall(r"\]\(([^)]+\.md)\)", disk[fm.end():] if fm else disk)))
            latest = latest_starts(atts)
            if not latest:
                print("     not delivered as an instruction file, and no SessionStart output in this transcript")
            else:
                outs = [x for o in latest for x in outputs(o["attachment"].get("content"))]
                print(f"     not an instruction file; MS4CC sends its index in SessionStart output. The latest start had {len(outs)} output(s):")
                for p_, size, text in outs:
                    n = sum(1 for nm in names if nm in text)
                    shown = "in the 2 KB preview" if p_ else "in it"
                    print(f"       {'SAVED to a file, 2 KB preview shown' if p_ else 'inline'} ({kb(size)}): "
                          f"{n} of the {len(names)} memory files the index names appear {shown}")
                    if n:
                        delivered_text += text + "\n"
    for pth, c in instr:
        if not idx.exists() or os.path.realpath(pth) != os.path.realpath(idx):
            cut = CUT.search(c)
            how = (f"lines 1-{int(cut.group(3)) - 1} of {cut.group(2)} cut" if cut else "whole file")
            print(f"   {pth} (Claude Code auto-memory, a different file): {how}, {kb(len(c.encode()))} injected")
    return delivered_text


def hooks(atts: list[dict]) -> list[str]:
    print("3. Hook output (additionalContext)")
    by_event: dict[str, list[tuple[bool, int]]] = {}
    events: dict[str, int] = {}
    for o in atts:
        a = o["attachment"]
        if a.get("type") != "hook_additional_context":
            continue
        ev = str(a.get("hookEvent", "?"))
        events[ev] = events.get(ev, 0) + 1
        by_event.setdefault(ev, []).extend((p, size) for p, size, _ in outputs(a.get("content")))
    for ev, xs in sorted(by_event.items()):
        saved = [s for p, s in xs if p]
        inline = [s for p, s in xs if not p]
        print(f"   {ev}: {events[ev]} events, {len(xs)} outputs; inline {len(inline)} (largest {kb(max(inline, default=0))}); "
              f"saved to a file {len(saved)} (largest {kb(max(saved, default=0))})")
    # The latest session start: SessionStart outputs within 60 s of the last one.
    latest = latest_starts(atts)
    seen: list[str] = []
    if latest:
        print(f"   latest session start ({latest[-1].get('timestamp', '')}):")
        for o in latest:
            for p, size, text in outputs(o["attachment"].get("content")):
                print(f"     {'SAVED, 2 KB preview shown (the preview names the saved file)' if p else 'inline'}: {kb(size)}")
            seen.append(joined(o["attachment"].get("content")))
    return seen


def canary(rules: list[str], seen: list[str], index: str, instructions: list[str], why_unknown: tuple[str, list[str]] = ("", [])) -> None:
    if not rules:
        print("4. Load-bearing rule: pass --rule \"...\" to check one")
        return
    print("4. Load-bearing rules: present in what the harness recorded as delivered?")
    norm = lambda s: re.sub(r"\s+", " ", s).lower()
    sources = [("session-start hook output (as shown)", norm("\n".join(seen))),
               ("MEMORY.md as delivered", norm(index)),
               ("instruction files in context (since the last compaction, plus re-reads)", norm("\n".join(instructions)))]
    for r in rules:
        hit = [name for name, h in sources if norm(r) in h]
        verdict = "YES" if hit else ("UNK" if why_unknown[0] else "NO ")
        print(f"   {verdict}  {r[:80]}{'  <- ' + ', '.join(hit) if hit else ''}")
    if why_unknown[0]:
        files = f" {', '.join(why_unknown[1])}" if why_unknown[1] else ""
        print(f"   UNK means: not found, but {why_unknown[0]}{files}, so their state is unknown.")
    print("   NO means: not in the session-start output the model saw, the delivered memory index, or the instruction")
    print("   files in context now (re-sent at the last compaction, plus later re-reads). Per-prompt recall is not checked: it surfaces memories by")
    print("   similarity, intermittently. A file loaded only with the system prompt and never recorded can't be checked.")


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
    ap.add_argument("--project-dir", help="Claude Code's transcript dir, ~/.claude/projects/<escaped cwd> (not the repo); default from the cwd")
    ap.add_argument("--memory-dir", help="the memory dir whose MEMORY.md to measure; default <project dir>/memory")
    ap.add_argument("--transcript", help="a session .jsonl; default the newest in the project dir")
    ap.add_argument("--rule", action="append", default=[], help="a load-bearing rule, verbatim; repeatable")
    a = ap.parse_args()
    # Claude Code names a project dir after the cwd with every non-alphanumeric character as "-".
    proj = Path(a.project_dir) if a.project_dir else Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
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
    canary(a.rule, seen, index, instruction_files(atts), unrecorded(atts))
    duplicates(mem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
