"""Versioned JSON-RPC contract tests for the local daemon."""

from __future__ import annotations

from cli import __version__
from validators.kernel.daemon import DaemonStorage, LocalDaemon, SessionManager
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.tui.client import DaemonClient


def _contract() -> dict[str, object]:
    return {"agent_id": "codex", "work_order": "WO-013", "scope": {"allow": ["tests/**"]}}


def _request(method: str, params: dict[str, object] | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_protocol_handshake_and_typed_errors(tmp_path):
    protocol = JsonRpcProtocol(SessionManager(DaemonStorage(tmp_path)))

    version = protocol.handle(_request("health.version"))["result"]
    assert version["package_version"] == __version__
    assert version["capabilities"] == [
        "session",
        "operation",
        "events",
        "cooperative_cancellation",
        "executionBackends",
        "subagents",
    ]

    mismatch = protocol.handle(_request("health.version", {"protocol_version": 2}))
    assert mismatch["error"] == {"code": -32004, "message": "Protocol mismatch"}
    assert protocol.handle([])["error"] == {"code": -32600, "message": "Invalid Request"}
    assert protocol.handle(_request("not.a.method"))["error"] == {
        "code": -32601,
        "message": "Method not found",
    }
    invalid = protocol.handle(_request("session.create", {"agent": "codex"}))
    assert invalid["error"] == {"code": -32602, "message": "Invalid params"}
    missing_session = protocol.handle(_request("session.get", {"session_id": "missing"}))
    assert missing_session["error"] == {"code": -32001, "message": "Session not found"}
    missing_operation = protocol.handle(_request("operation.get", {"operation_id": "missing"}))
    assert missing_operation["error"] == {"code": -32002, "message": "Operation not found"}
    assert "Traceback" not in str(invalid)


def test_session_operation_and_event_namespaces(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    protocol = JsonRpcProtocol(manager)
    created = protocol.handle(
        _request(
            "session.create",
            {"agent": "codex", "provider": "test", "contract": _contract(), "workspace": "workspace"},
        )
    )["result"]
    session_id = created["session_id"]

    assert protocol.handle(_request("session.get", {"session_id": session_id}))["result"]["session_id"] == session_id
    assert len(protocol.handle(_request("session.list"))["result"]) == 1
    assert protocol.handle(_request("session.pause", {"session_id": session_id}))["result"]["state"] == "PAUSED"
    assert protocol.handle(_request("session.resume", {"session_id": session_id}))["result"]["state"] == "RUNNING"

    operation = protocol.handle(
        _request(
            "operation.begin",
            {
                "session_id": session_id,
                "operation": "provider.call",
                "metadata": {"source": "test"},
                "work_order_id": "WO-013",
            },
        )
    )["result"]
    operation_id = operation["operation_id"]
    assert operation["status"] == "RUNNING"
    assert protocol.handle(_request("operation.get", {"operation_id": operation_id}))["result"]["operation"] == "provider.call"
    assert [item["operation_id"] for item in protocol.handle(_request("operation.list", {"session_id": session_id}))["result"]] == [operation_id]
    assert protocol.handle(_request("operation.cancel", {"operation_id": operation_id}))["result"]["status"] == "CANCEL_REQUESTED"
    manager.complete_operation(session_id, operation_id)

    history = protocol.handle(_request("session.history", {"session_id": session_id}))["result"]
    assert history[0]["status"] == "CANCELLED"
    events = protocol.handle(_request("event.list", {"session_id": session_id}))["result"]
    assert any(event["name"] == "operation.cancelled" for event in events)
    assert protocol.handle(_request("session.close", {"session_id": session_id}))["result"]["state"] == "COMPLETED"


def test_daemon_client_exposes_versioned_operation_helpers(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        client = DaemonClient(daemon.url)
        assert client.health_version()["protocol_version"] == 1
        created = client.create_session(
            agent="codex", provider="test", contract=_contract(), workspace="workspace"
        )
        operation = client.operation_begin(created["session_id"], "provider.call")
        assert client.operation_get(operation["operation_id"])["operation_id"] == operation["operation_id"]
        assert client.operation_cancel(operation["operation_id"])["status"] == "CANCEL_REQUESTED"
        assert client.session_history(created["session_id"])
