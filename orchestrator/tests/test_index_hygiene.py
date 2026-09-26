#!/usr/bin/env python3
"""Regression test: index hygiene (#111).

1. A deleted memory file's chunks are removed from the store. Dropping only its
   hash entry left them recallable forever (design_synapse.md, deleted, still
   had 4 chunks).
2. The post-compaction deferred embed picks the newest SESSION archive
   (<uuid>.jsonl), never recall_usage.jsonl or a dated copy, both of which also
   match *.jsonl and one of which is appended on every recall.

Run with the orchestrator venv (needs sqlite-vec), no network:
    orchestrator/.venv/bin/python orchestrator/tests/test_index_hygiene.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import session_end as se  # noqa: E402
import session_start as ss  # noqa: E402
from vectorstore import Chunk, VectorStore  # noqa: E402

DIMS = 768


def main() -> int:
    passed = failed = 0

    def check(desc, cond):
        nonlocal passed, failed
        if cond:
            print(f"  ok   - {desc}")
            passed += 1
        else:
            print(f"  FAIL - {desc}")
            failed += 1

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mem = tmp / "memory"
        mem.mkdir()
        other = tmp / "elsewhere"
        other.mkdir()
        store = VectorStore(tmp / "v.db")
        store.init_schema()
        vec = [1.0] + [0.0] * (DIMS - 1)

        def add(path: Path):
            path.write_text("x")
            store.upsert([Chunk(source_type="memory", source_path=str(path), start_line=1,
                                end_line=1, text=f"body of {path.name}", metadata={})], [vec])

        kept, gone, foreign = mem / "feedback_kept.md", mem / "feedback_gone.md", other / "feedback_x.md"
        for p in (kept, gone, foreign):
            add(p)
        gone.unlink()
        foreign.unlink()  # deleted, but not under memory_dir: must be left alone

        def stored():
            return {r[0] for r in store._conn_or_init().execute("SELECT source_path FROM chunks")}

        live = {str(p) for p in mem.glob("*.md")}
        n = se.prune_deleted_memory_chunks(store, mem, stored(), live)
        check("a deleted memory's chunks are removed", str(gone) not in stored() and n == 1)
        check("  a live memory's chunks are kept", str(kept) in stored())
        check("  chunks outside memory_dir are never touched", str(foreign) in stored())
        # The race: present when `live` was taken, briefly missing now (a non-atomic
        # save). Its hash is still in state, so its chunks must stay too.
        kept.unlink()
        se.prune_deleted_memory_chunks(store, mem, stored(), live)
        check("a file missing only AFTER the live snapshot keeps its chunks", str(kept) in stored())
        kept.write_text("x")

        # Wiring: through the real reindex_changed_memory, with a stub embedder.
        import embedder as emb

        class Stub:
            def __init__(self, *a, **k):
                self.stats = {"truncated": 0, "failed": 0}

            def embed_batch(self, texts):
                return [vec for _ in texts]

        saved = (emb.Embedder, se.MEMORY_DIR, se.DB_PATH, se.STATE_PATH)
        emb.Embedder = Stub
        wmem = tmp / "wmem"
        wmem.mkdir()
        se.MEMORY_DIR, se.DB_PATH, se.STATE_PATH = wmem, tmp / "w.db", tmp / "w-state.json"
        try:
            a, b = wmem / "feedback_a.md", wmem / "feedback_b.md"
            a.write_text("---\nname: a\n---\n\n# A\n\nbody a\n")
            se.reindex_changed_memory()
            a.rename(b)
            se.reindex_changed_memory()
            ws = VectorStore(tmp / "w.db")
            wpaths = {r[0] for r in ws._conn_or_init().execute("SELECT source_path FROM chunks")}
            check("wiring: a RENAMED memory keeps its chunks under the new name", str(b) in wpaths)
            check("  and the old name's chunks are pruned", str(a) not in wpaths)
            b.unlink()
            se.reindex_changed_memory()
            wpaths = {r[0] for r in ws._conn_or_init().execute("SELECT source_path FROM chunks")}
            check("wiring: a DELETED memory's chunks are pruned by the checkpoint path", not wpaths)
        finally:
            emb.Embedder, se.MEMORY_DIR, se.DB_PATH, se.STATE_PATH = saved

        tr = tmp / "transcripts"
        tr.mkdir()
        session = tr / "35a6d93a-eb5c-45fa-9ccb-75c80cb8ab82.jsonl"
        session.write_text("{}\n")
        time.sleep(0.01)
        (tr / "2026-09-25__35a6d93a-eb5c-45fa-9ccb-75c80cb8ab82.jsonl").write_text("{}\n")
        time.sleep(0.01)
        (tr / "recall_usage.jsonl").write_text("{}\n")  # newest by mtime
        check("the deferred embed picks the session archive, not recall_usage.jsonl",
              ss.newest_session_archive(tr) == session)
        check("  and returns None when there is no session archive",
              ss.newest_session_archive(tmp / "memory") is None)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
