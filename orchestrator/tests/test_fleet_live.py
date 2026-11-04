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
    monkeypatch.setattr(L, "_activity_on", lambda: True)  # the follower re-reads the config
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


def test_a_partial_line_waits_and_is_never_sent_alone(tmp_path, synapse, fast):
    """A line goes out only once it ends: a fragment can't be redacted (a key without its value
    reads as nothing). A last line with no newline is whole once the command has ended."""
    f = tmp_path / "b1.output"
    f.write_text("")
    t = start(synapse.url, f)
    append(f, "progress 10%")
    time.sleep(0.2)
    assert outputs(synapse) == []
    append(f, " 20%\nno newline at the end")
    time.sleep(0.2)
    f.unlink()
    t.join(5)
    out = outputs(synapse)
    assert [p["text"] for p in out] == ["progress 10% 20%\n", "no newline at the end\n", ""]
    assert [p["done"] for p in out] == [False, False, True]

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
    assert texts == ["start\n", "later\n"]  # an idle stop isn't the end: no done


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
    start(synapse.url, f).join(5)  # idle, not running: stops, without done; "part" is kept
    append(f, "ial two\nthree\n")
    start(synapse.url, f).join(5)
    f.unlink()
    start(synapse.url, f).join(5)  # gone: done, once
    out = outputs(synapse)
    assert [p["text"] for p in out] == ["one\n", "partial two\nthree\n", ""]
    assert [p["seq"] for p in out] == [0, 1, 2]
    assert [p["done"] for p in out] == [False, False, True]
    assert [p["truncated"] for p in out] == [False, False, False]

def test_a_restarted_follower_redacts_a_line_it_saw_only_the_start_of(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("one\nexport PASSWORD=")
    start(synapse.url, f).join(5)  # idle: stops; the line's start is not sent
    append(f, "Hunter2CorrectHorseBatteryStaple\nnext\n")
    start(synapse.url, f).join(5)
    sent = "".join(p["text"] for p in outputs(synapse))
    assert "CorrectHorse" not in sent and "Hunter2" not in sent and sent.endswith("next\n")
    assert "export PASSWORD=" in sent  # sent whole, redacted

def test_the_stream_names_its_call_once_known(tmp_path, synapse, fast):
    f = tmp_path / "b1.output"
    f.write_text("x\n")
    L.remember_task(SID, "b1", "toolu_BG1")
    start(synapse.url, f).join(5)
    assert {p.get("tool_use_id") for p in outputs(synapse)} == {"toolu_BG1"}


def test_one_follower_per_task(tmp_path, fast):
    first = L._claim(SID, "b1")
    assert first is not None and L._claim(SID, "b1") is None  # a lock, not a pid: no race
    os.close(first)  # the follower exited
    again = L._claim(SID, "b1")
    assert again is not None
    os.close(again)

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
    out = [e["payload"] for e in synapse.wait(1) if e["type"] == "tool.output"]
    assert [(p["text"], p["done"]) for p in out] == [("hello from the task\n", False)]  # idle stop
    (d / "b9.output").unlink()
    hook(env, file_changed(tmp_path, "unlink", d / "b9.output"))  # the command ended: done, once
    hook(env, file_changed(tmp_path, "unlink", d / "b9.output"))
    out = [e["payload"] for e in synapse.wait(2) if e["type"] == "tool.output"]
    assert [(p["text"], p["seq"], p["done"]) for p in out] == [("hello from the task\n", 0, False), ("", 1, True)]


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




# --- seams: a secret must never reach the wire in a form redaction couldn't see ------------------
# A deterministic driver: each poll's sleep runs the next scripted step, on a fake clock, and
# sends are captured in process. READ_MAX, CTX_MAX and HOLD_MAX are made small so a sweep over
# every fixture at every offset runs in seconds; the logic doesn't depend on their size.

from test_fleet_activity import _shared_fixtures  # noqa: E402

SECRETS = _shared_fixtures()["secret"]


class Drive:
    def __init__(self, path: Path, steps: list, unlink_after: bool = True):
        self.t, self.path, self.steps, self.unlink_after = 1000.0, path, list(steps), unlink_after

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        if self.steps:
            self.steps.pop(0)()
        elif self.unlink_after and self.path.exists():
            self.path.unlink()
        else:
            self.t += 3600  # nothing more: idle, then MAX_S


@pytest.fixture
def small(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(L, "READ_MAX", 2048)
    monkeypatch.setattr(L, "CTX_MAX", 1024)
    monkeypatch.setattr(L, "HOLD_MAX", 1024)
    monkeypatch.setattr(L, "_activity_on", lambda: True)
    sent: list[dict] = []
    monkeypatch.setattr(L, "_send", lambda cfg, events: sent.extend(events) or True)

    def run(path: Path, steps: list, unlink_after: bool = True) -> list[dict]:
        d = Drive(path, steps, unlink_after)
        monkeypatch.setattr(L, "time", types.SimpleNamespace(time=d.time, sleep=d.sleep))
        L.follow(cfg("http://127.0.0.1:9"), SID, path, path.stem)
        return [e["payload"] for e in sent]
    run.sent = sent
    return run


def _w(path: Path, text: str, mode: str = "a"):
    def step():
        with path.open(mode) as f:
            f.write(text)
    return step


def _wire(payloads: list[dict]) -> str:
    return "".join(p["text"] for p in payloads)


def _reset(tmp_path: Path, run) -> Path:
    import shutil
    shutil.rmtree(tmp_path / "state", ignore_errors=True)
    run.sent.clear()
    f = tmp_path / "s1.output"
    f.write_text("")
    return f


def test_every_fixture_split_across_two_polls_at_every_byte(tmp_path, small):
    """Round-1 S1-1: a Secret's kind or a key's END in a different poll from its values."""
    leaks = []
    for case in SECRETS:
        text = "start\n" + case["input"] + "\nend\n"
        assert case["leak"] not in clean(text)
        for i in range(1, len(text)):
            f = _reset(tmp_path, small)
            out = small(f, [_w(f, text[:i]), _w(f, text[i:])])
            if case["leak"] in _wire(out):
                leaks.append((case["name"], i))
    assert leaks == []


def test_every_fixture_cut_by_a_stop_and_resumed_at_every_byte(tmp_path, small):
    """Round-1 S1-2: a follower stops (idle) mid-line or mid-construct, a new one resumes."""
    leaks = []
    for case in SECRETS:
        text = "start\n" + case["input"] + "\nend\n"
        for i in range(1, len(text)):
            f = _reset(tmp_path, small)
            small(f, [_w(f, text[:i])], unlink_after=False)  # stops idle, no done
            out = small(f, [_w(f, text[i:])])
            if case["leak"] in _wire(out):
                leaks.append((case["name"], i))
    assert leaks == []


def test_every_fixture_whose_tail_arrives_as_the_command_ends(tmp_path, small):
    """Round-1 S1-2: the rest is written and the file goes before the next poll."""
    leaks = []
    for case in SECRETS:
        text = "start\n" + case["input"] + "\nend\n"
        for i in range(1, len(text)):
            f = _reset(tmp_path, small)

            def rest_and_gone(f=f, rest=text[i:]):
                _w(f, rest)()
                f.unlink()
            out = small(f, [_w(f, text[:i]), rest_and_gone])
            if case["leak"] in _wire(out):
                leaks.append((case["name"], i))
    assert leaks == []


def test_a_burst_read_starting_inside_any_fixture_never_sends_it(tmp_path, small):
    """Round-1 S1-1: the burst window starts at every offset inside the fixture."""
    leaks = []
    for case in SECRETS:
        body = case["input"] + "\n" + "".join(f"row {n:05d}\n" for n in range(400))
        for k in range(0, len(case["input"]) + 1):
            f = _reset(tmp_path, small)
            filler = "f" * 40 + "\n"
            lead = filler * 80 + case["input"][:0]
            text = lead + body
            # make the read (the last READ_MAX bytes) start k bytes into the fixture
            cut = len(lead.encode()) + len(case["input"][:k].encode())
            tail = text.encode()[cut:]
            if len(tail) > L.READ_MAX:
                tail = tail[: L.READ_MAX]
            data = text.encode()[:cut] + tail
            out = small(f, [lambda f=f, data=data: f.write_bytes(data)])
            if case["leak"] in _wire(out):
                leaks.append((case["name"], k))
    assert leaks == []


def test_a_line_of_read_max_or_more_is_never_sent_in_part(tmp_path, small):
    """Round-1 S1-2, forced path: a line still growing past READ_MAX is skipped whole."""
    leaks = []
    for case in SECRETS:
        if "\n" in case["input"]:
            continue
        line = "w " * (L.READ_MAX // 2) + case["input"] + " tail\n"  # always over READ_MAX
        for i in list(range(L.READ_MAX - 60, L.READ_MAX, 7)) + [L.READ_MAX]:  # READ_MAX: one poll
            f = _reset(tmp_path, small)
            out = small(f, [_w(f, line[:i]), _w(f, line[i:] + "next\n")])
            if case["leak"] in _wire(out) or "w w w" in _wire(out):
                leaks.append((case["name"], i))
            assert _wire(out).endswith("next\n") and out[-2]["truncated"] is True
    assert leaks == []


def test_lines_are_redacted_with_the_output_before_them(tmp_path, small):
    """kubectl prints `kind: Secret` in one poll and the data in the next: the second poll is
    redacted with the first as context."""
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, 'kind: "Secret"\n'), _w(f, "data:\n  app: c2VjcmV0dmFsdWU=\n")])
    assert "c2VjcmV0dmFsdWU=" not in _wire(out) and "data:" in _wire(out)


def test_a_secret_data_block_waits_for_its_kind(tmp_path, small):
    """kubectl -o yaml sorts keys: data comes before kind. The data waits for the kind line."""
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, "apiVersion: v1\ndata:\n  app: c2VjcmV0dmFsdWU=\n"), lambda: None,
                    _w(f, "kind: Secret\nmetadata:\n  name: x\n")])
    assert "c2VjcmV0dmFsdWU=" not in _wire(out)
    assert out[0]["text"] == "apiVersion: v1\n"  # sent before the hold began


def test_a_key_block_waits_for_its_end_line(tmp_path, small):
    f = _reset(tmp_path, small)
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
    out = small(f, [_w(f, "x\n-----BEGIN PRIVATE KEY-----\n" + body), lambda: None,
                    _w(f, body + "-----END PRIVATE KEY-----\ny\n")])
    assert "MIIEvQIBADAN" not in _wire(out) and _wire(out).startswith("x\n") and _wire(out).endswith("y\n")


def test_held_lines_past_hold_max_are_skipped_with_the_key_lines_after_them(tmp_path, small):
    """An END that never comes doesn't stall the stream, and the BEGIN that made the lines
    secret may have left the context window by then: the held lines are skipped, and so are
    key lines after them, until a line that isn't key material."""
    f = _reset(tmp_path, small)
    line = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZn\n"
    out = small(f, [_w(f, "x\n-----BEGIN PRIVATE KEY-----\n")] + [_w(f, line * 4) for _ in range(12)]
                + [_w(f, "after\n")])
    assert "QUJDREVGR0hJSktM" not in _wire(out)
    assert _wire(out) == "x\nafter\n" and out[1]["truncated"] is True

def test_done_is_sent_once_per_task(tmp_path, small):
    """Round-1 S2-3: an idle stop isn't the end; only the file going (or the session) is."""
    f = _reset(tmp_path, small)
    small(f, [_w(f, "a\n")], unlink_after=False)
    small(f, [_w(f, "b\n")], unlink_after=False)
    out = small(f, [_w(f, "c\n")])
    assert [p["done"] for p in out] == [False, False, False, True]
    assert [p["seq"] for p in out] == [0, 1, 2, 3]
    small(f, [])  # a follower for a finished task sends nothing
    assert len(small.sent) == 4


def test_a_file_rewritten_in_place_restarts_from_its_start(tmp_path, small):
    """Round-1 S2-4: rewritten to more than the old offset; the old offset would land mid-line."""
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, "hello world " * 6 + "\n"), _w(f, "export PASSWORD=Hunter2CorrectHorseBatteryStaple\nnext\n", "w")])
    assert "Hunter2" not in _wire(out) and "CorrectHorse" not in _wire(out) and _wire(out).endswith("next\n")
    assert out[1]["truncated"] is True


def test_a_failed_send_marks_the_next_event(tmp_path, small, monkeypatch):
    f = _reset(tmp_path, small)
    calls = []

    def flaky(cfg, events):
        calls.append(events)
        small.sent.extend(events)
        return len(calls) != 1
    monkeypatch.setattr(L, "_send", flaky)
    out = small(f, [_w(f, "a\n"), _w(f, "b\n")])
    assert [p["truncated"] for p in out] == [False, True, False]


def test_activity_turned_off_stops_the_follower_without_done(tmp_path, small, monkeypatch):
    f = _reset(tmp_path, small)
    state = {"on": True}
    monkeypatch.setattr(L, "_activity_on", lambda: state["on"])

    def off():
        state["on"] = False
    out = small(f, [_w(f, "a\n"), off, _w(f, "b\n")])
    assert [p["text"] for p in out] == ["a\n"]


def test_pieces_cap_by_bytes_as_sent_and_lose_nothing():
    for text in ["漢字" * 3000 + "\n", "é" * 9000 + "\n", "\x01" * 3000 + "\n", ("word " * 2000 + "\n") * 2]:
        pieces = L._pieces(text)
        assert all(size(p) <= L.CHUNK_MAX for p in pieces)
        assert "".join(pieces) == text


def test_a_burst_starting_at_a_line_start_skips_nothing(tmp_path, small):
    f = _reset(tmp_path, small)
    lines = "".join(f"row {n:05d}\n" for n in range(1000))  # 10 bytes a line, READ_MAX 2048
    out = small(f, [_w(f, lines[: len(lines) - 2040] + lines[len(lines) - 2040:])])
    assert _wire(out).startswith("row ") and _wire(out).endswith("row 00999\n")


# --- wiring: CwdChanged and resumed sessions ------------------------------------------------------

def test_the_watch_hook_answers_cwdchanged_too(tmp_path, synapse):
    """Round-1 S2-1: a cd rebuilds Claude Code's watch list from CwdChanged hooks' watchPaths."""
    inp = {"hook_event_name": "CwdChanged", "session_id": SID, "transcript_path": transcript_path(tmp_path),
           "cwd": "/Users/x/proj/sub", "old_cwd": "/Users/x/proj", "new_cwd": "/Users/x/proj/sub"}
    on = subprocess.run([sys.executable, "-B", str(LIVE), "--watch"], input=json.dumps(inp).encode(),
                        env=live_env(tmp_path, synapse.url), capture_output=True, timeout=20)
    assert json.loads(on.stdout) == {"hookSpecificOutput": {"hookEventName": "CwdChanged",
                                                            "watchPaths": [str(expected_tasks(tmp_path))]}}


def test_a_resumed_session_streams_again(tmp_path, synapse):
    """Round-1 S2-2: --resume keeps the session id; SessionStart clears the end mark."""
    env = live_env(tmp_path, synapse.url)
    hook(env, base("SessionEnd", reason="other"))
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet"
    assert (state / f"{SID}.ended").exists()
    inp = {"hook_event_name": "SessionStart", "session_id": SID, "transcript_path": transcript_path(tmp_path),
           "cwd": "/Users/x/proj", "source": "resume"}
    subprocess.run([sys.executable, "-B", str(LIVE), "--watch"], input=json.dumps(inp).encode(),
                   env=env, capture_output=True, timeout=20)
    assert not (state / f"{SID}.ended").exists()


def test_a_background_call_starts_its_follower_at_once(tmp_path, synapse):
    """Claude Code reports a file only once its writes pause; a background call's task id is
    known at PostToolUse, so its follower starts there."""
    env = live_env(tmp_path, synapse.url)
    d = expected_tasks(tmp_path)
    d.mkdir(parents=True)
    (d / "b7.output").write_text("from the background\n")
    hook(env, base("PostToolUse", tool_name="Bash", tool_use_id="toolu_B", tool_input={},
                   tool_response={"stdout": "", "stderr": "", "backgroundTaskId": "b7"},
                   transcript_path=transcript_path(tmp_path)))
    out = [e["payload"] for e in synapse.wait(2) if e["type"] == "tool.output"]
    assert [(p["text"], p.get("tool_use_id")) for p in out] == [("from the background\n", "toolu_B")]
