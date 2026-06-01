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
#   bash ~/Projects/MFC/testflight/orchestrator/bootstrap.sh

set -e

ORCHESTRATOR_DIR="$(cd "$(dirname "$0")" && pwd)"
TESTFLIGHT_DIR="$(dirname "$ORCHESTRATOR_DIR")"
CLAUDE_DIR="$HOME/.claude"
VENV_DIR="$ORCHESTRATOR_DIR/.venv"
DB_PATH="$ORCHESTRATOR_DIR/vectors.db"

echo "MindStone for Claude Code — bootstrap"
echo "====================================="
echo "Orchestrator dir: $ORCHESTRATOR_DIR"
echo "TestFlight dir:   $TESTFLIGHT_DIR"
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

echo "[3/5] Symlinking memory directory..."
ESCAPED_PATH=$(echo "$TESTFLIGHT_DIR" | sed 's|/|-|g')
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
    # Overwrite `hooks` entirely (prevents duplicate hook entries across re-runs),
    # set autoCompactEnabled from the fragment, and deep-merge env (fragment wins).
    # autoCompactEnabled=true + CLAUDE_AUTOCOMPACT_PCT_OVERRIDE are part of the
    # MS4CC compaction-handoff design — harness auto-compact is the backstop,
    # PreCompact is the handoff floor. MS4CC (Claude Code) substrate only, NOT
    # MindStone-proper (its no-compaction rule does not apply). See docs/scri-38-cc-*.md.
    jq --slurpfile frag "$TEMP_FRAGMENT" \
       '.hooks = $frag[0].hooks
        | (if $frag[0].autoCompactEnabled != null then .autoCompactEnabled = $frag[0].autoCompactEnabled else . end)
        | .env = ((.env // {}) + ($frag[0].env // {}))' \
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
from vectorstore import VectorStore
from pathlib import Path
import sys
sys.path.insert(0, '$ORCHESTRATOR_DIR/hooks')
s = VectorStore(Path('$DB_PATH'))
s.init_schema()
print(s.count())
" 2>/dev/null || echo "0")
  echo "  OK: vectors.db exists with $existing_count chunks"
else
  echo "  Building initial index (this may take a minute)..."
  if [[ -n "$(ls "$ORCHESTRATOR_DIR/memory"/*.md 2>/dev/null)" ]] && [[ -f "$HOME/.config/openai-api-key" || -n "$OPENAI_API_KEY" ]]; then
    (cd "$ORCHESTRATOR_DIR/hooks" && "$VENV_DIR/bin/python" indexer.py backfill) || {
      echo "  WARN: Initial indexing failed. Check your OpenAI API key."
    }
  else
    echo "  SKIP: no memory files yet OR no OPENAI_API_KEY / ~/.config/openai-api-key."
    echo "        Index will build on first /checkpoint or first Stop hook fire."
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
