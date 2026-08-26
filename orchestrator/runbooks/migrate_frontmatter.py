#!/usr/bin/env python3
"""One-time migration script to bring all memory files up to Cairn v0.2 frontmatter schema.

Idempotent — running twice leaves state unchanged.
Adds missing fields; preserves existing values (e.g., `name`, `description`, `type`).
"""

import re
import sys
from pathlib import Path
from datetime import datetime

MEMORY_DIR = Path(__file__).parent
TODAY = "2026-04-24"

# The 5 load-bearing critical feedback files (per MEMORY.md context).
CRITICAL_FEEDBACK = {
    "feedback_never_destructive_git.md",
    "feedback_no_fsmonitor.md",
    "feedback_verify_before_rebuild.md",
    "feedback_stop_guessing.md",
    "feedback_canonical_framework_verification.md",
}

# Reference files and identity/user files are evergreen (don't decay).
EVERGREEN_PREFIXES = ("reference_",)
EVERGREEN_EXACT = {"MEMORY.md", "IDENTITY.md", "USER.md", "LOG.md"}

# Type inference from filename.
def infer_type(name: str) -> str:
    if name.startswith("feedback_"):
        return "feedback"
    if name.startswith("project_"):
        return "project"
    if name.startswith("reference_"):
        return "reference"
    if name.startswith("CAIRN_DESIGN"):
        return "design"
    if name == "MEMORY.md":
        return "index"
    # Unclassified legacy files get generic "memory".
    return "memory"

# Tag inference — simple keyword list from filename stem.
def infer_tags(name: str) -> list[str]:
    stem = name.replace(".md", "")
    # Strip the type prefix for tag purposes.
    for prefix in ("feedback_", "project_", "reference_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    # Tags are the stem split on underscores, skipping short tokens.
    return [t for t in stem.split("_") if len(t) > 2]

# Project inference — which projects does this memory apply to?
# Example install-specific map — replace the keys/values with your own
# project keywords and tags.
PROJECT_HINTS = {
    "webapp": "web-app",
    "my-web-app": "web-app",
    "billing": "billing",
    "infra": "infrastructure",
}

def infer_projects(name: str, tags: list[str]) -> list[str]:
    matches = set()
    name_lower = name.lower()
    for hint, project in PROJECT_HINTS.items():
        if hint in name_lower:
            matches.add(project)
    return sorted(matches)

V2_DEFAULTS = {
    "tags": None,           # inferred
    "projects": None,       # inferred
    "hits": 0,
    "prevented": 0,
    "last_applied": None,
    "created": TODAY,       # best-guess; can be refined manually
    "half_life_days": 30,
    "critical": False,
    "evergreen": False,
}

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Preserves order-insensitive key-value pairs."""
    if not text.startswith("---\n"):
        return {}, text
    # Find closing ---
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    fm = {}
    current_key = None
    for line in block.split("\n"):
        if not line.strip():
            continue
        # Simple "key: value" parser (doesn't handle nested, but we don't need that).
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            # Parse common value shapes.
            if val == "":
                fm[key] = None
            elif val.lower() == "true":
                fm[key] = True
            elif val.lower() == "false":
                fm[key] = False
            elif val.lower() in ("null", "~"):
                fm[key] = None
            elif re.match(r"^-?\d+$", val):
                fm[key] = int(val)
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                fm[key] = [s.strip().strip('"').strip("'") for s in inner.split(",")] if inner else []
            elif val.startswith('"') and val.endswith('"'):
                fm[key] = val[1:-1]
            else:
                fm[key] = val
            current_key = key
    return fm, body

def format_frontmatter(fm: dict) -> str:
    """Format dict as YAML-ish frontmatter, preserving a canonical key order."""
    order = [
        "name", "description", "type", "tags", "projects",
        "hits", "prevented", "last_applied", "created",
        "half_life_days", "critical", "evergreen",
        "status", "author", "date", "supersedes",  # design-doc keys
        "originSessionId",                          # legacy key
    ]
    # Unknown keys append at end.
    keys = [k for k in order if k in fm] + [k for k in fm if k not in order]
    lines = ["---"]
    for k in keys:
        v = fm[k]
        if v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                joined = ", ".join(str(x) for x in v)
                lines.append(f"{k}: [{joined}]")
        elif isinstance(v, str):
            if ":" in v or "#" in v or v.startswith(("[", "{", "&", "*", "!", "|", ">")):
                # Quote when the value contains YAML-meaningful chars.
                escaped = v.replace('"', '\\"')
                lines.append(f'{k}: "{escaped}"')
            else:
                lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"

def migrate(path: Path) -> bool:
    """Migrate one file. Returns True if changed."""
    text = path.read_text()
    fm, body = parse_frontmatter(text)

    name = path.name

    # Set derived defaults.
    if "type" not in fm:
        fm["type"] = infer_type(name)
    if "name" not in fm:
        fm["name"] = name.replace(".md", "")
    if "description" not in fm:
        # First non-blank line of body as a fallback description.
        first = next((line.strip() for line in body.split("\n") if line.strip() and not line.startswith("#")), "")
        fm["description"] = first[:200] if first else f"Auto-migrated: {name}"

    # v0.2 schema fields.
    if "tags" not in fm or fm["tags"] is None:
        fm["tags"] = infer_tags(name)
    if "projects" not in fm or fm["projects"] is None:
        fm["projects"] = infer_projects(name, fm.get("tags", []))
    fm.setdefault("hits", 0)
    fm.setdefault("prevented", 0)
    fm.setdefault("last_applied", None)
    fm.setdefault("created", TODAY)
    fm.setdefault("half_life_days", 30)

    # Critical flag.
    if name in CRITICAL_FEEDBACK:
        fm["critical"] = True
    else:
        fm.setdefault("critical", False)

    # Evergreen flag.
    if name in EVERGREEN_EXACT or any(name.startswith(p) for p in EVERGREEN_PREFIXES):
        fm["evergreen"] = True
    else:
        fm.setdefault("evergreen", False)

    # Design docs are evergreen personal artifacts.
    if fm.get("type") == "design":
        fm["evergreen"] = True

    new_text = format_frontmatter(fm) + body.lstrip("\n")
    if new_text == text:
        return False
    path.write_text(new_text)
    return True

def main():
    changed = []
    unchanged = []
    for path in sorted(MEMORY_DIR.glob("*.md")):
        try:
            if migrate(path):
                changed.append(path.name)
            else:
                unchanged.append(path.name)
        except Exception as e:
            print(f"ERROR: {path.name}: {e}", file=sys.stderr)
            raise
    print(f"Changed: {len(changed)}")
    for n in changed:
        print(f"  {n}")
    print(f"Unchanged: {len(unchanged)}")

if __name__ == "__main__":
    main()
