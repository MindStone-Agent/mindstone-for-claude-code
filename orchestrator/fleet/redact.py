"""Redaction for activity previews (synapse#31, activity layer): commands, file paths, tool
output, the agent's replies, subagent descriptions.

Two passes, in this order, both bounded:
1. `hooks/scrubber.py` `scrub()`, the framework's secret scrubber (keys, tokens, Bearer values,
   URL userinfo, CLI password flags, private-key blocks, key=value secrets, high-entropy
   tokens). The Synapse server vendors the same file, pinned by sha256, and runs it again.
2. `redact()` below: shapes `scrub()` leaves alone, measured on 2026-11-03:
   - the value of credential headers whatever the scheme: Authorization, Proxy-Authorization,
     Cookie, Set-Cookie, Api-Key, X-*-Key/Token/Secret/Auth (`scrub()` only handles Bearer
     values that contain a digit);
   - `NAME=value` and `name: value` (YAML, JSON, `.env`) where a whole `_`/`.`/`-` separated
     part of NAME is SECRET, TOKEN, KEY, APIKEY, PASS, PASSWD, PASSWORD, PASSPHRASE, PWD or
     CREDENTIAL(S), at any length (`scrub()` has a length floor, so `MY_SECRET_TOKEN=zz9` passes
     it), and an all-caps `NAME=` with a value of 32+ hex digits. KEYBOARD=1 is left alone;
   - passwords given as plain arguments: `curl -u user:pass`, `sshpass -p`, `docker login -p`,
     `redis-cli -a`, `mysql -pX`, `openssl ... pass:X`, `htpasswd -b`, `aws configure set
     *secret*`, `.netrc` lines;
   - Stripe keys, Slack webhook URLs, and base64 of a PEM block (a Kubernetes secret's tls.key).
   These are heuristics: a secret in a shape none of them knows still gets through.

`preview()` cuts long text to a head and a tail. Scrubbing happens before the cut, and a word
split by any cut is dropped, so no fragment of a secret survives at an edge.
If the scrubber can't be loaded, `preview()` returns None: nothing is sent unscrubbed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]
SCAN_WINDOW = 128 * 1024  # the most text scrubbed per end; beyond it, only head and tail matter

# Header names whose value is a credential: Authorization (any scheme), cookies, API keys.
_HEADER_NAMES = r"(?:proxy-)?authorization|cookie|set-cookie|api-?key|x-[a-z0-9-]{0,40}?(?:key|token|secret|auth)[a-z0-9-]{0,40}"
_AUTH_HEADER = re.compile(rf"(?im)^([ \t]*(?:{_HEADER_NAMES})[ \t]*:[ \t]*)(\S[^\r\n]{{0,4096}})")
_AUTH_INLINE = re.compile(rf"(?i)((?:-H|--header)[ \t]+[\"']?(?:{_HEADER_NAMES})[ \t]*:[ \t]*)([^\"'\r\n]{{1,4096}})")

# A name whose `_`, `.` or `-` separated parts include one of these holds a secret:
# API_KEY, tls.key, db-password yes; KEYBOARD, MONKEY no.
_SECRET_PARTS = frozenset({"SECRET", "SECRETS", "TOKEN", "TOKENS", "KEY", "KEYS", "APIKEY", "PASS", "PASSWD",
                           "PASSWORD", "PASSPHRASE", "PWD", "CREDENTIAL", "CREDENTIALS", "DOCKERCONFIGJSON"})
_VALUE = r"(\"[^\"\r\n]{0,4096}\"|'[^'\r\n]{0,4096}'|[^\s\"']{1,4096})"
# Names are matched in one bounded run, never split by backtracking (a long underscore-joined
# word with no `=` once took quadratic time here), and checked in Python.
_ENV_ASSIGN = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]{1,128})([ \t]*=[ \t]*)(?![ \t]*\[REDACTED)" + _VALUE)
_KV_COLON = re.compile(r"(?<![A-Za-z0-9_.-])([\"']?)([A-Za-z0-9_.-]{1,128})\1([ \t]*:[ \t]*)(?![ \t]*\[REDACTED)"
                       r"(\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}'|[^\s\"',}\]\[{][^\s\"',}\]]{0,4095})")
_HEX_KEY = re.compile(r"[0-9a-fA-F]{32,}")

# Command shapes that carry a password as a plain argument.
_CLI_SECRETS = [re.compile(p) for p in (
    r"(?i)(?<!\S)(?:-u|--user)(?:[ \t]+|=)[\"']?([^\s\"':]{1,256}:[^\s\"']{1,256})",            # curl user:pass
    r"\bsshpass[ \t]+-p[ \t]*(\S{1,512})",
    r"(?i)\b(?:docker|podman|nerdctl|helm(?:[ \t]+registry)?)[ \t]+login\b[^\n]{0,512}?[ \t](?:-p|--password)(?:[ \t]+|=)(\S{1,512})",
    r"\bredis-cli\b[^\n]{0,512}?[ \t](?:-a|--pass)(?:[ \t]+|=)(\S{1,512})",
    r"\bmysql(?:dump|admin)?\b[^\n]{0,512}?[ \t]-p(\S{1,512})",
    r"(?<![A-Za-z0-9])pass:(\S{1,512})",                                                           # openssl -pass pass:x
    r"(?i)\baws[ \t]+configure[ \t]+set[ \t]+\S{0,64}(?:secret|token|key)\S{0,64}[ \t]+(\S{1,512})",
    r"\bmachine[ \t]+\S{1,256}[^\n]{0,256}?\bpassword[ \t]+(\S{1,512})",                          # .netrc
    r"(?m)^[ \t]*password[ \t]+(\S{1,512})[ \t]*$",
)]
_HTPASSWD = re.compile(r"\bhtpasswd[ \t]+((?:-[A-Za-z]+[ \t]+){0,8})(\S+)[ \t]+(\S+)(?:[ \t]+(\S+))?")
_VENDOR = [re.compile(p) for p in (
    r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{8,}",                                                    # Stripe
    r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]+",               # Slack webhooks
    r"LS0tLS1CRUdJTi[A-Za-z0-9+/=]*",                                                             # base64 of -----BEGIN
)]


def _secret_name(name: str) -> bool:
    return any(part in _SECRET_PARTS for part in re.split(r"[_.\-]", name.upper()))


def _env_assign(m: re.Match) -> str:
    name, eq, value = m.group(1), m.group(2), m.group(3)
    if _secret_name(name) or (name.isupper() and _HEX_KEY.fullmatch(value.strip("\"'"))):
        return f"{name}{eq}[REDACTED-SECRET]"
    return m.group(0)


def _kv_colon(m: re.Match) -> str:
    if not _secret_name(m.group(2)):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}[REDACTED-SECRET]"


def _sub_group(rx: re.Pattern, text: str, group: int = 1) -> str:
    """Replace only `group` of each match, keeping the flag or name around it."""
    def repl(m: re.Match) -> str:
        whole, start = m.group(0), m.start()
        return whole[: m.start(group) - start] + "[REDACTED-SECRET]" + whole[m.end(group) - start:]
    return rx.sub(repl, text)


def _htpasswd(m: re.Match) -> str:
    flags = m.group(1)
    if "b" not in flags:
        return m.group(0)
    # -b: the password is on the command line, after the file (unless -n) and the user
    group = 3 if "n" in flags else 4
    if m.group(group) is None:
        return m.group(0)
    whole, start = m.group(0), m.start()
    return whole[: m.start(group) - start] + "[REDACTED-SECRET]" + whole[m.end(group) - start:]


_scrub = None


def _scrubber():
    global _scrub
    if _scrub is None:
        sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))
        from scrubber import scrub  # type: ignore
        _scrub = scrub
    return _scrub


def redact(text: str) -> str:
    """The extra rules, after scrub()."""
    text = _AUTH_HEADER.sub(r"\1[REDACTED-AUTH]", text)
    text = _AUTH_INLINE.sub(r"\1[REDACTED-AUTH]", text)
    for rx in _CLI_SECRETS:
        text = _sub_group(rx, text)
    text = _HTPASSWD.sub(_htpasswd, text)
    for rx in _VENDOR:
        text = rx.sub("[REDACTED-SECRET]", text)
    text = _ENV_ASSIGN.sub(_env_assign, text)
    return _KV_COLON.sub(_kv_colon, text)


def clean(text: str) -> str:
    """scrub() then redact(). Raises if the scrubber can't load; callers treat that as 'omit'."""
    return redact(_scrubber()(text))


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
