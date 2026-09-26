#!/usr/bin/env python3
"""Regression test: transcript ingest keeps content, drops harness scaffolding (#111 M5).

Harness-generated user turns were embedded verbatim: <task-notification> in 14.4%
of transcript chunks and <command-name> in 12.9%, so chunks matched each other on
the scaffolding (ids, output paths, wrapper tags) rather than on what was said.

Standalone (no network):  python3 test_transcript_ingest_cleans_harness.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import indexer as ix  # noqa: E402


def main() -> int:
    passed = failed = 0

    def check(desc, cond):
        nonlocal passed, failed
        if cond:
            print(f"  ok   - {desc}")
            passed += 1
        else:
            print(f"  FAIL - {desc}")
            failed += 1

    c = ix._clean_user_text
    note = ("<task-notification>\n<task-id>a1b2</task-id>\n<tool-use-id>toolu_X</tool-use-id>\n"
            "<output-file>/tmp/x/tasks/a1b2.output</output-file>\n<status>completed</status>\n"
            "<summary>Agent \"QA pass\" finished</summary>\n<result>Verdict: READY. The fix holds.</result>\n"
            "</task-notification>")
    got = c(note)
    check("a task result keeps its summary and result", "QA pass" in got and "Verdict: READY" in got)
    check("  and drops the ids, output path and status",
          all(x not in got for x in ("a1b2", "toolu_X", "/tmp/x/tasks", "<status>", "<task-id>")))
    event = ("<task-notification>\n<task-id>bt1</task-id>\n<summary>Monitor event: \"Synapse poll\""
             "</summary>\n<event>{\"sender_handle\": \"clint\", \"body\": \"pause then for a bit\"}"
             "</event>\n</task-notification>")
    got = c(event)
    check("a Monitor event keeps its <event> payload (Synapse messages)",
          "pause then for a bit" in got and "clint" in got)
    quoted = ("<task-notification>\n<task-id>q1</task-id>\n<status>completed</status>\n"
              "<result>The regex stops at a literal </result> tag and also sees <status>x</status> "
              "mid-line. VERDICT: FIX-FIRST</result>\n<usage><tool_uses>3</tool_uses></usage>\n"
              "</task-notification>")
    got = c(quoted)
    check("a result quoting </result> is kept whole", "VERDICT: FIX-FIRST" in got)
    check("  a scaffolding tag quoted mid-line inside it is kept", "<status>x</status>" in got)
    check("  while the real scaffolding lines are dropped",
          "q1" not in got and "completed" not in got and "tool_uses" not in got)
    check("a slash-command wrapper becomes '/name args'",
          c("<command-message>loop</command-message>\n<command-name>/loop</command-name>\n"
            "<command-args>/synapse-watch</command-args>") == "/loop /synapse-watch")
    check("local command output is dropped", c("<local-command-stdout>ok</local-command-stdout>") == "")
    check("a leading system-reminder is dropped, the prompt after it kept",
          c("<system-reminder>Message sent at Mon.</system-reminder>\nuse a black background")
          == "use a black background")
    prose = "we should strip <task-notification> envelopes at ingest"
    check("a tag mentioned inside prose is left alone", c(prose) == prose)
    check("ordinary text is unchanged", c("what did we decide?") == "what did we decide?")

    # Through the real chunker: only user turns are cleaned; tool results and
    # assistant text are untouched.
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": note}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "<task-notification> is just a tag I am discussing"}]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "<task-id>keep-me</task-id>"}]}}),
    ]
    lines.append(json.dumps({"type": "user", "isMeta": True, "message": {"role": "user",
                             "content": "# /loop skill body SKILLBODY-MARKER instructions"}}))
    text = "\n".join(ch.text for ch in ix.chunk_transcript("\n".join(lines), "/x/s.jsonl"))
    check("chunker: the task result's content is embedded", "Verdict: READY" in text)
    check("  its scaffolding is not", "toolu_X" not in text and "<output-file>" not in text)
    check("  assistant text is untouched", "<task-notification> is just a tag" in text)
    check("  tool results are untouched", "keep-me" in text)
    check("  harness-injected isMeta records (skill bodies) are not embedded",
          "SKILLBODY-MARKER" not in text)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
