#!/usr/bin/env python3
"""Stop hook — fires when a Claude Code session ends.

Mechanical, non-LLM operations:
  1. Archive the current session's JSONL transcript into orchestrator/transcripts/
  2. Vectorize the new transcript chunks into the vector store
  3. Auto-increment `hits` counters on memory files that appear to have been
     cited in this session (simple filename match against transcript content)

None of this requires the orchestrator to be "awake" or running. Runs
independently every session end. The reflective parts of /checkpoint
(proposing new memories, drift detection, Option D confirmation) remain
manual — those need the orchestrator's judgment.

If the venv isn't set up yet (fresh clone before bootstrap), this hook
logs a warning to stderr and exits cleanly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Compute paths relative to this script's location.
HOOK_FILE = Path(__file__).resolve()
ORCHESTRATOR_DIR = HOOK_FILE.parent.parent
TRANSCRIPTS_DIR = ORCHESTRATOR_DIR / "transcripts"
MEMORY_DIR = ORCHESTRATOR_DIR / "memory"
DB_PATH = ORCHESTRATOR_DIR / "vectors.db"

# Where Claude Code stores session JSONLs.
# Format: ~/.claude/projects/-<escaped-cwd>/<session-uuid>.jsonl
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# ---------------------------------------------------------------------------
# Stop-hook input protocol
# ---------------------------------------------------------------------------

def read_hook_input() -> dict:
    """Claude Code Stop hooks receive JSON on stdin with session info."""
    try:
        if sys.stdin.isatty():
            return {}
        data = sys.stdin.read().strip()
        if not data:
            return {}
        return json.loads(data)
    except Exception:
        return {}

def resolve_session_path(hook_input: dict) -> Path | None:
    """Figure out which JSONL file corresponds to this session."""
    # Try common shapes in order
    session_id = hook_input.get("session_id") or hook_input.get("sessionId")
    if not session_id:
        sess = hook_input.get("session")
        if isinstance(sess, dict):
            session_id = sess.get("id")
    cwd = hook_input.get("cwd") or os.getcwd()

    # Derive the project dir name (Claude Code escapes slashes to dashes)
    escaped = cwd.replace("/", "-")
    project_dir = CLAUDE_PROJECTS_DIR / escaped

    if session_id:
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate

    # Fallback: find the most recent .jsonl in the project dir
    if project_dir.exists():
        jsonls = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if jsonls:
            return jsonls[0]

    return None

# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_transcript(session_jsonl: Path) -> Path | None:
    """Copy the session JSONL into orchestrator/transcripts/.

    The archived filename includes the date and original session UUID.
    Returns the archived path, or None if archive was skipped (already exists).
    """
    if not session_jsonl.exists():
        return None

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    date_prefix = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    uuid = session_jsonl.stem  # the session UUID
    archived_name = f"{date_prefix}__{uuid}.jsonl"
    archived_path = TRANSCRIPTS_DIR / archived_name

    # Copy only if the archive is older or missing (newer session data in source).
    if archived_path.exists():
        src_mtime = session_jsonl.stat().st_mtime
        dst_mtime = archived_path.stat().st_mtime
        if dst_mtime >= src_mtime:
            return None

    shutil.copy2(session_jsonl, archived_path)
    return archived_path

# ---------------------------------------------------------------------------
# Vectorize
# ---------------------------------------------------------------------------

def vectorize_transcript(archived_path: Path) -> int:
    """Chunk, embed, and store the archived transcript in the vector DB.

    Returns number of new chunks added, or -1 if vector stack isn't available.
    """
    # Lazy-import so the hook works even if deps aren't installed yet.
    try:
        from embedder import Embedder
        from indexer import Indexer
        from vectorstore import VectorStore
    except Exception as e:
        print(f"[session_end] Vector stack unavailable ({e}); skipping vectorization.", file=sys.stderr)
        return -1

    store = VectorStore(DB_PATH)
    store.init_schema()
    try:
        embedder = Embedder()
    except Exception as e:
        print(f"[session_end] Embedder not available ({e}); skipping vectorization.", file=sys.stderr)
        return -1

    idx = Indexer(store, embedder, verbose=False)
    try:
        return idx.index_transcript(archived_path)
    except Exception as e:
        print(f"[session_end] Failed to index transcript: {e}", file=sys.stderr)
        return 0

# ---------------------------------------------------------------------------
# Auto-increment hits based on memory filenames appearing in the transcript
# ---------------------------------------------------------------------------

def auto_increment_hits(archived_path: Path) -> list[str]:
    """Scan the transcript for memory-file filename mentions and bump `hits`.

    Heuristic: if a memory filename like `feedback_never_destructive_git` or
    `feedback_never_destructive_git.md` appears anywhere in the transcript
    text, count it as a citation and increment that file's `hits` frontmatter.

    Returns list of memory filenames that were incremented.
    """
    if not archived_path.exists():
        return []
    if not MEMORY_DIR.exists():
        return []

    try:
        transcript_text = archived_path.read_text()
    except Exception:
        return []

    memory_names = {p.name: p for p in MEMORY_DIR.glob("*.md")}
    incremented: list[str] = []

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    for name, path in memory_names.items():
        stem = name.replace(".md", "")
        # Match either `name.md` or just `stem` (to avoid false positives,
        # require the stem be "underscore-shaped" — generic English words
        # would false-positive, but our memory names like
        # feedback_never_destructive_git don't).
        if "_" not in stem and stem not in ("MEMORY", "IDENTITY", "USER", "LOG", "ROADMAP"):
            # Not a conventional memory filename — use full name match only
            patterns = [re.escape(name)]
        else:
            patterns = [re.escape(name), re.escape(stem)]

        if any(re.search(p, transcript_text) for p in patterns):
            if _bump_memory_hits(path, today):
                incremented.append(name)

    return incremented

def _bump_memory_hits(path: Path, today: str) -> bool:
    """Increment `hits` and set `last_applied` in a memory file's frontmatter.

    Returns True if the file was modified.
    """
    try:
        text = path.read_text()
    except Exception:
        return False
    if not text.startswith("---\n"):
        return False
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return False
    fm_block, body = m.group(1), m.group(2)

    # Parse + update frontmatter lines
    lines = fm_block.split("\n")
    new_lines = []
    hits_found = False
    last_applied_found = False
    for line in lines:
        if line.startswith("hits:"):
            try:
                current = int(line.split(":", 1)[1].strip())
            except Exception:
                current = 0
            new_lines.append(f"hits: {current + 1}")
            hits_found = True
        elif line.startswith("last_applied:"):
            new_lines.append(f"last_applied: {today}")
            last_applied_found = True
        else:
            new_lines.append(line)

    if not hits_found:
        new_lines.append("hits: 1")
    if not last_applied_found:
        new_lines.append(f"last_applied: {today}")

    new_fm = "\n".join(new_lines)
    new_text = f"---\n{new_fm}\n---\n{body}"
    try:
        path.write_text(new_text)
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    hook_input = read_hook_input()

    # Skip entirely if we're not in a TestFlight/orchestrator context
    # (sanity check — the orchestrator dir should exist if we're running).
    if not ORCHESTRATOR_DIR.exists():
        return

    session_jsonl = resolve_session_path(hook_input)
    if not session_jsonl:
        print("[session_end] No session JSONL found; nothing to archive.", file=sys.stderr)
        return

    archived = archive_transcript(session_jsonl)
    if archived is None:
        # Already up-to-date
        return

    n_chunks = vectorize_transcript(archived)

    incremented = auto_increment_hits(archived)

    # Append a brief note to LOG.md so the activity is visible next session.
    log_entry = [
        f"",
        f"### Auto-archive — {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}",
        f"- Archived: `{archived.name}`",
        f"- Transcript chunks vectorized: {n_chunks if n_chunks >= 0 else 'skipped (vectors unavailable)'}",
        f"- Memory hits auto-incremented: {len(incremented)}"
        + (f" ({', '.join(incremented[:5])}{'...' if len(incremented) > 5 else ''})" if incremented else ""),
    ]
    log_path = ORCHESTRATOR_DIR / "LOG.md"
    if log_path.exists():
        try:
            with log_path.open("a") as f:
                f.write("\n".join(log_entry) + "\n")
        except Exception as e:
            print(f"[session_end] Could not write LOG.md entry: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
