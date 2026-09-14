from __future__ import annotations

from pathlib import Path

import pytest

from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import (
    DaemonClient,
    StackMindTuiAdapter,
    activity_line,
    diff_viewer,
    hitl_prompt,
    verification_matrix,
)


def session_params(tmp_path: Path) -> dict:
    return {
        "agent": "codex",
        "provider": "test",
        "contract": {"scope": {}},
        "workspace": str(tmp_path),
    }


def test_tui_daemon_connection_and_lifecycle(tmp_path):
    with LocalDaemon(tmp_path / "daemon") as daemon:
        adapter = StackMindTuiAdapter(DaemonClient(daemon.url))
        session = adapter.command(":new", **session_params(tmp_path))
        assert adapter.command(f":pause {session['session_id']}")["state"] == "PAUSED"
        assert adapter.client.resume(session["session_id"])["state"] == "RUNNING"
        assert adapter.command(f":cancel {session['session_id']}")["state"] == "CANCELLED"


def test_tui_event_streaming_and_reconnect_cursor(tmp_path):
    with LocalDaemon(tmp_path / "daemon") as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(**session_params(tmp_path))
        adapter = StackMindTuiAdapter(client)
        first = list(adapter.stream(session["session_id"]))
        assert [event["name"] for event in first][:2] == ["session.started", "attempt.started"]
        daemon.manager.events.publish("provider.token", session["session_id"], delta="hello")
        assert [event["payload"]["delta"] for event in adapter.stream(session["session_id"])] == [
            "hello"
        ]


def test_tui_hitl_approval_flow_is_daemon_recorded(tmp_path):
    with LocalDaemon(tmp_path / "daemon") as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(**session_params(tmp_path))
        adapter = StackMindTuiAdapter(client)
        assert (
            adapter.decide(session["session_id"], True, "reviewed")["journal"][-1]["status"]
            == "APPROVED"
        )
        assert adapter.decide(session["session_id"], False)["journal"][-1]["status"] == "REJECTED"
        assert "[Approve] [Reject]" in hitl_prompt("write_file", "tests/a.py")


def test_tui_verification_and_diff_presentation():
    dimensions = {
        name: True for name in ("scope", "state", "ast", "behavioral", "security", "outcome")
    }
    assert "PASS" in verification_matrix(dimensions)
    assert diff_viewer("--- a/file\n+++ b/file") == "--- a/file\n+++ b/file"
    assert "✓ Allowed" in activity_line({"name": "operation.started", "payload": {}})


def test_tui_runtime_boundary_isolation(tmp_path):
    adapter = StackMindTuiAdapter(DaemonClient("http://127.0.0.1:1"))
    with pytest.raises(ValueError, match="runtime-owned"):
        adapter.command("fix the issue")
    assert not hasattr(adapter, "run_command") and not hasattr(adapter.client, "read_file")


def test_tui_adapter_whitespace_and_argument_hardening():
    class _StubClient:
        def __init__(self):
            self.calls = []

        def get_session(self, sid):
            self.calls.append(("get_session", sid))
            return {"session_id": sid}

        def resume(self, sid):
            self.calls.append(("resume", sid))
            return {"session_id": sid, "state": "RUNNING"}

        def pause(self, sid):
            self.calls.append(("pause", sid))
            return {"session_id": sid, "state": "PAUSED"}

        def cancel(self, sid):
            self.calls.append(("cancel", sid))
            return {"session_id": sid, "state": "CANCELLED"}

        def approve(self, sid, approved, reason):
            self.calls.append(("approve", sid, approved, reason))
            return {"session_id": sid, "approved": approved}

        def turn(self, sid, prompt, **params):
            self.calls.append(("turn", sid, prompt, params))
            return {"operation_id": "op-1"}

    stub = _StubClient()
    adapter = StackMindTuiAdapter(stub)  # type: ignore[arg-type]

    # Command with surrounding and internal whitespace
    res = adapter.command("   :status    sess-abc   ")
    assert res == {"session_id": "sess-abc"}
    assert stub.calls[-1] == ("get_session", "sess-abc")

    adapter.command("   :pause    sess-abc   ")
    assert stub.calls[-1] == ("pause", "sess-abc")

    adapter.command("   :resume    sess-abc   ")
    assert stub.calls[-1] == ("resume", "sess-abc")

    adapter.command("   :cancel    sess-abc   ")
    assert stub.calls[-1] == ("cancel", "sess-abc")

    adapter.command("   :approve   looks good   ", session_id="sess-abc")
    assert stub.calls[-1] == ("approve", "sess-abc", True, "looks good")

    adapter.command("   :reject   not ready   ", session_id="sess-abc")
    assert stub.calls[-1] == ("approve", "sess-abc", False, "not ready")

    # Prompt with whitespace preserved cleanly
    adapter.command("   write unit test   ", session_id="sess-abc")
    assert stub.calls[-1] == ("turn", "sess-abc", "write unit test", {})


def test_daemon_client_http_error_and_safe_lists(monkeypatch):
    import io
    from urllib.error import HTTPError

    client = DaemonClient("http://127.0.0.1:9999")

    # Simulate HTTPError containing JSON-RPC error payload
    error_body = b'{"jsonrpc":"2.0","error":{"code":-32600,"message":"Invalid request payload"}}'
    fake_http_error = HTTPError("http://127.0.0.1:9999/rpc", 400, "Bad Request", {}, io.BytesIO(error_body))

    def fake_open(req):
        raise fake_http_error

    monkeypatch.setattr(client._opener, "open", fake_open)

    with pytest.raises(RuntimeError, match="Invalid request payload"):
        client.call("test.method")

    # Test safe list fallbacks when daemon returns dict wrapping or unexpected types
    monkeypatch.setattr(client, "call", lambda method, **params: {"events": [{"sequence": 1}]})
    assert client.events("session-1") == [{"sequence": 1}]

    monkeypatch.setattr(client, "call", lambda method, **params: {"sessions": [{"session_id": "s1"}]})
    assert client.list_sessions() == [{"session_id": "s1"}]

    monkeypatch.setattr(client, "call", lambda method, **params: {"history": [{"role": "user"}]})
    assert client.session_history("session-1") == [{"role": "user"}]

    monkeypatch.setattr(client, "call", lambda method, **params: {"children": [{"operation_id": "op-2"}]})
    assert client.operation_children("op-1") == [{"operation_id": "op-2"}]

    monkeypatch.setattr(client, "call", lambda method, **params: None)
    assert client.events("session-1") == []
    assert client.list_sessions() == []
    assert client.session_history("session-1") == []
    assert client.operation_children("op-1") == []


def test_daemon_client_stream_events_malformed_tolerance(monkeypatch):
    import io

    client = DaemonClient("http://127.0.0.1:9999")

    sse_data = (
        b": heartbeat comment\n"
        b"\n"
        b"data: {corrupted json\n"
        b"data: \"not a dict\"\n"
        b"data: {\"sequence\": 1, \"name\": \"test.event\"}\n"
        b"\n"
    )

    class _FakeResponse:
        def __enter__(self):
            return io.BytesIO(sse_data)

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(client._opener, "open", lambda req: _FakeResponse())

    events = list(client.stream_events(session_id="s1"))
    assert len(events) == 1
    assert events[0]["name"] == "test.event"


def test_state_resilience_to_malformed_events_and_sessions():
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="test-session")

    # None or non-mapping inputs must not raise
    state.update_from_session(None)  # type: ignore[arg-type]
    state.update_from_session({})
    state.update_from_session({"plans": None, "journal": "not a list"})
    state.update_from_session({"plans": {"p1": None}, "journal": [None, "invalid"]})

    state.sync_work_orders(None)  # type: ignore[arg-type]
    state.sync_work_orders(["not a mapping", None])  # type: ignore[list-item]

    state.process_event(None)  # type: ignore[arg-type]
    state.process_event({})
    state.process_event({"name": "plan.proposed", "payload": None})
    state.process_event({"name": "plan.approved", "payload": {"created_records": "invalid"}})
    state.process_event({"name": "plan.rejected", "payload": None})
    state.process_event({"name": "event.agentSpawned", "payload": None})
    state.process_event({"name": "operation.started", "payload": None})
    state.process_event({"name": "operation.completed", "payload": None})
    state.process_event({"name": "operation.cancelled", "payload": None})
    state.process_event({"name": "tool_call.write", "payload": None})
    state.process_event({"name": "event.toolCall", "payload": {"arguments": None}})
    state.process_event({"name": "event.toolResult", "payload": None})
    state.process_event({"name": "verification.completed", "payload": {"result": None}})
    state.process_event({"name": "project.completed", "payload": None})

    assert state.session_id == "test-session"

