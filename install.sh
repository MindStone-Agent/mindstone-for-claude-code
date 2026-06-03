#!/usr/bin/env bash
# install.sh — Install MindStone for Claude Code (MS4CC) into a project.
#
# MS4CC adds a persistent identity and SCRI semantic memory to ANY Claude Code
# install. This installer fetches the MS4CC framework at a pinned version and
# lays it into a target project: the `orchestrator/` engine, the onboarding
# templates, the MS4CC slash commands, and the AGENTS.md orchestration guide.
# It then runs the wire-up step (orchestrator/bootstrap.sh).
#
# It NEVER overwrites your per-user files — your IDENTITY.md, USER.md, LOG.md,
# and your accumulated memory are left untouched. Only framework files are
# written. Re-running it upgrades the framework in place.
#
# Usage:
#   ./install.sh [--project DIR] [--ref GITREF] [--source DIR] [--no-bootstrap]
#
#   --project DIR    Project to install MS4CC into (default: current directory).
#   --ref GITREF     MS4CC version to install — a commit SHA, tag, or branch
#                    (default: main). Recorded in <project>/.ms4cc-version.
#   --source DIR     Install from a local MS4CC checkout instead of fetching
#                    from GitHub (offline use / testing). Overrides --ref.
#   --no-bootstrap   Lay down files but don't run orchestrator/bootstrap.sh.
#
# Access: the MS4CC repo is currently PRIVATE. You need read access to it plus
# either configured git credentials or `gh auth login` (the installer falls back
# to `gh repo clone`, which uses your gh token). Once the repo is public, the
# curl one-liner below also works:
#   curl -fsSL https://raw.githubusercontent.com/R1ngZer0/mindstone-for-claude-code/main/install.sh | bash -s -- --project .
#
# Requirements: git, rsync, and (for the private-repo fetch) the gh CLI.
# bootstrap.sh additionally needs python3, and jq for the settings merge.

set -euo pipefail

REPO_URL="https://github.com/R1ngZer0/mindstone-for-claude-code.git"
REF="main"
PROJECT_DIR="$PWD"
SOURCE_DIR=""
RUN_BOOTSTRAP=1

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)      PROJECT_DIR="$2"; shift 2;;
    --ref)          REF="$2"; shift 2;;
    --source)       SOURCE_DIR="$2"; shift 2;;
    --no-bootstrap) RUN_BOOTSTRAP=0; shift;;
    -h|--help)      sed -n '2,33p' "$0"; exit 0;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2;;
  esac
done

log()     { printf '  %s\n' "$*"; }
section() { printf '\n[%s] %s\n' "$1" "$2"; }

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "ERROR: --project dir does not exist: $PROJECT_DIR" >&2
  exit 1
fi
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

for tool in git rsync; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' is required but not found." >&2; exit 1; }
done

echo "MindStone for Claude Code — installer"
echo "====================================="
echo "Target project: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# 1. Obtain the MS4CC source tree at the requested version
# ---------------------------------------------------------------------------
CLEANUP_TMP=""
cleanup() {
  # NB: an `if` block (not `[[ ]] && ...`) so the trap returns 0 even when there's
  # no temp dir — otherwise the failed test would become the script's exit code.
  if [[ -n "$CLEANUP_TMP" ]]; then
    rm -rf "$CLEANUP_TMP"
  fi
}
trap cleanup EXIT

if [[ -n "$SOURCE_DIR" ]]; then
  [[ -d "$SOURCE_DIR" ]] || { echo "ERROR: --source dir does not exist: $SOURCE_DIR" >&2; exit 1; }
  SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
  RESOLVED_REF="$( (cd "$SOURCE_DIR" && git rev-parse HEAD 2>/dev/null) || echo "local" )"
  section 1 "Using local MS4CC source: $SOURCE_DIR ($RESOLVED_REF)"
else
  section 1 "Fetching MS4CC ($REF) from $REPO_URL"
  CLEANUP_TMP="$(mktemp -d)"
  # Try a plain clone first; if it fails (e.g. the repo is private and git has no
  # creds), fall back to `gh repo clone`, which authenticates via the gh token.
  if ! git clone --quiet "$REPO_URL" "$CLEANUP_TMP/ms4cc" 2>/dev/null; then
    if command -v gh >/dev/null 2>&1 && gh repo clone R1ngZer0/mindstone-for-claude-code "$CLEANUP_TMP/ms4cc" -- --quiet 2>/dev/null; then
      log "fetched via gh (private-repo auth)"
    else
      echo "ERROR: couldn't fetch MS4CC from $REPO_URL." >&2
      echo "       The repo is private — ensure you have access and either git" >&2
      echo "       credentials or 'gh auth login'. Or use --source <local checkout>." >&2
      exit 1
    fi
  fi
  ( cd "$CLEANUP_TMP/ms4cc" && git checkout --quiet "$REF" )
  SOURCE_DIR="$CLEANUP_TMP/ms4cc"
  RESOLVED_REF="$( cd "$SOURCE_DIR" && git rev-parse HEAD )"
  log "Resolved $REF → $RESOLVED_REF"
fi

# Sanity-check that the source really is an MS4CC checkout.
if [[ ! -d "$SOURCE_DIR/orchestrator/hooks" ]]; then
  echo "ERROR: $SOURCE_DIR doesn't look like MS4CC (no orchestrator/hooks/)." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Mirror the pure-engine framework (exact match to MS4CC — divergence here
#    is a bug, so these dirs are mirrored with deletion of stale files).
# ---------------------------------------------------------------------------
section 2 "Installing engine (exact mirror)"

MIRROR_DIRS=(
  "orchestrator/hooks"
  "orchestrator/integrations"
  "orchestrator/runbooks"
  "orchestrator/templates"
)
for rel in "${MIRROR_DIRS[@]}"; do
  src="$SOURCE_DIR/$rel"
  [[ -d "$src" ]] || { log "skip (absent in source): $rel"; continue; }
  mkdir -p "$PROJECT_DIR/$rel"
  rsync -a --delete "$src"/ "$PROJECT_DIR/$rel"/
  log "mirrored: $rel/"
done

# Single framework files (overwrite in place).
FRAMEWORK_FILES=(
  "orchestrator/bootstrap.sh"
  "orchestrator/settings.fragment.json"
  "orchestrator/pyproject.toml"
  "orchestrator/uv.lock"
  "orchestrator/BOOTSTRAP.md"
  "orchestrator/ROADMAP.md"
  "orchestrator/config/synapse.example.toml"
  "orchestrator/config/project_hints.example.toml"
  "orchestrator/memory/.migrate_frontmatter.py"
  "AGENTS.md"
)
for rel in "${FRAMEWORK_FILES[@]}"; do
  src="$SOURCE_DIR/$rel"
  [[ -e "$src" ]] || { log "skip (absent in source): $rel"; continue; }
  mkdir -p "$PROJECT_DIR/$(dirname "$rel")"
  cp "$src" "$PROJECT_DIR/$rel"
  log "installed: $rel"
done
[[ -x "$PROJECT_DIR/orchestrator/bootstrap.sh" ]] || chmod +x "$PROJECT_DIR/orchestrator/bootstrap.sh" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. Overlay framework templates/docs (additive — never delete consumer extras)
# ---------------------------------------------------------------------------
section 3 "Installing onboarding templates"
if [[ -d "$SOURCE_DIR/onboarding" ]]; then
  mkdir -p "$PROJECT_DIR/onboarding"
  rsync -a "$SOURCE_DIR/onboarding"/ "$PROJECT_DIR/onboarding"/
  log "overlaid: onboarding/"
fi

# ---------------------------------------------------------------------------
# 4. Seed memory templates ONLY if absent (never clobber the user's memory)
# ---------------------------------------------------------------------------
section 4 "Seeding memory templates (only if absent)"
SEED_FILES=(
  "orchestrator/memory/MEMORY.md"
  "orchestrator/memory/design_synapse.md"
)
for rel in "${SEED_FILES[@]}"; do
  src="$SOURCE_DIR/$rel"; dst="$PROJECT_DIR/$rel"
  [[ -e "$src" ]] || { log "skip (absent in source): $rel"; continue; }
  if [[ -e "$dst" ]]; then log "keep existing: $rel"; continue; fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  log "seeded: $rel"
done

# ---------------------------------------------------------------------------
# 5. Install MS4CC slash commands (overwrite same-named; leave the rest)
# ---------------------------------------------------------------------------
section 5 "Installing MS4CC slash commands"
if compgen -G "$SOURCE_DIR/.claude/commands/*.md" >/dev/null; then
  mkdir -p "$PROJECT_DIR/.claude/commands"
  for cmd in "$SOURCE_DIR"/.claude/commands/*.md; do
    cp "$cmd" "$PROJECT_DIR/.claude/commands/"
    log "command: $(basename "$cmd")"
  done
else
  log "skip (no commands in source)"
fi

# ---------------------------------------------------------------------------
# 6. Record the installed version (track this file in your project)
# ---------------------------------------------------------------------------
section 6 "Recording installed version"
{
  echo "ref=$RESOLVED_REF"
  echo "requested=$REF"
  echo "installed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "repo=$REPO_URL"
} > "$PROJECT_DIR/.ms4cc-version"
log "wrote .ms4cc-version ($RESOLVED_REF)"

# ---------------------------------------------------------------------------
# 7. Wire up (bootstrap)
# ---------------------------------------------------------------------------
if [[ "$RUN_BOOTSTRAP" -eq 1 ]]; then
  section 7 "Wiring up (orchestrator/bootstrap.sh)"
  bash "$PROJECT_DIR/orchestrator/bootstrap.sh"
else
  section 7 "Skipping wire-up (--no-bootstrap)"
  log "Run later: bash $PROJECT_DIR/orchestrator/bootstrap.sh"
fi

echo ""
echo "====================================="
echo "MS4CC install complete → $PROJECT_DIR"
echo "Version: $RESOLVED_REF"
echo ""
echo "Next: open a fresh Claude Code session in $PROJECT_DIR."
echo "If there's no orchestrator/IDENTITY.md yet, the first-run invitation will"
echo "guide you through onboarding/IDENTITY.md.example to author your identity."
