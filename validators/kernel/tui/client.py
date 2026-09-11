"""HTTP/JSON-RPC client for the StackMind daemon; it has no execution privileges."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


class DaemonClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self._request_id = 0
        self._opener = build_opener(ProxyHandler({}))

    def call(self, method: str, **params: Any) -> Any:
        self._request_id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        ).encode()
        request = Request(
            f"{self.url}/rpc", data=payload, headers={"Content-Type": "application/json"}
        )
        with self._opener.open(request) as response:
            body = json.loads(response.read())
        if "error" in body:
            raise RuntimeError(body["error"]["message"])
        return body["result"]

    def create_session(self, **params: Any) -> dict[str, Any]:
        return self.call("session.create", **params)

    def health_version(self, protocol_version: int | None = None) -> dict[str, Any]:
        params = {} if protocol_version is None else {"protocol_version": protocol_version}
        return self.call("health.version", **params)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.call("session.get", session_id=session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.call("session.list")

    def pause(self, session_id: str) -> dict[str, Any]:
        return self.call("session.pause", session_id=session_id)

    def resume(self, session_id: str) -> dict[str, Any]:
        return self.call("session.resume", session_id=session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self.call("session.cancel", session_id=session_id)

    def session_history(self, session_id: str) -> list[dict[str, Any]]:
        return self.call("session.history", session_id=session_id)

    def session_close(self, session_id: str) -> dict[str, Any]:
        return self.call("session.close", session_id=session_id)

    def operation_begin(self, session_id: str, operation: str, **params: Any) -> dict[str, Any]:
        return self.call("operation.begin", session_id=session_id, operation=operation, **params)

    def operation_get(self, operation_id: str) -> dict[str, Any]:
        return self.call("operation.get", operation_id=operation_id)

    def operation_cancel(self, operation_id: str, cascade: bool = False) -> dict[str, Any]:
        return self.call("operation.cancel", operation_id=operation_id, cascade=cascade)

    def turn(self, session_id: str, prompt: str, **params: Any) -> dict[str, Any]:
        return self.call("session.turn", session_id=session_id, prompt=prompt, **params)

    def approve(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        return self.call(
            "session.approval", session_id=session_id, approved=approved, reason=reason
        )

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.call("event.list", session_id=session_id, after=after)

    def stream_events(
        self, session_id: str | None = None, after: int = 0
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"after": after}
        if session_id is not None:
            params["session_id"] = session_id
        request = Request(f"{self.url}/events?{urlencode(params)}")
        with self._opener.open(request) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data: "):
                    yield json.loads(line[6:])
