"""Focused tests for authoring-failure recovery and contract-safe dispatch.

Covers the implementation plan "Authoring Failure Recovery and Contract-Safe
Dispatch": authoring readiness gate, fail-closed ARCHITECT_REPAIR routing,
durable worker-blocker evidence packets, machine-readable recovery decisions,
and plan-derived contract budgets.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_lifecycle_supervisor import MockSessionManager, write_ready_artifact_set
from validators.harness.authoring_readiness import (
    AuthoringReadinessResult,
    validate_authoring_readiness,
)
from validators.harness.authoring_gate import AuthoringGate
from validators.kernel.daemon.authoring import (
    build_child_contract,
    derive_implementation_estimate,
    synthesize_child_work_orders,
)
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.harness.contract_gate import (
    build_contract_failure_evidence,
    persist_blocker_evidence,
    verify_post_execution,
)
from validators.knowledge.contract import ContractAccessDenied


# ─── Shared helpers ───────────────────────────────────────────────────

WO_NOW = "2026-01-01T00:00:00+00:00"


def write_wo(ws: Path, record: dict[str, Any]) -> Path:
    path = ws / ".sync" / "work-orders" / "ACTIVE" / f"{record['id']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return path


def write_contract(ws: Path, record: dict[str, Any]) -> Path:
    path = ws / ".sync" / "contracts" / f"{record['work_order']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return path


def make_wo(wo_id: str, agent: str = "codex", deps: list[str] | None = None,
            deliv_path: str = "src/app.py", title: str | None = None,
            estimate: dict[str, Any] | None = None) -> dict[str, Any]:
    deliverable: dict[str, Any] = {
        "type": "code",
        "description": f"{wo_id} deliverable",
        "path": deliv_path,
    }
    record: dict[str, Any] = {
        "id": wo_id,
        "type": "FEATURE",
        "title": title or f"Task {wo_id}",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": [agent],
        "dependencies": list(deps or []),
        "deliverable": deliverable,
        "description": f"Implement {wo_id}",
        "created": WO_NOW,
        "updated": WO_NOW,
    }
    if estimate is None and deliv_path:
        # Default: plan the D024 companion test so the readiness gate's
        # test-coverage check passes unless a test asserts otherwise.
        stem = Path(deliv_path).stem
        estimate = {
            "expected_files": [deliv_path, f"tests/test_{stem}.py"],
            "max_files_touched": 2,
            "rationale": "deliverable plus companion test",
        }
    if estimate is not None:
        record["implementation_estimate"] = estimate
    return record


def make_contract(wo_id: str, agent: str = "codex", max_files: int = 5,
                  extra_allow: list[str] | None = None) -> dict[str, Any]:
    # Default allow covers the default deliverable path used by make_wo
    allow = [{"module": "PLAN.md"}, {"module": ".sync/decisions/**"},
             {"module": "src/app.py"}]
    for path in [*(extra_allow or [])]:
        allow.append({"module": path})
    return {
        "schema_version": 1,
        "agent_id": agent,
        "work_order": wo_id,
        "identity": {"role": "backend", "reports_to": "claude"},
        "scope": {
            "allow": allow,
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": max_files, "max_tokens": 0},
    }


def make_evidence(
    wo_id: str,
    tmp_path: Path,
    *,
    failure_code: str = "CONTRACT_FILE_BUDGET_EXCEEDED",
    observed: list[str] | None = None,
    file_budget: int = 10,
) -> dict[str, Any]:
    observed = observed if observed is not None else [
        f"src/module_{i:02d}.py" for i in range(11)
    ]
    contract_file = tmp_path / ".sync" / "contracts" / f"{wo_id}.yaml"
    contract_hash = (
        hashlib.sha256(contract_file.read_bytes()).hexdigest()
        if contract_file.is_file() else None
    )
    return {
        "failure_code": failure_code,
        "canonical_message": f"{failure_code}: {len(observed)} file(s) modified, "
                             f"exceeding budget max_files_touched limit of {file_budget}",
        "raw_reason": "11 file(s) modified, exceeding budget max_files_touched limit of 10",
        "work_order_id": wo_id,
        "agent": "codex",
        "operation_id": "op-010",
        "observed_files": observed,
        "observed_file_count": len(observed),
        "declared_modified_files": observed[:1],
        "declaration_mismatch": {"undeclared": observed[1:], "missing": []},
        "file_budget": file_budget,
        "contract_hash": contract_hash,
        "contract_revision": 1,
        "blocked_at": WO_NOW,
    }


def blocked_result(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": evidence["canonical_message"],
        "error": evidence["canonical_message"],
        "blockers": [evidence["canonical_message"]],
        "failure": evidence,
        "summary": None,
    }


def setup_executing_state(tmp_path: Path, wo_id: str = "WO-001") -> tuple[MockSessionManager, LifecycleSupervisor, RunState]:
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-recovery", "Scaffold app", tmp_path, "sess-001")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = [wo_id]
    write_ready_artifact_set(tmp_path, [(wo_id, "codex", "backend", "code", "src/app.py")])
    return mock_mgr, supervisor, state


# ─── Readiness gate unit tests ────────────────────────────────────────

class TestAuthoringReadinessGate:
    def test_valid_complete_set_passes(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001"))
        write_wo(tmp_path, make_wo("WO-002", deps=["WO-001"]))
        write_contract(tmp_path, make_contract("WO-001"))
        write_contract(tmp_path, make_contract("WO-002"))

        result = validate_authoring_readiness(tmp_path, plan_id="PLAN-001")
        assert isinstance(result, AuthoringReadinessResult)
        assert result.ready, [i.message for i in result.issues]
        assert result.issues == ()

    def test_missing_contract_fails(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001"))

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        codes = result.issue_codes()
        assert "MISSING_CONTRACT" in codes

    def test_orphan_contract_fails(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001"))
        write_contract(tmp_path, make_contract("WO-001"))
        write_contract(tmp_path, make_contract("WO-009"))

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "ORPHAN_CONTRACT" in result.issue_codes()

    def test_malformed_yaml_reports_yaml_category(self, tmp_path: Path) -> None:
        wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
        wo_dir.mkdir(parents=True, exist_ok=True)
        (wo_dir / "WO-001.yaml").write_text(
            "id: WO-001\n  bad indentation: [unclosed\n", encoding="utf-8"
        )

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "YAML_PARSE_ERROR" in result.issue_codes()
        assert any(i.category == "yaml" for i in result.issues)

    def test_milestone_uncovered_and_ambiguous(self, tmp_path: Path) -> None:
        (tmp_path / "PLAN.md").write_text(
            "# Plan\n\n## Milestones & Roadmap\n"
            "- [ ] Milestone 1: Build the rate limiter module\n"
            "- [ ] Milestone 2: Add the CLI interface\n",
            encoding="utf-8",
        )
        # Only one of the two milestones is covered
        write_wo(tmp_path, make_wo("WO-001", title="Build the rate limiter module"))
        write_contract(tmp_path, make_contract("WO-001"))

        result = validate_authoring_readiness(
            tmp_path, expected_milestones=[
                "Build the rate limiter module", "Add the CLI interface",
            ],
        )
        assert not result.ready
        assert "MILESTONE_UNCOVERED" in result.issue_codes()

        # Two WOs covering the same milestone is ambiguous
        write_wo(tmp_path, make_wo("WO-002", title="Rate limiter module helpers"))
        write_contract(tmp_path, make_contract("WO-002"))
        result2 = validate_authoring_readiness(
            tmp_path, expected_milestones=["Build the rate limiter module"],
        )
        assert not result2.ready
        assert "MILESTONE_AMBIGUOUS" in result2.issue_codes()

    def test_dependency_missing_and_cycle(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", deps=["WO-099"]))
        write_wo(tmp_path, make_wo("WO-002", deps=["WO-003"]))
        write_wo(tmp_path, make_wo("WO-003", deps=["WO-002"]))
        for wo_id in ("WO-001", "WO-002", "WO-003"):
            write_contract(tmp_path, make_contract(wo_id))

        result = validate_authoring_readiness(tmp_path)
        codes = result.issue_codes()
        assert "DEPENDENCY_MISSING" in codes
        assert "DEPENDENCY_CYCLE" in codes

    def test_budget_under_estimate_fails(self, tmp_path: Path) -> None:
        estimate = {
            "expected_files": [
                "requirements.txt", "src/app.py", "src/config.py", "tests/test_app.py",
            ],
            "max_files_touched": 4,
            "rationale": "manifest, entrypoint, config, companion test",
        }
        write_wo(tmp_path, make_wo("WO-001", estimate=estimate))
        # Contract under-budgets the estimate
        write_contract(tmp_path, make_contract("WO-001", max_files=2))

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "BUDGET_UNDER_ESTIMATE" in result.issue_codes()

        # Covering budget passes
        write_contract(tmp_path, make_contract("WO-001", max_files=4))
        result2 = validate_authoring_readiness(tmp_path)
        assert result2.ready, [i.message for i in result2.issues]

    def test_deliverable_outside_contract_scope(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", deliv_path="src/app.py"))
        # Contract does not authorize the deliverable path
        contract = make_contract("WO-001")
        contract["scope"]["allow"] = [
            {"module": "PLAN.md"}, {"module": ".sync/decisions/**"},
        ]
        write_contract(tmp_path, contract)

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "DELIVERABLE_OUTSIDE_SCOPE" in result.issue_codes()

    def test_worker_role_not_permitted(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", agent="claude"))
        write_contract(tmp_path, make_contract("WO-001", agent="claude"))

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "WORKER_ROLE_NOT_PERMITTED" in result.issue_codes()

    def test_contract_binding_mismatches(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001"))
        wrong = make_contract("WO-001")
        wrong["agent_id"] = "gemini"
        write_contract(tmp_path, wrong)

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "CONTRACT_AGENT_MISMATCH" in result.issue_codes()


# ─── Fail-closed authoring semantics ──────────────────────────────────

class TestFailClosedAuthoring:
    def test_malformed_authored_yaml_routes_to_repair_without_dispatch(
        self, tmp_path: Path,
    ) -> None:
        """A malformed authored artifact must block dispatch and produce a YAML
        diagnostic for the ARCHITECT_REPAIR turn."""
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-yaml", "Create login", tmp_path, "sess-001")
        state.phase = Phase.AUTHORING

        op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
        state.authoring_operation_id = op["operation_id"]

        # One valid WO+contract and one malformed work order artifact
        write_ready_artifact_set(tmp_path, [("WO-001", "codex", "backend", "code", "src/app.py")])
        wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
        (wo_dir / "WO-002.yaml").write_text(
            "Completed task via tools\nid: [WO-002\n  broken: yaml\n", encoding="utf-8"
        )

        mock_mgr.complete_operation(op["operation_id"], "COMPLETED")

        res = supervisor.advance(state)  # AUTHORING -> AUTHORING_READINESS_GATE
        assert res == AdvanceResult.TRANSITIONED
        res2 = supervisor.advance(state)  # gate fails -> ARCHITECT_REPAIR
        assert res2 == AdvanceResult.WAITING_FOR_OPERATION
        assert state.phase == Phase.ARCHITECT_REPAIR
        assert any(
            i.get("code") == "YAML_PARSE_ERROR" for i in state.readiness_issues
        )
        # Fail-closed: nothing was dispatched to any worker
        worker_ops = [
            o for o in mock_mgr.list_operations()
            if o.get("work_order_id") in ("WO-001", "WO-002")
        ]
        assert worker_ops == []
        # The malformed artifact was preserved for audit, not silently overwritten
        audit_dir = tmp_path / ".sync" / "reports" / "authoring" / "run-yaml"
        assert audit_dir.is_dir()

    def test_incomplete_artifact_set_prevents_dispatch(self, tmp_path: Path) -> None:
        """A valid WO without its contract must not begin dispatch."""
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-incomplete", "Create login", tmp_path, "sess-001")
        state.phase = Phase.AUTHORING
        op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
        state.authoring_operation_id = op["operation_id"]
        write_wo(tmp_path, make_wo("WO-001"))
        mock_mgr.complete_operation(op["operation_id"], "COMPLETED")

        supervisor.advance(state)
        res = supervisor.advance(state)
        assert state.phase == Phase.ARCHITECT_REPAIR
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert any(
            i.get("code") == "MISSING_CONTRACT" for i in state.readiness_issues
        )
        dispatched = [
            o for o in mock_mgr.list_operations() if o.get("work_order_id") == "WO-001"
        ]
        assert dispatched == []

    def test_valid_set_dispatches_only_dependency_ready_work_orders(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-valid", "Create login", tmp_path, "sess-001")
        state.phase = Phase.AUTHORING
        op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
        state.authoring_operation_id = op["operation_id"]
        write_wo(tmp_path, make_wo("WO-001"))
        write_wo(tmp_path, make_wo("WO-002", deps=["WO-001"]))
        write_contract(tmp_path, make_contract("WO-001"))
        write_contract(tmp_path, make_contract("WO-002"))
        mock_mgr.complete_operation(op["operation_id"], "COMPLETED")

        supervisor.advance(state)
        supervisor.advance(state)  # readiness gate passes
        assert state.phase == Phase.DISPATCHING
        supervisor.advance(state)  # dispatch pass
        assert state.phase == Phase.EXECUTING
        dispatched = {
            o["work_order_id"] for o in mock_mgr.list_operations() if o.get("work_order_id")
        }
        assert "WO-001" in dispatched
        assert "WO-002" not in dispatched  # dependency not yet completed

    def test_authoring_repair_loop_is_bounded(self, tmp_path: Path) -> None:
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-loop", "Create login", tmp_path, "sess-001")
        state.phase = Phase.AUTHORING
        op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
        state.authoring_operation_id = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "COMPLETED")

        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.phase == Phase.ARCHITECT_REPAIR

        # Each completed repair turn without authored artifacts re-enters repair
        for expected_attempt in (2, 3):
            repair_ops = [
                o for o in mock_mgr.list_operations()
                if o.get("metadata", {}).get("is_repair")
            ]
            assert repair_ops
            mock_mgr.complete_operation(repair_ops[-1]["operation_id"], "COMPLETED")
            supervisor.advance(state)

        assert state.phase == Phase.BLOCKED
        assert state.authoring_repair_attempts > state.max_authoring_repairs - 1
        assert "operator intervention" in (state.error or "")


# ─── Worker-blocker evidence packet ───────────────────────────────────

class TestWorkerBlockerEvidence:
    def _contract_project(self, tmp_path: Path, max_files: int = 10) -> Path:
        write_wo(tmp_path, make_wo("WO-001"))
        write_contract(tmp_path, make_contract("WO-001", max_files=max_files))
        return tmp_path

    def test_file_budget_breach_retains_all_observed_files(self, tmp_path: Path) -> None:
        project = self._contract_project(tmp_path, max_files=10)
        observed = tuple(
            f"src/module_{i:02d}.py" for i in range(11)
        )

        class _Task:
            work_order_id = "WO-001"
            identifier = "WO-001"

        class _Decision:
            modified_files = ("src/module_00.py",)

        with pytest.raises(ContractAccessDenied) as excinfo:
            verify_post_execution(
                project, "codex", _Task(), _Decision(), observed_files=observed
            )

        evidence = build_contract_failure_evidence(
            project, "codex", _Task(), excinfo.value,
            observed_files=observed, decision=_Decision(), operation_id="op-77",
        )
        assert evidence["failure_code"] == "CONTRACT_FILE_BUDGET_EXCEEDED"
        assert evidence["observed_file_count"] == 11
        assert evidence["observed_files"] == sorted(observed)  # exact 11, stable order
        assert evidence["file_budget"] == 10
        assert evidence["contract_hash"]
        assert evidence["declaration_mismatch"]["undeclared"] == sorted(observed)[1:]

        # Persisted before scratch cleanup and readable back
        path = persist_blocker_evidence(project, evidence)
        assert path is not None
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
        assert len(stored["observed_files"]) == 11
        assert stored["failure_code"] == "CONTRACT_FILE_BUDGET_EXCEEDED"

    def test_scope_violation_failure_code(self, tmp_path: Path) -> None:
        project = self._contract_project(tmp_path)
        observed = ("src/app.py", "secrets/other.py")

        class _Task:
            work_order_id = "WO-001"
            identifier = "WO-001"

        class _Decision:
            modified_files = ("src/app.py", "secrets/other.py")

        with pytest.raises(ContractAccessDenied):
            verify_post_execution(
                project, "codex", _Task(), _Decision(), observed_files=observed
            )
        # Direct scope denial message classification
        exc = ContractAccessDenied(
            "Modification to file secrets/other.py is outside allowed contract scope"
        )
        evidence = build_contract_failure_evidence(
            project, "codex", _Task(), exc, observed_files=observed,
        )
        assert evidence["failure_code"] == "CONTRACT_SCOPE_VIOLATION"

    def test_supervisor_routes_budget_breach_to_recovery_not_retry(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        evidence = make_evidence("WO-001", tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute work order WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))

        res = supervisor.advance(state)
        # No automatic same-contract worker retry: the run routes to the
        # Architect recovery decision phase.
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.worker_blockers["WO-001"]["failure_code"] == "CONTRACT_FILE_BUDGET_EXCEEDED"
        assert len(state.worker_blockers["WO-001"]["observed_files"]) == 11
        recovery_ops = [
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        ]
        assert len(recovery_ops) == 1
        assert recovery_ops[0]["agent_id"] == "claude"
        # The recovery prompt carries bounded evidence, not repeated strings
        prompt = recovery_ops[0]["prompt"]
        assert "CONTRACT_FILE_BUDGET_EXCEEDED" in prompt
        assert "src/module_00.py" in prompt
        assert "schemas/recovery-decision.schema.json" in prompt


# ─── Recovery decision contract ───────────────────────────────────────

class TestRecoveryDecisions:
    def test_budget_failure_cannot_retry_unchanged(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        evidence = make_evidence("WO-001", tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        supervisor.advance(state)  # -> ARCHITECT_RECOVERY_DECISION

        # Invalid decision: retry_unchanged on a budget failure
        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "retry_unchanged",
            "reason": "looks transient",
            "transient": True,
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        res = supervisor.advance(state)
        # Decision rejected -> corrective re-dispatch, still in recovery phase
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        decisions = state.recovery_decisions
        assert decisions and decisions[-1]["applied"] is False
        # No worker re-dispatch happened for the blocked WO
        worker_ops = [
            o for o in mock_mgr.list_operations()
            if o.get("work_order_id") == "WO-001"
            and not o.get("metadata", {}).get("is_recovery_decision")
        ]
        assert len(worker_ops) == 1  # only the original blocked attempt

    def test_transient_retry_unchanged_with_unchanged_contract(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        evidence = make_evidence(
            "WO-001", tmp_path, failure_code="OUTCOME_NOT_VERIFIED",
            observed=["src/app.py"],
        )
        evidence["canonical_message"] = "OUTCOME_NOT_VERIFIED: outcome_verified failed"
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        # Transient evidence escalates to the Architect only once the bounded
        # auto-retry budget is exhausted.
        state.retry_counts["WO-001"] = state.max_retries
        supervisor.advance(state)

        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "retry_unchanged",
            "reason": "tool loop halt was transient",
            "transient": True,
            "failure_code": "OUTCOME_NOT_VERIFIED",
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.phase == Phase.EXECUTING
        assert state.recovery_decisions[-1]["applied"] is True
        assert "WO-001" not in state.worker_blockers
        # The blocked attempt's operation is ignored and the worker re-dispatched
        assert op["operation_id"] in state.ignored_operation_ids
        reops = [
            o for o in mock_mgr.list_operations()
            if o.get("work_order_id") == "WO-001"
            and "recovered" in str(o.get("prompt", ""))
        ]
        assert len(reops) == 1

    def test_verification_block_retries_with_declared_observed_feedback(
        self, tmp_path: Path,
    ) -> None:
        """A verification-gate block (declared vs observed mismatch) retries
        with the scope evidence in the prompt instead of a blind same-prompt
        retry, and synthesizes the OUTCOME_NOT_VERIFIED evidence packet."""
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result={
            "status": "blocked",
            "error": (
                "verification gate failed: outcome_verified, scope_verified "
                "(declared ['index.html']; observed ['index/html/index.html'])"
            ),
            "scope_evidence": {
                "declared": ["index.html"],
                "observed": ["index/html/index.html"],
                "mismatch_reason": "declared but not written: ['index.html']",
            },
        })

        res = supervisor.advance(state)

        assert res == AdvanceResult.WAITING_FOR_OPERATION  # retry dispatched
        assert state.retry_counts["WO-001"] == 1
        assert state.worker_blockers["WO-001"]["failure_code"] == "OUTCOME_NOT_VERIFIED"
        retry_ops = [
            o for o in mock_mgr.list_operations()
            if o.get("work_order_id") == "WO-001" and o.get("operation_id") != op["operation_id"]
        ]
        assert retry_ops, "retry turn was not dispatched"
        prompt = str(retry_ops[-1].get("prompt", ""))
        assert "You declared these files" in prompt
        assert "'index.html'" in prompt
        assert "index/html/index.html" in prompt
        assert "EXACTLY that path" in prompt

    def test_verification_block_escalates_to_recovery_on_exhaustion(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        state.retry_counts["WO-001"] = state.max_retries
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result={
            "status": "blocked",
            "error": (
                "verification gate failed: outcome_verified "
                "(declared deliverable 'index.html' was not added or modified)"
            ),
            "scope_evidence": {"declared": ["index.html"], "observed": []},
        })

        supervisor.advance(state)

        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        assert state.recovery_attempts["WO-001"] == 1
        assert state.worker_blockers["WO-001"]["failure_code"] == "OUTCOME_NOT_VERIFIED"

    def test_split_rejected_when_children_collide_with_active_work_orders(
        self, tmp_path: Path,
    ) -> None:
        """Regression: the architect's split decision reused live work-order ids
        (WO-002/WO-003 were active, contracted, and mid-execution) — the split
        must be rejected back with a corrective diagnostic instead of applied."""
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        # WO-002 is an authored, active work order — the split reuses its id
        write_wo(tmp_path, make_wo("WO-002", deliv_path="src/other.py"))
        write_contract(tmp_path, make_contract("WO-002", extra_allow=["src/other.py"]))
        evidence = make_evidence("WO-001", tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        supervisor.advance(state)
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION

        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "split_work_order",
            "reason": "split WO-001",
            "replacement_work_orders": ["WO-002", "WO-004"],  # WO-002 collides
            "contract_revision": 1,
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        supervisor.advance(state)

        assert state.recovery_decisions[-1]["applied"] is False
        assert "collide" in state.recovery_decisions[-1]["rejected_reason"]
        # initial decision + corrective re-dispatch with the collision feedback
        assert state.recovery_attempts.get("WO-001") == 2
        # Corrective re-dispatch: the architect decides again with the feedback
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        # Nothing applied — the blocked WO's contract is untouched
        assert (tmp_path / ".sync" / "contracts" / "WO-001.yaml").exists()

    def test_amend_contract_places_repair_authorization_contract(
        self, tmp_path: Path,
    ) -> None:
        """amend_contract archives the original contract but must leave a
        transitional repair-authorization contract in its place: the repair
        turn's pre-execution gate matches the task WO against it, and it grants
        write authorization for the amended contract itself."""
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        state.retry_counts["WO-001"] = state.max_retries
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result={
            "status": "blocked",
            "error": (
                "verification gate failed: outcome_verified "
                "(declared deliverable 'src/app.py' was not added or modified)"
            ),
            "scope_evidence": {"declared": ["src/app.py"], "observed": []},
        })
        supervisor.advance(state)
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION

        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "amend_contract",
            "reason": "authorize root-level deliverable writes",
            "failure_code": "OUTCOME_NOT_VERIFIED",
            "transient": False,
            "contract_revision": 2,
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        supervisor.advance(state)

        assert state.phase == Phase.ARCHITECT_REPAIR
        assert state.repair_context.get("action") == "amend_contract"
        contract = yaml.safe_load(
            (tmp_path / ".sync" / "contracts" / "WO-001.yaml").read_text(encoding="utf-8")
        )
        # Pre-execution identity restored: contract work order matches the task.
        assert contract["work_order"] == "WO-001"
        assert contract["synthesized_by"] == "recovery-repair"
        modules = {r["module"] for r in contract["scope"]["allow"]}
        assert ".sync/contracts/WO-001.yaml" in modules  # self-write authorization
        assert ".sync/decisions/**" in modules
        # The original authored scope is preserved into the transitional contract.
        assert "src/app.py" in modules

    def test_stale_inbox_notices_archived_before_recovery_repair(
        self, tmp_path: Path,
    ) -> None:
        """Stale completion notices for non-active work orders are moved to
        _read/ before the repair turn dispatches — discover_next_task falls
        back to the first inbox item when the explicit work order cannot be
        resolved, and a stale notice hijacks the repair turn."""
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        stale = tmp_path / ".sync" / "inbox" / "claude" / "2026-10-07_claude_WO-000-complete.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("completed", encoding="utf-8")

        evidence = make_evidence("WO-001", tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        supervisor.advance(state)  # recovery decision dispatched
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION

        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "amend_contract",
            "reason": "authorize writes",
            "failure_code": "OUTCOME_NOT_VERIFIED",
            "transient": False,
            "contract_revision": 2,
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        supervisor.advance(state)

        assert state.phase == Phase.ARCHITECT_REPAIR
        assert not stale.exists()
        assert (tmp_path / ".sync" / "inbox" / "claude" / "_read" / stale.name).exists()

    def test_split_decision_supersedes_original_and_readies_children(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        evidence = make_evidence("WO-001", tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        supervisor.advance(state)

        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "split_work_order",
            "reason": "scaffold creates too many files for one contract",
            "replacement_work_orders": ["WO-002", "WO-003"],
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        res = supervisor.advance(state)
        # Decision applied -> bounded repair turn dispatched to author children
        assert state.phase == Phase.ARCHITECT_REPAIR
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert "WO-001" in state.superseded_wo_ids

        # The supervisor placed a transitional repair-authorization contract so
        # the repair turn passes pre-execution validation (task WO matches
        # contract WO) and is authorized to author the replacement contracts.
        transition_contract = yaml.safe_load(
            (tmp_path / ".sync" / "contracts" / "WO-001.yaml").read_text(encoding="utf-8")
        )
        assert transition_contract["work_order"] == "WO-001"
        assert transition_contract["synthesized_by"] == "recovery-repair"
        modules = {r["module"] for r in transition_contract["scope"]["allow"]}
        assert ".sync/work-orders/**" in modules
        assert ".sync/contracts/**" in modules
        assert ".sync/decisions/**" in modules

        # The Architect authors the dependency-safe children during repair
        write_wo(tmp_path, make_wo("WO-002", deliv_path="src/part_one.py", title="Scaffold part 1"))
        write_wo(tmp_path, make_wo("WO-003", deps=["WO-002"], deliv_path="src/part_two.py", title="Scaffold part 2"))
        write_contract(tmp_path, make_contract("WO-002", extra_allow=["src/part_one.py"]))
        write_contract(tmp_path, make_contract("WO-003", extra_allow=["src/part_two.py"]))
        repair_op = next(
            o for o in mock_mgr.list_operations() if o.get("metadata", {}).get("is_repair")
        )
        mock_mgr.complete_operation(repair_op["operation_id"], "COMPLETED")

        res2 = supervisor.advance(state)
        assert state.phase == Phase.EXECUTING
        assert res2 == AdvanceResult.WAITING_FOR_OPERATION
        # Original WO removed from ACTIVE and preserved for audit
        assert not (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").exists()
        superseded_dir = tmp_path / ".sync" / "reports" / "authoring" / "run-recovery" / "superseded"
        assert any(superseded_dir.glob("WO-001*"))
        # The superseded WO is no longer dispatchable; children are published
        assert "WO-001" not in state.published_wo_ids
        assert set(state.published_wo_ids) == {"WO-002", "WO-003"}
        # Child dependencies are correct and the graph is dependency-safe
        wo3 = yaml.safe_load(
            (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-003.yaml").read_text(encoding="utf-8")
        )
        assert wo3["dependencies"] == ["WO-002"]
        # Next dispatch pass dispatches WO-002 (deps met), not WO-003
        supervisor.advance(state)
        dispatched = {
            o["work_order_id"] for o in mock_mgr.list_operations() if o.get("work_order_id")
        }
        assert "WO-002" in dispatched
        assert "WO-003" not in dispatched

    def test_escalate_human_blocks_for_operator(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        evidence = make_evidence("WO-001", tmp_path, failure_code="CONTRACT_SCOPE_VIOLATION")
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        supervisor.advance(state)

        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "escalate_human",
            "reason": "material scope change requires product authority",
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_HUMAN
        assert state.phase == Phase.BLOCKED
        assert state.recovery_decisions[-1]["action"] == "escalate_human"
        assert "product authority" in (state.error or "")

    def test_terminal_block_decision(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        evidence = make_evidence("WO-001", tmp_path, failure_code="CONTRACT_SCOPE_DENIED")
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        supervisor.advance(state)

        decision_path = tmp_path / ".sync" / "decisions" / "recovery" / "WO-001.decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps({
            "work_order": "WO-001",
            "action": "terminal_block",
            "reason": "no safe recovery exists for this deliverable",
        }), encoding="utf-8")
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")

        res = supervisor.advance(state)
        assert res == AdvanceResult.BLOCKED
        assert state.phase == Phase.BLOCKED

    def test_missing_decision_file_bounded_redispatch_then_blocked(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path)
        evidence = make_evidence("WO-001", tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        evidence["operation_id"] = op["operation_id"]
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
        supervisor.advance(state)  # consumes recovery attempt 1

        # Recovery turn completes but produces no decision file
        rec_op = next(
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        )
        mock_mgr.complete_operation(rec_op["operation_id"], "COMPLETED")
        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION  # corrective re-dispatch (attempt 2)

        rec_ops = [
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        ]
        assert len(rec_ops) == 2  # original + corrective re-dispatch
        mock_mgr.complete_operation(rec_ops[-1]["operation_id"], "COMPLETED")
        supervisor.advance(state)
        # Attempts exhausted -> terminal BLOCKED with the evidence retained
        assert state.phase == Phase.BLOCKED
        assert "WO-001" in state.worker_blockers


# ─── Plan-derived budgets ─────────────────────────────────────────────

class TestPlanDerivedBudgets:
    def test_derive_implementation_estimate(self) -> None:
        estimate = derive_implementation_estimate(
            "Scaffold project", "create requirements.txt and src/app.py",
            "requirements.txt", ["requirements.txt", "src/app.py"],
        )
        assert estimate is not None
        assert estimate["expected_files"] == ["requirements.txt", "src/app.py"]
        assert estimate["max_files_touched"] == 2
        assert estimate["rationale"]

        assert derive_implementation_estimate("Title", "desc", None, None) is None

    def test_synthesized_scaffold_contract_budget_is_plan_derived(self, tmp_path: Path) -> None:
        plan_content = """# Project Plan: Scaffolding

## Current Architecture
Python utility project.

## Milestones & Roadmap
- [ ] Milestone 1: Project Scaffolding & Dependencies
  - [ ] Task 1.1: Configure project dependency manifest (requirements.txt)
"""
        (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
        records = synthesize_child_work_orders(tmp_path, plan_content)
        assert len(records) == 1
        wo = records[0]
        assert wo["implementation_estimate"]["expected_files"] == ["requirements.txt"]
        contract = yaml.safe_load(
            (tmp_path / ".sync" / "contracts" / "WO-001.yaml").read_text(encoding="utf-8")
        )
        assert contract["budget"]["max_files_touched"] == 1

    def test_build_child_contract_default_is_explicit_bootstrap_only(self, tmp_path: Path) -> None:
        contract = build_child_contract("WO-001", "codex", "backend")
        # Conservative default retained for explicitly designated bootstrap/internal WOs
        assert contract["budget"]["max_files_touched"] == 10
        sized = build_child_contract("WO-001", "codex", "backend", max_files_touched=3)
        assert sized["budget"]["max_files_touched"] == 3

    def test_recovery_decision_schema(self, tmp_path: Path) -> None:
        gate = AuthoringGate(project_root=tmp_path)
        schema = gate.get_schema("recovery-decision.schema.json")
        assert schema is not None
        from jsonschema import Draft7Validator
        validator = Draft7Validator(schema)
        good = {
            "work_order": "WO-001",
            "action": "amend_contract",
            "reason": "budget too small",
            "contract_revision": 2,
        }
        assert list(validator.iter_errors(good)) == []
        bad = {"work_order": "WO-001", "action": "do_something_else", "reason": "x"}
        assert list(validator.iter_errors(bad))


# ─── QA test-authoring loop (gemma writes and executes the suite) ─────

class TestQaTestAuthoringLoop:
    def test_qa_deliverable_spec_is_executable_suite(self) -> None:
        from validators.kernel.daemon.authoring import extract_deliverable_spec

        spec = extract_deliverable_spec(
            "Quality Assurance & Security Audit",
            ["Task 5.1: Execute end-to-end tests for the authentication flow."],
            "gemma",
        )
        assert spec["type"] == "code"
        assert spec["path"].startswith("tests/test_")
        assert spec["path"].endswith(".py")

    def test_companion_test_injected_into_code_estimates(self) -> None:
        estimate = derive_implementation_estimate(
            "Frontend", "build page", "src/frontend.html", [], "code"
        )
        assert "tests/test_frontend.py" in estimate["expected_files"]
        assert estimate["max_files_touched"] == 2
        # A QA work order delivering a test itself does not double-plan
        qa_estimate = derive_implementation_estimate(
            "QA", "suite", "tests/test_auth.py", [], "code"
        )
        assert qa_estimate["expected_files"] == ["tests/test_auth.py"]

    def test_readiness_flags_signoff_only_qa_work_order(self, tmp_path: Path) -> None:
        wo = make_wo("WO-005", agent="gemma")
        wo["deliverable"] = {"type": "doc", "description": "QA sign-off"}
        write_wo(tmp_path, wo)
        contract = make_contract("WO-005", agent="gemma")
        contract["identity"] = {"role": "qa", "reports_to": "claude"}
        write_contract(tmp_path, contract)

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "QA_DELIVERABLE_NOT_EXECUTABLE" in result.issue_codes()

    def test_readiness_flags_unplanned_test_coverage(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo(
            "WO-001",
            deliv_path="src/module.py",
            estimate={
                "expected_files": ["src/module.py"],
                "max_files_touched": 1,
                "rationale": "no test planned",
            },
        ))
        write_contract(tmp_path, make_contract("WO-001"))

        result = validate_authoring_readiness(tmp_path)
        assert not result.ready
        assert "TEST_COVERAGE_UNPLANNED" in result.issue_codes()
        issue = next(i for i in result.issues if i.code == "TEST_COVERAGE_UNPLANNED")
        assert "tests/test_module.py" in issue.message

    def test_readiness_passes_with_qa_planned_coverage(self, tmp_path: Path) -> None:
        # Code WO relies on the QA work order's declared suite for coverage
        write_wo(tmp_path, make_wo(
            "WO-001",
            estimate={
                "expected_files": ["src/module.py"],
                "max_files_touched": 1,
                "rationale": "implementation only",
            },
        ))
        write_wo(tmp_path, make_wo(
            "WO-002",
            agent="gemma",
            deliv_path="tests/test_module.py",
        ))
        write_contract(tmp_path, make_contract("WO-001"))
        qa_contract = make_contract("WO-002", agent="gemma", extra_allow=["tests/test_module.py"])
        qa_contract["identity"] = {"role": "qa", "reports_to": "claude"}
        qa_contract["scope"]["allow"].append({"module": ".sync/inbox/claude/**"})
        write_contract(tmp_path, qa_contract)

        result = validate_authoring_readiness(tmp_path)
        assert result.ready, [i.message for i in result.issues]

    def _gemma_setup(self, tmp_path: Path) -> tuple[MockSessionManager, LifecycleSupervisor, RunState]:
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-qa", "Build app", tmp_path, "sess-001")
        state.phase = Phase.EXECUTING
        state.worker_wo_ids = ["WO-005"]
        write_wo(tmp_path, make_wo("WO-005", agent="gemma", deliv_path="tests/test_app.py"))
        contract = make_contract("WO-005", agent="gemma", extra_allow=["tests/test_app.py"])
        contract["identity"] = {"role": "qa", "reports_to": "claude"}
        contract["scope"]["allow"].append({"module": "tests/**"})
        contract["scope"]["allow"].append({"module": ".sync/inbox/claude/**"})
        write_contract(tmp_path, contract)
        (tmp_path / "tests").mkdir(exist_ok=True)
        return mock_mgr, supervisor, state

    def test_qa_dispatch_prompt_demands_author_and_execute(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = self._gemma_setup(tmp_path)
        supervisor.advance(state)
        qa_ops = [o for o in mock_mgr.list_operations() if o.get("work_order_id") == "WO-005"]
        assert len(qa_ops) == 1
        prompt = qa_ops[0]["prompt"]
        assert "tests/test_app.py" in prompt
        assert "run_tests" in prompt
        assert "run_security_scan" in prompt
        assert "author" in prompt.lower()
        assert "security checklist" in prompt.lower()

    def test_qa_completion_requires_test_file_and_execution_evidence(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr, supervisor, state = self._gemma_setup(tmp_path)
        op = mock_mgr.start_turn("sess-001", "Execute WO-005", work_order_id="WO-005", agent_id="gemma")
        # Telemetry reported, but no test file written and nothing executed
        mock_mgr.complete_operation(op["operation_id"], "COMPLETED", result={
            "status": "completed",
            "summary": "QA done",
            "commands_audit": [],
            "tool_calls_audit": [{"tool": "read_file", "path": "src/app.py"}],
        })
        res = supervisor.advance(state)
        # Bounded nudge retry, not silent completion
        assert state.phase == Phase.EXECUTING
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.retry_counts.get("WO-005") == 1
        assert "WO-005" not in state.completed_wo_ids
        retry_ops = [o for o in mock_mgr.list_operations() if "attempt 2" in str(o.get("prompt", ""))]
        assert retry_ops and "run_tests" in retry_ops[0]["prompt"]

        # Second attempt authors the suite, executes it, and runs the scan
        (tmp_path / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
        retry_op = [o for o in mock_mgr.list_operations() if "attempt 2" in str(o.get("prompt", ""))][-1]
        mock_mgr.complete_operation(retry_op["operation_id"], "COMPLETED", result={
            "status": "completed",
            "summary": "QA done",
            "commands_audit": [],
            "tool_calls_audit": [
                {"tool": "write_file", "path": "tests/test_app.py"},
                {"tool": "run_tests", "path": "tests/test_app.py"},
                {"tool": "run_security_scan", "target": "src"},
            ],
        })
        supervisor.advance(state)
        assert "WO-005" in state.completed_wo_ids

    def test_qa_completion_requires_security_scan_evidence(
        self, tmp_path: Path,
    ) -> None:
        """A QA turn that ran the tests but skipped the security scan is not
        accepted — the scan is part of the required evidence."""
        mock_mgr, supervisor, state = self._gemma_setup(tmp_path)
        (tmp_path / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
        op = mock_mgr.start_turn("sess-001", "Execute WO-005", work_order_id="WO-005", agent_id="gemma")
        mock_mgr.complete_operation(op["operation_id"], "COMPLETED", result={
            "status": "completed",
            "summary": "QA done",
            "commands_audit": [],
            "tool_calls_audit": [
                {"tool": "write_file", "path": "tests/test_app.py"},
                {"tool": "run_tests", "path": "tests/test_app.py"},
            ],
        })
        res = supervisor.advance(state)
        assert state.phase == Phase.EXECUTING
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert "WO-005" not in state.completed_wo_ids
        assert state.retry_counts.get("WO-005") == 1
        retry_ops = [o for o in mock_mgr.list_operations() if "attempt 2" in str(o.get("prompt", ""))]
        assert retry_ops and "run_security_scan" in retry_ops[-1]["prompt"]

    def test_qa_evidence_exhaustion_routes_to_architect_recovery(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr, supervisor, state = self._gemma_setup(tmp_path)
        # The suite was authored, but no execution was recorded — the gap the
        # evidence check targets (a missing file is caught by the earlier
        # deliverable-existence retry).
        (tmp_path / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
        op = mock_mgr.start_turn("sess-001", "Execute WO-005", work_order_id="WO-005", agent_id="gemma")
        mock_mgr.complete_operation(op["operation_id"], "COMPLETED", result={
            "status": "completed",
            "summary": "QA done",
            "commands_audit": [],
            "tool_calls_audit": [],
        })
        # Each exhausted-evidence turn consumes a nudge retry
        for expected_attempt in (2, 3):
            res = supervisor.advance(state)
            assert res == AdvanceResult.WAITING_FOR_OPERATION
            assert state.phase == Phase.EXECUTING
            assert state.retry_counts.get("WO-005") == expected_attempt - 1
            nudges = [
                o for o in mock_mgr.list_operations()
                if f"attempt {expected_attempt}" in str(o.get("prompt", ""))
            ]
            assert nudges, f"expected nudge attempt {expected_attempt}"
            mock_mgr.complete_operation(nudges[-1]["operation_id"], "COMPLETED", result={
                "status": "completed",
                "summary": "QA done",
                "commands_audit": [],
                "tool_calls_audit": [],
            })
        # Retries exhausted — the gap becomes a governance blocker routed to
        # the Architect's recovery decision, not a silent completion.
        res = supervisor.advance(state)
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        evidence = state.worker_blockers["WO-005"]
        assert evidence["failure_code"] == "QA_EVIDENCE_MISSING"
        recovery_ops = [
            o for o in mock_mgr.list_operations()
            if o.get("metadata", {}).get("is_recovery_decision")
        ]
        assert len(recovery_ops) == 1
        assert "QA_EVIDENCE_MISSING" in recovery_ops[0]["prompt"]

    def test_declared_qa_suite_missing_rejects_signoff_notices(
        self, tmp_path: Path,
    ) -> None:
        mock_mgr, supervisor, state = self._gemma_setup(tmp_path)
        # gemma wrote sign-off notices but never the declared test suite
        inbox = tmp_path / ".sync" / "inbox" / "claude"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "gemma_WO-005_verdict.md").write_text("APPROVED", encoding="utf-8")
        assert supervisor._check_deliverable_exists("WO-005", tmp_path) is False
        # ...but a sign-off style WO (no declared path) is accepted downstream
        wo = make_wo("WO-006", agent="gemma", deliv_path="tests/test_app.py")
        wo["deliverable"] = {"type": "doc", "description": "QA sign-off"}
        del wo["implementation_estimate"]
        write_wo(tmp_path, wo)
        (inbox / "gemma_WO-006_verdict.md").write_text("APPROVED", encoding="utf-8")
        assert supervisor._check_deliverable_exists("WO-006", tmp_path) is True


# ─── State serialization ──────────────────────────────────────────────

def test_run_state_recovery_fields_roundtrip(tmp_path: Path) -> None:
    state = RunState(
        run_id="run-rt",
        product_goal="goal",
        workspace=str(tmp_path),
        session_id="sess-rt",
        bootstrap_mode=True,
        authoring_revision=3,
        authoring_repair_attempts=1,
        authoring_operation_failed=True,
        published_wo_ids=["WO-001"],
        readiness_issues=[{"code": "YAML_PARSE_ERROR", "category": "yaml", "message": "m"}],
        worker_blockers={"WO-001": {"failure_code": "CONTRACT_FILE_BUDGET_EXCEEDED"}},
        recovery_wo_id="WO-001",
        recovery_attempts={"WO-001": 1},
        recovery_decisions=[{"work_order": "WO-001", "action": "retry_unchanged"}],
        superseded_wo_ids=["WO-000"],
        milestone_exemptions=["Scaffold"],
        contract_revisions={"WO-001": 2},
    )
    restored = RunState.from_dict(state.to_dict())
    assert restored.bootstrap_mode is True
    assert restored.authoring_revision == 3
    assert restored.authoring_operation_failed is True
    assert restored.published_wo_ids == ["WO-001"]
    assert restored.readiness_issues[0]["code"] == "YAML_PARSE_ERROR"
    assert restored.worker_blockers["WO-001"]["failure_code"] == "CONTRACT_FILE_BUDGET_EXCEEDED"
    assert restored.recovery_wo_id == "WO-001"
    assert restored.recovery_attempts == {"WO-001": 1}
    assert restored.superseded_wo_ids == ["WO-000"]
    assert restored.milestone_exemptions == ["Scaffold"]
    assert restored.contract_revisions == {"WO-001": 2}


# ─── Authoring-turn verification robustness ───────────────────────────

class TestAuthoringTurnVerification:
    """Architect authoring turns are validated by the gate machinery, not by
    declaration equality: a 31B model's modified_files list is informational."""

    def _init_workspace(self, tmp_path: Path) -> Path:
        from cli.init import init
        from validators.kernel.daemon.manager import synthesize_bootstrap_planning
        init(tmp_path, name="T", no_git=True)
        synthesize_bootstrap_planning(tmp_path, "Build app")
        return tmp_path

    VALID_WO_YAML = (
        "id: WO-001\n"
        "type: FEATURE\n"
        "title: Authored Backend\n"
        "status: ACTIVE\n"
        "priority: P1\n"
        "assigned_agents: [codex]\n"
        "dependencies: []\n"
        "deliverable:\n"
        "  type: code\n"
        "  path: src/backend.py\n"
        "  description: Backend implementation\n"
        "description: Implement the backend\n"
        "created: '2026-01-01T00:00:00+00:00'\n"
        "updated: '2026-01-01T00:00:00+00:00'\n"
    )

    def _scripted_authoring_adapter(self, writes: list[tuple[str, str]], declared: list[str]):
        from validators.kernel.providers.models import (
            Message, ProviderResponse, ToolCallRequest, TokenUsage,
        )

        class Adapter:
            provider_name = "scripted"
            model_name = "scripted-model"

            def complete(self, messages, **kwargs):
                if not any(m.role == "tool" for m in messages):
                    return ProviderResponse(
                        message=Message.assistant(content="", tool_calls=[
                            ToolCallRequest(id=f"c{i}", name="write_file",
                                            arguments={"path": p, "content": c})
                            for i, (p, c) in enumerate(writes)
                        ]),
                        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                    )
                return ProviderResponse(
                    message=Message.assistant(content=json.dumps({
                        "status": "completed",
                        "summary": "Authored artifacts",
                        "report_markdown": "Authored artifacts.",
                        "modified_files": declared,
                        "release_target": "v3.1.0",
                    })),
                    usage=TokenUsage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
                )

        return Adapter()

    def test_authoring_turn_declaration_mismatch_still_completes(self, tmp_path: Path) -> None:
        """Declared set larger than the observed write set no longer dead-ends
        the authoring turn (the readiness gate validates the real artifacts)."""
        from validators.harness.runner import AgentRunner

        ws = self._init_workspace(tmp_path)
        adapter = self._scripted_authoring_adapter(
            writes=[(".sync/work-orders/ACTIVE/WO-001.yaml", self.VALID_WO_YAML)],
            declared=[
                ".sync/work-orders/ACTIVE/WO-001.yaml",
                ".sync/contracts/WO-001.yaml",  # declared but never written
            ],
        )
        runner = AgentRunner(ws, "claude", provider_adapter=adapter)
        result = runner.run_once(
            prompt="Author the implementation work orders and contracts for the tasks in PLAN.md",
            work_order_id="WO-000",
            operation_id="op-authoring",
            is_authoring=True,
        )
        assert result.status == "completed", result.reason

    def test_authoring_turn_out_of_scope_write_blocked_with_evidence(self, tmp_path: Path) -> None:
        """When the scope gate fails an authoring turn, the block carries the
        observed/declared evidence. (Out-of-scope writes are usually denied
        earlier at the tool boundary; this exercises the second net.)"""
        import validators.harness.contract_gate as cg
        from validators.harness.runner import AgentRunner
        from validators.knowledge.contract import ContractAccessDenied

        ws = self._init_workspace(tmp_path)
        adapter = self._scripted_authoring_adapter(
            writes=[(".sync/work-orders/ACTIVE/WO-001.yaml", self.VALID_WO_YAML)],
            declared=[".sync/work-orders/ACTIVE/WO-001.yaml"],
        )

        real_verify = cg.verify_post_execution

        def _failing_verify(*args, **kwargs):
            raise ContractAccessDenied(
                "Modification to file src/hack.py is outside allowed contract scope"
            )

        cg.verify_post_execution = _failing_verify
        try:
            runner = AgentRunner(ws, "claude", provider_adapter=adapter)
            result = runner.run_once(
                prompt="Author the implementation work orders and contracts for the tasks in PLAN.md",
                work_order_id="WO-000",
                operation_id="op-authoring",
                is_authoring=True,
            )
        finally:
            cg.verify_post_execution = real_verify

        assert result.status == "blocked"
        # The primary post-decision contract gate blocks first, with the full
        # durable evidence packet (observed files, contract hash, failure code).
        assert "CONTRACT_SCOPE_VIOLATION" in (result.reason or "")
        failure = (result.meta or {}).get("failure") or {}
        assert failure.get("failure_code") == "CONTRACT_SCOPE_VIOLATION"
        assert ".sync/work-orders/ACTIVE/WO-001.yaml" in failure.get("observed_files", [])


# ─── Lazy-turn nudge (declared writes, no tool invocations) ───────────

class TestLazyTurnNudge:
    """A turn that declared writes but invoked no tools is retried with an
    explicit write instruction instead of dead-ending the run."""

    def _blocked_result(self, declared: list[str]) -> dict[str, Any]:
        return {
            "status": "blocked",
            "reason": (
                f"verification gate failed: scope_verified "
                f"(declared {declared}; observed [])"
            ),
            "scope_evidence": {"declared": declared, "observed": []},
        }

    def test_lazy_qa_turn_gets_explicit_write_nudge(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path, "WO-004")
        state.worker_wo_ids = ["WO-004"]
        # Swap in a gemma WO with a QA deliverable
        write_wo(tmp_path, make_wo("WO-004", agent="gemma", deliv_path="tests/test_app.py"))
        contract = make_contract("WO-004", agent="gemma")
        contract["identity"] = {"role": "qa", "reports_to": "claude"}
        write_contract(tmp_path, contract)

        op = mock_mgr.start_turn("sess-001", "Execute WO-004", work_order_id="WO-004", agent_id="gemma")
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=self._blocked_result(
            ["tests/test_quality_assurance_testing_agent_gemma.py"]))

        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.phase == Phase.EXECUTING
        assert state.retry_counts.get("WO-004") == 1
        assert op["operation_id"] in state.ignored_operation_ids
        nudges = [o for o in mock_mgr.list_operations() if "attempt 2" in str(o.get("prompt", ""))]
        assert len(nudges) == 1
        prompt = nudges[0]["prompt"]
        assert "write_file" in prompt
        assert "tests/test_app.py" in prompt  # deliverable path takes precedence
        assert "run_tests" in prompt and "run_security_scan" in prompt

    def test_lazy_worker_turn_nudged_with_declared_paths(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path, "WO-001")
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=self._blocked_result(
            ["src/backend.py", "tests/test_backend.py"]))

        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.phase == Phase.EXECUTING
        nudges = [o for o in mock_mgr.list_operations() if "attempt 2" in str(o.get("prompt", ""))]
        assert len(nudges) == 1
        prompt = nudges[0]["prompt"]
        assert "invoked NO tools" in prompt
        assert "src/backend.py" in prompt and "write_file" in prompt

    def test_lazy_turn_exhaustion_escalates_to_recovery_with_evidence(self, tmp_path: Path) -> None:
        mock_mgr, supervisor, state = setup_executing_state(tmp_path, "WO-001")
        state.max_retries = 1
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result=self._blocked_result(["src/backend.py"]))
        res = supervisor.advance(state)  # nudge (attempt 2)
        assert res == AdvanceResult.WAITING_FOR_OPERATION

        nudges = [o for o in mock_mgr.list_operations() if "attempt 2" in str(o.get("prompt", ""))]
        mock_mgr.complete_operation(nudges[-1]["operation_id"], "BLOCKED", result=self._blocked_result(["src/backend.py"]))
        supervisor.advance(state)  # retries exhausted -> Architect recovery decision
        assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
        assert state.worker_blockers["WO-001"]["failure_code"] == "OUTCOME_NOT_VERIFIED"
        assert "scope_verified" in state.worker_blockers["WO-001"]["canonical_message"]

    def test_non_lazy_scope_block_still_terminal(self, tmp_path: Path) -> None:
        """Observed non-empty (real writes, wrong scope) keeps the terminal
        behavior — the nudge is only for turns that wrote nothing."""
        mock_mgr, supervisor, state = setup_executing_state(tmp_path, "WO-001")
        op = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
        mock_mgr.complete_operation(op["operation_id"], "BLOCKED", result={
            "status": "blocked",
            "reason": "verification gate failed: scope_verified (declared ['src/app.py']; observed ['src/other.py'])",
            "scope_evidence": {"declared": ["src/app.py"], "observed": ["src/other.py"]},
        })
        res = supervisor.advance(state)
        assert res == AdvanceResult.BLOCKED
        assert state.phase == Phase.BLOCKED


def test_readiness_requires_qa_verdict_channel(tmp_path: Path) -> None:
    """A QA contract without the Architect-inbox verdict channel fails
    readiness — the QA worker is instructed to write its verdict there."""
    write_wo(tmp_path, make_wo("WO-005", agent="gemma", deliv_path="tests/test_auth.py"))
    contract = make_contract("WO-005", agent="gemma", extra_allow=["tests/test_auth.py"])
    contract["scope"]["allow"] = [
        {"module": "PLAN.md"},
        {"module": "tests/**"},  # no .sync/inbox/claude/**
    ]
    write_contract(tmp_path, contract)

    result = validate_authoring_readiness(tmp_path)
    assert not result.ready
    assert "QA_VERDICT_CHANNEL_MISSING" in result.issue_codes()

    # With the channel authorized, readiness passes
    contract["scope"]["allow"].append({"module": ".sync/inbox/claude/**"})
    write_contract(tmp_path, contract)
    result2 = validate_authoring_readiness(tmp_path)
    assert result2.ready, [i.message for i in result2.issues]


# ─── Worker declaration direction (write-all-declared; extras scope-gated) ──

class TestWorkerDeclarationDirection:
    """The declaration gate fails a worker only for claimed-but-unwritten
    files; extra in-scope writes are authorized by the scope gates."""

    def _worker_fixture(self, tmp_path: Path) -> Path:
        from cli.init import init
        init(tmp_path, name="W", no_git=True)
        wo = {
            "id": "WO-001", "type": "FEATURE", "title": "Backend", "status": "ACTIVE",
            "priority": "P1", "assigned_agents": ["codex"], "dependencies": [],
            "deliverable": {"type": "code", "path": "src/app.py", "description": "app"},
            "description": "Implement backend",
            "created": "2026-01-01T00:00:00+00:00", "updated": "2026-01-01T00:00:00+00:00",
        }
        wo_path = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml"
        wo_path.parent.mkdir(parents=True, exist_ok=True)
        wo_path.write_text(yaml.safe_dump(wo, sort_keys=False), encoding="utf-8")
        contract = {
            "schema_version": 1, "agent_id": "codex", "work_order": "WO-001",
            "identity": {"role": "backend", "reports_to": "claude"},
            "scope": {
                "allow": [{"module": "PLAN.md"}, {"module": "src/**"},
                          {"module": ".sync/decisions/**"}],
                "deny": [{"module": ".git/**"}],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 5, "max_tokens": 0},
        }
        cpath = tmp_path / ".sync" / "contracts" / "WO-001.yaml"
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        return tmp_path

    def _scripted_worker_adapter(self, writes: list[tuple[str, str]], declared: list[str]):
        from validators.kernel.providers.models import (
            Message, ProviderResponse, ToolCallRequest, TokenUsage,
        )

        class Adapter:
            provider_name = "scripted"
            model_name = "scripted-model"

            def complete(self, messages, **kwargs):
                if not any(m.role == "tool" for m in messages):
                    return ProviderResponse(
                        message=Message.assistant(content="", tool_calls=[
                            ToolCallRequest(id=f"c{i}", name="write_file",
                                            arguments={"path": p, "content": c})
                            for i, (p, c) in enumerate(writes)
                        ]),
                        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                    )
                return ProviderResponse(
                    message=Message.assistant(content=json.dumps({
                        "status": "completed",
                        "summary": "Implemented",
                        "report_markdown": "Implemented.",
                        "modified_files": declared,
                        "release_target": "v3.1.0",
                    })),
                    usage=TokenUsage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
                )

        return Adapter()

    def test_worker_extra_in_scope_write_still_completes(self, tmp_path: Path) -> None:
        """A worker that writes its deliverable plus an extra in-scope file but
        declares only the deliverable completes — extras are scope-gated."""
        from validators.harness.runner import AgentRunner

        ws = self._worker_fixture(tmp_path)
        adapter = self._scripted_worker_adapter(
            writes=[
                ("src/app.py", "app = True\n"),
                ("src/helper.py", "helper = True\n"),  # extra, in scope, undeclared
            ],
            declared=["src/app.py"],
        )
        runner = AgentRunner(ws, "codex", provider_adapter=adapter)
        result = runner.run_once(
            prompt="Implement work order WO-001",
            work_order_id="WO-001",
            operation_id="op-w",
        )
        assert result.status == "completed", result.reason

    def test_worker_claimed_but_unwritten_claims_are_pruned(self, tmp_path: Path) -> None:
        """Turns with some real writes get unwritten claims pruned by the
        pre-existing phantom-declaration pruning (the turn completes, and the
        published decision only lists what was actually written). Turns with
        NO writes at all keep their declarations and are handled by the
        supervisor's lazy-turn nudge instead."""
        from validators.harness.runner import AgentRunner

        ws = self._worker_fixture(tmp_path)
        adapter = self._scripted_worker_adapter(
            writes=[("src/app.py", "app = True\n")],
            declared=["src/app.py", "src/missing.py"],  # never written
        )
        runner = AgentRunner(ws, "codex", provider_adapter=adapter)
        result = runner.run_once(
            prompt="Implement work order WO-001",
            work_order_id="WO-001",
            operation_id="op-w",
        )
        assert result.status == "completed"
        meta = result.meta or {}
        assert meta.get("modified_files") == ["src/app.py"]
        assert (ws / "src" / "missing.py").exists() is False


def test_blocker_details_plumb_through_decision_and_meta(tmp_path: Path) -> None:
    """A blocked decision carrying blocker_details survives validation,
    lands in the run result meta, and the schema accepts the field."""
    import json as _json

    from validators.harness.runner import AgentRunner, HarnessDecision

    # HarnessDecision carries the details
    d = HarnessDecision(
        status="blocked", summary="s", report_markdown="r",
        blockers=("finding one",),
        blocker_details=({"finding": "finding one", "remediation": "fix it"},),
        modified_files=(), release_target=None,
        retrieval_queries=(), uncertainty=(),
    )
    assert d.blocker_details[0]["remediation"] == "fix it"

    # The output schema accepts the optional field
    from validators.harness.authoring_gate import AuthoringGate
    gate = AuthoringGate(project_root=tmp_path)
    schema = gate.get_schema("harness-output.schema.json")
    assert schema is not None and "blocker_details" in schema["properties"]

    # A full runner turn with a scripted blocked decision exposes the details
    from validators.kernel.providers.models import (
        Message, ProviderResponse, ToolCallRequest, TokenUsage,
    )
    from cli.init import init
    from validators.kernel.daemon.manager import synthesize_bootstrap_planning
    init(tmp_path / "ws2", name="BD", no_git=True)
    ws2 = tmp_path / "ws2"
    synthesize_bootstrap_planning(ws2, "Build app")

    class BlockedAdapter:
        provider_name = "scripted"
        model_name = "scripted-model"

        def complete(self, messages, **kwargs):
            if not any(m.role == "tool" for m in messages):
                return ProviderResponse(
                    message=Message.assistant(content="", tool_calls=[
                        ToolCallRequest(id="c0", name="write_file",
                                        arguments={"path": "PLAN.md",
                                                   "content": "# Plan\n\n## Milestones & Roadmap\n- [ ] M1: x\n"})]
                    ),
                    usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                )
            return ProviderResponse(
                message=Message.assistant(content=_json.dumps({
                    "status": "blocked",
                    "summary": "Review found issues",
                    "report_markdown": "Issues.",
                    "blockers": ["finding one"],
                    "blocker_details": [
                        {"finding": "finding one", "remediation": "fix it", "path": "src/app.py"}
                    ],
                })),
                usage=TokenUsage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
            )

    runner = AgentRunner(ws2, "claude", provider_adapter=BlockedAdapter())
    result = runner.run_once(
        prompt="Review the deliverables",
        work_order_id="WO-000",
        operation_id="op-rev",
        is_authoring=True,
    )
    assert result.status == "blocked"
    meta = result.meta or {}
    details = meta.get("blocker_details") or []
    assert details and details[0]["remediation"] == "fix it"
