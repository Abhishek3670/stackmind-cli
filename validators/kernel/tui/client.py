"""HTTP/JSON-RPC client for the StackMind daemon; it has no execution privileges."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen


class DaemonClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self._request_id = 0

    def call(self, method: str, **params: Any) -> Any:
        self._request_id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        ).encode()
        request = Request(
            f"{self.url}/rpc", data=payload, headers={"Content-Type": "application/json"}
        )
        with urlopen(request) as response:
            body = json.loads(response.read())
        if "error" in body:
            raise RuntimeError(body["error"]["message"])
        return body["result"]

    def create_session(self, **params: Any) -> dict[str, Any]:
        return self.call("session.create", **params)

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

    def approve(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        return self.call(
            "session.approval", session_id=session_id, approved=approved, reason=reason
        )

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.call("event.list", session_id=session_id, after=after)
