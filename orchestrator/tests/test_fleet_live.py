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


def test_a_command_gone_before_its_follower_opens_the_file_sends_nothing(tmp_path, synapse, fast):
    """A short foreground command: tool.finished already carries its output."""
    f = tmp_path / "b1.output"
    L.follow(cfg(synapse.url), SID, f, "b1")  # the file is already gone
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
    """A background task Claude Code no longer lists as running is let go when idle."""
    L.remember_task(SID, "b1", "toolu_R")
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
    # done after a stop it can't see past: output may have come and gone in between
    assert [p["truncated"] for p in out] == [False, False, True]

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
    ("Bash", {"stdout": "", "stderr": "", "backgroundTaskId": "../../etc/x"}, None),
    ("Monitor", {"taskId": "a b"}, None),
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
    assert "c2VjcmV0dmFsdWU=" not in _wire(out) and L.WITHHELD in _wire(out)
    assert out[0]["text"] == "apiVersion: v1\ndata:\n"  # the header goes; its values wait
    assert "kind: Secret\nmetadata:\n  name: x\n" in _wire(out)  # the withheld run isn't context


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
    assert len(line) < 60  # the server's own key-line rule takes 60 to 76; these need the BEGIN
    # 6 batches pass HOLD_MAX (skipped); the 7th is held anew, then closed by "after": it would
    # go out with no BEGIN left in the context unless key lines after a skip stay unsent.
    # The run ends at a line that can't be key material: a bare word like "after" could be
    # (round-3 S2-2: any line of [A-Za-z0-9+/=] inside a key block), so it would stay withheld.
    out = small(f, [_w(f, "x\n-----BEGIN PRIVATE KEY-----\n")] + [_w(f, line * 4) for _ in range(7)]
                + [_w(f, "after it\n")])
    assert "QUJDREVGR0hJSktM" not in _wire(out)
    assert _wire(out) == f"x\n{L.WITHHELD}\nafter it\n"  # one marker for the whole run

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


def test_more_pieces_than_a_poll_sends_are_skipped_and_marked(tmp_path, small, monkeypatch):
    """Not a burst: one poll's lines make more than PER_TICK events. The oldest are skipped."""
    monkeypatch.setattr(L, "CHUNK_MAX", 256)
    f = _reset(tmp_path, small)
    lines = "".join(f"line {n:04d} " + "z " * 20 + "\n" for n in range(40))  # about 2 KB
    out = small(f, [_w(f, lines)])
    sent = [p for p in out if not p["done"]]
    assert len(sent) == L.PER_TICK and sent[0]["truncated"] is True
    assert _wire(out).endswith("line 0039 " + "z " * 20 + "\n")


def test_pieces_cap_by_bytes_as_sent_and_lose_nothing():
    for text in ["漢字" * 3000 + "\n", "é" * 9000 + "\n", "\x01" * 3000 + "\n", ("word " * 2000 + "\n") * 2]:
        pieces = L._pieces(text)
        assert all(size(p) <= L.CHUNK_MAX for p in pieces)
        assert "".join(pieces) == text


def test_a_burst_inside_a_line_longer_than_the_context_skips_that_line(tmp_path, small):
    """The context can't help when the line began more than CTX_MAX before the read: its value
    would go out as a bare tail, which redaction can't recognise."""
    v = "Hunter2-Correct.Horse!" * 200
    assert "Correct.Horse" in clean(v[2500:] + "\n")  # a bare tail isn't redacted
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, "a\n" * 50 + "export API_TOKEN=" + v + "\nlast\n")])
    assert "Correct.Horse" not in _wire(out) and _wire(out) == "last\n"
    assert out[0]["truncated"] is True


def test_a_burst_starting_at_a_line_start_skips_nothing(tmp_path, small):
    """Round 1: the burst skip took the first line even when the read began exactly on one."""
    f = _reset(tmp_path, small)
    lines = [f"row  {n:010d}\n" for n in range(1000)]  # 16 bytes each; READ_MAX 2048 = 128 lines
    assert len(lines[0]) == 16 and L.READ_MAX % 16 == 0
    out = small(f, [_w(f, "".join(lines))])
    assert _wire(out) == "".join(lines[-L.READ_MAX // 16:])
    assert out[0]["truncated"] is True  # the lines before the read were skipped



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


# --- round 2: Secrets across objects, Secrets longer than the context, the join -----------------

V = "c2VjcmV0dmFsdWU="
POD_THEN_SECRET_YAML = ("apiVersion: v1\nkind: Pod\nmetadata:\n  name: web\napiVersion: v1\ndata:\n"
                        f"  app.conf: {V}\nkind: Secret\nmetadata:\n  name: db\ntype: Opaque\n")
POD_THEN_SECRET_JSON = ('{\n  "apiVersion": "v1",\n  "kind": "Pod",\n  "metadata": {\n    "name": "web"\n  }\n}\n'
                        '{\n  "apiVersion": "v1",\n  "data": {\n    "app.conf": "' + V + '"\n  },\n'
                        '  "kind": "Secret",\n  "metadata": {\n    "name": "db"\n  },\n  "type": "Opaque"\n}\n')
LIST_YAML = ("apiVersion: v1\nitems:\n- apiVersion: v1\n  data:\n    color: blue\n  kind: ConfigMap\n"
             "  metadata:\n    name: cm\n- apiVersion: v1\n  data:\n" f"    app.conf: {V}\n"
             "  kind: Secret\n  metadata:\n    name: db\n  type: Opaque\nkind: List\n")
LIST_JSON = ('{\n    "apiVersion": "v1",\n    "items": [\n        {\n            "apiVersion": "v1",\n'
             '            "data": {\n                "color": "blue"\n            },\n'
             '            "kind": "ConfigMap"\n        },\n        {\n            "apiVersion": "v1",\n'
             '            "data": {\n                "app.conf": "' + V + '"\n            },\n'
             '            "kind": "Secret",\n            "type": "Opaque"\n        }\n    ],\n    "kind": "List"\n}\n')


@pytest.mark.parametrize("text", [POD_THEN_SECRET_YAML, POD_THEN_SECRET_JSON, LIST_YAML, LIST_JSON],
                         ids=["pod-secret-yaml", "pod-secret-json", "list-yaml", "list-json"])
def test_an_earlier_objects_kind_never_releases_a_secrets_data(tmp_path, small, text):
    """Round-2 S1-A: a Pod's or ConfigMap's kind before a Secret's data released it."""
    assert V not in clean(text)
    leaks = []
    for i in range(1, len(text)):
        f = _reset(tmp_path, small)
        if V in _wire(small(f, [_w(f, text[:i]), _w(f, text[i:])])):
            leaks.append(i)
    assert leaks == []
    # A List is one document to the server too: with a Secret in it, every data block in it is
    # withheld, the ConfigMap's as well.


def _big_secret(n: int, plain: bool = False) -> tuple[str, list[str]]:
    vals = [f"plainvalue{k:05d}" if plain else f"c2VjcmV0dmFsdWU{k:05d}" for k in range(n)]
    return ("apiVersion: v1\ndata:\n" + "".join(f"  key{k:05d}.conf: {v}\n" for k, v in enumerate(vals))
            + "kind: Secret\nmetadata:\n  name: big\ntype: Opaque\n"), vals


def test_a_secret_longer_than_the_context_in_one_burst_sends_no_value(tmp_path, small):
    """Round-2 S1-B: the data: header leaves the context window (and the burst skips it)."""
    text, vals = _big_secret(300)  # about 12 KB; READ_MAX, CTX_MAX and HOLD_MAX are 1-2 KB here
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, text)])
    assert [v for v in vals if v in _wire(out)] == [] and "kind: Secret" in _wire(out)


def test_a_secret_held_past_hold_max_sends_no_value_after_it(tmp_path, small):
    """Round-2 S1-B: 17 KB of data held then skipped, and the next poll's values went out."""
    text, vals = _big_secret(60)
    cut = text.index("kind: Secret")
    f = _reset(tmp_path, small)
    first, rest = text[: text.index("  key00045")], text[text.index("  key00045"):]
    out = small(f, [_w(f, first), _w(f, rest)])
    assert [v for v in vals if v in _wire(out)] == [] and cut


def test_a_key_after_a_long_secret_never_sends_the_secrets_values(tmp_path, small):
    """Round-2 S1-C: a key block collapsing to one line made the join reach back into values."""
    text, vals = _big_secret(60)
    key = ("-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7Mhgw\n" * 25
           + "-----END RSA PRIVATE KEY-----\n")
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, text), _w(f, key)])
    assert [v for v in vals if v in _wire(out)] == [] and "MIIEowIBAAKCAQEA" not in _wire(out)


def test_a_documents_end_resets_the_secret_state(tmp_path, small):
    f = _reset(tmp_path, small)
    text = (f"kind: Secret\ndata:\n  a: {V}\n---\nkind: ConfigMap\ndata:\n  color: blue\nmetadata:\n  name: cm\n")
    out = small(f, [_w(f, text)])
    assert V not in _wire(out) and "color: blue" in _wire(out)


def test_a_key_block_ends_at_its_end_line(tmp_path, small):
    f = _reset(tmp_path, small)
    k = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZn\n"
    plain = "thisisanordinarylongwordwithoutspaces"  # key-like in shape, not to the redactor
    assert L._KEYISH.fullmatch(plain) and plain in clean(plain + "\n")
    out = small(f, [_w(f, "-----BEGIN PRIVATE KEY-----\n" + k * 3 + "-----END PRIVATE KEY-----\n"
                       + plain + "\nafter\n")])
    assert k.strip() not in _wire(out)
    assert plain + "\nafter\n" in _wire(out)  # after the END: sent


def test_a_file_rewritten_longer_restarts_from_its_start(tmp_path, small):
    """Round-2 M11: rewritten in place to more than the old offset (the size check can't see it)."""
    f = _reset(tmp_path, small)
    first = "hello world " * 6 + "\n"
    second = "z " * 30 + "\n" + "starts here and is longer than the first content was\n"
    out = small(f, [_w(f, first), _w(f, second, "w")])
    assert len(second.encode()) > len(first.encode()) and second[:64] != first[:64]
    assert _wire(out) == first + second and out[1]["truncated"] is True


def test_a_file_truncated_below_the_offset_restarts_from_its_start(tmp_path, small):
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, "a b c\n" * 5), _w(f, "x y\n", "w")])
    assert _wire(out) == "a b c\n" * 5 + "x y\n" and out[1]["truncated"] is True


def test_a_replaced_file_is_read_from_its_start_by_the_next_follower(tmp_path, small):
    """Round-2 M12: a new file (another inode) at the path when a follower resumes."""
    L.remember_task(SID, "s1", "toolu_S")  # a background task: let go when idle
    f = _reset(tmp_path, small)
    small(f, [_w(f, "one two three four\n")], unlink_after=False)
    f.unlink()
    f.write_text("new file first line\n")
    out = small(f, [])
    assert _wire(out) == "one two three four\nnew file first line\n" and out[1]["truncated"] is True


def test_session_end_sends_what_arrived_then_done_marked_for_what_waits(tmp_path, small):
    """Round-2 M20, M21: the last poll before the session's done, and the mark for held lines."""
    f = _reset(tmp_path, small)

    def lines_then_end():
        _w(f, "last line\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZn\n")()
        L.mark_ended(SID)
    out = small(f, [lines_then_end])
    assert [p["text"] for p in out] == ["last line\n", ""]
    assert out[-1]["done"] is True and out[-1]["truncated"] is True  # a key-like line waited


def test_session_end_sends_done_for_a_follower_already_stopped(tmp_path, small):
    """Round-2 sev-3: SessionEnd only marked the session; a stopped follower's task got no done."""
    L.remember_task(SID, "s1", "toolu_S")
    f = _reset(tmp_path, small)
    small(f, [_w(f, "a\n")], unlink_after=False)  # idle stop
    L.end_session(cfg("http://127.0.0.1:9"), SID)
    out = [e["payload"] for e in small.sent]
    assert [(p["text"], p["done"]) for p in out] == [("a\n", False), ("", True)]
    assert out[-1]["truncated"] is True  # output after the stop can't be known


def test_a_foreground_task_is_not_let_go_when_idle(tmp_path, small, monkeypatch):
    """Round-2 sev-3: a foreground command silent for a minute lost its last lines."""
    monkeypatch.setattr(L, "IDLE_S", 3.0)  # the fake clock passes it in two polls
    f = _reset(tmp_path, small)
    idle = [lambda: None] * 5  # the fake clock jumps past IDLE_S
    out = small(f, [_w(f, "step 1 of 2\n")] + idle + [_w(f, "step 2 of 2: FAILED\n")])
    assert _wire(out) == "step 1 of 2\nstep 2 of 2: FAILED\n" and out[-1]["done"] is True


def test_a_secret_whose_kind_comes_after_other_keys_withholds_its_data(tmp_path, small):
    """Hand-written order: data, then metadata, then kind. The next key isn't kind, but the
    document's kind (read in the same poll) is Secret."""
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, f"apiVersion: v1\ndata:\n  a: {V}\nmetadata:\n  name: x\nkind: Secret\n")])
    assert V not in _wire(out)


def test_a_configmaps_data_at_the_end_of_the_output_is_sent(tmp_path, small):
    """Undecided when the poll ends; the command's end decides it: not a Secret."""
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, "kind: ConfigMap\ndata:\n  color: blue\n")])
    assert _wire(out) == "kind: ConfigMap\ndata:\n  color: blue\n"


def test_a_repeat_that_changes_the_context_sends_nothing_rather_than_a_misaligned_cut(tmp_path, small):
    """The new line makes a value seen earlier a known secret, so the earlier line redacts
    differently: what belongs to the new line can't be told apart, so nothing of it is sent."""
    bare, keyed = "the value is horse-battery-77\n", "export API_TOKEN=horse-battery-77\n"
    assert clean(bare) == bare and "horse-battery-77" not in clean(bare + keyed)  # the setup holds
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, "the value is horse-battery-77\n"),
                    _w(f, "export API_TOKEN=horse-battery-77\n"), _w(f, "next\n")])
    assert _wire(out) == "the value is horse-battery-77\nnext\n" and out[1]["truncated"] is True


def test_a_withheld_run_is_not_context_for_what_follows(tmp_path, small, monkeypatch):
    """A Secret withheld past HOLD_MAX in one poll, its kind in the next. Were the withheld raw
    values context, the kind would re-redact them and the whole next batch would be dropped."""
    monkeypatch.setattr(L, "CTX_MAX", 8192)  # the header and the values inside the window
    text, vals = _big_secret(60, plain=True)  # values only the Secret rule redacts
    assert all(v in clean("  a: " + v + "\n") for v in vals[:3])
    first, rest = text[: text.index("kind: Secret")], text[text.index("kind: Secret"):]
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, first), _w(f, rest)])
    assert [v for v in vals if v in _wire(out)] == []
    assert "kind: Secret\nmetadata:\n  name: big\ntype: Opaque\n" in _wire(out)


# --- round 3: a data block is decided only by its kind, its document's end, or the command's end -

IMMUTABLE_YAML = f"apiVersion: v1\ndata:\n  a: {V}\nimmutable: true\nkind: Secret\nmetadata:\n  name: x\n"
IMMUTABLE_JSON = ('{\n    "apiVersion": "v1",\n    "data": {\n        "a": "' + V + '"\n    },\n'
                  '    "immutable": true,\n    "kind": "Secret",\n    "metadata": {\n        "name": "x"\n'
                  '    },\n    "type": "Opaque"\n}\n')
HAND_ORDER = f"apiVersion: v1\ndata:\n  a: {V}\nmetadata:\n  name: x\n  labels:\n    app: db\nkind: Secret\n"
NESTED_JSON = ('{\n    "apiVersion": "v1",\n    "spec": {\n        "data": {\n            "a": "' + V + '"\n'
               '        }\n    },\n    "kind": "Secret"\n}\n')


@pytest.mark.parametrize("text", [IMMUTABLE_YAML, IMMUTABLE_JSON, HAND_ORDER, NESTED_JSON],
                         ids=["immutable-yaml", "immutable-json", "data-metadata-kind", "json-closer-below-data"])
def test_a_secrets_data_waits_for_its_kind_whatever_comes_between(tmp_path, small, text):
    """Round-3 S1-1, S2-1: `immutable: true` (kubectl sorts it between data and kind), other
    keys, or a JSON closer at a lower indent than the data released it as not a Secret."""
    assert V in clean("data:\n  a: " + V + "\n") and V not in clean(text)  # the kind decides
    leaks = []
    for i in range(1, len(text)):
        f = _reset(tmp_path, small)
        if V in _wire(small(f, [_w(f, text[:i]), _w(f, text[i:])])):
            leaks.append(i)
    assert leaks == []


def test_a_short_last_key_line_is_withheld(tmp_path, small):
    """Round-3 S2-2: inside a key block, a key line of any length is key material."""
    k = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZn\n"
    text = "x\n-----BEGIN PRIVATE KEY-----\n" + k * 3 + "QUJDzz==\n-----END PRIVATE KEY-----\ny\n"
    leaks = []
    for i in range(1, len(text)):
        f = _reset(tmp_path, small)
        w = _wire(small(f, [_w(f, text[:i]), _w(f, text[i:])]))
        if "QUJDzz" in w or "QUJDREVG" in w:
            leaks.append(i)
    assert leaks == []


def test_a_burst_past_replay_max_withholds_indented_lines_until_the_margin(tmp_path, small, monkeypatch):
    """Round-3 S3-3: past REPLAY_MAX the scanner starts with no state: where the read starts
    may be inside a Secret's data, so indented lines wait for a line at the margin."""
    monkeypatch.setattr(L, "REPLAY_MAX", 512)
    vals = [f"plainvalue{k:05d}" for k in range(200)]
    text = ("kind: Secret\ndata:\n" + "".join(f"  key{k:05d}: {v}\n" for k, v in enumerate(vals))
            + "type: Opaque\nafter\n")
    assert len(text.encode()) > L.READ_MAX + 512 * 4
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, text)])
    assert [v for v in vals if v in _wire(out)] == []
    assert _wire(out).endswith("type: Opaque\nafter\n") and out[0]["truncated"] is True


def test_a_file_rewritten_in_place_under_64_bytes_restarts_from_its_start(tmp_path, small):
    """Round-3 S3-4: the head check hashed nothing until the file reached 64 bytes."""
    f = _reset(tmp_path, small)
    out = small(f, [_w(f, "ab cd\n"), _w(f, "zz yy xx\n", "w")])
    assert _wire(out) == "ab cd\nzz yy xx\n" and out[1]["truncated"] is True


def test_a_file_gone_as_its_follower_stops_still_gets_its_done(tmp_path, small, monkeypatch):
    """Round-3 S3-5: the file went after the follower stopped and before it let go of its lock;
    the unlink saw the lock held and sent nothing. The follower's exit sends it."""
    import types
    L.remember_task(SID, "s1", "toolu_S")  # a background task: let go when idle
    f = _reset(tmp_path, small)
    d = Drive(f, [_w(f, "a\n")], unlink_after=False)
    monkeypatch.setattr(L, "time", types.SimpleNamespace(time=d.time, sleep=d.sleep))
    real = L.follow
    unlink_saw = []

    def follow_then_gone(*a):
        real(*a)  # stopped (idle), lock still held
        f.unlink()
        L.on_file_changed(cfg("http://127.0.0.1:9"), {"event": "unlink", "file_path": str(f)}, SID)
        unlink_saw.append([e["payload"]["done"] for e in small.sent])
    monkeypatch.setattr(L, "follow", follow_then_gone)
    monkeypatch.setattr(L, "_task_path", lambda inp, p: Path(p))
    monkeypatch.setenv("MS4CC_FLEET_INLINE", "1")
    L._start(cfg("http://127.0.0.1:9"), SID, f, "s1")
    assert unlink_saw == [[False]]  # the unlink's own done was refused: the lock was held
    out = [e["payload"] for e in small.sent]
    assert [(p["text"], p["done"]) for p in out] == [("a\n", False), ("", True)]


def test_a_claimed_session_resumed_streams_again(tmp_path, synapse):
    """Round-3 S3-1: a session claimed while running (no launch mark) ends, releasing its claim
    and marking it ended; resumed, its SessionStart must clear the mark before it is claimed
    again, or every follower stops at its first poll."""
    env = live_env(tmp_path, synapse.url)
    env.pop("MS4CC_FLEET_AGENT")
    claim = ORCH / "fleet" / "claim.py"
    run_claim = lambda *a: subprocess.run([sys.executable, "-B", str(claim), *a], env=dict(env, CLAUDE_CODE_SESSION_ID=SID),
                                          capture_output=True, timeout=20)
    assert run_claim().returncode == 0
    hook(env, base("SessionEnd", reason="other", transcript_path=transcript_path(tmp_path)))
    state = Path(env["MS4CC_SYNAPSE_HOME"]) / "fleet"
    assert (state / f"{SID}.ended").exists()
    start_inp = {"hook_event_name": "SessionStart", "session_id": SID, "transcript_path": transcript_path(tmp_path),
                 "cwd": "/Users/x/proj", "source": "resume"}
    w = subprocess.run([sys.executable, "-B", str(LIVE), "--watch"], input=json.dumps(start_inp).encode(),
                       env=env, capture_output=True, timeout=20)
    assert json.loads(w.stdout)["hookSpecificOutput"]["watchPaths"] == [str(expected_tasks(tmp_path))]
    assert not (state / f"{SID}.ended").exists()
    assert run_claim().returncode == 0  # claimed again
    d = expected_tasks(tmp_path)
    (d / "b9.output").write_text("after the resume\n")
    hook(env, file_changed(tmp_path, "add", d / "b9.output"))
    out = [e["payload"] for e in synapse.wait(1) if e["type"] == "tool.output"]
    assert [p["text"] for p in out] == ["after the resume\n"]
