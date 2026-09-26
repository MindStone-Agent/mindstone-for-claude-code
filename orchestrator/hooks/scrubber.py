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

# Lookaheads that scan a run are bounded ({0,200}) so a long hyphenated or
# base64 run can't make a rule quadratic (transcript turns can be very long).
_HAS_DIGIT = r"(?=[A-Za-z0-9_\-]{0,200}\d)"

# Assignment keys: bare or env-var style (HF_TOKEN, OPENAI_API_KEY,
# TELEGRAM_BOT_TOKEN, SYNAPSE_TOKEN, client_secret). The rule starts at the
# key's final word (literal-led, so the scan is linear) and the separator must
# follow it immediately, so "max_tokens:" never matches. Only the value is
# replaced; whatever precedes the keyword in the name is left as written.
_KEY_TAIL = r"(?:api|secret)[_\-]?key|token|secret|passw(?:or)?d"

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private keys: full blocks (bounded, and never across another BEGIN, which
    # keeps repeated headers linear), then truncated blocks: a header
    # followed by base64 runs of 20+ chars with no END line. Runs may be
    # separated by whitespace, JSONL "\n" escapes, or `cat -n` line-number
    # prefixes ("12\t", "12→") as tool output shows them. Prose after a header has no such runs and is left alone.
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----(?:(?!-----BEGIN )[\s\S]){0,8000}?-----END [A-Z ]+PRIVATE KEY-----"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----(?:(?:\\n|\s|\d{1,6}(?:\t|\u2192))*[A-Za-z0-9+/=]{20,})+"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"ssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{100,}"), "[REDACTED-SSH-KEY]"),
    # Vendor API keys.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}"), "[REDACTED-OPENAI-KEY]"),
    # Generic sk-/anthropic-/voyage-: word-anchored and must contain a digit,
    # so prose like "task-management-framework" or "sk-learn-…" is left alone.
    (re.compile(r"\bsk-" + _HAS_DIGIT + r"[A-Za-z0-9_\-]{20,}"), "[REDACTED-OPENAI-KEY]"),
    (re.compile(r"\banthropic-" + _HAS_DIGIT + r"[A-Za-z0-9_\-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"\bvoyage-" + _HAS_DIGIT + r"[A-Za-z0-9_\-]{20,}"), "[REDACTED-VOYAGE-KEY]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "[REDACTED-HF-TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), "[REDACTED-GOOGLE-API-KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-ACCESS-KEY]"),
    # Platform tokens.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "[REDACTED-GITHUB-PAT]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])"), "[REDACTED-TELEGRAM-BOT-TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED-JWT]"),
    # Generic shapes last. Bearer values must contain a digit, so prose like
    # "Bearer authentication/authorization" survives.
    (re.compile(r"(?i)\b(Bearer)\s+(?=[A-Za-z0-9._~+/\-]{0,200}\d)[A-Za-z0-9._~+/\-]{20,}=*"), r"\1 [REDACTED-BEARER]"),
    # key = value. The value must be one whole token (no shorter backtrack:
    # the lookahead rejects a following token char) and must not be a call
    # or attribute chain, so `password = get_password_from_env()` survives.
    (
        re.compile(
            r"(?i)(" + _KEY_TAIL + r")(\s*[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED)"
            r"[A-Za-z0-9_\-/+=]{16,}(?![A-Za-z0-9_\-/+=(.])"
        ),
        r"\1\2[REDACTED-SECRET]",
    ),
    (re.compile(_URLSAFE_43), "[REDACTED-URLSAFE-TOKEN]"),
]


def _one_pass(text: str) -> str:
    for pattern, placeholder in SECRET_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def scrub(text: str) -> str:
    """Replace secret-shaped tokens in text with placeholders. Irreversible.

    Runs to a fixed point (at most 3 passes): a later rule can rewrite a
    neighbour that blocked an earlier rule's boundary, so one pass is not
    always idempotent.
    """
    if not isinstance(text, str):
        return text
    for _ in range(3):
        new = _one_pass(text)
        if new == text:
            break
        text = new
    return text
