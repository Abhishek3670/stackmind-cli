"""Tests for Lead Architect Team Status Awareness and Direct Blocker Resolution fixes."""

import json
from pathlib import Path
import pytest
import yaml

from validators.harness.task_contract import render_team_status, render_work_order_board
from validators.kernel.identity import get_role_policy
from validators.kernel.operations import OperationType
from validators.kernel.providers.gateway import get_curated_tools_for_phase
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from tests.test_lifecycle_supervisor import MockSessionManager


def setup_workspace_with_wos(tmp_path: Path):
    """Set up a workspace with .sync directory, work orders, and supervisor state."""
    sync_dir = tmp_path / ".sync"
    wo_dir = sync_dir / "work-orders"
    (wo_dir / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (wo_dir / "BLOCKED").mkdir(parents=True, exist_ok=True)
    (wo_dir / "COMPLETED").mkdir(parents=True, exist_ok=True)
    (sync_dir / "contracts").mkdir(parents=True, exist_ok=True)
    (sync_dir / "inbox" / "claude").mkdir(parents=True, exist_ok=True)
    (sync_dir / "decisions" / "recovery").mkdir(parents=True, exist_ok=True)

    # 1. Completed WO-001 (Frontend)
    wo1 = {
        "id": "WO-001",
        "title": "Foundation structure",
        "status": "COMPLETED",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": "index.html"},
    }
    (wo_dir / "COMPLETED" / "WO-001.yaml").write_text(yaml.safe_dump(wo1), encoding="utf-8")

    # 2. Blocked WO-004 (QA)
    wo4 = {
        "id": "WO-004",
        "title": "QA verification tests",
        "status": "BLOCKED",
        "assigned_agents": ["gemma"],
        "dependencies": ["WO-001"],
        "deliverable": {"type": "code", "path": "tests/test_qa.py"},
        "error": "AssertionError: Expected conversion button with ID 'cta-submit' not found in index.html",
    }
    (wo_dir / "BLOCKED" / "WO-004.yaml").write_text(yaml.safe_dump(wo4), encoding="utf-8")

    # Contract for WO-004
    c4 = {
        "schema_version": 1,
        "agent_id": "gemma",
        "work_order": "WO-004",
        "identity": {"role": "qa", "reports_to": "ceo"},
        "scope": {"allow": [{"module": "tests/**"}], "deny": [{"module": ".git/**"}], "write": "read-write"},
        "budget": {"max_files_touched": 5, "max_tokens": 20000},
    }
    (sync_dir / "contracts" / "WO-004.yaml").write_text(yaml.safe_dump(c4), encoding="utf-8")

    # QA verdict report in inbox
    qa_verdict = (
        "# QA Verdict: REJECTED\n"
        "Deliverable index.html is missing conversion button #cta-submit.\n"
    )
    (sync_dir / "inbox" / "claude" / "WO-004-qa-verdict.md").write_text(qa_verdict, encoding="utf-8")

    return sync_dir


def test_render_team_status_shows_agents_and_blockers(tmp_path: Path):
    """Verify render_team_status provides complete visibility into agents, WOs, and blockers."""
    setup_workspace_with_wos(tmp_path)

    status_text = render_team_status(tmp_path)

    # 1. Specialist agent team status
    assert "SPECIALIST AGENT TEAM STATUS:" in status_text
    assert "Architecture (claude):" in status_text
    assert "Frontend (gemini): COMPLETED all assigned tasks (WO-001)" in status_text
    assert "QA (gemma): BLOCKED on WO-004:" in status_text
    assert "cta-submit" in status_text

    # 2. Active blockers
    assert "ACTIVE BLOCKERS & DEFECT FINDINGS:" in status_text
    assert "[WO-004] (gemma):" in status_text
    assert "AssertionError" in status_text

    # 3. Work order board including completed and blocked
    assert "WORK ORDERS LIFECYCLE BOARD:" in status_text
    assert "WO-001 [COMPLETED]" in status_text
    assert "WO-004 [BLOCKED]" in status_text

    # 4. Lead Architect directive
    assert "LEAD ARCHITECT DIRECTIVE:" in status_text
    assert "AUTHOR RESOLUTION FIXES using write_file or apply_patch" in status_text


def test_architect_identity_and_gateway_tools():
    """Verify Lead Architect policy and tools include direct patch and test verification capabilities."""
    policy = get_role_policy("claude")
    assert policy.permits(OperationType.WRITE_FILE.value)
    assert policy.permits(OperationType.APPLY_PATCH.value)
    assert policy.permits(OperationType.RUN_TESTS.value)
    assert policy.permits(OperationType.VERIFY_DELIVERABLE.value)

    tools = get_curated_tools_for_phase("claude")
    tool_names = {t.name for t in tools}
    assert "write_file" in tool_names
    assert "apply_patch" in tool_names
    assert "run_tests" in tool_names
    assert "verify_deliverable" in tool_names


def test_supervisor_architect_fix_recovery_lifecycle(tmp_path: Path):
    """Verify end-to-end recovery where Lead Architect directly fixes a blocked deliverable."""
    setup_workspace_with_wos(tmp_path)

    mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mgr)
    state = supervisor.start_run("run-test-01", "Build test app", tmp_path, "sess-01")
    state.phase = Phase.ARCHITECT_RECOVERY_DECISION
    state.worker_wo_ids = ["WO-004"]
    state.recovery_wo_id = "WO-004"
    state.worker_blockers = {
        "WO-004": {
            "failure_code": "TEST_EXECUTION_FAILED",
            "canonical_message": "Missing #cta-submit in index.html",
            "deliverable_path": "tests/test_qa.py",
        }
    }

    # 1. Write architect_fix decision
    dec_file = tmp_path / ".sync" / "decisions" / "recovery" / "WO-004.decision.json"
    dec_file.write_text(json.dumps({
        "work_order": "WO-004",
        "action": "architect_fix",
        "reason": "Lead Architect directly fixing the deliverable and updating test requirements",
    }), encoding="utf-8")

    # Advance recovery decision -> routes to ARCHITECT_REPAIR
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.ARCHITECT_REPAIR
    assert state.repair_context.get("action") == "architect_fix"
    assert state.architect_repair_operation_id is not None

    # Check repair prompt generated for Architect
    repair_op = mgr.get_operation(state.architect_repair_operation_id)
    assert repair_op["agent_id"] == "claude"
    assert "architect_fix: You are Lead Architect directly resolving the blocker on WO-004" in repair_op["prompt"]

    # 2. Simulate Architect fixing the deliverable on disk
    deliv_file = tmp_path / "tests" / "test_qa.py"
    deliv_file.parent.mkdir(parents=True, exist_ok=True)
    deliv_file.write_text("def test_fixed(): assert True\n", encoding="utf-8")

    # Complete the Architect repair operation
    mgr.complete_operation(state.architect_repair_operation_id, "COMPLETED", result={"status": "completed"})

    # Advance repair turn -> outcome verified, WO-004 marked completed, transitions to EXECUTING!
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert "WO-004" in state.completed_wo_ids
    assert "WO-004" not in state.blocked_wo_ids
    assert "WO-004" not in state.worker_blockers


def test_qa_defect_escalates_to_architect_after_worker_rework(tmp_path: Path):
    """Verify that repeated QA defects escalate directly to Lead Architect recovery decision."""
    setup_workspace_with_wos(tmp_path)

    mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mgr)
    state = supervisor.start_run("run-test-02", "Build test app", tmp_path, "sess-02")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001", "WO-004"]
    state.completed_wo_ids = ["WO-001"]
    # Worker already attempted rework once
    state.qa_reworks = {"WO-001": 1}

    # QA turn blocks reporting a defect against WO-001
    qa_op = mgr.start_turn("sess-02", "Execute QA", work_order_id="WO-004", agent_id="gemma")
    mgr.complete_operation(
        qa_op["operation_id"],
        "BLOCKED",
        result={
            "status": "blocked",
            "blockers": ["QA found defect in index.html"],
            "failure": {
                "failure_code": "TEST_EXECUTION_FAILED",
                "canonical_message": "index.html missing header",
            },
        },
    )

    # Advance executing -> detects second defect on WO-001, escalates to ARCHITECT_RECOVERY_DECISION
    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.recovery_wo_id == "WO-001"
