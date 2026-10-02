#!/usr/bin/env python3
"""Tests for per-session Synapse cursors (state.py, cli.py fetch, the prompt hook).

Several Claude Code sessions can be open at once. With one shared cursor, the
session that reads a mention first advances it and the others never see it.
Each session now keeps <handle>.cursor.<session_id>.json instead.

Standalone (no pytest, no network, no real ~/.synapse):
    python3 test_synapse_session_cursors.py
"""
import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.integrations.synapse import cli as CLI  # noqa: E402
from orchestrator.integrations.synapse import config as CONFIG  # noqa: E402
from orchestrator.integrations.synapse import state as S  # noqa: E402

passed = failed = 0


def check(desc, cond):
    global passed, failed
    if cond:
        print(f"  ok   - {desc}")
        passed += 1
    else:
        print(f"  FAIL - {desc}")
        failed += 1


class Page:
    def __init__(self, messages, head):
        self.messages, self.head_cursor = messages, head


class Msg:
    channel, sender_kind, mentioned_handles = "ch", "agent", ()

    def __init__(self, created_at, body="hi"):
        self.created_at, self.body, self.sender_handle = created_at, body, "sender"


class FakeClient:
    """Returns every message newer than `since`; head cursor is the newest one."""

    def __init__(self, stamps):
        self.stamps = stamps

    def list_messages(self, slug, since=None, mentions_me=False, limit=50, order="asc"):
        msgs = [Msg(t) for t in self.stamps if since is None or t > since]
        return Page(msgs, msgs[-1].created_at if msgs else None)


def fetch(cfg, stamps, *, advance, session_id):
    """Run `synapse fetch` and return the created_at values it printed."""
    CLI._require_config = lambda: cfg
    CLI._client = lambda _cfg: FakeClient(stamps)
    if session_id:
        os.environ["CLAUDE_CODE_SESSION_ID"] = session_id
    else:
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    args = types.SimpleNamespace(channel=None, mentions_only=True, advance_cursor=advance, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CLI.cmd_fetch(args)
    return [json.loads(line)["created_at"] for line in buf.getvalue().splitlines() if line.strip()]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        CONFIG.HOME_SYNAPSE = Path(tmp)  # ensure_synapse_dir() reads this at call time
        cfg = types.SimpleNamespace(
            handle="me", channels=["ch"], limit_per_channel=50, cursor_path=Path(tmp) / "me.cursor.json"
        )

        print("session id validation")
        check("a normal id is accepted", S.valid_session_id("example-session_01") == "example-session_01")
        for bad in ("", "../x", "a/b", "a b", "x" * 65, None, 7):
            check(f"rejected: {bad!r}", S.valid_session_id(bad) is None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        check("current_session_id reads the env var", S.current_session_id() == "s1")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "../evil"
        check("current_session_id rejects an unsafe env value", S.current_session_id() is None)

        print("two sessions do not consume each other's mentions")
        stamps = ["2026-01-01T00:00:01", "2026-01-01T00:00:02"]
        a = fetch(cfg, stamps, advance=True, session_id="sessA")
        b = fetch(cfg, stamps, advance=True, session_id="sessB")
        check("session A sees both mentions", a == stamps)
        check("session B still sees both mentions after A advanced", b == stamps)
        check("session A sees nothing new on its next fetch", fetch(cfg, stamps, advance=True, session_id="sessA") == [])
        newer = stamps + ["2026-01-01T00:00:03"]
        check("a later mention reaches both sessions",
              fetch(cfg, newer, advance=True, session_id="sessA") == newer[2:]
              and fetch(cfg, newer, advance=True, session_id="sessB") == newer[2:])
        check("the shared cursor was never written", not cfg.cursor_path.exists())
        check("each session has its own 0600 file",
              all(stat.S_IMODE((Path(tmp) / f"me.cursor.{s}.json").stat().st_mode) == 0o600 for s in ("sessA", "sessB")))

        print("read-only unless --advance-cursor")
        before = sorted(os.listdir(tmp))
        fetch(cfg, newer + ["2026-01-01T00:00:04"], advance=False, session_id="sessC")
        check("no cursor file is created or changed", sorted(os.listdir(tmp)) == before)

        print("seeding from the shared cursor")
        cfg.cursor_path.write_text(json.dumps({"ch": "2026-01-01T00:00:02"}))
        seeded = fetch(cfg, newer, advance=True, session_id="sessD")
        check("a new session starts from the shared cursor", seeded == newer[2:])
        cfg.cursor_path.write_text(json.dumps({"ch": "2026-01-01T00:00:03"}))
        check("moving the shared cursor later does not change a pinned session",
              fetch(cfg, newer + ["2026-01-01T00:00:04"], advance=True, session_id="sessD") == ["2026-01-01T00:00:04"])

        print("no session id: shared cursor, as before")
        cfg.cursor_path.write_text(json.dumps({"ch": "2026-01-01T00:00:00"}))
        got = fetch(cfg, stamps, advance=True, session_id=None)
        check("reads the shared cursor", got == stamps)
        check("writes the shared cursor", json.loads(cfg.cursor_path.read_text())["ch"] == stamps[-1])

        print("a failed write for one channel is not saved by another channel's write")
        two = types.SimpleNamespace(handle="me", channels=["ch", "ch2"], limit_per_channel=50, cursor_path=cfg.cursor_path)
        cfg.cursor_path.write_text(json.dumps({"ch": "2026-01-01T00:00:00", "ch2": "2026-01-01T00:00:00"}))
        real, calls = CLI.write_session_cursors, []

        def fail_first(*a, **k):
            calls.append(1)
            return False if len(calls) == 1 else real(*a, **k)

        CLI.write_session_cursors = fail_first
        fetch(two, stamps, advance=True, session_id="sessE")
        CLI.write_session_cursors = real
        again = fetch(two, stamps, advance=True, session_id="sessE")
        check("the channel whose write failed repeats its mentions (the other does not)", again == stamps)

        print("write and prune")
        check("write_session_cursors returns True", S.write_session_cursors(cfg, "p1", {"ch": "x"}) is True)
        check("no stray .tmp is left", not list(Path(tmp).glob("*.tmp")))
        old, fresh = Path(tmp) / "me.cursor.old.json", Path(tmp) / "me.cursor.fresh.json"
        oldtmp = Path(tmp) / "me.cursor.killed.json.tmp"
        for f in (old, fresh, oldtmp):
            f.write_text("{}")
        aged = time.time() - S.SESSION_CURSOR_MAX_AGE_S - 3600
        os.utime(old, (aged, aged))
        os.utime(oldtmp, (aged, aged))
        S.prune_session_cursors(cfg)
        check("an idle file and an old .tmp are pruned", not old.exists() and not oldtmp.exists())
        check("a recent file is kept", fresh.exists())
        check("the shared cursor is never pruned", cfg.cursor_path.exists())
        os.utime(fresh, (aged, aged))
        S.touch_session_cursors(cfg, "fresh")
        S.prune_session_cursors(cfg)
        check("touching a file keeps a live session from being pruned", fresh.exists())

        print("write failure is reported, not raised")
        bad_cfg = types.SimpleNamespace(handle="me", cursor_path=Path(tmp) / "nope" / "x" / "me.cursor.json")
        CONFIG.HOME_SYNAPSE = Path(tmp) / "file-not-dir"
        Path(tmp, "file-not-dir").write_text("x")  # a file where a directory is needed
        with contextlib.redirect_stderr(io.StringIO()):
            check("returns False", S.write_session_cursors(bad_cfg, "p2", {"ch": "x"}) is False)

        print("hook: reading the session id from stdin")
        spec = importlib.util.spec_from_file_location("hook", ROOT / "orchestrator" / "hooks" / "synapse_user_prompt_submit.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)

        def with_stdin(writer):
            r, w = os.pipe()
            writer(w)
            old_stdin, sys.stdin = sys.stdin, os.fdopen(r, "r")
            try:
                t0 = time.monotonic()
                return hook._read_session_id(), time.monotonic() - t0
            finally:
                sys.stdin.close()
                sys.stdin = old_stdin

        def closed(payload):
            def go(w):
                os.write(w, payload)
                os.close(w)
            return go

        check("valid payload", with_stdin(closed(b'{"session_id": "abc-123"}'))[0] == "abc-123")
        check("not JSON falls back", with_stdin(closed(b"garbage"))[0] is None)
        check("unsafe id falls back", with_stdin(closed(b'{"session_id": "../x"}'))[0] is None)
        big = json.dumps({"prompt": "x" * 300_000, "session_id": "big-1"}).encode()

        def big_writer(w):
            # Bigger than the pipe buffer, so the writer must run alongside the reader.
            def run():
                view = memoryview(big)
                while view:
                    view = view[os.write(w, view):]
                os.close(w)
            threading.Thread(target=run, daemon=True).start()

        check("a 300 KB payload is read in full", with_stdin(big_writer)[0] == "big-1")
        held = []
        sid, took = with_stdin(lambda w: (os.write(w, b'{"session_id": "x"'), held.append(w)))
        check("a pipe held open mid-payload falls back within the wait bound", sid is None and took < 1.5)
        for w in held:
            os.close(w)

        print("hook: the early exit and the camelCase spelling")
        sid, took = with_stdin(lambda w: (os.write(w, b'{"session_id": "open-1"}'), held.append(w)))
        check("a complete payload on a pipe left open returns at once", sid == "open-1" and took < 0.3)
        for w in held[1:]:
            os.close(w)
        check("sessionId is accepted too", with_stdin(closed(b'{"sessionId": "camel-1"}'))[0] == "camel-1")

        print("hook main(): two sessions, shared cursor untouched")
        CONFIG.HOME_SYNAPSE = Path(tmp)
        pkg = sys.modules["orchestrator.integrations.synapse"]
        hcfg = types.SimpleNamespace(
            handle="me", channels=(), limit_per_channel=50, digest_mentions_only=True,
            base_url="http://unused", http_timeout=1, cursor_path=Path(tmp) / "me.cursor.json",
            read_token=lambda: "token",
        )

        class HookClient(FakeClient):
            def __init__(self, *a, **k):
                super().__init__(stamps)

            def list_channels(self):
                return [{"slug": "ch"}, {"slug": "ch2"}]

        pkg.load_config, pkg.is_active, pkg.SynapseClient = (lambda: hcfg), (lambda cfg: True), HookClient

        def prompt(session_id):
            """One prompt through the real hook main(); returns how many mentions it injected."""
            payload = json.dumps({"session_id": session_id} if session_id else {}).encode()
            r, w = os.pipe()
            os.write(w, payload)
            os.close(w)
            old_stdin, sys.stdin = sys.stdin, os.fdopen(r, "r")
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    hook.main()
            finally:
                sys.stdin.close()
                sys.stdin = old_stdin
            out = buf.getvalue().strip()
            return json.loads(out)["hookSpecificOutput"]["additionalContext"].count("- [") if out else 0

        for f in Path(tmp).glob("me.cursor*"):
            f.unlink()
        check("session A is shown both mentions on both channels", prompt("hookA") == 4)
        check("session B is still shown them after A consumed them", prompt("hookB") == 4)
        check("A is shown nothing the second time", prompt("hookA") == 0)
        check("the hook did not write the shared cursor", not hcfg.cursor_path.exists())
        check("no session id: the shared cursor is used and written",
              prompt(None) == 4 and json.loads(hcfg.cursor_path.read_text())["ch"] == stamps[-1])

        print("hook main(): a failed write for one channel is rolled back")
        hcfg.cursor_path.write_text(json.dumps({"ch": "2026-01-01T00:00:00", "ch2": "2026-01-01T00:00:00"}))
        real, calls = S.write_session_cursors, []

        def fail_first(*a, **k):
            calls.append(1)
            return False if len(calls) == 1 else real(*a, **k)

        S.write_session_cursors = fail_first
        prompt("hookE")
        S.write_session_cursors = real
        check("only the channel whose write failed repeats", prompt("hookE") == 2)

        print("fetch pins the seed even when the first fetch returns nothing")
        cfg.cursor_path.write_text(json.dumps({"ch": "2026-01-01T00:00:09"}))
        check("nothing new past the seed", fetch(cfg, stamps, advance=True, session_id="pin1") == [])
        cfg.cursor_path.write_text(json.dumps({"ch": "2026-01-01T00:00:00"}))
        check("a later move of the shared cursor does not change a pinned session",
              fetch(cfg, stamps, advance=True, session_id="pin1") == [])

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
