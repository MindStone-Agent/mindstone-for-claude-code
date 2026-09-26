"""Secret scrubbing for everything that enters the vector store.

Kept separate from embedder.py so every writer (vectorstore.upsert, the
transcript parser, the embedder itself) shares one pattern list regardless of
which embedder build an install uses. embedder.py re-exports `scrub` and
`SECRET_PATTERNS` for existing importers.

Order matters: specific shapes run before the generic ones so the placeholder
names the credential type. Every replacement is irreversible by design.
"""

from __future__ import annotations

import math
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

# key = value (also key: value and plist/Ruby-style "KEY" => "value",
# as `plutil -p` prints). The value is one whole token, dots and ~ allowed inside
# (aaa.bbb, sk.xyz~1) but not trailing (a sentence-final "." stays prose).
# (?=(?P<v>…))(?P=v) makes the match atomic without 3.11 atomic groups: the
# lookahead captures the longest run and can't be backtracked into, so
# `config.get_api_key_for_provider(name)` is skipped whole rather than
# shortened to "config". A value followed by "(" is a call, never redacted.
_KV = re.compile(
    r"(?i)(" + _KEY_TAIL + r")(\s*[\"']?\s*(?:=>|[:=])\s*[\"']?)(?!\[REDACTED)"
    r"(?=(?P<v>[A-Za-z0-9_\-/+=~]+(?:\.[A-Za-z0-9_\-/+=~]+)*))(?P=v)(?!\()"
)

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private keys: full blocks (bounded, and never across another BEGIN, which
    # keeps repeated headers linear), then truncated blocks: a header
    # followed by base64 runs of 20+ chars with no END line. Runs may be
    # separated by whitespace, JSON escapes (\n \r \t as literal backslash
    # pairs, e.g. a repr()'d tool result), or `cat -n` line-number prefixes
    # ("12\t", "12→") as tool output shows them. Prose after a header has no such runs and is left alone.
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----(?:(?!-----BEGIN )[\s\S]){0,8000}?-----END [A-Z ]+PRIVATE KEY-----"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----(?:(?:\\[nrt]|\s|\d{1,9}(?:\t|\\t|\u2192))*[A-Za-z0-9+/=]{20,})+"), "[REDACTED-PRIVATE-KEY]"),
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
    # Laravel Sanctum personal access tokens ("<id>|<40+ chars>"), e.g. Coolify's API.
    (re.compile(r"(?<![A-Za-z0-9|])\d{1,6}\|[A-Za-z0-9]{40,80}(?![A-Za-z0-9])"), "[REDACTED-API-TOKEN]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "[REDACTED-GITHUB-PAT]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])"), "[REDACTED-TELEGRAM-BOT-TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED-JWT]"),
    # Generic shapes last. Bearer values must contain a digit, so prose like
    # "Bearer authentication/authorization" survives.
    (re.compile(r"(?i)\b(Bearer)\s+(?=[A-Za-z0-9._~+/\-]{0,200}\d)[A-Za-z0-9._~+/\-]{20,}=*"), r"\1 [REDACTED-BEARER]"),
    (re.compile(_URLSAFE_43), "[REDACTED-URLSAFE-TOKEN]"),
]


# Credentials in URLs: scheme://user:SECRET@host (postgres, redis, amqp, https).
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,20}://[^\s:/@'\"]{1,64}:)([^\s@/'\"]{6,200})@")

# Bare secrets with no key nearby (e.g. an env value printed on its own line):
# a STANDALONE 20-100 char token (whitespace, quote, =, line start or a JSON
# escape like a literal \\n on the left; the same, or `,` `;`, on the right; no `/` or `.`
# inside, so URL and path segments never qualify) mixing upper, lower and
# digits, with Shannon entropy >= 4.2 bits/char. Hex digests, UUIDs, camelCase
# identifiers and model names never qualify. Measured on a live store: every
# hit that wasn't a known credential was an opaque id (cursor, run id).
_BARE = re.compile(
    r"(?:(?<=^)|(?<=[\s\"'=`])|(?<=\\[nrt]))[A-Za-z0-9+=_\-]{20,100}(?=$|[\s\"'`,;]|\\[nrt])", re.M
)
_BARE_MIN_ENTROPY = 4.2


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _bare_sub(m: re.Match[str]) -> str:
    v = m.group(0)
    if not (re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and re.search(r"\d", v)):
        return v
    if v.startswith("[REDACTED") or _entropy(v) < _BARE_MIN_ENTROPY:
        return v
    return "[REDACTED-HIGH-ENTROPY]"


def _kv_pass(text: str) -> str:
    """Redact key=value secrets, then every other exact occurrence of those
    values in the same text (a value quoted twice must not survive once)."""
    found: set[str] = set()

    def _sub(m: re.Match[str]) -> str:
        v = m.group("v")
        if len(v) < 16:
            return m.group(0)
        found.add(v)
        return m.group(1) + m.group(2) + "[REDACTED-SECRET]"

    text = _KV.sub(_sub, text)
    for v in sorted(found, key=len, reverse=True):
        text = text.replace(v, "[REDACTED-SECRET]")
    return text


def _one_pass(text: str) -> str:
    # Vendor/platform shapes first (they name the credential type), then
    # key=value (with value propagation), then the 43-char URL-safe rule.
    *named, urlsafe = SECRET_PATTERNS
    for pattern, placeholder in named:
        text = pattern.sub(placeholder, text)
    text = _URL_USERINFO.sub(r"\1[REDACTED-SECRET]@", text)
    text = _kv_pass(text)
    text = urlsafe[0].sub(urlsafe[1], text)
    return _BARE.sub(_bare_sub, text)


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
