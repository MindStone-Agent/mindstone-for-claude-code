#!/usr/bin/env python3
"""Stop hook — fires on each completed turn (Claude Code "Stop").

Mechanical, non-LLM operations:
  1. Archive the current session's JSONL transcript into orchestrator/transcripts/
  2. Auto-increment `hits` counters on memory files that appear to have been
     cited in this session (simple filename match against transcript content)

Embedding (transcript vectorization + memory reindex) is DELIBERATELY NOT done
here. `index_transcript` re-embeds the ENTIRE transcript on every run, so doing
it on the per-turn Stop hook (and the intra-session watchdog) re-embedded ~15k
chunks through the local embedder every single turn and ran the machine hot.
Per Clint's 2026-05-31 directive, embedding happens ONLY at /checkpoint, which
invokes this script with CAIRN_CHECKPOINT_MODE=1. (MS4CC agents only —
MindStone-proper vectorizes via its own gateway/plugin path, not this hook.)

None of this requires the orchestrator to be "awake" or running. Runs
independently. The reflective parts of /checkpoint (proposing new memories,
drift detection, Option D confirmation) remain manual — those need judgment.

If the venv isn't set up yet (fresh clone before bootstrap), this hook
logs a warning to stderr and exits cleanly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
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

# When invoked by the intra-session watchdog (from user_prompt_submit.py), we
# archive only and must NOT auto-increment memory hit counters — the per-turn
# Stop hook already owns that, and repeating it from the watchdog would inflate
# counters.
WATCHDOG_MODE = os.environ.get("CAIRN_WATCHDOG_MODE") == "1"

# Set by /checkpoint (step 7). This is the ONLY mode that embeds (transcript
# vectorize + memory reindex). The per-turn Stop hook and the watchdog archive
# only: index_transcript re-embeds the FULL transcript every run, so embedding
# on every turn pegged the local embedder (ollama/nomic) and ran the machine
# hot. Clint directive 2026-05-31: "embedding should ONLY happen in checkpoints."
CHECKPOINT_MODE = os.environ.get("CAIRN_CHECKPOINT_MODE") == "1"

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

def resolve_session_path(hook_input: dict, args: argparse.Namespace | None = None) -> Path | None:
    """Figure out which JSONL file corresponds to this session.

    Resolution order for the session id: --session-id flag → hook stdin JSON.
    Resolution order for the project dir: --cwd flag → hook stdin JSON →
    process cwd → the install root derived from THIS SCRIPT's location
    (orchestrator/..). The last one kills the wrong-cwd failure class: a
    /checkpoint step 7 (or any manual invocation) run from some other
    directory used to look in the wrong ~/.claude/projects/<escaped> dir and
    silently find nothing (Hearth, #devops 2026-06-10).
    """
    session_id = (args.session_id if args else None) or \
        hook_input.get("session_id") or hook_input.get("sessionId")
    if not session_id:
        sess = hook_input.get("session")
        if isinstance(sess, dict):
            session_id = sess.get("id")

    cwd_candidates = []
    if args and args.cwd:
        cwd_candidates.append(args.cwd)
    if hook_input.get("cwd"):
        cwd_candidates.append(hook_input["cwd"])
    cwd_candidates.append(os.getcwd())
    cwd_candidates.append(str(ORCHESTRATOR_DIR.parent))  # script-relative install root

    # ---- Pass 1: a KNOWN session id is authoritative. -------------------
    # Try the derivable project dirs first, then every project dir. A session
    # started under one cwd and checkpointed from another lives under a dir we
    # cannot derive from any candidate, and the old code fell through to
    # "most recent .jsonl in whichever dir happened to exist" -- which returns
    # a DIFFERENT session's transcript and archives it while reporting success.
    if session_id:
        for cwd in cwd_candidates:
            candidate = CLAUDE_PROJECTS_DIR / cwd.replace("/", "-") / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate

        # Global scan by exact uuid across ~/.claude/projects/*/ .
        if CLAUDE_PROJECTS_DIR.exists():
            matches = sorted(
                CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                return matches[0]

        # Asked for a specific session and it is nowhere. Return None so the
        # caller fails loudly. Falling back to "most recent" here would archive
        # some other session under this one's name and print [checkpoint] OK --
        # a wrong answer is worse than no answer.
        return None

    # ---- Pass 2: no session id (manual invocation, e.g. /checkpoint). ---
    # Most recent .jsonl in the first candidate project dir that has one.
    for cwd in cwd_candidates:
        project_dir = CLAUDE_PROJECTS_DIR / cwd.replace("/", "-")
        if project_dir.exists():
            jsonls = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if jsonls:
                return jsonls[0]

    # Last resort: most recent .jsonl anywhere under ~/.claude/projects/.
    # The derived dir can EXIST and still hold no transcripts -- Claude Code
    # keys the project dir on the directory it was LAUNCHED from, not on the
    # project being worked in. If sessions are habitually started from the home
    # directory, `-Users-<user>-Projects-<repo>/` can exist while holding only a
    # stray symlink, with every real transcript under `-Users-<user>/`. Then
    # /checkpoint step 7 (invoked with `< /dev/null`, hence no session id)
    # resolves to None and fails outright even though a transcript plainly exists.
    # Guessing the most recently active session is a guess, but the caller
    # prints the file it archived, so the guess is visible rather than silent
    # -- and it beats failing when a transcript plainly exists.
    if CLAUDE_PROJECTS_DIR.exists():
        jsonls = sorted(
            CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if jsonls:
            return jsonls[0]

    return None

# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_transcript(session_jsonl: Path) -> Path | None:
    """Copy the session JSONL into orchestrator/transcripts/ — ONE file per session.

    Archived as `<session-uuid>.jsonl` (no date prefix) and UPDATED IN PLACE.
    Claude Code keeps a single live file per session (`<uuid>.jsonl`) that grows
    as the session is resumed across days; mirroring it to one stable archived
    name means one archive per session, not one-per-day. The previous scheme
    (`YYYY-MM-DD__<uuid>.jsonl`) created a brand-new full copy every day a
    long-lived session was active — 35 copies / ~9 GB for a single 5-week
    session here — and, because the indexer keys chunks by source_path, made
    every checkpoint re-embed the whole cumulative session under a new name.
    A stable name keeps source_path constant so incremental indexing works.

    Returns (archived_path, copied). `copied` is False when the archive was
    already up to date — callers in CHECKPOINT_MODE must still embed against
    the returned path (the old None-return made /checkpoint silently skip the
    ENTIRE embed, memory reindex included, whenever the per-turn Stop hook had
    archived seconds earlier — Hearth, #devops 2026-06-10).
    """
    if not session_jsonl.exists():
        return None, False

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    uuid = session_jsonl.stem  # the session UUID (Claude Code's stable per-session name)
    archived_path = TRANSCRIPTS_DIR / f"{uuid}.jsonl"

    # Copy only if the archive is older or missing (newer session data in source).
    if archived_path.exists():
        src_mtime = session_jsonl.stat().st_mtime
        dst_mtime = archived_path.stat().st_mtime
        if dst_mtime >= src_mtime:
            return archived_path, False

    shutil.copy2(session_jsonl, archived_path)
    return archived_path, True

# ---------------------------------------------------------------------------
# Vectorize
# ---------------------------------------------------------------------------

def vectorize_transcript(archived_path: Path) -> dict:
    """Chunk, embed, and store the archived transcript in the vector DB.

    Returns {chunks, truncated, failed, error}. `chunks` is the number of new
    chunks added, or -1 if the vector stack isn't available. `truncated`/`failed`
    surface lossy embedder operations so a silent drop (the bug that hid a month
    of transcript-vectorization failures) becomes visible in LOG.md.
    """
    result = {"chunks": -1, "truncated": 0, "failed": 0, "error": None}
    # Lazy-import so the hook works even if deps aren't installed yet.
    try:
        from embedder import Embedder
        from indexer import Indexer
        from vectorstore import VectorStore
    except Exception as e:
        print(f"[session_end] Vector stack unavailable ({e}); skipping vectorization.", file=sys.stderr)
        return result

    store = VectorStore(DB_PATH)
    store.init_schema()
    try:
        embedder = Embedder()
    except Exception as e:
        print(f"[session_end] Embedder not available ({e}); skipping vectorization.", file=sys.stderr)
        return result

    idx = Indexer(store, embedder, verbose=False)
    try:
        result["chunks"] = idx.index_transcript(archived_path)
    except Exception as e:
        print(f"[session_end] Failed to index transcript: {e}", file=sys.stderr)
        result["chunks"] = 0
        result["error"] = str(e)[:200]
    result["truncated"] = embedder.stats.get("truncated", 0)
    result["failed"] = embedder.stats.get("failed", 0)
    return result

# ---------------------------------------------------------------------------
# Re-index changed memory files (content-hash gate + mtime seed)
# ---------------------------------------------------------------------------

# Frontmatter keys the hooks themselves mutate. Bumping these must NOT count
# as a content change: the per-turn hits tracking touches most cited memory
# files every day, so an mtime-only gate degraded to "re-embed the whole
# corpus at every checkpoint" (observed 2026-06-10: 1,161 chunks recreated,
# nearly all byte-identical).
VOLATILE_FM_KEYS = ("hits:", "last_applied:", "prevented:")

STATE_PATH = ORCHESTRATOR_DIR / ".memory-index-state.json"


def _memory_body_hash(path: Path) -> str | None:
    """Hash the file with volatile frontmatter counter lines stripped."""
    try:
        text = path.read_text()
    except Exception:
        return None
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if m:
        fm = "\n".join(
            ln for ln in m.group(1).split("\n")
            if not ln.strip().startswith(VOLATILE_FM_KEYS)
        )
        text = f"{fm}\n---\n{m.group(2)}"
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _load_index_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_index_state(state: dict) -> None:
    try:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=0, sort_keys=True))
        tmp.replace(STATE_PATH)
    except Exception as e:
        print(f"[session_end] Could not save memory-index state: {e}", file=sys.stderr)


def reindex_changed_memory() -> tuple[int, int, int, int]:
    """Re-index memory files whose CONTENT changed since their stored chunks.

    Change detection is a body hash that ignores the volatile frontmatter
    counters (hits / last_applied / prevented) the hooks themselves bump —
    only real edits trigger an embed. Hashes live in a sidecar state file
    (orchestrator/.memory-index-state.json, per-install like vectors.db).
    On first run after this upgrade the sidecar is seeded from the old mtime
    rule, so files already considered indexed adopt their hash WITHOUT a
    one-time full re-embed.

    Returns (files_reindexed, chunks_added, truncated, failed). truncated/
    failed surface lossy embeds so an oversized memory file can't fail
    silently.
    """
    if not MEMORY_DIR.exists():
        return 0, 0, 0, 0

    try:
        from embedder import Embedder
        from indexer import Indexer
        from vectorstore import VectorStore
    except Exception as e:
        print(f"[session_end] Vector stack unavailable ({e}); skipping memory reindex.", file=sys.stderr)
        return 0, 0, 0, 0

    store = VectorStore(DB_PATH)
    store.init_schema()
    try:
        embedder = Embedder()
    except Exception as e:
        print(f"[session_end] Embedder not available ({e}); skipping memory reindex.", file=sys.stderr)
        return 0, 0, 0, 0

    # Build {source_path: max(last_seen_at)} for memory chunks already in store.
    stored: dict[str, int] = {}
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT source_path, MAX(last_seen_at) FROM chunks "
            "WHERE source_type='memory' GROUP BY source_path"
        )
        for path, ts in cur.fetchall():
            stored[path] = int(ts or 0)
        conn.close()
    except Exception as e:
        print(f"[session_end] Could not query vector store ({e}); skipping memory reindex.", file=sys.stderr)
        return 0, 0, 0, 0

    idx = Indexer(store, embedder, verbose=False)
    state = _load_index_state()
    files_reindexed = 0
    chunks_added = 0
    for p in sorted(MEMORY_DIR.glob("*.md")):
        body_hash = _memory_body_hash(p)
        if body_hash is None:
            continue
        key = str(p)
        if state.get(key) == body_hash:
            continue  # counters may have bumped, but the content is unchanged

        if key not in state:
            # No hash recorded yet. Seed from the legacy mtime rule: if the
            # store already considers this file indexed, adopt the hash
            # without re-embedding (keeps the sidecar upgrade free).
            try:
                mtime = int(p.stat().st_mtime)
            except Exception:
                continue
            last_seen = stored.get(key, 0)
            if mtime <= last_seen + 2:
                state[key] = body_hash
                continue

        try:
            n = idx.index_memory_file(p)
            state[key] = body_hash
            if n > 0:
                files_reindexed += 1
                chunks_added += n
        except Exception as e:
            print(f"[session_end] Failed to re-index {p.name}: {e}", file=sys.stderr)

    # Drop state entries for deleted memory files.
    live = {str(p) for p in MEMORY_DIR.glob("*.md")}
    state = {k: v for k, v in state.items() if k in live}
    _save_index_state(state)

    return (
        files_reindexed,
        chunks_added,
        embedder.stats.get("truncated", 0),
        embedder.stats.get("failed", 0),
    )

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
    parser = argparse.ArgumentParser(description="MS4CC archive/embed hook (Stop hook + /checkpoint step 7)")
    parser.add_argument("--session-id", default=None,
                        help="Explicit session UUID (overrides hook stdin JSON)")
    parser.add_argument("--cwd", default=None,
                        help="Explicit project cwd for ~/.claude/projects resolution (overrides stdin/process cwd)")
    args = parser.parse_args()

    started = time.monotonic()
    hook_input = read_hook_input()

    # Skip entirely if we're not in an MS4CC/orchestrator context
    # (sanity check — the orchestrator dir should exist if we're running).
    if not ORCHESTRATOR_DIR.exists():
        return

    session_jsonl = resolve_session_path(hook_input, args)
    if not session_jsonl:
        print("[session_end] FAILED — no session JSONL found (checked --cwd/stdin/process-cwd/"
              f"install-root candidates under {CLAUDE_PROJECTS_DIR}). Nothing archived or embedded.",
              file=sys.stderr)
        sys.exit(1 if CHECKPOINT_MODE else 0)

    archived, copied = archive_transcript(session_jsonl)
    if archived is None:
        print(f"[session_end] FAILED — session JSONL vanished during archive: {session_jsonl}",
              file=sys.stderr)
        sys.exit(1 if CHECKPOINT_MODE else 0)
    if not copied and not CHECKPOINT_MODE:
        # Per-turn / watchdog with an up-to-date archive: genuinely nothing to do.
        return
    # CHECKPOINT_MODE continues even when the archive was already current —
    # embedding (transcript vectorize + memory reindex) must still run.

    # Embedding (full-transcript vectorize + memory reindex) happens ONLY at
    # /checkpoint (CAIRN_CHECKPOINT_MODE=1). The per-turn Stop hook and the
    # intra-session watchdog archive only — index_transcript re-embeds the
    # ENTIRE transcript on every run, so doing that per-turn pegged the local
    # embedder and ran the machine hot. (Clint directive 2026-05-31.)
    if CHECKPOINT_MODE:
        vec = vectorize_transcript(archived)
        n_chunks = vec["chunks"]
        # Re-index memory files added/edited since last vectorization (mtime check).
        mem_files, mem_chunks, mem_trunc, mem_fail = reindex_changed_memory()
    else:
        vec = {"chunks": -1, "truncated": 0, "failed": 0, "error": None}
        n_chunks = None  # sentinel: deferred to /checkpoint (distinct from -1 "unavailable")
        mem_files = mem_chunks = mem_trunc = mem_fail = 0

    # Hit-counter bumping belongs to the plain per-turn Stop hook only. The
    # watchdog (would inflate counters) and /checkpoint (the per-turn hook
    # already owns it) both skip it.
    incremented = [] if (WATCHDOG_MODE or CHECKPOINT_MODE) else auto_increment_hits(archived)

    # Append a brief note to LOG.md so the activity is visible next session.
    if CHECKPOINT_MODE:
        header = "### Checkpoint-archive"
    elif WATCHDOG_MODE:
        header = "### Watchdog-archive"
    else:
        header = "### Auto-archive"
    if n_chunks is None:
        vec_line = "- Transcript chunks vectorized: deferred to /checkpoint"
        mem_line = "- Memory files re-indexed: deferred to /checkpoint"
    else:
        vec_line = (
            f"- Transcript chunks vectorized: "
            f"{n_chunks if n_chunks >= 0 else 'skipped (vectors unavailable)'}"
        )
        mem_line = f"- Memory files re-indexed: {mem_files} ({mem_chunks} chunks)"
    log_entry = [
        "",
        f"{header} — {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}",
        f"- Archived: `{archived.name}`",
        vec_line,
        mem_line,
    ]

    # Vector health — surface lossy/failed embeds so a silent drop is never
    # mistaken for "nothing new" again (project_scri_vectorization_failure_2026-05-21).
    trunc = vec["truncated"] + mem_trunc
    fail = vec["failed"] + mem_fail
    if vec["error"] or fail or trunc:
        line = f"- ⚠️ Vector health: truncated={trunc}, failed={fail}"
        if vec["error"]:
            line += f", error={vec['error']}"
        log_entry.append(line)
    if not WATCHDOG_MODE and not CHECKPOINT_MODE:
        log_entry.append(
            f"- Memory hits auto-incremented: {len(incremented)}"
            + (f" ({', '.join(incremented[:5])}{'...' if len(incremented) > 5 else ''})" if incremented else "")
        )
    log_path = ORCHESTRATOR_DIR / "LOG.md"
    if log_path.exists():
        try:
            with log_path.open("a") as f:
                f.write("\n".join(log_entry) + "\n")
        except Exception as e:
            print(f"[session_end] Could not write LOG.md entry: {e}", file=sys.stderr)

    # Success summary to stderr — /checkpoint step 7's verification reads this.
    # Silence on success made real failures indistinguishable from "fine"
    # (Hearth, #devops 2026-06-10): a checkpoint that embedded 1,161 chunks and
    # one that crashed both printed nothing.
    if CHECKPOINT_MODE:
        elapsed = time.monotonic() - started
        vec_ok = n_chunks is not None and n_chunks >= 0
        print(
            f"[checkpoint] {'OK' if vec_ok else 'DEGRADED'} in {elapsed:.1f}s — "
            f"archived {archived.name} ({'copied' if copied else 'already current'}); "
            f"transcript chunks +{n_chunks if vec_ok else 0}"
            f"{'' if vec_ok else ' (VECTOR STACK UNAVAILABLE — embed did NOT run)'}; "
            f"memory files re-embedded {mem_files} (+{mem_chunks} chunks); "
            f"vector health: truncated={trunc}, failed={fail}"
            + (f", error={vec['error']}" if vec["error"] else ""),
            file=sys.stderr,
        )
        if not vec_ok:
            sys.exit(1)

if __name__ == "__main__":
    main()
