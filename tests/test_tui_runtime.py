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


# ─── Phase 4: Dynamic Runtime Panel Population (WO-044) ──────────────────────


def test_runtime_panel_dynamic_agent_hierarchy():
    """Verify dynamic agent hierarchy rendering with tree formatting per §7, §8."""
    from cli.tui.runtime_panel import render_runtime_panel_str
    from cli.tui.state import AutonomousDeliveryState, RoleStatus

    state = AutonomousDeliveryState(
        session_id="test-p4-hierarchy",
        roles={
            "Architecture": RoleStatus("Architecture", backend="Claude", state="RUNNING"),
            "Backend": RoleStatus("Backend", backend="Codex", state="RUNNING"),
            "Frontend": RoleStatus("Frontend", backend="AGY", state="WAITING"),
            "Q/A": RoleStatus("Q/A", backend="Ollama", state="WAITING"),
            "GitOps": RoleStatus("GitOps", backend="Ollama", state="WAITING"),
        },
    )

    out = render_runtime_panel_str(width=36, state=state)

    # 1. Heading
    assert "StackMind Runtime" in out
    assert "AGENTS" in out

    # 2. Tree hierarchy formatting
    assert "◉ Architecture" in out
    assert "orchestrating" in out
    assert "├─ ● Backend" in out
    assert "implementing" in out or "running" in out
    assert "├─ ○ Frontend" in out
    assert "waiting" in out
    assert "├─ ○ Q/A" in out
    assert "└─ ○ GitOps" in out


def test_runtime_panel_never_hardcodes_roster():
    """Verify runtime panel adapts dynamically to completely arbitrary project roles per §7."""
    from cli.tui.runtime_panel import render_runtime_panel_str
    from cli.tui.state import AutonomousDeliveryState, RoleStatus

    # Project with unique, non-standard agents
    custom_roles = {
        "LeadArchitect": RoleStatus("LeadArchitect", backend="CustomLLM", state="RUNNING"),
        "DataEngineer": RoleStatus("DataEngineer", backend="SparkAgent", state="RUNNING"),
        "SecurityAuditor": RoleStatus("SecurityAuditor", backend="SecBot", state="WAITING"),
    }
    state = AutonomousDeliveryState(session_id="custom-project", roles=custom_roles)

    out = render_runtime_panel_str(width=38, state=state)

    # Must render the custom roles and NOT the hardcoded default roster
    assert "LeadArchitect" in out
    assert "DataEngineer" in out
    assert "SecurityAuditor" in out
    assert "Backend" not in out
    assert "Frontend" not in out
    assert "GitOps" not in out


def test_runtime_panel_all_standard_status_symbols():
    """Verify all 8 standard status symbols from §8 are correctly mapped."""
    from cli.tui.runtime_panel import get_status_symbol

    assert get_status_symbol("orchestrating")[0] == "◉"
    assert get_status_symbol("running")[0] == "●"
    assert get_status_symbol("implementing")[0] == "●"
    assert get_status_symbol("completed")[0] == "✓"
    assert get_status_symbol("done")[0] == "✓"
    assert get_status_symbol("waiting")[0] == "○"
    assert get_status_symbol("idle")[0] == "○"
    assert get_status_symbol("failed")[0] == "×"
    assert get_status_symbol("error")[0] == "×"
    assert get_status_symbol("cancelled")[0] == "⊘"
    assert get_status_symbol("canceled")[0] == "⊘"
    assert get_status_symbol("blocked")[0] == "!"


def test_runtime_panel_work_orders_section():
    """Verify work orders section displays actual orders with standard status symbols."""
    from cli.tui.runtime_panel import render_runtime_panel_str
    from cli.tui.state import AutonomousDeliveryState, WorkOrderItem

    state = AutonomousDeliveryState(
        session_id="test-wo-panel",
        work_orders=[
            WorkOrderItem("WO-044", "Dynamic Runtime Panel Population", status="RUNNING"),
            WorkOrderItem("WO-045", "Conversation styling & markdown", status="WAITING"),
            WorkOrderItem("WO-043", "Two-Column Workspace Layout", status="COMPLETED"),
        ],
    )

    out = render_runtime_panel_str(width=36, state=state)

    assert "WORK ORDERS" in out
    assert "● WO-044" in out
    assert "○ WO-045" in out
    assert "✓ WO-043" in out


def test_runtime_panel_current_operation_section():
    """Verify current operation section displays compact active operation and idle state."""
    from cli.tui.runtime_panel import render_runtime_panel_str
    from cli.tui.state import AutonomousDeliveryState, OperationNode

    # 1. Active operation
    state = AutonomousDeliveryState(
        session_id="test-op-panel",
        operations={
            "op-1": OperationNode("op-1", "compile_knowledge_graph", role="Backend", backend="Codex", status="RUNNING")
        },
    )
    out = render_runtime_panel_str(width=36, state=state)
    assert "CURRENT OPERATION" in out
    assert "● Backend · Codex" in out
    assert "compile_knowledge_graph" in out
    assert "(running)" in out

    # 2. Idle operation
    state_idle = AutonomousDeliveryState(
        session_id="test-op-idle",
        operations={
            "op-1": OperationNode("op-1", "prior_task", role="Backend", backend="Codex", status="COMPLETED")
        },
    )
    out_idle = render_runtime_panel_str(width=36, state=state_idle)
    assert "CURRENT OPERATION" in out_idle
    assert "○ Idle" in out_idle


def test_runtime_panel_initialization_before_first_prompt(tmp_path):
    """Verify runtime panel initializes and populates on launch before first prompt (§9, §37)."""
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.daemon import LocalDaemon
    from validators.kernel.tui import DaemonClient

    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="codex",
            provider="test",
            contract={"scope": {}},
            workspace=str(tmp_path),
        )

        state = AutonomousDeliveryState(project_name="init-test", session_id=session["session_id"])
        # Populate from runtime before any user prompt
        state.populate_from_runtime(client=client, session=session, workspace=tmp_path)

        # Confirm state is populated with actual daemon data
        assert state.session_id == session["session_id"]
        assert len(state.roles) > 0
        # Agent hierarchy can be extracted
        hierarchy = state.get_agent_hierarchy()
        assert isinstance(hierarchy, list)
        assert len(hierarchy) > 0


def test_runtime_panel_event_driven_state_transitions():
    """Verify runtime state transitions are driven by daemon events without fake states (§9)."""
    from cli.tui.runtime_panel import render_runtime_panel_str
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(
        session_id="test-event-driven",
        roles={},
        work_orders=[],
        operations={},
    )

    # Initial empty state
    panel_empty = render_runtime_panel_str(width=36, state=state)
    assert "No agents" in panel_empty
    assert "No active orders" in panel_empty
    assert "○ Idle" in panel_empty

    # Event 1: Agent spawned
    state.process_event({
        "sequence": 1,
        "name": "agent.spawned",
        "payload": {"agent_id": "codex-1", "role": "Backend", "backend": "Codex", "work_order_id": "WO-044"},
    })
    panel_spawned = render_runtime_panel_str(width=36, state=state)
    assert "Backend" in panel_spawned
    assert "●" in panel_spawned

    # Event 2: Operation started
    state.process_event({
        "sequence": 2,
        "name": "operation.started",
        "payload": {"operation": "compile_graph", "role": "Backend", "work_order_id": "WO-044"},
    })
    panel_running = render_runtime_panel_str(width=36, state=state)
    assert "CURRENT OPERATION" in panel_running
    assert "compile_graph" in panel_running

    # Event 3: Operation completed
    state.process_event({
        "sequence": 3,
        "name": "operation.completed",
        "payload": {"operation": "compile_graph", "role": "Backend", "work_order_id": "WO-044"},
    })
    assert state.roles["Backend"].state == "COMPLETED"


def test_runtime_panel_independent_scroll_and_activity_indicator():
    """Verify independent vertical scroll and '↓ New runtime activity' indicator (§10)."""
    from cli.tui.runtime_panel import RuntimePanelScroll, render_runtime_panel_str
    from cli.tui.state import AutonomousDeliveryState

    scroll = RuntimePanelScroll()
    state = AutonomousDeliveryState(session_id="test-scroll", scroll=scroll)

    # Initially at bottom -> live-following
    assert scroll.follow_bottom is True
    assert scroll.has_new_activity is False

    # Scroll upward manually
    scroll.scroll_up(3)
    assert scroll.follow_bottom is False
    assert scroll.scroll_offset == 3

    # New runtime event arrives outside viewport
    state.process_event({
        "sequence": 10,
        "name": "operation.started",
        "payload": {"operation": "async_task", "role": "Backend"},
    })
    assert scroll.has_new_activity is True

    # Panel rendering includes '↓ New runtime activity'
    out = render_runtime_panel_str(width=36, state=state, scroll=scroll)
    assert "↓ New runtime activity" in out

    # Scroll back to bottom resumes live following and clears indicator
    scroll.scroll_to_bottom()
    assert scroll.follow_bottom is True
    assert scroll.has_new_activity is False
    out_resumed = render_runtime_panel_str(width=36, state=state, scroll=scroll)
    assert "↓ New runtime activity" not in out_resumed


