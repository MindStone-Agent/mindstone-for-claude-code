"""Fleet live output (synapse#31 follow-up): fleet/live.py and its wiring in report.py.

Shapes from Claude Code 2.1.317, measured in isolated `claude -p` runs: a task's output file is
`<tmp>/claude-<uid>/<project>/<session>/tasks/<id>.output`; FileChanged gives
{event: add|change|unlink, file_path}; Stop gives background_tasks [{id, type, status, ...}].

    cd orchestrator && python -m pytest -q tests/test_fleet_live.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_reporter import ORCH, REPORT, SID, TOKEN, base, hook, make_env, synapse  # noqa: E402,F401

sys.path.insert(0, str(ORCH))
from fleet import common as C  # noqa: E402
from fleet import live as L  # noqa: E402
from fleet.redact import clean, size  # noqa: E402

LIVE = ORCH / "fleet" / "live.py"


@pytest.fixture
def fast(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(L, "POLL_S", 0.05)
    monkeypatch.setattr(L, "IDLE_S", 0.4)
    monkeypatch.setattr(L, "MAX_S", 10.0)
    monkeypatch.setenv("MS4CC_FLEET_INLINE", "1")


def cfg(url: str) -> C.FleetConfig:
    return C.FleetConfig(base_url=url, handle="plumb", token=TOKEN, machine="m", activity=True)


def outputs(synapse) -> list[dict]:
    return [e["payload"] for e in synapse.events() if e["type"] == "tool.output"]


def start(url: str, path: Path, task: str = "b1") -> threading.Thread:
    t = threading.Thread(target=L.follow, args=(cfg(url), SID, path, task), daemon=True)
    t.start()
    return t


def append(path: Path, text: str) -> None:
    with path.open("a") as f:
        f.write(text)


def test_lines_stream_in_order_then_done_when_the_file_goes(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("line1\n")
    t = start(synapse.url, f)
    for i in range(2, 6):
        time.sleep(0.12)
        append(f, f"line{i}\n")
    time.sleep(0.2)
    f.unlink()
    t.join(5)
    out = outputs(synapse)
    assert "".join(p["text"] for p in out) == "".join(f"line{i}\n" for i in range(1, 6))
    assert [p["seq"] for p in out] == list(range(len(out)))  # from 0, per task
    assert out[-1]["done"] is True and all(p["done"] is False for p in out[:-1])
    assert all(p["task_id"] == "b1" for p in out)


def test_a_command_gone_before_the_first_poll_sends_nothing(tmp_path, synapse, fast, monkeypatch):
    """A short foreground command: tool.finished already carries its output."""
    monkeypatch.setattr(L, "POLL_S", 0.3)
    f = tmp_path / "b1.output"
    f.write_text("quick\n")
    t = start(synapse.url, f)
    f.unlink()
    t.join(5)
    assert outputs(synapse) == []


def test_a_partial_line_waits_and_is_flushed_at_the_end(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("")
    t = start(synapse.url, f)
    append(f, "progress 10%")
    time.sleep(0.2)
    assert outputs(synapse) == []
    append(f, " 20%\nno newline at the end")
    t.join(5)  # idle, not running: finishes
    out = outputs(synapse)
    assert [p["text"] for p in out] == ["progress 10% 20%\n", "no newline at the end", ""]
    assert [p["truncated"] for p in out] == [False, False, False]


def test_a_burst_is_capped_per_poll_and_marked_truncated(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("")
    t = start(synapse.url, f)
    append(f, "".join(f"row {i:06d} " + "x" * 60 + "\n" for i in range(5000)))  # ~350 KB at once
    t.join(5)
    out = [p for p in outputs(synapse) if not p["done"]]
    assert 1 <= len(out) <= L.PER_TICK
    assert out[0].get("truncated") is True
    assert all(size(p["text"]) <= L.CHUNK_MAX for p in out)
    assert out[-1]["text"].endswith("row 004999 " + "x" * 60 + "\n")


def test_live_output_is_redacted(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("export API_TOKEN=supersecretvalue99\nok\n")
    t = start(synapse.url, f)
    t.join(5)
    raw = json.dumps(synapse.requests)
    assert "supersecretvalue99" not in raw and "ok\\n" in raw


def test_an_idle_task_still_running_is_followed(tmp_path, synapse, fast):
    """Idle past IDLE_S, but Stop said it's running: keep following, then stop when it isn't."""
    f = tmp_path / "b1.output"
    f.write_text("start\n")
    L.remember_running(SID, [{"id": "b1", "type": "shell", "status": "running"}])
    t = start(synapse.url, f)
    time.sleep(0.8)  # twice IDLE_S
    append(f, "later\n")
    time.sleep(0.3)
    L.remember_running(SID, [])
    t.join(5)
    texts = [p["text"] for p in outputs(synapse)]
    assert texts == ["start\n", "later\n", ""]


def test_session_end_stops_the_follower(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("a\n")
    L.remember_running(SID, [{"id": "b1"}])
    t = start(synapse.url, f)
    time.sleep(0.2)
    L.mark_ended(SID)
    t.join(5)
    assert not t.is_alive() and outputs(synapse)[-1]["done"] is True


def test_a_restarted_follower_resumes_where_the_last_stopped(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("one\npart")
    start(synapse.url, f).join(5)  # idle, not running: ends, flushing "part"
    append(f, "ial two\nthree\n")
    start(synapse.url, f).join(5)
    out = outputs(synapse)
    # "part" went out when the first follower finished. The rest of that line is skipped (it
    # may be a value whose key was in "part"), and the next chunk says something was skipped.
    assert [p["text"] for p in out] == ["one\n", "part", "", "three\n", ""]
    assert [p["truncated"] for p in out] == [False, False, False, True, False]
    assert [p["seq"] for p in out] == [0, 1, 2, 3, 4]


def test_a_restarted_follower_never_sends_the_value_after_a_flushed_key(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("one\nexport PASSWORD=")
    start(synapse.url, f).join(5)  # idle: ends, flushing "export PASSWORD="
    append(f, "Hunter2CorrectHorseBatteryStaple\nnext\n")
    start(synapse.url, f).join(5)
    sent = "".join(p["text"] for p in outputs(synapse))
    assert "CorrectHorse" not in sent and sent.endswith("next\n")


def test_the_stream_names_its_call_once_known(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("x\n")
    L.remember_task(SID, "b1", "toolu_BG1")
    start(synapse.url, f).join(5)
    assert {p.get("tool_use_id") for p in outputs(synapse)} == {"toolu_BG1"}


def test_one_follower_per_task(tmp_path, fast):
    C._ensure_state_dir()
    first = L._claim(SID, "b1")
    first.write_text(str(os.getpid()))  # a live follower
    assert first is not None and L._claim(SID, "b1") is None
    first.write_text("999999")  # its process is gone: taken over
    assert L._claim(SID, "b1") is not None


# --- wiring: hooks ------------------------------------------------------------------------------

def live_env(tmp_path, url, activity=True):
    env = make_env(tmp_path, url)
    toml = Path(env["MS4CC_SYNAPSE_TOML"])
    toml.write_text(toml.read_text() + ("activity = true\n" if activity else ""))
    env.update(CLAUDE_CODE_TMPDIR=str(tmp_path / "tmp"), MS4CC_FLEET_LIVE_POLL_S="0.05",
               MS4CC_FLEET_LIVE_IDLE_S="0.2", MS4CC_FLEET_LIVE_MAX_S="5")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return env


def transcript_path(tmp_path) -> str:
    return str(tmp_path / "projects" / "-Users-x-proj" / f"{SID}.jsonl")


def expected_tasks(tmp_path) -> Path:
    return Path(os.path.realpath(tmp_path / "tmp")) / f"claude-{os.getuid()}" / "-Users-x-proj" / SID / "tasks"


def test_tasks_dir_follows_claude_codes_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(tmp_path / "tmp"))
    assert L.tasks_dir({"transcript_path": transcript_path(tmp_path), "session_id": SID}) == expected_tasks(tmp_path)
    assert L.tasks_dir({"transcript_path": transcript_path(tmp_path), "session_id": "../evil"}) is None
    assert L.tasks_dir({"session_id": SID}) is None


def test_the_watch_hook_returns_the_tasks_dir_only_with_activity_on(tmp_path, synapse):
    inp = {"hook_event_name": "SessionStart", "session_id": SID, "transcript_path": transcript_path(tmp_path),
           "cwd": "/Users/x/proj", "source": "startup"}
    on = subprocess.run([sys.executable, "-B", str(LIVE), "--watch"], input=json.dumps(inp).encode(),
                        env=live_env(tmp_path, synapse.url), capture_output=True, timeout=20)
    assert json.loads(on.stdout) == {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                            "watchPaths": [str(expected_tasks(tmp_path))]}}
    assert expected_tasks(tmp_path).is_dir()
    off = subprocess.run([sys.executable, "-B", str(LIVE), "--watch"], input=json.dumps(inp).encode(),
                         env=live_env(tmp_path, synapse.url, activity=False), capture_output=True, timeout=20)
    assert off.stdout == b"", "SessionStart stdout reaches the model: nothing unless activity is on"


def file_changed(tmp_path, kind, path) -> dict:
    d = base("FileChanged", file_path=str(path), transcript_path=transcript_path(tmp_path))
    d["event"] = kind  # base()'s own first parameter is called event
    return d


def test_file_changed_add_starts_a_follower(tmp_path, synapse):
    env = live_env(tmp_path, synapse.url)
    d = expected_tasks(tmp_path)
    d.mkdir(parents=True)
    (d / "b9.output").write_text("hello from the task\n")
    hook(env, file_changed(tmp_path, "add", d / "b9.output"))  # inline: follows until idle
    out = [e["payload"] for e in synapse.wait(2) if e["type"] == "tool.output"]
    assert [(p["text"], p["done"]) for p in out] == [("hello from the task\n", False), ("", True)]


@pytest.mark.parametrize("where", ["outside", "badname", "unlink"])
def test_file_changed_outside_the_tasks_dir_or_odd_names_are_ignored(tmp_path, synapse, where):
    env = live_env(tmp_path, synapse.url)
    d = expected_tasks(tmp_path)
    d.mkdir(parents=True)
    path = {"outside": tmp_path / "b9.output", "badname": d / "notes.txt", "unlink": d / "b9.output"}[where]
    path.write_text("data\n")
    hook(env, file_changed(tmp_path, "unlink" if where == "unlink" else "add", path))
    time.sleep(0.4)
    assert [e for e in synapse.events() if e["type"] == "tool.output"] == []


def test_activity_off_never_follows(tmp_path, synapse):
    env = live_env(tmp_path, synapse.url, activity=False)
    d = expected_tasks(tmp_path)
    d.mkdir(parents=True)
    (d / "b9.output").write_text("data\n")
    hook(env, file_changed(tmp_path, "add", d / "b9.output"))
    time.sleep(0.4)
    assert synapse.events() == []


@pytest.mark.parametrize("tool,resp,task", [
    ("Bash", {"stdout": "", "stderr": "", "backgroundTaskId": "b0r0svj79"}, "b0r0svj79"),
    ("Monitor", {"taskId": "bmb0vxgzz", "timeoutMs": 1800000, "persistent": False}, "bmb0vxgzz"),
    ("Bash", {"stdout": "x", "stderr": ""}, None),
])
def test_tool_finished_names_the_task_and_maps_it(tmp_path, synapse, tool, resp, task):
    env = live_env(tmp_path, synapse.url)
    hook(env, base("PostToolUse", tool_name=tool, tool_use_id="toolu_T", tool_input={}, tool_response=resp))
    (ev,) = synapse.wait(1)
    assert ev["payload"].get("task_id") == task
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet" / f"{SID}.task.{task}"
    assert (state.read_text() == "toolu_T") if task else not list(state.parent.glob(f"{SID}.task.*"))


def test_stop_records_running_tasks_and_session_end_marks_the_end(tmp_path, synapse):
    env = live_env(tmp_path, synapse.url)
    hook(env, base("Stop", background_tasks=[{"id": "b1", "type": "shell", "status": "running",
                                              "description": "d", "command": "sleep 20"}]))
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet"
    assert json.loads((state / f"{SID}.bgtasks").read_text()) == ["b1"]
    assert "sleep 20" not in json.dumps(synapse.requests)
    hook(env, base("SessionEnd", reason="other"))
    assert (state / f"{SID}.ended").exists()


# --- seams: a secret must never straddle a cut unredacted ------------------------------------------

SEAM_SECRETS = [  # (text with the secret, a fragment that must never be sent)
    ("password=Hunter2CorrectHorseBatteryStaple", "CorrectHorseBattery"),
    ('"api_key": "AKfakeKeyValueThatIsLong12345678"', "fakeKeyValueThatIsLong"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789", "nopqrstuvwxyz0123"),
]


def _sent(text: str, skipped: bool = False) -> str:
    out = L._pieces(text, skipped)
    assert out is not None
    return "".join(t for t, _ in out)


@pytest.mark.parametrize("secret,frag", SEAM_SECRETS)
def test_a_secret_across_a_chunk_cut_in_one_long_line_is_redacted(secret, frag):
    """A line longer than CHUNK_CHARS is cut; the cut must not split a secret from its key."""
    leaks = []
    for pad in range(L.CHUNK_CHARS - len(secret) - 2, L.CHUNK_CHARS + 2):
        line = "a" * pad + " " + secret + " " + "b" * 500 + "\n"
        if frag in _sent(line):
            leaks.append(pad)
    assert leaks == []


def test_a_line_longer_than_a_chunk_waits_for_its_newline(tmp_path, synapse, fast):
    """A line with no newline yet isn't sent once it passes CHUNK_CHARS: the rest of the
    secret may still be on its way."""
    f = tmp_path / "b1.output"
    f.write_text("")
    t = start(synapse.url, f)
    append(f, "x" * (L.CHUNK_CHARS + 10) + " password=Hunter2Correct")
    time.sleep(0.3)
    append(f, "HorseBatteryStaple tail\n")
    t.join(5)
    sent = "".join(p["text"] for p in outputs(synapse))
    assert "HorseBattery" not in sent and "Hunter2Correct" not in sent and sent.endswith("tail\n")


def test_a_line_forced_out_at_read_max_never_sends_the_rest_of_that_line(tmp_path, synapse, fast):
    """A line with no newline is sent once it reaches READ_MAX. Its rest may begin with a secret's
    value whose key was in the part already sent, so the rest of that line is skipped."""
    f = tmp_path / "b1.output"
    f.write_text("")
    t = start(synapse.url, f)
    append(f, "word " * (L.READ_MAX // 5 + 20) + "password=")
    time.sleep(0.3)
    append(f, "Hunter2CorrectHorseBatteryStaple more\nnext\n")
    t.join(5)
    out = [p for p in outputs(synapse) if not p["done"]]
    sent = "".join(p["text"] for p in out)
    assert "CorrectHorse" not in sent and sent.endswith("next\n")
    assert out[-1]["truncated"] is True  # the skipped rest is marked on the next event


def test_after_a_burst_the_line_the_read_starts_in_is_skipped(tmp_path, synapse, fast):
    """A burst is read from READ_MAX bytes before the end, which can be inside a value whose
    key was before it. Redaction can shrink the rest below PER_TICK pieces, so the per-poll cap
    doesn't hide that line: it has to be skipped."""
    import base64
    tail_of_value = "HorseBatteryStapleTail"
    head = "x" * 100 + " password=Hunter2Correct"  # before the read: never seen
    blob = base64.b64encode(os.urandom(L.READ_MAX)).decode()
    pem = ("-----BEGIN PRIVATE KEY-----\n", "\n-----END PRIVATE KEY-----\nlast\n")
    fixed = len(tail_of_value) + 1 + len(pem[0]) + len(pem[1])
    rest = tail_of_value + "\n" + pem[0] + blob[: L.READ_MAX + 4 - fixed] + pem[1]  # read starts 4 in
    data = head + rest
    pos = len(data) - L.READ_MAX
    assert len(head) <= pos < len(head) + len(tail_of_value) - 8  # the read starts inside the value
    f = tmp_path / "b1.output"
    f.write_text("")
    t = start(synapse.url, f)
    append(f, data)
    t.join(5)
    out = [p for p in outputs(synapse) if not p["done"]]
    sent = "".join(p["text"] for p in out)
    assert len(L._chunks(clean(rest[pos - len(head):]))) <= L.PER_TICK  # the cap can't hide it
    assert tail_of_value[-8:] not in sent and sent.endswith("last\n")
    assert out[0]["truncated"] is True
