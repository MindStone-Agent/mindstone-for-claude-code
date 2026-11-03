"""Redaction for activity previews (synapse#31, activity layer): commands, file paths, tool
output, the agent's replies, subagent descriptions.

Two passes, in this order, both bounded:
1. `hooks/scrubber.py` `scrub()`, the framework's secret scrubber (keys, tokens, Bearer values,
   URL userinfo, CLI password flags, private-key blocks, key=value secrets, high-entropy
   tokens). The Synapse server vendors the same file, pinned by sha256, and runs it again.
2. `redact()` below: two shapes `scrub()` leaves alone, measured on 2026-11-03:
   - the value of any `Authorization:` / `Proxy-Authorization:` header, whatever the scheme
     (`scrub()` only handles Bearer values that contain a digit);
   - `.env`-style `NAME=value` where a whole `_`-separated part of NAME is SECRET, TOKEN, KEY,
     APIKEY, PASS, PASSWD, PASSWORD, PASSPHRASE, PWD or CREDENTIAL(S), at any length (`scrub()`
     has a length floor, so `MY_SECRET_TOKEN=zz9` passes it). KEYBOARD=1 is left alone.

`preview()` cuts long text to a head and a tail. Scrubbing happens before the cut, and a word
split by any cut is dropped, so no fragment of a secret survives at an edge.
If the scrubber can't be loaded, `preview()` returns None: nothing is sent unscrubbed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]
SCAN_WINDOW = 128 * 1024  # the most text scrubbed per end; beyond it, only head and tail matter

_AUTH_HEADER = re.compile(r"(?im)^([ \t]*(?:proxy-)?authorization[ \t]*:[ \t]*)(\S[^\r\n]{0,4096})")
_AUTH_INLINE = re.compile(r"(?i)((?:-H|--header)[ \t]+[\"']?(?:proxy-)?authorization[ \t]*:[ \t]*)([^\"'\r\n]{1,4096})")
_ENV_SECRET = re.compile(  # the secret word is a whole _-separated part: API_KEY yes, KEYBOARD no
    r"(?i)(?<![A-Za-z0-9_])((?:[A-Z0-9]+_)*(?:SECRETS?|TOKENS?|KEYS?|APIKEY|PASS|PASSWD|PASSWORD|PASSPHRASE|PWD|CREDENTIALS?)"
    r"(?:_[A-Z0-9]+)*[ \t]*=[ \t]*)(?![ \t]*\[REDACTED)"
    r"(\"[^\"\r\n]{0,4096}\"|'[^'\r\n]{0,4096}'|[^\s\"']{1,4096})")

_scrub = None


def _scrubber():
    global _scrub
    if _scrub is None:
        sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))
        from scrubber import scrub  # type: ignore
        _scrub = scrub
    return _scrub


def redact(text: str) -> str:
    """The two extra rules, after scrub(). Exposed for the server's shared fixtures."""
    text = _AUTH_HEADER.sub(r"\1[REDACTED-AUTH]", text)
    text = _AUTH_INLINE.sub(r"\1[REDACTED-AUTH]", text)
    return _ENV_SECRET.sub(r"\1[REDACTED-SECRET]", text)


def clean(text: str) -> str:
    """scrub() then redact(). Raises if the scrubber can't load; callers treat that as 'omit'."""
    return redact(_scrubber()(text))


def _drop_split_head(s: str) -> str:
    """s was cut at its END: drop the last, possibly split, word."""
    i = max(s.rfind(" "), s.rfind("\n"), s.rfind("\t"))
    return s[: i + 1] if i >= 0 else ""


def _drop_split_tail(s: str) -> str:
    """s was cut at its START: drop the first, possibly split, word."""
    for i, ch in enumerate(s):
        if ch in " \n\t":
            return s[i:]
    return ""


def preview(text: object, limit: int) -> tuple[str | None, bool]:
    """(redacted text of at most `limit` chars, truncated?). Long text keeps its head and its
    tail with a marker between them. None when there's nothing to send or no scrubber."""
    if not isinstance(text, str) or not text:
        return None, False
    try:
        if len(text) <= 2 * SCAN_WINDOW:
            text = clean(text)
        else:
            head = clean(_drop_split_head(text[:SCAN_WINDOW]))
            tail = clean(_drop_split_tail(text[-SCAN_WINDOW:]))
            text = head + f"\n… [{len(text) - 2 * SCAN_WINDOW:,} chars not scanned] …\n" + tail
    except Exception:
        return None, False
    if len(text) <= limit:
        return text, False
    marker = f"\n… [{len(text):,} chars, cut to {limit:,}] …\n"
    half = max(0, (limit - len(marker)) // 2)
    head = _drop_split_head(text[:half])
    tail = _drop_split_tail(text[len(text) - half:]) if half else ""
    return (head + marker + tail)[:limit], True
