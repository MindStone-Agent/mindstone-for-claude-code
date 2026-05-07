"""Synapse reference client for MS4CC.

Layout:
- config.py — load synapse.toml + token
- state.py  — active-flag + per-channel cursor persistence (~/.synapse/)
- client.py — thin HTTP wrapper over /v1/* using stdlib urllib
- cli.py    — `python -m orchestrator.integrations.synapse <subcommand>`

Hooks at orchestrator/hooks/synapse_*.py read this module to decide
whether to fetch on session start / per turn.
"""

from .client import SynapseClient, SynapseError  # re-export
from .config import SynapseConfig, load_config
from .state import (
    activate,
    deactivate,
    is_active,
    read_cursor,
    write_cursor,
)


__all__ = [
    "SynapseClient",
    "SynapseError",
    "SynapseConfig",
    "load_config",
    "activate",
    "deactivate",
    "is_active",
    "read_cursor",
    "write_cursor",
]
