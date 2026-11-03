"""Shared pieces of the Fleet Phase 0 reporter (synapse#31): config, rate limits, the sender.

Standard library only, POSIX only (the sender forks; without fork every event is dropped).
Everything here is on a hook's critical path, so nothing waits on the network in the hook's
own process.

The three MS4CC_* environment variables exist for the test suite, which points the reporter
at a temp config and a fake Synapse; nothing sets them in normal use.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("MS4CC_SYNAPSE_TOML", ORCHESTRATOR_DIR / "config" / "synapse.toml"))
HOME_SYNAPSE = Path(os.environ.get("MS4CC_SYNAPSE_HOME", Path.home() / ".synapse"))
STATE_DIR = HOME_SYNAPSE / "fleet"
HTTP_TIMEOUT_S = 3.0      # per socket operation (synapse#31 section 5)
SEND_DEADLINE_S = 5       # the whole send, DNS and a dripping server included
SUBJECT_MAX = 120
STATE_MAX_AGE_S = 14 * 86400


@dataclass(frozen=True)
class FleetConfig:
    base_url: str
    handle: str
    token: str
    machine: str


def fleet_config() -> FleetConfig | None:
    """The reporter's config, or None when Fleet is off or can't run.

    Off unless synapse.toml has `[fleet]` with `enabled = true`. The handle comes from the
    `[synapse]` table (the status-line JSON doesn't name the agent) and the token from
    ~/.synapse/<handle>.token, the agent's own existing token: an agent reports only about
    itself (synapse#31 section 1). On a Python without tomllib (3.10) Fleet is simply off."""
    try:
        import tomllib
    except ImportError:
        return None
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    fleet = data.get("fleet")
    syn = data.get("synapse")
    if not isinstance(fleet, dict) or fleet.get("enabled") is not True or not isinstance(syn, dict):
        return None
    base, handle = syn.get("base_url"), syn.get("handle")
    if not isinstance(base, str) or not base or not isinstance(handle, str) or not handle:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", handle):
        return None
    try:
        token = (HOME_SYNAPSE / f"{handle}.token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    machine = fleet.get("machine")
    if not isinstance(machine, str) or not machine:
        machine = socket.gethostname().split(".")[0]
    return FleetConfig(base_url=base.rstrip("/"), handle=handle, token=token, machine=machine[:64])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_session(session: str) -> str:
    """A session id as a file-name component: no separators, no dots, at most 128 chars."""
    return "".join(c for c in session if c.isalnum() or c in "-_")[:128] or "none"


def _state_file(session: str, what: str) -> Path:
    return STATE_DIR / f"{safe_session(session)}.{what}"


def _ensure_state_dir() -> bool:
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def rate_ok(session: str, what: str, every_s: float) -> bool:
    """True at most once per `every_s` seconds per session and kind; records the send.

    Check and mark happen under an exclusive lock, so parallel hooks (a burst of tool calls)
    can't all pass. The mark is the file's mtime, so a killed reporter can't wedge it."""
    if not _ensure_state_dir():
        return False
    path = _state_file(session, what)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        now = time.time()
        st = os.fstat(fd)
        if st.st_size and now - st.st_mtime < every_s:
            return False
        os.ftruncate(fd, 0)
        os.write(fd, b"1")
        os.utime(path, (now, now))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)  # releases the lock


def rate_remaining(session: str, what: str, every_s: float) -> float:
    try:
        return max(0.0, every_s - (time.time() - _state_file(session, what).stat().st_mtime))
    except OSError:
        return 0.0


def mark_sent(session: str, ts: str) -> None:
    """Record the ts of the newest status sent, so a held snapshot never overwrites it."""
    if _ensure_state_dir():
        try:
            _state_file(session, "status.sent").write_text(ts)
        except OSError:
            pass


def last_sent(session: str) -> str:
    try:
        return _state_file(session, "status.sent").read_text().strip()
    except OSError:
        return ""


def reset_rate(session: str, what: str) -> None:
    try:
        _state_file(session, what).unlink()
    except OSError:
        pass


PERMISSION_MODES = {"default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"}


def remember_mode(session: str, mode: object) -> None:
    """Cache the session's permission mode from a hook input, for the status line to report.

    The status-line JSON has no permission mode; hook inputs do. So a mode change shows on
    the board from the next hook event, not the instant it happens. Only known mode names are
    stored, and only when the value changes."""
    if not isinstance(mode, str) or mode not in PERMISSION_MODES:
        return
    path = _state_file(session, "mode")
    try:
        if path.read_text() == mode:
            return
    except OSError:
        pass
    if _ensure_state_dir():
        try:
            path.write_text(mode)
        except OSError:
            pass


def read_mode(session: str) -> str | None:
    try:
        mode = _state_file(session, "mode").read_text().strip()
    except OSError:
        return None
    return mode if mode in PERMISSION_MODES else None


def prune_state(max_age_s: float = STATE_MAX_AGE_S) -> None:
    """Delete per-session state files untouched for 14 days. Run at SessionStart."""
    try:
        cutoff = time.time() - max_age_s
        for p in STATE_DIR.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


# A task subject is the one model-written string the reporter sends, and the framework's
# scrubber is tuned for transcripts, where a miss stays local. These run after it, on
# subjects only. They are heuristics: they catch the common shapes (a secret word followed by
# its value, credentials, keys, emails) and leave ordinary words, times, file:line references
# and branch names alone. A secret written as plain prose can still get through.
_SECRET_WORD = re.compile(
    r"(?i)\b(pass(?:word|wd|phrase)?|pw|pwd|pin|secret|token|api[-_ ]?key|key|credentials?|creds|bearer)\b"
    r"(\s*(?:is|was|to|as|=|:)\s*|\s+)(\S+)")
_CONNECTOR = re.compile(r"(?i)\s*(?:is|was|to|as|=|:)\s*$")
_SECRETISH = re.compile(r"[0-9!@#$%^&*+=?~]")
_SUBJECT_RULES = [
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}=\S+"),                                   # ENV_VAR=value
    re.compile(r"(?<![\w.])[A-Za-z][\w.-]*:(?=[^\s/\[\]]*[A-Za-z])(?=[^\s/\[\]]*[0-9!#$%^&*+=?~])[^\s/\[\]]{4,}"),  # user:pass
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),                                # email addresses
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                                       # SSN shape
    re.compile(r"\b[0-9a-fA-F]{16,}\b"),                                       # long hex
]
_WORD = re.compile(r"[A-Za-z0-9_+/=-]{8,}")


def _keyish(w: str) -> bool:
    """A word that looks like a credential rather than an identifier: 16+ chars with three
    or more digits, or 8+ chars mixing upper case, lower case and digits."""
    digits = sum(ch.isdigit() for ch in w)
    if len(w) >= 16 and digits >= 3:
        return True
    return digits > 0 and any(ch.islower() for ch in w) and any(ch.isupper() for ch in w)


def _strict_subject(v: str) -> str:
    def word(m: re.Match) -> str:
        value = m.group(3)
        if m.group(1).lower() == "bearer" or _CONNECTOR.match(m.group(2)) or _SECRETISH.search(value):
            return f"{m.group(1)}{m.group(2)}[redacted]"
        return m.group(0)
    v = _SECRET_WORD.sub(word, v)
    for rule in _SUBJECT_RULES:
        v = rule.sub("[redacted]", v)
    return _WORD.sub(lambda m: "[redacted]" if _keyish(m.group(0)) else m.group(0), v)


SUBJECT_SCAN_MAX = 512  # only 120 chars are sent; scanning more only costs time (regex cost)


def scrub_subject(v: object, scrubber_dir: Path | None = None) -> str | None:
    """A task subject: cut to 512 chars, the framework scrubber, the stricter subject rules,
    then cut to 120 chars. Scrubbed before the final cut, so a secret straddling it can't
    survive as a fragment. If the scrubber can't load, the subject is omitted, never sent
    unscrubbed."""
    if not isinstance(v, str) or not v:
        return None
    v = v[:SUBJECT_SCAN_MAX]
    try:
        sys.path.insert(0, str(scrubber_dir or ORCHESTRATOR_DIR / "hooks"))
        from scrubber import scrub  # type: ignore
        v = scrub(v)
    except Exception:
        return None
    v = " ".join(_strict_subject(v).split())
    return v[:SUBJECT_MAX] or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would re-send the bearer token to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


RETRY_AFTER_MAX_S = 2.0


def _post_once(cfg: FleetConfig, body: bytes) -> tuple[int | None, float | None]:
    req = urllib.request.Request(
        cfg.base_url + "/v1/fleet/events", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        try:
            wait = float(e.headers.get("Retry-After", "1"))
        except (TypeError, ValueError):
            wait = 1.0
        return e.code, wait
    except Exception:
        return None, None


def post(cfg: FleetConfig, events: list[dict]) -> int | None:
    """POST /v1/fleet/events. Returns the HTTP status, or None on any failure (dropped).

    A 429 is retried once, after its Retry-After (at most 2 s), then dropped (synapse#31)."""
    body = json.dumps({"events": events}, separators=(",", ":")).encode()
    status, wait = _post_once(cfg, body)
    if status == 429:
        time.sleep(min(max(wait or 0.0, 0.0), RETRY_AFTER_MAX_S))
        status, _ = _post_once(cfg, body)
    return status


def detach(fn) -> None:
    """Run fn() in a detached grandchild and return at once (synapse#31 section 5).

    The grandchild leaves the session and points fds 0-2 at /dev/null, so Claude Code isn't
    held open by a pipe it is reading. Without fork, nothing runs: a hook never waits on the
    network."""
    if os.environ.get("MS4CC_FLEET_INLINE") == "1":  # tests only
        fn()
        return
    if not hasattr(os, "fork"):
        return
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    try:
        pid = os.fork()
    except OSError:
        return
    if pid:
        try:
            os.waitpid(pid, 0)  # the first child exits immediately
        except OSError:
            pass
        return
    try:  # first child
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        fn()
    finally:
        os._exit(0)


def send_now(cfg: FleetConfig, events: list[dict]) -> None:
    """The body of a detached send: SIGALRM ends it after SEND_DEADLINE_S, whatever it is
    waiting on, so a sick Synapse or proxy can't pile up processes."""
    if os.environ.get("MS4CC_FLEET_INLINE") != "1":
        signal.signal(signal.SIGALRM, signal.SIG_DFL)  # an inherited "ignore" would disarm it
        signal.alarm(SEND_DEADLINE_S)
    post(cfg, events)


def emit(cfg: FleetConfig, events: list[dict]) -> None:
    detach(lambda: send_now(cfg, events))
