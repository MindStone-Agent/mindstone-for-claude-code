#!/usr/bin/env python3
"""Adversarial probe for the voice relay — proves the allowlist REFUSES.

WHY THIS EXISTS
---------------
The relay holds Mira's gateway token. That token is administrative, not
conversational: measured against the live gateway, it authorises 62 methods
including `config.set`, `agents.delete`, `sessions.reset`, `cron.add`,
`exec.approvals.set` and `agents.files.set`. (`system.run` is NOT a live method
— the gateway answers `unknown method` — and `config.get` redacts credentials.
Both checked rather than assumed.)

So the relay's allowlist is the entire security boundary between a tailnet
device and Mira's control plane. It is about fifteen lines of code, and fifteen
lines of code that are load-bearing are exactly the kind that get a "tested it,
works" and no test.

This probe attacks the relay the way an attacker would and requires every attack
to fail. It asserts PROPERTIES, not response shapes, because the relay's schema
is Mira's to choose and a probe coupled to it would break on every refactor.

THE POINT: --self-test runs the probe against TWO in-process mock relays — one
correct, one a deliberately transparent proxy — and requires the probe to PASS
the good one and FAIL the bad one. A probe that cannot fail a vulnerable relay
proves nothing about a safe one. That is the whole lesson of this week, applied
to the thing before it is built rather than after.

Usage:
    python3 relay_probe.py --url http://127.0.0.1:8600 --secret-env RELAY_SECRET
    python3 relay_probe.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# Methods that must NEVER be reachable through the relay. Each one is live on the
# gateway and each one is destructive or authority-widening.
FORBIDDEN = [
    "config.set", "config.patch", "config.apply", "config.get",
    "agents.delete", "agents.update", "agents.create", "agents.files.set",
    "sessions.reset", "sessions.delete", "sessions.patch",
    "cron.add", "cron.run", "cron.update",
    "exec.approvals.set", "system.execApprovals.set",
    "channels.logout",
]

# Strings that must never reach the browser in an error or response body.
LEAKY = [
    (re.compile(r"/Users/[A-Za-z0-9_.-]+"), "a filesystem path"),
    (re.compile(r"\.mindstone|\.openclaw"), "an internal config directory"),
    (re.compile(r"__MINDSTONE_REDACTED__"), "raw gateway config"),
]


class Result:
    def __init__(self):
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, label: str, passed: bool, detail: str = "", quiet: bool = False):
        if not quiet:
            print(f"  {'PASS' if passed else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
        if not passed:
            self.failures.append(label)
        return passed


def _post(url: str, path: str, body: dict, secret: str | None, timeout: float = 6.0):
    """Return (status, text). Never raises — a refused request is a RESULT."""
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {secret}"} if secret else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 — connection refused is a result, not a crash
        return 0, f"{type(e).__name__}: {e}"


def probe(url: str, secret: str, r: Result, *, quiet: bool = False) -> Result:
    """Attack the relay. Every one of these must be refused."""

    # --- 1. No credential at all must be refused.
    st, _ = _post(url, "/voice", {"text": "hello"}, None)
    r.check("unauthenticated request is refused", st not in (200, 0),
            f"status {st}", quiet)

    # --- 2. A wrong credential must be refused. Without this, (1) passes on a
    #        relay that accepts everything and also on one that accepts nothing.
    st, _ = _post(url, "/voice", {"text": "hello"}, "not-the-relay-secret-000000")
    r.check("wrong credential is refused", st not in (200, 0), f"status {st}", quiet)

    # --- 3. CONTROL: the correct credential must WORK. A relay that refuses
    #        everything would pass every security check and be useless.
    st_ok, body_ok = _post(url, "/voice", {"text": "hello"}, secret)
    r.check("CONTROL the correct credential is accepted", st_ok == 200,
            f"status {st_ok}", quiet)

    # --- 4. THE BOUNDARY: a method name supplied by the client must not be
    #        forwarded. This is the single failure that publishes the control
    #        plane to the tailnet.
    forwarded = []
    for m in FORBIDDEN:
        st, body = _post(url, "/voice", {"text": "hi", "method": m}, secret)
        # Evidence of FORWARDING is the method surfacing in the response — not the
        # request merely succeeding.
        #
        # The first version also flagged any 200 containing `"ok":true`, which made
        # it fail the CORRECT relay: a relay that properly IGNORES the injected
        # method still answers 200 with an ok body, because the `text` was valid.
        # That detector was measuring "did the request succeed", not "did the
        # privileged method run" — the same wrong-property mistake that has cost
        # this repo a week of false results. Caught by the vulnerable/correct pair;
        # a probe with only attack cases would have shipped it.
        if st == 200 and m in body:
            forwarded.append(m)
    r.check("a client-supplied method name is NOT forwarded", not forwarded,
            f"{len(FORBIDDEN)} attempted, {len(forwarded)} reached the gateway"
            + (f": {', '.join(forwarded[:4])}" if forwarded else ""), quiet)

    # --- 5. Direct RPC paths must not exist. If the relay mounts the gateway's
    #        method surface as routes, the allowlist in the body handler is moot.
    reachable = []
    for m in FORBIDDEN[:8]:
        for path in (f"/{m}", f"/rpc/{m}", f"/api/{m}", f"/gateway/{m}"):
            st, _ = _post(url, path, {}, secret)
            if st == 200:
                reachable.append(path)
    r.check("privileged methods are not mounted as routes", not reachable,
            f"reachable: {', '.join(reachable[:4])}" if reachable else "none", quiet)

    # --- 6. A client-supplied sessionKey must not be honoured. Otherwise a
    #        caller injects into any conversation, not just the voice loop.
    st, body = _post(url, "/voice",
                     {"text": "hi", "sessionKey": "some:other:session"}, secret)
    r.check("a client-supplied sessionKey is ignored or refused",
            st != 200 or "some:other:session" not in body,
            "echoed back" if (st == 200 and "some:other:session" in body) else "not honoured",
            quiet)

    # --- 7. Errors must not leak internals to the browser.
    leaked = []
    for probe_body in ({"text": None}, {}, {"text": "x" * 5000}):
        _st, body = _post(url, "/voice", probe_body, secret)
        for pat, what in LEAKY:
            if pat.search(body or ""):
                leaked.append(what)
    r.check("responses do not leak paths or internal config", not leaked,
            f"leaked {', '.join(sorted(set(leaked)))}" if leaked else "clean", quiet)

    return r


# ---------------------------------------------------------------------------
# Mock relays for the self-test. One correct, one deliberately vulnerable.
# ---------------------------------------------------------------------------

SECRET = "test-relay-secret"
ALLOWED = {"chat.send", "chat.subscribe", "chat.history", "sessions.resolve"}


def _handler(vulnerable: bool):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _reply(self, code, obj):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", errors="replace")
            try:
                body = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            auth = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()

            if vulnerable:
                # The mistakes, all of them, on purpose:
                #  - no auth check
                #  - forwards a client-supplied method
                #  - honours a client-supplied sessionKey
                #  - echoes an internal path in errors
                method = body.get("method", "chat.send")
                if body.get("text") is None and "method" not in body:
                    return self._reply(500, {"error": "ENOENT /Users/someone/.mindstone/x.json"})
                return self._reply(200, {"ok": True, "method": method,
                                         "sessionKey": body.get("sessionKey", "voice:pinned")})

            # The correct relay.
            if auth != SECRET:
                return self._reply(401, {"error": "unauthorized"})
            if self.path != "/voice":
                return self._reply(404, {"error": "not found"})
            if not isinstance(body.get("text"), str) or not body["text"]:
                return self._reply(400, {"error": "bad request"})
            # method NEVER comes from the client; sessionKey is pinned server-side.
            return self._reply(200, {"ok": True, "reply": "hi back"})
    return H


def _serve(vulnerable: bool):
    srv = HTTPServer(("127.0.0.1", 0), _handler(vulnerable))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _self_test() -> int:
    print("self-test — the probe must PASS a correct relay and FAIL a vulnerable one\n")

    good_srv, good_url = _serve(vulnerable=False)
    bad_srv, bad_url = _serve(vulnerable=True)
    try:
        print("  against the CORRECT relay:")
        rg = probe(good_url, SECRET, Result())
        print("\n  against the DELIBERATELY VULNERABLE relay (failures are the point):")
        rb = probe(bad_url, SECRET, Result())
    finally:
        good_srv.shutdown()
        bad_srv.shutdown()

    print()
    ok_good = not rg.failures
    ok_bad = len(rb.failures) >= 4
    print(f"  {'ok  ' if ok_good else 'FAIL'} correct relay passes cleanly "
          f"({len(rg.failures)} failures, want 0)")
    print(f"  {'ok  ' if ok_bad else 'FAIL'} vulnerable relay is caught "
          f"({len(rb.failures)} failures, want >=4)")
    print()
    if ok_good and ok_bad:
        print("SELF-TEST PASSED — the probe discriminates. A green run against the real "
              "relay means the allowlist actually refuses.")
        return 0
    print("SELF-TEST FAILED — the probe cannot tell a safe relay from an unsafe one. "
          "Do not trust its verdict.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="relay base URL, e.g. http://127.0.0.1:8600")
    ap.add_argument("--secret-env", default="RELAY_SECRET",
                    help="env var holding the relay secret (never pass it on argv)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.url:
        ap.error("--url is required (or use --self-test)")

    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"FATAL: ${args.secret_env} is not set. The secret is read from the "
              f"environment so it never lands in shell history or a process list.",
              file=sys.stderr)
        return 2

    # Gate the real run on the self-test, so a green verdict cannot come from a
    # broken probe.
    if _self_test() != 0:
        return 2
    print()
    print(f"=== probing {args.url} ===")
    r = probe(args.url, secret, Result())
    print()
    if r.failures:
        print(f"RELAY UNSAFE — {len(r.failures)} check(s) failed:")
        for f in r.failures:
            print(f"  - {f}")
        return 1
    print("RELAY SAFE — unauthenticated and wrong-credential requests refused, no "
          "client-supplied method forwarded, no privileged route mounted, sessionKey "
          "pinned server-side, no internals leaked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
