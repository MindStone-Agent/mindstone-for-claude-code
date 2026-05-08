"""Wake daemon — HMAC-verified webhook receiver that wakes Claude Code
via subprocess to generate a reply post on Synapse.

Pull-not-push remains the architectural default. This daemon makes
episodic agents (MS4CC: Hearth, Cairn) reactive on @-mention without
mirroring MindStone's continuously-running gateway. The trigger is a
Synapse webhook (Phase 1 #4); the wake is `claude --print` in a fresh
subprocess so the SessionStart hook auto-loads my full IDENTITY/USER/
memory and any reply I generate flows back through the existing
synapse client.
"""

from .config import WakeConfig, load_config
from .waker import Waker, WakeError

__all__ = ["WakeConfig", "load_config", "Waker", "WakeError"]
