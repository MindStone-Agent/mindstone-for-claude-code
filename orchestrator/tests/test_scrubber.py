"""Tests for orchestrator/hooks/scrubber.py (MS4CC#117).

Every credential below is synthetic and assembled at runtime, so no literal
secret-shaped string lives in the repo (and push protection stays quiet).
"""

from __future__ import annotations

import random
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from scrubber import scrub  # noqa: E402

_rng = random.Random(117)
_URLSAFE = string.ascii_letters + string.digits + "_-"


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(_rng.choice(alphabet) for _ in range(n))


def _mixed(n: int) -> str:
    # Guarantee upper, lower and digit so shape-gated patterns apply.
    return "A" + "a" + "7" + _rand(n - 3, _URLSAFE)


POSITIVE = {
    "anthropic": ("sk-" + "ant-" + "api03-" + _rand(90, _URLSAFE), "[REDACTED-ANTHROPIC-KEY]"),
    "openai-proj": ("sk-" + "proj-" + _rand(150, _URLSAFE), "[REDACTED-OPENAI-KEY]"),
    "openai": ("sk-" + _rand(48), "[REDACTED-OPENAI-KEY]"),
    "google": ("AI" + "za" + _rand(35, _URLSAFE), "[REDACTED-GOOGLE-API-KEY]"),
    "aws": ("AK" + "IA" + _rand(16, string.ascii_uppercase + string.digits), "[REDACTED-AWS-ACCESS-KEY]"),
    "github-ghp": ("gh" + "p_" + _rand(36), "[REDACTED-GITHUB-TOKEN]"),
    "github-gho": ("gh" + "o_" + _rand(36), "[REDACTED-GITHUB-TOKEN]"),
    "github-pat": ("github" + "_pat_" + _rand(60, string.ascii_letters + string.digits + "_"), "[REDACTED-GITHUB-PAT]"),
    "slack-xoxp": ("xo" + "xp-" + _rand(40, string.ascii_letters + string.digits + "-"), "[REDACTED-SLACK-TOKEN]"),
    "telegram": (str(_rng.randint(10**8, 10**9)) + ":" + "AA" + _rand(33, _URLSAFE), "[REDACTED-TELEGRAM-BOT-TOKEN]"),
    "jwt": ("ey" + "J" + _rand(20, _URLSAFE) + ".ey" + "J" + _rand(40, _URLSAFE) + "." + _rand(43, _URLSAFE), "[REDACTED-JWT]"),
    "synapse-43": (_mixed(43), "[REDACTED-URLSAFE-TOKEN]"),
}


@pytest.mark.parametrize("name", sorted(POSITIVE))
def test_positive_shapes_are_redacted(name: str) -> None:
    secret, placeholder = POSITIVE[name]
    out = scrub(f"config line: {secret} (end)")
    assert secret not in out
    assert placeholder in out


def test_bearer_keeps_scheme_word() -> None:
    tok = _rand(40, _URLSAFE)
    out = scrub(f"Authorization: Bearer {tok}")
    assert tok not in out
    assert "Bearer [REDACTED-BEARER]" in out


@pytest.mark.parametrize("key", ["api_key", "API-KEY", "access_token", "client_secret", "password"])
def test_generic_assignment_keeps_name_redacts_value(key: str) -> None:
    val = _rand(24)
    out = scrub(f'{key} = "{val}"')
    assert val not in out
    assert key in out
    assert "[REDACTED-SECRET]" in out


def test_private_key_full_and_truncated() -> None:
    body = "\\n".join(_rand(64, string.ascii_letters + string.digits + "+/") for _ in range(4))
    header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
    full = f"{header}\\n{body}\\n-----END OPENSSH " + "PRIVATE KEY-----"
    assert scrub(full) == "[REDACTED-PRIVATE-KEY]"
    truncated = f"{header}\\n{body}"
    assert body[:64] not in scrub(truncated)


NEGATIVE = [
    "git sha " + "0123456789abcdef" * 2 + "01234567",  # 40 lowercase hex
    "sha256 " + "0123456789abcdef" * 4,  # 64 lowercase hex
    "uuid 3ad7c136-9284-44c2-96c1-e03e93228762",
    "the task-management-framework-overview is here",  # prose with hyphens
    "risk-assessment and desk-research notes",
    "use sk-learn-for-the-classifier-pipeline here",  # anchored sk- prose, no digit
    "the anthropic-sdk-python-client-library docs",
    "timestamp 2026-09-26T19:54:56.994Z and port 11434",
    "a lowercase_only_identifier_that_is_quite_long_ok",  # no upper/digit mix
    "password: [REDACTED-SECRET]",  # already scrubbed: idempotent
    "call ssh -i ~/.ssh/id_ed25519 host",  # a key PATH is not key material
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_text_is_untouched(text: str) -> None:
    assert scrub(text) == text


def test_idempotent() -> None:
    blob = " ".join(s for s, _ in POSITIVE.values())
    once = scrub(blob)
    assert scrub(once) == once


def test_non_string_passthrough() -> None:
    assert scrub(None) is None  # type: ignore[arg-type]
