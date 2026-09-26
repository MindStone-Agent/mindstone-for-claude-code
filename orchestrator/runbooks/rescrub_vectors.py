#!/usr/bin/env python3
"""One-time rescrub of an existing vectors.db with the current scrubber (MS4CC#117).

Rows indexed before a pattern existed keep the secret in `chunks.text`, which
is what recall returns. This rewrites those rows through scrub() and
re-embeds them. It is a security redaction, not a capacity trim: no row is
deleted, and only text that matches a secret shape changes.

Output is COUNT-ONLY. No chunk text or matched value is ever printed.

  dry run (default):  python orchestrator/runbooks/rescrub_vectors.py
  apply:              python orchestrator/runbooks/rescrub_vectors.py --apply

--apply copies the DB to `<db>.bak-rescrub-<UTC>` first (a 600-mode file; it
still holds the unscrubbed text, so delete it once the rescan is clean).
Idempotent: a second run finds nothing to change.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import os
import re
import shutil
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))

from scrubber import scrub  # noqa: E402
from vectorstore import VectorStore, _vec_to_blob  # noqa: E402

PLACEHOLDER = re.compile(r"\[REDACTED-[A-Z0-9-]+\]")
# The pre-#117 generic rule had no word boundary, so prose like "task-manage…"
# became "ta[REDACTED-OPENAI-KEY]". Counted (not repaired: the original text
# is gone from the store; re-indexing the source restores it).
LEGACY_GLUED = re.compile(r"[A-Za-z]\[REDACTED-OPENAI-KEY\]")


def chunk_id(source_path: str, start: int, end: int, text: str) -> str:
    # Same formula as vectorstore.Chunk.compute_id.
    h = hashlib.sha256()
    h.update(source_path.encode())
    h.update(f":{start}-{end}:".encode())
    h.update(text.encode())
    return h.hexdigest()[:32]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "vectors.db"))
    ap.add_argument("--apply", action="store_true", help="snapshot, rewrite and re-embed (default: dry run)")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[rescrub] no DB at {db}", file=sys.stderr)
        return 2

    store = VectorStore(db)
    conn = store._conn_or_init()
    rows = conn.execute(
        "SELECT rowid, chunk_id, source_type, source_path, start_line, end_line, text FROM chunks"
    ).fetchall()

    changed = []
    kinds: collections.Counter[str] = collections.Counter()
    by_source: collections.Counter[str] = collections.Counter()
    legacy_glued = 0
    for rowid, cid, stype, spath, start, end, text in rows:
        legacy_glued += len(LEGACY_GLUED.findall(text))
        new = scrub(text)
        if new == text:
            continue
        before = collections.Counter(PLACEHOLDER.findall(text))
        after = collections.Counter(PLACEHOLDER.findall(new))
        kinds.update(after - before)
        by_source[stype] += 1
        changed.append((rowid, cid, spath, start, end, new))

    print(f"[rescrub] chunks={len(rows)} to_rewrite={len(changed)} by_source={dict(by_source)}")
    print(f"[rescrub] new redactions by kind={dict(kinds)}")
    print(f"[rescrub] legacy prose damage (old unanchored sk- rule), occurrences={legacy_glued}")
    if not args.apply or not changed:
        print("[rescrub] dry run: nothing written" if not args.apply else "[rescrub] nothing to do")
        return 0

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = db.with_name(f"{db.name}.bak-rescrub-{stamp}")
    conn.commit()
    shutil.copy2(db, backup)
    os.chmod(backup, 0o600)
    print(f"[rescrub] snapshot: {backup}")

    from embedder import Embedder  # lazy: dry runs need no embedder

    embedder = Embedder()
    done = 0
    for i in range(0, len(changed), args.batch):
        batch = changed[i : i + args.batch]
        vectors = embedder.embed_batch([t for *_, t in batch])
        for (rowid, cid, spath, start, end, new), vec in zip(batch, vectors):
            new_id = chunk_id(spath, start, end, new)
            clash = conn.execute("SELECT 1 FROM chunks WHERE chunk_id = ? AND rowid != ?", (new_id, rowid)).fetchone()
            conn.execute(
                "UPDATE chunks SET text = ?, chunk_id = ? WHERE rowid = ?",
                (new, cid if clash else new_id, rowid),
            )
            conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rowid,))
            conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)", (rowid, _vec_to_blob(vec)))
            done += 1
        conn.commit()
    left = sum(1 for (t,) in conn.execute("SELECT text FROM chunks") if scrub(t) != t)
    print(f"[rescrub] rewrote={done} remaining_unscrubbed={left}")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
