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
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))

from scrubber import scrub, scrub_collect  # noqa: E402
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


def strong(v: str) -> bool:
    """A context-proven value safe to hunt store-wide: 6+ chars with a digit or
    a symbol. Shapes that are code or placeholders rather than secrets are
    never hunted, because a store-wide rewrite of them is destructive and
    permanent once the backups go: identifiers (word chars with '_'), lowercase
    kebab/dotted names, key=value fragments, ${...}/{{...}}/[...]/<...> templates, runs of one character, and
    our own [REDACTED-...] placeholders."""
    if len(v) < 6 or "[REDACTED" in v:
        return False
    if not re.search(r"[0-9]|[^A-Za-z0-9_]", v):
        return False
    # snake_case code identifiers (single-case); a mixed-case one with a digit,
    # like Summer_2024x, is a password shape and is hunted.
    if re.fullmatch(r"[a-z0-9_]+|[A-Z0-9_]+", v) and "_" in v:
        return False
    if re.fullmatch(r"(.)\1*", v) or not re.search(r"[A-Za-z0-9]", v):
        return False
    # Lowercase kebab/dotted names (model names, hostnames, bundle ids) and
    # short lowercase key=N fragments. Measured on the live store: these were
    # the only false positives among 106 learned values, hitting 129 rows.
    # A proven value declined here is never excused: the unhunted gate fails
    # the run while it sits bare in a live row.
    if re.fullmatch(r"[a-z0-9]+([-.][a-z0-9]+)+", v):
        return False
    if re.fullmatch(r"[a-z_]+=[A-Za-z0-9]{1,4}", v):
        return False
    if re.search(r"\$\{|\{\{|%\(|^\[.*\]|^<.*>$|^\$[A-Z_][A-Z0-9_]*$", v):
        return False
    return True


def variants(v: str) -> set[str]:
    """The value as stored raw and as it appears JSON-escaped (with and
    without escaped slashes), so neither the hunt nor the byte gate misses a
    transcript copy."""
    esc = json.dumps(v)[1:-1]
    return {v, esc, esc.replace("/", "\\/")}


def hunt_re(v: str) -> re.Pattern[str]:
    """v at token boundaries only (a JSON-escaped \\n/\\t/\\r counts as a
    boundary). An embedded copy is left in place and fails the leftover and
    byte gates instead: rewriting inside a longer word can corrupt code."""
    return re.compile(
        r"(?:(?<![A-Za-z0-9_])|(?<=\\n)|(?<=\\t)|(?<=\\r))"
        + re.escape(v)
        + r"(?![A-Za-z0-9_])"
    )


def checkable(v: str) -> bool:
    """A removed value worth byte-checking: 6+ chars with a digit or a symbol
    (short passwords and URL passwords included); plain words are skipped."""
    if len(v) < 6 or v.startswith("[REDACTED"):
        return False
    return any(ch.isdigit() for ch in v) or bool(re.search(r"[^A-Za-z0-9_]", v))


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
    ap.add_argument(
        "--seed-from",
        metavar="SNAPSHOT",
        help="an earlier (pre-scrub) snapshot to learn context-proven secrets from; "
        "needed when a previous run already redacted the rows that proved them",
    )
    ap.add_argument(
        "--accept-unhunted",
        type=int,
        default=0,
        metavar="N",
        help="accept exactly N proven values that are not hunted (strong() rejects their shape) but still "
        "sit bare in live rows, after reviewing the masked shapes the run prints (default 0: any fails)",
    )
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

    seeded: set[str] = set()
    seed_removed: set[str] = set()
    # Every value a context rule proved to be a secret, hunted or not.
    proven: set[str] = set()
    seeds = []
    if args.seed_from:
        seed = Path(args.seed_from).expanduser().resolve()
        if not seed.is_file():
            print(f"[rescrub] --seed-from snapshot not found: {seed}", file=sys.stderr)
            return 2
        seeds.append(seed)
    # This runbook's own earlier snapshots hold the pre-scrub text, so a re-run
    # still knows every value an earlier run proved and redacted.
    seeds += [Path(b).resolve() for b in sorted(glob.glob(str(db) + ".bak-rescrub-*"))]
    for seed in dict.fromkeys(seeds):
        # as_uri() percent-encodes '?' and '#', so mode=ro can't be cut off.
        snap = sqlite3.connect(f"{seed.as_uri()}?mode=ro", uri=True)
        for (t,) in snap.execute("SELECT text FROM chunks"):
            new, known = scrub_collect(t)
            if new != t:
                proven |= known
                seeded |= {v for v in known if strong(v)} | {v for v in removed_values(t, new) if secret_like(v)}
                seed_removed |= {v for v in removed_values(t, new) if checkable(v)}
        snap.close()
    if seeds:
        print(f"[rescrub] seeded {len(seeded)} known secret values from {len(set(seeds))} snapshot(s)")
    # Pass 1: scrub each row. Pass 2: a value redacted in one row (where its
    # context gave it away) is replaced in every other row too; the scrubber
    # sees one chunk at a time, so it can't do this itself.
    scrubbed = {}
    hunt: set[str] = set(seeded)
    for rowid, cid, stype, spath, start, end, text in rows:
        new, known = scrub_collect(text)
        if new != text:
            scrubbed[rowid] = new
            hunt |= {v for v in removed_values(text, new) if secret_like(v)}
            # Values redacted because of their context (after a password
            # key, in a URL, a CLI flag...) are secrets wherever they appear.
            hunt |= {v for v in known if strong(v)}
            proven |= known
    changed = []
    kinds: collections.Counter[str] = collections.Counter()
    by_source: collections.Counter[str] = collections.Counter()
    cross_row = 0
    for rowid, cid, stype, spath, start, end, text in rows:
        new = scrubbed.get(rowid, text)
        before = new
        for v in sorted(hunt, key=len, reverse=True):
            for form in variants(v):
                if form in new:
                    new = hunt_re(form).sub("[REDACTED-SECRET]", new)
        if new != before and rowid not in scrubbed:
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
    pending = []
    if conn.execute("SELECT name FROM sqlite_master WHERE name = '_rescrub_pending'").fetchone():
        pending = [r for (r,) in conn.execute("SELECT rowid FROM _rescrub_pending")]
    if pending:
        print(f"[rescrub] rows with a stale vector from an earlier run: {len(pending)}")
    if not args.apply:
        print("[rescrub] dry run: nothing written")
        return 1 if pending else 0
    rewrote = raced = refreshed = 0
    removed: set[str] = set()
    if changed or pending:
        res = rewrite(conn, db, args, changed, pending)
        if isinstance(res, int):
            return res
        rewrote, raced, refreshed, removed = res
    else:
        # Still run every gate: "nothing to change" must not mean "clean".
        print("[rescrub] nothing to rewrite; running the gates")
    return gates(conn, store, db, hunt, proven, removed | seed_removed, rewrote, raced, refreshed, args.accept_unhunted)


def rewrite(conn, db: Path, args, changed: list, pending: list):
    """Snapshot, then rewrite and re-embed the changed rows. Returns an exit
    code on a preflight failure, else (rewrote, raced, refreshed, removed)."""

    # Preflight: refuse to write anything if the embedder is down.
    from embedder import Embedder as _PreflightEmbedder  # noqa: E402

    probe = _PreflightEmbedder().embed_batch(["rescrub preflight"])
    if not probe or not any(probe[0]):
        print("[rescrub] embedder preflight FAILED (zero vector): nothing written; start the embedder and re-run")
        return 3

    # Nanosecond suffix: two runs in the same second must not collide (O_EXCL).
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{time.time_ns() % 10**9:09d}"
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
    conn.execute("CREATE TABLE IF NOT EXISTS _rescrub_pending (rowid INTEGER PRIMARY KEY)")
    conn.commit()
    rewrote = vector_pending = raced = 0
    removed: set[str] = set()
    refreshed = 0
    if pending:
        rows_p = conn.execute(
            f"SELECT rowid, text FROM chunks WHERE rowid IN ({','.join('?' * len(pending))})", pending
        ).fetchall()
        for i in range(0, len(rows_p), args.batch):
            part = rows_p[i : i + args.batch]
            for (rid, text), vec in zip(part, embedder.embed_batch([t for _, t in part])):
                if any(vec):
                    conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rid,))
                    conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)", (rid, _vec_to_blob(vec)))
                    conn.execute("DELETE FROM _rescrub_pending WHERE rowid = ?", (rid,))
                    refreshed += 1
        # rows that vanished (re-indexed) need no refresh
        conn.execute("DELETE FROM _rescrub_pending WHERE rowid NOT IN (SELECT rowid FROM chunks)")
        conn.commit()
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
                # Embedder went down mid-run: the text is scrubbed now; the
                # stale vector is recorded and refreshed on the next run.
                conn.execute("INSERT OR IGNORE INTO _rescrub_pending(rowid) VALUES (?)", (rowid,))
                vector_pending += 1
            removed |= removed_values(old, new)
            rewrote += 1
        conn.commit()
    return rewrote, raced, refreshed, removed


def mask(v: str) -> str:
    """A value's shape with no content: letters -> a, digits -> 9, symbols kept."""
    return re.sub(r"[A-Za-z]", "a", re.sub(r"[0-9]", "9", v))


def gates(
    conn, store, db: Path, hunt: set[str], proven: set[str], removed: set[str],
    rewrote: int, raced: int, refreshed: int, accept_unhunted: int = 0,
) -> int:
    """The acceptance gates, run on every --apply. Counts only are printed."""
    live = [t for (t,) in conn.execute("SELECT text FROM chunks")]
    hunted = {form for v in hunt for form in variants(v)}
    left = sum(1 for t in live if scrub(t) != t or any(f in t for f in hunted))
    # Byte-check the hunted secrets, plus every other removed value that is
    # 6+ chars with a digit or symbol. A removed fragment that still appears in
    # a live row is ordinary text the scrubber keeps (the leftover gate judges
    # live rows), so only its absence from the rest of the file is checked.
    live_blob = "\x00".join(live)
    byte_set = hunted | {f for v in removed if checkable(v) for f in variants(v) if f not in live_blob}
    # A proven value strong() won't hunt (its shape is also code-like) must not
    # pass silently: if any copy is still in a live row, it is surfaced as a
    # masked shape and fails the run unless the operator accepts that count.
    # Anywhere else in the file it is byte-checked like a hunted one.
    unhunted = []
    for v in sorted(proven - hunt):
        forms = [f for f in variants(v) if f in live_blob]
        if forms:
            # Any copy counts, glued ones too: a proven value is never excused.
            n = sum(1 for t in live if any(f in t for f in forms))
            if n:
                unhunted.append((mask(v), n))
        byte_set |= {f for f in variants(v) if f not in live_blob}
    for shape, n in unhunted:
        print(f"[rescrub] proven but not hunted, bare in live rows: shape={shape} rows={n}")
    has_pending = conn.execute("SELECT 1 FROM sqlite_master WHERE name = '_rescrub_pending'").fetchone()
    still_pending = conn.execute("SELECT count(*) FROM _rescrub_pending").fetchone()[0] if has_pending else 0
    if not still_pending:
        conn.execute("DROP TABLE IF EXISTS _rescrub_pending")
    conn.commit()
    vacuumed = True
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError as err:
        vacuumed = False
        print(f"[rescrub] VACUUM failed ({err}); freed pages may still hold old text. Re-run when no session holds the store.")
    # Every chunk must still have its vector (rowids are not guaranteed stable
    # across VACUUM without an INTEGER PRIMARY KEY).
    # After VACUUM no free page may remain: free pages are where deleted rows'
    # old text survives, and the byte check can't tell them apart.
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    orphans = conn.execute(
        "SELECT count(*) FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM vec_chunks v WHERE v.rowid = c.rowid)"
    ).fetchone()[0]
    store.close()
    # Byte-check the values known to be secrets: the context-proven ones and
    # the secret-shaped ones. Fragments a redaction swallowed (a key name, a
    # "-----BEGIN" header) legitimately appear elsewhere and are not checked.
    in_bytes = byte_check(db, byte_set)
    print(
        f"[rescrub] rewrote={rewrote} raced={raced} refreshed={refreshed} vector_pending={still_pending} "
        f"remaining_unscrubbed_rows={left} vacuumed={vacuumed} freelist_pages={freelist} chunks_without_vector={orphans} "
        f"removed_values_still_in_file_bytes={in_bytes} proven_values_not_hunted_but_bare={len(unhunted)} "
        f"accepted={accept_unhunted}"
    )
    ok = len(unhunted) == accept_unhunted and left == 0 and in_bytes == 0 and still_pending == 0 and vacuumed and freelist == 0 and orphans == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
