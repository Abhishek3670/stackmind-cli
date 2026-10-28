"""Tests for Pre-Flight DAG Linting, Idle Stall Architect Recovery, and Remediation Directives."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest
import yaml

from tests.test_lifecycle_supervisor import MockSessionManager
from validators.harness.authoring_gate import AuthoringGate
from validators.harness.authoring_readiness import validate_authoring_readiness
from validators.harness.task_contract import render_team_status
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _make_wo(wo_id: str, title: str, agent: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": wo_id,
        "title": title,
        "assigned_agents": [agent],
        "dependencies": deps or [],
        "deliverable": {
            "type": "code",
            "path": f"src/{wo_id.lower()}.py",
            "description": f"Deliverable for {wo_id}",
        },
        "test_plan": [{"tests": [f"tests/test_{wo_id.lower()}.py"]}],
    }


def _make_contract(wo_id: str, agent: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "work_order": wo_id,
        "agent_id": agent,
        "identity": {"role": "worker", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": "src/**"}, {"module": "tests/**"}],
            "deny": [],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 5, "max_tokens": 50000},
    }


class TestAuthoringGateDAGLinter:
    """Test Pre-Flight DAG Linting in AuthoringGate and validate_authoring_readiness."""

    def test_authoring_gate_rejects_self_dependency(self, tmp_path: Path):
        gate = AuthoringGate(project_root=tmp_path)
        wo_data = _make_wo("WO-001", "Core Logic", "codex", deps=["WO-001"])
        content = yaml.safe_dump(wo_data)
        dec = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", content, agent="claude")
        assert not dec.passed
        assert any("cannot depend on itself" in err for err in dec.errors)

    def test_authoring_gate_rejects_inverted_gitops_dependency(self, tmp_path: Path):
        # Create an existing GitOps work order on disk
        gitops_data = _make_wo("WO-002", "Release & GitOps", "local-llm")
        _write_yaml(tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml", gitops_data)

        gate = AuthoringGate(project_root=tmp_path)
        # Worker WO-001 depends on GitOps WO-002
        worker_data = _make_wo("WO-001", "Frontend UI", "gemini", deps=["WO-002"])
        content = yaml.safe_dump(worker_data)
        dec = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", content, agent="claude")
        assert not dec.passed
        assert any("inverted GitOps dependency" in err for err in dec.errors)

    def test_authoring_gate_index_linting_detects_cycle_and_inverted_gitops(self, tmp_path: Path):
        gate = AuthoringGate(project_root=tmp_path)
        index_data = {
            "orders": [
                {"id": "WO-001", "title": "Backend", "assigned_agents": ["codex"], "dependencies": ["WO-003"]},
                {"id": "WO-002", "title": "Frontend", "assigned_agents": ["gemini"], "dependencies": ["WO-001"]},
                {"id": "WO-003", "title": "Release & GitOps", "assigned_agents": ["local-llm"], "dependencies": ["WO-002"]},
            ]
        }
        content = yaml.safe_dump(index_data)
        dec = gate.validate_artifact_content(".sync/work-orders/INDEX.yaml", content, agent="claude")
        assert not dec.passed
        # Should catch both inverted gitops dependency (WO-001 depending on GitOps WO-003) and cycle
        err_str = " ".join(dec.errors)
        assert "inverted GitOps dependency" in err_str or "dependency cycle" in err_str

    def test_readiness_gate_flags_inverted_gitops_dependency(self, tmp_path: Path):
        # Setup WO-001 (Worker) depending on WO-002 (GitOps)
        wo1 = _make_wo("WO-001", "Frontend UI", "gemini", deps=["WO-002"])
        wo2 = _make_wo("WO-002", "Version & Release", "local-llm", deps=[])
        _write_yaml(tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml", wo1)
        _write_yaml(tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml", wo2)
        _write_yaml(tmp_path / ".sync" / "contracts" / "WO-001.yaml", _make_contract("WO-001", "gemini"))
        _write_yaml(tmp_path / ".sync" / "contracts" / "WO-002.yaml", _make_contract("WO-002", "local-llm"))

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "INVERTED_GITOPS_DEPENDENCY" in result.issue_codes()
        inverted_issue = next(i for i in result.issues if i.code == "INVERTED_GITOPS_DEPENDENCY")
        assert "cannot depend on GitOps release work order 'WO-002'" in inverted_issue.message
        assert inverted_issue.canonical_rule == "dependency.dag"


class TestSupervisorIdleStallRecovery:
    """Test IDLE_STALL escalation to ARCHITECT_RECOVERY_DECISION."""

    def test_idle_stall_escalates_to_architect_recovery_decision(self, tmp_path: Path):
        mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mgr)

        # Set up active work orders where WO-002 has unmet dependency on WO-001
        wo2 = _make_wo("WO-002", "Frontend", "gemini", deps=["WO-001"])
        _write_yaml(tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml", wo2)

        state = RunState(
            run_id="run-test",
            product_goal="Test Goal",
            session_id="sess-test",
            workspace=str(tmp_path),
            phase=Phase.EXECUTING,
            worker_wo_ids=["WO-002"],
            completed_wo_ids=[],
            stall_ticks=2,
            max_stall_ticks=3,
            global_turn_budget=10,
            max_retries=3,
        )

        # Advance tick: stall_ticks becomes 3 -> should escalate to ARCHITECT_RECOVERY_DECISION
        res = supervisor.advance(state)
        assert res == AdvanceResult.TRANSITIONED
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        assert state.recovery_wo_id == "WO-002"
        assert state.stall_ticks == 0
        assert "WO-002" in state.worker_blockers
        assert state.worker_blockers["WO-002"]["failure_code"] == "DEPENDENCY_DEADLOCK"
        assert state.recovery_attempts["IDLE_STALL"] == 1

    def test_idle_stall_retries_exhausted_falls_back_to_waiting_for_human(self, tmp_path: Path):
        mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mgr)

        wo2 = _make_wo("WO-002", "Frontend", "gemini", deps=["WO-001"])
        _write_yaml(tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml", wo2)

        state = RunState(
            run_id="run-test",
            product_goal="Test Goal",
            session_id="sess-test",
            workspace=str(tmp_path),
            phase=Phase.EXECUTING,
            worker_wo_ids=["WO-002"],
            completed_wo_ids=[],
            stall_ticks=2,
            max_stall_ticks=3,
            global_turn_budget=10,
            max_retries=2,
            recovery_attempts={"IDLE_STALL": 2},  # retries exhausted
        )

        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_HUMAN
        assert state.phase == Phase.WAITING_FOR_HUMAN
        assert "IDLE_STALL" in state.error

    def test_dependency_deadlock_architect_fix_redispatches_worker(self, tmp_path: Path):
        mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mgr)

        wo_file = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml"
        _write_yaml(wo_file, _make_wo("WO-002", "Frontend", "gemini", deps=[]))

        state = RunState(
            run_id="run-test",
            product_goal="Test Goal",
            session_id="sess-test",
            workspace=str(tmp_path),
            phase=Phase.ARCHITECT_REPAIR,
            worker_wo_ids=["WO-002"],
            completed_wo_ids=[],
            worker_blockers={"WO-002": {"failure_code": "DEPENDENCY_DEADLOCK"}},
            recovery_wo_id="WO-002",
            repair_context={
                "source": "recovery",
                "action": "architect_fix",
                "work_order": "WO-002",
                "is_dependency_deadlock": True,
            },
            architect_repair_operation_id="op-repair-done",
        )

        # Mock operation completed in manager
        mgr.operations["op-repair-done"] = {
            "operation_id": "op-repair-done",
            "status": "COMPLETED",
            "result": {"status": "completed"},
        }

        res = supervisor._advance_architect_repair(state)
        # Should return WAITING_FOR_OPERATION and transition back to EXECUTING with WO-002 re-dispatched
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.phase == Phase.EXECUTING
        # Work order must NOT be marked completed because the worker hasn't executed it yet!
        assert "WO-002" not in state.completed_wo_ids
        # Blocker must be cleared
        assert "WO-002" not in state.worker_blockers


class TestLeadArchitectRemediationDirective:
    """Test that render_team_status directs active work order remediation."""

    def test_render_team_status_contains_active_remediation_directive(self, tmp_path: Path):
        wo1 = _make_wo("WO-001", "Frontend UI", "gemini")
        _write_yaml(tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml", wo1)

        # Simulate a stalled supervisor run
        sup_data = {
            "run_id": "run-999",
            "phase": "WAITING_FOR_HUMAN",
            "error": "IDLE_STALL (C_stall): Phase EXECUTING stalled for 3 ticks.",
        }
        _write_yaml(tmp_path / ".sync" / "runtime" / "supervisor" / "run-999.yaml", sup_data)

        status_text = render_team_status(tmp_path)
        assert "YOU HAVE FULL AUTHORITY TO DIRECTLY REPAIR WORK ORDERS" in status_text
        assert "DO NOT simply instruct the user to run :resume" in status_text
        assert "reconfigure work orders" in status_text
