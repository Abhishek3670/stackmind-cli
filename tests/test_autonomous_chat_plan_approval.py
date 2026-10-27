from pathlib import Path
import time
import pytest
import yaml

from cli.init import init
from cli.tui.state import AutonomousDeliveryState, ProjectPhase
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.harness.runner import AgentRunner
from validators.kernel.providers.models import ProviderResponse, Message, TokenUsage


class FakeAuthoringAdapter:
    provider_name = "test"
    model_name = "test-authoring-model"

    def complete(self, messages, **kwargs):
        return ProviderResponse(
            message=Message.assistant(
                '{"status": "completed", "summary": "Authored implementation work orders", "modified_files": []}'
            ),
            usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        )


def test_tui_state_does_not_falsely_mark_backend_running_on_plan_approved():
    """Verify TUI state sets worker roles to WAITING, not RUNNING, upon plan.approved."""
    state = AutonomousDeliveryState("test-project")
    
    # Process plan.approved event with a created work order for Backend
    payload = {
        "plan_id": "PLAN-001",
        "reason": "Approved by operator",
        "created_records": [
            {
                "id": "WO-001",
                "title": "Core Backend Implementation",
                "role": "Backend",
                "assigned_agents": ["codex"],
                "status": "ACTIVE",
            }
        ],
    }
    state.process_event({"name": "plan.approved", "payload": payload})
    
    # Backend must NOT be RUNNING/implementing before an operation actually starts
    assert state.roles["Backend"].state == "WAITING"
    assert state.roles["Frontend"].state == "WAITING"
    assert state.roles["Q/A"].state == "WAITING"
    assert state.roles["GitOps"].state == "WAITING"


def test_authoring_runner_without_preexisting_wo000_does_not_abort(tmp_path: Path):
    """Verify that an authoring turn without a pre-existing WO-000.yaml on disk
    executes cleanly and does NOT abort with 'no inbox items or assigned work orders'.
    """
    init(tmp_path, name="Test Chat Session", no_git=True)
    
    runner = AgentRunner(tmp_path, "claude", provider_adapter=FakeAuthoringAdapter())
    
    # Run an authoring turn with work_order_id="WO-000" where WO-000.yaml does NOT exist on disk
    assert not (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-000.yaml").exists()
    
    result = runner.run_once(
        prompt="Author the implementation Work Orders and Contracts for PLAN.md",
        work_order_id="WO-000",
        is_authoring=True,
    )
    
    # Must succeed, not return idle with 'no inbox items or assigned work orders'
    assert result.status == "completed", f"Runner failed with: {result.reason}"
    assert result.reason != "no inbox items or assigned work orders"


def test_approve_plan_chat_flow_spins_up_autonomous_execution(tmp_path: Path):
    """Verify that approving a plan from an adhoc/chat turn synthesizes work orders,
    safely executes the authoring turn without WO-000 missing error, and attaches
    a supervisor run to drive autonomous execution.
    """
    init(tmp_path, name="Autonomous Chat Test", no_git=True)
    
    # Write a plan with milestones
    plan_content = """# Project Plan: Autonomous Chat Test

## Architecture Overview
Test architecture overview.

## Milestones & Roadmap
- [ ] Milestone 1: Core Implementation
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    
    storage = DaemonStorage(tmp_path / ".sync" / "runtime" / "daemon")
    manager = SessionManager(
        storage=storage,
        runner_factory=lambda ws, agent: AgentRunner(
            Path(ws), agent, provider_adapter=FakeAuthoringAdapter()
        ),
    )
    
    # Start session
    session = manager.create_session(
        workspace=str(tmp_path),
        agent="claude",
        provider="custom",
        contract={"allow": [], "deny": [], "write_mode": "governed"},
    )
    sid = session["session_id"]
    
    # Propose a plan (as happens after an adhoc chat turn that writes PLAN.md)
    manager.propose_plan(
        session_id=sid,
        plan_id="PLAN-001",
        title="Project Plan: Autonomous Chat Test",
        content=plan_content,
        metadata={"is_goal": True},
    )
    
    # Approve plan
    created_wos = manager.approve_plan(sid, "PLAN-001", reason="Approved by operator")
    assert len(created_wos) >= 1
    assert created_wos[0]["id"] == "WO-001"
    
    # Verify WO-001 exists on disk
    assert (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").is_file()
    
    # Verify supervisor run was created to autonomously drive the plan
    active_runs = [r for r in manager._active_runs.values() if r.session_id == sid]
    assert len(active_runs) == 1, "A supervisor run must be registered for the approved session"
    
    # Wait briefly for Turn 2 authoring thread to finish
    authoring_op_id = active_runs[0].authoring_operation_id
    if authoring_op_id:
        for _ in range(50):
            op = manager.get_operation(authoring_op_id)
            if op.get("status") in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.05)
        assert op.get("status") == "COMPLETED"
        assert op.get("result", {}).get("error") != "no inbox items or assigned work orders"
