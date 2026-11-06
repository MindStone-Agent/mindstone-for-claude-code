#!/usr/bin/env python3
"""Regression test: recall runs on what a person asked, not on harness text (#111 M4).

Two query sources dominated auto-recall and matched nothing a person asked:
  - background-task results, delivered as a user turn wrapped in
    <task-notification> (1,363 of 5,607 logged recall rows);
  - the `/loop /synapse-watch` wakeup, re-fired every few minutes and recalling
    the same chunks each time (2,634 of 5,607). Slash commands reach the hook as
    the raw typed text, so leading `/command` tokens are dropped and whatever is
    left (e.g. "5m check the deploy") is recalled on.

Run it from a worktree, not the live install: the end-to-end leg goes through the
real hook, which appends its recall rows to transcripts/recall_usage.jsonl.
Use the orchestrator venv so that leg can load sqlite-vec:
    orchestrator/.venv/bin/python orchestrator/tests/test_recall_skips_harness_prompts.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import user_prompt_submit as ups  # noqa: E402


def _run_hook(prompt: str) -> subprocess.CompletedProcess:
    # No session id in the input, so the watchdog stays inert.
    return subprocess.run([sys.executable, str(HOOKS / "user_prompt_submit.py")],
                          input=json.dumps({"prompt": prompt}), capture_output=True,
                          text=True, env=dict(os.environ), timeout=120)


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

    q = ups.recall_query
    check("a task-notification gets no recall", q("<task-notification>\n<result>READY</result>") == "")
    check("  with leading whitespace too", q("  <task-notification>x") == "")
    check("`/loop /synapse-watch` gets no recall", q("/loop /synapse-watch") == "")
    check("`/compact` gets no recall", q("/compact") == "")
    check("`/loop 5m check the deploy` recalls on its args",
          q("/loop 5m check the deploy") == "5m check the deploy")
    check("a pasted path is kept whole",
          q("/Users/clint/foo.py is broken, why?") == "/Users/clint/foo.py is broken, why?")
    check("a command split from its args by a newline",
          q("/loop\ncheck the deploy please") == "check the deploy please")
    check("a namespaced plugin command recalls on its args",
          q("/telegram:access pair 123") == "pair 123")
    check("a human prompt is unchanged",
          q("what did we decide about the hit counter?") == "what did we decide about the hit counter?")
    check("a tag mentioned mid-text is not a harness prompt",
          q("look at <task-notification> handling") == "look at <task-notification> handling")
    check("a queued prompt wrapped in <system-reminder> still gets recall",
          q("<system-reminder>Message sent at Mon.</system-reminder>\nuse a black background") != "")

    # End to end through the real hook. Only meaningful if a HUMAN prompt gets a
    # recall block through the same path (store present, sqlite-vec loadable,
    # embedder up); otherwise the harness-prompt check would pass vacuously.
    control = _run_hook("what did we decide about the memory hit counter and recall?")
    if "semantic-recall" not in control.stdout:
        print("  skip - end-to-end leg: a human prompt got no recall here "
              "(no store, no sqlite-vec, or embedder down), so it would prove nothing")
    else:
        check("CONTROL a human prompt gets a recall block end to end", True)
        r = _run_hook("<task-notification>\n<task-id>a1</task-id>\n<summary>Agent QA finished"
                      "</summary>\n<result>what did we decide about the memory hit counter and "
                      "recall?</result>\n</task-notification>")
        check("a task-notification through the hook injects nothing", "semantic-recall" not in r.stdout)
        check("  and exits 0", r.returncode == 0)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
