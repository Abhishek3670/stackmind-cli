from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from cli.tui.state import AutonomousDeliveryState, ProjectPhase
from cli.tui.runtime_panel import format_current_operation
from cli.tui.commands import dispatch_delivery_command


def test_runtime_panel_shows_plan_ready_when_awaiting_approval():
    state = AutonomousDeliveryState(session_id="test-plan-ux")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-003", "state": "AWAITING_APPROVAL"}

    lines = format_current_operation(None, width=40, state=state)
    rendered = "".join(l.plain for l in lines)
    assert "Plan Ready (PLAN-003)" in rendered
    assert "Run :approve to execute" in rendered


def test_plan_proposed_event_adds_chat_message():
    state = AutonomousDeliveryState(session_id="test-plan-ux")
    event = {
        "name": "plan.proposed",
        "payload": {
            "plan_id": "PLAN-003",
            "metadata": {"work_orders": [{"id": "WO-009"}, {"id": "WO-010"}]},
        },
    }
    state.process_event(event)
    assert state.phase == ProjectPhase.AWAITING_APPROVAL
    assert any("PLAN READY" in str(getattr(m, "content", "")) for m in state.messages)


def test_continue_command_auto_approves_when_awaiting_approval():
    state = AutonomousDeliveryState(session_id="test-plan-ux")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-003", "state": "AWAITING_APPROVAL"}

    mock_client = MagicMock()
    mock_adapter = MagicMock()
    session = {"session_id": "test-sess", "workspace": "."}

    dispatch_delivery_command(
        mock_adapter,
        mock_client,
        session,
        "continue",
        state,
    )

    # Should call client.plan_approve for PLAN-003
    mock_client.plan_approve.assert_called_once_with(
        "test-sess", "PLAN-003", reason="Approved by operator"
    )


def test_approve_without_plan_forwards_approval_turn_to_architect_in_tty(tmp_path):
    """When :approve is entered in TTY mode with no plan or WOs on disk, forward approval to Lead Architect."""
    state = AutonomousDeliveryState(session_id="test-forward-approval", work_orders=[])
    state.phase = ProjectPhase.INITIALIZING

    mock_client = MagicMock()
    mock_client.plan_get.return_value = None
    mock_client.run_get.return_value = None
    mock_client.get_session.return_value = {"active_operation": None}

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = None

    calls = []
    def mock_stream(adapter, client, session, state, **kwargs):
        calls.append(kwargs)
        return session, False

    import cli.tui.commands as cmd_mod
    orig_stream = cmd_mod._stream_turn_execution
    cmd_mod._stream_turn_execution = mock_stream
    import sys
    orig_isatty = sys.stdout.isatty
    sys.stdout.isatty = lambda: True
    try:
        session = {"session_id": "test-sess", "workspace": str(tmp_path)}
        dispatch_delivery_command(
            mock_adapter,
            mock_client,
            session,
            ":approve proceed with login page",
            state,
        )

        assert any("Forwarding operator approval" in str(getattr(m, "content", "")) for m in state.messages)
        # Verify adapter.command called with the approval turn
        mock_adapter.command.assert_any_call(":approve proceed with login page", session_id="test-sess")
    finally:
        cmd_mod._stream_turn_execution = orig_stream
        sys.stdout.isatty = orig_isatty


def test_timeout_command_persists_to_settings(tmp_path):
    """Setting :timeout <seconds> updates state and persists client_timeout into settings.json."""
    from validators.kernel.settings import load_settings
    state = AutonomousDeliveryState(session_id="test-timeout-ux", work_orders=[])
    mock_client = MagicMock()
    mock_adapter = MagicMock()
    session = {"session_id": "test-sess", "workspace": str(tmp_path)}

    dispatch_delivery_command(
        mock_adapter,
        mock_client,
        session,
        ":timeout 900",
        state,
    )

    assert state.client_timeout == 900.0
    saved_settings = load_settings(tmp_path)
    assert saved_settings.client_timeout == 900


def test_stream_idle_timeout_command_persists_to_settings(tmp_path):
    """Setting :stream_idle_timeout updates state, os.environ, and persists into settings.json."""
    import os
    from validators.kernel.settings import load_settings
    state = AutonomousDeliveryState(session_id="test-stream-to-ux", work_orders=[])
    mock_client = MagicMock()
    mock_adapter = MagicMock()
    session = {"session_id": "test-sess", "workspace": str(tmp_path)}

    # 1. Query initial stream idle timeout
    dispatch_delivery_command(
        mock_adapter,
        mock_client,
        session,
        ":stream_idle_timeout",
        state,
    )
    assert any("Current stream idle timeout" in str(getattr(m, "content", "")) for m in state.messages)

    # 2. Set new stream idle timeout to 900s
    dispatch_delivery_command(
        mock_adapter,
        mock_client,
        session,
        ":stream_idle_timeout 900",
        state,
    )
    assert state.stream_idle_timeout == 900.0
    assert os.environ.get("PROVIDER_STREAM_IDLE_TIMEOUT") == "900"
    saved = load_settings(tmp_path)
    assert saved.stream_idle_timeout == 900

    # 3. Test alias :stream-timeout
    dispatch_delivery_command(
        mock_adapter,
        mock_client,
        session,
        ":stream-timeout 1200",
        state,
    )
    assert state.stream_idle_timeout == 1200.0
    assert os.environ.get("PROVIDER_STREAM_IDLE_TIMEOUT") == "1200"
    saved = load_settings(tmp_path)
    assert saved.stream_idle_timeout == 1200
