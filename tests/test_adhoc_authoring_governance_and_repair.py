"""Tests for Ad-Hoc Mode Authoring Governance, In-Turn Validation, and Auto-Repair.

Verifies:
1. Ad-hoc system prompt for Lead Architect contains full governance digest, allowed enums, and templates.
2. AuthoringGate._validate_index enforces index.schema.json validation (rejecting invalid enums).
3. AuthoringGate and schemas accept 'QA', 'TEST', and 'VALIDATION' for work orders and 'test'/'code' for deliverables.
4. ToolGateway write_file normalizes 'work-orders/...' and enforces in-turn schema validation.
5. SessionManager daemon automatically dispatches an Architect repair turn on staged work-order validation failure.
6. ContractGate intrinsically permits Claude to write recovery decisions to .sync/decisions/recovery/.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from validators.harness.authoring_gate import AuthoringGate
from validators.harness.contract_gate import verify_post_execution
from validators.harness.runner import AgentRunner
from validators.knowledge.contract import AgentContract
from validators.kernel.boundary import OperationJournal, RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.daemon.manager import DaemonStorage, SessionManager
from validators.kernel.permission_matrix import get_permission_matrix_prompt
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


def test_adhoc_architect_system_message_contains_governance_spec(tmp_path: Path):
    """Lead Architect adhoc prompt must contain valid enums and minimal YAML template."""
    runner = AgentRunner(tmp_path, agent="claude")
    msg = runner._build_adhoc_system_message()

    assert "WORK ORDER & CONTRACT GOVERNANCE SPECIFICATION" in msg
    assert "VALIDATION" in msg
    assert "deliverable:" in msg
    assert "code" in msg
    assert "id: WO-002" in msg


def test_permission_matrix_contains_mandatory_schema_constraints():
    """Permission matrix prompt for architect must reinforce schema constraints."""
    perm_msg = get_permission_matrix_prompt("claude")
    assert "MANDATORY SCHEMA CONSTRAINTS FOR WORK ORDERS & INDEX.yaml" in perm_msg
    assert "VALIDATION" in perm_msg
    assert "deliverable.type" in perm_msg


def test_authoring_gate_rejects_invalid_index_enum(tmp_path: Path):
    """AuthoringGate._validate_index must reject unparseable/unknown type enum."""
    gate = AuthoringGate(project_root=tmp_path)
    invalid_index_data = {
        "schema_version": 1,
        "next_id": 3,
        "orders": [
            {
                "id": "WO-001",
                "type": "FEATURE",
                "title": "Build calculator",
                "status": "COMPLETED",
            },
            {
                "id": "WO-002",
                "type": "NONEXISTENT_TYPE",  # Invalid enum!
                "title": "Verify calculator",
                "status": "ACTIVE",
            },
        ],
    }
    errors = []
    gate._validate_index(".sync/work-orders/INDEX.yaml", invalid_index_data, errors)
    assert any("schema error" in e.lower() and "'NONEXISTENT_TYPE' is not one of" in e for e in errors)


def test_authoring_gate_rejects_qa_index_enum(tmp_path: Path):
    """AuthoringGate._validate_index must pass for VALIDATION and FEATURE, but reject QA."""
    gate = AuthoringGate(project_root=tmp_path)
    valid_index_data = {
        "schema_version": 1,
        "next_id": 3,
        "orders": [
            {
                "id": "WO-001",
                "type": "FEATURE",
                "title": "Build calculator",
                "status": "COMPLETED",
            },
            {
                "id": "WO-002",
                "type": "VALIDATION",
                "title": "Verify calculator",
                "status": "ACTIVE",
            },
        ],
    }
    errors = []
    gate._validate_index(".sync/work-orders/INDEX.yaml", valid_index_data, errors)
    assert not errors

    qa_index_data = {
        "schema_version": 1,
        "next_id": 2,
        "orders": [
            {
                "id": "WO-001",
                "type": "QA",
                "title": "QA calculator",
                "status": "ACTIVE",
            },
        ],
    }
    qa_errors = []
    gate._validate_index(".sync/work-orders/INDEX.yaml", qa_index_data, qa_errors)
    assert any("schema error" in e.lower() and "'QA' is not one of" in e for e in qa_errors)


def test_tools_write_file_normalizes_and_validates_relative_work_orders(tmp_path: Path):
    """write_file with relative 'work-orders/ACTIVE/WO-002.yaml' normalizes and validates schema."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-wo-norm")
    contract = KernelContract(
        agent_id="claude",
        work_order="WO-000",
        allow=("workspace/.sync/**", "workspace/work-orders/**"),
        deny=(),
        write_mode="read-write",
    ).freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file", "read_file"))
    boundary = RuntimeBoundary(OperationJournal())

    tools = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="s1",
        attempt_id="a1",
        actor_id="claude",
        provider_id="mock",
    )

    invalid_wo_yaml = yaml.safe_dump({
        "id": "WO-002",
        "type": "UNKNOWN_GARBAGE_TYPE",  # Invalid!
        "title": "Verify calculator",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": ["WO-001"],
        "deliverable": {
            "type": "unsupported_deliv_type",  # Invalid!
            "description": "Test calculator",
        },
    })

    with pytest.raises(PermissionError) as exc_info:
        tools.write_file("work-orders/ACTIVE/WO-002.yaml", invalid_wo_yaml)

    err_str = str(exc_info.value)
    assert "Authoring validation failed" in err_str
    assert "'UNKNOWN_GARBAGE_TYPE' is not one of" in err_str or "invalid" in err_str.lower()


def test_tools_write_file_accepts_qa_and_test_deliverable(tmp_path: Path):
    """write_file accepts QA work order with deliverable.type 'test'."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-wo-qa")
    contract = KernelContract(
        agent_id="claude",
        work_order="WO-000",
        allow=("workspace/.sync/**", "workspace/work-orders/**"),
        deny=(),
        write_mode="read-write",
    ).freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file", "read_file"))
    boundary = RuntimeBoundary(OperationJournal())

    tools = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="s1",
        attempt_id="a1",
        actor_id="claude",
        provider_id="mock",
    )

    qa_wo_yaml = yaml.safe_dump({
        "id": "WO-002",
        "type": "QA",
        "title": "Verify calculator",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": ["WO-001"],
        "deliverable": {
            "type": "test",
            "description": "Verify calculator operations",
        },
    })

    with pytest.raises(PermissionError) as exc_info:
        tools.write_file("work-orders/ACTIVE/WO-002.yaml", qa_wo_yaml)
    assert "Authoring validation failed" in str(exc_info.value)

    # Valid VALIDATION work order with deliverable.type 'code' must succeed:
    valid_wo_yaml = yaml.safe_dump({
        "id": "WO-002",
        "type": "VALIDATION",
        "title": "Verify calculator",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": ["WO-001"],
        "deliverable": {
            "type": "code",
            "path": "tests/test_calculator.py",
            "description": "Verify calculator operations",
        },
    })
    tools.write_file("work-orders/ACTIVE/WO-002.yaml", valid_wo_yaml)
    target_path = ws.path_for(".sync/work-orders/ACTIVE/WO-002.yaml")
    assert target_path.exists()
    content = target_path.read_text(encoding="utf-8")
    assert "type: VALIDATION" in content
    assert "type: code" in content


def test_contract_gate_intrinsically_allows_recovery_decision(tmp_path: Path):
    """Claude writing a recovery decision to .sync/decisions/recovery/ is intrinsically permitted."""
    contract_data = {
        "schema_version": 1,
        "agent_id": "claude",
        "work_order": "WO-003",
        "scope": {
            "allow": [
                {"module": "PLAN.md"},
                {"module": "README.md"},
            ],
            "deny": [
                {"module": ".git/**"},
            ],
            "write": "read-write",
        },
        "budget": {
            "max_files_touched": 10,
            "max_tokens": 1000,
        },
    }
    contract_file = tmp_path / "test.contract.yaml"
    contract_file.write_text(yaml.safe_dump(contract_data), encoding="utf-8")
    contract = AgentContract.load(contract_file, tmp_path)

    decision_obj = MagicMock()
    decision_obj.commands = []
    task_obj = MagicMock()
    task_obj.work_order_id = "WO-003"

    written_files = [".sync/decisions/recovery/WO-003.decision.json"]

    # Should not raise ContractAccessDenied
    verify_post_execution(
        project_path=tmp_path,
        agent="claude",
        task=task_obj,
        decision=decision_obj,
        observed_files=[".sync/decisions/recovery/WO-003.decision.json"],
        contract=contract,
    )


def test_manager_auto_repairs_staged_work_order_validation_failure(tmp_path: Path):
    """Manager auto-dispatches an architect repair turn when work-order validation fails."""
    storage = DaemonStorage(tmp_path / "daemon.json")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "mock", {}, str(tmp_path), session_id="test-session")
    session_id = session["session_id"]

    mock_runner = MagicMock()
    mock_result = MagicMock()
    mock_result.status = "blocked"
    mock_result.reason = (
        "staged stackmind validate failed: work-orders/INDEX.yaml: orders.1.type - 'INVALID' is not one of [...]; "
        "work-orders/ACTIVE/WO-002.yaml: type - 'INVALID' is not one of [...]; deliverable.type - 'bad' is not one of [...]"
    )
    mock_result.meta = {}
    mock_result.payload = {}
    mock_runner.run_once.return_value = mock_result

    with patch.object(manager, "_runner_factory", return_value=mock_runner):
        with patch.object(manager, "start_turn") as mock_start_turn:
            cancel_event = MagicMock()
            cancel_event.is_set.return_value = False
            _, op_id = manager.begin_operation(session_id, "turn", {"prompt": "verify calculator"})

            manager._run_turn(session_id, op_id, cancel_event, "verify calculator")

            mock_start_turn.assert_called_once()
            call_kwargs = mock_start_turn.call_args[1]
            assert call_kwargs.get("role") == "architecture"
            assert call_kwargs.get("agent_id") == "claude"
            assert call_kwargs.get("is_authoring") is True
            assert call_kwargs.get("wo_repair_attempt") == 1
            repair_prompt = mock_start_turn.call_args[0][1]
            assert "CRITICAL ARCHITECTURE INSTRUCTION" in repair_prompt
            assert "staged stackmind validate failed" in repair_prompt
            assert "Allowed 'type' enums for Work Orders and INDEX.yaml" in repair_prompt
            assert "VALIDATION" in repair_prompt


def test_manager_respects_wo_repair_retry_limit(tmp_path: Path):
    """Manager stops auto-repairing work orders when retry limit (2) is reached."""
    storage = DaemonStorage(tmp_path / "daemon.json")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "mock", {}, str(tmp_path), session_id="test-session-limit")
    session_id = session["session_id"]

    mock_runner = MagicMock()
    mock_result = MagicMock()
    mock_result.status = "blocked"
    mock_result.reason = "staged stackmind validate failed: work-orders/INDEX.yaml: orders.1.type - 'INVALID' is not one of [...]"
    mock_result.meta = {}
    mock_result.payload = {}
    mock_runner.run_once.return_value = mock_result

    with patch.object(manager, "_runner_factory", return_value=mock_runner):
        with patch.object(manager, "start_turn") as mock_start_turn:
            cancel_event = MagicMock()
            cancel_event.is_set.return_value = False
            _, op_id = manager.begin_operation(
                session_id, "turn", {"prompt": "repair wo", "wo_repair_attempt": 2}
            )

            manager._run_turn(session_id, op_id, cancel_event, "repair wo")

            mock_start_turn.assert_not_called()
            op = manager.get_operation(op_id)
            assert op["status"] == "BLOCKED"
