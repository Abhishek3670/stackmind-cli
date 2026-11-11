"""Unit tests for autonomous uninterrupted orchestration and Laya Decision Specialist.

Verifies:
1. LayaDecisionAdapter.decide_recovery_action produces valid decisions conforming to schemas/recovery-decision.schema.json.
2. Direct manifest nudge when worker contract permits writing dependency manifests.
3. Fallback from failed architecture dependency escalation to ARCHITECT_RECOVERY_DECISION instead of Phase.BLOCKED.
4. Autonomous recovery decision synthesis via Laya when decision turn produces no artifact or fails.
5. Cleanup of stale dependency_escalation_op_id and recovery_decision_operation_id to eliminate instant re-blocking.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jsonschema import Draft7Validator

from validators.routing.laya_adapter import LayaDecisionAdapter, LayaMode
from validators.kernel.daemon.supervisor.models import Phase, RunState, AdvanceResult


@pytest.fixture
def recovery_schema() -> dict:
    schema_path = Path("schemas/recovery-decision.schema.json")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def test_laya_decide_recovery_action_transient_failure(recovery_schema):
    """Test that transient verification and execution errors produce valid retry_unchanged decisions."""
    adapter = LayaDecisionAdapter(mode=LayaMode.DISABLED)
    decision = adapter.decide_recovery_action(
        wo_id="WO-001",
        error_message="verification gate failed: code_verified (Deliverable 'src/backend.py' imports external module(s) ['flask', 'flask_cors'] but no dependency manifest found)",
        failure_code="CODE_NOT_VERIFIED",
    )

    validator = Draft7Validator(recovery_schema)
    errors = list(validator.iter_errors(decision))
    assert not errors, f"Schema validation errors: {errors}"

    assert decision["work_order"] == "WO-001"
    assert decision["action"] == "retry_unchanged"
    assert decision["transient"] is True
    assert "Decision Specialist (Laya)" in decision["reason"]


def test_laya_decide_recovery_action_scope_violation(recovery_schema):
    """Test that scope or file budget violations produce valid amend_contract decisions."""
    adapter = LayaDecisionAdapter(mode=LayaMode.DISABLED)
    decision = adapter.decide_recovery_action(
        wo_id="WO-002",
        error_message="contract file budget exceeded: touched 5 files, budget was 2",
        failure_code="CONTRACT_FILE_BUDGET_EXCEEDED",
        evidence={"contract_revision": 2},
    )

    validator = Draft7Validator(recovery_schema)
    errors = list(validator.iter_errors(decision))
    assert not errors, f"Schema validation errors: {errors}"

    assert decision["work_order"] == "WO-002"
    assert decision["action"] == "amend_contract"
    assert decision["contract_revision"] == 3
    assert "Decision Specialist (Laya)" in decision["reason"]


def test_laya_decide_recovery_action_structural_deadlock(recovery_schema):
    """Test that dependency deadlocks or governance failures produce architect_fix decisions."""
    adapter = LayaDecisionAdapter(mode=LayaMode.DISABLED)
    decision = adapter.decide_recovery_action(
        wo_id="WO-003",
        error_message="dependency deadlock detected between WO-003 and WO-004",
        failure_code="DEPENDENCY_DEADLOCK",
    )

    validator = Draft7Validator(recovery_schema)
    errors = list(validator.iter_errors(decision))
    assert not errors, f"Schema validation errors: {errors}"

    assert decision["work_order"] == "WO-003"
    assert decision["action"] == "architect_fix"
    assert "Decision Specialist (Laya)" in decision["reason"]


def test_direct_manifest_nudge_when_contract_permits(tmp_path):
    """Test that when a deliverable imports modules and contract permits writing manifests, worker is nudged directly without supervisor halt."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor

    mock_start_turn = MagicMock()
    manager_mock = MagicMock()
    manager_mock.start_turn = mock_start_turn
    supervisor = LifecycleSupervisor(manager_mock)
    ws = tmp_path

    state = RunState(
        run_id="run-test",
        session_id="sess-test",
        workspace=str(ws),
        product_goal="Test Goal",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-001"],
        retry_counts={"WO-001": 0},
        max_retries=5,
    )

    err_msg = (
        "verification gate failed: code_verified (Deliverable 'src/backend.py' imports external module(s) "
        "['flask', 'flask_cors'] but no dependency manifest (pyproject.toml, requirements*.txt) was found in project. "
        "Your contract permits writing dependency manifests. Use write_file to create requirements.txt or pyproject.toml declaring these dependencies.)"
    )

    op = {"operation_id": "op-1", "work_order_id": "WO-001"}
    result = {
        "status": "blocked",
        "error": err_msg,
        "reason": err_msg,
    }

    with patch.object(supervisor, "_agent_for_wo", return_value="codex"), \
         patch.object(supervisor, "_role_for_agent", return_value="backend"), \
         patch.object(supervisor, "_get_wo_deliverable_path", return_value="src/backend.py"):
        
        advance_result = supervisor._handle_blocked_wo(state, ws, "WO-001", op, result)

        # None means a retry turn was dispatched and execution continues autonomously
        assert advance_result is None
        assert state.retry_counts["WO-001"] == 1
        assert "op-1" in state.ignored_operation_ids
        mock_start_turn.assert_called_once()
        call_prompt = mock_start_turn.call_args[0][1]
        assert "Retry work order WO-001" in call_prompt
        assert "Your contract explicitly permits writing dependency manifests" in call_prompt
        assert "requirements.txt" in call_prompt


def test_failed_architecture_dependency_escalation_falls_back_to_recovery_decision(tmp_path):
    """Test that a failed/timed-out architecture escalation turn transitions to ARCHITECT_RECOVERY_DECISION rather than Phase.BLOCKED."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor

    manager_mock = MagicMock()
    manager_mock.get_operation.return_value = {
        "operation_id": "esc-op-1",
        "status": "CANCELLED",
        "result": {"status": "cancelled", "reason": "Turn timed out after 300.0s"},
    }

    supervisor = LifecycleSupervisor(manager_mock)
    ws = tmp_path

    state = RunState(
        run_id="run-test",
        session_id="sess-test",
        workspace=str(ws),
        product_goal="Test Goal",
        phase=Phase.EXECUTING,
        dependency_escalation_op_id="esc-op-1",
        dependency_escalation_wo_id="WO-001",
    )

    with patch.object(supervisor, "escalate_wo_to_recovery") as mock_escalate:
        res = supervisor._poll_dependency_escalation(state, ws)

        assert res == AdvanceResult.TRANSITIONED
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        mock_escalate.assert_called_once()
        assert state.dependency_escalation_op_id is None
        assert state.dependency_escalation_wo_id is None


def test_laya_fallback_in_recovery_decision_when_llm_turn_fails(tmp_path, recovery_schema):
    """Test that when an Architect recovery decision turn fails or produces no artifact, Laya synthesizes a decision so orchestration proceeds."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor

    manager_mock = MagicMock()
    manager_mock.get_operation.return_value = {
        "operation_id": "rec-op-1",
        "status": "FAILED",
        "result": {"status": "failed", "summary": "I could not write the JSON file"},
    }

    supervisor = LifecycleSupervisor(manager_mock)
    ws = tmp_path

    # Create dummy contract file so contract hash passes
    contracts_dir = ws / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    contract_file = contracts_dir / "WO-001.yaml"
    contract_file.write_text("schema_version: 1\n", encoding="utf-8")

    state = RunState(
        run_id="run-test",
        session_id="sess-test",
        workspace=str(ws),
        product_goal="Test Goal",
        phase=Phase.ARCHITECT_RECOVERY_DECISION,
        recovery_wo_id="WO-001",
        recovery_decision_operation_id="rec-op-1",
        worker_blockers={
            "WO-001": {
                "failure_code": "CODE_NOT_VERIFIED",
                "canonical_message": "verification gate failed: code_verified missing requirements.txt",
                "contract_hash": supervisor._sha256_file(contract_file),
            }
        },
    )

    with patch.object(supervisor, "_apply_recovery_decision") as mock_apply:
        mock_apply.return_value = (AdvanceResult.WAITING_FOR_OPERATION, None)
        res = supervisor._advance_recovery_decision(state)

        assert res == AdvanceResult.WAITING_FOR_OPERATION
        mock_apply.assert_called_once()
        applied_decision = mock_apply.call_args[0][3]
        assert applied_decision["action"] == "retry_unchanged"
        assert applied_decision["transient"] is True
        assert applied_decision["work_order"] == "WO-001"


def test_cleanup_orphaned_operations_clears_dead_escalation_and_recovery_operations():
    """Test that dead or ignored dependency_escalation_op_id and recovery_decision_operation_id are cleaned up."""
    from validators.kernel.daemon.manager import _reset_phase_operation_id

    state = RunState(
        run_id="run-test",
        session_id="sess-test",
        workspace=".",
        product_goal="Test Goal",
        dependency_escalation_op_id="dead-op-1",
        dependency_escalation_wo_id="WO-001",
        recovery_decision_operation_id="dead-op-2",
        architect_repair_operation_id="dead-op-3",
    )

    # When resuming into EXECUTING, stale escalation/recovery ops must be cleared
    _reset_phase_operation_id(state, Phase.EXECUTING)
    assert state.dependency_escalation_op_id is None
    assert state.dependency_escalation_wo_id is None
    assert state.recovery_decision_operation_id is None
    assert state.architect_repair_operation_id is None
