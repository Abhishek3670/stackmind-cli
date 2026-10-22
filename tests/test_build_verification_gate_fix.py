"""Regression tests verifying build verification gate fix:
1. IntegrationGate skips mandatory build execution when node_modules is absent in package.json projects.
2. Supervisor waives unfulfillable build execution blockers when dependencies are not installed in workspace.
3. Child contracts grant workspace/command to QA agents and build/install tasks.
4. ContractEvaluator authorizes workspace/command correctly.
"""

from pathlib import Path
import json
import yaml
from typing import Any

from validators.harness.integration_gate import IntegrationGate
from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState, Phase, AdvanceResult
from validators.kernel.daemon.authoring import build_child_contract
from validators.kernel.contract import ContractEvaluator, ContractNormalizer


class MockSessionManager:
    """Mock SessionManager implementing the supervisor's turn dispatch boundary."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._operations: list[dict[str, Any]] = []

    def start_turn(
        self,
        session_id: str,
        prompt: str,
        role: str | None = None,
        agent_id: str | None = None,
        work_order_id: str | None = None,
        parent_operation_id: str | None = None,
    ) -> dict[str, Any]:
        op = {
            "operation_id": f"op-{len(self._operations) + 1}",
            "session_id": session_id,
            "prompt": prompt,
            "role": role,
            "agent_id": agent_id,
            "work_order_id": work_order_id,
            "parent_operation_id": parent_operation_id,
            "status": "RUNNING",
            "result": None,
        }
        self._operations.append(op)
        return op

    def complete_operation(self, operation_id: str, status: str, result: dict[str, Any] | None = None) -> None:
        for op in self._operations:
            if op["operation_id"] == operation_id:
                op["status"] = status
                op["result"] = result
                break

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        for op in self._operations:
            if op["operation_id"] == operation_id:
                return dict(op)
        return None

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._operations)


def _setup_clean_workspace(tmp_path: Path) -> None:
    (tmp_path / "PLAN.md").write_text("# Plan\nDeliverables verified.\n", encoding="utf-8")
    sync = tmp_path / ".sync"
    (sync / "contracts").mkdir(parents=True, exist_ok=True)
    (sync / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (sync / "work-orders" / "COMPLETED").mkdir(parents=True, exist_ok=True)
    (sync / "work-orders" / "BLOCKED").mkdir(parents=True, exist_ok=True)
    (sync / "inbox" / "claude").mkdir(parents=True, exist_ok=True)
    (sync / "inbox" / "gemma").mkdir(parents=True, exist_ok=True)
    (sync / "inbox" / "codex").mkdir(parents=True, exist_ok=True)


def test_integration_gate_skips_build_when_node_modules_absent(tmp_path: Path) -> None:
    """When package.json defines a build script but node_modules is absent, build_execution check is skipped."""
    _setup_clean_workspace(tmp_path)
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(json.dumps({
        "name": "static-landing",
        "scripts": {"build": "vite build"}
    }), encoding="utf-8")

    gate = IntegrationGate(tmp_path)
    decision = gate.evaluate()

    # The gate must pass deterministically because dependencies are not installed
    assert decision.passed is True
    checks_by_name = {c.name: c for c in decision.checks}
    assert "build_execution" in checks_by_name
    assert checks_by_name["build_execution"].passed is True
    assert checks_by_name["build_execution"].status == "skipped"
    assert "node_modules absent" in checks_by_name["build_execution"].message


def test_supervisor_waives_unfulfillable_build_blocker(tmp_path: Path) -> None:
    """When an LLM reviewer blocks solely due to missing build record on an uninstalled project,
    the supervisor waives the unfulfillable blocker and transitions to PRODUCT_READY."""
    _setup_clean_workspace(tmp_path)
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(json.dumps({
        "name": "static-landing",
        "scripts": {"build": "vite build"}
    }), encoding="utf-8")

    # Component deliverable
    src_file = tmp_path / "src" / "App.jsx"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("export default function App() { return <div>Rustic</div>; }\n", encoding="utf-8")

    # Completed work order
    wo_file = tmp_path / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml"
    wo_file.write_text(yaml.safe_dump({
        "id": "WO-001",
        "title": "UI Components",
        "assigned_agents": ["codex"],
        "status": "COMPLETED",
        "deliverable": {"path": "src/App.jsx", "type": "code"},
    }), encoding="utf-8")

    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    review_op = mock_mgr.start_turn(
        "sess-test",
        "Perform integration review",
        role="architecture",
        agent_id="claude",
        work_order_id="WO-002",
    )
    mock_mgr.complete_operation(
        review_op["operation_id"],
        "BLOCKED",
        result={
            "status": "blocked",
            "summary": "Build verification failed: No record of build execution found.",
            "report_markdown": "Deliverables are verified, but build execution was not recorded.",
            "blockers": ["Build execution not verified."],
            "blocker_details": [
                {
                    "finding": "package.json defines 'build': 'vite build', but no record of build execution exists",
                    "remediation": "Execute npm run build",
                    "path": "package.json",
                }
            ],
        },
    )

    state = RunState(
        run_id="run-test",
        product_goal="Build static crochet landing page",
        workspace=str(tmp_path),
        session_id="sess-test",
        phase=Phase.INTEGRATION_REVIEW,
        completed_wo_ids=["WO-001"],
        worker_wo_ids=["WO-001"],
        integration_operation_id=review_op["operation_id"],
        integration_wo_id="WO-002",
    )

    # Advance supervisor
    res = supervisor.advance(state)

    # Because all components are valid and node_modules is absent, the supervisor waives
    # the unfulfillable build blocker and transitions to PRODUCT_READY!
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.PRODUCT_READY
    assert state.integration_blockers == []


def test_build_child_contract_grants_command_permission() -> None:
    """Child contracts for QA and install/build tasks automatically grant workspace/command."""
    # 1. QA contract
    qa_contract = build_child_contract(
        wo_id="WO-005",
        agent_id="gemma",
        role="qa",
        deliverable_path="tests/test_landing_page.py",
        skills=["diagnosing-bugs"],
        title="QA & Performance Validation",
        description="Run test suite and verify performance",
    )
    qa_allows = [r["module"] for r in qa_contract["scope"]["allow"]]
    assert "workspace/command" in qa_allows

    # 2. Build task contract
    build_contract = build_child_contract(
        wo_id="WO-011",
        agent_id="codex",
        role="backend",
        deliverable_path="dist/index.html",
        title="Application Build Execution",
        description="Execute npm run build",
    )
    build_allows = [r["module"] for r in build_contract["scope"]["allow"]]
    assert "workspace/command" in build_allows

    # 3. Verify ContractEvaluator authorizes run_command on workspace/command
    evaluator = ContractEvaluator()
    norm_qa = ContractNormalizer.normalize(qa_contract)
    authorized, reason = evaluator.authorize(norm_qa, "run_command", "workspace/command")
    assert authorized is True
    assert reason == "authorized"
