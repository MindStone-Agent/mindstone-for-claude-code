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


@pytest.mark.parametrize("raw", ["KEYBOARD=ok", "MONKEY=1", "PATH=/usr/bin:/bin", "ls -la && echo done"])
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
    _, started = synapse.wait(2)
    assert started["type"] == "subagent.started" and started["agent_id"] == "a1"
    assert started["payload"] == {"agent_type": "general-purpose", "parent_tool_use_id": "toolu_AG1", "description": "check files"}
    assert PROMPT not in json.dumps(synapse.requests)


def test_pairing_takes_the_oldest_call_of_the_same_type(tmp_path, synapse):
    env = act_env(tmp_path, synapse.url)
    for tid, typ in (("c1", "Explore"), ("c2", "general-purpose"), ("c3", "Explore")):
        hook(env, base("PreToolUse", tool_name="Agent", tool_use_id=tid, tool_input={"subagent_type": typ, "description": tid}))
    for aid, typ in (("g1", "general-purpose"), ("e1", "Explore"), ("e2", "Explore"), ("x1", "Explore")):
        hook(env, base("SubagentStart", agent_id=aid, agent_type=typ))
    starts = [e for e in synapse.wait(7) if e["type"] == "subagent.started"]
    assert [(e["agent_id"], e["payload"].get("parent_tool_use_id")) for e in starts] == [
        ("g1", "c2"), ("e1", "c1"), ("e2", "c3"), ("x1", None)]


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
# toolUseResult); the canaries stand in for what must never leave.
FILE_BODY = "FILE_BODY_CANARY_x81"
ANSWER = "ANSWER_CANARY_77"
POST_RESPONSES = {
    "Agent": {"status": "completed", "prompt": PROMPT, "agentId": "a1", "agentType": "Explore",
              "content": [{"type": "text", "text": "found 3 tests"}], "totalTokens": 10},
    "Write": {"type": "create", "filePath": "/p/a.py", "content": FILE_BODY, "structuredPatch": [], "originalFile": None},
    "Edit": {"filePath": "/p/b.py", "oldString": FILE_BODY, "newString": FILE_BODY, "originalFile": FILE_BODY,
             "structuredPatch": [{"lines": ["-" + FILE_BODY, "+" + FILE_BODY]}], "userModified": False, "replaceAll": False},
    "Read": {"type": "text", "file": {"filePath": "/p/c.py", "content": FILE_BODY, "numLines": 3, "startLine": 1, "totalLines": 3}},
    "AskUserQuestion": {"questions": [{"question": "Which?"}], "answers": {"Which?": ANSWER}, "annotations": {}},
    "SendMessage": {"success": True, "message": PROMPT},
    "mcp__x__y": {"result": "ok", "nested": {"prompt": PROMPT, "message": PROMPT}},
}
WANT_OUTPUT = {"Agent": "[completed] found 3 tests", "Write": "create /p/a.py", "Edit": "updated /p/b.py",
               "Read": "3 lines", "AskUserQuestion": "answered", "SendMessage": "sent", "mcp__x__y": '{"result": "ok", "nested": {}}'}


@pytest.mark.parametrize("tool", sorted(POST_RESPONSES))
def test_tool_output_is_allow_listed_per_tool(tmp_path, synapse, tool):
    """Round 1, finding 1 (sev-1): tool.finished dumped tool_response whole, so the Agent
    prompt and Write/Edit file bodies went out. Run through report.py as Claude Code would."""
    env = act_env(tmp_path, synapse.url)
    hook(env, base("PostToolUse", tool_name=tool, tool_use_id="t1", tool_input={}, tool_response=POST_RESPONSES[tool]))
    (ev,) = synapse.wait(1)
    assert ev["type"] == "tool.finished" and ev["payload"]["output_preview"] == WANT_OUTPUT[tool]
    raw = json.dumps(synapse.requests)
    assert PROMPT not in raw and FILE_BODY not in raw and ANSWER not in raw


@pytest.mark.parametrize("tool,ti,want", [
    ("SendMessage", {"to": "researcher", "message": PROMPT}, "to researcher"),
    ("ScheduleWakeup", {"delaySeconds": 60, "prompt": PROMPT}, '{"delaySeconds": 60}'),
    ("CronCreate", {"cron": "*/5 * * * *", "prompt": PROMPT}, '{"cron": "*/5 * * * *"}'),
    ("mcp__fs__write_file", {"path": "/p", "content": FILE_BODY}, '{"path": "/p"}'),
    ("mcp__x__y", {"a": [{"message": PROMPT, "b": 1}]}, '{"a": [{"b": 1}]}'),
])
def test_instruction_text_and_file_bodies_never_leave_in_inputs(tool, ti, want):
    """Round 1, finding 3: the default branch sent SendMessage's message and any prompt."""
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
                                 "monkey: banana", "url: https://x.y/z", "password reset flow", "keys: []",
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


def test_an_agent_call_that_never_starts_does_not_shift_later_pairings(tmp_path, synapse):
    """Round 1, finding 6: a denied or failed Agent call stayed at the head of the queue and
    every later subagent of its type got the previous call's id."""
    env = act_env(tmp_path, synapse.url)
    for tid in ("A", "B", "C"):
        hook(env, base("PreToolUse", tool_name="Agent", tool_use_id=tid, tool_input={"subagent_type": "Explore", "description": tid}))
    hook(env, base("PostToolUseFailure", tool_name="Agent", tool_use_id="A", tool_input={}, error="denied"))
    hook(env, base("SubagentStart", agent_id="b1", agent_type="Explore"))
    hook(env, base("SubagentStart", agent_id="c1", agent_type="Explore"))
    starts = [e for e in synapse.wait(8) if e["type"] == "subagent.started"]
    assert [(e["agent_id"], e["payload"].get("parent_tool_use_id")) for e in starts] == [("b1", "B"), ("c1", "C")]


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
    text, cut = preview("A" * 40000, 1000)
    assert cut and "A" not in text


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
