"""Minimal JSON-RPC 2.0 protocol for the local daemon."""

from __future__ import annotations

from typing import Any

from cli import __version__

from .manager import SessionManager


_PROTOCOL_VERSION = 1
_CAPABILITIES = ["session", "operation", "events", "cooperative_cancellation"]


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class JsonRpcProtocol:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            return self._error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "Invalid Request")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "params must be an object")
        try:
            result = self._dispatch(request["method"], params)
        except _RpcError as error:
            return self._error(request_id, error.code, error.message)
        except KeyError as error:
            message = str(error).lower()
            if "operation" in message:
                return self._error(request_id, -32002, "Operation not found")
            return self._error(request_id, -32001, "Session not found")
        except PermissionError:
            return self._error(request_id, -32003, "Policy denied")
        except InterruptedError:
            return self._error(request_id, -32005, "Request cancelled")
        except (TypeError, ValueError):
            return self._error(request_id, -32602, "Invalid params")
        except Exception:  # pragma: no cover - defensive protocol boundary
            return self._error(request_id, -32603, "Internal error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "health.version":
            requested = params.get("protocol_version")
            if requested is not None and requested != _PROTOCOL_VERSION:
                raise _RpcError(-32004, "Protocol mismatch")
            return {
                "package_version": __version__,
                "protocol_version": _PROTOCOL_VERSION,
                "capabilities": _CAPABILITIES,
            }
        if method == "session.create":
            return self.manager.create_session(**params)
        if method in {"session.get", "session.attach"}:
            return self.manager.get_session(params["session_id"])
        if method == "session.list":
            return self.manager.list_sessions()
        if method == "session.history":
            return self.manager.session_history(params["session_id"])
        if method == "session.pause":
            return self.manager.pause_session(params["session_id"])
        if method == "session.resume":
            return self.manager.resume_session(params["session_id"])
        if method == "session.cancel":
            return self.manager.cancel_session(params["session_id"])
        if method == "session.close":
            return self.manager.close_session(params["session_id"])
        if method in {"session.turn", "operation.turn"}:
            turn_params = {
                key: value for key, value in params.items() if key not in {"session_id", "prompt"}
            }
            return self.manager.start_turn(params["session_id"], params["prompt"], **turn_params)
        if method == "operation.begin":
            operation_name = params.get("operation", params.get("operation_name"))
            if not isinstance(operation_name, str) or not operation_name:
                raise ValueError("operation is required")
            cancel, operation_id = self.manager.begin_operation(
                params["session_id"],
                operation_name,
                params.get("metadata"),
                parent_operation_id=params.get("parent_operation_id"),
                work_order_id=params.get("work_order_id"),
                contract_scope=params.get("contract_scope"),
            )
            del cancel
            return self.manager.get_operation(operation_id)
        if method == "operation.get":
            return self.manager.get_operation(params["operation_id"])
        if method == "operation.list":
            return self.manager.list_operations(params.get("session_id"))
        if method == "operation.cancel":
            return self.manager.cancel_operation(
                params["operation_id"], bool(params.get("cascade", False))
            )
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
        raise _RpcError(-32601, "Method not found")
