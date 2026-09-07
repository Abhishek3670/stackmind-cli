"""Minimal JSON-RPC 2.0 protocol for the local daemon."""

from __future__ import annotations

from typing import Any

from .manager import SessionManager


class JsonRpcProtocol:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "Invalid Request")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "params must be an object")
        try:
            result = self._dispatch(request["method"], params)
        except (KeyError, ValueError) as error:
            return self._error(request_id, -32602, str(error))
        except Exception as error:  # pragma: no cover - defensive protocol boundary
            return self._error(request_id, -32603, str(error))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "session.create":
            return self.manager.create_session(**params)
        if method in {"session.get", "session.attach"}:
            return self.manager.get_session(params["session_id"])
        if method == "session.list":
            return self.manager.list_sessions()
        if method == "session.pause":
            return self.manager.pause_session(params["session_id"])
        if method == "session.resume":
            return self.manager.resume_session(params["session_id"])
        if method == "session.cancel":
            return self.manager.cancel_session(params["session_id"])
        if method == "session.approval":
            self.manager.record_approval(
                params["session_id"], bool(params["approved"]), str(params.get("reason", ""))
            )
            return self.manager.get_session(params["session_id"])
        if method == "event.list":
            return [
                event.as_dict()
                for event in self.manager.events.events(
                    params.get("session_id"), int(params.get("after", 0))
                )
            ]
        raise ValueError("Method not found")
