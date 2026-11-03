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
SECRET = "CANARY_SHOULD_NEVER_LEAVE_7f3a"


class Synapse:
    """A fake POST /v1/fleet/events that records each request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.latency = 0.0        # seconds before answering
        self.statuses: list[int] = []  # scripted response codes, consumed in order; then 202
        self.retry_after = "1"
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                time.sleep(outer.latency)
                code = outer.statuses.pop(0) if outer.statuses else 202
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "raw": body.decode(), "json": json.loads(body), "code": code})
                self.send_response(code)
                if code == 429:
                    self.send_header("Retry-After", outer.retry_after)
                self.end_headers()
                self.wfile.write(b'{"accepted": 1}' if code == 202 else b'{}')

            def do_GET(self):  # noqa: N802 - a followed redirect arrives as a GET
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "raw": "", "json": {"events": []}})
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def events(self) -> list[dict]:
        """Events the server accepted (a 429'd attempt isn't one)."""
        return [e for r in self.requests if r.get("code", 202) == 202 for e in r["json"]["events"]]

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


@pytest.mark.parametrize("fleet_table", ["[fleet]\nenabled = false\n", '[fleet]\nenabled = "true"\n', "[fleet]\nmachine = \"x\"\n"])
def test_fleet_table_without_enabled_true_sends_nothing(tmp_path, synapse, fleet_table):
    env = make_env(tmp_path, synapse.url, enabled=False)
    toml = Path(env["MS4CC_SYNAPSE_TOML"])
    toml.write_text(toml.read_text() + fleet_table)
    assert hook(env, base("SessionStart", source="startup")).returncode == 0
    assert hook(env, STATUS_IN, STATUSLINE).returncode == 0
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
    (base("Notification", notification_type="permission_prompt", message=f"Claude needs your permission {SECRET}"),
     "needs_input", {"kind": "permission_prompt"}),
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


def test_untyped_subagent_stop_is_not_reported(tmp_path, synapse):
    """Claude Code's internal agents fire SubagentStop with no agent_type and no start."""
    env = make_env(tmp_path, synapse.url)
    hook(env, base("SubagentStop", agent_id="a20914a8", stop_hook_active=False, last_assistant_message="x"))
    time.sleep(0.3)
    assert synapse.requests == []


def test_first_tool_after_needs_input_is_never_sampled_out(tmp_path, synapse):
    """'Needs you' clears on the next tool event, so that event must not be swallowed."""
    env = make_env(tmp_path, synapse.url)
    hook(env, base("PostToolUse", tool_name="Read", tool_input={}, tool_response={}))
    hook(env, base("Notification", notification_type="permission_prompt", message="x"))
    hook(env, base("PostToolUse", tool_name="Bash", tool_input={}, tool_response={}))
    evs = synapse.wait(3)
    assert [e["type"] for e in evs] == ["tool", "needs_input", "tool"]
    assert evs[2]["payload"] == {"tool_name": "Bash"}


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
    base("Notification", notification_type="idle_prompt", message="Claude is waiting for your input"),  # plain idle
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
    "cost": {"total_cost_usd": 1.23400000001, "total_duration_ms": 5},
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


def test_status_debounced_10s_and_the_last_update_is_kept(tmp_path, synapse):
    """At most one status per 10 s, and an update inside the window is sent when it ends."""
    env = make_env(tmp_path, synapse.url, inline=False)
    for pct in (10, 20, 30):
        inp = json.loads(json.dumps(STATUS_IN))
        inp["context_window"]["used_percentage"] = pct
        assert hook(env, inp, STATUSLINE).returncode == 0
    time.sleep(1.0)
    assert [e["payload"]["context_pct"] for e in synapse.events()] == [10]
    evs = synapse.wait(2, timeout=14)
    assert [e["payload"]["context_pct"] for e in evs] == [10, 30], "the newest suppressed snapshot is sent at window end"
    time.sleep(1.0)
    assert len(synapse.events()) == 2, "exactly one trailing send"


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
    r = hook(env, STATUS_IN, STATUSLINE, ["--wrap-b64", base64.b64encode(b"printf partial; exit 3").decode()])
    assert (r.stdout, r.returncode) == (b"partial", 3), "the wrapped exit code passes through, as before the wrap"
    assert hook(env, STATUS_IN, STATUSLINE, ["--wrap-b64", "!!not-base64"]).stdout.decode() == "Opus 5.5 · ctx 42% · $1.23"


def test_status_line_passes_non_utf8_bytes_and_survives_a_latin1_locale(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url, enabled=False)
    env.update(LC_ALL="en_US.ISO8859-1", LANG="en_US.ISO8859-1")
    env.pop("PYTHONIOENCODING", None)
    wrap = base64.b64encode(b"printf 'caf\\351 \\342\\234\\223'").decode()
    assert hook(env, STATUS_IN, STATUSLINE, ["--wrap-b64", wrap]).stdout == b"caf\xe9 \xe2\x9c\x93"
    assert hook(env, STATUS_IN, STATUSLINE).stdout.decode() == "Opus 5.5 · ctx 42% · $1.23"


def test_status_line_shows_the_users_bar_even_if_fleet_cannot_load(tmp_path, synapse):
    """Without fleet/common.py (or tomllib, on 3.10) the wrapped bar must still show."""
    lone = tmp_path / "orch" / "fleet"
    lone.mkdir(parents=True)
    (lone / "statusline.py").write_text(STATUSLINE.read_text())
    env = make_env(tmp_path, synapse.url)
    wrap = base64.b64encode(b"printf MY-BAR").decode()
    r = hook(env, STATUS_IN, lone / "statusline.py", ["--wrap-b64", wrap])
    assert (r.stdout, r.returncode) == (b"MY-BAR", 0)
    old = Path("/usr/bin/python3")
    if old.exists() and subprocess.run([str(old), "-c", "import tomllib"], capture_output=True).returncode != 0:
        r = subprocess.run([str(old), str(STATUSLINE), "--wrap-b64", wrap], input=json.dumps(STATUS_IN).encode(),
                           env=env, capture_output=True, timeout=20)
        assert (r.stdout, r.returncode) == (b"MY-BAR", 0), "a Python without tomllib still shows the bar"


def test_status_line_early_session_nulls(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    inp = {"session_id": SID, "model": {"id": "m", "display_name": "M"},
           "context_window": {"used_percentage": None, "context_window_size": 200000}, "cost": {}}
    r = hook(env, inp, STATUSLINE)
    assert r.stdout.decode() == "M"
    ev = synapse.wait(1)[-1]
    assert ev["payload"] == {"context_window": 200000, "model": "m"}


# --- review round 1 regressions -------------------------------------------------------

def test_redirect_is_never_followed_so_the_token_stays_home(tmp_path, synapse):
    elsewhere = synapse  # records anything that reaches it
    class R(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", elsewhere.url + "/steal")
            self.end_headers()
        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = make_env(tmp_path, f"http://127.0.0.1:{srv.server_address[1]}")
        assert hook(env, base("Stop")).returncode == 0
        time.sleep(0.5)
        assert elsewhere.requests == []
    finally:
        srv.shutdown()


@pytest.mark.parametrize("subject", [
    "Rotate the DB password hunter2 on prod",
    "Use password Tr0ub4dor&3 for staging login",
    "Set API key to 4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c",
    "Login as admin:S3cr3tP@ss to the router",
    "export OPENAI_API_KEY=sk-abc",
    "mail jane.doe@example.com about it",
    "SSN 123-45-6789 on the form",
    "token ghp9aZ8bY7cX6dW5eV4fU3",
    "pw: hunter2",
    "PIN 4821",
    "login admin / Sup3rS3cret!",
    "Bearer abc123def456ghi",
    "the passphrase is hunter2 horse",
    "admin password is: hunter2",
    "secret key is s3cr3t",
    "the secret token is hunter2",
    "api token secret: hunter2",
    "the password for root is hunter2",
    "set the password for admin to Hunter2",
    "token=" + "x" * 495 + " AKIAIOSFODNN7EXAMPLE",
    "run deploy --password hunter now",   # only the framework scrubber catches these two:
    "mysql -u root -phunterpw db",        # they prove scrub() actually runs
    "Seed test user alice / Welcome123",
    "Use admin/Summer2024 on staging",
    "Log in as root with Password1",
    "Use invite code Xq7Lp2Mz9Rw4",
    "Connect with mQ4xR7tY2wE9 to the sandbox",
    "admin login Hz8Kq4Lw",
])
def test_prose_secrets_in_task_subjects_are_redacted(tmp_path, synapse, subject):
    env = make_env(tmp_path, synapse.url)
    hook(env, base("TaskCreated", task_id="1", task_subject=subject))
    (ev,) = synapse.wait(1)
    sent = ev["payload"].get("subject", "")
    for secret in ("hunter2", "Tr0ub4dor", "4f9a8b7c6d5e", "S3cr3tP", "@ss", "sk-abc", "jane.doe", "123-45-6789",
                   "ghp9aZ8bY7", "4821", "Sup3rS3cret", "abc123def", "s3cr3t", "Hunter2", "AKIAIOSF", "hunter", "Welcome123", "Summer2024", "Password1", "Xq7Lp2",
                   "mQ4xR7", "Hz8Kq4"):
        assert secret not in sent, f"{secret!r} left the machine in {sent!r}"


@pytest.mark.parametrize("subject", [
    "Write the PR body for the fleet reporter", "Update keyboard shortcuts", "Fix key handling in the parser",
    "Update password reset flow", "Add secret scanning to CI", "token redirect bug", "Meeting at 10:30",
    "Fix README.md:42 typo", "Rebase feature/fleet-reporter-v2", "Investigate test_fleet_reporter2 flake",
    "Set MAX_RETRIES = 3", "Bump Python to 3.14 in CI", "Review PR #142 and #143", "see http://host:8080/path",
    "Fix token issue in login", "Fix key tooltip", "Rotate key asap", "Handle Bearer tokens in middleware",
    "Refactor OAuth2Client", "Add Http2Server", "Update Base64Encoder for UTF8", "Fix src/components/NavBar2.tsx",
    "Add S3Bucket policy", "Fix the auth token refresh in api/client.py", "Add PIN entry screen",
    "Rotate keys after the incident", "Update CHANGELOG for 2.3.1", "Migrate users table to UUID4 ids",
    "Handle HTTP 429 from GitHub", "Port base64 helpers", "Use IPv6Address and H264Decoder",
    "Add Win32Api shim", "Wrap Float32Array", "Parse X509Certificate",
])
def test_plain_task_subject_survives(tmp_path, subject):
    sys.path.insert(0, str(ORCH))
    from fleet.common import scrub_subject
    assert scrub_subject(subject) == subject


def test_subject_scrub_is_bounded_on_huge_input():
    sys.path.insert(0, str(ORCH))
    from fleet.common import scrub_subject
    for shape in ("key" + " " * 40000, "x" * 40000 + ":", "a:" * 20000, "A1" * 30000):
        t = time.perf_counter()
        scrub_subject(shape)
        assert time.perf_counter() - t < 0.5, f"{len(shape)}-char subject took too long"


def test_subject_omitted_when_the_scrubber_cannot_load(tmp_path):
    empty = tmp_path / "no-scrubber"
    empty.mkdir()
    code = ("import sys; sys.path.insert(0, %r); from fleet.common import scrub_subject; "
            "print(repr(scrub_subject('deploy key abc', scrubber_dir=__import__('pathlib').Path(%r))))") % (str(ORCH), str(empty))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.stdout.strip() == "None", r.stdout + r.stderr


@pytest.mark.parametrize("handle", ["../other", "a/b", ".hidden"])
def test_handle_with_path_chars_reports_nothing(tmp_path, synapse, handle):
    env = make_env(tmp_path, synapse.url)
    toml = Path(env["MS4CC_SYNAPSE_TOML"])
    toml.write_text(toml.read_text().replace('handle = "plumb"', f'handle = "{handle}"'))
    (tmp_path / "other.token").write_text("someone-elses-token")
    assert hook(env, base("Stop")).returncode == 0
    time.sleep(0.3)
    assert synapse.requests == []


def test_session_id_cannot_escape_the_state_dir(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    hook(env, base("PostToolUse", session_id="../../escape", tool_name="Read", tool_input={}, tool_response={}))
    synapse.wait(1)
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet"
    assert not list(tmp_path.rglob("escape*")) or all(state in p.parents for p in tmp_path.rglob("escape*"))
    assert not (tmp_path / "escape.tool").exists() and not (tmp_path.parent / "escape.tool").exists()


@pytest.mark.parametrize("inp,typ,payload", [
    (base("PostToolUseFailure", tool_name="Bash", tool_input={"command": SECRET}, error=SECRET), "tool", {"tool_name": "Bash"}),
    (base("Notification", notification_type="elicitation_url_dialog", message=SECRET), "needs_input", {"kind": "elicitation_url_dialog"}),
    (base("Notification", notification_type="agent_needs_input", message=SECRET), "needs_input", {"kind": "agent_needs_input"}),
    (base("SessionStart", source="clear", cwd="/"), "session.started", {"source": "clear"}),
])
def test_round1_mappings(tmp_path, synapse, inp, typ, payload):
    env = make_env(tmp_path, synapse.url)
    hook(env, inp)
    (ev,) = synapse.wait(1)
    assert (ev["type"], ev["payload"]) == (typ, payload)
    assert SECRET not in synapse.requests[0]["raw"]


def test_session_start_prunes_state_older_than_14_days(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet"
    state.mkdir(parents=True)
    old, new = state / "dead-session.tool", state / "live-session.tool"
    for p in (old, new):
        p.write_text("1")
    os.utime(old, (time.time() - 15 * 86400,) * 2)
    hook(env, base("SessionStart", source="startup"))
    synapse.wait(1)
    assert not old.exists() and new.exists()


def test_parallel_tool_burst_sends_one(tmp_path, synapse):
    """40 hooks released together: the sampling check must be atomic, so exactly one sends.

    Each process is started and its interpreter warmed up, then all stdins close at once,
    so the processes reach rate_ok() together (communicate() one by one would serialise them)."""
    env = make_env(tmp_path, synapse.url)
    data = json.dumps(base("PostToolUse", tool_name="Read", tool_input={}, tool_response={})).encode()
    procs = [subprocess.Popen([sys.executable, str(REPORT)], stdin=subprocess.PIPE, env=env) for _ in range(40)]
    for p in procs:
        p.stdin.write(data)
    time.sleep(1.5)  # every process is now blocked in json.load(stdin), past import and config
    for p in procs:
        p.stdin.close()
    for p in procs:
        p.wait(timeout=20)
    time.sleep(0.5)
    assert len([e for e in synapse.events() if e["type"] == "tool"]) == 1


def test_sampling_check_holds_a_lock(tmp_path, synapse):
    """Deterministic twin of the burst test: while another process holds the sampling lock,
    a hook never checks and marks unlocked. Since #144 review S3-6 it waits a bounded time
    (LOCK_WAIT_S), then gives up and sends nothing; with the lock free it samples normally."""
    import fcntl
    env = make_env(tmp_path, synapse.url)
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet"
    state.mkdir(parents=True)
    fd = os.open(state / f"{SID}.tool", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    tool = base("PostToolUse", tool_name="Read", tool_input={}, tool_response={})
    try:
        t = time.perf_counter()
        r = hook(env, tool)
        took = time.perf_counter() - t
    finally:
        os.close(fd)
    assert r.returncode == 0 and took < 3, f"took {took:.1f} s"
    time.sleep(0.3)
    assert [e for e in synapse.events() if e["type"] == "tool"] == [], "checked and marked without the lock"
    hook(env, tool)
    assert [e["type"] for e in synapse.wait(1)] == ["tool"]


def test_update_during_the_flushers_send_is_not_lost(tmp_path, synapse):
    """Review round 2, finding A: an update landing while the flusher's POST is in flight
    used to find the claim still held and be dropped for good."""
    synapse.latency = 0.6
    env = make_env(tmp_path, synapse.url, inline=False)
    def status(pct):
        inp = json.loads(json.dumps(STATUS_IN))
        inp["context_window"]["used_percentage"] = pct
        assert hook(env, inp, STATUSLINE).returncode == 0
    t0 = time.time()
    status(10)                       # direct send; opens the window
    time.sleep(1.0)
    status(20)                       # held; a flusher waits for the window to end (~t0+10)
    time.sleep(max(0.0, t0 + 10.35 - time.time()))
    status(30)                       # lands while the flusher's POST is in flight
    evs = synapse.wait(3, timeout=25)
    assert [e["payload"]["context_pct"] for e in evs] == [10, 20, 30]


N1_RACE = r"""
import sys, threading, time
sys.path.insert(0, %r)
from fleet import statusline as sl
from fleet import common as c
sl.STATUS_EVERY_S = 1.0
sent = []
c.send_now = lambda cfg, evs: sent.append(evs[0]["payload"]["context_pct"])
c.emit = c.send_now
stall, go = threading.Event(), threading.Event()
real_last_sent = c.last_sent
def slow_last_sent(sid):
    stall.set(); go.wait(2)
    return real_last_sent(sid)
def inp(p): return {"session_id": "S1", "context_window": {"used_percentage": p}}
sl.report(inp(10))
c.last_sent = slow_last_sent
t = threading.Thread(target=sl.report, args=(inp(20),)); t.start()
stall.wait(5)
c.last_sent = real_last_sent
sl.report(inp(25)); time.sleep(0.005); sl.report(inp(30))
go.set(); t.join(); time.sleep(1.5)
print(sent, c._state_file("S1", "status.pending").exists())
"""


def test_update_landing_while_the_flusher_decides_is_not_orphaned(tmp_path, synapse):
    """Review round 3, N1: the flusher used to hold its claim from taking the snapshot until
    it finished, so an update landing in that window was left pending with no flusher.
    Deterministic: the flusher is stalled inside that window (a reproduction by the reviewer)."""
    env = make_env(tmp_path, synapse.url)
    r = subprocess.run([sys.executable, "-c", N1_RACE % str(ORCH)], env=env, capture_output=True, text=True,
                       timeout=30, cwd=str(tmp_path))
    assert r.stdout.strip() == "[10.0, 25.0, 30.0] False", r.stdout + r.stderr


R4_RACE = r"""import sys, threading, time, pathlib
sys.path.insert(0, %r)
from fleet import statusline as sl
from fleet import common as c
sl.STATUS_EVERY_S = 1.0
sent = []
c.send_now = lambda cfg, evs: sent.append(evs[0]["payload"]["context_pct"])
c.emit = c.send_now
threads = []
def detach(fn):
    t = threading.Thread(target=fn); threads.append(t); t.start()
c.detach = detach
def inp(p): return {"session_id": "S1", "context_window": {"used_percentage": p}}
claim = c._state_file("S1", "status.flusher")
state = {"armed": False, "fired_d": False, "fired_u": False}
real_rr = c.rate_remaining
def rr(sid, what, every):
    # F1 about to sleep its last bit: the window ends and a direct send D lands first
    if state["armed"] and not state["fired_d"] and threading.current_thread() is threads[0]:
        state["fired_d"] = True
        c.reset_rate("S1", "status"); time.sleep(0.002)
        sl.report(inp(25))                      # direct send: unlinks pending 20, opens a new window
        return 0.0
    return real_rr(sid, what, every)
c.rate_remaining = rr
real_unlink = pathlib.Path.unlink
def unlink(self, *a, **k):
    real_unlink(self, *a, **k)
    if self == claim and state["fired_d"] and not state["fired_u"] and threading.current_thread() is threads[0]:
        state["fired_u"] = True
        time.sleep(0.002)
        sl.report(inp(30))                      # inside the new window, after F1's release: claims, gets F2
pathlib.Path.unlink = unlink
sl.report(inp(10)); time.sleep(0.002)
state["armed"] = True
sl.report(inp(20))                              # held; F1 starts
for _ in range(50):
    for t in list(threads): t.join()
    time.sleep(0.05)
time.sleep(1.5)
for t in list(threads): t.join()
print(sent, "pending left:", c._state_file("S1", "status.pending").exists(), "flushers:", len(threads))
"""


def test_flusher_that_loses_the_reclaim_puts_its_snapshot_back(tmp_path, synapse):
    """Review round 4, S2-1 (reviewer's deterministic reproduction): a direct send lands during
    the flusher's last sleep, then an update takes the free claim while the flusher holds the
    newest snapshot. Losing the re-claim must not drop that snapshot."""
    env = make_env(tmp_path, synapse.url)
    r = subprocess.run([sys.executable, "-c", R4_RACE % str(ORCH)], env=env, capture_output=True, text=True,
                       timeout=60, cwd=str(tmp_path))
    assert r.stdout.strip().startswith("[10.0, 25.0, 30.0] pending left: False"), r.stdout + r.stderr


def test_whitespace_or_unbroken_huge_subject_drops_only_the_subject(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    hook(env, base("TaskCreated", task_id="9", task_subject=" " * 512 + "tok"))
    hook(env, base("TaskCompleted", task_id="9", task_subject="x" * 600))
    evs = synapse.wait(2)
    assert [(e["type"], e["payload"]) for e in evs] == [("task.created", {"task_id": "9"}),
                                                       ("task.completed", {"task_id": "9"})]


def test_429_is_retried_once_then_dropped(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url)
    synapse.statuses = [429]
    hook(env, base("Stop"))
    assert [r["code"] for r in synapse.requests] == [429, 202]
    assert [e["type"] for e in synapse.events()] == ["turn.ended"]
    synapse.statuses = [429, 429]
    hook(env, base("UserPromptSubmit", prompt="x"))
    assert [r["code"] for r in synapse.requests][2:] == [429, 429], "one retry only, then dropped"
    synapse.statuses, synapse.retry_after = [429], "nan"
    hook(env, base("Stop"))
    assert [r["code"] for r in synapse.requests][4:] == [429, 202], "a NaN Retry-After still retries"


def test_report_hook_is_silent_on_a_python_without_tomllib(tmp_path, synapse):
    old = Path("/usr/bin/python3")
    if not old.exists() or subprocess.run([str(old), "-c", "import tomllib"], capture_output=True).returncode == 0:
        pytest.skip("no tomllib-less python on this box")
    env = make_env(tmp_path, synapse.url)
    r = subprocess.run([str(old), str(REPORT)], input=json.dumps(base("SessionStart", source="startup")).encode(),
                       env=env, capture_output=True, timeout=20)
    assert (r.returncode, r.stdout) == (0, b"")
    time.sleep(0.3)
    assert synapse.requests == []


def test_fragment_notification_matcher_matches_the_needs_input_kinds():
    """The matcher is the real gate in Claude Code; the reporter's list must agree with it."""
    sys.path.insert(0, str(ORCH))
    from fleet.report import NEEDS_INPUT_KINDS
    frag = json.loads((ORCH / "settings.fragment.json").read_text())
    groups = [g for g in frag["hooks"]["Notification"] if any("fleet/report.py" in h["command"] for h in g["hooks"])]
    assert len(groups) == 1 and set(groups[0]["matcher"].split("|")) == NEEDS_INPUT_KINDS


@pytest.mark.parametrize("fleet_toml,expected", [
    ('[fleet]\nenabled = true\nmachine = "box 💻"\n', {"enabled": True, "machine": "box 💻"}),
    ('[fleet]\nenabled = false\nmachine = "a \\"q\\" \\\\ b"\n', {"enabled": False, "machine": 'a "q" \\ b'}),
    ('[fleet]\nenabled = "yes"\nmachine = 7\n', None),
    ('[fleet]\nenabled = true\nmachine = "a\\u007fb"\n', {"enabled": True, "machine": "a\x7fb"}),
    ('', None),
])
def test_setup_keeps_the_fleet_table(tmp_path, fleet_toml, expected):
    import tomllib
    sys.path.insert(0, str(ORCH))
    from integrations.synapse.cli import _kept_fleet_lines
    p = tmp_path / "synapse.toml"
    p.write_text('[synapse]\nhandle = "x"\n' + fleet_toml)
    out = "\n".join(['[synapse]', 'handle = "x"', ""] + _kept_fleet_lines(p))
    assert tomllib.loads(out).get("fleet") == expected


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


# --- #144 review (Cairn) ---------------------------------------------------------------

S3_1_RACE = r"""import sys, threading, time, json
sys.path.insert(0, %r)
from fleet import statusline as sl
from fleet import common as c
sl.STATUS_EVERY_S = 1.0
sent = []
c.send_now = lambda cfg, evs: sent.append(evs[0]["payload"]["context_pct"])
c.emit = c.send_now
threads = []
def detach(fn):
    t = threading.Thread(target=fn); threads.append(t); t.start()
c.detach = detach
def inp(p): return {"session_id": "S1", "context_window": {"used_percentage": p}}
real_loads = json.loads
fired = []
def loads(s, *a, **k):
    # The flusher has its snapshot in hand. A direct send opens a new window, and an update
    # lands inside it before the flusher removes what it took.
    if threads and threading.current_thread() is threads[0] and not fired:
        fired.append(1)
        c.reset_rate("S1", "status"); time.sleep(0.002)
        sl.report(inp(25))
        time.sleep(0.002)
        sl.report(inp(30))
    return real_loads(s, *a, **k)
json.loads = loads
sl.report(inp(10)); time.sleep(0.002)
sl.report(inp(20))
for _ in range(60):
    for t in list(threads): t.join()
    time.sleep(0.05)
print(sent, "pending left:", c._state_file("S1", "status.pending").exists())
"""


def test_flusher_never_removes_a_snapshot_newer_than_the_one_it_took(tmp_path, synapse):
    """#144 review S3-1 (Cairn's reproduction): the flusher read the pending snapshot, then
    unlinked the PATH, so a newer snapshot written between the two was deleted unseen."""
    env = make_env(tmp_path, synapse.url)
    r = subprocess.run([sys.executable, "-c", S3_1_RACE % str(ORCH)], env=env, capture_output=True, text=True,
                       timeout=60, cwd=str(tmp_path))
    assert r.stdout.strip() == "[10.0, 25.0, 30.0] pending left: False", r.stdout + r.stderr


S3_2_WRITERS = r"""import sys, os, time, json, multiprocessing, pathlib
mp = multiprocessing.get_context("fork")
sys.path.insert(0, %r)
from fleet import statusline as sl
from fleet import common as c
c.emit = c.send_now = lambda cfg, evs: None
c.detach = lambda fn: None
barrier = mp.Barrier(2)
def writer(pct, late):
    def write_text(self, data, *a, **k):  # open (truncating), meet the other writer, then write
        with open(self, "w") as f:
            barrier.wait(5)
            if late:
                time.sleep(0.3)
            f.write(data)
    pathlib.Path.write_text = write_text
    sl.report({"session_id": "S1", "model": {"id": "m" * pct}, "context_window": {"used_percentage": pct}})
sl.report({"session_id": "S1", "context_window": {"used_percentage": 1}})  # opens the window
ps = [mp.Process(target=writer, args=(90, False)), mp.Process(target=writer, args=(5, True))]
for p in ps: p.start()
for p in ps: p.join(10)
try:
    print(json.loads(c._state_file("S1", "status.pending").read_text())["payload"]["context_pct"])
except ValueError:
    print("CORRUPT")
"""


def test_two_status_lines_writing_at_once_never_leave_corrupt_json(tmp_path, synapse):
    """#144 review S3-2: every writer used one temp name, so two concurrent writes could
    interleave (a long one, then a short one over its head) and leave broken JSON pending."""
    env = make_env(tmp_path, synapse.url)
    r = subprocess.run([sys.executable, "-c", S3_2_WRITERS % str(ORCH)], env=env, capture_output=True, text=True,
                       timeout=60, cwd=str(tmp_path))
    assert r.stdout.strip() in ("90.0", "5.0"), r.stdout + r.stderr


@pytest.mark.parametrize("stdin,own", [
    (b"[" * 100000 + b"]" * 100000, b""),
    (b'{"session_id": "s", "model": {"display_name": "M"}, "context_window": {"used_percentage": 1'
     + b"0" * 400 + b"}}", b"M"),
    (b'{"session_id": "s", "model": {"display_name": "M"}, "cost": {"total_cost_usd": Infinity}}', b"M"),
], ids=["deeply-nested", "400-digit-int", "infinity"])
def test_malformed_status_input_still_shows_a_bar(tmp_path, synapse, stdin, own):
    """#144 review S3-3: RecursionError and OverflowError escaped main() and blanked the bar.
    The wrapped bar shows whatever the input; without a wrap, our own bar shows what it can."""
    env = make_env(tmp_path, synapse.url)
    wrap = base64.b64encode(b"cat >/dev/null; printf MY-BAR").decode()
    r = subprocess.run([sys.executable, str(STATUSLINE), "--wrap-b64", wrap], input=stdin, env=env,
                       capture_output=True, timeout=20)
    assert (r.stdout, r.returncode) == (b"MY-BAR", 0), r.stderr
    r = subprocess.run([sys.executable, str(STATUSLINE)], input=stdin, env=env, capture_output=True, timeout=20)
    assert (r.stdout, r.returncode) == (own, 0), r.stderr
    time.sleep(0.3)
    assert all("Infinity" not in q["raw"] and "inf" not in json.dumps(q["json"]) for q in synapse.requests)


PARSE_ERROR = r"""import sys, json
sys.path.insert(0, %r)
from fleet import statusline as sl
def boom(*a, **k):
    raise RecursionError("maximum recursion depth exceeded")  # Python < 3.14 on deep nesting
json.loads = boom
sys.exit(sl.main(sys.argv[1:]))
"""


def test_a_parse_error_that_is_not_a_valueerror_still_shows_the_wrapped_bar(tmp_path, synapse):
    """#144 review S3-3, made deterministic: 3.14's decoder no longer raises RecursionError on
    deep nesting, so the input above can't reach the guard here; the error is forced instead."""
    env = make_env(tmp_path, synapse.url)
    wrap = base64.b64encode(b"cat >/dev/null; printf MY-BAR").decode()
    r = subprocess.run([sys.executable, "-c", PARSE_ERROR % str(ORCH), "--wrap-b64", wrap], input=b"{}",
                       env=env, capture_output=True, timeout=20)
    assert (r.stdout, r.returncode) == (b"MY-BAR", 0), r.stderr


def test_wrapped_command_past_the_timeout_is_killed_with_everything_it_started(tmp_path, monkeypatch):
    """#144 review S3-4 and S3-5: the timeout killed only /bin/sh (its children lived on, one
    set per refresh), and nothing tested the timeout or the fallback bar at all."""
    sys.path.insert(0, str(ORCH))
    from fleet import statusline as sl
    monkeypatch.setattr(sl, "WRAP_TIMEOUT_S", 0.5)
    pidfile = tmp_path / "child.pid"
    t = time.perf_counter()
    out, code = sl.bar(b"{}", STATUS_IN, f"sleep 30 & echo $! > '{pidfile}'; wait")
    took = time.perf_counter() - t
    assert (out.decode(), code) == ("Opus 5.5 · ctx 42% · $1.23", 0), "the MS4CC bar shows instead"
    assert took < 3, f"took {took:.1f} s"
    pid = int(pidfile.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        os.kill(pid, 9)
        pytest.fail("the wrapped command's child outlived the timeout")


def test_a_held_rate_lock_never_stalls_the_status_line(tmp_path, synapse):
    """#144 review S3-6: a blocking flock on the status-line path waited for as long as the
    holder lived (a suspended process, a network home)."""
    import fcntl
    env = make_env(tmp_path, synapse.url, inline=False)
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet"
    state.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(state / f"{SID}.status", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t = time.perf_counter()
        r = hook(env, STATUS_IN, STATUSLINE)
        assert r.stdout.decode() == "Opus 5.5 · ctx 42% · $1.23"
        assert time.perf_counter() - t < 2.5, "the status line waited on the lock"
    finally:
        os.close(fd)


@pytest.mark.parametrize("toml,state", [
    ('[synapse]\nbase_url = "https://syn.example"\nhandle = "p"\n', "off"),
    ('[synapse]\nbase_url = "https://syn.example"\nhandle = "p"\n[fleet]\nenabled = false\n', "off"),
    ('[synapse]\nbase_url = "https://syn.example"\nhandle = "p"\n[fleet]\nenabled = "true"\n', "off"),
    ('[synapse]\nbase_url = "https://syn.example"\nhandle = "p"\n[fleet]\nenabled = true\n', "on"),
    ('[synapse]\nbase_url = "http://localhost:8000"\nhandle = "p"\n[fleet]\nenabled = true\n', "on"),
    ('[synapse]\nbase_url = "http://[::1]:8000"\nhandle = "p"\n[fleet]\nenabled = true\n', "on"),
    ('[synapse]\nbase_url = "http://syn.example"\nhandle = "p"\n[fleet]\nenabled = true\n', "on plain-http"),
    ('[synapse]\nbase_url = "HTTP://10.0.0.5:8000"\nhandle = "p"\n[fleet]\nenabled = true\n', "on plain-http"),
    ('not toml [', "off"),
])
def test_bootstrap_state_decides_the_status_line_install(tmp_path, toml, state):
    """#144 review S2-1 and S3-9: bootstrap installs the status line only while Fleet is on
    (the token need not exist yet), and warns when the token would cross plain http."""
    (tmp_path / "s.toml").write_text(toml)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MS4CC_")}
    env.update(MS4CC_SYNAPSE_TOML=str(tmp_path / "s.toml"), MS4CC_SYNAPSE_HOME=str(tmp_path / "home"))
    r = subprocess.run([sys.executable, "-c", "from fleet.common import bootstrap_state; print(bootstrap_state())"],
                       cwd=str(ORCH), env=env, capture_output=True, text=True, timeout=20)
    assert r.stdout.strip() == state, r.stderr


S3_C_RACE = r"""import sys, os, threading, time
sys.path.insert(0, %r)
from fleet import statusline as sl
from fleet import common as c
sl.STATUS_EVERY_S = 1.0
sent = []
c.send_now = lambda cfg, evs: sent.append(evs[0]["payload"]["context_pct"])
c.emit = c.send_now
threads = []
def detach(fn):
    t = threading.Thread(target=fn); threads.append(t); t.start()
c.detach = detach
def inp(p): return {"session_id": "S1", "context_window": {"used_percentage": p}}
pending = c._state_file("S1", "status.pending")
real_last_sent, real_write = c.last_sent, os.write
state = {"window": False, "stole": False}
def last_sent(sid):
    if threads and threading.current_thread() is threads[0] and not state["window"]:
        state["window"] = True
        c.reset_rate("S1", "status"); c.rate_ok("S1", "status", 1.0)  # a direct send opens a window
    return real_last_sent(sid)
def write(fd, data):
    # A second flusher takes the put-back file between its create and its write: it reads it
    # empty, drops it, and the write lands in an unlinked file.
    if threads and threading.current_thread() is threads[0] and not state["stole"] and pending.exists():
        state["stole"] = True
        stolen = pending.with_name(pending.name + ".stolen")
        os.rename(pending, stolen)
        stolen.unlink()
    return real_write(fd, data)
c.last_sent = last_sent
os.write = write
sl.report(inp(10)); time.sleep(0.002)
sl.report(inp(20))
for _ in range(60):
    for t in list(threads): t.join()
    time.sleep(0.05)
print(sent)
"""


def test_a_snapshot_put_back_can_never_be_taken_half_written(tmp_path, synapse):
    """#144 delta S3-C (Cairn): the put-back created the file, then wrote it, so a second
    flusher could take it empty in between, and the snapshot was lost."""
    env = make_env(tmp_path, synapse.url)
    r = subprocess.run([sys.executable, "-B", "-c", S3_C_RACE % str(ORCH)], env=env, capture_output=True, text=True,
                       timeout=60, cwd=str(tmp_path))
    assert r.stdout.strip() == "[10.0, 20.0]", r.stdout + r.stderr


def test_a_nul_in_the_wrapped_command_still_shows_a_bar(tmp_path, synapse):
    env = make_env(tmp_path, synapse.url, enabled=False)
    r = hook(env, STATUS_IN, STATUSLINE, ["--wrap-b64", base64.b64encode(b"echo a\x00b").decode()])
    assert (r.stdout.decode(), r.returncode) == ("Opus 5.5 · ctx 42% · $1.23", 0)
