"""Chunker and indexer for memory files (markdown) and session transcripts (JSONL).

Takes the raw source material, splits it into embedding-friendly chunks,
calls the embedder, and writes to the vector store.

Usage:
    from indexer import Indexer
    idx = Indexer(store, embedder)

    # Memory file
    n = idx.index_memory_file(Path("orchestrator/memory/project_example.md"))

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
try:
    from embedder import SAFE_INPUT_CHARS as _SAFE_INPUT_CHARS
except ImportError:  # older embedder build without the constant
    _SAFE_INPUT_CHARS = 2400
from vectorstore import Chunk, VectorStore

# ---------------------------------------------------------------------------
# Chunk sizing
# ---------------------------------------------------------------------------

# The hard cap is the ACTIVE embedder's safe input ceiling (SAFE_INPUT_CHARS),
# so the chunker stays correct across embedder builds (nomic ~2400 / OpenAI
# ~24000) WITHOUT this file diverging between a consumer install and the MS4CC repo.
# Every assembled chunk — its header AND body — is guaranteed <= MAX_CHUNK_CHARS,
# which keeps it under the model's token ceiling at any tokenization density.
# (Before 2026-05-29 this was a flat 4000 "~1000 tokens" assuming ~4 chars/token;
# dense transcript content is ~1.5 chars/token, so 4000-char chunks hit ~2700
# tokens and 400'd against nomic's real 2048 ceiling — silently killing a month
# of transcript vectorization. See project_scri_vectorization_failure_2026-05-21.)
MAX_CHUNK_CHARS = _SAFE_INPUT_CHARS                          # hard cap (header + body)
TARGET_CHUNK_CHARS = max(800, int(_SAFE_INPUT_CHARS * 0.6))  # soft packing target
MIN_CHUNK_CHARS = 200                                        # below this, merge with neighbor


def _hard_split(text: str, limit: int) -> list[str]:
    """Split `text` into pieces each <= `limit` chars.

    Prefers line boundaries; a single line longer than `limit` is cut into
    fixed char windows. Guarantees every returned piece is <= limit regardless
    of whether the text has paragraph/line structure — the failure mode that let
    a 15.5K-char blank-line-free MEMORY.md index become one un-splittable chunk.
    """
    limit = max(1, limit)
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if buf:
                pieces.append(buf)
                buf = ""
            for j in range(0, len(line), limit):
                pieces.append(line[j:j + limit])
            continue
        candidate = (buf + "\n" + line) if buf else line
        if len(candidate) > limit:
            pieces.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces

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

    # Split too-large sections by length within the section. The threshold
    # accounts for the file_header prepended to every chunk, so the ASSEMBLED
    # chunk text (header + body) stays <= MAX_CHUNK_CHARS.
    section_body_limit = max(256, MAX_CHUNK_CHARS - len(file_header) - 2)
    chunks: list[Chunk] = []
    for start, sect_lines in merged:
        text_section = "\n".join(sect_lines)
        if len(text_section) <= section_body_limit:
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
    """Split by paragraphs, packing into chunks whose assembled text (header +
    body) stays <= MAX_CHUNK_CHARS. Paragraphs longer than the body budget are
    hard-split (line, then char window) so no structureless block can produce an
    over-ceiling chunk."""
    body_limit = max(256, MAX_CHUNK_CHARS - len(header) - 2)
    pack_target = min(TARGET_CHUNK_CHARS, body_limit)

    paragraphs: list[str] = []
    for p in re.split(r"\n\s*\n", text):
        paragraphs.extend(_hard_split(p, body_limit) if len(p) > body_limit else [p])

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
        if buf and buf_len + p_len > pack_target:
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

def chunk_transcript(jsonl_text: str, source_path: str, start_line_offset: int = 0) -> list[Chunk]:
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

    `start_line_offset` lets the caller chunk only the *tail* of a transcript
    (the lines after an already-embedded prefix) while keeping the recorded
    start_line/end_line ABSOLUTE within the full file. When chunking the tail,
    pass the tail text plus the number of lines that precede it; line numbers
    become `start_line_offset + i + 1`. This is the basis for incremental
    transcript indexing (only new turns get embedded each checkpoint).
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
        turn = _extract_turn(obj, line_num=start_line_offset + i + 1)
        if turn:
            turns.append(turn)

    if not turns:
        return []

    # Reserve room for the per-chunk header so the ASSEMBLED chunk text (header +
    # role-prefixed lines) stays <= MAX_CHUNK_CHARS. Long turns (tool dumps) are
    # SPLIT across lines/chunks rather than truncated, so no texture is lost and
    # no single dense chunk can exceed the embedder's token ceiling.
    header_reserve = len(source_path) + 80
    body_limit = max(256, MAX_CHUNK_CHARS - header_reserve)
    pack_target = min(TARGET_CHUNK_CHARS, body_limit)

    # Flatten turns into role-prefixed lines, hard-splitting any line that alone
    # exceeds the body budget. Each line keeps its source line_num so the chunk
    # still maps back to the transcript.
    lines: list[tuple[int, str]] = []
    for t in turns:
        prefix = f"[{t['role']}]: "
        avail = max(128, body_limit - len(prefix))
        for k, piece in enumerate(_hard_split(t["content"], avail)):
            tag = prefix if k == 0 else f"[{t['role']} cont.]: "
            lines.append((t["line_num"], tag + piece))

    chunks: list[Chunk] = []
    buf: list[tuple[int, str]] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return
        first_line = buf[0][0]
        last_line = buf[-1][0]
        header = f"[Session transcript chunk — source: {source_path}, turns {first_line}-{last_line}]"
        chunk_text = header + "\n" + "\n".join(x[1] for x in buf)
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

    for ln, txt in lines:
        add = len(txt) + 1
        if buf and buf_len + add > pack_target:
            flush()
        buf.append((ln, txt))
        buf_len += add

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
                        # Marker only, never the body: tool output is where
                        # pasted configs, plist dumps and env files arrive
                        # (MS4CC#117). The model's own reply carries what
                        # mattered about the result.
                        content_parts.append("[tool-result]")

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
        # Scrub before chunking: a split can cut a secret across two chunks,
        # and a half-token matches no rule (upsert scrubs each chunk again).
        text = scrub(path.read_text())
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
        """Chunk + embed + store a transcript JSONL INCREMENTALLY. Returns new chunks.

        Session transcripts are append-only and a single resumed session can grow
        to hundreds of MB over many days. The old behaviour (delete all chunks for
        this path, then re-embed the whole file every checkpoint) was O(filesize)
        per checkpoint — it re-embedded the entire cumulative session each time and
        ran for many minutes once the file got large. Instead, we embed ONLY the
        turns added since the last index: look up the highest end_line already
        stored for this source_path, chunk just the tail beyond it (with absolute
        line numbers via start_line_offset), and append. No delete, no re-embed.

        Fallbacks: if nothing is stored yet, embed the whole file once (baseline).
        If the file is SHORTER than what we've stored (truncated/replaced — should
        never happen for an append-only transcript), rebuild from scratch.
        """
        if not path.exists():
            return 0
        text = path.read_text()
        total_lines = text.count("\n") + 1
        last_embedded = self.store.max_end_line_for_source(str(path))

        # Incremental: embed only the tail past the last-embedded line.
        if last_embedded and last_embedded < total_lines:
            tail = "\n".join(text.split("\n")[last_embedded:])  # lines (last_embedded+1)..end
            chunks = chunk_transcript(tail, str(path), start_line_offset=last_embedded)
            if not chunks:
                return 0
            vectors = self.embedder.embed_batch([c.text for c in chunks])
            n = self.store.upsert(chunks, vectors)
            self._log(f"  indexed transcript (incremental): {path.name} +{n} chunks "
                      f"(from line {last_embedded + 1})")
            return n

        # Nothing new to add (already embedded up to the current end of file).
        if last_embedded and last_embedded == total_lines:
            return 0

        # Baseline (nothing stored yet) OR rebuild (last_embedded > total_lines,
        # i.e. the file shrank/was replaced → stale chunks must be cleared first).
        chunks = chunk_transcript(text, str(path))
        if not chunks:
            return 0
        self.store.delete_by_source_path(str(path))
        vectors = self.embedder.embed_batch([c.text for c in chunks])
        n = self.store.upsert(chunks, vectors)
        self._log(f"  indexed transcript (full baseline): {path.name} → {n} chunks")
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
