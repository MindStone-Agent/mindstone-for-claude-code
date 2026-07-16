#!/usr/bin/env python3
"""Regression test for SynapseClient.me() empty-body guard (#60 / PR #61).

`_request()` returns None on an empty 200 body. Before the guard, me() passed
that straight through, so callers doing `me.get(...)` hit AttributeError — which
isn't a SynapseError, so it escaped the `except SynapseError` handlers and
crashed `synapse status` instead of taking the fail-soft path. me() must now
either return a dict or raise SynapseError.

Standalone (no pytest / no network):  python3 test_synapse_client_me.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrations" / "synapse"))
import client as C  # noqa: E402


def _stub(return_value):
    """A SynapseClient whose _request returns return_value without any network."""
    c = C.SynapseClient.__new__(C.SynapseClient)  # bypass __init__
    c._request = lambda *a, **k: return_value
    return c


def main() -> int:
    passed = failed = 0

    def check(desc, cond):
        nonlocal passed, failed
        if cond:
            print(f"  ok   - {desc}"); passed += 1
        else:
            print(f"  FAIL - {desc}"); failed += 1

    # Empty body -> _request returns None -> must raise SynapseError (fail-soft path),
    # NOT AttributeError (the crash) and NOT a silent None.
    try:
        _stub(None).me()
        check("empty body raises (did not raise)", False)
    except C.SynapseError:
        check("empty body -> SynapseError (routes to except SynapseError handlers)", True)
    except AttributeError:
        check("empty body -> SynapseError (still crashes with AttributeError)", False)

    # Non-object body (e.g. a bare string) -> also SynapseError, not a dict-typed leak.
    try:
        _stub("not-a-dict").me()
        check("non-object body raises (did not raise)", False)
    except C.SynapseError:
        check("non-object body -> SynapseError", True)

    # Normal object body passes straight through untouched.
    payload = {"handle": "cairn", "kind": "agent"}
    try:
        check("valid dict body returned unchanged", _stub(payload).me() == payload)
    except Exception as e:  # noqa: BLE001
        check(f"valid dict body returned unchanged (raised {e!r})", False)

    print(f"\npassed: {passed}   failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
