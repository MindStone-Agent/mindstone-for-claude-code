"""Redaction for activity previews (synapse#31, activity layer): commands, file paths, tool
output, the agent's replies, subagent descriptions.

Two passes, in this order, both bounded:
1. `hooks/scrubber.py`, the framework's secret scrubber (keys, tokens, Bearer values, URL
   userinfo, CLI password flags, private-key blocks, key=value secrets, high-entropy tokens),
   run on chunks of at most 1 KB: whole, it re-scans the text once per secret it finds, which
   costs seconds on 64 KB (Cairn's round-2 measurement). A secret found in one chunk is then
   removed from the others, for the 32 longest. The Synapse server vendors the same file,
   pinned by sha256, and runs it the same way.
2. `redact()`: the server's extra rules, verbatim (api/fleet_redact.py), then the reporter's
   own additions (more CLI password shapes, `NAME=value` and `"name": value` for secret-named
   variables, Slack workflow webhooks, base64 of a PEM header). Both sides pass the shared
   fixtures in synapse's tests/fixtures/fleet_secrets.json. These are heuristics: a secret in a
   shape none of them knows still gets through.

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

_R = "[REDACTED-SECRET]"

# --- The server's extras, verbatim (synapse api/fleet_redact.py at 0dd221c) -------------------
# Both sides run the shared fixtures (synapse tests/fixtures/fleet_secrets.json) against
# scrub() + these rules, so the reporter never sends what the server would have removed.
EXTRA: list[tuple[re.Pattern[str], str]] = [
    # Any Authorization / Proxy-Authorization header value, whatever the scheme
    # (scrub() only handles Bearer).
    (re.compile(r"(?im)\b((?:proxy-)?authorization[ \t]{0,8}:[ \t]{0,8})[^\r\n]{1,4096}"), r"\1" + _R),
    # Cookie headers and x-*-key / x-*-token headers.
    (re.compile(r"(?im)\b((?:set-)?cookie[ \t]{0,8}:[ \t]{0,8})[^\r\n]{1,4096}"), r"\1" + _R),
    (re.compile(r"(?im)(?<![A-Za-z0-9-])(x-[a-z0-9-]{0,40}(?:key|token|secret|auth)[ \t]{0,8}:[ \t]{0,8})[^\r\n]{1,4096}"), r"\1" + _R),
    # .env-style NAME=value or NAME: value where NAME contains SECRET, TOKEN,
    # KEY, PASS or PWD, at any value length (scrub() has a length floor).
    (re.compile(r"(?<![A-Za-z0-9_])((?:export[ \t]{1,8})?[A-Z0-9_]{0,64}(?:SECRET|TOKEN|KEY|PASS|PWD)[A-Z0-9_]{0,64}[ \t]{0,8}[=:][ \t]{0,8}[\"']?)(?!\[REDACTED)[^\s\"']{1,4096}"), r"\1" + _R),
    # The same names in lower case, as compound keys (db_password=, secret_key_base:).
    (re.compile(r"(?i)(?<![A-Za-z0-9_])([a-z0-9]{1,32}_(?:password|passwd|secret|token|key)(?:_[a-z0-9]{1,32}){0,3}[ \t]{0,8}[=:][ \t]{0,8}[\"']?)(?!\[REDACTED)[^\s\"']{1,4096}"), r"\1" + _R),
    # kubeconfig and similar: client-key-data:, token-data:, *-secret-data:.
    (re.compile(r"(?im)(?<![A-Za-z0-9-])([a-z-]{0,40}(?:key|token|secret|password|cert)[a-z-]{0,20}-data[ \t]{0,8}:[ \t]{0,8})\S{1,65536}"), r"\1" + _R),
    # Space-separated CLI secrets scrub() doesn't cover: -p after sshpass /
    # docker login, --password-stdin excluded.
    (re.compile(r"(?i)(\b(?:sshpass|docker[ \t]{1,8}login|htpasswd)\b[^\n]{0,120}?[ \t]-p[ \t]{1,8})([^\s'\"]{3,256})"), r"\1" + _R),
    # Lines of base64 that look like a key body (PEM content without its BEGIN
    # line, e.g. a slice of a key file): 60 to 76 base64 characters alone on a
    # line, including / and +.
    (re.compile(r"(?m)^[ \t]{0,8}[A-Za-z0-9+/]{60,76}={0,2}[ \t]{0,8}$"), "[REDACTED-KEY-MATERIAL]"),
    # GitLab, Stripe restricted/webhook, Slack webhooks, Sentry DSN keys.
    (re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_\-]{20,64}"), "[REDACTED-GITLAB-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|whsec|pk)_(?:live|test)?_?[A-Za-z0-9]{16,256}"), "[REDACTED-STRIPE-KEY]"),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,200}"), "[REDACTED-SLACK-WEBHOOK]"),
    (re.compile(r"(?i)(AccountKey=)[A-Za-z0-9+/=]{20,256}"), r"\1" + _R),
    # Round-2 security review: shapes that got through.
    # Google OAuth access and refresh tokens; Vault tokens; Slack app tokens;
    # Databricks; Discord webhooks.
    (re.compile(r"(?<![A-Za-z0-9])ya29\.[A-Za-z0-9_\-]{20,2048}"), "[REDACTED-GOOGLE-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9/])1//[A-Za-z0-9_\-]{20,512}"), "[REDACTED-GOOGLE-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])hv[sbr]\.[A-Za-z0-9_\-]{20,512}"), "[REDACTED-VAULT-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])xapp-[0-9A-Za-z\-]{10,256}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])dapi[0-9a-f]{32}(?:-[0-9]{1,3})?"), "[REDACTED-DATABRICKS-TOKEN]"),
    (re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]{5,30}/[A-Za-z0-9_\-]{20,128}"), "[REDACTED-DISCORD-WEBHOOK]"),
    # Signed URLs: Azure SAS sig=, AWS presigned signature and session token.
    (re.compile(r"(?i)([?&]sig=)[A-Za-z0-9%+/=]{16,512}"), r"\1" + _R),
    (re.compile(r"(?i)([?&]X-Amz-(?:Signature|Security-Token|Credential)=)[^&\s\"'<>]{8,4096}"), r"\1" + _R),
    # CLI passwords: redis-cli -a, curl -u/--user user:pass, mysql -p'quoted'.
    (re.compile(r"(\bredis-cli\b[^\n]{0,160}?[ \t]-a[ \t]{1,8})([^\s'\"]{1,256})"), r"\1" + _R),
    (re.compile(r"(\bcurl\b[^\n]{0,200}?[ \t](?:-u|--user)(?:[ \t]{1,8}|=)['\"]?[^\s:'\"]{1,128}:)([^\s'\"]{1,256})"), r"\1" + _R),
    (re.compile(r"(\bmysql(?:dump|admin)?\b[^\n]{0,200}?[ \t]-p)(['\"])[^'\"\n]{1,256}\2"), r"\1\2" + _R + r"\2"),
    # .netrc password lines and .pgpass lines (host:port:db:user:password).
    (re.compile(r"(?im)((?:^|\bmachine[ \t]+\S{1,253}[^\n]{0,200}?)[ \t]*\bpassword[ \t]+)(?![:=])(?!\[REDACTED)\S{1,256}"), r"\1" + _R),
    (re.compile(r"(?m)^([^:\s]{1,253}:(?:[0-9]{1,5}|\*):[^:\s]{1,128}:[^:\s]{1,128}:)(?!\[REDACTED)\S{1,256}$"), r"\1" + _R),
    # YAML/INI keys at the start of a line, any value length (token: abc), and
    # an unquoted multi-word password. Type names (password: string) survive.
    (re.compile(r"(?im)^([ \t]{0,32}-?[ \t]{0,4}(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key)[ \t]{0,8}:[ \t]{0,8}['\"]?)(?!\[REDACTED)(?!(?:string|str|int|number|bool|boolean|null|none|any|unknown|undefined|optional|true|false)\b)[^\s'\"]{1,4096}(?:[ \t]{1,8}[^\s|\[\](){}<>=,;'\"]{1,256}){0,8}"), r"\1" + _R),
]


_END_KEY = re.compile(r"-----END [A-Z ]{0,40}PRIVATE KEY-----")
_B64_LINE = re.compile(r"[ \t]{0,8}[A-Za-z0-9+/=]{4,76}[ \t]{0,8}")


def _key_tail(text: str) -> str:
    """Key material left above an END ... PRIVATE KEY line whose BEGIN line was
    cut off (a slice of a key file, or a preview's head/tail cut). Walks back
    line by line from each END line, so it stays linear."""
    if "PRIVATE KEY-----" not in text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _END_KEY.search(line)
        if not m:
            continue
        lines[i] = "[REDACTED-PRIVATE-KEY]" + line[m.end():]
        j = i - 1
        while j >= 0 and i - j <= 200 and _B64_LINE.fullmatch(lines[j]):
            lines[j] = "[REDACTED-PRIVATE-KEY]"
            j -= 1
    return "\n".join(lines)


_CHUNK = 1024
_PROPAGATE_MAX = 32


def _chunks(text: str) -> list[str]:
    """Pieces of at most _CHUNK characters that rejoin to `text`, each cut
    after a line break if one falls in its second half, else after a space or
    tab, else hard."""
    out: list[str] = []
    i, n = 0, len(text)
    while n - i > _CHUNK:
        lo, hi = i + _CHUNK // 2, i + _CHUNK
        j = text.rfind("\n", lo, hi)
        if j < 0:
            j = max(text.rfind(" ", lo, hi), text.rfind("\t", lo, hi))
        end = j + 1 if j >= 0 else hi
        out.append(text[i:end])
        i = end
    out.append(text[i:])
    return out


def _scrub_bounded(text: str) -> str:
    if len(text) <= _CHUNK:
        return _scrubber().scrub_collect(text)[0]
    parts, found = [], set()
    for piece in _chunks(text):
        clean, values = _scrubber().scrub_collect(piece)
        parts.append(clean)
        found |= values
    text = "".join(parts)
    for v in sorted((v for v in found if _scrubber()._propagatable(v)), key=len, reverse=True)[:_PROPAGATE_MAX]:
        text = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(v) + r"(?![A-Za-z0-9_])", _R, text)
    return text


_SECRET_DATA_HDR = re.compile(r"^([ \t]*)(?:data|stringData):[ \t]*$")
_YAML_KV = re.compile(r"^([ \t]+[^\s:#][^:\n]{0,252}:[ \t]+)(\S.{0,65535})$")


def _k8s_secret_data(text: str) -> str:
    """Values under a `data:` / `stringData:` block (a Kubernetes Secret, or
    `kubectl get secret -o yaml`) are secrets whatever their key is called
    (tls.key, .dockerconfigjson). Only when the text says `kind: Secret`.
    A line walk, so linear."""
    if "kind: Secret" not in text or "ata:" not in text:
        return text
    lines = text.split("\n")
    indent = None
    for i, line in enumerate(lines):
        if indent is not None:
            lead = len(line) - len(line.lstrip(" \t"))
            if line.strip() and lead <= indent:
                indent = None
            else:
                m = _YAML_KV.match(line)
                if m and not m.group(2).startswith("[REDACTED"):
                    lines[i] = m.group(1) + _R
                continue
        m = _SECRET_DATA_HDR.match(line)
        if m:
            indent = len(m.group(1))
    return "\n".join(lines)


_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{100,}={0,2}")


def _long_b64(m: re.Match[str]) -> str:
    """A 100+ character base64 run with upper case, lower case and digits:
    past the scrubber's 100-character bare-token limit. Lowercase hex (git
    and sha256 digests) has no upper case and survives."""
    s = m.group(0)
    if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
        return "[REDACTED-KEY-MATERIAL]"
    return s



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


def _kv_quoted(m: re.Match) -> str:
    q, name, colon, value = m.group(1), m.group(2), m.group(3), m.group(4)
    if not _secret_name(name) or value.strip("\"'").lower() in _TYPE_NAMES:
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


_scrub_mod = None


def _scrubber():
    """hooks/scrubber.py, the framework's scrubber (the server vendors it verbatim, pinned)."""
    global _scrub_mod
    if _scrub_mod is None:
        sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))
        import scrubber  # type: ignore
        _scrub_mod = scrubber
    return _scrub_mod


def redact(text: str) -> str:
    """The extra rules, after the scrubber: the server's, then the reporter's own."""
    for pattern, repl in EXTRA:
        text = pattern.sub(repl, text)
    for pattern, repl in _MORE:
        text = pattern.sub(repl, text)
    text = _HTPASSWD.sub(_htpasswd, text)
    text = _ENV_ASSIGN.sub(_env_assign, text)
    text = _KV_QUOTED.sub(_kv_quoted, text)
    text = _B64_RUN.sub(_long_b64, text)
    text = _k8s_secret_data(text)
    return _key_tail(text)


def clean(text: str) -> str:
    """The bounded scrub, then redact(). Raises if the scrubber can't load; callers treat that
    as 'omit'."""
    return redact(_scrub_bounded(text))


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
