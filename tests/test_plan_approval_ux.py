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
