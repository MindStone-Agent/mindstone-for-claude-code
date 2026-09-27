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

    # ---- Pass 3: ANCESTOR project dirs, before any global guess. -----------
    # Claude Code keys the project dir on the directory the session was LAUNCHED
    # from, not the project being worked in. Launching from $HOME and working in
    # ~/Projects/<repo> puts every transcript under `-Users-<user>/` while
    # `-Users-<user>-Projects-<repo>/` exists holding nothing.
    #
    # So the launch dir is usually an ANCESTOR of the working dir, which makes
    # this a derivation rather than a guess: walk each candidate's parents and
    # look there. Observed live 2026-08-26 -- /checkpoint step 7 (invoked with
    # `< /dev/null`, hence no session id) failed outright while a 611 MB
    # transcript sat under the parent dir.
    seen: set[Path] = set()
    for cwd in cwd_candidates:
        for ancestor in Path(cwd).parents:
            project_dir = CLAUDE_PROJECTS_DIR / str(ancestor).replace("/", "-")
            if project_dir in seen or not project_dir.exists():
                continue
            seen.add(project_dir)
            jsonls = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if jsonls:
                return jsonls[0]

    # ---- Pass 4: global most-recent. A GUESS, and it says so. --------------
    # Only reached when no candidate dir and no ancestor dir holds a transcript.
    # Deliberately NOT silent: this can return another project's session, which
    # is the failure mode that makes "most recent anywhere" dangerous -- with
    # several project dirs holding zero transcripts, "most recent anywhere"
    # resolves to whatever unrelated project was touched last. The caller prints
    # what it archived, and this warns, so the guess is visible on two channels.
    if CLAUDE_PROJECTS_DIR.exists():
        jsonls = sorted(
            CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if jsonls:
            print(f"[session_end] WARNING — no transcript under any candidate or ancestor project "
                  f"dir; falling back to the most recently modified transcript ANYWHERE: "
                  f"{jsonls[0].parent.name}/{jsonls[0].name}. This may belong to a different "
                  f"project. Pass --session-id/--cwd to resolve exactly.", file=sys.stderr)
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

# The transcript is JSONL: one RECORD per physical line. Record SHAPE is the
# anchor — not the tag text (#96).
#
# ALLOWLIST, NOT DENYLIST. The first structural version dropped only
# `hook_additional_context` records and credited everything else. Claude Code
# writes many other record types that name memory files without anyone using
# them — `file-history-snapshot` (whose trackedFileBackups list up to 91 memory
# paths), `hook_success` (the SessionStart output, i.e. the whole memory index),
# `edited_text_file`, `file`, `system`, tool results. Measured 2026-09-25: 447 of
# 729 Stop runs credited 102–116 of 117 memories, so `hits` was counting turns
# again (#91 by another route) and `last_applied` pinned decay at ~1.0. A denylist
# fails OPEN on every record type added later; an allowlist fails closed.
#
# A citation is text a human or the model WROTE:
#   - assistant records: `text`, `thinking`, and `tool_use` input, except a
#     write whose target is a memory file (see _writes_memory below);
#   - user records: human-typed text only — not `tool_result` blocks, not
#     `isMeta` records, not compaction summaries (`isCompactSummary`: the old
#     context read back, 86% of user-record credits when they were counted), and
#     not harness-generated strings (task notifications, slash-command wrappers,
#     local or `!` shell output, system reminders);
#   - `queued_command` attachments in prompt mode: messages the human typed while
#     the model was busy (#107), filtered by the same harness prefixes.
# Writing a memory file is not using it (Cairn, #112): the Write that created
# it, index edits naming every memory, and checkpoint counter edits would
# otherwise credit it, so nothing could ever recount to zero. The rule is the
# same for every tool: a write whose TARGET is a memory file doesn't count;
# writing another file that names a memory (a LOG entry, a handoff) is authored
# text and does.
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# A shell command that writes to a memory file. Linear-time on any input: no
# unbounded bridge between "memory/" and the write operation.
_SHELL_MEMORY_WRITE = re.compile(
    r"(?<![\w>=-])>>?\s*[\"']?\S*memory/\S*\.md"   # > / >> into memory/*.md (not `->`, not `<dir>/memory`)
    r"|\btee\b[^\n]*memory/\S*\.md"                # | tee [-a] memory/x.md
    r"|\b(?:sed\s+-i|perl\s+-\w*i\b)[^\n]*memory/" # in-place edit
)
_OPEN_W = r"write_text\(|open\([^()\n]*,\s*(?:mode\s*=\s*)?[\"'][rbt+]*[wax]"  # the mode arg, not a path
_SCRIPT_WRITE = re.compile(_OPEN_W)
_ANY_WRITE = re.compile(r"(?<![\w>=-])>>?\s*[\"']?[^\s\"'|;&]+\.md\b|\btee\b|\bsed\s+-i|\bperl\s+-\w*i\b|" + _OPEN_W)
_CD_MEMORY = re.compile(r"\bcd\s+[\"']?\S*memory/?[\"']?(?=[\s;&|]|$)", re.M)


def _writes_memory(cmd: str) -> bool:
    if _SHELL_MEMORY_WRITE.search(cmd):
        return True
    if "memory/" in cmd and _SCRIPT_WRITE.search(cmd):
        return True
    # `cd .../memory && cat >> x.md`: the target is a bare name relative to it.
    return bool(_CD_MEMORY.search(cmd) and _ANY_WRITE.search(cmd))


_HARNESS_USER_PREFIXES = (
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
    "Caveat: The messages below were generated",
)


def _authored_strings(rec: dict) -> list[str]:
    """The human- or model-written text in one transcript record (allowlist)."""
    rtype = rec.get("type")
    att = rec.get("attachment")
    if rtype == "attachment" and isinstance(att, dict) and att.get("type") == "queued_command":
        if att.get("commandMode") != "prompt":
            return []
        prompt = att.get("prompt")
        blocks = [{"type": "text", "text": prompt}] if isinstance(prompt, str) else prompt
        return [b["text"] for b in (blocks if isinstance(blocks, list) else [])
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                and not b["text"].lstrip().startswith(_HARNESS_USER_PREFIXES)]
    msg = rec.get("message")
    if rtype not in ("assistant", "user") or not isinstance(msg, dict):
        return []
    if rtype == "user" and (rec.get("isMeta") or rec.get("isCompactSummary")):
        return []
    content = msg.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    out: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if rtype == "assistant":
            if bt == "text" and isinstance(b.get("text"), str):
                out.append(b["text"])
            elif bt == "thinking" and isinstance(b.get("thinking"), str):
                out.append(b["thinking"])
            elif bt == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                target = inp.get("file_path") or inp.get("notebook_path")
                if (b.get("name") in _WRITE_TOOLS and isinstance(target, str)
                        and "/memory/" in target and target.endswith(".md")):
                    continue
                cmd = inp.get("command")
                if isinstance(cmd, str) and _writes_memory(cmd):
                    continue
                out.append(json.dumps(inp, ensure_ascii=False))
        elif bt == "text" and isinstance(b.get("text"), str):
            text = b["text"]
            if not text.lstrip().startswith(_HARNESS_USER_PREFIXES):
                out.append(text)
    return out


def authored_text(chunk: str) -> str:
    """Return only text a human or the model wrote, dropping hook injections.

    WHY NOT A TAG REGEX (#96, found by Cairn)
    -----------------------------------------
    The first version matched `<orchestrator-context>...</orchestrator-context>`
    with DOTALL. Those tag names also occur in ORDINARY CONTENT — most sharply in
    this repo's own source, where `assemble_context`'s docstring literally says
    "Build the `<orchestrator-context>` block". A mere mention opened a match,
    `.*?` then ran forward to the next real closing tag, and every genuine
    citation in between was deleted. Cairn measured 10 such records in one 398 MB
    archive and 66% of a test document removed.

    The bias was the worst part: it preferentially destroyed citations from
    sessions that WORK ON THE MEMORY SYSTEM — the sessions where a citation
    carries the most signal. And because the watermark advances regardless, those
    bytes are never rescanned, so the loss is permanent.

    My stated justification was wrong too. I claimed over-stripping "under-counts,
    which is the safe direction." That holds inside a complete document; it fails
    at a WINDOW boundary, which the watermark makes the normal case — a window
    starting or ending mid-region leaves an unmatched tag and OVER-counts.

    So the filter is structural instead. JSONL is one record per line, which makes
    cross-record bridging impossible, and injections are identifiable by record
    shape rather than by text that anything may quote.

    THIS FILTER IS LOAD-BEARING, not belt-and-braces. `semantic-recall` fires on
    EVERY user turn and names the memories it recalled, all of it inside the
    watermark window. Without this, every recalled memory is credited every turn
    and #91 returns in a milder form.

    Parsed records go through `_authored_strings` (an allowlist; see the comment
    above it). Only the authored TEXT is returned, not the raw record, so names in
    metadata fields (paths, file backups, tool results) cannot match.
    """
    out = []
    for line in chunk.splitlines():
        s = line.strip()
        if not s:
            continue
        if not s.startswith("{"):
            # Not a JSON record (a torn line, or a plain-text file). Keep it:
            # dropping unrecognised text would silently lose real citations, and
            # this filter has already cost us once by discarding too much.
            out.append(line)
            continue
        try:
            rec = json.loads(s)
        except Exception:  # noqa: BLE001
            out.append(line)
            continue
        if not isinstance(rec, dict):
            continue
        out.extend(_authored_strings(rec))
    return "\n".join(out)


def auto_increment_hits(archived_path: Path) -> list[str]:
    """Credit a memory with a citation when its name appears in NEW authored text.

    Two filters, both load-bearing:

    1. **Only the bytes written since the last scan.** This is the one that
       mattered. The scan used to read the WHOLE cumulative transcript every
       turn, so a memory mentioned once was re-credited on every turn forever
       after. `hits` therefore measured turns-elapsed-since-first-mention, i.e.
       file age (measured: r=0.85 with age, and 103/103 files credited on every
       single run), and `last_applied` was stamped to today for every file every
       turn — which pinned the exponential decay term near 1.0 permanently and
       meant the half-life mechanism had NEVER ONCE fired.

       Measured on a 617 MB transcript: scanning the last 50 KB credits 1 memory,
       the last 500 KB credits 4, the whole file credits all 103.

    2. **Not text the hooks injected.** The memory index names every memory that
       exists; crediting a name found there is the system reading its own output
       back as evidence of use. Secondary to (1) but independently correct, and
       it is what keeps a fresh install from self-crediting on turn one.

    A counter with no input that produces zero is not a measurement. See #91.

    Returns list of memory filenames that were incremented.
    """
    if not archived_path.exists():
        return []
    if not MEMORY_DIR.exists():
        return []

    # Resume from wherever the previous scan stopped. Keyed by filename so a
    # re-archive of the same session continues rather than restarting.
    state = _load_index_state()
    offsets = state.setdefault("hit_scan_offsets", {})
    key = archived_path.name
    try:
        size = archived_path.stat().st_size
    except Exception:
        return []
    start = int(offsets.get(key, 0) or 0)
    # Truncated or replaced file — never seek past EOF and silently scan nothing.
    if start > size:
        start = 0

    try:
        with archived_path.open("rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except Exception:
        return []

    # LINE-ALIGN the watermark. `archive_transcript` copies a JSONL that Claude
    # Code may be appending to concurrently, so the last record can be TORN.
    # Advancing to EOF across a partial record would scan half of it now and skip
    # the rest forever; stopping at the last newline means the torn record is
    # simply re-read next time, when it is whole. (Cairn's note on #93.)
    cut = raw.rfind(b"\n") + 1
    if cut <= 0:
        # No complete record in the window yet — leave the watermark alone and
        # come back when there is one. Advancing here would skip the record.
        return []
    transcript_text = raw[:cut].decode("utf-8", errors="ignore")

    # Record the new watermark even if nothing matches, so an unproductive turn
    # does not leave the window open to be rescanned next time.
    offsets[key] = start + cut
    _save_index_state(state)

    transcript_text = authored_text(transcript_text)
    if not transcript_text.strip():
        return []

    incremented: list[str] = []
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    for name, path in cited_memories(transcript_text).items():
        if _bump_memory_hits(path, today):
            incremented.append(name)

    return incremented


def cited_memories(authored: str) -> dict[str, Path]:
    """Memory files whose name appears in already-filtered authored text.

    Shared by the per-turn counter and the runbook's recount, so both credit by
    exactly the same rule.
    """
    cited: dict[str, Path] = {}
    for path in MEMORY_DIR.glob("*.md"):
        name = path.name
        if name == "MEMORY.md":
            continue  # the index names every memory; it is not one
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
        if any(re.search(p, authored) for p in patterns):
            cited[name] = path
    return cited

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

    # Failure exits NON-ZERO in every mode, not just checkpoint mode.
    #
    # This used to be `sys.exit(1 if CHECKPOINT_MODE else 0)`, so a failed archive
    # reported success on the per-turn Stop-hook path -- the path that runs on
    # EVERY turn completion, roughly a hundred times more often than /checkpoint.
    # The mode that almost never runs was the loud one, and the mode that is the
    # actual safety net was silent. An unarchived transcript is a real failure
    # whoever invoked it, and a hook exiting 0 after printing FAILED is precisely
    # the "check that cannot fail" shape this codebase keeps getting bitten by.
    #
    # Exiting non-zero from a Stop hook does not interrupt the session; it surfaces
    # the failure where it can be seen instead of leaving it in stderr nobody reads.
    session_jsonl = resolve_session_path(hook_input, args)
    if not session_jsonl:
        print("[session_end] FAILED — no session JSONL found (checked --cwd/stdin/process-cwd/"
              f"install-root candidates, and their ancestors, under {CLAUDE_PROJECTS_DIR}). "
              "Nothing archived or embedded.", file=sys.stderr)
        sys.exit(1)

    archived, copied = archive_transcript(session_jsonl)
    if archived is None:
        print(f"[session_end] FAILED — session JSONL vanished during archive: {session_jsonl}",
              file=sys.stderr)
        sys.exit(1)
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

        # Store health, reported where it will actually be read. The index is
        # hand-maintained on purpose (a curated description beats a generated one),
        # which means a skipped step is invisible forever unless something checks.
        # 16 of 102 memories had drifted out of the index over three months before
        # anyone looked. Warn, never fail: the checkpoint's job is persistence, and
        # refusing to persist a session because an index line is missing would
        # trade a small gap for a large one.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runbooks"))
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from invariant_audit import index_check, degeneracy  # noqa: E402
            # Reuse the REAL parser rather than writing a second one here: a
            # divergent copy would disagree with what actually gets injected,
            # and then this check would be auditing a fiction.
            from session_start import parse_frontmatter  # noqa: E402
            n_unindexed, unindexed = index_check()
            no_inv, degen = [], []
            for p in sorted(MEMORY_DIR.glob("*.md")):
                if p.name == "MEMORY.md":
                    continue
                fm, _ = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
                if not fm.get("critical"):
                    continue
                inv = (fm.get("invariant") or "").strip()
                if not inv:
                    no_inv.append(p.name)
                elif degeneracy(inv, fm.get("description", "")):
                    degen.append(p.name)
            if n_unindexed or no_inv or degen:
                bits = []
                if n_unindexed:
                    bits.append(f"{n_unindexed} memory(s) missing an index pointer "
                                f"({', '.join(unindexed[:3])}{'…' if n_unindexed > 3 else ''})")
                if no_inv:
                    bits.append(f"{len(no_inv)} critical without an invariant "
                                f"({', '.join(no_inv[:3])}{'…' if len(no_inv) > 3 else ''})")
                if degen:
                    bits.append(f"{len(degen)} degenerate invariant(s) "
                                f"({', '.join(degen[:3])}{'…' if len(degen) > 3 else ''})")
                print(f"[checkpoint] STORE HEALTH: " + "; ".join(bits)
                      + ". Run orchestrator/runbooks/invariant_audit.py", file=sys.stderr)
        except Exception as e:
            print(f"[checkpoint] store-health check unavailable ({e})", file=sys.stderr)

        if not vec_ok:
            sys.exit(1)

if __name__ == "__main__":
    main()
