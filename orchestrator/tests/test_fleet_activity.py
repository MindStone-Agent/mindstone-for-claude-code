"""Fleet activity layer, reporter side (synapse#31 scope change).

Hook inputs are shaped from real captures on Claude Code 2.1.312 (PreToolUse / PostToolUse /
PostToolUseFailure inside and outside a subagent, SubagentStart / Stop, transcripts with
`assistant`, `user` and `api-request-blob` lines).

    cd orchestrator && python -m pytest -q tests/test_fleet_activity.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_reporter import ORCH, REPORT, SECRET, SID, base, hook, make_env, synapse  # noqa: E402,F401

sys.path.insert(0, str(ORCH))
from fleet import activity as A  # noqa: E402
from fleet.common import activity_transport_ok  # noqa: E402
from fleet.redact import clean, preview  # noqa: E402

PROMPT = "PROMPT_CANARY_never_leaves_9q"


def act_env(tmp_path, url, activity=True):
    env = make_env(tmp_path, url)
    toml = Path(env["MS4CC_SYNAPSE_TOML"])
    toml.write_text(toml.read_text() + ("activity = true\n" if activity else ""))
    return env


def transcript(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    return path


def assistant(uuid: str, *texts: str, thinking: str | None = None) -> dict:
    blocks = ([{"type": "thinking", "thinking": thinking}] if thinking else []) + [{"type": "text", "text": t} for t in texts]
    return {"type": "assistant", "uuid": uuid, "message": {"role": "assistant", "content": blocks}}


def noise() -> list[dict]:
    """What else a real transcript holds: the user's prompt, a request blob that repeats it."""
    return [{"type": "user", "uuid": "u1", "message": {"role": "user", "content": PROMPT}},
            {"type": "api-request-blob", "message": {"role": "user", "content": [{"type": "text", "text": PROMPT}]}},
            {"type": "assistant", "uuid": "t1", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": PROMPT}}]}}]


# --- transport and the switch ---------------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://synapse.example.ts.net", True), ("http://127.0.0.1:8080", True), ("http://localhost:8080", True),
    ("http://[::1]:8080", True), ("http://192.168.1.84:8080", False), ("http://synapse.lan", False),
    ("ftp://127.0.0.1", False), ("http://127.0.0.1.evil.example", False),
])
def test_activity_only_over_https_or_to_this_machine(url, ok):
    assert activity_transport_ok(url) is ok


def test_activity_off_sends_no_tool_started_and_keeps_sampling(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url, activity=False)
    hook(env, base("PreToolUse", tool_name="Bash", tool_use_id="x1", tool_input={"command": "ls"}))
    hook(env, base("PostToolUse", tool_name="Bash", tool_use_id="x1", tool_input={"command": "ls"}, tool_response={"stdout": SECRET}))
    hook(env, base("PostToolUse", tool_name="Read", tool_use_id="x2", tool_input={}, tool_response={}))
    time.sleep(0.3)
    assert [(e["type"], e["payload"]) for e in synapse.events()] == [("tool", {"tool_name": "Bash"})]
    assert SECRET not in json.dumps(synapse.requests)


def test_activity_on_but_plain_http_to_another_host_sends_status_only(tmp_path):
    env = act_env(tmp_path, "http://192.0.2.1:9")
    sys.path.insert(0, str(ORCH))
    import subprocess
    code = "import sys; sys.path.insert(0, %r); from fleet.common import fleet_config; print(fleet_config().activity)" % str(ORCH)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.stdout.strip() == "False", r.stderr


# --- tool calls -------------------------------------------------------------------------------

def test_tool_started_and_finished_pair_on_tool_use_id(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    hook(env, base("PreToolUse", tool_name="Bash", tool_use_id="toolu_A", tool_input={"command": "cat a.txt", "description": PROMPT}))
    hook(env, base("PostToolUse", tool_name="Bash", tool_use_id="toolu_A", tool_input={"command": "cat a.txt"},
                   tool_response={"stdout": "hello", "stderr": "", "interrupted": False}, duration_ms=70))
    hook(env, base("PostToolUseFailure", tool_name="Bash", tool_use_id="toolu_B", tool_input={"command": "ls nope"},
                   error="Exit code 1\nls: nope: No such file or directory", duration_ms=22))
    started, finished, failed = synapse.wait(3)
    assert started["type"] == "tool.started" and started["payload"] == {
        "tool_use_id": "toolu_A", "tool_name": "Bash", "input_preview": "cat a.txt"}
    assert finished["type"] == "tool.finished" and finished["payload"] == {
        "tool_use_id": "toolu_A", "tool_name": "Bash", "ok": True, "duration_ms": 70, "output_preview": "hello"}
    assert failed["payload"] == {"tool_use_id": "toolu_B", "tool_name": "Bash", "ok": False, "duration_ms": 22,
                                 "error": "Exit code 1\nls: nope: No such file or directory"}
    assert PROMPT not in json.dumps(synapse.requests), "the Bash description isn't part of the preview"


def test_tool_calls_are_not_sampled_with_activity_on(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    for i in range(4):
        hook(env, base("PostToolUse", tool_name="Read", tool_use_id=f"r{i}", tool_input={"file_path": "/x"}, tool_response="ok"))
    assert len(synapse.wait(4)) == 4


def test_subagent_calls_carry_its_agent_id(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    hook(env, base("PreToolUse", tool_name="Bash", tool_use_id="s1", tool_input={"command": "ls"},
                   agent_id="affac8da", agent_type="general-purpose"))
    (ev,) = synapse.wait(1)
    assert ev["agent_id"] == "affac8da"


@pytest.mark.parametrize("name,ti,want", [
    ("Bash", {"command": "pytest -q", "description": "run"}, "pytest -q"),
    ("Read", {"file_path": "/a/b.py", "offset": 10, "limit": 5}, "/a/b.py offset=10 limit=5"),
    ("Write", {"file_path": "/a/c.py", "content": "x\ny\nz"}, "/a/c.py (3 lines)"),
    ("Edit", {"file_path": "/a/d.py", "old_string": "a\nb", "new_string": "c", "replace_all": True}, "/a/d.py (-2/+1 lines all)"),
    ("MultiEdit", {"file_path": "/a/e.py", "edits": [{}, {}]}, "/a/e.py (2 edits)"),
    ("Glob", {"pattern": "**/*.py", "path": "src"}, "**/*.py path=src"),
    ("Grep", {"pattern": "TODO", "glob": "*.ts"}, "TODO glob=*.ts"),
    ("WebFetch", {"url": "https://example.com/x", "prompt": PROMPT}, "https://example.com/x"),
    ("WebSearch", {"query": "python flock"}, "python flock"),
    ("Agent", {"subagent_type": "Explore", "description": "find tests", "prompt": PROMPT}, "Explore find tests"),
    ("mcp__x__y", {"a": 1}, '{"a": 1}'),
])
def test_input_preview_per_tool(name, ti, want):
    assert A.input_text(name, ti) == want


def test_edit_and_write_never_send_file_contents():
    assert SECRET not in A.input_text("Write", {"file_path": "/f", "content": SECRET})
    assert SECRET not in A.input_text("Edit", {"file_path": "/f", "old_string": SECRET, "new_string": SECRET})


def test_bash_output_joins_stdout_and_stderr():
    assert A.output_text({"stdout": "out", "stderr": "err"}) == "out\n[stderr]\nerr"
    assert A.output_text({"stdout": "", "stderr": "err"}) == "[stderr]\nerr"
    assert A.output_text("plain") == "plain"


# --- redaction and cuts -----------------------------------------------------------------------

@pytest.mark.parametrize("raw,secret", [
    ("Authorization: Basic dXNlcjpwYXNzd29yZDEy", "dXNlcjpw"),
    ('curl -H "Authorization: token abc123" https://x', "abc123"),
    ("MY_SECRET_TOKEN=zz9 make", "zz9"),
    ('DB_PASSWORD="hunter two"', "hunter two"),
    ("export GITHUB_TOKEN=ghp_aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5", "ghp_aB3"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMw\n-----END OPENSSH PRIVATE KEY-----", "b3BlbnNz"),
    ("postgres://u:pa55word@db:5432/x", "pa55word"),
])
def test_previews_are_redacted(raw, secret):
    assert secret not in clean(raw)


@pytest.mark.parametrize("raw", ["KEYBOARD=ok", "MONKEY=1", "PATH=/usr/bin:/bin", "ls -la && echo done"])
def test_ordinary_text_survives_redaction(raw):
    assert clean(raw) == raw


def test_long_output_keeps_head_and_tail_and_drops_split_words():
    raw = "HEAD " + "word " * 10000 + "TAIL"
    text, cut = preview(raw, 2000)
    assert cut and len(text) <= 2000 and text.startswith("HEAD ") and text.endswith("TAIL") and "cut to 2,000" in text
    for piece in text.replace("\n", " ").split():
        assert piece in ("HEAD", "TAIL", "word", "…") or piece.startswith(("[", "chars")) or piece.replace(",", "").rstrip("]").isdigit() \
            or piece in ("cut", "to"), f"split fragment {piece!r} survived the cut"


def test_a_secret_straddling_a_cut_leaves_no_fragment():
    raw = "x " * 900 + "AKIAIOSFODNN7EXAMPLE " + "y " * 900
    text, _ = preview(raw, 1800)
    assert "AKIA" not in text and "EXAMPLE" not in text


def test_huge_output_is_bounded_in_time_and_size():
    raw = "line of ordinary output 12345\n" * 60000
    t = time.perf_counter()
    text, cut = preview(raw, A.OUTPUT_MAX)
    assert time.perf_counter() - t < 2.0 and cut and len(text) <= A.OUTPUT_MAX


def test_no_scrubber_means_no_preview(tmp_path):
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import fleet.redact as r; r.ORCHESTRATOR_DIR = __import__('pathlib').Path(%r); "
            "print(r.preview('MY_SECRET_TOKEN=zz9', 100))") % (str(ORCH), str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(tmp_path)).stdout.strip()
    assert out == "(None, False)"


# --- subagent pairing -------------------------------------------------------------------------

def test_subagent_started_names_its_agent_call(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    hook(env, base("PreToolUse", tool_name="Agent", tool_use_id="toolu_AG1",
                   tool_input={"subagent_type": "general-purpose", "description": "check files", "prompt": PROMPT}))
    hook(env, base("SubagentStart", agent_id="a1", agent_type="general-purpose"))
    _, started = synapse.wait(2)
    assert started["type"] == "subagent.started" and started["agent_id"] == "a1"
    assert started["payload"] == {"agent_type": "general-purpose", "parent_tool_use_id": "toolu_AG1", "description": "check files"}
    assert PROMPT not in json.dumps(synapse.requests)


def test_pairing_takes_the_oldest_call_of_the_same_type(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    for tid, typ in (("c1", "Explore"), ("c2", "general-purpose"), ("c3", "Explore")):
        hook(env, base("PreToolUse", tool_name="Agent", tool_use_id=tid, tool_input={"subagent_type": typ, "description": tid}))
    for aid, typ in (("e1", "Explore"), ("g1", "general-purpose"), ("e2", "Explore"), ("x1", "Explore")):
        hook(env, base("SubagentStart", agent_id=aid, agent_type=typ))
    starts = [e for e in synapse.wait(7) if e["type"] == "subagent.started"]
    assert [(e["agent_id"], e["payload"].get("parent_tool_use_id")) for e in starts] == [
        ("e1", "c1"), ("g1", "c2"), ("e2", "c3"), ("x1", None)]


# --- replies from the transcript ---------------------------------------------------------------

def test_replies_are_tailed_from_session_start_without_history(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", noise() + [assistant("old", "history before the start")])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    transcript(tp, noise() + [assistant("m1", "first reply", thinking=PROMPT), assistant("m2", "second", "third")])
    hook(env, base("Stop", transcript_path=str(tp), last_assistant_message="third"))
    msgs = [e for e in synapse.wait(5) if e["type"] == "message"]
    assert [(m["payload"]["message_id"], m["payload"]["text"]) for m in msgs] == [
        ("m1.0", "first reply"), ("m2.0", "second"), ("m2.1", "third")]
    raw = json.dumps(synapse.requests)
    assert PROMPT not in raw and "history before the start" not in raw


def test_each_reply_is_sent_once_across_tails(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    transcript(tp, [assistant("m1", "one")])
    hook(env, base("PostToolUse", tool_name="Read", tool_use_id="r1", tool_input={}, tool_response="", transcript_path=str(tp)))
    transcript(tp, [assistant("m2", "two")])
    hook(env, base("Stop", transcript_path=str(tp), last_assistant_message="two"))
    hook(env, base("Stop", transcript_path=str(tp), last_assistant_message="two"))
    texts = [e["payload"]["text"] for e in synapse.wait(6) if e["type"] == "message"]
    assert texts == ["one", "two"]


def test_a_final_reply_not_yet_in_the_transcript_comes_from_the_hook_and_isnt_repeated(tmp_path, synapse):
    """Seen on 2.1.312: a subagent's last reply reaches its transcript after SubagentStop."""
    env = act_env(tmp_path, synapse.url)
    main = tmp_path / "proj" / f"{SID}.jsonl"
    sub = main.parent / SID / "subagents" / "agent-sa1.jsonl"
    transcript(sub, [assistant("s0", "working on it")])
    hook(env, base("SubagentStop", agent_id="sa1", agent_type="Explore", transcript_path=str(main),
                   agent_transcript_path=str(sub), last_assistant_message="listed"))
    transcript(sub, [assistant("s1", "listed")])  # written after the hook
    hook(env, base("PostToolUse", agent_id="sa1", tool_name="Read", tool_use_id="r9", tool_input={},
                   tool_response="", transcript_path=str(main)))
    msgs = [(e["agent_id"], e["payload"]["text"]) for e in synapse.wait(4) if e["type"] == "message"]
    assert msgs == [("sa1", "working on it"), ("sa1", "listed")]


def test_partial_last_line_waits_for_the_next_tail(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    line = json.dumps(assistant("m1", "complete later"))
    with tp.open("a") as f:
        f.write(line[:20])
    hook(env, base("PostToolUse", tool_name="Read", tool_use_id="r1", tool_input={}, tool_response="", transcript_path=str(tp)))
    with tp.open("a") as f:
        f.write(line[20:] + "\n")
    hook(env, base("PostToolUse", tool_name="Read", tool_use_id="r2", tool_input={}, tool_response="", transcript_path=str(tp)))
    texts = [e["payload"]["text"] for e in synapse.wait(3) if e["type"] == "message"]
    assert texts == ["complete later"]


def test_replies_are_redacted(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    transcript(tp, [assistant("m1", "set MY_SECRET_TOKEN=zz9 in the env")])
    hook(env, base("Stop", transcript_path=str(tp)))
    (msg,) = [e for e in synapse.wait(2) if e["type"] == "message"]
    assert "zz9" not in msg["payload"]["text"] and msg["payload"]["text"].startswith("set MY_SECRET_TOKEN=")


def test_session_start_on_resume_keeps_the_offset(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    transcript(tp, [assistant("m1", "before resume")])
    hook(env, base("SessionStart", source="resume", transcript_path=str(tp)))
    hook(env, base("Stop", transcript_path=str(tp)))
    texts = [e["payload"]["text"] for e in synapse.wait(3) if e["type"] == "message"]
    assert texts == ["before resume"]


def test_big_batches_are_split_to_fit_the_server(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    transcript(tp, [assistant(f"m{i}", ("reply %d " % i) * 1000) for i in range(20)])
    hook(env, base("Stop", transcript_path=str(tp)))
    msgs = [e for e in synapse.wait(21) if e["type"] == "message"]
    assert len(msgs) == 20
    assert all(len(r["raw"].encode()) <= 64 * 1024 and len(r["json"]["events"]) <= 100 for r in synapse.requests)
