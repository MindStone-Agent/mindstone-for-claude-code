"""Secret scrubbing for everything that enters the vector store.

Kept separate from embedder.py so every writer (vectorstore.upsert, the
transcript parser, the embedder itself) shares one pattern list regardless of
which embedder build an install uses. embedder.py re-exports `scrub` and
`SECRET_PATTERNS` for existing importers.

Order matters: specific shapes run before the generic ones so the placeholder
names the credential type. Every replacement is irreversible by design.
"""

from __future__ import annotations

import re

# Synapse bearer tokens are secrets.token_urlsafe(32): exactly 43 URL-safe
# characters with no prefix. Match only a standalone 43-char run that mixes
# upper, lower and digits, so hex digests (40/64 lowercase hex) and UUIDs
# (hyphenated, 36) are untouched.
_URLSAFE_43 = r"(?<![A-Za-z0-9_\-])(?=[A-Za-z0-9_\-]{0,42}[A-Z])(?=[A-Za-z0-9_\-]{0,42}[a-z])(?=[A-Za-z0-9_\-]{0,42}[0-9])[A-Za-z0-9_\-]{43}(?![A-Za-z0-9_\-])"

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private keys: full blocks, then truncated blocks (a header followed by a
    # base64 body with no END line, common in cut-off transcript lines).
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[A-Za-z0-9+/=\s\\]{40,}"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"ssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{100,}"), "[REDACTED-SSH-KEY]"),
    # Vendor API keys.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}"), "[REDACTED-OPENAI-KEY]"),
    # Generic sk-: anchored and must contain a digit, so prose like
    # "task-management-framework" or "sk-learn-…" is left alone.
    (re.compile(r"\bsk-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"), "[REDACTED-OPENAI-KEY]"),
    (re.compile(r"\banthropic-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"\bvoyage-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"), "[REDACTED-VOYAGE-KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"), "[REDACTED-GOOGLE-API-KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-ACCESS-KEY]"),
    # Platform tokens.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "[REDACTED-GITHUB-PAT]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])"), "[REDACTED-TELEGRAM-BOT-TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED-JWT]"),
    # Generic shapes last.
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/\-]{20,}=*"), r"\1 [REDACTED-BEARER]"),
    (
        re.compile(
            r"(?i)\b(api[_\-]?key|access[_\-]?token|auth[_\-]?token|client[_\-]?secret|secret[_\-]?key|password|passwd)"
            r"(\s*[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED)[A-Za-z0-9_\-/+=.]{16,}"
        ),
        r"\1\2[REDACTED-SECRET]",
    ),
    (re.compile(_URLSAFE_43), "[REDACTED-URLSAFE-TOKEN]"),
]


def scrub(text: str) -> str:
    """Replace secret-shaped tokens in text with placeholders. Irreversible."""
    if not isinstance(text, str):
        return text
    for pattern, placeholder in SECRET_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text
