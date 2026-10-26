"""Tests for Architect Product Delivery Status prompt feature rules and delivery lifecycle."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from validators.harness.runner import AgentRunner, HarnessTask, LLMRequest
from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState, Phase, AdvanceResult
from validators.kernel.daemon.storage import DaemonStorage


def test_architect_delivery_status_prompt_rules(tmp_path: Path) -> None:
    """AgentRunner constructs dedicated system prompt with all 6 Product Delivery Status feature rules."""
    runner = AgentRunner(
        project_path=tmp_path,
        agent="claude",
    )
    task = HarnessTask(
        kind="delivery_status",
        identifier="task-delivery-status",
        path=tmp_path / "adhoc.md",
        title="Provide the product delivery status report",
        body="All tasks are completed. As lead architect, provide the official product delivery status.",
        query="product delivery status",
        is_delivery_status=True,
    )
    request = LLMRequest(
        agent="claude",
        session_count=1,
        task=task,
        context=MagicMock(text=""),
        retrieval=MagicMock(),
    )

    # Mock tool runtime
    mock_runtime = MagicMock()
    mock_runtime.gateway.contract = None
    mock_runtime.workspace.attempt_id = "att-1"
    mock_runtime.workspace.root = tmp_path

    # Capture messages handed to gateway.run_loop
    captured_messages = []
    with patch("validators.kernel.providers.gateway.ProviderGateway") as mock_gw_cls:
        mock_gw = MagicMock()
        mock_gw_cls.return_value = mock_gw

        def fake_run_loop(messages, **kwargs):
            nonlocal captured_messages
            captured_messages = messages
            from validators.kernel.providers.models import Message
            return [
                Message.assistant(
                    '{"status": "completed", "summary": "Delivery status delivered", "report_markdown": "# Delivery Status\\nAll good."}'
                )
            ]

        mock_gw.run_loop = fake_run_loop
        runner._complete_request(request, mock_runtime)

    assert len(captured_messages) >= 2
    system_msg = captured_messages[0].content
    user_msg = captured_messages[1].content

    # Verify all 6 prompt feature rules are present in the system message
    assert "PRODUCT DELIVERY STATUS PROMPT FEATURE RULES" in system_msg
    assert "Executive Goal Fulfillment" in system_msg
    assert "Architecture & Components Delivered" in system_msg
    assert "Release & Versioning Manifest" in system_msg
    assert "Quality Assurance & Verification" in system_msg
    assert "Getting Started & Execution Guide" in system_msg
    assert "HarnessDecision" in system_msg

    # Verify user instructions enforce the rules
    assert "Product Delivery Status report" in user_msg


def test_supervisor_format_delivery_status_prompt(tmp_path: Path) -> None:
    """format_delivery_status_prompt compiles verified facts from workspace and state."""
    (tmp_path / "VERSION.md").write_text("1.2.0\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## 1.2.0\n- Initial feature release\n", encoding="utf-8")

    wo_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text("id: WO-001\ntitle: Build Core API\nassigned_agents: [codex]\n", encoding="utf-8")
    (wo_dir / "WO-002.yaml").write_text("id: WO-002\ntitle: Build Web UI\nassigned_agents: [gemini]\n", encoding="utf-8")

    state = RunState(
        run_id="run-test",
        product_goal="Build a genset rental portal",
        workspace=str(tmp_path),
        session_id="sess-test",
        completed_wo_ids=["WO-001", "WO-002"],
        release_commit_sha="a1b2c3d4e5f6",
    )

    supervisor = LifecycleSupervisor(MagicMock())
    prompt = supervisor.format_delivery_status_prompt(state)

    assert "Build a genset rental portal" in prompt
    assert "1.2.0" in prompt
    assert "a1b2c3d4e5f6" in prompt
    assert "WO-001" in prompt
    assert "Build Core API" in prompt
    assert "WO-002" in prompt
    assert "Build Web UI" in prompt
    assert "APPROVED by Gemma" in prompt
    assert "APPROVED by Claude" in prompt


def test_supervisor_generate_delivery_status_report(tmp_path: Path) -> None:
    """generate_delivery_status_report produces deterministic markdown report."""
    (tmp_path / "VERSION.md").write_text("0.5.0\n", encoding="utf-8")

    state = RunState(
        run_id="run-test",
        product_goal="Build rate limiter module",
        workspace=str(tmp_path),
        session_id="sess-test",
        completed_wo_ids=["WO-001"],
        release_commit_sha="commit-sha-123",
    )

    supervisor = LifecycleSupervisor(MagicMock())
    report = supervisor.generate_delivery_status_report(state)

    assert "PRODUCT DELIVERY STATUS" in report
    assert "Build rate limiter module" in report
    assert "0.5.0" in report
    assert "commit-sha-123" in report
    assert "WO-001" in report
    assert "QA Verdict: ALL PASS" in report
    assert "Integration Review: APPROVED" in report


def test_manager_drive_run_triggers_delivery_status(tmp_path: Path) -> None:
    """When a run reaches COMPLETE, manager automatically executes the Architect delivery status turn."""
    from validators.kernel.daemon.manager import SessionManager
    from threading import Event

    storage = DaemonStorage(tmp_path / "daemon-state.json")
    manager = SessionManager(storage)
    session = manager.create_session(
        agent="claude",
        provider="daemon",
        contract={},
        workspace=str(tmp_path),
    )
    sid = session["session_id"]

    state = RunState(
        run_id="run-auto-deliv",
        product_goal="Deliver autonomous site",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.GITOPS,
        completed_wo_ids=["WO-001"],
    )
    manager._active_runs[state.run_id] = state
    stop_event = Event()

    # Mock supervisor advance so it completes
    def fake_advance(s: RunState) -> AdvanceResult:
        s.phase = Phase.COMPLETE
        return AdvanceResult.COMPLETE

    manager.supervisor.advance = fake_advance

    # Track start_turn calls
    turns_called = []
    def fake_start_turn(session_id, prompt, **kwargs):
        turns_called.append({"session_id": session_id, "prompt": prompt, "kwargs": kwargs})
        # Add operation record to session journal so manager.get_operation succeeds
        op_id = "op-deliv-turn"
        session["journal"].append({
            "operation_id": op_id,
            "status": "COMPLETED",
            "result": {"status": "completed", "summary": "Product delivery successfully concluded."},
        })
        return {"operation_id": op_id}

    manager.start_turn = fake_start_turn

    # Execute driver loop
    manager._drive_run(state.run_id, stop_event)

    assert state.phase == Phase.COMPLETE
    assert state.delivery_status_delivered is True
    assert len(turns_called) == 1
    call = turns_called[0]
    assert call["kwargs"]["role"] == "architecture"
    assert call["kwargs"]["agent_id"] == "claude"
    assert call["kwargs"]["is_delivery_status"] is True
    assert state.delivery_status_summary == "Product delivery successfully concluded."


def test_dispatch_delivery_status_command(tmp_path: Path) -> None:
    """TUI :delivery-status command displays the official Lead Architect delivery report in chat."""
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.tui.adapter import StackMindTuiAdapter
    import json

    mock_client = MagicMock()
    mock_adapter = StackMindTuiAdapter(mock_client)

    state_dir = tmp_path / ".sync" / "runtime" / "daemon"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "active_run.json").write_text(
        json.dumps({
            "run_id": "run-001",
            "product_goal": "Build landing page",
            "phase": "COMPLETE",
            "delivery_status_summary": "# Lead Architect Report\nDelivery complete and verified.",
        }),
        encoding="utf-8",
    )

    session = {"session_id": "sess-tui-1", "workspace": str(tmp_path), "goal": "Build landing page"}
    state = AutonomousDeliveryState(session_id="sess-tui-1")

    session, should_exit = dispatch_delivery_command(
        mock_adapter, mock_client, session, ":delivery-status", state
    )
    assert should_exit is False
    assert len(state.messages) >= 2
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":delivery-status"
    assert state.messages[-1].role == "assistant"
    assert "Lead Architect Report" in state.messages[-1].content
    assert "Delivery complete and verified" in state.messages[-1].content

