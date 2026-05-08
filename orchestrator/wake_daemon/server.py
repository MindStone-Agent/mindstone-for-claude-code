"""Wake daemon HTTP server — stdlib http.server based.

Accepts POST /webhook from the Synapse delivery worker. Verifies HMAC
against the configured shared secret, then dispatches to the Waker on
a worker thread so the webhook response returns quickly while the
subprocess + reply post run async.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import WakeConfig
from .waker import WakeError, Waker


log = logging.getLogger(__name__)


def _verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Verify X-Synapse-Signature against a shared HMAC secret.

    Header shape: 'sha256=<hex>'. Constant-time compare to avoid
    timing attacks on the secret material.
    """
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256=") :], expected)


def make_handler(cfg: WakeConfig, waker: Waker, executor: ThreadPoolExecutor):
    """Build a request handler class closed over the daemon's state."""

    class WakeHandler(BaseHTTPRequestHandler):
        # Keep the http.server log-line per request, but route through
        # our logger so it interleaves cleanly with other logs.
        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("http: %s", fmt % args)

        def _respond(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            # Tiny health endpoint
            if self.path in ("/health", "/healthz"):
                self._respond(
                    200,
                    {
                        "status": "ok",
                        "service": "wake-daemon",
                        "handle": cfg.handle,
                    },
                )
                return
            self._respond(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self._respond(404, {"error": "not_found"})
                return

            length_str = self.headers.get("Content-Length", "0")
            try:
                length = int(length_str)
            except ValueError:
                self._respond(400, {"error": "bad_content_length"})
                return
            if length <= 0 or length > 1_000_000:
                self._respond(400, {"error": "invalid_body_length"})
                return

            body = self.rfile.read(length)

            secret = cfg.read_secret()
            if not secret:
                log.error(
                    "wake: no secret at %s; rejecting webhook",
                    cfg.secret_file,
                )
                self._respond(503, {"error": "no_secret_configured"})
                return

            sig_header = self.headers.get("X-Synapse-Signature", "")
            if not _verify_signature(secret, body, sig_header):
                log.warning(
                    "wake: signature mismatch on POST /webhook from %s",
                    self.address_string(),
                )
                self._respond(401, {"error": "invalid_signature"})
                return

            try:
                envelope = json.loads(body)
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid_json"})
                return

            event = self.headers.get("X-Synapse-Event", "?")
            delivery = self.headers.get("X-Synapse-Delivery", "?")
            log.info(
                "wake: accepted webhook event=%s delivery=%s channel=%s",
                event,
                delivery,
                envelope.get("channel"),
            )

            # Dispatch to worker pool — the subprocess can take 30-60s,
            # we don't want to hold the HTTP socket open for that.
            executor.submit(_safe_handle, waker, envelope, delivery)

            # Acknowledge fast. Synapse's delivery worker treats 200 as
            # success and won't retry.
            self._respond(202, {"status": "accepted", "delivery": delivery})

    return WakeHandler


def _safe_handle(waker: Waker, envelope: dict[str, Any], delivery: str) -> None:
    """Run the Waker, catch and log everything (never let exceptions
    escape into the executor's thread pool unhandled)."""
    try:
        result = waker.handle(envelope)
        log.info("wake: delivery=%s result=%s", delivery, result)
    except WakeError as e:
        log.warning("wake: delivery=%s WakeError: %s", delivery, e)
    except Exception:
        log.error(
            "wake: delivery=%s unexpected error:\n%s",
            delivery,
            traceback.format_exc(),
        )


def run(cfg: WakeConfig) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    waker = Waker(cfg)
    # 4 workers is generous; chain-limit + sane traffic means at most
    # 1-2 concurrent wakes per agent in practice.
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wake")

    server = ThreadingHTTPServer(
        (cfg.bind_host, cfg.bind_port), make_handler(cfg, waker, executor)
    )
    log.info(
        "wake-daemon: listening on %s:%d (handle=%s, cwd=%s, claude=%s)",
        cfg.bind_host,
        cfg.bind_port,
        cfg.handle,
        cfg.working_directory,
        cfg.claude_binary,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("wake-daemon: shutting down (SIGINT)")
    finally:
        server.shutdown()
        executor.shutdown(wait=False, cancel_futures=True)
    return 0
