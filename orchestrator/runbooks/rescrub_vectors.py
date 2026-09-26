#!/usr/bin/env python3
"""One-time rescrub of an existing vectors.db with the current scrubber (MS4CC#117).

Rows indexed before a rule existed keep the secret in `chunks.text`, which is
what recall returns. This rewrites those rows through scrub(), re-embeds them,
then VACUUMs so the old text is gone from the file's bytes too. It is a
security redaction, not a capacity trim: no row is deleted, and only text that
matches a secret shape changes.

Output is COUNT-ONLY. No chunk text or matched value is ever printed.

  dry run (default):  python orchestrator/runbooks/rescrub_vectors.py
  apply:              python orchestrator/runbooks/rescrub_vectors.py --apply

--apply:
  1. snapshots the DB (SQLite online backup) into `<db>.bak-rescrub-<UTC>`, a
     file created 0600. It holds the UNSCRUBBED text: it is gitignored, and
     every such backup should be deleted once the byte check is clean;
  2. rewrites each changed row only if it is still exactly what was read
     (rowid + chunk_id + text), so a concurrent re-index can't be clobbered;
  3. always stores the scrubbed text. The vector is replaced only when the
     embedder returns a real (non-zero) vector; otherwise the old vector stays
     and the row is reported as vector-pending (re-run to refresh it);
  4. VACUUMs (with secure_delete on) and then checks the file's BYTES for every
     removed value, reporting only how many are still present.
Idempotent: a second run finds nothing to change.
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))

from scrubber import scrub  # noqa: E402
from vectorstore import VectorStore, _vec_to_blob  # noqa: E402

PLACEHOLDER = re.compile(r"\[REDACTED-[A-Z0-9-]+\]")
TOKENISH = re.compile(r"[^\s\"'`<>(){}\[\],;]{8,}")


def chunk_id(source_path: str, start: int, end: int, text: str) -> str:
    # Same formula as vectorstore.Chunk.compute_id.
    h = hashlib.sha256()
    h.update(source_path.encode())
    h.update(f":{start}-{end}:".encode())
    h.update(text.encode())
    return h.hexdigest()[:32]


def removed_values(old: str, new: str) -> set[str]:
    """Token-ish runs present in the old text but gone from the new one: the
    values the scrub removed (used only for the byte check, never printed)."""
    return {t for t in TOKENISH.findall(old) if t not in new and not t.startswith("[REDACTED")}


def secret_like(v: str) -> bool:
    """A removed value worth hunting across the whole store: one that looks
    like a credential on its own (the scrubber's bare high-entropy shape, or
    32+ hex), so paths, model names and key names are never hunted."""
    from scrubber import _BARE_MIN_ENTROPY, _BARE_MIN_SWITCH_RATE, _entropy, _switch_rate

    if re.fullmatch(r"[0-9a-f]{32,128}", v):
        return True
    if len(v) < 20 or "/" in v or "." in v:
        return False
    if not (re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and sum(ch.isdigit() for ch in v) >= 2):
        return False
    return _entropy(v) >= _BARE_MIN_ENTROPY and _switch_rate(v) >= _BARE_MIN_SWITCH_RATE


def byte_check(db: Path, values: set[str]) -> int:
    """How many removed values still occur anywhere in the DB file bytes
    (including a leftover -journal/-wal). Count only."""
    blobs = []
    for f in [db, *(Path(p) for p in glob.glob(str(db) + "-*")) ]:
        if f.exists() and not f.name.count(".bak-"):
            blobs.append(f.read_bytes())
    data = b"\x00".join(blobs)
    return sum(1 for v in values if v.encode("utf-8", "ignore") in data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "vectors.db"))
    ap.add_argument("--apply", action="store_true", help="snapshot, rewrite, re-embed, VACUUM, byte-check (default: dry run)")
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

    # Pass 1: scrub each row. Pass 2: a value redacted in one row (where its
    # context gave it away) is replaced in every other row too; the scrubber
    # sees one chunk at a time, so it can't do this itself.
    scrubbed = {}
    hunt: set[str] = set()
    for rowid, cid, stype, spath, start, end, text in rows:
        new = scrub(text)
        if new != text:
            scrubbed[rowid] = new
            hunt |= {v for v in removed_values(text, new) if secret_like(v)}
    changed = []
    kinds: collections.Counter[str] = collections.Counter()
    by_source: collections.Counter[str] = collections.Counter()
    cross_row = 0
    for rowid, cid, stype, spath, start, end, text in rows:
        new = scrubbed.get(rowid, text)
        for v in sorted(hunt, key=len, reverse=True):
            if v in new:
                new = new.replace(v, "[REDACTED-SECRET]")
                if rowid not in scrubbed:
                    cross_row += 1
        if new == text:
            continue
        kinds.update(collections.Counter(PLACEHOLDER.findall(new)) - collections.Counter(PLACEHOLDER.findall(text)))
        by_source[stype] += 1
        changed.append((rowid, cid, spath, start, end, text, new))

    backups = sorted(glob.glob(str(db) + ".bak-rescrub-*"))
    print(f"[rescrub] chunks={len(rows)} to_rewrite={len(changed)} by_source={dict(by_source)}")
    print(f"[rescrub] new redactions by kind={dict(kinds)}")
    print(f"[rescrub] secret-like values hunted across rows={len(hunt)}; rows changed only by that hunt={cross_row}")
    if backups:
        print(f"[rescrub] existing raw backups: {len(backups)} (delete them all once the byte check is clean)")
    if not args.apply or not changed:
        print("[rescrub] dry run: nothing written" if not args.apply else "[rescrub] nothing to do")
        return 0

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = db.with_name(f"{db.name}.bak-rescrub-{stamp}")
    conn.commit()
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    dest = sqlite3.connect(backup)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.chmod(backup, 0o600)
    print(f"[rescrub] snapshot: {backup}")

    from embedder import Embedder  # lazy: dry runs need no embedder

    embedder = Embedder()
    rewrote = vector_pending = raced = 0
    removed: set[str] = set()
    for i in range(0, len(changed), args.batch):
        batch = changed[i : i + args.batch]
        vectors = embedder.embed_batch([new for *_, new in batch])
        for (rowid, cid, spath, start, end, old, new), vec in zip(batch, vectors):
            new_id = chunk_id(spath, start, end, new)
            clash = conn.execute("SELECT 1 FROM chunks WHERE chunk_id = ? AND rowid != ?", (new_id, rowid)).fetchone()
            cur = conn.execute(
                "UPDATE chunks SET text = ?, chunk_id = ? WHERE rowid = ? AND chunk_id = ? AND text = ?",
                (new, cid if clash else new_id, rowid, cid, old),
            )
            if cur.rowcount != 1:
                raced += 1  # re-indexed since it was read; the new row went through upsert's scrub
                continue
            if any(vec):
                conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rowid,))
                conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)", (rowid, _vec_to_blob(vec)))
            else:
                vector_pending += 1  # embedder down: text is scrubbed, vector refreshes on a re-run
            removed |= removed_values(old, new)
            rewrote += 1
        conn.commit()

    left = sum(
        1 for (t,) in conn.execute("SELECT text FROM chunks") if scrub(t) != t or any(v in t for v in hunt)
    )
    conn.execute("VACUUM")
    store.close()
    # Only secret-shaped removed values are checked: a key name or prose word
    # that a redaction swallowed can legitimately appear elsewhere.
    in_bytes = byte_check(db, {v for v in removed if secret_like(v)} | hunt)
    print(
        f"[rescrub] rewrote={rewrote} raced={raced} vector_pending={vector_pending} "
        f"remaining_unscrubbed_rows={left} secret_like_values_still_in_file_bytes={in_bytes}"
    )
    return 0 if left == 0 and in_bytes == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
