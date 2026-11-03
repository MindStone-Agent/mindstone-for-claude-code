"""Redaction for activity previews (synapse#31, activity layer): commands, file paths, tool
output, the agent's replies, subagent descriptions.

Two passes, in this order, both bounded:
1. The server's redaction, verbatim: fleet/fleet_redact_vendor.py is synapse's
   api/fleet_redact.py (pinned by sha256 in the tests) with only its scrubber import changed.
   It runs hooks/scrubber.py on 1 KB chunks with a re-scan around each cut, then the server's
   extra rules and its private-key, Kubernetes Secret and long-base64 walks.
2. `redact()`: the reporter's own additions (more CLI password shapes, `NAME=value` and
   `"name": value` for secret-named variables, short Stripe keys, Slack workflow webhooks,
   base64 of a PEM header).
Both sides pass the shared fixtures in synapse's tests/fixtures/fleet_secrets.json. These are
heuristics: a secret in a shape none of them knows still gets through.

`preview()` cuts long text to a head and a tail. Scrubbing happens before the cut, and a word
split by any cut is dropped, so no fragment of a secret survives at an edge. Text over
2 * SCAN_WINDOW is first cut to its first and last SCAN_WINDOW (dropping the split words), which
are then scrubbed: only those can reach the preview.
If the scrubber can't be loaded, `preview()` returns None: nothing is sent unscrubbed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]
SCAN_WINDOW = 128 * 1024  # the most text scrubbed per end; beyond it, only head and tail matter

_R = "[REDACTED-SECRET]"

# --- Reporter-side additions: shapes the server's list doesn't name ----------------------------
# A name whose parts include one of these holds a secret: `_`, `.` and `-` separated parts
# anywhere (API_KEY, tls.key, db-password), or the last part of a camelCase name (AccountKey).
_SECRET_PARTS = frozenset({"SECRET", "SECRETS", "TOKEN", "TOKENS", "KEY", "KEYS", "APIKEY", "PASS", "PASSWD",
                           "PASSWORD", "PASSPHRASE", "PWD", "CREDENTIAL", "CREDENTIALS", "DOCKERCONFIGJSON"})
_TYPE_NAMES = frozenset({"string", "str", "int", "number", "bool", "boolean", "null", "none", "any", "unknown",
                         "undefined", "optional", "true", "false"})
_VALUE = r"(\"[^\"\r\n]{0,4096}\"|'[^'\r\n]{0,4096}'|[^\s\"']{1,4096})"
# Names are matched in one bounded run, never split by backtracking (a long underscore-joined
# word with no `=` once took quadratic time here), and checked in Python.
_ENV_ASSIGN = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]{1,128})([ \t]*=[ \t]*)(?![ \t]*\[REDACTED)" + _VALUE)
# "key": value, JSON style (a quoted key, anywhere on a line).
_KV_QUOTED = re.compile(r"([\"'])([A-Za-z0-9_.-]{1,128})\1([ \t]*:[ \t]*)(?![ \t]*\[REDACTED)"
                        r"(\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}'|[^\s\"',}\]\[{][^\s\"',}\]]{0,4095})")
_HEX_KEY = re.compile(r"[0-9a-fA-F]{32,}")
_CAMEL = re.compile(r"[a-z0-9]+|[A-Z][a-z0-9]*")

_MORE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?im)^([ \t]*api-?key[ \t]*:[ \t]*)\S[^\r\n]{0,4096}"), r"\1" + _R),
    (re.compile(r"(?i)(?<!\S)((?:-u|--user)(?:[ \t]+|=)[\"']?[^\s\"':]{1,256}:)(?!\[REDACTED)([^\s\"']{1,256})"), r"\1" + _R),
    (re.compile(r"(?i)(\b(?:docker|podman|nerdctl|helm(?:[ \t]+registry)?)[ \t]+login\b[^\n]{0,512}?[ \t](?:-p|--password)(?:[ \t]+|=))(?!\[REDACTED)(\S{1,512})"), r"\1" + _R),
    (re.compile(r"(\bredis-cli\b[^\n]{0,512}?[ \t](?:-a|--pass)(?:[ \t]+|=))(?!\[REDACTED)(\S{1,512})"), r"\1" + _R),
    (re.compile(r"(\bmysql(?:dump|admin)?\b[^\n]{0,512}?[ \t]-p)(?!['\"])(?!\[REDACTED)(\S{1,512})"), r"\1" + _R),
    (re.compile(r"((?<![A-Za-z0-9])pass:)(?!\[REDACTED)(\S{1,512})"), r"\1" + _R),
    (re.compile(r"(?i)(\baws[ \t]+configure[ \t]+set[ \t]+\S{0,64}(?:secret|token|key)\S{0,64}[ \t]+)(?!\[REDACTED)(\S{1,512})"), r"\1" + _R),
    (re.compile(r"https://hooks\.slack\.com/(?:workflows|triggers)/[A-Za-z0-9/_-]{10,200}"), "[REDACTED-SLACK-WEBHOOK]"),
    (re.compile(r"LS0tLS1CRUdJTi[A-Za-z0-9+/=]*"), "[REDACTED-KEY-MATERIAL]"),  # base64 of -----BEGIN
    (re.compile(r"(?<![A-Za-z0-9])[rs]k_(?:live|test)_[A-Za-z0-9]{8,15}(?![A-Za-z0-9])"), "[REDACTED-STRIPE-KEY]"),  # shorter than the server's 16
]
_HTPASSWD = re.compile(r"\bhtpasswd[ \t]+((?:-[A-Za-z]+[ \t]+){0,8})(\S+)[ \t]+(\S+)(?:[ \t]+(\S+))?")


def _secret_name(name: str) -> bool:
    if any(part in _SECRET_PARTS for part in re.split(r"[_.\-]", name.upper())):
        return True
    camel = _CAMEL.findall(name)
    return len(camel) > 1 and camel[-1].upper() in _SECRET_PARTS


def _env_assign(m: re.Match) -> str:
    name, eq, value = m.group(1), m.group(2), m.group(3)
    if _secret_name(name) or (name.isupper() and _HEX_KEY.fullmatch(value.strip("\"'"))):
        return f"{name}{eq}{_R}"
    return m.group(0)


# Credential headers as dict or JSON keys ({"Authorization": "Basic ..."}, 'Cookie': ...):
# the header rules need `Name:` with nothing between (activity review round 2, S2-5).
_HEADER_KEYS = frozenset({"AUTHORIZATION", "PROXY-AUTHORIZATION", "COOKIE", "SET-COOKIE"})


def _kv_quoted(m: re.Match) -> str:
    q, name, colon, value = m.group(1), m.group(2), m.group(3), m.group(4)
    if not (_secret_name(name) or name.upper() in _HEADER_KEYS) or value.strip("\"'").lower() in _TYPE_NAMES:
        return m.group(0)
    return f"{q}{name}{q}{colon}{_R}"


def _htpasswd(m: re.Match) -> str:
    flags = m.group(1)
    if "b" not in flags:
        return m.group(0)
    group = 3 if "n" in flags else 4  # -b: the password follows the file (unless -n) and the user
    if m.group(group) is None or m.group(group).startswith("[REDACTED"):
        return m.group(0)
    whole, start = m.group(0), m.start()
    return whole[: m.start(group) - start] + _R + whole[m.end(group) - start:]


# What is left of a secret a chunk cut split in two, when only one half was caught: a
# token-like run glued to a redaction marker with no separator (activity review round 2:
# an Atlassian token cut mid-way kept its first 41 characters). `=` and `:` are not part of
# the run, so `API_TOKEN=[REDACTED-SECRET]` keeps its name.
# Anchored at the start of a run: unanchored, a long run with no marker was tried from every
# position (7 s on 256 KB of KEY_KEY_...).
_REMNANT_BEFORE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{8,4096}(?=\[REDACTED-)")
_REMNANT_AFTER = re.compile(r"(\[REDACTED-[A-Z-]{0,40}\])[A-Za-z0-9+/_-]{8,4096}")


def redact(text: str) -> str:
    """The reporter's own rules, after the server's full pass."""
    text = _REMNANT_BEFORE.sub(_R, text)
    text = _REMNANT_AFTER.sub(r"\1" + _R, text)
    for pattern, repl in _MORE:
        text = pattern.sub(repl, text)
    text = _HTPASSWD.sub(_htpasswd, text)
    text = _ENV_ASSIGN.sub(_env_assign, text)
    return _KV_QUOTED.sub(_kv_quoted, text)


def clean(text: str) -> str:
    """The server's redaction, verbatim (fleet_redact_vendor.redact_preview: the bounded
    scrub, its extra rules, the PEM, k8s and base64 walks), then redact(). Raises if the
    scrubber can't load; callers treat that as 'omit'."""
    if not (ORCHESTRATOR_DIR / "hooks" / "scrubber.py").is_file():
        raise ImportError("hooks/scrubber.py is missing")
    from fleet import fleet_redact_vendor as server
    return redact(server.redact_preview(text))


_BOUNDARY = re.compile(r"[^A-Za-z0-9+/=_-]")  # not part of a token or a base64 run


def _drop_split_head(s: str) -> str:
    """s was cut at its END: drop the last, possibly split, word. Text with no whitespace
    (minified JSON) is cut after its last punctuation instead; a single unbroken token is
    dropped whole, since it may be a secret."""
    i = max(s.rfind(" "), s.rfind("\n"), s.rfind("\t"))
    if i >= 0:
        return s[: i + 1]
    m = None
    for m in _BOUNDARY.finditer(s):
        pass
    return s[: m.end()] if m else ""


def _drop_split_tail(s: str) -> str:
    """s was cut at its START: drop the first, possibly split, word (see _drop_split_head)."""
    for i, ch in enumerate(s):
        if ch in " \n\t":
            return s[i:]
    m = _BOUNDARY.search(s)
    return s[m.start():] if m else ""


def size(s: str) -> int:
    """What a string costs on the wire: its JSON-escaped UTF-8 bytes, without the quotes. The
    server takes previews by characters and bodies by bytes; this bounds both (a CJK char is 3
    bytes, a control char escapes to 6)."""
    return len(json.dumps(s, ensure_ascii=False).encode("utf-8")) - 2


def _fit(s: str, budget: int, from_end: bool) -> str:
    """The longest head (or tail) of s whose size() is within budget."""
    lo, hi = 0, min(len(s), budget)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size(s[len(s) - mid:] if from_end else s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return (s[len(s) - lo:] if from_end else s[:lo]) if lo else ""


def preview(text: object, limit: int) -> tuple[str | None, bool]:
    """(redacted text whose size() is at most `limit`, truncated?). Long text keeps its head
    and its tail with a marker between them. None when there's nothing to send or no scrubber."""
    if not isinstance(text, str) or not text:
        return None, False
    text = text.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate can't be sent
    try:
        if len(text) <= 2 * SCAN_WINDOW:
            text = clean(text)
        else:
            head = clean(_drop_split_head(text[:SCAN_WINDOW]))
            tail = clean(_drop_split_tail(text[-SCAN_WINDOW:]))
            text = head + f"\n… [{len(text) - 2 * SCAN_WINDOW:,} chars not scanned] …\n" + tail
    except Exception:
        return None, False
    if size(text) <= limit:
        return text, False
    marker = f"\n… [{len(text):,} chars, cut to {limit:,} bytes] …\n"
    half = max(0, (limit - size(marker)) // 2)
    head = _drop_split_head(_fit(text, half, from_end=False))
    tail = _drop_split_tail(_fit(text, half, from_end=True)) if half else ""
    out = head + marker + tail
    return (out if size(out) <= limit else _fit(out, limit, from_end=False)), True
