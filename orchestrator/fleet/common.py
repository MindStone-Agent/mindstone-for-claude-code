"""Shared pieces of the Fleet Phase 0 reporter (synapse#31): config, rate limits, the sender.

Standard library only, POSIX only (the sender forks; without fork every event is dropped).
Everything here is on a hook's critical path, so nothing waits on the network in the hook's
own process.

MS4CC_FLEET_AGENT is set in normal use: by an agent's launch command, to mark its sessions as
the agent's (agent_session). The other MS4CC_* environment variables exist for the test suite,
which points the reporter at a temp config and a fake Synapse; nothing sets them in normal use.
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
import urllib.parse
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
    activity: bool = False  # [fleet] activity = true, AND the transport is safe (see below)


def activity_transport_ok(base_url: str) -> bool:
    """Activity previews (commands, output, replies) go only over https, or to this machine.
    Plain http across a network would put them on the wire in the clear (synapse#31)."""
    try:
        u = urllib.parse.urlsplit(base_url)
    except ValueError:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and (u.hostname or "") in ("localhost", "127.0.0.1", "::1")


def short(v: object, limit: int = 64) -> str | None:
    """A short identifier-like string, or None. Never free text."""
    if not isinstance(v, str) or not v:
        return None
    return v[:limit]


def _read_config() -> dict | None:
    try:
        import tomllib
    except ImportError:
        return None
    try:
        with CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def fleet_enabled() -> bool:
    """`[fleet] enabled = true`, whatever else is set. Bootstrap installs the status line only
    then, and removes it again when this is false."""
    data = _read_config()
    fleet = data.get("fleet") if data else None
    return isinstance(fleet, dict) and fleet.get("enabled") is True


def plain_http_remote(base_url: object) -> bool:
    """True when the token would cross the network unencrypted: http:// to anything but
    localhost. Bootstrap warns about it; the board needs https before production."""
    if not isinstance(base_url, str) or not base_url.lower().startswith("http://"):
        return False
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return True
    return host not in ("localhost", "127.0.0.1", "::1")


def bootstrap_state() -> str:
    """One line for bootstrap.sh: `on`, `on plain-http` or `off`."""
    if not fleet_enabled():
        return "off"
    syn = (_read_config() or {}).get("synapse")
    return "on plain-http" if isinstance(syn, dict) and plain_http_remote(syn.get("base_url")) else "on"


def fleet_config() -> FleetConfig | None:
    """The reporter's config, or None when Fleet is off or can't run.

    Off unless synapse.toml has `[fleet]` with `enabled = true`. The handle comes from the
    `[synapse]` table (the status-line JSON doesn't name the agent) and the token from
    ~/.synapse/<handle>.token, the agent's own existing token: an agent reports only about
    itself (synapse#31 section 1). On a Python without tomllib (3.10) Fleet is simply off."""
    data = _read_config()
    if data is None:
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
    base = base.rstrip("/")
    activity = fleet.get("activity") is True and activity_transport_ok(base)
    return FleetConfig(base_url=base, handle=handle, token=token, machine=machine[:64], activity=activity)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


AGENT_ENV = "MS4CC_FLEET_AGENT"


CLAIMS_KEPT = 50  # claimed ids kept per handle; the oldest go first


def claimed_sessions_file(handle: str) -> Path:
    """The session ids claimed as the agent's (fleet/claim.py), one per line."""
    return HOME_SYNAPSE / "fleet-sessions" / handle


def update_claims(handle: str, change) -> list[str]:
    """Read the claimed ids, change them, write them back, all under an exclusive lock, so a
    claim and a release at the same time can't undo each other. Returns the new list."""
    path = claimed_sessions_file(handle)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(path.parent / f".{handle}.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ids = path.read_text(encoding="utf-8").split()
        except OSError:
            ids = []
        new = change(list(ids))[-CLAIMS_KEPT:]
        if new != ids:
            tmp = path.with_name(f".{handle}.{os.getpid()}")
            fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(i + "\n" for i in new))
            os.replace(tmp, path)
        return new
    finally:
        os.close(lock)


def release_claim(cfg: FleetConfig, session: str) -> None:
    """SessionEnd: a claim ends with its session. `claude --continue` reuses the id, and a
    person resuming the agent's conversation in their own terminal isn't the agent."""
    if os.environ.get(AGENT_ENV) is not None:
        return
    try:
        update_claims(cfg.handle, lambda ids: [i for i in ids if i != session])
    except OSError:
        pass


def agent_session(cfg: FleetConfig, session: object) -> bool:
    """Whether this Claude Code session is the agent's own. Only those report (Clint,
    2026-11-04: "Agent sessions only"); every other session on the machine shares the same
    settings.json and hooks, and sends nothing. Default deny, decided without the network:
    - MS4CC_FLEET_AGENT set (by the agent's launch command): the session is the agent's if it
      names this config's handle, and refused if it names another;
    - otherwise only a session id claimed with fleet/claim.py, listed in
      ~/.synapse/fleet-sessions/<handle>. A new id (/clear, a fork) isn't claimed by the old."""
    marked = os.environ.get(AGENT_ENV)
    if marked is not None:
        return marked == cfg.handle
    if not isinstance(session, str) or not session or safe_session(session) != session:
        return False
    try:
        return session in claimed_sessions_file(cfg.handle).read_text(encoding="utf-8").split()
    except OSError:
        return False


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


LOCK_WAIT_S = 0.5  # the status line runs this on Claude Code's render path: never wait long


def _lock(fd: int, wait_s: float = LOCK_WAIT_S) -> bool:
    """An exclusive flock, given up after `wait_s` (a suspended holder, a slow network home)."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        except OSError:
            return False


def rate_ok(session: str, what: str, every_s: float) -> bool:
    """True at most once per `every_s` seconds per session and kind; records the send.

    Check and mark happen under an exclusive lock, so parallel hooks (a burst of tool calls)
    can't all pass. The mark is the file's mtime, so a killed reporter can't wedge it. A lock
    that can't be had within LOCK_WAIT_S counts as not ok: the event waits or is dropped."""
    if not _ensure_state_dir():
        return False
    path = _state_file(session, what)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        if not _lock(fd):
            return False
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
# its value, credentials, keys, emails) and leave most ordinary words, times, file:line
# references, branch names and identifiers alone. They over-redact a little by design (the
# word after "token to", "password as", "key for X to/is") and a secret written as plain
# prose can still get through.
_SECRET_WORD = re.compile(
    r"(?i)\b(pass(?:word|wd|phrase)?|pw|pwd|pin|secret|token|api[-_ ]?key|key|credentials?|creds|bearer)\b"
    r"(\s+for\s+\S+\s+(?:is|was|to)\b\s*[:=]?\s*|\s*(?:(?:is|was|to|as)\b\s*[:=]?|[:=])\s*|\s+)(\S+)")
_CONNECTOR = re.compile(r"(?i)(?:\s+for\s+\S+\s+(?:is|was|to)\s*[:=]?|\s*(?:(?:is|was|to|as)\s*[:=]?|[:=]))\s*$")
_SECRETISH = re.compile(r"[0-9!@#$%^&*+=?~]")
_SUBJECT_RULES = [
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}=\S+"),                                   # ENV_VAR=value
    re.compile(r"(?<![\w.])[A-Za-z][\w.-]*:(?=[^\s/\[\]]*[A-Za-z])(?=[^\s/\[\]]*[0-9!#$%^&*+=?~])[^\s/\[\]]{4,}"),  # user:pass
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),                                # email addresses
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                                       # SSN shape
    re.compile(r"\b[0-9a-fA-F]{16,}\b"),                                       # long hex
]
_WORD = re.compile(r"[A-Za-z0-9_+=-]{8,}")  # no "/": a path's parts are judged one by one


def _keyish(w: str) -> bool:
    """A word that looks like a credential rather than an identifier: 16+ chars with three
    or more digits, or upper, lower and digits mixed with any of: 20+ chars, two digit runs,
    a trailing digit run (Welcome123), or digits inside lower case (Sup3rS3cret). OAuth2Client,
    Base64Encoder and S3Bucket stay. A mixed-case word of 8+ chars ending in digits is redacted,
    which includes version-suffixed names (Python312) and camelCase names such as fromBase64,
    handleV2 and LoginForm2: over-redaction, chosen over letting Welcome123 through."""
    digits = sum(ch.isdigit() for ch in w)
    if len(w) >= 16 and digits >= 3:
        return True
    mixed = digits > 0 and any(ch.islower() for ch in w) and any(ch.isupper() for ch in w)
    return mixed and (len(w) >= 20 or len(re.findall(r"[0-9]+", w)) >= 2 or w[-1].isdigit()
                      or re.search(r"[a-z][0-9]+[a-z]|[0-9][a-z]+[0-9]", w) is not None)


def _strict_subject(v: str) -> str:
    pos = 0  # a word left as it is must not swallow the secret word after it ("secret key is x")
    while (m := _SECRET_WORD.search(v, pos)):
        if _CONNECTOR.match(m.group(2)) or _SECRETISH.search(m.group(3)):
            v = v[:m.start(3)] + "[redacted]" + v[m.end(3):]
            pos = m.start(3) + len("[redacted]")
        else:
            pos = m.end(1)
    for rule in _SUBJECT_RULES:
        v = rule.sub("[redacted]", v)
    return _WORD.sub(lambda m: "[redacted]" if _keyish(m.group(0)) else m.group(0), v)


SUBJECT_SCAN_MAX = 512  # only 120 chars are sent; scanning more only costs time (regex cost)


def scrub_subject(v: object, scrubber_dir: Path | None = None) -> str | None:
    """A task subject: cut to 512 chars, the preview redaction (fleet/redact.py), the stricter subject rules,
    then cut to 120 chars. Scrubbed before the final cut, so a secret straddling it can't
    survive as a fragment. If the scrubber can't load, the subject is omitted, never sent
    unscrubbed."""
    if not isinstance(v, str) or not v:
        return None
    if len(v) > SUBJECT_SCAN_MAX:  # drop the word the cut split, so no fragment of it survives
        parts = v[:SUBJECT_SCAN_MAX].rsplit(None, 1)
        v = parts[0] if len(parts) == 2 else ""  # one unbroken run: nothing safe to keep
        if not v.strip():
            return None
    try:
        if not ((scrubber_dir or ORCHESTRATOR_DIR / "hooks") / "scrubber.py").is_file():
            return None
        # the same redaction as every other board-visible field (the server's, vendored, plus
        # the reporter's), before the subject is flattened onto one line: the framework
        # scrubber alone let 9 of the shared fixtures through (Cairn's #145 review, S2-2)
        from fleet.redact import clean
        v = clean(v)
    except Exception:
        return None
    v = " ".join(_strict_subject(v).split())
    return v[:SUBJECT_MAX] or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would re-send the bearer token to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
# To this machine, never through a proxy: an http_proxy env var or a macOS system proxy
# without a localhost exception would otherwise carry plain-http activity off the box.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _loopback(base_url: str) -> bool:
    try:
        return (urllib.parse.urlsplit(base_url).hostname or "") in ("localhost", "127.0.0.1", "::1")
    except ValueError:
        return False


def body_bytes(obj: object) -> bytes:
    """The wire form: UTF-8 JSON, non-ASCII unescaped (a CJK char is 3 bytes, not 6)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")


RETRY_AFTER_MAX_S = 2.0


def _post_once(cfg: FleetConfig, body: bytes) -> tuple[int | None, float | None]:
    req = urllib.request.Request(
        cfg.base_url + "/v1/fleet/events", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"})
    try:
        opener = _LOCAL_OPENER if _loopback(cfg.base_url) else _OPENER
        with opener.open(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        try:
            wait = float(e.headers.get("Retry-After", "1"))
        except (TypeError, ValueError):
            wait = 1.0
        if wait != wait:  # NaN
            wait = 1.0
        return e.code, wait
    except Exception:
        return None, None


def post(cfg: FleetConfig, events: list[dict]) -> int | None:
    """POST /v1/fleet/events. Returns the HTTP status, or None on any failure (dropped).

    A 429 is retried once, after its Retry-After (at most 2 s), then dropped (synapse#31)."""
    body = body_bytes({"events": events})
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


BATCH_MAX_BYTES = 60_000  # the server takes 64 KB and 100 events per request


def batches(events: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = [[]]
    size = 0
    for ev in events:
        n = len(body_bytes(ev)) + 1
        if out[-1] and (size + n > BATCH_MAX_BYTES or len(out[-1]) >= 100):
            out.append([])
            size = 0
        out[-1].append(ev)
        size += n
    return [b for b in out if b]


def emit(cfg: FleetConfig, events: list[dict]) -> None:
    detach(lambda: send_now(cfg, events))


def emit_later(cfg: FleetConfig, events: list[dict], make_more) -> None:
    """Send `events`, then build more in the detached grandchild (a transcript tail can read
    megabytes) and send those, in batches that fit the server's limits, inside one deadline.
    The events go first, so a tail that fails or runs out of time can't take them with it."""
    def run() -> None:
        if os.environ.get("MS4CC_FLEET_INLINE") != "1":
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(SEND_DEADLINE_S * 2)
        for batch in batches(events):
            post(cfg, batch)
        try:
            more = make_more()
        except Exception:
            return
        for batch in batches(more):
            post(cfg, batch)
    detach(run)
