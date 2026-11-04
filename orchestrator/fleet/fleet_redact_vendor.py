"""Server-side redaction for Fleet activity previews (synapse#31).

The same pass the reporter runs: MS4CC's scrubber (vendored verbatim in
api/vendor/scrubber.py, sha256-pinned), then the extra rules below for shapes
it leaves on purpose because it was written for conversation text, not shell
commands and their output. Plumb's reporter-side redact.py is meant to carry
the same extras; when its sha256 is posted, this module is checked against it
through the shared fixtures in tests/fixtures/fleet_secrets.json.

Every EXTRA rule is bounded. The scrubber isn't on its own: after its
key=value pass it re-scans the whole text once per secret value it found, so
64 KB of distinct NAME=value lines cost 1.5 to 2.9 s (round-2 review). It
stays verbatim (it's pinned), so this module bounds it from outside: it runs
on chunks of at most _CHUNK characters, cut at a line break where there is
one, and the cross-chunk part of that re-scan (a secret found in one chunk,
repeated bare in another) is done here, once over the whole text, for the
longest _PROPAGATE_MAX values. The EXTRA rules then run over the rejoined
whole text, so a NAME = value split across a cut is still caught.
"""

from __future__ import annotations

# ruff: noqa: E501  (the patterns read best on one line each)
import json
import re
from contextvars import ContextVar

import sys as _sys  # vendored change: the scrubber is hooks/scrubber.py here
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "hooks"))
from scrubber import _propagatable, scrub_collect  # noqa: E402

# What this pass has redacted so far, so redact_preview can remove each value
# where it repeats bare too (Hearth's blocker, round 5). Per call (the API
# redacts in a threadpool), so a context variable, not a global.
_SINK: ContextVar[set[str] | None] = ContextVar("fleet_redact_sink", default=None)


def _note(value: str) -> None:
    sink = _SINK.get()
    if sink is None:
        return
    value = value.strip(" \t\r\n\"'\\,;")
    if "[REDACTED" in value or "\n" in value:
        return
    # The value, and the pieces a repeat is likely to carry on its own: the
    # credential after a scheme ("Basic dXNl..."), a cookie's value after its
    # name (sid=...), and each without base64 padding.
    pieces = {value}
    for part in re.split(r"[\s;,&]+", value):
        pieces.add(part)
        pieces.add(part.split("=", 1)[1] if "=" in part.rstrip("=") else part)
    for v in pieces:
        v = v.strip(" \t\r\n\"'\\,;")
        for w in (v, v.rstrip("=")):
            if len(w) >= 6:
                sink.add(w)

_R = "[REDACTED-SECRET]"

EXTRA: list[tuple[re.Pattern[str], str]] = [
    # Any Authorization / Proxy-Authorization header value, whatever the scheme
    # (scrub() only handles Bearer).
    (re.compile(r"(?im)\b((?:proxy-)?authorization[ \t]{0,8}:[ \t]{0,8})[^\r\n]{1,4096}"), r"\1" + _R),
    # Cookie headers and x-*-key / x-*-token headers.
    (re.compile(r"(?im)\b((?:set-)?cookie[ \t]{0,8}:[ \t]{0,8})[^\r\n]{1,4096}"), r"\1" + _R),
    (re.compile(r"(?im)(?<![A-Za-z0-9-])(x-[a-z0-9-]{0,40}(?:key|token|secret|auth)[ \t]{0,8}:[ \t]{0,8})[^\r\n]{1,4096}"), r"\1" + _R),
    # .env-style NAME=value or NAME: value where NAME contains SECRET, TOKEN,
    # KEY, PASS or PWD, at any value length (scrub() has a length floor).
    (re.compile(r"(?<![A-Za-z0-9_])(?!(?:SECRET|TOKEN|KEY|PASS|PWD)[ \t]{0,8}:)((?:export[ \t]{1,8})?[A-Z0-9_]{0,64}(?:SECRET|TOKEN|KEY|PASS|PWD)[A-Z0-9_]{0,64}[ \t]{0,8}[=:][ \t]{0,8}[\"']?)(?!\[REDACTED)[^\s\"']{1,4096}"), r"\1" + _R),
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
    # .netrc: the one-line form (machine ... password X), and a password line
    # on its own (the multi-line form): only "password <one token>" alone on
    # its line, so prose ("password reset email sent") survives.
    (re.compile(r"(?i)(\bmachine[ \t]+\S{1,253}[^\n]{0,200}?[ \t]\bpassword[ \t]+)(?![:=])(?!\[REDACTED)\S{1,256}"), r"\1" + _R),
    (re.compile(r"(?im)^([ \t]*password[ \t]+)(?![:=])(?!\[REDACTED)\S{1,256}([ \t]*)$"), r"\1" + _R + r"\2"),
    # .pgpass: exactly host:port:db:user:password (an IPv6 address has more
    # fields, so it doesn't match).
    (re.compile(r"(?m)^([^:\s]{1,253}:(?:[0-9]{1,5}|\*):[^:\s]{1,128}:[^:\s]{1,128}:)(?!\[REDACTED)[^:\s]{1,256}$"), r"\1" + _R),
    # YAML/INI secret keys at the start of a line with a single value of any
    # length, alone on the line (token: abc12; not "token: The API token
    # ..."), and a multi-word value for password keys only. Type names
    # (password: string) survive.
    (re.compile(r"(?im)^([ \t]{0,32}-?[ \t]{0,4}(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key)[ \t]{0,8}:[ \t]{0,8}['\"]?)(?!\[REDACTED)(?!(?:string|str|int|number|bool|boolean|null|none|any|unknown|undefined|optional|true|false)\b)[^\s'\"]{1,4096}(['\"]?[ \t]*)$"), r"\1" + _R + r"\2"),
    (re.compile(r"(?im)^([ \t]{0,32}-?[ \t]{0,4}(?:password|passwd|passphrase)[ \t]{0,8}:[ \t]{0,8})(?!\[REDACTED)(?!(?:string|str|int|number|bool|boolean|null|none|any|unknown|undefined|optional|true|false)\b)[^\s|\[\](){}<>=,;'\"]{1,256}(?:[ \t]{1,8}[^\s|\[\](){}<>=,;'\"]{1,256}){1,8}[ \t]*$"), r"\1" + _R),
    # Round 3: more vendor tokens and key formats.
    (re.compile(r"(?<![A-Za-z0-9])AGE-SECRET-KEY-1[0-9A-Z]{58}"), "[REDACTED-AGE-KEY]"),
    (re.compile(r"(?im)^([ \t]*(?:PrivateKey|PresharedKey)[ \t]{0,8}=[ \t]{0,8})[A-Za-z0-9+/]{40,44}={0,2}"), r"\1" + _R),
    (re.compile(r"(?<![A-Za-z0-9])do[opr]_v1_[0-9a-f]{64}"), "[REDACTED-DIGITALOCEAN-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])shp(?:at|ss|ca|pa)_[0-9a-fA-F]{32}"), "[REDACTED-SHOPIFY-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_\-]{50,256}"), "[REDACTED-PYPI-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])ATATT[A-Za-z0-9_\-=]{50,512}"), "[REDACTED-ATLASSIAN-TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])PMAK-[0-9a-f]{24}-[0-9a-f]{34}"), "[REDACTED-POSTMAN-KEY]"),
    # Round 3: command-line passwords.
    (re.compile(r"(\bhtpasswd\b[^\n]{0,80}?[ \t]-[a-zA-Z]{0,4}b[a-zA-Z]{0,4}[ \t]{1,8}(?:-[A-Za-z][ \t]{1,8}){0,3}\S{1,256}[ \t]{1,8}\S{1,128}[ \t]{1,8})(['\"]?)[^\s'\"]{1,256}\2"), r"\1\2" + _R + r"\2"),
    (re.compile(r"(?i)(\bopenssl\b[^\n]{0,200}?[ \t]-pass(?:in|out)?[ \t]{1,8}['\"]?(?:pass|env):)[^\s'\"]{1,256}"), r"\1" + _R),
    (re.compile(r"(\bopenssl\b[^\n]{0,200}?[ \t]-k[ \t]{1,8})(['\"]?)[^\s'\"]{1,256}\2"), r"\1\2" + _R + r"\2"),
    (re.compile(r"(\bsshpass\b[^\n]{0,40}?[ \t]-p[ \t]{0,8})(['\"])[^'\"\n]{1,256}\2"), r"\1\2" + _R + r"\2"),
    (re.compile(r"(\becho[ \t]{1,8}(?:-[ne][ \t]{1,8})?)(['\"]?)[^\s'\"|]{1,256}\2([ \t]{0,8}\|[ \t]{0,8}sudo\b[^\n]{0,40}?[ \t]-S\b)"), r"\1\2" + _R + r"\2\3"),
    # Round 4 (Plumb): credential headers written as dict or JSON keys
    # ({"Authorization": "Basic ..."}, 'Cookie': 'sid=...'), either quote,
    # and JSON inside a string ({\"Authorization\": \"...\"}, round 5).
    (re.compile(r"(?i)(\\?[\"'](?:proxy-)?authorization\\?[\"'][ \t]{0,8}:[ \t]{0,8}\\?[\"'])(?!\[REDACTED)(?:[^\"'\\\n]|\\.){1,4096}(\\?[\"'])"), r"\1" + _R + r"\2"),
    (re.compile(r"(?i)(\\?[\"'](?:set-)?cookie\\?[\"'][ \t]{0,8}:[ \t]{0,8}\\?[\"'])(?!\[REDACTED)(?:[^\"'\\\n]|\\.){1,4096}(\\?[\"'])"), r"\1" + _R + r"\2"),
    (re.compile(r"(?i)(\\?[\"']x-[a-z0-9-]{0,40}(?:key|token|secret|auth)\\?[\"'][ \t]{0,8}:[ \t]{0,8}\\?[\"'])(?!\[REDACTED)(?:[^\"'\\\n]|\\.){1,4096}(\\?[\"'])"), r"\1" + _R + r"\2"),
    # A bare secret key in lower case with any value, dotted or not
    # (password=correct.horse.battery.staple; Plumb, round 3). The scrubber
    # takes a dotted value for code; this over-redacts code like
    # password=args.password, which is the safe side.
    (re.compile(r"(?i)(?<![A-Za-z0-9_])((?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key)[ \t]{0,8}=[ \t]{0,8}(?:\\?[\"'])?)(?!\[REDACTED)[^\s\"']{1,4096}"), r"\1" + _R),
    # Round 3: JSON keys naming a secret, any value (punctuation, short).
    (re.compile(r"(?i)(\"[A-Za-z0-9_\-]{0,40}(?:secret|password|passwd|passphrase|token|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)(?:[_-]?(?:value|data|b64|base64))?\"[ \t]{0,8}:[ \t]{0,8}\")(?!\[REDACTED)(?:[^\"\\\n]|\\.){1,4096}\""), r"\1" + _R + "\""),
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
            _note(lines[j])
            lines[j] = "[REDACTED-PRIVATE-KEY]"
            j -= 1
    return "\n".join(lines)


_CHUNK = 1024
_SEAM = 512
_PROPAGATE_MAX = 64

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")
# Key material after a BEGIN line with no END: base64 runs of 20+, each
# after up to 256 separators (indentation counts: a key in a YAML block
# nested 16 deep, round 5) (newlines raw or JSON-escaped, spaces, a line
# number from `cat -n`, `nl`, `awk '{print NR, $0}'` or a Read tool's "N→"). Modelled on the scrubber's own
# truncated-key rule, so prose after the key ("Now I will run...") is kept.
_PEM_SEP = r"(?:\\[nrt]|\s|\d{1,9}(?:[ \t:\-]|\\t|\u2192))"
_PEM_BODY = re.compile(
    rf"(?:{_PEM_SEP}{{0,256}}[A-Za-z0-9+/=]{{20,4096}}){{0,400}}"
    # and a last, shorter line of the key (4+), alone on its line
    rf"(?:{_PEM_SEP}{{0,256}}[A-Za-z0-9+/=]{{4,19}}(?=[ \t]*(?:\n|\\n|$)))?"
)


def _pem_blocks(text: str) -> str:
    """Private-key blocks, found on the whole text before it's chunked (a
    block is longer than a chunk). BEGIN to END when the END is within 16 KB;
    otherwise BEGIN and the key material after it. Linear: the END is looked
    for once per block, and never again once there is none."""
    if "PRIVATE KEY-----" not in text:
        return text
    out, i, no_end = [], 0, False
    while True:
        m = _PEM_BEGIN.search(text, i)
        if not m:
            break
        e = None if no_end else _PEM_END.search(text, m.end())
        if e is None:
            no_end = True
        if e is not None and e.start() - m.end() <= 16384:
            stop = e.end()
        else:
            body = _PEM_BODY.match(text, m.end())
            if body is None or body.end() == m.end():
                # No key material we recognise: leave the BEGIN line for the
                # scrubber's own truncated-key rule, which needs it.
                out.append(text[i:m.end()])
                i = m.end()
                continue
            stop = body.end()
        for run in re.findall(r"[A-Za-z0-9+/=]{20,}", text[m.end():stop]):
            _note(run)
        out.append(text[i:m.start()])
        out.append("[REDACTED-PRIVATE-KEY]")
        i = stop
    out.append(text[i:])
    return "".join(out)


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
        if j < 0:
            # No whitespace (minified JSON): after a quote, bracket or
            # backtick, which can't be inside a key=value value, so the cut
            # doesn't split one (round 4).
            j = max(text.rfind(c, lo, hi) for c in "\"'<>`\\")
        end = j + 1 if j >= 0 else hi
        out.append(text[i:end])
        i = end
    out.append(text[i:])
    return out


_WORD = re.compile(r"[A-Za-z0-9_]+")


def _propagate(text: str, found: set[str]) -> str:
    """Remove every found secret wherever else it appears whole (the
    scrubber's own rule: at [A-Za-z0-9_] boundaries). A value of word
    characters only is exactly a maximal word run there, so all of those go in
    one pass with a set lookup; the rest (values with punctuation) take a
    regex each, the longest _PROPAGATE_MAX of them."""
    vals = {v for v in found if _propagatable(v)}
    if not vals:
        return text
    words = {v for v in vals if _WORD.fullmatch(v)}
    if words:
        text = _WORD.sub(lambda m: _R if m.group(0) in words else m.group(0), text)
    for v in sorted(vals - words, key=len, reverse=True)[:_PROPAGATE_MAX]:
        text = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(v) + r"(?![A-Za-z0-9_])", _R, text)
    return text


_BOUNDARY = " \t\n\"'<>`,;()[]{}"


def _snap(text: str, lo: int, hi: int) -> tuple[int, int]:
    """Move a window's edges out to the nearest boundary (up to 64 characters),
    so it doesn't start or end mid-token: a token cut by the window's edge
    looks like a standalone one to the scrubber (round 4)."""
    for k in range(lo, max(0, lo - 64) - 1, -1):
        if k == 0 or text[k - 1] in _BOUNDARY:
            lo = k
            break
    for k in range(hi, min(len(text), hi + 64) + 1):
        if k == len(text) or text[k] in _BOUNDARY:
            hi = k
            break
    return lo, hi


def _scrub_bounded_noted(text: str) -> str:
    return _scrub_bounded(text, _SINK.get())


def _scrub_bounded(text: str, sink: set[str] | None = None) -> str:
    """scrub(), kept linear. Private-key blocks go first, on the whole text.
    Then the scrubber runs per chunk, and again on a window of _SEAM either
    side of every cut, so a key and its value on either side of one (a
    'password': at the end of a chunk) are seen together; right to left, so
    the earlier cut positions stay put. Then the found values are removed
    everywhere (_propagate)."""
    text = _pem_blocks(text)
    if len(text) <= _CHUNK:
        clean, found = scrub_collect(text)
        if sink is not None:
            sink |= found
        return _propagate(clean, found)
    parts, found, cuts, pos = [], set(), [], 0
    for piece in _chunks(text):
        clean, values = scrub_collect(piece)
        parts.append(clean)
        found |= values
        pos += len(clean)
        cuts.append(pos)
    text = "".join(parts)
    for cut in reversed(cuts[:-1]):
        lo, hi = _snap(text, max(0, cut - _SEAM), min(len(text), cut + _SEAM))
        clean, values = scrub_collect(text[lo:hi])
        text = text[:lo] + clean + text[hi:]
        found |= values
    if sink is not None:
        sink |= found
    return _propagate(text, found)


_DOC_SPLIT = re.compile(r"(?m)^---[ \t]*$")
_KIND_SECRET = re.compile(r"(?im)^[ \t]*\"?kind\"?[ \t]*:[ \t]*['\"]?Secret['\"]?[ \t]*,?[ \t]*$")
_SECRET_DATA_HDR = re.compile(r"^([ \t]*)\"?(?:data|stringData)\"?[ \t]*:[ \t]*(\{)?[ \t]*$")
_YAML_KV = re.compile(r"^([ \t]+[^\s:#][^:\n]{0,252}:[ \t]+)(\S.{0,65535})$")
_JSON_KV = re.compile(r"^([ \t]*\"[^\"\n]{1,253}\"[ \t]*:[ \t]*\")((?:[^\"\\\n]|\\.){0,65535})(\",?[ \t]*)$")


def _secret_doc(doc: str) -> str:
    """One YAML/JSON document that is a Kubernetes Secret: every value under
    its data: / stringData: block, whatever the key is called, including the
    lines of a block scalar (.htpasswd: |) and JSON ("data": {...})."""
    lines = doc.split("\n")
    indent: int | None = None
    json_block = False
    for i, line in enumerate(lines):
        if indent is not None:
            lead = len(line) - len(line.lstrip(" \t"))
            stripped = line.strip()
            if json_block and stripped.startswith("}"):
                indent, json_block = None, False
                continue
            if stripped and lead <= indent and not json_block:
                indent = None
            else:
                if not stripped:
                    continue
                m = (_JSON_KV if json_block else _YAML_KV).match(line)
                if m:
                    if not m.group(2).startswith("[REDACTED"):
                        _note(m.group(2))
                        tail = m.group(3) if json_block else ""
                        lines[i] = m.group(1) + _R + tail
                elif not stripped.startswith("[REDACTED"):
                    # a block scalar's line, or a value on its own line
                    _note(stripped)
                    lines[i] = line[:lead] + _R
                continue
        m = _SECRET_DATA_HDR.match(line)
        if m:
            indent, json_block = len(m.group(1)), m.group(2) is not None
    return "\n".join(lines)


_JSON_KIND_SECRET = re.compile(r"\\?\"kind\\?\"[ \t]*:[ \t]*\\?\"Secret\\?\"")
_JSON_DATA_OPEN = re.compile(r"\\?\"(?:data|stringData)\\?\"[ \t]*:[ \t]*\{")
# One "key": "value" pair, plain or JSON-escaped (inside a string, as in the
# last-applied-configuration annotation). A value may hold braces.
_JSON_PAIR = {
    '"': re.compile(r"[ \t\r\n,]*(\"[^\"\\\n]{1,253}\"[ \t]*:[ \t]*\")((?:[^\"\\\n]|\\.){0,65536})(\")"),
    # inside a string: a value's own escapes are \\\\ (a backslash), \\\" (a
    # quote) and \\x (\\n, \\u0026, ...), none of which ends the value
    '\\"': re.compile(r"[ \t\r\n,]*(\\\"(?:[^\"\\\n]|\\\\\\\\){1,253}\\\"[ \t]*:[ \t]*\\\")((?:[^\"\\\n]|\\{4}|\\{3}\"|\\{2}[^\"\\\n])*)(\\\")"),
}
# The same pairs anywhere on a line: what's left of a data object the pair
# scan couldn't follow (a null or number value, a nested object) is redacted
# rather than skipped (round 6).
_JSON_ANY_PAIR = {
    '"': re.compile(r"(\"[^\"\\\n]{1,253}\"[ \t]*:[ \t]*\")((?:[^\"\\\n]|\\.){0,65536})(\")"),
    '\\"': re.compile(r"(\\\"(?:[^\"\\\n]|\\\\\\\\){1,253}\\\"[ \t]*:[ \t]*\\\")((?:[^\"\\\n]|\\{4}|\\{3}\"|\\{2}[^\"\\\n])*)(\\\")"),
}


def _json_secret_data(doc: str) -> str:
    """A Secret as one-line JSON (kubectl -o json | jq -c, an API response,
    or escaped inside another string): every value in its "data" or
    "stringData" object, pair by pair, so a value holding braces (an
    embedded config.json) doesn't stop the scan (round 5)."""
    if not _JSON_KIND_SECRET.search(doc):
        return doc
    out, i = [], 0
    for m in _JSON_DATA_OPEN.finditer(doc):
        if m.start() < i:
            continue
        q = '\\"' if doc[m.start()] == "\\" else '"'
        pair = _JSON_PAIR[q]
        out.append(doc[i:m.end()])
        i = m.end()
        while True:
            p = pair.match(doc, i)
            if not p:
                rest = doc[i:].lstrip(" \t\r\n,")
                if not rest.startswith("}") and not rest.startswith("\\}"):
                    eol = doc.find("\n", i)
                    eol = len(doc) if eol < 0 else eol

                    def any_pair(a: re.Match[str]) -> str:
                        if a.group(2).startswith("[REDACTED"):
                            return a.group(0)
                        _note(a.group(2))
                        return a.group(1) + _R + a.group(3)

                    # both quoting styles: a later data object on this line
                    # in the other style is skipped by the loop (final pass)
                    seg = _JSON_ANY_PAIR['\\"'].sub(any_pair, doc[i:eol])
                    out.append(_JSON_ANY_PAIR['"'].sub(any_pair, seg))
                    i = eol
                break
            out.append(doc[i:p.start(2)])
            if not p.group(2).startswith("[REDACTED"):
                _note(p.group(2))
            out.append(p.group(2) if p.group(2).startswith("[REDACTED") else _R)
            out.append(p.group(3))
            i = p.end(3)
    out.append(doc[i:])
    return "".join(out)


def _k8s_secret_data(text: str) -> str:
    """Values under a Secret's data: block (`kubectl get secret -o yaml|json`,
    a manifest), per document, so a ConfigMap beside it keeps its values.
    A line walk, so linear."""
    if "ecret" not in text or "ata" not in text:
        return text
    docs = _DOC_SPLIT.split(text)
    seps = _DOC_SPLIT.findall(text)
    out = []
    for k, doc in enumerate(docs):
        out.append(_json_secret_data(_secret_doc(doc) if _KIND_SECRET.search(doc) else doc))
        if k < len(seps):
            out.append(seps[k])
    return "".join(out)


_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{100,}={0,2}")


def _long_b64(m: re.Match[str]) -> str:
    """A 100+ character base64 run with upper case, lower case and digits:
    past the scrubber's 100-character bare-token limit. Lowercase hex (git
    and sha256 digests) has no upper case and survives, and so does a path
    (a / every few characters; base64 has one in 64)."""
    s = m.group(0)
    if s.count("/") * 20 > len(s):
        return s
    if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
        _note(s)
        return "[REDACTED-KEY-MATERIAL]"
    return s


# Round 4 (Plumb): a hard cut through a token can leave its first half
# beside the marker the second half became, too short for its own rule
# ("ATATT3x..Gf[REDACTED-HIGH-ENTROPY]"). A token-like run of 8+ glued to a
# marker goes too. Anchored at the run's start, so it's one try per run.
# No "=" or ":" in the run, so a key in front of its value ("password=[RED")
# isn't one.
_REMNANT_BEFORE = re.compile(r"(?<![A-Za-z0-9_\-+/])[A-Za-z0-9_\-+/]{8,4096}(?=\[REDACTED)")
_REMNANT_AFTER = re.compile(r"(\[REDACTED[A-Z\-]{0,40}\])[A-Za-z0-9_\-+/.]{8,4096}")


def _remnants(text: str) -> str:
    if "[REDACTED" not in text:
        return text
    def before(m: re.Match[str]) -> str:
        _note(m.group(0))
        return _R

    def after(m: re.Match[str]) -> str:
        _note(m.group(0)[len(m.group(1)):])
        return m.group(1) + _R

    text = _REMNANT_BEFORE.sub(before, text)
    return _REMNANT_AFTER.sub(after, text)


def redact_preview(text: str) -> str:
    """scrub() (bounded, above), then the extras. Irreversible; never raises
    on a str. Line endings are normalised first: a line-anchored rule's `$`
    doesn't match before a \r, so Windows (CRLF) text got past 9 of them
    (Plumb, round 3). JSON string values are redacted as text of their own
    first (_json_strings)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    original = text
    found: set[str] = set()
    token = _SINK.set(found)
    try:
        text = _redact_core(_json_strings(text, 0))
        # Every value redacted above by its context (a key, a header, a
        # Secret, a key block) is a known secret where it repeats bare too
        # (echo $TOKEN, a second .env line), the same propagation the
        # scrubber gives its own finds (Hearth's blocker, round 5).
        return _cut_open_string(_remnants(_propagate(text, found)), original)
    finally:
        _SINK.reset(token)


CUT = "[REDACTED-CUT]"


def _open_string_at(text: str) -> int | None:
    """The earlier of two readings: by quote parity, and by structure (a quote opens a string
    only at a line's start or after one of {[,: ). A stray quote earlier on the line (5" of
    output: {"k": "...) evens the parity, and structure still sees the open value."""
    a, b = _open_parity(text, False), _open_parity(text, True)
    return b if a is None else a if b is None else min(a, b)


def _open_parity(text: str, structural: bool) -> int | None:
    """Where the first JSON string still open at the end of its line opens, or None. A quote
    after an odd run of backslashes is escaped and opens nothing."""
    if '"' not in text:
        return None
    start = 0
    while start < len(text):
        end = text.find("\n", start)
        end = len(text) if end < 0 else end
        opened, i = -1, start
        while i < end:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                if opened >= 0:
                    opened = -1
                elif not structural:
                    opened = i
                else:
                    j = i - 1
                    while j >= start and text[j] in " \t":
                        j -= 1
                    if j < start or text[j] in "{[,:":
                        opened = i
            i += 1
        if opened >= 0:
            return opened
        start = end + 1
    return None


def _quotes(text: str, start: int) -> int:
    """Unescaped quotes on the line that starts at start."""
    end = text.find("\n", start)
    end = len(text) if end < 0 else end
    n, i = 0, start
    while i < end:
        if text[i] == "\\":
            i += 2
            continue
        n += text[i] == '"'
        i += 1
    return n


def _cut_open_string(text: str, original: str | None = None) -> str:
    """text (the rules' output), from the opening quote of the first JSON string still open
    at the end of its line to the end, as one CUT marker. A tool that caps its output
    (web_fetch's maxChars) can end the text inside a JSON string, and an unterminated literal
    reads to the rules as escaped text: cut just before its closing quote, 16 of the 89 shared
    secrets went out whole (MindStone #290 round 5). This runs on the redacted text, so it only
    removes what the rules let through, never anything they hid. A stray quote in prose (5"
    long) opens a string too: what follows it on the board is lost, which a preview can afford.

    A rule can eat the escapes before a value (password=\\\" became password=[REDACTED]"),
    leaving a raw quote that closes the string in the output though it was open in original,
    or, with two such values on the line, reopens it past what it should have cut. So
    original's open string is found too and its line mapped into text (counted from either end,
    the earlier: a rule can fold lines). The cut starts at that line's start if text has no open
    string by then, or has one on that line but a different count of quotes there than
    original's (a rule changed them)."""
    at = _open_string_at(text)
    o = _open_string_at(original) if original is not None else None
    if o is not None:
        line = original.count("\n", 0, o)
        k = max(0, min(line, text.count("\n") - (original.count("\n") - line)))
        line_start = 0
        for _ in range(k):
            line_start = text.find("\n", line_start) + 1
        al = None if at is None else text.count("\n", 0, at)
        if al is None or al > k or (al == k and _quotes(text, line_start) != _quotes(original, original.rfind("\n", 0, o) + 1)):
            at = line_start
    return text if at is None else text[:at] + CUT


def _redact_core(text: str) -> str:
    """Every rule but the final propagation, on normalised text; what it removes
    is noted in the current sink."""
    text = _scrub_bounded_noted(text)
    for pattern, repl in EXTRA:
        text = pattern.sub(_capturing(repl), text)
    text = _B64_RUN.sub(_long_b64, text)
    text = _k8s_secret_data(text)
    return _remnants(_key_tail(text))


# A JSON string literal on one line: quotes, then plain characters or escapes. The second
# branch takes an unterminated one to the end of its line, so the scan never restarts inside
# it (tried from every quote, 64 KB of \\" took 8.3 s: Cairn's review of #40).
_JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.){0,65536}"|"[^\n]*')
JSON_NEST_MAX = 4


_MARKER = re.compile(r"\[REDACTED[A-Z-]{0,40}\]")


def _note_removed(before: str, after: str) -> None:
    """Note what redaction took out of a decoded JSON value, piece by piece: inside its own
    literal a value can lose its context (response="<hex>" is decoded on its own, and a hex
    token alone is redacted by shape, which the scrubber doesn't record), and it is still a
    known secret where it repeats bare."""
    parts = _MARKER.split(after)
    pos = 0
    for i, part in enumerate(parts):
        at = before.find(part, pos) if part else pos
        if at < 0:
            return  # can't align (a rule rewrote text it kept): note nothing more
        if i:
            _note(before[pos:at])
        pos = at + len(part)
    if len(parts) > 1 and pos < len(before):
        _note(before[pos:])


def _json_strings(text: str, depth: int) -> str:
    """Secrets inside JSON string values: a service account's private_key, a
    ConfigMap holding a file, a tool result wrapping JSON, NDJSON logs. JSON
    escaping turns a value's newlines into \\n and wraps it in quotes, so the
    line-anchored rules never see its lines (16 of the 89 shared fixtures
    leaked from inside a JSON value: Plumb, MindStone #290 round 3). Each
    literal that decodes to a string is redacted as text of its own (its own
    JSON strings first, to JSON_NEST_MAX deep) and put back, re-encoded, only
    if that changed it. Past the depth, or on a literal that doesn't decode,
    the text is left to the rules as it stands. A stray quote earlier on a line (5" long)
    pairs with the next one, so the value after it is left to the rules too."""
    if depth >= JSON_NEST_MAX or '"' not in text:
        return text

    def sub(m: re.Match[str]) -> str:
        lit = m.group(0)
        if len(lit) < 8 or not lit.endswith('"'):  # short, or unterminated
            return lit
        try:
            value = json.loads(lit)
        except ValueError:
            return lit
        if not isinstance(value, str):
            return lit
        norm = value.replace("\r\n", "\n").replace("\r", "\n")
        red = _redact_core(_json_strings(norm, depth + 1))
        if red == norm:
            return lit
        _note_removed(norm, red)
        out = json.dumps(red, ensure_ascii=False)
        try:  # a lone surrogate (\\ud800) decodes raw: keep it escaped, or the text can't be UTF-8
            out.encode("utf-8")
        except UnicodeEncodeError:
            out = json.dumps(red)
        return out

    return _JSON_STRING.sub(sub, text)


def _capturing(repl: str):  # type: ignore[no-untyped-def]
    """An EXTRA replacement that also records what it removed: the part of
    the match that the replacement doesn't keep (between the common prefix
    and suffix of the two)."""

    def sub(m: re.Match[str]) -> str:
        old, new = m.group(0), m.expand(repl)
        i = 0
        while i < len(old) and i < len(new) and old[i] == new[i]:
            i += 1
        j = 0
        while j < len(old) - i and j < len(new) - i and old[-1 - j] == new[-1 - j]:
            j += 1
        _note(old[i:len(old) - j])
        return new

    return sub
