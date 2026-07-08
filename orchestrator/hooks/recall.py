"""Semantic recall utility — query the vector store for relevant chunks.

Used by:
    - SessionStart hook (automatic: query by CWD + recent context)
    - /checkpoint command (orchestrator's judgment query)
    - Ad-hoc CLI (for testing / manual search)

Usage (CLI):
    python3 recall.py "what did we decide about celery beat?"
    python3 recall.py "celery beat" --source transcript --k 5 --mmr

Usage (library):
    from recall import recall
    results = recall("query", k=10, source_types=["memory"])
"""

from __future__ import annotations

import argparse
from pathlib import Path

from embedder import Embedder
from vectorstore import VectorStore

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = ORCHESTRATOR_DIR / "vectors.db"

def recall(
    query: str,
    k: int = 10,
    source_types: list[str] | None = None,
    mmr: bool = True,
    mmr_lambda: float = 0.7,
    db_path: Path = DEFAULT_DB,
) -> list[dict]:
    """Semantic search over the vector store.

    Returns a list of result dicts with keys:
      chunk_id, source_type, source_path, start_line, end_line,
      text, metadata, similarity
    """
    if not db_path.exists():
        return []  # Vector store not built yet — graceful no-op
    store = VectorStore(db_path)
    store.init_schema()
    embedder = Embedder()
    query_vec = embedder.embed(query)
    return store.search(
        query_vec,
        k=k,
        source_types=source_types,
        mmr=mmr,
        mmr_lambda=mmr_lambda,
    )

def format_results(results: list[dict], max_chars_per_result: int = 400) -> str:
    """Human-readable rendering for display."""
    if not results:
        return "(no matches)"
    lines = []
    for i, r in enumerate(results, 1):
        preview = r["text"]
        if len(preview) > max_chars_per_result:
            preview = preview[:max_chars_per_result] + "..."
        lines.append(
            f"{i}. [{r['source_type']}] {r['source_path']} "
            f"(lines {r['start_line']}-{r['end_line']}) "
            f"sim={r['similarity']:.3f}\n"
            f"   {preview.strip()}"
        )
    return "\n\n".join(lines)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Semantic recall over orchestrator memory + transcripts")
    parser.add_argument("query", help="The query string")
    parser.add_argument("--k", type=int, default=8, help="Top-K results (default 8)")
    parser.add_argument("--source", action="append", choices=["memory", "transcript", "identity"],
                        help="Filter by source type (can pass multiple)")
    parser.add_argument("--no-mmr", action="store_true", help="Disable MMR diversification")
    parser.add_argument("--lambda", dest="mmr_lambda", type=float, default=0.7,
                        help="MMR lambda: 1.0=pure similarity, 0.0=pure diversity (default 0.7)")
    args = parser.parse_args()

    results = recall(
        args.query,
        k=args.k,
        source_types=args.source,
        mmr=not args.no_mmr,
        mmr_lambda=args.mmr_lambda,
    )
    print(format_results(results))

    # Usage logging (manual path) — MS4CC #63. The CLI stays RAW-ranked (Clint's
    # 2026-06-10 ruling that experiential weighting is auto-recall-only); we log it
    # only so manual search is comparable to the auto per-turn path. This runs on the
    # CLI entry point ONLY — the library recall() used by /checkpoint stays silent, so
    # judgment queries don't pollute the usage signal. Fail-open.
    try:
        import recall_usage
        recall_usage.log("manual", args.query, [
            {
                "source_type": r.get("source_type"),
                "source_path": r.get("source_path"),
                "chunk_id": r.get("chunk_id"),
                "similarity": r.get("similarity"),
                "rank": i,
                "authority_factor": None,
                "injected": True,
            }
            for i, r in enumerate(results)
        ])
    except Exception:
        pass
