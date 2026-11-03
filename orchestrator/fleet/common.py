"""Shared pieces of the Fleet Phase 0 reporter (synapse#31): config, rate limits, the sender.

Standard library only. Everything here is on a hook's critical path, so nothing is
imported that isn't needed and nothing waits on the network in the hook's own process.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("MS4CC_SYNAPSE_TOML", ORCHESTRATOR_DIR / "config" / "synapse.toml"))
HOME_SYNAPSE = Path(os.environ.get("MS4CC_SYNAPSE_HOME", Path.home() / ".synapse"))
STATE_DIR = HOME_SYNAPSE / "fleet"
HTTP_TIMEOUT_S = 3.0
SUBJECT_MAX = 120


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
    itself (synapse#31 section 1)."""
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
    if "/" in handle or handle.startswith("."):
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


def _state_file(session: str, what: str) -> Path:
    safe = "".join(c for c in session if c.isalnum() or c in "-_")[:128] or "none"
    return STATE_DIR / f"{safe}.{what}"


def rate_ok(cfg: FleetConfig, session: str, what: str, every_s: float) -> bool:
    """True at most once per `every_s` seconds per session and kind; records the send.

    The mark is the file's mtime, so a crashed or killed reporter can't wedge it."""
    path = _state_file(session, what)
    now = time.time()
    try:
        if now - path.stat().st_mtime < every_s:
            return False
    except OSError:
        pass
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (now, now))
    except OSError:
        return False
    return True


def reset_rate(session: str, what: str) -> None:
    try:
        _state_file(session, what).unlink()
    except OSError:
        pass


PERMISSION_MODES = {"default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"}


def remember_mode(session: str, mode: object) -> None:
    """Cache the session's permission mode from a hook input, for the status line to report.

    The status-line JSON has no permission mode; hook inputs do. Only known mode names are
    stored, and only when the value changes."""
    if not isinstance(mode, str) or mode not in PERMISSION_MODES:
        return
    path = _state_file(session, "mode")
    try:
        if path.read_text() == mode:
            return
    except OSError:
        pass
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(mode)
    except OSError:
        pass


def read_mode(session: str) -> str | None:
    try:
        mode = _state_file(session, "mode").read_text().strip()
    except OSError:
        return None
    return mode if mode in PERMISSION_MODES else None


def scrub_subject(v: object) -> str | None:
    """A task subject, secret-scrubbed with the framework's scrubber, then cut to 120 chars.

    Scrubbed before cutting, so a secret straddling the cut can't survive as a fragment."""
    if not isinstance(v, str) or not v:
        return None
    try:
        sys.path.insert(0, str(ORCHESTRATOR_DIR / "hooks"))
        from scrubber import scrub  # type: ignore
        v = scrub(v)
    except Exception:
        return None  # no scrubber, no subject: never send it unscrubbed
    v = " ".join(v.split())
    return v[:SUBJECT_MAX] or None


def post(cfg: FleetConfig, events: list[dict]) -> int | None:
    """POST /v1/fleet/events. Returns the HTTP status, or None on any failure (dropped)."""
    body = json.dumps({"events": events}, separators=(",", ":")).encode()
    req = urllib.request.Request(
        cfg.base_url + "/v1/fleet/events", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def emit(cfg: FleetConfig, events: list[dict]) -> None:
    """Send from a detached grandchild and return at once (synapse#31 section 5).

    The grandchild leaves the session, points fds 0-2 at /dev/null so Claude Code isn't
    held open by a pipe it is reading, posts once with a 3 s timeout, and exits. A failed
    send is dropped: status is ephemeral and the next event refreshes it. If fork isn't
    available the event is dropped rather than sent inline, so a hook never waits on HTTP."""
    if os.environ.get("MS4CC_FLEET_INLINE") == "1":  # tests only: send in-process
        post(cfg, events)
        return
    if not hasattr(os, "fork"):
        return
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
    # first child
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        post(cfg, events)
    finally:
        os._exit(0)
