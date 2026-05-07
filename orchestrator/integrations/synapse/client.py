"""Stdlib-only HTTP client for the Synapse REST API.

Bearer-token auth via the Authorization header. urllib + json — no
httpx, requests, or aiohttp. Keeps MS4CC's dep surface minimal.

Surface (Phase 1):
  - me()                                 → /v1/auth/me
  - list_channels()                      → /v1/channels
  - list_messages(channel, …)            → /v1/messages
  - post_message(channel, body, …)       → POST /v1/messages

Errors raise SynapseError with the response status. Caller decides
whether to fail-soft (hook path) or surface to the user (CLI path).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


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
