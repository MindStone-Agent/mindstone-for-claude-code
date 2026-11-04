"""Fleet activity layer, reporter side (synapse#31 scope change).

Hook inputs are shaped from real captures on Claude Code 2.1.312 (PreToolUse / PostToolUse /
PostToolUseFailure inside and outside a subagent, SubagentStart / Stop, transcripts with
`assistant`, `user` and `api-request-blob` lines).

    cd orchestrator && python -m pytest -q tests/test_fleet_activity.py
"""
from __future__ import annotations

import json
import re
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
            # a user line that passes the cheap byte filter (a nested "type": "assistant" key)
            # and carries a text block: only the top-level type check keeps it out
            {"type": "user", "origin": {"type": "assistant"}, "uuid": "u2",
             "message": {"role": "user", "content": [{"type": "text", "text": PROMPT}]}},
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
    ("Glob", {"pattern": "**/*.py", "path": "src"}, "**/*.py\nsrc (path)"),
    ("Grep", {"pattern": "TODO", "glob": "*.ts"}, "TODO\n*.ts (glob)"),
    ("WebFetch", {"url": "https://example.com/x", "prompt": PROMPT}, "https://example.com/x"),
    ("WebSearch", {"query": "python flock"}, "python flock"),
    ("Agent", {"subagent_type": "Explore", "description": "find tests", "prompt": "look in tests/"}, "Explore\nfind tests\n\nlook in tests/"),
    ("Agent", {"subagent_type": "Explore", "description": "find tests"}, "Explore\nfind tests"),
    ("mcp__x__y", {"a": 1}, '{"a": 1}'),
])
def test_input_preview_per_tool(name, ti, want):
    assert A.input_text(name, ti) == want


def test_edit_and_write_inputs_are_summaries():
    """The bodies show in tool.finished (16 KB), not in the 4 KB input preview."""
    assert SECRET not in A.input_text("Write", {"file_path": "/f", "content": SECRET})
    assert SECRET not in A.input_text("Edit", {"file_path": "/f", "old_string": SECRET, "new_string": SECRET})


def test_bash_output_joins_stdout_and_stderr():
    assert A.output_text("Bash", {"stdout": "out", "stderr": "err"}) == "out\n[stderr]\nerr"
    assert A.output_text("Bash", {"stdout": "", "stderr": "err"}) == "[stderr]\nerr"
    assert A.output_text("mcp__x__y", "plain") == "plain"


# --- redaction and cuts -----------------------------------------------------------------------

@pytest.mark.parametrize("raw,secret", [
    ("Authorization: Basic dXNlcjpwYXNzd29yZDEy", "dXNlcjpw"),
    ('curl -H "Authorization: token abc123" https://x', "abc123"),
    ("MY_SECRET_TOKEN=zz9 make", "zz9"),
    ('DB_PASSWORD="hunter two"', "hunter two"),
    # built at runtime so the repo's privacy scan doesn't flag these fake fixtures
    ("export GITHUB_TOKEN=" + "ghp" + "_aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5", "ghp_aB3"),
    ("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMw\n-----END OPENSSH "
     + "PRIVATE KEY-----", "b3BlbnNz"),
    ("postgres://u:pa55word@db:5432/x", "pa55word"),
])
def test_previews_are_redacted(raw, secret):
    assert secret not in clean(raw)


# KEYBOARD=ok is redacted, on purpose: the server's rule (taken verbatim) matches KEY anywhere
# in an all-caps name, and over-redacting is the safe direction.
@pytest.mark.parametrize("raw", ["PATH=/usr/bin:/bin", "ls -la && echo done", "HOME=/Users/x"])
def test_ordinary_text_survives_redaction(raw):
    assert clean(raw) == raw


def test_long_output_keeps_head_and_tail_and_drops_split_words():
    raw = "HEAD " + "word " * 10000 + "TAIL"
    text, cut = preview(raw, 2000)
    assert cut and len(text) <= 2000 and text.startswith("HEAD ") and text.endswith("TAIL") and "cut to 2,000" in text
    for piece in text.replace("\n", " ").split():
        assert piece in ("HEAD", "TAIL", "word", "…") or piece.startswith(("[", "chars")) or piece.replace(",", "").rstrip("]").isdigit() \
            or piece in ("cut", "to", "bytes]"), f"split fragment {piece!r} survived the cut"


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
    tool_started, started = synapse.wait(2)
    assert started["type"] == "subagent.started" and started["agent_id"] == "a1"
    assert started["payload"] == {"agent_type": "general-purpose", "parent_tool_use_id": "toolu_AG1", "description": "check files"}
    assert tool_started["payload"]["input_preview"] == "general-purpose\ncheck files\n\n" + PROMPT


def test_pairing_happens_only_when_one_call_of_the_type_is_pending(tmp_path, synapse):
    """Round 2, S2-1/S2-2: with two calls of a type pending (a denied one, or parallel calls
    whose hooks race), "the oldest" is a guess the server can't undo. No parent instead."""
    env = act_env(tmp_path, synapse.url)
    for tid, typ in (("c1", "Explore"), ("c2", "general-purpose"), ("c3", "Explore")):
        hook(env, base("PreToolUse", tool_name="Agent", tool_use_id=tid, tool_input={"subagent_type": typ, "description": tid}))
    for aid, typ in (("g1", "general-purpose"), ("e1", "Explore")):
        hook(env, base("SubagentStart", agent_id=aid, agent_type=typ))
    hook(env, base("PostToolUse", tool_name="Agent", tool_use_id="c1", tool_input={},
                   tool_response={"status": "completed", "content": []}))
    hook(env, base("SubagentStart", agent_id="e2", agent_type="Explore"))
    starts = [e for e in synapse.wait(9) if e["type"] == "subagent.started"]
    assert [(e["agent_id"], e["payload"].get("parent_tool_use_id")) for e in starts] == [
        ("g1", "c2"), ("e1", None), ("e2", "c3")]


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



# --- activity review round 1 ------------------------------------------------------------------

# Shaped like the real 2.1.312 PostToolUse tool_response of each tool (captures and transcripts'
# toolUseResult). Hearth's ruling (#devops 2026-11-03): cap and redact, don't hide; Read's body
# and the user's own answers are the exclusions.
FILE_BODY = "FILE_BODY_CANARY_x81"
ANSWER = "ANSWER_CANARY_77"
POST_RESPONSES = {
    "Agent": ({"status": "completed", "prompt": PROMPT, "agentId": "a1", "agentType": "Explore",
               "content": [{"type": "text", "text": "found 3 tests"}], "totalTokens": 10}, {}),
    "Write": ({"type": "create", "filePath": "/p/a.py", "content": "a\nb\n", "structuredPatch": [], "originalFile": None}, {}),
    "Edit": ({"filePath": "/p/b.py", "oldString": "x = 1", "newString": "x = 2", "originalFile": FILE_BODY,
              "structuredPatch": [], "userModified": False, "replaceAll": False}, {}),
    "MultiEdit": ({"filePath": "/p/e.py"}, {"file_path": "/p/e.py", "edits": [{"old_string": "a", "new_string": "b"},
                                                                          {"old_string": "c", "new_string": "d"}]}),
    "Read": ({"type": "text", "file": {"filePath": "/p/c.py", "content": FILE_BODY, "numLines": 3, "startLine": 1, "totalLines": 9}}, {}),
    "AskUserQuestion": ({"questions": [{"question": "Which?"}], "answers": {"Which?": ANSWER}, "annotations": {}}, {}),
    "SendMessage": ({"success": True, "message": "queued"}, {}),
    "mcp__x__y": ({"result": "ok", "nested": {"prompt": "p"}}, {}),
}
WANT_OUTPUT = {"Agent": "[completed]\nfound 3 tests", "Write": "/p/a.py\ncreate, 2 lines, 4 bytes\na\nb\n",
               "Edit": "/p/b.py\n--- old\nx = 1\n+++ new\nx = 2",
               "MultiEdit": "/p/e.py\n--- old\na\n+++ new\nb\n--- old\nc\n+++ new\nd",
               "Read": "3 lines of 9", "AskUserQuestion": "answered:\nWhich?",
               "SendMessage": '{"success": true, "message": "queued"}', "mcp__x__y": '{"result": "ok", "nested": {"prompt": "p"}}'}


@pytest.mark.parametrize("tool", sorted(POST_RESPONSES))
def test_tool_output_is_shaped_per_tool(tmp_path, synapse, tool):
    """Round 1, finding 1 (sev-1): tool.finished dumped tool_response whole and uncapped per
    field. Each tool now has its own shape; Read's body and the user's answers never leave."""
    env = act_env(tmp_path, synapse.url)
    resp, ti = POST_RESPONSES[tool]
    hook(env, base("PostToolUse", tool_name=tool, tool_use_id="t1", tool_input=ti, tool_response=resp))
    (ev,) = synapse.wait(1)
    assert ev["type"] == "tool.finished" and ev["payload"]["output_preview"] == WANT_OUTPUT[tool]
    raw = json.dumps(synapse.requests)
    assert FILE_BODY not in raw and ANSWER not in raw and PROMPT not in raw


def test_edit_caps_old_and_new_separately():
    """A long old string can't push the new one out of the preview."""
    text = A.output_text("Edit", {"filePath": "/p", "oldString": "old " * 20000, "newString": "NEWTEXT"})
    out, cut = preview(text, A.OUTPUT_MAX)
    assert "+++ new\nNEWTEXT" in out and not cut


def test_write_shows_head_and_tail_of_the_content(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    body = "FIRST line\n" + "middle\n" * 20000 + "LAST line\n"
    hook(env, base("PostToolUse", tool_name="Write", tool_use_id="w1", tool_input={"file_path": "/p", "content": body},
                   tool_response={"type": "create", "filePath": "/p", "content": body}))
    (ev,) = synapse.wait(1)
    out = ev["payload"]["output_preview"]
    assert ev["payload"]["output_truncated"] and "FIRST line" in out and "LAST line" in out


@pytest.mark.parametrize("tool,ti,want", [
    ("SendMessage", {"to": "researcher", "message": "check X"}, '{"to": "researcher", "message": "check X"}'),
    ("ScheduleWakeup", {"delaySeconds": 60, "prompt": "/loop"}, '{"delaySeconds": 60, "prompt": "/loop"}'),
    ("AskUserQuestion", {"questions": [{"question": "Which?"}], "answers": {"Which?": ANSWER}}, '{"questions": [{"question": "Which?"}]}'),
])
def test_other_inputs_are_compact_json_without_the_users_answers(tool, ti, want):
    assert A.input_text(tool, ti) == want


def test_env_secret_rule_is_linear_on_hostile_input():
    """Round 1, finding 2: one long underscore-joined run with no `=` was quadratic (4.3 s for
    66 KB), long enough for the send deadline to kill the tail on every later hook."""
    for raw in ("my_api_key_" * 6000, "KEY_" * 64000, "a.b-c_" * 45000, "x" * 200 + "=" * 1):
        t = time.perf_counter()
        preview(raw, A.OUTPUT_MAX)
        assert time.perf_counter() - t < 0.5, raw[:20]


def _pem_b64() -> str:
    import base64
    body = "MIIEowIBAAKCAQEAtest" * 20
    return base64.b64encode(("-----BEGIN RSA " + "PRIVATE KEY-----\n" + body + "\n-----END RSA " + "PRIVATE KEY-----\n").encode()).decode()


@pytest.mark.parametrize("raw,secret", [
    ("curl --user admin:hunter2 https://x", "hunter2"),
    ("curl -u 'admin:Tr0ub4dor&3' https://x", "Tr0ub4dor"),
    ("docker login -u bob -p S3cretPass1 reg.io", "S3cretPass1"),
    ("sshpass -p hunter2 ssh h", "hunter2"),
    ("redis-cli -h x -a hunter2 ping", "hunter2"),
    ("openssl enc -aes256 -pass pass:hunter2 -in f", "hunter2"),
    ("htpasswd -b f bob hunter2", "hunter2"),
    ("htpasswd -nb bob hunter2", "hunter2"),
    ("mysql -u root -phunter2 db", "hunter2"),
    ("aws configure set aws_secret_access_key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtn"),
    ("Cookie: sessionid=abc123def", "abc123def"),
    ("Set-Cookie: session=abc123; Path=/", "abc123"),
    ("X-Api-Key: abcd1234", "abcd1234"),
    ('curl -H "X-Auth-Token: tok3n" x', "tok3n"),
    ("data:\n  tls.key: " + _pem_b64(), _pem_b64()[:24]),
    ("blob " + _pem_b64(), _pem_b64()[:24]),
    ("STRIPE_SK=" + "sk" + "_live_51H8abcdefghijk", "51H8abcdef"),
    ("charge with " + "sk" + "_test_51H8abcdefghijk", "51H8abcdef"),
    ("machine h login bob password hunter2pass", "hunter2pass"),
    ('{"token": "abc123"}', "abc123"),
    ("api_key: s3cr3tvalue", "s3cr3tvalue"),
    ("post to https://hooks." + "slack.com/services/T000/B000/XXXXsecretXXXX", "XXXXsecret"),  # split: privacy scan
    ("DD_APP=0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123"),
])
def test_round1_redaction_shapes(raw, secret):
    """Round 1, finding 4: each of these went through preview() unchanged."""
    assert secret not in clean(raw)


@pytest.mark.parametrize("raw", ["git push -u origin main", "docker run -p 8080:80 img", "ssh -p 22 host", "sort -u f",
                                 "monkey: banana", "url: https://x.y/z", "see the password reset flow", "keys: []",
                                 "COMMIT=0123456789abcdef0123456789abcdef01234567 is lowercase-free"])
def test_round1_rules_leave_ordinary_commands_alone(raw):
    if raw.startswith("COMMIT="):  # an all-caps NAME= with 32+ hex is redacted by design
        assert "0123456789abcdef" not in clean(raw)
    else:
        assert clean(raw) == raw


@pytest.mark.parametrize("ch", ["漢", "─", "\x01"])
def test_non_ascii_output_fits_the_server(tmp_path, synapse, ch):
    """Round 1, finding 5: 16,380 CJK chars escaped to an 89 KB body; the server answered 413
    and dropped the batch. Previews are capped by their encoded size, bodies are UTF-8."""
    env = act_env(tmp_path, synapse.url)
    hook(env, base("PostToolUse", tool_name="Bash", tool_use_id="t1", tool_input={"command": "x"},
                   tool_response={"stdout": ch * 16380, "stderr": ""}))
    (ev,) = synapse.wait(1)
    assert ev["type"] == "tool.finished" and ev["payload"]["output_truncated"] is True
    assert all(len(r["raw"].encode()) <= 64 * 1024 for r in synapse.requests)
    assert len(json.dumps(ev["payload"]["output_preview"], ensure_ascii=False).encode()) - 2 <= A.OUTPUT_MAX


def test_a_denied_agent_call_never_lends_its_id_to_the_next_subagent(tmp_path, synapse):
    """Round 1 #6 and round 2 S2-1: Claude Code sends no PostToolUse for a denied call. The
    reviewer's case: A denied, B runs, b starts. b must not get A's id."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    hook(env, base("PreToolUse", tool_name="Agent", tool_use_id="toolu_A_denied", transcript_path=str(tp),
                   tool_input={"subagent_type": "Explore", "description": "first try"}))
    hook(env, base("PreToolUse", tool_name="Agent", tool_use_id="toolu_B_runs", transcript_path=str(tp),
                   tool_input={"subagent_type": "Explore", "description": "second try"}))
    hook(env, base("SubagentStart", agent_id="agent_b", agent_type="Explore"))
    starts = [e for e in synapse.wait(4) if e["type"] == "subagent.started"]
    assert [e["payload"].get("parent_tool_use_id") for e in starts] == [None]


DENIAL = {"type": "user", "uuid": "u9", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_D", "is_error": True,
     "content": "The user doesn't want to proceed. DENIAL_FEEDBACK_CANARY"}]}}


def test_a_denied_call_is_closed_from_the_transcript_without_its_text(tmp_path, synapse):
    """Round 2, S2-3: tool.started went out, no PostToolUse ever came, and the call showed as
    running for ever. The next tail closes it; the user's denial text is not sent."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    hook(env, base("PreToolUse", tool_name="Bash", tool_use_id="toolu_D", tool_input={"command": "rm -rf x"},
                   transcript_path=str(tp)))
    transcript(tp, [DENIAL])
    hook(env, base("PreToolUse", tool_name="Read", tool_use_id="toolu_E", tool_input={"file_path": "/a"},
                   transcript_path=str(tp)))
    hook(env, base("Stop", transcript_path=str(tp)))  # a second tail: closed once only
    done = [e["payload"] for e in synapse.wait(6) if e["type"] == "tool.finished"]
    assert done == [{"tool_use_id": "toolu_D", "tool_name": "Bash", "ok": False, "error": "not run (denied or blocked)"}]
    assert "DENIAL_FEEDBACK_CANARY" not in json.dumps(synapse.requests)


def test_an_error_result_for_a_call_already_finished_is_not_closed_twice(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    hook(env, base("PreToolUse", tool_name="Bash", tool_use_id="toolu_D", tool_input={"command": "false"},
                   transcript_path=str(tp)))
    hook(env, base("PostToolUseFailure", tool_name="Bash", tool_use_id="toolu_D", tool_input={}, error="exit 1",
                   transcript_path=str(tp)))
    transcript(tp, [DENIAL])
    hook(env, base("Stop", transcript_path=str(tp)))
    done = [e["payload"] for e in synapse.wait(5) if e["type"] == "tool.finished"]
    assert [d["error"] for d in done] == ["exit 1"]


def test_an_untyped_agent_call_is_general_purpose_not_a_wildcard(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    hook(env, base("PreToolUse", tool_name="Agent", tool_use_id="U", tool_input={"description": "untyped"}))
    hook(env, base("SubagentStart", agent_id="e1", agent_type="Explore"))
    hook(env, base("SubagentStart", agent_id="g1", agent_type="general-purpose"))
    starts = [e for e in synapse.wait(3) if e["type"] == "subagent.started"]
    assert [(e["agent_id"], e["payload"].get("parent_tool_use_id")) for e in starts] == [("e1", None), ("g1", "U")]


def test_an_old_agent_call_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(A.c, "STATE_DIR", tmp_path / "state")
    A.remember_agent_call("S", {"tool_use_id": "old", "tool_input": {"subagent_type": "Explore"}})
    monkeypatch.setattr(A.time, "time", lambda real=time.time: real() + A.PAIR_TTL_S + 1)
    assert A.pair_subagent("S", "Explore") == (None, None)


def test_a_background_agent_keeps_its_call_until_its_subagent_starts(tmp_path, synapse):
    """A background Agent's PostToolUse comes at launch, possibly before SubagentStart."""
    env = act_env(tmp_path, synapse.url)
    hook(env, base("PreToolUse", tool_name="Agent", tool_use_id="BG", tool_input={"subagent_type": "Explore", "description": "bg"}))
    hook(env, base("PostToolUse", tool_name="Agent", tool_use_id="BG", tool_input={},
                   tool_response={"isAsync": True, "status": "async_launched", "agentId": "bg1", "prompt": PROMPT}))
    hook(env, base("SubagentStart", agent_id="bg1", agent_type="Explore"))
    (start,) = [e for e in synapse.wait(3) if e["type"] == "subagent.started"]
    assert start["payload"]["parent_tool_use_id"] == "BG"


def test_a_reply_repeated_in_a_later_turn_is_sent_again(tmp_path, synapse):
    """Round 1, finding 8: digests of sent texts suppressed the same words in a later turn."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    for turn in ("1", "2"):
        transcript(tp, [assistant(f"a{turn}", "Let me check the tests."), assistant(f"b{turn}", "Done.")])
        hook(env, base("Stop", transcript_path=str(tp), last_assistant_message="Done."))
    msgs = [e["payload"] for e in synapse.wait(4) if e["type"] == "message"]
    assert [m["text"] for m in msgs] == ["Let me check the tests.", "Done."] * 2
    assert len({m["message_id"] for m in msgs}) == 4


def test_a_repeated_final_reply_from_the_hook_gets_a_fresh_id(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    main = tmp_path / "proj" / f"{SID}.jsonl"
    sub = main.parent / SID / "subagents" / "agent-sa1.jsonl"
    transcript(sub, [])
    for turn in ("1", "2"):
        hook(env, base("SubagentStop", agent_id="sa1", agent_type="Explore", transcript_path=str(main),
                       last_assistant_message="Done."))
        transcript(sub, [assistant(f"s{turn}", "Done."), assistant(f"w{turn}", "next")])
    hook(env, base("PostToolUse", agent_id="sa1", tool_name="Read", tool_use_id="r9", tool_input={},
                   tool_response="", transcript_path=str(main)))
    msgs = [e["payload"] for e in synapse.wait(6) if e["type"] == "message"]
    assert [m["text"] for m in msgs] == ["Done.", "next", "Done.", "next"]
    assert len({m["message_id"] for m in msgs}) == 4


def test_a_bad_transcript_line_never_wedges_the_tail_or_its_events(tmp_path, synapse):
    """Round 1, finding 9: a non-object assistant line raised, the offset never advanced, and
    the tool.finished bundled with the tail was lost on every later hook."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    with tp.open("a") as f:
        f.write('[{"type":"assistant"}]\n"type":"assistant"\n')
    transcript(tp, [assistant("m1", "after the bad lines")])
    hook(env, base("PostToolUse", tool_name="Read", tool_use_id="r1", tool_input={}, tool_response="", transcript_path=str(tp)))
    types = [(e["type"], e["payload"].get("text")) for e in synapse.wait(3) if e["type"] != "session.started"]
    assert types == [("tool.finished", None), ("message", "after the bad lines")]


def test_events_are_sent_even_when_the_tail_fails(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    tp = tmp_path / "proj" / f"{SID}.jsonl"
    tp.mkdir(parents=True)  # a directory where the transcript should be: stat works, open fails
    hook(env, base("PostToolUse", tool_name="Read", tool_use_id="r1", tool_input={}, tool_response="", transcript_path=str(tp)))
    assert [e["type"] for e in synapse.wait(1)] == ["tool.finished"]


def test_state_survives_a_kill_between_write_and_truncate(tmp_path, monkeypatch):
    """Round 1, finding 10: truncate-then-write left an empty file if killed in between."""
    monkeypatch.setattr(A.c, "STATE_DIR", tmp_path / "state")
    fd, _ = A._locked_json("S", "k")
    A._write_locked(fd, {"offset": 123456789, "path": "/a/long/path/to/a/transcript.jsonl", "pad": "x" * 50})
    os_close = __import__("os").close
    os_close(fd)
    fd, _ = A._locked_json("S", "k")
    real = A.os.ftruncate
    monkeypatch.setattr(A.os, "ftruncate", lambda *a: None)  # killed before the truncate
    A._write_locked(fd, {"offset": 7})
    monkeypatch.setattr(A.os, "ftruncate", real)
    os_close(fd)
    fd, data = A._locked_json("S", "k")
    os_close(fd)
    assert data == {"offset": 7}


def test_a_word_straddling_the_cut_is_dropped_whole():
    """Round 1, finding 11: the old test couldn't fail (scrub replaced AKIA before the cut).
    A plain word the scrubber leaves alone shows whether the cut drops split words."""
    word = "straddlingwordthatnoscrubbertouches"
    raw = "x " * 3000 + word + " " + "y " * 3000
    for limit in range(1000, 1200, 7):
        text, cut = preview(raw, limit)
        assert cut
        for piece in text.split():
            assert piece in ("x", "y", word, "…") or not any(c.isalpha() for c in piece.strip("[],.…")) \
                or piece in ("chars,", "cut", "to", "bytes]"), (limit, piece)


def test_minified_output_is_cut_at_punctuation_not_dropped():
    """Round 1, finding 12: output with no whitespace collapsed to just the marker."""
    mj = json.dumps([{"id": i, "name": f"item{i}"} for i in range(2000)], separators=(",", ":"))
    text, cut = preview(mj, 4000)
    assert cut and len(text) > 2000 and text.startswith('[{"id":0') and text.endswith("}]")


def test_replies_carry_the_time_they_were_written_and_are_tailed_before_a_tool(tmp_path, synapse):
    """Round 1, finding 13: replies were stamped at tail time, after the tool they announce."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    line = assistant("m1", "Let me run the tests.")
    line["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 30))
    transcript(tp, [line])
    hook(env, base("PreToolUse", tool_name="Bash", tool_use_id="b1", tool_input={"command": "pytest"}, transcript_path=str(tp)))
    started, msg = [e for e in synapse.wait(3) if e["type"] != "session.started"]
    assert (started["type"], msg["type"]) == ("tool.started", "message")
    assert msg["ts"] == line["timestamp"] and msg["ts"] < started["ts"]


def test_status_and_setup_know_about_activity(tmp_path):
    """Round 1, finding 14 (and setup dropping `activity`, found while fixing it)."""
    sys.path.insert(0, str(ORCH.parent))
    from orchestrator.integrations.synapse import cli
    toml = tmp_path / "s.toml"
    toml.write_text('[synapse]\nbase_url = "http://10.0.0.5"\nhandle = "p"\n[fleet]\nenabled = true\nactivity = true\n')
    assert cli._fleet_status(toml, "http://10.0.0.5")[1].startswith("OFF")
    assert cli._fleet_status(toml, "https://s.example") == ("on", "on")
    assert "activity = true" in cli._kept_fleet_lines(toml)


def test_activity_to_localhost_never_goes_through_a_proxy(tmp_path, synapse):
    """Round 1, finding 15: urllib honours http_proxy, so plain-http activity could leave the box."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    env = act_env(tmp_path, synapse.url)
    env.update(http_proxy=f"http://127.0.0.1:{dead}", HTTP_PROXY=f"http://127.0.0.1:{dead}")
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    hook(env, base("PostToolUse", tool_name="Read", tool_use_id="r1", tool_input={}, tool_response=""))
    assert [e["type"] for e in synapse.wait(1)] == ["tool.finished"]



def test_emit_later_sends_the_events_before_building_more(monkeypatch):
    """The events go out first: a tail that raises (or that the deadline kills) can't take
    tool.finished / turn.ended with it."""
    from fleet import common as C
    sent = []
    monkeypatch.setattr(C, "post", lambda cfg, batch: sent.append([e["type"] for e in batch]))
    monkeypatch.setattr(C, "detach", lambda fn: fn())
    monkeypatch.setenv("MS4CC_FLEET_INLINE", "1")
    monkeypatch.setenv("MS4CC_FLEET_AGENT", "plumb")

    def boom():
        raise RuntimeError("tail failed")
    C.emit_later(None, [{"type": "tool.finished"}], boom)
    assert sent == [["tool.finished"]]


def test_a_failed_state_write_keeps_the_old_state(tmp_path, monkeypatch):
    """Write first, truncate after: a write that never happens leaves the old state, not an
    empty file."""
    monkeypatch.setattr(A.c, "STATE_DIR", tmp_path / "state")
    fd, _ = A._locked_json("S", "k")
    A._write_locked(fd, {"offset": 5})
    __import__("os").close(fd)
    fd, _ = A._locked_json("S", "k")

    def no_write(*a):
        raise OSError("killed")
    monkeypatch.setattr(A.os, "write", no_write)
    with pytest.raises(OSError):
        A._write_locked(fd, {"offset": 9})
    monkeypatch.undo()
    __import__("os").close(fd)
    monkeypatch.setattr(A.c, "STATE_DIR", tmp_path / "state")
    fd, data = A._locked_json("S", "k")
    __import__("os").close(fd)
    assert data == {"offset": 5}


def test_an_unbroken_token_cut_by_the_limit_is_dropped_whole():
    text, cut = preview("z" * 40000, 1000)  # not hex: the scrubber leaves it alone
    assert cut and "z" not in text


def test_an_owed_reply_is_skipped_once_only(tmp_path, synapse):
    """A final reply sent from the hook skips its transcript line once; the same words written
    again later are a new reply and are sent."""
    env = act_env(tmp_path, synapse.url)
    main = tmp_path / "proj" / f"{SID}.jsonl"
    sub = main.parent / SID / "subagents" / "agent-sa1.jsonl"
    transcript(sub, [])
    hook(env, base("SubagentStop", agent_id="sa1", agent_type="Explore", transcript_path=str(main),
                   last_assistant_message="Done."))
    transcript(sub, [assistant("s1", "Done.")])  # the owed line: skipped
    tool = base("PostToolUse", agent_id="sa1", tool_name="Read", tool_use_id="r9", tool_input={}, tool_response="",
                transcript_path=str(main))
    hook(env, tool)
    transcript(sub, [assistant("s2", "Done.")])  # said again later: sent
    hook(env, dict(tool, tool_use_id="r10"))
    msgs = [e["payload"] for e in synapse.wait(5) if e["type"] == "message"]
    assert [(m["message_id"].split(".")[0], m["text"]) for m in msgs] == [("last", "Done."), ("s2", "Done.")]


# --- shared fixtures with the server (synapse tests/fixtures/fleet_secrets.json) ---------------
# Stored base64-encoded so the repo's privacy scan doesn't flag the fake secrets; byte-identical
# to synapse's copy at synapse#40 (sha256 below). Refresh both together.
FLEET_SECRETS_SHA256 = "ad049d690c637abf0b9054a13145210d36f6556f64eefad5095c846adca318f2"


def _shared_fixtures() -> dict:
    import base64
    import hashlib
    raw = base64.b64decode((Path(__file__).parent / "fixtures" / "fleet_secrets.json.b64").read_text())
    assert hashlib.sha256(raw).hexdigest() == FLEET_SECRETS_SHA256
    return json.loads(raw)


@pytest.mark.parametrize("case", _shared_fixtures()["secret"], ids=lambda c: c["name"])
def test_shared_fixture_secrets_never_survive(case):
    assert case["leak"] not in clean(case["input"])


@pytest.mark.parametrize("case", _shared_fixtures()["safe"], ids=lambda c: c["name"])
def test_shared_fixture_safe_text_is_unchanged(case):
    assert clean(case["input"]) == case["input"]


# synapse#40: every fixture inside JSON (a value, pretty-printed, NDJSON, with a footer). JSON
# escaping hid 15 of 89 from this reporter's line rules before the vendored redaction decoded
# JSON string literals (Plumb, MindStone #290 round 3).
@pytest.mark.parametrize("case", _shared_fixtures()["secret_in_json"], ids=lambda c: c["name"])
def test_shared_fixture_secrets_inside_json_never_survive(case):
    out = clean(case["input"])
    assert case["leak"] not in out
    assert case["leak_plain"] not in out  # the value as it reads once decoded


@pytest.mark.parametrize("case", _shared_fixtures()["safe_in_json"], ids=lambda c: c["name"])
def test_shared_fixture_safe_text_inside_json_is_unchanged(case):
    assert clean(case["input"]) == case["input"]


def test_scrubbing_many_distinct_secrets_stays_bounded():
    """Cairn's round-2 measurement: the scrubber re-scans the whole text once per secret value
    it finds (2.4 s for 108 KB here, quadratic). It runs on 1 KB chunks."""
    lines = "".join(f"VAR{i}_TOKEN=val{i:06d}xyzSECRET{i}\n" for i in range(3000))
    t = time.perf_counter()
    text, _ = preview(lines, A.OUTPUT_MAX)
    assert time.perf_counter() - t < 1.0
    assert "xyzSECRET" not in text


SERVER_REDACT_SHA256 = "a74bc722b407ab68ca775f9355a31b28aba0ab820e93866371acae04a557cfe4"  # synapse#40


def test_the_vendored_server_redaction_is_verbatim():
    """fleet_redact_vendor.py is synapse's api/fleet_redact.py with only its scrubber import
    changed; undoing that one change must give the pinned file byte for byte."""
    import hashlib
    text = (ORCH / "fleet" / "fleet_redact_vendor.py").read_text()
    ours = ("import sys as _sys  # vendored change: the scrubber is hooks/scrubber.py here\n"
            "from pathlib import Path as _Path\n"
            "_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / \"hooks\"))\n"
            "from scrubber import _propagatable, scrub_collect  # noqa: E402\n")
    assert text.count(ours) == 1
    original = text.replace(ours, "from api.vendor.scrubber import _propagatable, scrub_collect\n")
    assert hashlib.sha256(original.encode()).hexdigest() == SERVER_REDACT_SHA256


# --- activity review round 2 ------------------------------------------------------------------

_SINGLE_LINE = [c for c in _shared_fixtures()["secret"] if "\n" not in c["input"]]


@pytest.mark.parametrize("case", _SINGLE_LINE, ids=lambda c: c["name"])
def test_a_secret_across_the_scrub_chunk_cut_never_survives(case):
    """Round 2, S1-1: with no whitespace near it, a 1 KB chunk cut lands inside the secret and
    the scrubber sees two harmless halves. Every single-line fixture, across offset 1024."""
    s = case["input"]
    base, _ = preview("," + s + ",", A.OUTPUT_MAX)  # no cut: the rules themselves catch it
    assert case["leak"] not in base
    for k in (1, len(s) // 2, len(s) - 1):  # the cut falls k characters into the secret
        raw = "," * (1024 - k) + s + "," * 1200
        text, _ = preview(raw, A.OUTPUT_MAX)
        assert case["leak"] not in text, (k, case["name"])


@pytest.mark.parametrize("case", _shared_fixtures()["secret"], ids=lambda c: c["name"])
def test_a_secret_inside_compact_json_never_survives(case):
    """Round 2, S1-2: an unknown tool's JSON was dumped, then redacted, so newlines became
    literal \\n and quotes \\", which line-anchored and quoted-key rules can't see."""
    for text in (A.output_text("mcp__x__y", {"result": case["input"]}),
                 A.input_text("mcp__x__y", {"args": {"body": case["input"]}})):
        out, _ = preview(text, A.OUTPUT_MAX)
        assert case["leak"] not in out, case["name"]



@pytest.mark.parametrize("raw,secret", [
    ('{"headers": {"Authorization": "Basic YWRtaW46aHVudGVyMg==", "Cookie": "session=s3ss10nV4lue"}}', "YWRtaW46"),
    ('{"headers": {"Authorization": "Basic YWRtaW46aHVudGVyMg==", "Cookie": "session=s3ss10nV4lue"}}', "s3ss10nV4lue"),
    ("{'Authorization': 'Basic YWRtaW46aHVudGVyMg=='}", "YWRtaW46"),
    ('"Authorization":"token abcdef123456"', "abcdef123456"),
    ('"Proxy-Authorization": "Basic cHJveHk6cGFzcw=="', "cHJveHk6"),
    ('"Set-Cookie": "sid=xyz987abc"', "xyz987abc"),
])
def test_credential_headers_as_dict_keys_are_redacted(raw, secret):
    """Round 2, S2-5: header rules need `Name:` with nothing in between, so the dict and JSON
    forms of the same headers went out whole."""
    assert secret not in clean(raw)
    assert secret not in (preview(A.output_text("mcp__http__get", json.loads(raw) if raw.startswith("{\"") else raw), A.OUTPUT_MAX)[0] or "")


# --- activity review round 3 ------------------------------------------------------------------

@pytest.mark.parametrize("password", ["correct.horse.battery.staple", "s3cr3t!P@ssw0rd!2024", "Xk2$mQ9#vLpq7Rz8wT"])
@pytest.mark.parametrize("key", ["DB_PASSWORD", "password"])
def test_a_punctuated_password_across_the_chunk_cut_leaves_no_fragment(password, key):
    """Round 3, finding 1: the chunk scrubber redacted the head of the value in one chunk; the
    rest, starting with punctuation, sat after the marker in the next chunk, where every rule
    skips text that follows [REDACTED. Docker-env shaped output, every cut alignment."""
    entries = "".join(f',"LOG_{i}=info"' for i in range(80))
    for pad in range(900, 1060):
        raw = '["PATH=/usr/bin","NOTE=' + "x" * pad + f'","{key}={password}"' + entries + "]"
        text, _ = preview(A.output_text("Bash", {"stdout": raw, "stderr": ""}), A.OUTPUT_MAX)
        for i in range(len(password) - 3):
            assert password[i:i + 4] not in text, (pad, password[i:i + 4])


@pytest.mark.parametrize("eol", ["\r\n", "\r"], ids=["crlf", "cr"])
@pytest.mark.parametrize("case", _shared_fixtures()["secret"], ids=lambda c: c["name"])
def test_a_secret_in_crlf_text_never_survives(case, eol):
    """Round 3, finding 2: the line-anchored rules end in $, which doesn't match before \r, so
    10 fixtures leaked once Windows line endings were used (7 with lone CRs, round 4 N1)."""
    out, _ = preview(case["input"].replace("\n", eol) + eol, A.OUTPUT_MAX)
    assert case["leak"] not in out



def _blocked(tool_use_id: str) -> dict:
    return {"type": "user", "uuid": "u-" + tool_use_id, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True, "content": "PreToolUse:Agent hook error"}]}}


def test_the_retry_after_a_blocked_agent_call_keeps_its_parent(tmp_path, synapse):
    """Round 3, finding 4: A is blocked, the model retries at once as B, and B's subagent
    starts before any tail has forgotten A. A's error result is already in the transcript."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    hook(env, base("PreToolUse", tool_name="Agent", tool_use_id="toolu_A", transcript_path=str(tp),
                   tool_input={"subagent_type": "Explore", "description": "first try"}))
    hook(env, base("PreToolUse", tool_name="Agent", tool_use_id="toolu_B", transcript_path=str(tp),
                   tool_input={"subagent_type": "Explore", "description": "second try"}))
    # A's error result, written before B existed in reality, is seen here only after B's own
    # tail ran: in production that tail runs detached and SubagentStart can beat it.
    transcript(tp, [_blocked("toolu_A")])
    hook(env, base("SubagentStart", agent_id="agent_b", agent_type="Explore", transcript_path=str(tp)))
    (start,) = [e for e in synapse.wait(6) if e["type"] == "subagent.started"]
    assert (start["payload"].get("parent_tool_use_id"), start["payload"].get("description")) == ("toolu_B", "second try")


def test_a_failure_that_lands_before_its_start_is_not_closed_twice(tmp_path, synapse):
    """Round 3, finding 3: the hooks are async, so PostToolUseFailure can run before the
    PreToolUse that opens the call; the later tail then closed it again with a made-up label."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    hook(env, base("PostToolUseFailure", tool_name="mcp__x__y", tool_use_id="toolu_D", tool_input={},
                   error="MCP error -32000: boom", transcript_path=str(tp)))
    hook(env, base("PreToolUse", tool_name="mcp__x__y", tool_use_id="toolu_D", tool_input={}, transcript_path=str(tp)))
    transcript(tp, [_blocked("toolu_D")])
    hook(env, base("Stop", transcript_path=str(tp)))
    done = [e["payload"] for e in synapse.wait(5) if e["type"] == "tool.finished"]
    assert [d["error"] for d in done] == ["MCP error -32000: boom"]


def test_only_a_user_lines_error_result_closes_a_call(tmp_path, synapse):
    """api-request-blob lines also carry "is_error":true; only the user line's result counts."""
    env = act_env(tmp_path, synapse.url)
    tp = transcript(tmp_path / "proj" / f"{SID}.jsonl", [])
    hook(env, base("SessionStart", source="startup", transcript_path=str(tp)))
    hook(env, base("PreToolUse", tool_name="Bash", tool_use_id="toolu_Q", tool_input={"command": "x"}, transcript_path=str(tp)))
    blob = dict(_blocked("toolu_Q"), type="api-request-blob")
    transcript(tp, [blob])
    hook(env, base("Stop", transcript_path=str(tp)))
    assert [e for e in synapse.wait(3) if e["type"] == "tool.finished"] == []



# --- activity review round 4 ------------------------------------------------------------------

def _repeat_value(case):
    """The secret as a later bare repeat would carry it (echo $TOKEN, a second .env line): the
    whole token around the fixture's leak, a flag's value without the flag, a multi-word value
    as the rest of its line. The same helper as synapse's tests/test_fleet_redact.py."""
    inp, leak = case["input"], case["leak"]
    if " " in leak:
        line = next(x for x in inp.split("\n") if leak in x)
        return line.split(":", 1)[-1].split("=", 1)[-1].strip()
    delim = " \t\n\"',{}()[]<>\\|;"
    i = inp.find(leak)
    s, e = i, i + len(leak)
    while s > 0 and inp[s - 1] not in delim:
        s -= 1
    while e < len(inp) and inp[e] not in delim:
        e += 1
    value = inp[s:e]
    return value[2:] if value.startswith("-p") and len(value) > 8 else value


@pytest.mark.parametrize("case", _shared_fixtures()["secret"], ids=lambda c: c["name"])
def test_a_secret_repeated_bare_later_is_redacted_too(case):
    """Round 4, S1-1, and Hearth's blocker on both suites: a value found in its keyed form must
    also go where it is repeated bare later (echo $TOKEN, a second .env line). Values under 6
    characters or plain lowercase words aren't propagated (the scrubber's own rule), so those
    fixtures are skipped, as on the server."""
    sys.path.insert(0, str(ORCH / "hooks"))
    from scrubber import _propagatable
    value = _repeat_value(case)
    if len(value) < 6 or not _propagatable(value):
        pytest.skip(f"not propagated by design: {value!r}")
    out, _ = preview(case["input"] + "\nlater: " + value, A.OUTPUT_MAX)
    assert case["leak"] not in out, value


@pytest.mark.parametrize("password", ["s3cr3t!P@ssw0rd!2024", "correct.horse.battery.staple", "Xk2$mQ9#vLpq7Rz8wT",
                                      "Tr0ub4dorXk2mQ9vLpq.tail!2024", "hunter2hunter2hunter2#Zq8",
                                      "Tr0ub4dor[Xk2mQ9vLpq7", "hunter2hunter2[hunter2#Zq8"])
@pytest.mark.parametrize("key", ["passwd:", "password:", "Password:", "pwd:", "passphrase:", "DB_PASSWORD=",
                                 "PGPASSWORD=", "password=", "db_password="])
@pytest.mark.parametrize("pad", ["Q", "Q,", "a;"])
def test_a_split_value_leaves_no_fragment_for_any_key_form(password, key, pad):
    """Round 4, S1-2: in whitespace-free text the 1 KB cut lands inside the value, the chunk
    scrubber redacts its head, and every later rule skips what follows the marker."""
    for p in range(980, 1031):
        raw = (pad * 1100)[:p] + "," + key + password + "," + (pad * 1500)
        text, _ = preview(raw, A.OUTPUT_MAX)
        for i in range(len(password) - 3):
            assert password[i:i + 4] not in text, (p, key, password[i:i + 4])



# --- activity review round 5 ------------------------------------------------------------------

def _fragments(secret: str, n: int = 5) -> list[str]:
    return [secret[i:i + n] for i in range(len(secret) - n + 1)]


@pytest.mark.parametrize("case", [c for c in _SINGLE_LINE], ids=lambda c: c["name"])
def test_no_fragment_of_a_one_line_fixture_survives_a_seam(case):
    """Round 5, S1-2: when a typed rule (the GitHub token rule, a JWT rule) took the head of a
    value split by the 1 KB cut, the tail stayed glued after its typed marker. Check every
    5-character fragment of the value, not only the fixture's leak string."""
    value = _repeat_value(case)
    # the part of the token that holds the secret, not its key (db_password=, postgres://user:)
    secret = next((part for part in re.split(r"[=:]", value) if case["leak"] in part), value)
    if len(secret) < 12:
        pytest.skip("too short to leave a 5-character fragment that means anything")
    for k in range(1, len(value)):
        raw = "," * (1024 - k) + case["input"] + "," * 1200
        text, _ = preview(raw, A.OUTPUT_MAX)
        for f in _fragments(secret):
            # token-like fragments only: a digit, or mixed case. Plain words (a command name, a
            # host) and a vendor's fixed prefix (ATATT) are not secret material.
            tokenish = any(ch.isdigit() for ch in f) or (f.lower() != f and f.upper() != f)
            if f.isalnum() and tokenish and f not in case["input"].replace(secret, ""):
                assert f not in text, (k, f)


def test_two_markers_back_to_back_both_lose_their_tails():
    """Round 5, S1-1: the server can emit [REDACTED-SECRET][REDACTED-SECRET].tail; the glued
    rule ate the second marker and its tail survived."""
    out = A.preview("password=[REDACTED-SECRET][REDACTED-SECRET].tail!2024, next", A.OUTPUT_MAX)[0]
    assert "tail" not in out and "2024" not in out and out.endswith(", next")


def test_a_glued_tail_longer_than_256_characters_goes_whole():
    """Round 5, S2-1: a cap of 256 cut the run mid-way and left the rest, sometimes a whole
    credential further along the same run."""
    raw = "git clone https://deploy:Zq8vX2mN4pL7@git.example.com/a/b/" + "x" * 225 + "?MY_CREDENTIAL=SuperSecretValue123 done"
    out, _ = preview(raw, A.OUTPUT_MAX)
    assert "SuperSecretValue123" not in out and "Zq8vX2mN4pL7" not in out


# --- the send path, field by field (Cairn's #145 review, S2-2) -------------------------------

def _send_path_fields(x: str, tp: str) -> list[tuple[str, list[dict]]]:
    """(field, hook inputs) pairs that put x into each board-visible field."""
    return [
        ("tool.started Bash command", [base("PreToolUse", tool_name="Bash", tool_use_id="t1", tool_input={"command": x})]),
        ("tool.started Agent prompt", [base("PreToolUse", tool_name="Agent", tool_use_id="t2",
                                            tool_input={"subagent_type": "Explore", "description": "d", "prompt": x})]),
        ("tool.started Agent description + subagent.started description", [
            base("PreToolUse", tool_name="Agent", tool_use_id="t3", tool_input={"subagent_type": "Plan", "description": x}),
            base("SubagentStart", agent_id="sa1", agent_type="Plan")]),
        ("tool.started MCP input", [base("PreToolUse", tool_name="mcp__x__y", tool_use_id="t4", tool_input={"q": x})]),
        ("tool.finished Bash stdout", [base("PostToolUse", tool_name="Bash", tool_use_id="t5", tool_input={},
                                            tool_response={"stdout": x, "stderr": ""})]),
        ("tool.finished Agent report", [base("PostToolUse", tool_name="Agent", tool_use_id="t6", tool_input={},
                                             tool_response={"status": "completed", "content": [{"type": "text", "text": x}]})]),
        ("tool.finished Write content", [base("PostToolUse", tool_name="Write", tool_use_id="t7", tool_input={},
                                              tool_response={"type": "create", "filePath": "/p", "content": x})]),
        ("tool.finished Edit old/new", [base("PostToolUse", tool_name="Edit", tool_use_id="t8", tool_input={},
                                             tool_response={"filePath": "/p", "oldString": x, "newString": x})]),
        ("tool.finished MCP output", [base("PostToolUse", tool_name="mcp__x__y", tool_use_id="t9", tool_input={},
                                           tool_response={"result": x})]),
        ("tool.finished Grep content", [base("PostToolUse", tool_name="Grep", tool_use_id="t10", tool_input={},
                                             tool_response={"mode": "content", "content": x})]),
        ("tool.finished WebFetch result", [base("PostToolUse", tool_name="WebFetch", tool_use_id="t11", tool_input={},
                                                tool_response={"result": x, "code": 200})]),
        ("tool.finished AskUserQuestion questions", [base("PostToolUse", tool_name="AskUserQuestion", tool_use_id="t12",
                                                          tool_input={}, tool_response={"questions": [{"question": x}]})]),
        ("tool.finished error", [base("PostToolUseFailure", tool_name="Bash", tool_use_id="t13", tool_input={}, error=x)]),
        ("message (the final reply from the hook)", [base("Stop", transcript_path=tp, last_assistant_message=x)]),
        ("task.created subject", [base("TaskCreated", task_id="7", task_subject=x)]),
        ("task.completed subject", [base("TaskCompleted", task_id="7", task_subject=x)]),
        # the verify-only delta's list: every other sent free-text field
        ("tool.started Read path", [base("PreToolUse", tool_name="Read", tool_use_id="u1", tool_input={"file_path": x})]),
        ("tool.started Write path", [base("PreToolUse", tool_name="Write", tool_use_id="u2", tool_input={"file_path": x, "content": "c"})]),
        ("tool.started Edit path", [base("PreToolUse", tool_name="Edit", tool_use_id="u3", tool_input={"file_path": x})]),
        ("tool.started Grep pattern/path/glob/type", [base("PreToolUse", tool_name="Grep", tool_use_id="u4",
                                                           tool_input={"pattern": x, "path": x, "glob": x, "type": x})]),
        ("tool.started Glob pattern/path", [base("PreToolUse", tool_name="Glob", tool_use_id="u5", tool_input={"pattern": x, "path": x})]),
        ("tool.started WebFetch url", [base("PreToolUse", tool_name="WebFetch", tool_use_id="u6", tool_input={"url": x})]),
        ("tool.started WebSearch query", [base("PreToolUse", tool_name="WebSearch", tool_use_id="u7", tool_input={"query": x})]),
        ("tool.started AskUserQuestion input", [base("PreToolUse", tool_name="AskUserQuestion", tool_use_id="u8",
                                                     tool_input={"questions": [{"question": x}]})]),
        ("tool.started Agent type", [base("PreToolUse", tool_name="Task", tool_use_id="u9", tool_input={"subagent_type": x})]),
        ("tool.started generic input", [base("PreToolUse", tool_name="TodoWrite", tool_use_id="u10", tool_input={"todos": [x]})]),
        ("tool.finished Bash stderr", [base("PostToolUse", tool_name="Bash", tool_use_id="v1", tool_input={},
                                            tool_response={"stdout": "", "stderr": x})]),
        ("tool.finished Write path", [base("PostToolUse", tool_name="Write", tool_use_id="v2", tool_input={"file_path": x},
                                           tool_response={"type": "create", "filePath": x, "content": "c"})]),
        ("tool.finished MultiEdit", [base("PostToolUse", tool_name="MultiEdit", tool_use_id="v3", tool_response={},
                                          tool_input={"file_path": x, "edits": [{"old_string": x, "new_string": x}]})]),
        ("tool.finished NotebookEdit", [base("PostToolUse", tool_name="NotebookEdit", tool_use_id="v4", tool_response={},
                                             tool_input={"notebook_path": x, "new_source": x, "edit_mode": x})]),
        ("tool.finished Glob/Grep filenames", [base("PostToolUse", tool_name="Glob", tool_use_id="v5", tool_input={},
                                                    tool_response={"filenames": [x]})]),
        ("tool.finished plain string", [base("PostToolUse", tool_name="mcp__x__y", tool_use_id="v6", tool_input={}, tool_response=x)]),
        ("tool.finished Agent status and string result", [
            base("PostToolUse", tool_name="Agent", tool_use_id="v7", tool_input={}, tool_response={"status": x, "content": []}),
            base("PostToolUse", tool_name="Agent", tool_use_id="v8", tool_input={}, tool_response=x)]),
        ("tool.finished Read type", [base("PostToolUse", tool_name="Read", tool_use_id="v9", tool_input={}, tool_response={"type": x})]),
    ]


@pytest.fixture
def in_process_report(tmp_path, monkeypatch):
    """report.main() in this process, every POST collected: fast enough to run every fixture
    through every field."""
    import io
    from fleet import common as C
    from fleet import report as R
    home = tmp_path / "home"
    home.mkdir()
    (home / "plumb.token").write_text("test-token")
    toml = tmp_path / "s.toml"
    toml.write_text('[synapse]\nbase_url = "https://syn.example"\nhandle = "plumb"\n[fleet]\nenabled = true\nactivity = true\n')
    monkeypatch.setattr(C, "CONFIG_PATH", toml)
    monkeypatch.setattr(C, "HOME_SYNAPSE", home)
    monkeypatch.setattr(C, "STATE_DIR", home / "fleet")
    monkeypatch.setenv("MS4CC_FLEET_INLINE", "1")
    monkeypatch.setenv("MS4CC_FLEET_AGENT", "plumb")
    sent: list[dict] = []
    monkeypatch.setattr(C, "post", lambda cfg, events: sent.extend(events) or 202)

    def run(inp: dict) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(inp)))
        R.main()
    return run, sent


@pytest.mark.parametrize("case", _shared_fixtures()["secret"], ids=lambda c: c["name"])
def test_every_board_visible_field_is_redacted_on_the_send_path(case, tmp_path, in_process_report):
    """Cairn's #145 review, S2-2: nothing tested redaction field by field at the outbound
    payload, which is how the Agent report's `[status] ` prefix (S2-1) got through."""
    run, sent = in_process_report
    tp = tmp_path / "proj" / f"{SID}.jsonl"
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text("")
    run(base("SessionStart", source="startup", transcript_path=str(tp)))
    leaks = []
    # a reply tailed from the transcript itself, main and subagent
    sub = tp.parent / SID / "subagents" / "agent-sa9.jsonl"
    sub.parent.mkdir(parents=True, exist_ok=True)
    for path, extra in ((tp, {}), (sub, {"agent_id": "sa9", "agent_type": "Explore"})):
        sent.clear()
        with path.open("a") as f:
            f.write(json.dumps(assistant("m-" + path.stem, case["input"])) + "\n")
        run(base("PostToolUse", tool_name="Read", tool_use_id="r-" + path.stem, tool_input={}, tool_response="",
                 transcript_path=str(tp), **extra))
        if case["leak"] in json.dumps(sent, ensure_ascii=False):
            leaks.append("message (transcript tail) " + path.name)
    for field, inputs in _send_path_fields(case["input"], str(tp)):
        sent.clear()
        for inp in inputs:
            run(inp)
        if not sent:
            leaks.append(f"{field}: nothing sent")
        elif case["leak"] in json.dumps(sent, ensure_ascii=False):
            leaks.append(field)
    assert not leaks, leaks


@pytest.mark.parametrize(
    "case", [c for c in _shared_fixtures()["secret_in_json"] if c["name"].endswith("json with a footer")],
    ids=lambda c: c["name"])
def test_json_embedded_secrets_are_redacted_on_the_bash_send_path(case, in_process_report):
    """synapse#40 through the real hook and send path: `cat cfg.json; exit 1` style Bash output,
    a JSON document followed by a footer line (Plumb's review of #147)."""
    run, sent = in_process_report
    run(base("PostToolUse", tool_name="Bash", tool_use_id="j1", tool_input={"command": "cat cfg.json"},
             tool_response={"stdout": case["input"], "stderr": ""}))
    out = json.dumps(sent, ensure_ascii=False)
    assert sent
    assert case["leak"] not in out and case["leak_plain"] not in out


def test_emit_later_bounds_the_detached_send_with_an_alarm(monkeypatch):
    """Cairn's #145 review, S2-2: removing the alarm passed every test."""
    from fleet import common as C
    calls = []
    monkeypatch.delenv("MS4CC_FLEET_INLINE", raising=False)
    monkeypatch.setattr(C, "detach", lambda fn: fn())
    monkeypatch.setattr(C.signal, "signal", lambda *a: None)
    monkeypatch.setattr(C.signal, "alarm", lambda s: calls.append(("alarm", s)))
    monkeypatch.setattr(C, "post", lambda cfg, batch: calls.append(("post", len(batch))))
    C.emit_later(None, [{"type": "tool.finished"}], lambda: calls.append(("more",)) or [])
    assert calls[0] == ("alarm", C.SEND_DEADLINE_S * 2) and ("post", 1) in calls
