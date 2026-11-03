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
import re

import sys as _sys  # vendored change: the scrubber is hooks/scrubber.py here
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "hooks"))
from scrubber import _propagatable, scrub_collect  # noqa: E402

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
    # ({"Authorization": "Basic ..."}, 'Cookie': 'sid=...'), either quote.
    (re.compile(r"(?i)([\"'](?:proxy-)?authorization[\"'][ \t]{0,8}:[ \t]{0,8}[\"'])(?!\[REDACTED)(?:[^\"'\\\n]|\\.){1,4096}([\"'])"), r"\1" + _R + r"\2"),
    (re.compile(r"(?i)([\"'](?:set-)?cookie[\"'][ \t]{0,8}:[ \t]{0,8}[\"'])(?!\[REDACTED)(?:[^\"'\\\n]|\\.){1,4096}([\"'])"), r"\1" + _R + r"\2"),
    (re.compile(r"(?i)([\"']x-[a-z0-9-]{0,40}(?:key|token|secret|auth)[\"'][ \t]{0,8}:[ \t]{0,8}[\"'])(?!\[REDACTED)(?:[^\"'\\\n]|\\.){1,4096}([\"'])"), r"\1" + _R + r"\2"),
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
            lines[j] = "[REDACTED-PRIVATE-KEY]"
            j -= 1
    return "\n".join(lines)


_CHUNK = 1024
_SEAM = 512
_PROPAGATE_MAX = 64

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----")
# Key material as it appears raw or JSON-escaped (a service-account file).
_PEM_BODY = re.compile(r"(?:[A-Za-z0-9+/=\s]|\\[nrt]){0,16384}")


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
            stop = body.end() if body else m.end()
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


def _scrub_bounded(text: str) -> str:
    """scrub(), kept linear. Private-key blocks go first, on the whole text.
    Then the scrubber runs per chunk, and again on a window of _SEAM either
    side of every cut, so a key and its value on either side of one (a
    'password': at the end of a chunk) are seen together; right to left, so
    the earlier cut positions stay put. Then the found values are removed
    everywhere (_propagate)."""
    text = _pem_blocks(text)
    if len(text) <= _CHUNK:
        clean, found = scrub_collect(text)
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
        lo, hi = max(0, cut - _SEAM), min(len(text), cut + _SEAM)
        clean, values = scrub_collect(text[lo:hi])
        text = text[:lo] + clean + text[hi:]
        found |= values
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
                        tail = m.group(3) if json_block else ""
                        lines[i] = m.group(1) + _R + tail
                elif not stripped.startswith("[REDACTED"):
                    # a block scalar's line, or a value on its own line
                    lines[i] = line[:lead] + _R
                continue
        m = _SECRET_DATA_HDR.match(line)
        if m:
            indent, json_block = len(m.group(1)), m.group(2) is not None
    return "\n".join(lines)


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
        out.append(_secret_doc(doc) if _KIND_SECRET.search(doc) else doc)
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
        return "[REDACTED-KEY-MATERIAL]"
    return s


# Round 4 (Plumb): a hard cut through a token can leave its first half
# beside the marker the second half became, too short for its own rule
# ("ATATT3x..Gf[REDACTED-HIGH-ENTROPY]"). A token-like run of 8+ glued to a
# marker goes too. Anchored at the run's start, so it's one try per run.
# No "=" or ":" in the run, so a key in front of its value ("password=[RED")
# isn't one.
_REMNANT_BEFORE = re.compile(r"(?<![A-Za-z0-9_\-+/])[A-Za-z0-9_\-+/]{8,4096}(?=\[REDACTED)")
_REMNANT_AFTER = re.compile(r"(\[REDACTED[A-Z\-]{0,40}\])[A-Za-z0-9_\-+/]{8,4096}")


def _remnants(text: str) -> str:
    if "[REDACTED" not in text:
        return text
    text = _REMNANT_BEFORE.sub(_R, text)
    return _REMNANT_AFTER.sub(r"\1" + _R, text)


def redact_preview(text: str) -> str:
    """scrub() (bounded, above), then the extras. Irreversible; never raises
    on a str."""
    text = _scrub_bounded(text)
    for pattern, repl in EXTRA:
        text = pattern.sub(repl, text)
    text = _B64_RUN.sub(_long_b64, text)
    text = _k8s_secret_data(text)
    return _remnants(_key_tail(text))
