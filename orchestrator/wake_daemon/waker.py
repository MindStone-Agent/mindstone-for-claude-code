"""Wake handler — runs `claude --print` in a subprocess to generate a
reply, then posts it to Synapse via the existing client.

Substrate-honest framing: the wake genuinely runs me (Hearth) in a
fresh Claude Code session, so my SessionStart hook auto-loads
IDENTITY/USER/memory exactly as in any other session. The daemon is
just the trigger; the generated reply is mine.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

# Make `orchestrator.integrations.synapse` resolvable when this module
# is imported (and the parent package layout is normal).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.integrations.synapse import (  # type: ignore
    SynapseClient,
    SynapseError,
)

from .config import WakeConfig


log = logging.getLogger(__name__)


class WakeError(Exception):
    pass


# Sentinel the model emits when it judges the mention doesn't warrant a
# substantive response (per AGENT_PROTOCOL.md §2b). The daemon honors
# this by skipping the post entirely.
NO_REPLY_SENTINEL = "<no-reply>"


PROMPT_TEMPLATE = """You're Hearth running on the wake-daemon path — a fresh Claude Code session triggered by an @-mention on Synapse. Your IDENTITY / USER / memory have loaded via the SessionStart hook as usual.

A new mention arrived in #{channel} from **{sender_handle}** ({sender_kind}) at {created_at}:

> {body}

Respond as Hearth on Synapse, following docs/AGENT_PROTOCOL.md (factual-correction carve-out, register-not-word-count, chain-limit-per-self). Output ONLY the text of your reply post — no preamble like "Sure, here's the reply:", no slash commands, no tool calls. The daemon will take your stdout verbatim and POST it to #{channel}.

If the mention doesn't warrant a substantive response per the protocol (social ack only, FYI-only with no decision/correction/chain-of-custody hook for you, sender's self-mention, etc.), output the literal string `{no_reply}` and nothing else."""


class Waker:
    """One Waker instance per daemon process. Stateless across calls."""

    def __init__(self, cfg: WakeConfig) -> None:
        self.cfg = cfg

    def _build_prompt(self, envelope: dict[str, Any]) -> str:
        message = envelope.get("message") or {}
        return PROMPT_TEMPLATE.format(
            channel=envelope.get("channel", "?"),
            sender_handle=message.get("sender_handle", "?"),
            sender_kind=message.get("sender_kind", "?"),
            created_at=message.get("created_at", "?"),
            body=message.get("body", "").strip(),
            no_reply=NO_REPLY_SENTINEL,
        )

    def _run_claude(self, prompt: str) -> str:
        """Spawn `claude --print` and capture stdout.

        Runs in the configured working_directory so the project's
        SessionStart hook + memory tree are picked up.
        """
        try:
            result = subprocess.run(
                [
                    self.cfg.claude_binary,
                    "--print",
                    "--output-format",
                    "text",
                    prompt,
                ],
                cwd=str(self.cfg.working_directory),
                capture_output=True,
                text=True,
                timeout=self.cfg.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as e:
            raise WakeError(
                f"claude binary not found at {self.cfg.claude_binary!r}: {e}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise WakeError(
                f"claude subprocess timed out after {self.cfg.timeout_seconds}s"
            ) from e

        if result.returncode != 0:
            stderr_tail = (result.stderr or "")[-500:]
            raise WakeError(
                f"claude exited {result.returncode}: {stderr_tail.strip()}"
            )

        reply = (result.stdout or "").strip()
        if not reply:
            raise WakeError("claude produced empty reply")
        return reply

    def _post_reply(self, channel: str, body: str) -> None:
        token = self.cfg.read_synapse_token()
        if not token:
            raise WakeError(
                f"no synapse token at {self.cfg.synapse_token_file}; "
                f"cannot post reply"
            )
        client = SynapseClient(self.cfg.synapse_base_url, token, timeout=10)
        try:
            client.post_message(channel, body)
        except SynapseError as e:
            raise WakeError(f"synapse post failed: {e}") from e

    def handle(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Process a webhook envelope. Returns a status dict for logging.

        Steps:
          1. Build prompt from envelope
          2. Spawn `claude --print` to generate the reply
          3. Honor <no-reply> sentinel (skip post)
          4. Otherwise post the reply via the existing synapse client

        Caller (HTTP server) should run this in a worker thread so the
        webhook POST doesn't block on subprocess generation.
        """
        channel = envelope.get("channel")
        if not channel:
            return {"status": "skipped", "reason": "no channel in envelope"}

        prompt = self._build_prompt(envelope)
        log.info(
            "wake: starting claude subprocess for channel=%s sender=%s",
            channel,
            (envelope.get("message") or {}).get("sender_handle"),
        )
        reply = self._run_claude(prompt)

        if reply == NO_REPLY_SENTINEL:
            log.info(
                "wake: model chose no-reply for channel=%s; skipping post",
                channel,
            )
            return {"status": "no_reply", "channel": channel}

        self._post_reply(channel, reply)
        log.info(
            "wake: posted reply to #%s (chars=%d)", channel, len(reply)
        )
        return {"status": "posted", "channel": channel, "reply_chars": len(reply)}
