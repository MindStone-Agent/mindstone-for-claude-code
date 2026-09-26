"""Secret scrubbing for everything that enters the vector store.

Defense in depth, not the primary control: the transcript indexer no longer
stores tool-result bodies at all (that is where pasted configs, plist dumps and
env files arrive), and the store runs with secure_delete. This module catches
what is left in conversation text.

Kept separate from embedder.py so every writer (vectorstore.upsert, the
transcript parser, the embedder itself) shares one implementation regardless of
which embedder build an install uses. Use `scrub()`; `SECRET_PATTERNS` is kept
for importers of the old name and holds only the vendor/platform shapes.

Pipeline (order matters; each stage names the credential where it can):
  1. vendor/platform shapes (SECRET_PATTERNS)
  2. context rules: URL userinfo, CLI flags, plist XML, hex in a secret
     context, AWS secret keys
  3. key=value, with every other occurrence of a redacted value replaced too
  4. 43-char URL-safe tokens (Synapse bearer tokens)
  5. bare high-entropy tokens
Every scan is bounded so no rule is quadratic on long transcript turns.
Every replacement is irreversible by design.
"""

from __future__ import annotations

import math
import re

# Left guard: not preceded by an alphanumeric. Unlike \b this still matches
# after "_" (KEY_sk-ant-...) and after punctuation.
_L = r"(?<![A-Za-z0-9])"
# Bounded "contains a digit" lookahead for runs of token characters.
_HAS_DIGIT = r"(?=[A-Za-z0-9_\-]{0,200}\d)"

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private keys: full blocks (bounded, never spanning another BEGIN, so
    # repeated headers stay linear), then truncated blocks: a header followed
    # by base64 runs of 20+ chars with no END line. Runs may be separated by
    # whitespace, JSON escapes (\n \r \t as literal backslash pairs), or
    # `cat -n` line-number prefixes ("12\t", "12→"). Prose after a header has
    # no such runs and is left alone.
    (re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:(?!-----BEGIN )[\s\S]){0,8000}?-----END [A-Z ]{0,40}PRIVATE KEY-----"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:(?:\\[nrt]|\s|\d{1,9}(?:\t|\\t|\u2192)){0,20}[A-Za-z0-9+/=]{20,4096}){1,200}"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"ssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{100,8192}"), "[REDACTED-SSH-KEY]"),
    # Vendor API keys.
    (re.compile(_L + r"sk-ant-[A-Za-z0-9_\-]{20,512}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(_L + r"sk-proj-[A-Za-z0-9_\-]{20,512}"), "[REDACTED-OPENAI-KEY]"),
    # Generic sk-/anthropic-/voyage-: must contain a digit, so prose like
    # "task-management-framework" or "sk-learn-…" is left alone.
    (re.compile(_L + r"sk-" + _HAS_DIGIT + r"[A-Za-z0-9_\-]{20,512}"), "[REDACTED-OPENAI-KEY]"),
    (re.compile(_L + r"anthropic-" + _HAS_DIGIT + r"[A-Za-z0-9_\-]{20,512}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(_L + r"voyage-" + _HAS_DIGIT + r"[A-Za-z0-9_\-]{20,512}"), "[REDACTED-VOYAGE-KEY]"),
    (re.compile(_L + r"hf_[A-Za-z0-9]{30,128}"), "[REDACTED-HF-TOKEN]"),
    (re.compile(_L + r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED-GOOGLE-API-KEY]"),
    (re.compile(_L + r"AKIA[0-9A-Z]{16}(?![0-9A-Z])"), "[REDACTED-AWS-ACCESS-KEY]"),
    # Platform tokens. Sanctum personal access tokens ("<id>|<40+ chars>",
    # e.g. Coolify's API) are mixed-case base62; a "3|<sha40>" from git log is
    # lowercase hex and is left alone.
    (re.compile(r"(?<![A-Za-z0-9|])\d{1,6}\|(?=[A-Za-z0-9]{0,80}[A-Z])[A-Za-z0-9]{40,80}(?![A-Za-z0-9])"), "[REDACTED-API-TOKEN]"),
    (re.compile(_L + r"gh[pousr]_[A-Za-z0-9]{30,255}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(_L + r"github_pat_[A-Za-z0-9_]{30,255}"), "[REDACTED-GITHUB-PAT]"),
    (re.compile(_L + r"xox[abprs]-[A-Za-z0-9\-]{10,255}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"(?<![0-9])\d{7,11}:[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])"), "[REDACTED-TELEGRAM-BOT-TOKEN]"),
    (re.compile(_L + r"eyJ[A-Za-z0-9_\-]{10,4096}\.eyJ[A-Za-z0-9_\-]{10,4096}\.[A-Za-z0-9_\-]{10,4096}"), "[REDACTED-JWT]"),
    # Bearer values must contain a digit, so prose like "Bearer
    # authentication/authorization" survives.
    (re.compile(r"(?i)\b(Bearer)[ \t]{1,8}(?=[A-Za-z0-9._~+/\-]{0,200}\d)[A-Za-z0-9._~+/\-]{20,4096}=*"), r"\1 [REDACTED-BEARER]"),
]

# --- 2. context rules -------------------------------------------------------

# scheme://[user]:SECRET@host (postgres, redis with an empty user, amqp, https).
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,20}://[^\s:/@'\"]{0,64}:)([^\s@/'\"]{4,200})@")
# CLI flags: --password X, --password=X, --token X, --api-key X, and curl -u user:X.
_CLI_FLAG = re.compile(
    r"(?i)(--(?:password|passwd|pass|token|api[-_]key|secret|auth[-_]token|access[-_]token|client[-_]secret|aws[-_]secret[-_]access[-_]key|secret[-_]access[-_]key)(?:=|[ \t]{1,8}))"
    r"([\"']?)([^\s\"']{4,512})"
)
# mysql/mariadb/psql style -pSECRET (no space) after a db client name.
_MYSQL_P = re.compile(r"(?i)(\b(?:mysql|mysqldump|mariadb|mysqladmin)\b[^\n]{0,120}?[ \t]-p)([^\s'\"-][^\s'\"]{3,256})")
_CURL_USER = re.compile(r"(?<!\S)(-u[ \t]{1,8}[^\s:'\"]{1,64}:)([^\s'\"]{3,512})")
# Raw plist XML: <key>…TOKEN|SECRET|KEY|PASS…</key><string>value</string>.
_PLIST_XML = re.compile(
    r"(?i)(<key>[^<]{0,80}(?:token|secret|key|pass(?:word)?|credential)[^<]{0,40}</key>\s{0,40}<string>)([^<]{4,1024})(</string>)"
)
# Hex tokens right after a secret word (token: <hex>, --token <hex>, a
# markdown code span, Cookie: session=<hex>, X-Gateway-Key: <hex>, escaped
# JSON). Whole words only, so Author:, authentication, session_id: and
# tokenizer_sha256 are not contexts; git SHAs and digests stay.
_HEX_IN_CONTEXT = re.compile(
    r"(?i)(\b(?:token|secret|cookie|api[-_]?key|password|passwd|bearer|auth[-_]?token|access[-_]?token|[a-z]*[-_](?:token|key|secret)|session(?==))\b[\\\"'`]{0,3}[ \t]{0,8}(?:=>|[:=])?[ \t]{0,8}[\\\"'`]{0,3})"
    r"([0-9a-f]{32,128})(?![0-9a-z])"
)
# A line that is nothing but 32-128 hex characters (e.g. a token printed by
# `cat`), except exactly 40 (a bare git SHA-1 is common and not a secret).
_BARE_HEX_LINE = re.compile(r"(?im)^([ \t]{0,8})(?!(?:[0-9a-f]{40})[ \t]*$)([0-9a-f]{32,128})([ \t]{0,8})$")
# AWS secret access keys (40 base64 chars) next to their key names.
_AWS_SECRET = re.compile(
    r"(?i)((?:aws_secret_access_key|secret_access_key|secretaccesskey)[\"']?[ \t]{0,8}(?:=|:)[ \t]{0,8}[\"']?)([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"
)

# --- 3. key = value ---------------------------------------------------------

# Keys: bare or env-var style (HF_TOKEN, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN,
# client_secret). The rule starts at the key's final word (literal-led, so the
# scan is linear), and the separator must follow it immediately, so
# "max_tokens:" never matches. Only the value is replaced.
# "pass" alone only as an env-style suffix (DB_PASS, PG-PASS), never "bypass".
_PASSWORD_TAIL = r"passw(?:or)?d|pwd|passphrase|(?<=[_\-])pass"
_KEY_TAIL = r"(?:api|secret)[_\-]?key|token|secret|" + _PASSWORD_TAIL
# The separator allows plist/Ruby `=>`, JSON quotes (also backslash-escaped,
# as in a JSON string inside a transcript) and markdown code spans.
_SEP = r"([ \t]{0,8}\\?[\"'`]?[ \t]{0,8}(?:=>|[:=])[ \t]{0,8}\\?[\"'`]?)"
# (?=(?P<v>…))(?P=v) makes the value match atomic without 3.11 atomic groups:
# the lookahead captures the longest run and can't be backtracked into, so
# `config.get_api_key_for_provider(name)` is judged whole (a call) rather than
# shortened to "config".
_KV = re.compile(
    r"(?i)(" + _KEY_TAIL + r")" + _SEP + r"(?!\[REDACTED)"
    r"(?=(?P<v>[A-Za-z0-9_\-/+=~]{1,512}(?:\.[A-Za-z0-9_\-/+=~]{1,512}){0,16}))(?P=v)(?P<call>\()?"
)
# Password values: any run of 6+ non-whitespace, non-quote characters,
# punctuation included (passwords are short and full of symbols). A literal
# backslash ends the value, so a JSON-escaped \\n doesn't swallow the next line.
_KV_PASSWORD = re.compile(
    r"(?i)((?:" + _PASSWORD_TAIL + r"))" + _SEP + r"(?!\[REDACTED)"
    r"(?=(?P<v>[^\s\"'`<>\\]{6,256}))(?P=v)"
)
# Plain words and type names: all letters (optionally one Word[...] generic
# or a | union), or a GraphQL non-null scalar/type name (String!, UserInput!).
# Skipped ONLY after a colon (a declaration: `password: string`); after `=`
# (an assignment: PGPASSWORD=abcdef) the value is always a secret.
# What an unquoted declaration may name: a lowercase word, a strict PascalCase
# name (String, Optional[str], UserProfile), a known type, or a GraphQL
# non-null. Random mixed-case letters are not strict PascalCase.
_TYPE_DECL = re.compile(
    r"(?:[a-z_]+(?:\[[A-Za-z_, .|\[\]]*\])?(?:[ ]?\|[ ]?[A-Za-z_]+)*"
    r"|(?:[A-Z][a-z]+)+(?:\[[A-Za-z_, .|\[\]]*\])?(?:[ ]?\|[ ]?[A-Za-z_]+)*"
    r"|(?:String|Int|Float|Boolean|ID|[A-Z][A-Za-z]*(?:Input|Type|Enum))!)[;,]?"
)
_TYPE_OR_WORD = re.compile(
    r"(?:[A-Za-z_]+(?:\[[A-Za-z_, .|\[\]]*\])?(?:\|[A-Za-z_]+)*"
    r"|(?:String|Int|Float|Boolean|ID|[A-Z][A-Za-z]*(?:Input|Type|Enum))!)[;,]?"
)
# A quoted password may contain spaces: PASSWORD: 'p@ss w0rd'.
_KV_PASSWORD_QUOTED = re.compile(
    r"(?i)((?:" + _PASSWORD_TAIL + r")[ \t]{0,8}\\?[\"']?[ \t]{0,8}(?:=>|[:=])[ \t]{0,8})([\"'])([^\"'\n]{6,256})\2"
)
_DOTTED_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
# A filesystem path (/…, ~/…, ./…, ../…) is where a secret lives, not a secret.
_PATH = re.compile(r"(?:~|\.{1,2})?/[\w.\-/~]*")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# --- 4/5. token-shaped runs -------------------------------------------------

# Synapse bearer tokens are secrets.token_urlsafe(32): exactly 43 URL-safe
# characters. Match only a standalone 43-char run that mixes upper, lower and
# digits, so hex digests and UUIDs are untouched.
_URLSAFE_43 = re.compile(
    r"(?<![A-Za-z0-9_\-])(?=[A-Za-z0-9_\-]{0,42}[A-Z])(?=[A-Za-z0-9_\-]{0,42}[a-z])(?=[A-Za-z0-9_\-]{0,42}[0-9])[A-Za-z0-9_\-]{43}(?![A-Za-z0-9_\-])"
)
# Bare secrets with no key nearby: a STANDALONE 20-100 char token (bounded by
# whitespace, quotes, =, line ends or literal \n escapes; no `/` or `.` inside,
# so URL and path segments never qualify) that mixes upper, lower and digits,
# has high entropy, and switches character class often. Identifiers
# (getTotalPendingReplies2, snake_case_v2_names) switch class rarely.
_BARE = re.compile(
    r"(?:(?<=^)|(?<=[\s\"'=`])|(?<=\\[nrt]))[A-Za-z0-9+=_\-]{20,100}(?=$|[\s\"'`,;]|\\[nrt])", re.M
)
_BARE_MIN_ENTROPY = 4.2
_BARE_MIN_SWITCH_RATE = 0.45


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _class(ch: str) -> str:
    return "u" if ch.isupper() else "l" if ch.islower() else "d" if ch.isdigit() else "p"


def _switch_rate(s: str) -> float:
    return sum(1 for a, b in zip(s, s[1:]) if _class(a) != _class(b)) / max(1, len(s) - 1)


def _bare_sub(m: re.Match[str]) -> str:
    v = m.group(0)
    if v.startswith("[REDACTED"):
        return v
    if sum(ch.isdigit() for ch in v) < 2 or not (re.search(r"[A-Z]", v) and re.search(r"[a-z]", v)):
        return v
    if _entropy(v) < _BARE_MIN_ENTROPY or _switch_rate(v) < _BARE_MIN_SWITCH_RATE:
        return v
    return "[REDACTED-HIGH-ENTROPY]"


def _looks_like_code(v: str, call: bool) -> bool:
    """A value that is a call, a filesystem path, or a dotted member access
    with no digits (settings.OPENAI_API_KEY, user.password_hash) is code or a
    location, not a secret."""
    if call or _PATH.fullmatch(v):
        return True
    return bool(_DOTTED_IDENT.fullmatch(v)) and not any(ch.isdigit() for ch in v)


def _kv_pass(text: str, sink: set[str] | None = None) -> str:
    """Redact key=value secrets, then every other exact occurrence of those
    values in the same text (a value quoted twice must not survive once)."""
    found: set[str] = set()

    def _pw(m: re.Match[str]) -> str:
        v = m.group("v").rstrip(".,;)")
        sep = m.group(2)
        # A type/schema word is only plausible in an unquoted declaration
        # (`password: string`) under a lower-case key. Quoted values ("…": "…"),
        # assignments (=, =>) and upper-case env keys (POSTGRES_PASSWORD:) are
        # always values.
        declaration = (
            ":" in sep and "=" not in sep and not re.search(r"[\"'`]", sep) and not m.group(1).isupper()
        )
        if "(" in v or _looks_like_code(v, False) or (declaration and _TYPE_DECL.fullmatch(v)):
            # password = user.password_hash / get_pw() / a type annotation or
            # schema keyword (string, Optional[str], String!, required).
            return m.group(0)
        found.add(v)
        return m.group(1) + m.group(2) + "[REDACTED-SECRET]" + m.group("v")[len(v):]

    def _sub(m: re.Match[str]) -> str:
        v = m.group("v")
        if len(v) < 16 or _looks_like_code(v, m.group("call") is not None):
            return m.group(0)
        found.add(v)
        return m.group(1) + m.group(2) + "[REDACTED-SECRET]" + (m.group("call") or "")

    def _pwq(m: re.Match[str]) -> str:
        v = m.group(3)
        if " " not in v or _TYPE_OR_WORD.fullmatch(v) or not _propagatable(v.replace(" ", "")):
            return m.group(0)  # no space: the unquoted rule handles it
        found.add(v)
        return m.group(1) + m.group(2) + "[REDACTED-SECRET]" + m.group(2)

    text = _KV_PASSWORD_QUOTED.sub(_pwq, text)
    text = _KV_PASSWORD.sub(_pw, text)
    text = _KV.sub(_sub, text)
    if sink is not None:
        sink |= {v for v in found if len(v) >= 6 and "[REDACTED" not in v}
    # Propagate only secret-shaped values, and only as whole tokens, so a
    # redaction can never eat part of an ordinary word.
    for v in sorted(found, key=len, reverse=True):
        if _propagatable(v):
            text = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(v) + r"(?![A-Za-z0-9_])", "[REDACTED-SECRET]", text)
    return text


def _propagatable(v: str) -> bool:
    """Every redacted value of 6+ chars is removed elsewhere in the chunk too
    (at token boundaries), unless it is a plain lowercase word that could be
    ordinary text."""
    if len(v) < 6:
        return False
    return not re.fullmatch(r"[a-z]+", v)


def _capture(sink: set[str] | None, group: int, template: str):
    """A re.sub replacement that also records the secret group's value."""

    def repl(m: re.Match[str]) -> str:
        # A later pass can match an earlier pass's own placeholder; that is
        # not a secret value.
        if sink is not None and len(m.group(group)) >= 6 and "[REDACTED" not in m.group(group):
            sink.add(m.group(group))
        return m.expand(template)

    return repl


def _one_pass(text: str, sink: set[str] | None = None) -> str:
    for pattern, placeholder in SECRET_PATTERNS:
        text = pattern.sub(placeholder, text)
    text = _URL_USERINFO.sub(_capture(sink, 2, r"\1[REDACTED-SECRET]@"), text)
    text = _CLI_FLAG.sub(_capture(sink, 3, r"\1\2[REDACTED-SECRET]"), text)
    text = _CURL_USER.sub(_capture(sink, 2, r"\1[REDACTED-SECRET]"), text)
    text = _MYSQL_P.sub(_capture(sink, 2, r"\1[REDACTED-SECRET]"), text)
    text = _PLIST_XML.sub(_capture(sink, 2, r"\1[REDACTED-SECRET]\3"), text)
    text = _AWS_SECRET.sub(_capture(sink, 2, r"\1[REDACTED-AWS-SECRET-KEY]"), text)
    text = _HEX_IN_CONTEXT.sub(r"\1[REDACTED-HEX-TOKEN]", text)
    text = _BARE_HEX_LINE.sub(r"\1[REDACTED-HEX-TOKEN]\3", text)
    text = _kv_pass(text, sink)
    text = _URLSAFE_43.sub("[REDACTED-URLSAFE-TOKEN]", text)
    return _BARE.sub(_bare_sub, text)


def scrub_collect(text: str) -> tuple[str, set[str]]:
    """scrub(), plus the values that were redacted because of their CONTEXT
    (a password/token key, URL userinfo, a CLI flag, a plist key, an AWS
    secret key). Those are known secrets even where they later appear without
    that context, so a store-wide rescrub hunts them in every row."""
    sink: set[str] = set()
    if not isinstance(text, str):
        return text, sink
    for _ in range(3):
        new = _one_pass(text, sink)
        if new == text:
            break
        text = new
    return text, sink


def scrub(text: str) -> str:
    """Replace secret-shaped tokens in text with placeholders. Irreversible.

    Runs to a fixed point (at most 3 passes): a later rule can rewrite a
    neighbour that blocked an earlier rule's boundary.
    """
    if not isinstance(text, str):
        return text
    for _ in range(3):
        new = _one_pass(text)
        if new == text:
            break
        text = new
    return text
