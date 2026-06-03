"""SQLite + sqlite-vec vector store for MindStone-for-Claude-Code.

Two tables:
    chunks — the actual text + metadata (source file, line range, chunk hash)
    vec_chunks — sqlite-vec virtual table holding the embeddings

Source types supported:
    - memory  : markdown file from orchestrator/memory/
    - transcript : a chunk of a session JSONL
    - identity : IDENTITY.md, USER.md (typically not queried but can be)

Search returns chunks ranked by cosine similarity, with optional MMR
diversification to avoid near-duplicate results.

Usage:
    from vectorstore import VectorStore
    store = VectorStore(db_path="orchestrator/vectors.db")
    store.init_schema()
    store.upsert(chunks, vectors)           # parallel lists
    results = store.search(query_vector, k=10, mmr=True)
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import sqlite_vec

EMBEDDING_DIMS = 768  # nomic-embed-text via local Ollama (8K context)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """A unit of embedded content."""
    source_type: str          # "memory" | "transcript" | "identity"
    source_path: str          # absolute or repo-relative path
    start_line: int           # 1-indexed; 0 for whole-file or non-line-structured
    end_line: int             # inclusive
    text: str
    metadata: dict = field(default_factory=dict)  # extra: session_id, role, etc.
    chunk_id: str | None = None   # auto-derived from content hash if not set

    def compute_id(self) -> str:
        """Deterministic ID from source path + line range + content hash."""
        h = hashlib.sha256()
        h.update(self.source_path.encode())
        h.update(f":{self.start_line}-{self.end_line}:".encode())
        h.update(self.text.encode())
        return h.hexdigest()[:32]

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class VectorStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _conn_or_init(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_schema(self):
        conn = self._conn_or_init()
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_source_path ON chunks(source_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_source_type ON chunks(source_type);

            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                embedding float[{EMBEDDING_DIMS}]
            );
        """)
        conn.commit()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """Upsert chunks + vectors. Returns number of new (not-already-indexed) rows."""
        if len(chunks) != len(vectors):
            raise ValueError(f"chunks ({len(chunks)}) / vectors ({len(vectors)}) length mismatch")
        if not chunks:
            return 0

        conn = self._conn_or_init()
        import time
        now = int(time.time())

        inserted = 0
        for chunk, vec in zip(chunks, vectors):
            chunk_id = chunk.chunk_id or chunk.compute_id()
            # Check if already indexed
            existing = conn.execute(
                "SELECT chunk_id FROM chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE chunks SET last_seen_at = ? WHERE chunk_id = ?",
                    (now, chunk_id),
                )
            else:
                conn.execute(
                    """INSERT INTO chunks
                    (chunk_id, source_type, source_path, start_line, end_line, text, metadata_json, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        chunk.source_type,
                        chunk.source_path,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.text,
                        json.dumps(chunk.metadata),
                        now,
                        now,
                    ),
                )
                # Insert into vec table — need rowid bridge
                rowid = conn.execute(
                    "SELECT rowid FROM chunks WHERE chunk_id = ?", (chunk_id,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                    (rowid, _vec_to_blob(vec)),
                )
                inserted += 1

        conn.commit()
        return inserted

    def delete_by_source_path(self, source_path: str) -> int:
        """Remove all chunks for a given source path. Useful when a file changes."""
        conn = self._conn_or_init()
        # Get rowids first so we can clean the vec table too
        rows = conn.execute(
            "SELECT rowid FROM chunks WHERE source_path = ?", (source_path,)
        ).fetchall()
        if not rows:
            return 0
        rowids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(rowids))
        conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})", rowids)
        conn.execute(f"DELETE FROM chunks WHERE rowid IN ({placeholders})", rowids)
        conn.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def count(self, source_type: str | None = None) -> int:
        conn = self._conn_or_init()
        if source_type:
            r = conn.execute("SELECT COUNT(*) FROM chunks WHERE source_type = ?", (source_type,)).fetchone()
        else:
            r = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return r[0] if r else 0

    def max_end_line_for_source(self, source_path: str) -> int:
        """Highest end_line already stored for a source path (0 if none).

        Used for incremental transcript indexing: the next index pass embeds only
        the lines after this point. Append-only transcripts never rewrite earlier
        lines, so resuming from max(end_line) is exact — no overlap, no gap.
        """
        conn = self._conn_or_init()
        r = conn.execute(
            "SELECT MAX(end_line) FROM chunks WHERE source_path = ?", (source_path,)
        ).fetchone()
        return (r[0] or 0) if r else 0

    def search(
        self,
        query_vec: list[float],
        k: int = 10,
        source_types: list[str] | None = None,
        mmr: bool = False,
        mmr_lambda: float = 0.7,
    ) -> list[dict]:
        """Cosine-similarity search.

        If `mmr=True`, applies Maximal Marginal Relevance to diversify results.
        `mmr_lambda` ∈ [0,1]: 1.0 = pure similarity, 0.0 = pure diversity.
        """
        conn = self._conn_or_init()
        blob = _vec_to_blob(query_vec)

        # Pull a larger candidate set if MMR is requested.
        candidates_k = k * 3 if mmr else k

        where = ""
        params: list = [blob, candidates_k]
        if source_types:
            placeholders = ",".join("?" * len(source_types))
            where = f"AND c.source_type IN ({placeholders})"
            params = [blob, candidates_k] + source_types  # adjust order for the specific query below

        # Join vec_chunks ↔ chunks via rowid. sqlite-vec's KNN uses MATCH on the vector column.
        if source_types:
            sql = f"""
                SELECT c.chunk_id, c.source_type, c.source_path, c.start_line, c.end_line,
                       c.text, c.metadata_json, vc.distance, vc.rowid
                FROM vec_chunks vc
                JOIN chunks c ON c.rowid = vc.rowid
                WHERE vc.embedding MATCH ? AND k = ?
                AND c.source_type IN ({",".join("?" * len(source_types))})
                ORDER BY vc.distance
            """
            params = [blob, candidates_k] + source_types
        else:
            sql = """
                SELECT c.chunk_id, c.source_type, c.source_path, c.start_line, c.end_line,
                       c.text, c.metadata_json, vc.distance, vc.rowid
                FROM vec_chunks vc
                JOIN chunks c ON c.rowid = vc.rowid
                WHERE vc.embedding MATCH ? AND k = ?
                ORDER BY vc.distance
            """
            params = [blob, candidates_k]

        rows = conn.execute(sql, params).fetchall()

        candidates = []
        for row in rows:
            chunk_id, stype, spath, sline, eline, text, metadata_json, distance, rowid = row
            # sqlite-vec returns L2 distance for float[] by default. Convert to a similarity score.
            # similarity = 1 / (1 + distance); higher = more similar
            sim = 1.0 / (1.0 + float(distance))
            candidates.append({
                "chunk_id": chunk_id,
                "source_type": stype,
                "source_path": spath,
                "start_line": sline,
                "end_line": eline,
                "text": text,
                "metadata": json.loads(metadata_json),
                "distance": float(distance),
                "similarity": sim,
                "rowid": rowid,
            })

        if not mmr or len(candidates) <= k:
            return candidates[:k]

        # MMR reranking
        # Fetch vectors for candidates so we can compute inter-candidate similarity
        rowids = [c["rowid"] for c in candidates]
        placeholders = ",".join("?" * len(rowids))
        vec_rows = conn.execute(
            f"SELECT rowid, embedding FROM vec_chunks WHERE rowid IN ({placeholders})",
            rowids,
        ).fetchall()
        rowid_to_vec = {rid: _blob_to_vec(blob) for rid, blob in vec_rows}

        selected: list[dict] = []
        remaining = list(candidates)
        while remaining and len(selected) < k:
            best_score = -1e9
            best_idx = 0
            for i, c in enumerate(remaining):
                # Relevance to query = c["similarity"]
                relevance = c["similarity"]
                # Diversity penalty = max cosine similarity to any already-selected
                if selected:
                    penalty = max(
                        _cosine(rowid_to_vec[c["rowid"]], rowid_to_vec[s["rowid"]])
                        for s in selected
                    )
                else:
                    penalty = 0.0
                score = mmr_lambda * relevance - (1.0 - mmr_lambda) * penalty
                if score > best_score:
                    best_score = score
                    best_idx = i
            selected.append(remaining.pop(best_idx))

        return selected

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)

def _blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from embedder import Embedder
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        store = VectorStore(db_path)
        store.init_schema()
        print(f"Init schema OK, count={store.count()}")

        e = Embedder()
        texts = [
            "Cairn is the orchestrator for this project.",
            "A cairn is a stack of stones marking a path.",
            "The celery beat persistence issue was fixed in round 4.",
            "Unity scene files are never committed but contain critical Inspector wiring.",
        ]
        vectors = e.embed_batch(texts)
        chunks = [
            Chunk(source_type="test", source_path=f"test-{i}.md", start_line=0, end_line=0, text=t)
            for i, t in enumerate(texts)
        ]
        inserted = store.upsert(chunks, vectors)
        print(f"Inserted {inserted} new chunks, total count={store.count()}")

        q_vec = e.embed("what did we fix about celery?")
        results = store.search(q_vec, k=3)
        print("Top-3 for 'celery':")
        for r in results:
            print(f"  sim={r['similarity']:.3f}  {r['text']}")

        q_vec2 = e.embed("tell me about Cairn")
        results2 = store.search(q_vec2, k=3, mmr=True)
        print("Top-3 MMR for 'Cairn':")
        for r in results2:
            print(f"  sim={r['similarity']:.3f}  {r['text']}")
