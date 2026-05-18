"""Stdlib-only HTTP client for the Synapse REST API.

Bearer-token auth via the Authorization header. urllib + json — no
httpx, requests, or aiohttp. Keeps MS4CC's dep surface minimal.

Surface (Phase 1):
  - me()                                 → /v1/auth/me
  - list_channels()                      → /v1/channels
  - list_messages(channel, …)            → /v1/messages
  - post_message(channel, body, …)       → POST /v1/messages
  - await_message(channel, …)            → polls /v1/messages until match or timeout
                                           (Synapse#7 sync primitive)

Errors raise SynapseError with the response status. Caller decides
whether to fail-soft (hook path) or surface to the user (CLI path).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class SynapseAwaitTimeout(Exception):
    """Raised when await_message hits its timeout with no matching message."""

    def __init__(self, channel: str, timeout: float) -> None:
        super().__init__(f"await timed out after {timeout}s on channel #{channel}")
        self.channel = channel
        self.timeout = timeout


class SynapseError(Exception):
    def __init__(self, status: int, detail: str, body: Any = None) -> None:
        super().__init__(f"[{status}] {detail}")
        self.status = status
        self.detail = detail
        self.body = body


@dataclass(frozen=True)
class Message:
    id: str
    channel: str
    sender_handle: str
    sender_kind: str  # 'human' | 'agent'
    body: str
    body_format: str
    created_at: str  # ISO-8601
    mentioned_handles: tuple[str, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=str(data["id"]),
            channel=str(data["channel"]),
            sender_handle=str(data["sender_handle"]),
            sender_kind=str(data["sender_kind"]),
            body=str(data["body"]),
            body_format=str(data.get("body_format", "markdown")),
            created_at=str(data["created_at"]),
            mentioned_handles=tuple(data.get("mentioned_handles", []) or []),
        )


@dataclass(frozen=True)
class MessagesPage:
    messages: tuple[Message, ...]
    next_cursor: str | None
    head_cursor: str | None


def _await_match(
    msg: "Message",
    mention_filter: str | None,
    require_sender: str | None,
    body_contains: str | None,
) -> bool:
    """AND-combine the filter predicates for await_message."""
    if mention_filter is not None:
        handle = mention_filter.lstrip("@")
        if handle not in msg.mentioned_handles:
            return False
    if require_sender is not None:
        if msg.sender_handle != require_sender.lstrip("@"):
            return False
    if body_contains is not None:
        if body_contains not in msg.body:
            return False
    return True


class SynapseClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    # --- HTTP plumbing ----------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int | bool] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            cleaned = {k: str(v) for k, v in query.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(cleaned)}"

        data: bytes | None = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
                detail = (
                    payload.get("detail")
                    if isinstance(payload, dict)
                    else None
                ) or e.reason
            except Exception:
                payload = None
                detail = e.reason or "HTTP error"
            raise SynapseError(e.code, str(detail), payload) from e
        except urllib.error.URLError as e:
            raise SynapseError(0, f"Network error: {e.reason}") from e

    # --- API surface ------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/v1/auth/me")

    def list_channels(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/channels")
        if isinstance(data, dict) and "channels" in data:
            return list(data["channels"])
        return list(data) if isinstance(data, list) else []

    def list_messages(
        self,
        channel: str,
        *,
        since: str | None = None,
        mentions_me: bool = False,
        limit: int = 20,
        order: str = "asc",
    ) -> MessagesPage:
        query: dict[str, str | int | bool] = {
            "channel": channel,
            "limit": limit,
            "order": order,
        }
        if since is not None:
            query["since"] = since
        if mentions_me:
            query["mentions_me"] = "true"

        data = self._request("GET", "/v1/messages", query=query)
        if not isinstance(data, dict):
            return MessagesPage(messages=(), next_cursor=None, head_cursor=None)
        msgs = tuple(Message.from_json(m) for m in data.get("messages", []) or [])
        return MessagesPage(
            messages=msgs,
            next_cursor=data.get("next_cursor"),
            head_cursor=data.get("head_cursor"),
        )

    def await_message(
        self,
        channel: str,
        *,
        since: str | None = None,
        mention_filter: str | None = None,
        require_sender: str | None = None,
        body_contains: str | None = None,
        timeout: float = 180.0,
        poll_interval: float = 1.5,
        max_poll_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Message:
        """Block until a matching message arrives on `channel`, or `timeout`
        elapses (raises SynapseAwaitTimeout). Synapse#7 sync primitive.

        Filters (all optional, AND-combined):
          - `mention_filter`  — message.mentioned_handles must contain this handle
                                (case-sensitive, no `@` prefix; e.g. "aegis")
          - `require_sender`  — message.sender_handle must equal this handle
          - `body_contains`   — message.body must contain this substring (literal)

        Polling shape: poll every `poll_interval` seconds with backoff up to
        `max_poll_interval` on empty results. Resets to `poll_interval` on
        any new message (even if filter rejects). Uses the existing
        `list_messages` cursor pagination — no new server endpoint.

        `since`: optional cursor to start polling from. If None, the caller
        is expected to have captured the channel's head_cursor BEFORE posting
        their question, and passed it here so we don't miss a fast reply.
        """
        deadline = clock() + timeout
        cursor = since
        current_poll = poll_interval

        # If no cursor was provided, anchor on current head so we only see
        # NEW messages from this point forward.
        if cursor is None:
            head = self.list_messages(channel, limit=1, order="desc")
            cursor = head.head_cursor

        while clock() < deadline:
            page = self.list_messages(
                channel, since=cursor, limit=20, order="asc"
            )
            if page.messages:
                current_poll = poll_interval  # reset backoff on activity
                for msg in page.messages:
                    if _await_match(msg, mention_filter, require_sender, body_contains):
                        return msg
                # No match — advance cursor and keep polling.
                cursor = page.next_cursor or page.head_cursor or cursor
            else:
                current_poll = min(current_poll * 1.5, max_poll_interval)

            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(current_poll, remaining))

        raise SynapseAwaitTimeout(channel, timeout)

    def post_message(
        self,
        channel: str,
        body: str,
        *,
        body_format: str = "markdown",
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> Message:
        payload: dict[str, Any] = {
            "channel": channel,
            "body": body,
            "body_format": body_format,
        }
        if thread_id is not None:
            payload["thread_id"] = thread_id
        if reply_to is not None:
            payload["reply_to"] = reply_to
        data = self._request("POST", "/v1/messages", body=payload)
        return Message.from_json(data)
