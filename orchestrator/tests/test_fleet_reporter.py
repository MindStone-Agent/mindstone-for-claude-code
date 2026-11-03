"""Fleet Phase 0 reporter (synapse#31): payload allow-list, sampling, status line, latency.

Runs the real scripts as Claude Code would (stdin JSON, a subprocess per hook) against a
fake Synapse on localhost. Hook inputs below are shaped from a real capture (Claude Code
2.1.312, 2026-11-03), with ids and paths replaced.

    cd orchestrator && python -m pytest -q tests/test_fleet_reporter.py
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ORCH = Path(__file__).resolve().parents[1]
REPORT = ORCH / "fleet" / "report.py"
STATUSLINE = ORCH / "fleet" / "statusline.py"
TOKEN = "test-token-abc123"
SID = "11111111-2222-3333-4444-555555555555"
SECRET = "sk-live-SHOULD-NEVER-LEAVE-0123456789abcdef"


class Synapse:
    """A fake POST /v1/fleet/events that records each request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "raw": body.decode(), "json": json.loads(body)})
                self.send_response(202)
                self.end_headers()
                self.wfile.write(b'{"accepted": 1}')

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def events(self) -> list[dict]:
        return [e for r in self.requests for e in r["json"]["events"]]

    def wait(self, n: int, timeout: float = 5.0) -> list[dict]:
        end = time.time() + timeout
        while time.time() < end and len(self.events()) < n:
            time.sleep(0.02)
        return self.events()


@pytest.fixture
def synapse():
    s = Synapse()
    yield s
    s.server.shutdown()


def make_env(tmp_path: Path, url: str, enabled: bool = True, inline: bool = True) -> dict:
    home = tmp_path / "synapse-home"
    home.mkdir(exist_ok=True)
    (home / "plumb.token").write_text(TOKEN + "\n")
    toml = tmp_path / "synapse.toml"
    toml.write_text(f'[synapse]\nbase_url = "{url}"\nhandle = "plumb"\n'
                    + ("[fleet]\nenabled = true\nmachine = \"test-box\"\n" if enabled else ""))
    env = {k: v for k, v in os.environ.items() if not k.startswith("MS4CC_")}
    env.update(MS4CC_SYNAPSE_TOML=str(toml), MS4CC_SYNAPSE_HOME=str(home))
    if inline:
        env["MS4CC_FLEET_INLINE"] = "1"
    return env


def hook(env: dict, inp: dict, script: Path = REPORT, args: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *(args or [])], input=json.dumps(inp).encode(),
                          env=env, capture_output=True, timeout=20)


def base(event: str, **kw) -> dict:
    d = {"session_id": SID, "transcript_path": f"/x/{SECRET}.jsonl", "cwd": "/Users/someone/Projects/net-arbiter",
         "prompt_id": "p-1", "permission_mode": "default", "hook_event_name": event}
    d.update(kw)
    return d


# --- off by default -------------------------------------------------------------------

def test_disabled_sends_nothing(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url, enabled=False)
    r = hook(env, base("SessionStart", source="startup"))
    assert r.returncode == 0 and r.stdout == b""
    time.sleep(0.3)
    assert synapse.requests == []


def test_enabled_but_no_token_sends_nothing(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    (Path(env["MS4CC_SYNAPSE_HOME"]) / "plumb.token").unlink()
    assert hook(env, base("UserPromptSubmit", prompt="hi")).returncode == 0
    time.sleep(0.3)
    assert synapse.requests == []


# --- mapping and the allow-list --------------------------------------------------------

def test_session_started_shape_and_auth(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    r = hook(env, base("SessionStart", source="startup", model="claude-opus-5-5"))
    assert r.returncode == 0 and r.stdout == b"", "SessionStart stdout would be injected into context"
    (ev,) = synapse.wait(1)
    assert synapse.requests[0]["auth"] == f"Bearer {TOKEN}"
    assert synapse.requests[0]["path"] == "/v1/fleet/events"
    assert ev["type"] == "session.started"
    assert ev["machine"] == "test-box" and ev["harness"] == "claude-code" and ev["session_id"] == SID
    assert ev["payload"] == {"cwd_name": "net-arbiter", "model": "claude-opus-5-5", "source": "startup"}
    assert set(ev) == {"type", "ts", "machine", "harness", "session_id", "agent_id", "parent_agent_id", "payload"}


@pytest.mark.parametrize("inp,typ,payload", [
    (base("UserPromptSubmit", prompt=f"my key is {SECRET}"), "turn.started", {}),
    (base("Stop", stop_hook_active=False, last_assistant_message=SECRET, background_tasks=[]), "turn.ended", {}),
    (base("PostToolUse", tool_name="Bash", tool_input={"command": f"echo {SECRET}"},
          tool_response={"stdout": SECRET}, tool_use_id="toolu_1", duration_ms=9), "tool", {"tool_name": "Bash"}),
    (base("SubagentStart", agent_id="a1", agent_type="Explore"), "subagent.started", {"agent_type": "Explore"}),
    (base("SubagentStop", agent_id="a1", agent_type="Explore", last_assistant_message=SECRET,
          agent_transcript_path=f"/x/{SECRET}"), "subagent.stopped", {"agent_type": "Explore"}),
    (base("SessionEnd", reason="prompt_input_exit"), "session.ended", {"reason": "prompt_input_exit"}),
    (base("PreCompact", trigger="auto", custom_instructions=SECRET), "compact", {"phase": "pre"}),
    (base("PostCompact", trigger="auto", compact_summary=SECRET), "compact", {"phase": "post"}),
    (base("Notification", notification_type="idle_prompt", message=f"waiting {SECRET}"), "needs_input", {"kind": "idle_prompt"}),
    (base("Notification", notification_type="permission_prompt", message="Claude needs your permission to use Bash"),
     "needs_input", {"kind": "permission_prompt", "tool_name": "Bash"}),
])
def test_allow_list(tmp_path, synapse, inp, typ, payload):
    env = make_env(tmp_path, synapse.url)
    assert hook(env, inp).returncode == 0
    (ev,) = synapse.wait(1)
    assert ev["type"] == typ and ev["payload"] == payload
    assert SECRET not in synapse.requests[0]["raw"], "a prompt, tool input/output or path left the machine"


def test_subagent_events_carry_its_agent_id(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    hook(env, base("SubagentStart", agent_id="ae50eb41", agent_type="Explore"))
    (ev,) = synapse.wait(1)
    assert ev["agent_id"] == "ae50eb41"


def test_task_subject_is_scrubbed_and_cut(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    subject = "rotate token=" + "a1b2c3d4e5f6" * 4 + " then " + "x" * 300
    hook(env, base("TaskCreated", task_id="7", task_subject=subject, task_description=SECRET))
    (ev,) = synapse.wait(1)
    assert ev["type"] == "task.created" and ev["payload"]["task_id"] == "7"
    s = ev["payload"]["subject"]
    assert len(s) <= 120 and "a1b2c3d4e5f6" not in s and SECRET not in synapse.requests[0]["raw"]
    assert s.startswith("rotate token")


@pytest.mark.parametrize("inp", [
    base("Notification", notification_type="auth_success", message="ok"),
    base("PermissionRequest", tool_name="Bash", tool_input={"command": SECRET}),
    base("SomethingNew", anything=SECRET),
    {"hook_event_name": "UserPromptSubmit", "prompt": "no session id"},
])
def test_unmapped_inputs_send_nothing(tmp_path, synapse, inp):
    env = make_env(tmp_path, synapse.url)
    assert hook(env, inp).returncode == 0
    time.sleep(0.3)
    assert synapse.requests == []


def test_garbage_stdin_is_harmless(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    r = subprocess.run([sys.executable, str(REPORT)], input=b"not json{", env=env, capture_output=True, timeout=10)
    assert r.returncode == 0 and r.stdout == b""


def test_tool_events_sampled_once_per_5s(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    for name in ("Read", "Grep", "Edit"):
        hook(env, base("PostToolUse", tool_name=name, tool_input={}, tool_response={}))
    time.sleep(0.3)
    assert [e["payload"]["tool_name"] for e in synapse.events()] == ["Read"]


# --- status line -------------------------------------------------------------------

STATUS_IN = {
    "session_id": SID, "cwd": "/x", "transcript_path": f"/x/{SECRET}",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "workspace": {"current_dir": "/x", "project_dir": "/x"}, "version": "2.1.312",
    "cost": {"total_cost_usd": 1.234, "total_duration_ms": 5},
    "context_window": {"total_input_tokens": 1, "context_window_size": 1000000, "used_percentage": 42.5,
                       "current_usage": {"input_tokens": 1}},
    "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1}, "seven_day": {"used_percentage": 41.2, "resets_at": 1}},
    "session_name": SECRET, "pr": {"url": SECRET},
}


def test_status_line_reports_allow_listed_fields_and_mode(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    hook(env, base("UserPromptSubmit", prompt="x", permission_mode="acceptEdits"))  # caches the mode
    synapse.wait(1)
    r = hook(env, STATUS_IN, STATUSLINE)
    assert r.returncode == 0 and r.stdout.decode() == "Opus 5.5 · ctx 42% · $1.23"
    ev = synapse.wait(2)[-1]
    assert ev["type"] == "status"
    assert ev["payload"] == {"context_pct": 42.5, "context_window": 1000000, "cost_usd": 1.234,
                             "model": "claude-opus-5-5", "rate_5h_pct": 23.5, "rate_7d_pct": 41.2,
                             "permission_mode": "acceptEdits"}
    assert SECRET not in synapse.requests[-1]["raw"]


def test_status_debounced_10s(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    for _ in range(3):
        hook(env, STATUS_IN, STATUSLINE)
    time.sleep(0.3)
    assert [e["type"] for e in synapse.events()] == ["status"]


def test_status_line_wraps_existing_command_byte_for_byte(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url, enabled=False)
    script = tmp_path / "user sl.sh"
    script.write_text('#!/bin/sh\ninput=$(cat)\nprintf "\\033[1mUSER\\033[0m %s\\nline2" "$(printf %s "$input" | head -c 14)"\n')
    script.chmod(0o755)
    wrap = base64.b64encode(f'"{script}"'.encode()).decode()
    r = hook(env, STATUS_IN, STATUSLINE, ["--wrap-b64", wrap])
    assert r.stdout == b'\x1b[1mUSER\x1b[0m {"session_id":\nline2'


def test_status_line_falls_back_when_wrapped_command_fails(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url, enabled=False)
    wrap = base64.b64encode(b"exit 3").decode()
    assert hook(env, STATUS_IN, STATUSLINE, ["--wrap-b64", wrap]).stdout.decode() == "Opus 5.5 · ctx 42% · $1.23"
    assert hook(env, STATUS_IN, STATUSLINE, ["--wrap-b64", "!!not-base64"]).stdout.decode() == "Opus 5.5 · ctx 42% · $1.23"


def test_status_line_early_session_nulls(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    inp = {"session_id": SID, "model": {"id": "m", "display_name": "M"},
           "context_window": {"used_percentage": None, "context_window_size": 200000}, "cost": {}}
    r = hook(env, inp, STATUSLINE)
    assert r.stdout.decode() == "M"
    ev = synapse.wait(1)[-1]
    assert ev["payload"] == {"context_window": 200000, "model": "m"}


# --- latency with Synapse down (done item 6) ----------------------------------------------

def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _blackhole():
    """Accepts connections and never answers: the worst case for a timeout."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    held = []
    threading.Thread(target=lambda: [held.append(s.accept()) for _ in range(16)], daemon=True).start()
    return s, f"http://127.0.0.1:{s.getsockname()[1]}"


@pytest.mark.parametrize("kind", ["refused", "blackhole"])
def test_hooks_return_fast_when_synapse_is_down(tmp_path, kind):
    if kind == "refused":
        url, keep = f"http://127.0.0.1:{_closed_port()}", None
    else:
        keep, url = _blackhole()
    env = make_env(tmp_path, url, inline=False)  # the real fork path
    worst = 0.0
    for inp, script in [(base("SessionStart", source="startup"), REPORT), (base("UserPromptSubmit", prompt="x"), REPORT),
                        (base("Stop"), REPORT), (STATUS_IN, STATUSLINE)]:
        t = time.perf_counter()
        r = hook(env, inp, script)
        worst = max(worst, time.perf_counter() - t)
        assert r.returncode == 0
    assert worst < 0.2, f"slowest hook took {worst * 1000:.0f} ms with Synapse {kind}"


def test_fork_path_still_delivers(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url, inline=False)
    t = time.perf_counter()
    r = hook(env, base("Stop"))
    assert time.perf_counter() - t < 0.2 and r.stdout == b""
    (ev,) = synapse.wait(1, timeout=5)
    assert ev["type"] == "turn.ended"
