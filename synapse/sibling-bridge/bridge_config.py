"""Shared constants for the sibling bridge. Single source of truth.

To add or rename agents: update VALID_AGENTS and update CONFIG_NAME_MAP in relay.py.
"""

# Agent names must be lowercase. Add your instance names here.
VALID_AGENTS = {"raven", "warden", "lyra"}

VALID_PRIORITIES = {"normal", "urgent", "fyi"}

# Maximum body size in bytes. Protects against accidental large payloads
# (voice transcription + full AI response). Raise if legitimate use requires it.
MAX_BODY_BYTES = 16 * 1024  # 16 KB

# Bump when the message schema changes incompatibly.
SCHEMA_VERSION = 1
