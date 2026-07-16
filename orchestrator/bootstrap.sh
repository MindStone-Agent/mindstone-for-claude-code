#!/bin/bash
# bootstrap.sh — Resurrect the active orchestrator on a machine.
#
# Idempotent. Safe to run multiple times.
#
# Steps:
#   1. Create/update Python virtualenv at orchestrator/.venv (uv preferred, pip fallback)
#   2. Install dependencies from pyproject.toml
#   3. Symlink identity files to ~/.claude/ (IDENTITY.md, USER.md, LOG.md)
#   4. Symlink memory directory so Claude Code auto-loads project memories
#   5. Merge hook registrations into ~/.claude/settings.json
#   6. Backfill the vector store from all current memory files (first run only)
#
# Run from anywhere:
#   ./orchestrator/bootstrap.sh
#   or
#   bash /path/to/your/project/orchestrator/bootstrap.sh
#
# Normally you don't run this directly — install.sh (at the MS4CC repo root)
# fetches the framework and then calls this to wire it up.

set -e

ORCHESTRATOR_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$ORCHESTRATOR_DIR")"
CLAUDE_DIR="$HOME/.claude"
VENV_DIR="$ORCHESTRATOR_DIR/.venv"
DB_PATH="$ORCHESTRATOR_DIR/vectors.db"

echo "MindStone for Claude Code — bootstrap"
echo "====================================="
echo "Orchestrator dir: $ORCHESTRATOR_DIR"
echo "Project dir:      $PROJECT_DIR"
echo "Claude Code dir:  $CLAUDE_DIR"
echo ""

mkdir -p "$CLAUDE_DIR"

# ---------------------------------------------------------------------------
# 1 + 2. Create/refresh venv and install dependencies
# ---------------------------------------------------------------------------

echo "[1/5] Python virtualenv at $VENV_DIR..."
if [[ -d "$VENV_DIR" ]]; then
  echo "  OK: venv already exists"
fi

if command -v uv >/dev/null 2>&1; then
  echo "  Using uv"
  (cd "$ORCHESTRATOR_DIR" && uv sync --quiet) || {
    echo "  WARN: uv sync failed; falling back to pip"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet -e "$ORCHESTRATOR_DIR"
  }
else
  echo "  uv not found; using stdlib venv + pip"
  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -e "$ORCHESTRATOR_DIR"
fi

# Verify critical imports
"$VENV_DIR/bin/python" -c "import openai, sqlite_vec" || {
  echo "  ERROR: Dependencies failed to install. Check $VENV_DIR."
  exit 1
}
echo "  OK: openai + sqlite-vec installed in venv"
echo ""

# ---------------------------------------------------------------------------
# 3. Symlink identity files to ~/.claude/
# ---------------------------------------------------------------------------

echo "[2/5] Symlinking identity files..."
for f in IDENTITY.md USER.md LOG.md; do
  target="$ORCHESTRATOR_DIR/$f"
  link="$CLAUDE_DIR/$f"

  if [[ ! -f "$target" ]]; then
    echo "  SKIP: $target does not exist yet (first-run onboarding will create it)"
    continue
  fi

  if [[ -L "$link" ]]; then
    existing=$(readlink "$link")
    if [[ "$existing" == "$target" ]]; then
      echo "  OK:   $link → $target (already correct)"
      continue
    else
      echo "  REPLACE: $link was → $existing"
      rm "$link"
    fi
  elif [[ -e "$link" ]]; then
    echo "  WARN: $link exists and is not a symlink — moving to ${link}.backup"
    mv "$link" "${link}.backup"
  fi

  ln -s "$target" "$link"
  echo "  LINK: $link → $target"
done
echo ""

# ---------------------------------------------------------------------------
# 4. Symlink memory dir so Claude Code auto-loads memories
# ---------------------------------------------------------------------------

echo "[3/5] Symlinking memory directory + slash commands..."
ESCAPED_PATH=$(echo "$PROJECT_DIR" | sed 's|/|-|g')
MEM_LINK="$CLAUDE_DIR/projects/${ESCAPED_PATH}/memory"
MEM_TARGET="$ORCHESTRATOR_DIR/memory"

mkdir -p "$(dirname "$MEM_LINK")"

if [[ -L "$MEM_LINK" ]]; then
  existing=$(readlink "$MEM_LINK")
  if [[ "$existing" == "$MEM_TARGET" ]]; then
    echo "  OK: $MEM_LINK → $MEM_TARGET (already correct)"
  else
    echo "  REPLACE: $MEM_LINK was → $existing"
    rm "$MEM_LINK"
    ln -s "$MEM_TARGET" "$MEM_LINK"
    echo "  LINK: $MEM_LINK → $MEM_TARGET"
  fi
elif [[ -e "$MEM_LINK" ]]; then
  echo "  WARN: $MEM_LINK exists and is not a symlink. Manual migration required."
else
  ln -s "$MEM_TARGET" "$MEM_LINK"
  echo "  LINK: $MEM_LINK → $MEM_TARGET"
fi

# Slash commands: symlink the project's .claude/commands/*.md into ~/.claude/commands/
# so they are available no matter which directory Claude Code is launched from. A
# $HOME launch reads ~/.claude/commands (user scope), NOT the project's project-scope
# commands dir — without this, an operator who doesn't cd into the project before
# launching gets zero commands. The project/repo stays the single source of truth;
# re-running bootstrap (or a git pull on a direct checkout) keeps the symlinks current.
CMD_SRC_DIR="$PROJECT_DIR/.claude/commands"
CMD_LINK_DIR="$CLAUDE_DIR/commands"
if [[ -d "$CMD_SRC_DIR" ]] && compgen -G "$CMD_SRC_DIR/*.md" >/dev/null; then
  mkdir -p "$CMD_LINK_DIR"
  linked=0
  for cmd in "$CMD_SRC_DIR"/*.md; do
    link="$CMD_LINK_DIR/$(basename "$cmd")"
    if [[ -L "$link" ]]; then
      if [[ "$(readlink "$link")" == "$cmd" ]]; then linked=$((linked+1)); continue; fi
      rm "$link"
    elif [[ -e "$link" ]]; then
      mv "$link" "${link}.backup"
    fi
    ln -s "$cmd" "$link"
    linked=$((linked+1))
  done
  echo "  LINK: $CMD_LINK_DIR/*.md → $CMD_SRC_DIR/ ($linked commands)"
fi
echo ""

# ---------------------------------------------------------------------------
# 5. Merge settings fragment into ~/.claude/settings.json
# ---------------------------------------------------------------------------

echo "[4/5] Merging settings fragment..."
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
FRAGMENT_FILE="$ORCHESTRATOR_DIR/settings.fragment.json"

if [[ ! -f "$FRAGMENT_FILE" ]]; then
  echo "  SKIP: settings.fragment.json not found at $FRAGMENT_FILE"
elif ! command -v jq >/dev/null 2>&1; then
  echo "  WARN: jq not installed. Install via: brew install jq (macOS) / apt-get install jq (Linux)"
  echo "        Then manually merge $FRAGMENT_FILE into $SETTINGS_FILE."
else
  TEMP_FRAGMENT=$(mktemp)
  sed "s|\$ORCHESTRATOR_DIR|$ORCHESTRATOR_DIR|g" "$FRAGMENT_FILE" > "$TEMP_FRAGMENT"

  if [[ -f "$SETTINGS_FILE" ]]; then
    cp "$SETTINGS_FILE" "${SETTINGS_FILE}.backup.$(date +%s)"
    # Merge hooks PER-EVENT (orchestrator/lib/merge-settings.jq): re-add the
    # MS4CC-managed entries (command path under $ORCHESTRATOR_DIR) fresh from the
    # fragment so re-runs stay idempotent and don't stack duplicates, while
    # PRESERVING any hook the operator added on top of the framework. The old
    # `.hooks = $frag[0].hooks` overwrite dropped those foreign hooks (#68).
    # Also set autoCompactEnabled from the fragment and deep-merge env (fragment
    # wins). autoCompactEnabled=true + CLAUDE_AUTOCOMPACT_PCT_OVERRIDE are part of
    # the MS4CC compaction-handoff design — harness auto-compact is the backstop,
    # PreCompact is the handoff floor. MS4CC (Claude Code) substrate only, NOT
    # MindStone-proper (its no-compaction rule does not apply). See docs/scri-38-cc-*.md.
    jq --arg orch "$ORCHESTRATOR_DIR" --slurpfile frag "$TEMP_FRAGMENT" \
       -f "$ORCHESTRATOR_DIR/lib/merge-settings.jq" \
       "$SETTINGS_FILE" > "${SETTINGS_FILE}.tmp"
    mv "${SETTINGS_FILE}.tmp" "$SETTINGS_FILE"
    echo "  MERGED: $FRAGMENT_FILE → $SETTINGS_FILE"
    echo "          (backup at ${SETTINGS_FILE}.backup.*)"
  else
    cp "$TEMP_FRAGMENT" "$SETTINGS_FILE"
    echo "  CREATED: $SETTINGS_FILE from fragment"
  fi

  rm "$TEMP_FRAGMENT"
fi
echo ""

# ---------------------------------------------------------------------------
# 6. First-run index backfill
# ---------------------------------------------------------------------------

echo "[5/5] Vector index..."
if [[ -f "$DB_PATH" ]]; then
  existing_count=$("$VENV_DIR/bin/python" -c "
import sys
sys.path.insert(0, '$ORCHESTRATOR_DIR/hooks')
from vectorstore import VectorStore
from pathlib import Path
s = VectorStore(Path('$DB_PATH'))
s.init_schema()
print(s.count())
" 2>/dev/null || echo "0")
  echo "  OK: vectors.db exists with $existing_count chunks"
else
  if [[ -z "$(ls "$ORCHESTRATOR_DIR/memory"/*.md 2>/dev/null)" ]]; then
    echo "  SKIP: no memory files yet. Index will build on first /checkpoint."
  else
    # Preflight the CONFIGURED embedder before the initial index. The embedder is
    # LOCAL-FIRST (hooks/embedder.py: Ollama at 127.0.0.1:11434/v1, model
    # nomic-embed-text; EMBEDDER_BASE_URL / EMBEDDER_MODEL / EMBEDDER_API_KEY
    # override; OPENAI_API_KEY is consulted ONLY for openai.com endpoints). Probe
    # with a real 1-chunk embed — proves the endpoint is reachable AND the model is
    # pulled. A fresh default install needs Ollama + the embed model, NOT an OpenAI
    # key — the old gate here skipped silently whenever OPENAI_API_KEY was absent.
    if preflight=$(cd "$ORCHESTRATOR_DIR/hooks" && "$VENV_DIR/bin/python" - <<'PY' 2>&1
import sys
sys.path.insert(0, ".")
from embedder import Embedder
e = Embedder()
try:
    v = e.embed("bootstrap preflight")
    # embed() degrades to a ZERO VECTOR on failure (and warns on stderr) rather than
    # raising — so the truthful health signal is a non-zero vector, not "no exception".
    if not any(v):
        raise RuntimeError("embedder returned a zero vector (endpoint unreachable or model missing — see warning above)")
    print(f"{e.base_url} model={e.model}")
except Exception as exc:
    print(f"{e.base_url} model={e.model} :: {type(exc).__name__}: {exc}")
    sys.exit(1)
PY
    ); then
      echo "  Embedder reachable ($preflight). Building initial index (this may take a minute)..."
      (cd "$ORCHESTRATOR_DIR/hooks" && "$VENV_DIR/bin/python" indexer.py backfill) || {
        echo "  WARN: Initial indexing failed. The embedder preflight succeeded, so this is"
        echo "        likely a chunking/db issue, not credentials — inspect the output above,"
        echo "        then re-run: orchestrator/hooks/indexer.py backfill"
      }
    else
      echo "  ERROR: embedding endpoint NOT usable — initial index NOT built."
      echo "         $preflight"
      if [[ "$preflight" == *"127.0.0.1:11434"* ]]; then
        echo "         The default embedder is LOCAL Ollama — no OpenAI key is involved. Fix:"
        echo "           1) start Ollama (open the app, or run: ollama serve)"
        echo "           2) pull the embed model shown above:  ollama pull nomic-embed-text"
      elif [[ "$preflight" == *"openai.com"* ]]; then
        echo "         Cloud OpenAI endpoint configured — set OPENAI_API_KEY (or EMBEDDER_API_KEY)."
      else
        echo "         Check EMBEDDER_BASE_URL / EMBEDDER_MODEL / EMBEDDER_API_KEY."
      fi
      echo "         Then re-run orchestrator/bootstrap.sh — or run /checkpoint inside a session;"
      echo "         the index builds there too once the embedder is reachable."
    fi
  fi
fi
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo "====================================="
echo "Bootstrap complete."
echo ""
echo "Next steps:"
echo "  1. Open a fresh Claude Code session in any directory."
echo "  2. Verify the orchestrator identity loads at session start."
echo "  3. If this is a brand-new clone with no IDENTITY.md, the hook will"
echo "     emit a first-run onboarding invitation. Walk through"
echo "     onboarding/IDENTITY.md.example to author your identity."
echo "  4. Run /checkpoint at natural breaks to accumulate memory."
echo "     (The Stop hook auto-archives and vectorizes each session's"
echo "     transcript, even if you don't invoke /checkpoint.)"
echo ""
