"""Recall-usage log — the frequency/usage instrument for MS4CC #63.

Appends one JSONL line per recalled chunk so we can finally measure, empirically,
which corpus recall actually pulls from (memory vs transcript) and how the auto
per-turn path compares to manual CLI search. This answers Clint's question that
kicked off #63: "which has been more useful — memory vectors or transcript vectors?
Or is manual searching used more?" We had no data; now we will.

Logging is NOT weighting. The manual CLI stays RAW-ranked (Clint's 2026-06-10
ruling — experiential weighting is auto-recall-only); it is logged only so the two
paths are comparable. Fail-open by construction: every public call swallows its own
errors, because a logging fault must never break recall.

Schema — one JSON object per line at orchestrator/transcripts/recall_usage.jsonl:
  ts               ISO-8601 UTC timestamp
  path             "auto" (per-turn UserPromptSubmit) | "manual" (CLI recall.py)
  query            the recall query (truncated to QUERY_CAP chars)
  source_type      "memory" | "transcript" | ...
  source_path      basename-friendly path of the chunk's source
  chunk_id         vector-store chunk id (if available)
  similarity       raw cosine similarity from the vector search
  rank             0-based position in the FINAL ordering the caller produced
  authority_factor per-turn re-rank multiplier applied (auto only; null on manual/raw)
  injected         whether this chunk made it into the context that was surfaced
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

try:  # hooks dir is on sys.path for every caller
    from scrubber import scrub as _scrub
except Exception:  # pragma: no cover - fail closed: never log a raw query
    def _scrub(_text: str) -> str:
        return ""

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = ORCHESTRATOR_DIR / "transcripts" / "recall_usage.jsonl"
QUERY_CAP = 300


def log(path_kind: str, query: str, records: list[dict]) -> None:
    """Append one JSONL line per record. Caller assembles the per-chunk fields;
    this stamps ts/path/query and serializes. Never raises."""
    try:
        if not records:
            return
        ts = datetime.now(tz=timezone.utc).isoformat()
        q = _scrub((query or "")[:QUERY_CAP])
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for rec in records:
            row = {
                "ts": ts,
                "path": path_kind,
                "query": q,
                "source_type": rec.get("source_type"),
                "source_path": rec.get("source_path"),
                "chunk_id": rec.get("chunk_id"),
                "similarity": rec.get("similarity"),
                "rank": rec.get("rank"),
                "authority_factor": rec.get("authority_factor"),
                "injected": rec.get("injected"),
            }
            lines.append(json.dumps(row, default=str))
        with LOG_PATH.open("a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # Fail-open: usage logging must never break a recall path.
        return
