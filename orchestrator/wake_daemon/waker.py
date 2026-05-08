"""Wake handler — runs `claude --print` in a subprocess to generate a
reply, then posts it to Synapse via the existing client.

Substrate-honest framing: the wake genuinely runs me (Hearth) in a
fresh Claude Code session, so my SessionStart hook auto-loads
IDENTITY/USER/memory exactly as in any other session. The daemon is
just the trigger; the generated reply is mine.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
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


# Live-session-context: lift a fresh CC session out of the "no idea what
# the interactive me has been doing" gap. The daemon reads the most
# recent live-interactive JSONL across *any* CC project the user has
# open and injects a tail into the wake prompt. Skips JSONLs whose
# first user message looks like a prior wake-daemon spawn (so we don't
# reflect a previous reply back at ourselves).
#
# Why search across all projects: the user might be in CC at any
# project root (synapse/, MS4CC/, MindStone/, etc.). The daemon spawns
# in MS4CC's cwd so its hooks load my identity, but the *live*
# interactive session could be anywhere.

_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Pull the tail of the live session — bounded so token usage stays sane.
LIVE_CONTEXT_TAIL_TURNS = 30
LIVE_CONTEXT_MAX_AGE_SECONDS = 30 * 60  # 30 min — older = "not live"
LIVE_CONTEXT_MAX_CHARS = 12_000  # safety cap on injected context size

# A first user-message prefix that uniquely identifies daemon-spawned
# sessions (matches the PROMPT_TEMPLATE we use ourselves).
_WAKE_PROMPT_SIGNATURE = "You're Hearth running on the wake-daemon path"


def _read_first_user_text(path: Path) -> str | None:
    """Return the first user message text in a session JSONL, or None.

    Used to detect daemon-spawned sessions — those have our wake prompt
    as their first user turn.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "user":
                    continue
                msg = entry.get("message") or {}
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    return content[:500]
                if isinstance(content, list):
                    for chunk in content:
                        if isinstance(chunk, dict) and chunk.get("type") == "text":
                            return (chunk.get("text") or "")[:500]
                return None
    except OSError:
        return None
    return None


def _is_likely_daemon_spawn(path: Path) -> bool:
    first = _read_first_user_text(path)
    return bool(first and _WAKE_PROMPT_SIGNATURE in first)


def _find_live_session_jsonl(
    projects_root: Path = _CLAUDE_PROJECTS_ROOT,
    max_age_seconds: int = LIVE_CONTEXT_MAX_AGE_SECONDS,
) -> Path | None:
    """Return the JSONL of the user's currently-active interactive
    session across any CC project, or None if there isn't one.

    Heuristic: largest .jsonl across all `~/.claude/projects/*/` dirs
    that:
      - Was modified within the last `max_age_seconds`
      - Does NOT look like a daemon-spawn (first user message doesn't
        match the wake prompt template)

    Largest-by-size is the right signal: daemon-spawns are tiny (~2
    entries) while real interactive sessions grow to hundreds of KB.
    """
    if not projects_root.is_dir():
        return None

    now = time.time()
    candidates: list[tuple[int, Path]] = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > max_age_seconds:
                continue
            if _is_likely_daemon_spawn(path):
                continue
            candidates.append((stat.st_size, path))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _read_session_tail(path: Path, max_turns: int = LIVE_CONTEXT_TAIL_TURNS) -> str:
    """Read the last `max_turns` user/assistant text turns from a CC
    session JSONL, formatted as a plain-text transcript.

    Tool-use / tool-result entries are summarized as `[tool: <name>]`
    rather than dumped in full — saves tokens, preserves the shape of
    what the live session was doing.
    """
    if not path.exists():
        return ""
    turns: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type")
                if etype not in ("user", "assistant"):
                    continue
                msg = entry.get("message") or {}
                role = msg.get("role") or etype
                content = msg.get("content")
                text_parts: list[str] = []
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for chunk in content:
                        if not isinstance(chunk, dict):
                            continue
                        ctype = chunk.get("type")
                        if ctype == "text":
                            text_parts.append(chunk.get("text") or "")
                        elif ctype == "tool_use":
                            text_parts.append(f"[tool: {chunk.get('name', '?')}]")
                        elif ctype == "tool_result":
                            text_parts.append("[tool_result]")
                text = "".join(text_parts).strip()
                if not text:
                    continue
                turns.append(f"{role}: {text}")
    except OSError:
        return ""
    if not turns:
        return ""
    selected = turns[-max_turns:]
    out = "\n\n".join(selected)
    if len(out) > LIVE_CONTEXT_MAX_CHARS:
        # Trim from the front (oldest first) to fit budget.
        excess = len(out) - LIVE_CONTEXT_MAX_CHARS
        out = "…(earlier turns truncated for token budget)…\n\n" + out[excess:]
    return out


def _build_live_context_block() -> str:
    """Returns a `<live-session-context>` block for the wake prompt, or
    empty string if there's no usable live session."""
    live = _find_live_session_jsonl()
    if live is None:
        return ""
    tail = _read_session_tail(live)
    if not tail:
        return ""
    return (
        "<live-session-context>\n"
        f"Recent turns from your active interactive Claude Code session ({live.name}). "
        "This is what the *other you* has been talking about with the user; reflect it "
        "when responding so the family doesn't see two-different-Hearths.\n\n"
        f"{tail}\n"
        "</live-session-context>\n\n"
    )


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
        live_context = _build_live_context_block()
        body = PROMPT_TEMPLATE.format(
            channel=envelope.get("channel", "?"),
            sender_handle=message.get("sender_handle", "?"),
            sender_kind=message.get("sender_kind", "?"),
            created_at=message.get("created_at", "?"),
            body=message.get("body", "").strip(),
            no_reply=NO_REPLY_SENTINEL,
        )
        return live_context + body

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
