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
# subjects only, and err towards redacting.
_SECRET_NOUN = re.compile(
    r"(?i)\b(pass(?:word|wd|phrase)?|pwd|secret|token|api[-_ ]?key|key|credentials?|creds)"
    r"((?:\s+(?:is|to|as|of|for\s+\S+\s+(?:is|to)))?\s*[=:]?\s*)(\S+)")
_SUBJECT_RULES = [
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\s*=\s*\S+"),                     # ENV_VAR=value
    re.compile(r"\b[^\s:@/]+:[^\s@/]*[0-9!#$%^&*+=?~][^\s@/]*"),       # user:pass shapes
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),                        # email addresses
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                               # SSN shape
    re.compile(r"\b[0-9a-fA-F]{16,}\b"),                                # long hex
    re.compile(r"(?=[A-Za-z0-9_+/=-]*[A-Za-z])(?=[A-Za-z0-9_+/=-]*[0-9])[A-Za-z0-9_+/=-]{16,}"),  # long mixed tokens
]


def _strict_subject(v: str) -> str:
    v = _SECRET_NOUN.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", v)
    for rule in _SUBJECT_RULES:
        v = rule.sub("[redacted]", v)
    return v


def scrub_subject(v: object, scrubber_dir: Path | None = None) -> str | None:
    """A task subject: the framework scrubber, then the stricter subject rules, then cut to
    120 chars. Scrubbed before cutting, so a secret straddling the cut can't survive as a
    fragment. If the scrubber can't load, the subject is omitted, never sent unscrubbed."""
    if not isinstance(v, str) or not v:
        return None
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


def post(cfg: FleetConfig, events: list[dict]) -> int | None:
    """POST /v1/fleet/events. Returns the HTTP status, or None on any failure (dropped)."""
    body = json.dumps({"events": events}, separators=(",", ":")).encode()
    req = urllib.request.Request(
        cfg.base_url + "/v1/fleet/events", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


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
        signal.alarm(SEND_DEADLINE_S)
    post(cfg, events)


def emit(cfg: FleetConfig, events: list[dict]) -> None:
    detach(lambda: send_now(cfg, events))
