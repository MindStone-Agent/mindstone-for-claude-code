#!/usr/bin/env python3
"""Regression test: a failed embed must never become a match (#111 M9/M13).

On a non-length error the embedder returns a zero vector rather than raising
(it must never throw inside a hook). A zero vector sits at L2 distance 1.0 from
every unit vector, similarity exactly 0.500, which passes the recall floor. So:
  - as a QUERY it matched everything equally: an Ollama outage would inject
    arbitrary chunks that looked like real recall;
  - STORED, it matched every future query forever, and the resume point / file
    hash moved past it, so it was never retried.

This drives the real VectorStore (sqlite-vec) and Indexer with an embedder that
fails on chosen texts. Standalone (no pytest, no network):
    python3 test_embed_failure_fails_closed.py
"""
import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from indexer import Indexer  # noqa: E402
from vectorstore import VectorStore  # noqa: E402

DIMS = 768


def _unit(text: str) -> list[float]:
    """A deterministic unit vector for `text`."""
    seed = hashlib.sha256(text.encode()).digest()
    raw = [((seed[i % 32] + i * 7) % 251) - 125.0 for i in range(DIMS)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


class FlakyEmbedder:
    """Embeds normally, except texts containing a marker in `fail_on`.

    With outage=True, everything from the first failing text onward fails too:
    what a down embedder looks like mid-pass.
    """

    def __init__(self, fail_on=(), outage=False):
        self.fail_on = set(fail_on)
        self.outage = outage
        self.down = False
        self.stats = {"truncated": 0, "failed": 0}

    def _one(self, text):
        if self.down or any(m in text for m in self.fail_on):
            self.down = self.outage
            self.stats["failed"] += 1
            return [0.0] * DIMS  # exactly what the real embedder returns on failure
        return _unit(text)

    def embed(self, text):
        return self._one(text)

    def embed_batch(self, texts):
        return [self._one(t) for t in texts]


def _turn(role, text):
    return json.dumps({"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}})


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
        store = VectorStore(tmp / "v.db")
        store.init_schema()

        # --- 1. A zero QUERY returns nothing, not the whole store at 0.500.
        good = FlakyEmbedder()
        mem = tmp / "feedback_alpha.md"
        mem.write_text("---\nname: alpha\n---\n\n# Alpha\n\nAlways check the thing.\n")
        Indexer(store, good, verbose=False).index_memory_file(mem)
        check("setup: the memory was indexed", store.count() > 0)
        hits = store.search([0.0] * DIMS, k=5)
        check("a zero query vector returns no results", hits == [])
        check("  and says why (degenerate_query)",
              getattr(store, "last_search_stats", {}).get("degenerate_query") is True)
        check("CONTROL a real query still returns results",
              len(store.search(_unit("Always check the thing."), k=5)) > 0)

        # --- 2. A memory file whose embed fails keeps its previous chunks and raises
        #        (so reindex_changed_memory doesn't record the hash and retries it).
        before = store.count()
        mem.write_text("---\nname: alpha\n---\n\n# Alpha\n\nEDITED and FAILMARK here.\n")
        raised = False
        try:
            Indexer(store, FlakyEmbedder(fail_on={"FAILMARK"}), verbose=False).index_memory_file(mem)
        except RuntimeError:
            raised = True
        check("a memory file with a failed embed raises", raised)
        kept = " ".join(r for (r,) in store._conn_or_init().execute(
            "SELECT text FROM chunks WHERE source_type='memory'"))
        check("  and its previous chunks are kept, not deleted",
              store.count() == before and "Always check the thing." in kept and "EDITED" not in kept)

        # --- 3. Transcripts, embedder goes DOWN mid-pass: nothing from the first
        #        failure on is stored, so the resume point stays before it and the
        #        next run retries it.
        tr = tmp / "2026-01-01__00000000-0000-0000-0000-000000000000.jsonl"
        lines = []
        for i in range(12):
            lines.append(_turn("user", f"question {i} " + ("FAILMARK" if i == 5 else "") + " x" * 60))
            lines.append(_turn("assistant", f"answer {i} " + " y" * 60))
        tr.write_text("\n".join(lines) + "\n")
        flaky = FlakyEmbedder(fail_on={"FAILMARK"}, outage=True)
        Indexer(store, flaky, verbose=False).index_transcript(tr)
        check("setup: the flaky embedder really failed", flaky.stats["failed"] >= 1)
        conn = store._conn_or_init()
        zero_rows = sum(1 for (b,) in conn.execute("SELECT embedding FROM vec_chunks")
                        if not any(__import__("struct").unpack(f"{DIMS}f", b)))
        check("no zero vector is ever stored", zero_rows == 0)
        stored_text = " ".join(t for (t,) in conn.execute(
            "SELECT text FROM chunks WHERE source_type='transcript'"))
        check("the failed chunk is not stored", "FAILMARK" not in stored_text)
        check("  and neither is anything AFTER it (resume point stays before it)",
              "question 11" not in stored_text)
        first_pass = store.max_end_line_for_source(str(tr))
        Indexer(store, FlakyEmbedder(), verbose=False).index_transcript(tr)
        stored_text = " ".join(t for (t,) in conn.execute(
            "SELECT text FROM chunks WHERE source_type='transcript'"))
        check("once the embedder recovers, the failed chunk IS indexed", "FAILMARK" in stored_text)
        check("  along with everything after it", "question 11" in stored_text)
        check("  and the resume point moved forward",
              store.max_end_line_for_source(str(tr)) > first_pass)

        # --- 4. One chunk that ALWAYS fails, with the embedder otherwise up, must
        #        not wedge the transcript (re-embedding the whole tail forever).
        store4 = VectorStore(tmp / "v4.db")
        store4.init_schema()
        tr4 = tmp / "2026-01-02__00000000-0000-0000-0000-000000000004.jsonl"
        # Turns large enough that each is its own chunk: a poisoned chunk that is
        # also the LAST chunk of a pass is indistinguishable from an outage, and
        # is (correctly) held back for retry.
        tr4.write_text("\n".join(_turn("user", f"q{i} " + ("POISON " if i == 2 else "")
                                       + " ".join(f"t{i}w{k}" for k in range(700)))
                                 for i in range(8)) + "\n")
        poison = FlakyEmbedder(fail_on={"POISON"})
        Indexer(store4, poison, verbose=False).index_transcript(tr4)
        end1 = store4.max_end_line_for_source(str(tr4))
        with tr4.open("a") as f:
            f.write(_turn("user", "q8 later turn " + " ".join(f"t8w{k}" for k in range(700))) + "\n")
        embedded_before = poison.stats["failed"]
        Indexer(store4, poison, verbose=False).index_transcript(tr4)
        text4 = " ".join(r for (r,) in store4._conn_or_init().execute("SELECT text FROM chunks"))
        check("a permanently failing chunk does not wedge the transcript",
              "q7" in text4 and "q8 later turn" in text4)
        check("  the next run did not retry the poisoned chunk",
              poison.stats["failed"] == embedded_before)
        check("  and the resume point moved past it", store4.max_end_line_for_source(str(tr4)) > end1 > 0)

        # --- 5. A long turn hard-split into chunks sharing one line number: an
        #        outage in the middle of it must not skip the rest of that turn.
        store5 = VectorStore(tmp / "v5.db")
        store5.init_schema()
        tr5 = tmp / "2026-01-03__00000000-0000-0000-0000-000000000005.jsonl"
        big = " ".join(f"w{k}" for k in range(3000)) + " SPLITFAIL " + " ".join(f"v{k}" for k in range(3000))
        tr5.write_text("\n".join([_turn("user", "short opener " + " o" * 60),
                                   _turn("assistant", big),
                                   _turn("user", "closing turn " + " c" * 60)]) + "\n")
        Indexer(store5, FlakyEmbedder(fail_on={"SPLITFAIL"}, outage=True), verbose=False).index_transcript(tr5)
        Indexer(store5, FlakyEmbedder(), verbose=False).index_transcript(tr5)
        text5 = " ".join(r for (r,) in store5._conn_or_init().execute("SELECT text FROM chunks"))
        check("after an outage mid long-turn, the WHOLE turn is indexed on recovery",
              "SPLITFAIL" in text5 and "v2999" in text5 and "closing turn" in text5)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
