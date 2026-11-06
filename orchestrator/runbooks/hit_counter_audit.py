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
    python3 hit_counter_audit.py --recount    # recompute hits from all archives (dry run)
    python3 hit_counter_audit.py --recount --apply   # ...and write it (backs up first)
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

            # --- ALLOWLIST (2026-09-25): record types that NAME memories without
            #     anyone using them. Each shape is copied from a real transcript.
            #     With the old denylist every one of these credited the memory; 447
            #     of 729 Stop runs credited 102-116 of 117 memories that way.
            n = "feedback_alpha_rule.md"
            not_citations = {
                "file-history-snapshot": {"type": "file-history-snapshot", "snapshot": {
                    "trackedFileBackups": {f"/x/orchestrator/memory/{n}": {"backupFileName": "b"}}}},
                "hook_success (SessionStart index)": {"type": "attachment", "attachment": {
                    "type": "hook_success", "content": f"## MEMORY INDEX\n- [{n}]({n})"}},
                "edited_text_file": {"type": "attachment", "attachment": {
                    "type": "edited_text_file", "filename": f"/x/memory/{n}", "snippet": n}},
                "tool_result": {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": f"memory/{n}"}]},
                    "toolUseResult": {"file": {"filePath": f"/x/memory/{n}"}}},
                "task-notification": {"type": "user", "message": {"role": "user",
                    "content": f"<task-notification>\n<result>cited {n}</result>"}},
                "isMeta user record": {"type": "user", "isMeta": True, "message": {
                    "role": "user", "content": f"skill body mentioning {n}"}},
                "system record": {"type": "system", "content": f"hook ran on {n}"},
                "an unknown future record type": {"type": "brand-new-thing", "text": n},
                "compaction summary": {"type": "user", "isCompactSummary": True, "message": {
                    "role": "user", "content": f"This session is being continued... {n}"}},
                "`!` shell output": {"type": "user", "message": {"role": "user",
                    "content": f"<bash-stdout>memory/{n}</bash-stdout>"}},
                "Write that creates the memory": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Write",
                                 "input": {"file_path": f"/x/memory/{n}", "content": "body"}}]}},
                "Edit of the memory": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Edit",
                                 "input": {"file_path": f"/x/memory/{n}", "old_string": "a", "new_string": "b"}}]}},
                "shell heredoc writing the memory": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": f"cat > orchestrator/memory/{n} <<'EOF'\nbody\nEOF"}}]}},
                "sed -i on the memory": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": f"sed -i '' 's/a/b/' orchestrator/memory/{n}"}}]}},
                "cd into memory, then append": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": f"cd orchestrator/memory && cat >> {n} <<'EOF'\nx\nEOF"}}]}},
                "cd into memory, then sed -i": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": f"cd orchestrator/memory && sed -i '' 's/a/b/' {n}"}}]}},
                "python writing the memory": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": f"python3 - <<'EOF'\np='orchestrator/memory/{n}'\nopen(p,'w').write(s)\nEOF"}}]}},
                "queued task-notification": {"type": "attachment", "attachment": {
                    "type": "queued_command", "commandMode": "prompt",
                    "prompt": f"<task-notification>{n}</task-notification>"}},
            }
            for label, rec in not_citations.items():
                ck(f"allowlist: {label} is NOT a citation",
                   n in se.authored_text(json.dumps(rec)), False)
            citations = {
                "assistant tool_use (Read by name)": {"type": "assistant", "message": {
                    "role": "assistant", "content": [{"type": "tool_use", "name": "Read",
                        "input": {"file_path": f"/x/memory/{n}"}}]}},
                "Write of a LOG entry that names the memory": {"type": "assistant", "message": {
                    "role": "assistant", "content": [{"type": "tool_use", "name": "Write",
                        "input": {"file_path": "/x/orchestrator/LOG.md", "content": f"applied {n}"}}]}},
                "a read next to open('agents.md').read()": {"type": "assistant", "message": {
                    "role": "assistant", "content": [{"type": "tool_use", "name": "Bash",
                        "input": {"command": f"python3 -c \"open('agents.md').read(); print(open('memory/{n}').read())\""}}]}},
                "echo '-> memory/<name>'": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": f"echo 'see -> orchestrator/memory/{n}'"}}]}},
                "shell READ of the memory (cat)": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": f"cat orchestrator/memory/{n}"}}]}},
                "assistant thinking": {"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "thinking", "thinking": f"{n} says not to"}]}},
                "human-typed user text": {"type": "user", "message": {
                    "role": "user", "content": f"remember {n}?"}},
                "human message queued while busy (#107)": {"type": "attachment", "attachment": {
                    "type": "queued_command", "commandMode": "prompt",
                    "prompt": [{"type": "text", "text": f"also check {n}"}]}},
            }
            for label, rec in citations.items():
                ck(f"allowlist: {label} IS a citation",
                   n in se.authored_text(json.dumps(rec)), True)
            # End to end through the counter: a window of ONLY non-citations
            # credits nothing.
            quiet = tmp / "2026-01-02__quiet.jsonl"
            quiet.write_text("\n".join(json.dumps(r) for r in not_citations.values()) + "\n")
            before = hits("feedback_alpha_rule")
            got = se.auto_increment_hits(quiet)
            ck("a window of snapshots/index/tool output credits NOTHING", got, [])
            ck("  and the counter really did not move", hits("feedback_alpha_rule"), before)
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


def _is_stop_boundary(rec: dict) -> bool:
    """The record Claude Code writes after each Stop hook run.

    The live counter credits once per Stop run, so a recount has to cut its
    windows at the same place to stay on the same scale as the live increments
    that follow it.
    """
    return rec.get("type") == "system" and rec.get("subtype") == "stop_hook_summary"


def _recount(apply: bool) -> int:
    """Recompute hits / last_applied from every archived session with the CURRENT filter.

    The values on disk came from the denylist filter, which credited ~113 of 117
    memories on every turn (2026-09-25). They measure turns, not use, and don't
    become correct by accumulating more. Zeroing them throws away real history;
    recounting keeps it. hits = number of Stop-run windows whose authored text
    cites the memory; last_applied = the date of the last such window.
    `prevented` (human-confirmed) is never touched.

    Two hazards in the archives themselves:
      - Claude Code sometimes writes history back into a session file (31,388
        repeated record uuids in one archive), so records are de-duplicated by
        uuid across ALL archives.
      - archive_transcript rewrites an archive in place on every Stop run, so an
        archive that changes while it is being read is an error, not a result.
    Run --apply from a plain terminal with no Claude session active.

    Dry run by default. --apply backs up the memory dir first, then re-seeds the
    scan watermarks so the next Stop run starts at EOF.
    """
    import tarfile
    from datetime import datetime, timezone
    import session_end as se

    counts: dict[str, int] = {}
    last: dict[str, str] = {}
    seen_uuids: set[str] = set()
    windows = 0
    archives = [p for p in sorted((ORCHESTRATOR_DIR / "transcripts").glob("*.jsonl"))
                if _SESSION_JSONL.match(p.name)]
    for p in archives:
        before = (p.stat().st_size, p.stat().st_mtime_ns)
        window: list[str] = []
        stamp = ""

        def flush():
            nonlocal windows
            if not window:
                return
            windows += 1
            for name in se.cited_memories(se.authored_text("\n".join(window))):
                counts[name] = counts.get(name, 0) + 1
                if stamp and stamp > last.get(name, ""):
                    last[name] = stamp
            window.clear()

        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    window.append(line.rstrip("\n"))
                    continue
                if not isinstance(rec, dict):
                    continue
                uid = rec.get("uuid")
                if isinstance(uid, str):
                    if uid in seen_uuids:
                        continue
                    seen_uuids.add(uid)
                ts = rec.get("timestamp")
                if isinstance(ts, str) and len(ts) >= 10:
                    stamp = ts[:10]
                window.append(line.rstrip("\n"))
                if _is_stop_boundary(rec):
                    flush()
        flush()
        after = (p.stat().st_size, p.stat().st_mtime_ns)
        if after != before:
            print(f"ERROR: {p.name} was rewritten while it was being read. Re-run with no "
                  f"Claude session active.")
            return 1
        print(f"  scanned {p.name}")

    files = sorted(MEMORY_DIR.glob("*.md"))
    changes = []
    for path in files:
        m = re.match(r"---\n(.*?)\n---\n(.*)", path.read_text(), re.DOTALL)
        if not m:
            continue
        fm = dict(line.split(":", 1) for line in m.group(1).split("\n") if ":" in line)
        changes.append((path, fm.get("hits", "").strip(), counts.get(path.name, 0),
                        last.get(path.name, "null")))

    # The index names every memory, so it is always the maximum; leave it out of
    # the summary so the numbers describe the memories themselves.
    rows = [c for c in changes if c[0].name != "MEMORY.md"]
    olds = sorted(int(o) for _p, o, _n, _l in rows if o.isdigit())
    news = sorted(n for _p, _o, n, _l in rows)
    print(f"\nStop-run windows: {windows}   unique records: {len(seen_uuids):,}")
    if olds:
        print(f"hits before: median {statistics.median(olds)}, max {olds[-1]}")
    if news:
        print(f"hits after:  median {statistics.median(news)}, max {news[-1]}, "
              f"zero {sum(1 for n in news if n == 0)} of {len(news)}")
    print(f"distinct last_applied after: {len({la for *_x, la in rows})}")
    if not apply:
        print("\nDRY RUN. Re-run with --apply to write (backs up the memory dir first).")
        return 0

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = ORCHESTRATOR_DIR / "transcripts" / ".memory-backups"  # gitignored with transcripts/
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"memory-pre-recount-{ts}.tgz"
    with tarfile.open(backup, "w:gz") as tf:
        tf.add(MEMORY_DIR, arcname="memory")
    backup.chmod(0o600)
    print(f"\nbackup: {backup}")

    written = 0
    for path, _o, new_h, new_la in changes:
        m = re.match(r"---\n(.*?)\n---\n(.*)", path.read_text(), re.DOTALL)
        out = []
        seen_h = False
        seen_l = False
        for line in m.group(1).split("\n"):
            if line.startswith("hits:"):
                out.append(f"hits: {new_h}")
                seen_h = True
            elif line.startswith("last_applied:"):
                out.append(f"last_applied: {new_la}")
                seen_l = True
            else:
                out.append(line)
        if not seen_h:
            out.append(f"hits: {new_h}")
        if not seen_l:
            out.append(f"last_applied: {new_la}")
        path.write_text("---\n" + "\n".join(out) + "\n---\n" + m.group(2))
        written += 1
    print(f"wrote {written} memory files")
    return _seed_watermarks()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--seed-watermarks", action="store_true",
                    help="one-time: treat existing archived transcripts as already counted")
    ap.add_argument("--recount", action="store_true",
                    help="recompute hits/last_applied from all archives with the current filter (dry run)")
    ap.add_argument("--apply", action="store_true", help="with --recount: write the result")
    args = ap.parse_args()
    if args.apply and not args.recount:
        ap.error("--apply only applies to --recount")
    if args.self_test:
        return _self_test()
    if args.seed_watermarks:
        return _seed_watermarks()
    if args.recount:
        return _recount(args.apply)
    return _report()


if __name__ == "__main__":
    raise SystemExit(main())
