"""Chunker and indexer for memory files (markdown) and session transcripts (JSONL).

Takes the raw source material, splits it into embedding-friendly chunks,
calls the embedder, and writes to the vector store.

Usage:
    from indexer import Indexer
    idx = Indexer(store, embedder)

    # Memory file
    n = idx.index_memory_file(Path("orchestrator/memory/project_scryforge.md"))

    # Transcript
    n = idx.index_transcript(Path("orchestrator/transcripts/<uuid>.jsonl"))

    # Bulk backfill
    idx.backfill_all(memory_dir, transcripts_dir)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from embedder import Embedder, scrub
from vectorstore import Chunk, VectorStore

# ---------------------------------------------------------------------------
# Chunk sizing
# ---------------------------------------------------------------------------

# Soft targets for a single chunk. Markdown chunks try to respect headers;
# transcript chunks try to group conversational turns.
TARGET_CHUNK_CHARS = 2000       # ~500 tokens
MAX_CHUNK_CHARS = 4000          # ~1000 tokens (hard cap)
MIN_CHUNK_CHARS = 200           # below this, merge with neighbor

# ---------------------------------------------------------------------------
# Markdown chunker
# ---------------------------------------------------------------------------

def _strip_frontmatter(text: str) -> tuple[str, str]:
    """Separate frontmatter from body. Returns (frontmatter_text, body_text)."""
    if not text.startswith("---\n"):
        return "", text
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2)

def chunk_markdown(text: str, source_path: str) -> list[Chunk]:
    """Split a markdown file into chunks bounded by headers or length.

    Strategy:
    - Frontmatter is prepended as context to every chunk from this file
      (helps the embedder know what file/topic this chunk belongs to).
    - Split on `## ` level-2 headers as primary chunk boundaries.
    - Within a section, if the text exceeds MAX_CHUNK_CHARS, split on paragraphs.
    - Merge very short sections with their neighbor.
    """
    fm_text, body = _strip_frontmatter(text)

    # A small header describing the file — prepended to every chunk.
    file_header = f"[File: {source_path}]"
    if fm_text:
        # Pull name/description from frontmatter as a helpful context line.
        name_match = re.search(r"^name:\s*(.+)$", fm_text, re.MULTILINE)
        desc_match = re.search(r"^description:\s*(.+)$", fm_text, re.MULTILINE)
        if name_match:
            file_header += f"\n[Name: {name_match.group(1).strip()}]"
        if desc_match:
            file_header += f"\n[Description: {desc_match.group(1).strip()}]"

    # Split on level-2 headers, preserving them.
    lines = body.split("\n")
    sections: list[tuple[int, list[str]]] = []  # (start_line, lines)
    current_start = 1
    current: list[str] = []
    for i, line in enumerate(lines):
        # Level-2 header starts a new section (except the first)
        if line.startswith("## ") and current:
            sections.append((current_start, current))
            current = [line]
            current_start = i + 1
        else:
            current.append(line)
    if current:
        sections.append((current_start, current))

    # If no level-2 headers at all, just chunk by length
    if len(sections) == 1 and not any(l.startswith("## ") for l in sections[0][1]):
        return _chunk_by_length(body, source_path, file_header, start_line=1)

    # Merge small sections
    merged: list[tuple[int, list[str]]] = []
    for start, sect_lines in sections:
        joined = "\n".join(sect_lines)
        if merged and len(joined) < MIN_CHUNK_CHARS:
            # Merge with previous
            prev_start, prev_lines = merged[-1]
            merged[-1] = (prev_start, prev_lines + sect_lines)
        else:
            merged.append((start, sect_lines))

    # Split too-large sections by length within the section
    chunks: list[Chunk] = []
    for start, sect_lines in merged:
        text_section = "\n".join(sect_lines)
        if len(text_section) <= MAX_CHUNK_CHARS:
            chunk_text = f"{file_header}\n\n{text_section}"
            chunks.append(Chunk(
                source_type="memory",
                source_path=source_path,
                start_line=start,
                end_line=start + len(sect_lines) - 1,
                text=chunk_text,
            ))
        else:
            chunks.extend(_chunk_by_length(text_section, source_path, file_header, start_line=start))

    return chunks

def _chunk_by_length(text: str, source_path: str, header: str, start_line: int = 1) -> list[Chunk]:
    """Split by paragraphs, packing paragraphs into TARGET_CHUNK_CHARS-sized chunks."""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    buf_start_offset = 0
    offset = 0  # line offset within `text`

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return
        body = "\n\n".join(buf).strip()
        if not body:
            buf = []
            buf_len = 0
            return
        chunk_text = f"{header}\n\n{body}"
        # Rough line count
        lines_in_chunk = chunk_text.count("\n") + 1
        chunks.append(Chunk(
            source_type="memory",
            source_path=source_path,
            start_line=start_line + buf_start_offset,
            end_line=start_line + buf_start_offset + lines_in_chunk - 1,
            text=chunk_text,
        ))
        buf = []
        buf_len = 0

    for p in paragraphs:
        p_len = len(p)
        if buf_len + p_len > TARGET_CHUNK_CHARS and buf:
            flush()
            buf_start_offset = offset
        buf.append(p)
        buf_len += p_len + 2  # +2 for double-newline separator
        offset += p.count("\n") + 2
    flush()
    return chunks

# ---------------------------------------------------------------------------
# Transcript chunker (JSONL from Claude Code sessions)
# ---------------------------------------------------------------------------

def chunk_transcript(jsonl_text: str, source_path: str) -> list[Chunk]:
    """Split a Claude Code session JSONL into conversational chunks.

    Each line in the JSONL is a message with a role (user/assistant/system).
    We group consecutive user+assistant pairs into chunks, respecting size caps.

    The text of each chunk is formatted as:
        [SESSION TRANSCRIPT — turns M-N]
        [user]: <content>
        [assistant]: <content>
        ...

    Tool calls/results are included but summarized (the raw JSON bodies
    can get huge and aren't always useful for semantic recall).
    """
    turns: list[dict] = []
    for i, line in enumerate(jsonl_text.split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        turn = _extract_turn(obj, line_num=i + 1)
        if turn:
            turns.append(turn)

    if not turns:
        return []

    chunks: list[Chunk] = []
    buf: list[dict] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return
        first_line = buf[0]["line_num"]
        last_line = buf[-1]["line_num"]
        header = f"[Session transcript chunk — source: {source_path}, turns {first_line}-{last_line}]"
        body_lines = [f"[{t['role']}]: {t['content']}" for t in buf]
        chunk_text = header + "\n" + "\n".join(body_lines)
        chunks.append(Chunk(
            source_type="transcript",
            source_path=source_path,
            start_line=first_line,
            end_line=last_line,
            text=chunk_text,
            metadata={"turn_count": len(buf)},
        ))
        buf = []
        buf_len = 0

    for turn in turns:
        t_len = len(turn["content"])
        # Cap per-turn content length to avoid one monster turn eating everything
        if t_len > MAX_CHUNK_CHARS:
            turn["content"] = turn["content"][:MAX_CHUNK_CHARS] + "... [truncated]"
            t_len = MAX_CHUNK_CHARS

        if buf_len + t_len > TARGET_CHUNK_CHARS and buf:
            flush()
        buf.append(turn)
        buf_len += t_len

    flush()
    return chunks

def _extract_turn(obj: dict, line_num: int) -> dict | None:
    """Flatten a Claude Code transcript line into {role, content, line_num}."""
    # Claude Code JSONL format varies by version. Try common shapes.
    role = None
    content_parts: list[str] = []

    # Shape 1: {"type": "user"|"assistant", "message": {"role": ..., "content": [...]}}
    if "message" in obj and isinstance(obj["message"], dict):
        msg = obj["message"]
        role = msg.get("role")
        c = msg.get("content")
        if isinstance(c, str):
            content_parts.append(c)
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        content_parts.append(item.get("text", ""))
                    elif item.get("type") == "thinking":
                        content_parts.append(f"[thinking]: {item.get('thinking', '')}")
                    elif item.get("type") == "tool_use":
                        name = item.get("name", "?")
                        content_parts.append(f"[tool-call: {name}]")
                    elif item.get("type") == "tool_result":
                        content_parts.append(f"[tool-result]: {str(item.get('content', ''))[:300]}")

    # Shape 2: {"role": ..., "content": ...}
    elif "role" in obj:
        role = obj["role"]
        c = obj.get("content")
        if isinstance(c, str):
            content_parts.append(c)
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    content_parts.append(item.get("text", ""))

    # Shape 3: {"type": "summary", ...} or other system types — skip
    if role not in ("user", "assistant", "system"):
        return None

    content = "\n".join(p for p in content_parts if p).strip()
    if not content:
        return None

    return {"role": role, "content": scrub(content), "line_num": line_num}

# ---------------------------------------------------------------------------
# Indexer — ties it all together
# ---------------------------------------------------------------------------

class Indexer:
    def __init__(self, store: VectorStore, embedder: Embedder, verbose: bool = False):
        self.store = store
        self.embedder = embedder
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def index_memory_file(self, path: Path) -> int:
        """Chunk + embed + store a single memory file. Returns number of new chunks."""
        if not path.exists():
            return 0
        text = path.read_text()
        chunks = chunk_markdown(text, str(path))
        if not chunks:
            return 0
        # Delete any existing chunks for this path (handles file updates)
        self.store.delete_by_source_path(str(path))
        vectors = self.embedder.embed_batch([c.text for c in chunks])
        n = self.store.upsert(chunks, vectors)
        self._log(f"  indexed memory: {path.name} → {n} chunks")
        return n

    def index_transcript(self, path: Path) -> int:
        """Chunk + embed + store a single transcript JSONL. Returns new chunks."""
        if not path.exists():
            return 0
        text = path.read_text()
        chunks = chunk_transcript(text, str(path))
        if not chunks:
            return 0
        self.store.delete_by_source_path(str(path))
        vectors = self.embedder.embed_batch([c.text for c in chunks])
        n = self.store.upsert(chunks, vectors)
        self._log(f"  indexed transcript: {path.name} → {n} chunks")
        return n

    def backfill(self, memory_dir: Path | None, transcripts_dir: Path | None) -> dict:
        """Re-index everything in the given directories. Returns counts."""
        counts = {"memory": 0, "transcript": 0}
        if memory_dir and memory_dir.exists():
            for p in sorted(memory_dir.glob("*.md")):
                try:
                    counts["memory"] += self.index_memory_file(p)
                except Exception as e:
                    print(f"  ERROR indexing {p.name}: {e}")
        if transcripts_dir and transcripts_dir.exists():
            for p in sorted(transcripts_dir.glob("*.jsonl")):
                try:
                    counts["transcript"] += self.index_transcript(p)
                except Exception as e:
                    print(f"  ERROR indexing {p.name}: {e}")
        return counts

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
    memory_dir = ORCHESTRATOR_DIR / "memory"
    transcripts_dir = ORCHESTRATOR_DIR / "transcripts"
    db_path = ORCHESTRATOR_DIR / "vectors.db"

    store = VectorStore(db_path)
    store.init_schema()
    embedder = Embedder()
    idx = Indexer(store, embedder, verbose=True)

    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        print(f"Backfilling index from {memory_dir} and {transcripts_dir}...")
        counts = idx.backfill(memory_dir, transcripts_dir)
        print(f"New chunks: memory={counts['memory']}, transcript={counts['transcript']}")
        print(f"Total chunks in store: {store.count()}")
    else:
        print("Usage: python indexer.py backfill")
