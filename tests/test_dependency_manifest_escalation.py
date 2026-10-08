"""Tests for missing dependency manifest and external module escalation & recovery."""

from pathlib import Path
from typing import Any
import yaml

from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from tests.test_lifecycle_supervisor import MockSessionManager


def test_missing_manifest_external_modules_escalates_to_architecture(tmp_path: Path) -> None:
    """When a worker blocks because deliverable imports external modules without a manifest,
    supervisor detects the undeclared module, extracts the deliverable, and escalates to Claude (Architecture)."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    (wo_dir / "WO-009.yaml").write_text(yaml.safe_dump({
        "id": "WO-009",
        "title": "Backend API",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"path": "src/backend.py", "type": "code"},
    }), encoding="utf-8")

    state = RunState(
        run_id="run-manifest-1",
        product_goal="Backend Goal",
        workspace=str(tmp_path),
        session_id="sess-manifest-1",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-009"],
    )

    # 1. Worker turn fails with missing manifest and external module ['flask']
    worker_op = mock_mgr.start_turn("sess-manifest-1", "Execute WO-009", work_order_id="WO-009", role="backend", agent_id="codex")
    mock_mgr.complete_operation(
        worker_op["operation_id"],
        status="COMPLETED",
        result={
            "status": "blocked",
            "error": (
                "verification gate failed: code_verified (Deliverable 'src/backend.py' "
                "imports external module(s) ['flask'] but no dependency manifest "
                "(pyproject.toml, requirements*.txt) was found in project. Your contract permits "
                "writing dependency manifests. Use write_file to create requirements.txt "
                "or pyproject.toml declaring these dependencies.)"
            ),
        },
    )

    # 2. Advance: Supervisor detects missing manifest error and escalates to Claude (Architecture)
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING  # Must NOT hard-block to BLOCKED!
    assert state.dependency_escalation_op_id is not None
    assert state.dependency_escalation_wo_id == "WO-009"
    assert worker_op["operation_id"] in state.ignored_operation_ids

    # Verify escalation operation was dispatched to Claude with 'flask' and 'src/backend.py'
    esc_op = mock_mgr.get_operation(state.dependency_escalation_op_id)
    assert esc_op["agent_id"] == "claude"
    assert esc_op["role"] == "architecture"
    assert "flask" in esc_op["prompt"]
    assert "src/backend.py" in esc_op["prompt"]

    # 3. Claude resolves dependency by writing requirements.txt and completing turn
    (tmp_path / "requirements.txt").write_text("flask>=3.0.0\n", encoding="utf-8")
    mock_mgr.complete_operation(
        state.dependency_escalation_op_id,
        status="COMPLETED",
        result={"status": "completed", "modified_files": ["requirements.txt"]},
    )

    # 4. Next advance resolves the escalation and re-dispatches the worker
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.dependency_escalation_op_id is None
    assert state.dependency_escalation_wo_id is None
    assert "WO-009" not in state.blocked_wo_ids

    # Worker operation is re-dispatched
    new_worker_op = supervisor._find_latest_wo_operation(state, "WO-009")
    assert new_worker_op is not None
    assert new_worker_op["agent_id"] == "codex"


def test_multiple_external_modules_missing_manifest_escalates(tmp_path: Path) -> None:
    """Detects multiple external modules in brackets (e.g. ['flask', 'requests'])."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "title": "API",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"path": "src/api.py", "type": "code"},
    }), encoding="utf-8")

    state = RunState(
        run_id="run-multi-dep",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-multi",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-001"],
    )

    worker_op = mock_mgr.start_turn("sess-multi", "Execute WO-001", work_order_id="WO-001", role="backend", agent_id="codex")
    mock_mgr.complete_operation(
        worker_op["operation_id"],
        status="COMPLETED",
        result={
            "status": "blocked",
            "error": "verification gate failed: code_verified (Deliverable 'src/api.py' imports external module(s) ['flask', 'requests'] but no dependency manifest was found in project.)",
        },
    )

    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.dependency_escalation_op_id is not None
    esc_op = mock_mgr.get_operation(state.dependency_escalation_op_id)
    assert "flask" in esc_op["prompt"]
    assert "requests" in esc_op["prompt"]


def test_general_code_verified_failure_retries_worker(tmp_path: Path) -> None:
    """When a worker turn fails code_verified due to syntax/runtime error, it triggers a bounded worker retry."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-003.yaml").write_text(yaml.safe_dump({
        "id": "WO-003",
        "title": "Module",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"path": "src/mod.py", "type": "code"},
    }), encoding="utf-8")

    state = RunState(
        run_id="run-syntax-retry",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-syntax",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-003"],
        max_retries=2,
    )

    worker_op = mock_mgr.start_turn("sess-syntax", "Execute WO-003", work_order_id="WO-003", role="backend", agent_id="codex")
    mock_mgr.complete_operation(
        worker_op["operation_id"],
        status="COMPLETED",
        result={
            "status": "blocked",
            "error": "verification gate failed: code_verified (SyntaxError: invalid syntax in src/mod.py line 42)",
        },
    )

    # First retry attempt
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.retry_counts.get("WO-003") == 1

    retry_op = supervisor._find_latest_wo_operation(state, "WO-003")
    assert retry_op is not None
    assert "Retry work order WO-003" in retry_op["prompt"]
    assert "SyntaxError: invalid syntax" in retry_op["prompt"]


def test_unblock_in_flight_work_orders_clears_blocked_state(tmp_path: Path) -> None:
    """Verifies that unblock_in_flight_work_orders clears memory state.blocked_wo_ids."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-009.yaml").write_text(yaml.safe_dump({
        "id": "WO-009",
        "title": "Backend",
        "status": "BLOCKED",
    }), encoding="utf-8")

    state = RunState(
        run_id="run-unblock",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-unblock",
        phase=Phase.BLOCKED,
        worker_wo_ids=["WO-009"],
        blocked_wo_ids=["WO-009"],
    )

    supervisor.unblock_in_flight_work_orders(state)
    assert "WO-009" not in state.blocked_wo_ids
    disk_data = yaml.safe_load((wo_dir / "WO-009.yaml").read_text(encoding="utf-8"))
    assert disk_data["status"] == "ACTIVE"
