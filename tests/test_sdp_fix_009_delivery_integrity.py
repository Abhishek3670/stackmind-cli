"""SDP-FIX-009 — end-to-end delivery integrity acceptance suite.

Deterministic, fixture-driven scenarios over the authoritative production
evaluators:

* planning completeness and work-order integrity (`delivery_integrity`),
* artifact evidence and independent verification,
* revision-bound QA evidence (`review_gate`),
* combined-product integration (`integration_gate` + combined checks),
* release readiness aggregation,
* supervisor eligibility and lifecycle transitions,
* recovery and stale-evidence invalidation.

No live provider or external service is required. Real TUI acceptance is a
separate, manual procedure (`SDP-FIX-009-REAL-TUI-ACCEPTANCE.md`).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from cli.tui.commands import CANCEL_OUTCOME_UNKNOWN, CancellationOutcome
from cli.tui.commands import unresolved_cancellation_operations
from cli.tui.state import AutonomousDeliveryState, OperationNode
from validators.harness import delivery_integrity as di
from validators.harness import review_gate
from validators.harness.contract_gate import verify_post_execution
from validators.harness.integration_gate import (
    IntegrationCheck,
    IntegrationGateDecision,
)
from validators.harness.runner.models import HarnessDecision, HarnessTask
from validators.kernel.daemon.supervisor.control_plane import (
    WaitStatus,
    WaitType,
    evaluate_work_order_eligibility,
)
from validators.kernel.daemon.supervisor.models import Phase, RunState
from validators.knowledge.contract import ContractAccessDenied


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def write_plan(ws: Path, criteria=("User can register",)) -> None:
    text = "# Delivery Plan\n\n## Acceptance Criteria\n" + "".join(f"- {c}\n" for c in criteria)
    text += "\n## Stack\n- Python\n- SQLite\n"
    (ws / "PLAN.md").write_text(text, encoding="utf-8")


def write_wo(
    ws: Path,
    wo_id: str,
    *,
    status: str = "ACTIVE",
    agent: str = "codex",
    deliverable: str = "src/app.py",
    criteria: list[str] | None = None,
    deps: list[str] | None = None,
    wo_type: str = "FEATURE",
    title: str | None = None,
    test_plan: dict | None = None,
) -> dict:
    bucket = "ACTIVE" if status == "ACTIVE" else status
    directory = ws / ".sync" / "work-orders" / bucket
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": wo_id,
        "type": wo_type,
        "title": title or f"{wo_id} implementation",
        "status": status,
        "assigned_agents": [agent],
        "dependencies": deps or [],
        "deliverable": {"type": "code", "path": deliverable, "description": "deliverable"},
        "acceptance_criteria": criteria or [f"{wo_id} works"],
    }
    if test_plan:
        data["test_plan"] = test_plan
    (directory / f"{wo_id}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return data


def write_contract(ws: Path, wo_id: str, agent: str = "codex") -> None:
    directory = ws / ".sync" / "contracts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{wo_id}.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "agent_id": agent,
        "work_order": wo_id,
        "scope": {"allow": [{"module": "**"}], "deny": [], "write": "read-write"},
        "budget": {"max_files_touched": 20},
    }), encoding="utf-8")


def write_file(ws: Path, rel: str, content: str) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_qa(ws: Path, wo_id: str, verdict: str = "PASS", outcome: str = "PASS") -> dict:
    data = {
        "schema": review_gate.QA_VERDICT_SCHEMA,
        "work_order_id": wo_id,
        "verdict": verdict,
        "criteria": [{"id": "AC1", "outcome": outcome}],
        "findings": [],
        "required_actions": [],
        "evidence": [{"kind": "test", "command": "pytest -q tests/", "exit_code": 0, "passed": verdict == "PASS"}],
        "subject_hash": review_gate.subject_hash(ws, wo_id),
    }
    (ws / ".sync" / "reviews" / "qa").mkdir(parents=True, exist_ok=True)
    review_gate.persist_qa_verdict(ws, data)
    return data


def full_plan(ws: Path, *, qa: bool = True, integration: bool = True) -> None:
    write_plan(ws)
    write_wo(ws, "WO-001", deliverable="src/app.py", criteria=["User can register"])
    if qa:
        write_wo(
            ws, "WO-002", agent="gemma", wo_type="QA", deliverable="tests/test_app.py",
            criteria=["Automated tests pass"], title="Q/A Verification Suite",
        )
    if integration:
        write_wo(
            ws, "WO-003", agent="local-llm", wo_type="GITOPS", deliverable="VERSION.md",
            criteria=["Release documented"], title="GitOps Release",
        )


def complete_delivery(ws: Path) -> None:
    full_plan(ws)
    write_file(ws, "src/app.py", "def register(username):\n    return bool(username)\n")
    write_file(ws, "tests/test_app.py", "def test_register():\n    assert True\n")
    write_file(ws, "VERSION.md", "1.0.0\n")
    write_file(ws, "README.md", "# Task App\n\n## Startup\npython app.py\n")
    write_file(ws, "CHANGELOG.md", "# Changelog\n\n- initial release\n")
    write_contract(ws, "WO-001")
    write_qa(ws, "WO-001")


def patch_integration(monkeypatch, *, passed: bool = True, message: str = "integration evidence complete") -> None:
    def fake(self, state=None, integration_op=None, extra_telemetry=None):
        check = IntegrationCheck(
            name="deterministic",
            passed=passed,
            status="passed" if passed else "failed",
            message=message,
        )
        return IntegrationGateDecision(
            passed=passed,
            issues=() if passed else (message,),
            checks=(check,),
        )

    monkeypatch.setattr(di.IntegrationGate, "evaluate", fake)


def failing_names(decision: di.DeliveryDecision) -> set[str]:
    return {check.name for check in decision.checks if not check.passed}


# ─────────────────────────────────────────────────────────────────────────────
# Planning (scenarios 1–7)
# ─────────────────────────────────────────────────────────────────────────────

def test_01_simple_application_request_produces_executable_plan(tmp_path: Path) -> None:
    """Scenario 1: a simple app request yields an executable, owned plan."""
    full_plan(tmp_path)

    decision = di.evaluate_planning_completeness(tmp_path)

    assert decision.passed, decision.issues
    assert decision.evidence["executable_work_orders"]


def test_02_multi_component_request_is_decomposed_with_integration_owner(tmp_path: Path) -> None:
    """Scenario 2: multi-component work is decomposed and integration is owned."""
    full_plan(tmp_path)
    write_wo(tmp_path, "WO-004", deliverable="web/index.html", criteria=["User can register"])

    decision = di.evaluate_planning_completeness(tmp_path)

    assert decision.passed, decision.issues
    assert decision.evidence["integration_work_orders"]


def test_03_every_acceptance_criterion_has_implementation_ownership(tmp_path: Path) -> None:
    """Scenario 3: an unmapped acceptance criterion fails planning."""
    full_plan(tmp_path)
    write_plan(tmp_path, ["User can register", "User can delete tasks"])

    decision = di.evaluate_planning_completeness(tmp_path)

    assert not decision.passed
    assert "criteria_implementation_ownership" in failing_names(decision)


def test_04_missing_integration_ownership_is_rejected(tmp_path: Path) -> None:
    """Scenario 4: multi-component work without an integration/release owner fails."""
    full_plan(tmp_path, integration=False)
    write_wo(tmp_path, "WO-004", deliverable="web/index.html", criteria=["User can register"])

    decision = di.evaluate_planning_completeness(tmp_path)

    assert not decision.passed
    assert "integration_responsibility" in failing_names(decision)


def test_05_missing_qa_responsibility_is_rejected(tmp_path: Path) -> None:
    """Scenario 5: executable delivery with no QA/test responsibility fails planning."""
    full_plan(tmp_path, qa=False)

    decision = di.evaluate_planning_completeness(tmp_path)

    assert not decision.passed
    assert "qa_responsibility" in failing_names(decision)


def test_06_planning_retry_does_not_duplicate_work_orders(tmp_path: Path) -> None:
    """Scenario 6: a retried planning pass that re-publishes a WO is rejected."""
    full_plan(tmp_path)
    write_wo(tmp_path, "WO-001", status="COMPLETED", deliverable="src/app.py")

    decision = di.evaluate_planning_completeness(tmp_path)

    assert not decision.passed
    assert "no_duplicate_work_orders" in failing_names(decision)


def test_07_human_approval_blocks_execution(tmp_path: Path) -> None:
    """Scenario 7: execution requires recorded human approval, not a display label."""
    rule = next(
        r for r in di.LIFECYCLE_TRANSITIONS
        if r.source == "AWAITING_APPROVAL" and r.destination == "PLAN_APPROVED"
    )
    assert rule.authorization == "human_operator"
    assert any("human approval" in precondition for precondition in rule.preconditions)
    assert not any(r.source == "AWAITING_APPROVAL" and r.destination in ("EXECUTING", "DISPATCHING")
                   for r in di.LIFECYCLE_TRANSITIONS)


# ─────────────────────────────────────────────────────────────────────────────
# Execution (scenarios 8–15)
# ─────────────────────────────────────────────────────────────────────────────

def _state(ws: Path, **kwargs) -> RunState:
    state = RunState(
        run_id="run-sdp-009",
        product_goal="Task management application",
        workspace=str(ws),
        session_id="sess-sdp-009",
        phase=Phase.EXECUTING,
    )
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def test_08_approved_work_orders_are_dispatch_eligible(tmp_path: Path) -> None:
    """Scenario 8: a contracted, non-terminal work order reaches dispatch evaluation."""
    write_wo(tmp_path, "WO-001")
    write_contract(tmp_path, "WO-001")
    state = _state(tmp_path, worker_wo_ids=["WO-001"], wo_dependencies={})

    decision = evaluate_work_order_eligibility(state, "WO-001", tmp_path)

    assert decision.contract_status == "VALID"
    assert decision.status not in {"MISSING_CONTRACT", "BLOCKED", "AWAITING_APPROVAL", "COMPLETED"}


def test_09_unapproved_work_orders_cannot_dispatch(tmp_path: Path) -> None:
    """Scenario 9: an active human-approval wait blocks the work order."""
    write_wo(tmp_path, "WO-001")
    write_contract(tmp_path, "WO-001")
    state = _state(
        tmp_path,
        worker_wo_ids=["WO-001"],
        wait_registry={
            "wait-WO-001": {
                "subject_entity": "WO-001",
                "status": WaitStatus.ACTIVE.value,
                "wait_type": WaitType.HUMAN_APPROVAL.value,
            }
        },
    )

    decision = evaluate_work_order_eligibility(state, "WO-001", tmp_path)

    assert decision.is_eligible is False
    assert decision.status == "AWAITING_APPROVAL"


def test_10_blocked_work_does_not_prevent_independent_eligible_work(tmp_path: Path) -> None:
    """Scenario 10: a blocked WO does not block a different eligible WO."""
    write_wo(tmp_path, "WO-001")
    write_wo(tmp_path, "WO-002", deliverable="src/other.py")
    write_contract(tmp_path, "WO-001")
    write_contract(tmp_path, "WO-002")
    state = _state(
        tmp_path,
        worker_wo_ids=["WO-001", "WO-002"],
        blocked_wo_ids=["WO-001"],
        wo_dependencies={},
    )

    blocked = evaluate_work_order_eligibility(state, "WO-001", tmp_path)
    eligible = evaluate_work_order_eligibility(state, "WO-002", tmp_path)

    assert blocked.status == "BLOCKED"
    assert eligible.contract_status == "VALID"
    assert eligible.status not in {"BLOCKED", "MISSING_CONTRACT", "COMPLETED"}


def test_11_workers_remain_within_contract_scope(tmp_path: Path) -> None:
    """Scenario 11: a worker cannot modify files outside its contract scope."""
    task = HarnessTask(
        kind="work_order", identifier="WO-001", path=tmp_path / "WO-001.yaml",
        title="Backend", body="", query="", work_order_id="WO-001",
    )
    decision = HarnessDecision(
        status="completed", summary="done", report_markdown="done",
        blockers=(), modified_files=("src/other.py",), release_target=None,
        retrieval_queries=(), uncertainty=(),
    )
    contract = MagicMock()
    contract.work_order = "WO-001"
    contract.agent_id = "codex"
    contract.write_mode = "read-write"
    contract.budget = {"max_files_touched": 5}
    contract.allow_rules = ({"module": "src/app.py"},)
    contract.deny_rules = ()
    contract.data = {"identity": {"role": "backend"}}
    contract.is_expired.return_value = False
    contract.is_node_in_scope.return_value = False

    with pytest.raises(ContractAccessDenied):
        verify_post_execution(
            project_path=tmp_path, agent="codex", task=task, decision=decision,
            observed_files=("src/other.py",), contract=contract,
        )


def test_12_actual_artifacts_are_produced_and_verified(tmp_path: Path) -> None:
    """Scenario 12: real, meaningful deliverables pass independent verification."""
    full_plan(tmp_path)
    write_file(tmp_path, "src/app.py", "def register(username):\n    return bool(username)\n")
    write_file(tmp_path, "tests/test_app.py", "def test_register():\n    assert True\n")
    write_file(tmp_path, "VERSION.md", "1.0.0\n")

    decision = di.evaluate_artifact_integrity(tmp_path, ["WO-001"])

    assert decision.passed, decision.issues


def test_13_noop_implementation_is_rejected(tmp_path: Path) -> None:
    """Scenario 13: an empty or placeholder deliverable is a failed implementation."""
    full_plan(tmp_path)
    write_file(tmp_path, "src/app.py", "")
    write_file(tmp_path, "tests/test_app.py", "# TODO: implement tests\n")
    write_file(tmp_path, "VERSION.md", "1.0.0\n")

    artifact = di.evaluate_artifact_integrity(tmp_path, ["WO-001"])
    placeholder = di.evaluate_artifact_integrity(tmp_path, ["WO-002"])

    assert not artifact.passed
    assert not placeholder.passed


def test_14_worker_completion_requires_artifact_evidence(tmp_path: Path) -> None:
    """Scenario 14: a completion record whose hash no longer matches is stale."""
    full_plan(tmp_path)
    write_file(tmp_path, "src/app.py", "def register(username):\n    return bool(username)\n")
    wo = yaml.safe_load((tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").read_text())
    wo["completion"] = {
        "work_order_id": "WO-001",
        "deliverable_hashes": {"src/app.py": "0" * 64},
    }
    (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").write_text(
        yaml.safe_dump(wo), encoding="utf-8"
    )

    decision = di.evaluate_artifact_integrity(tmp_path, ["WO-001"])

    assert not decision.passed
    assert any("stale" in issue for issue in decision.issues)


def test_15_interrupted_work_recovers_without_losing_completed_state(tmp_path: Path) -> None:
    """Scenario 15: a serialized run reconstructs completed work and attempt accounting."""
    state = _state(tmp_path, completed_wo_ids=["WO-001"], worker_wo_ids=["WO-001", "WO-002"])
    state.attempt_counts = {"WO-001": 2}
    state.retry_counts = {"WO-001": 1}
    state.ignored_operation_ids = ["op-old"]

    restored = RunState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored.completed_wo_ids == ["WO-001"]
    assert restored.attempt_counts == {"WO-001": 2}
    assert restored.retry_counts == {"WO-001": 1}
    assert restored.ignored_operation_ids == ["op-old"]


# ─────────────────────────────────────────────────────────────────────────────
# QA (scenarios 16–21)
# ─────────────────────────────────────────────────────────────────────────────

def test_16_required_tests_execute_and_are_recorded(tmp_path: Path) -> None:
    """Scenario 16: QA PASS carries executed command evidence."""
    complete_delivery(tmp_path)

    status, reason = di.qa_status(tmp_path, "WO-001")
    verdict = review_gate.load_qa_verdict(tmp_path, "WO-001")

    assert status == "PASSED", reason
    assert verdict["evidence"][0]["command"].startswith("pytest")
    assert verdict["evidence"][0]["exit_code"] == 0


def test_17_test_evidence_is_bound_to_the_tested_revision(tmp_path: Path) -> None:
    """Scenario 17: verdict subject hash equals the current artifact revision."""
    complete_delivery(tmp_path)
    verdict = review_gate.load_qa_verdict(tmp_path, "WO-001")

    assert verdict["subject_hash"] == review_gate.subject_hash(tmp_path, "WO-001")
    assert di.evaluate_qa_evidence(tmp_path, ["WO-001"]).passed


def test_18_failed_qa_blocks_release(tmp_path: Path) -> None:
    """Scenario 18: a FAIL verdict prevents release."""
    complete_delivery(tmp_path)
    write_qa(tmp_path, "WO-001", verdict="FAIL", outcome="FAIL")

    decision = di.evaluate_release_readiness(tmp_path)

    assert not decision.passed
    assert "qa_gate" in failing_names(decision)


def test_19_source_changes_invalidate_stale_qa_evidence(tmp_path: Path) -> None:
    """Scenario 19: modifying the tested source invalidates the QA verdict."""
    complete_delivery(tmp_path)
    write_file(tmp_path, "src/app.py", "def register(username):\n    return username is not None\n")

    status, reason = di.qa_status(tmp_path, "WO-001")

    assert status == "FAILED"
    assert "stale" in reason.lower()


def test_20_remediation_triggers_retesting_with_fresh_evidence(tmp_path: Path) -> None:
    """Scenario 20: after remediation a new revision-bound PASS is accepted."""
    complete_delivery(tmp_path)
    write_qa(tmp_path, "WO-001", verdict="FAIL", outcome="FAIL")
    write_file(tmp_path, "src/app.py", "def register(username):\n    return bool(username)\n")
    write_qa(tmp_path, "WO-001", verdict="PASS", outcome="PASS")

    assert di.qa_status(tmp_path, "WO-001")[0] == "PASSED"
    assert di.evaluate_qa_evidence(tmp_path, ["WO-001"]).passed


def test_21_worker_self_reported_qa_cannot_bypass_verification(tmp_path: Path) -> None:
    """Scenario 21: a success report without a persisted verdict is NOT_TESTED."""
    full_plan(tmp_path)
    write_file(tmp_path, "src/app.py", "def register(username):\n    return bool(username)\n")
    write_file(tmp_path, "tests/test_app.py", "def test_register():\n    assert True\n")
    write_file(tmp_path, ".sync/reports/worker-qa-report.md", "QA: ALL TESTS PASSED\n")

    status, reason = di.qa_status(tmp_path, "WO-001")

    assert status == "NOT_TESTED"
    assert "no QA verdict" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Integration (scenarios 22–26)
# ─────────────────────────────────────────────────────────────────────────────

def _integration_fixture(ws: Path, *, frontend_call: str, backend_route: str) -> None:
    write_wo(ws, "WO-010", deliverable="web/app.js", criteria=["UI calls the API"])
    write_wo(ws, "WO-011", deliverable="api/server.py", criteria=["API serves the UI"])
    write_file(ws, "web/app.js", f"fetch('{frontend_call}').then(r => r.json());\n")
    write_file(ws, "api/server.py", f"@app.get('{backend_route}')\ndef handler():\n    return {{}}\n")


def test_22_compatible_work_orders_integrate_successfully(tmp_path: Path, monkeypatch) -> None:
    """Scenario 22: matching frontend calls and backend routes integrate."""
    _integration_fixture(tmp_path, frontend_call="/api/users", backend_route="/api/users")
    patch_integration(monkeypatch)

    decision = di.evaluate_integration(tmp_path)

    assert decision.passed, decision.issues


def test_23_incompatible_interfaces_are_detected(tmp_path: Path, monkeypatch) -> None:
    """Scenario 23: a frontend call with no backend route fails integration."""
    _integration_fixture(tmp_path, frontend_call="/api/tasks", backend_route="/api/users")
    patch_integration(monkeypatch)

    decision = di.evaluate_integration(tmp_path)

    assert not decision.passed
    assert "integration:interface_contracts" in failing_names(decision)


def test_24_missing_dependencies_are_detected(tmp_path: Path, monkeypatch) -> None:
    """Scenario 24: a declared but absent dependency manifest fails integration."""
    write_wo(tmp_path, "WO-010", deliverable="package.json", criteria=["Dependencies declared"])
    patch_integration(monkeypatch)

    decision = di.evaluate_integration(tmp_path)

    assert not decision.passed
    names = failing_names(decision)
    assert "integration:dependency_manifests" in names or "integration:combined_artifacts_present" in names


def test_25_integration_evidence_becomes_stale_after_modification(tmp_path: Path, monkeypatch) -> None:
    """Scenario 25: changing an artifact invalidates completion-bound evidence."""
    full_plan(tmp_path)
    write_file(tmp_path, "src/app.py", "def register(username):\n    return bool(username)\n")
    write_file(tmp_path, "tests/test_app.py", "def test_register():\n    assert True\n")
    write_file(tmp_path, "VERSION.md", "1.0.0\n")
    wo = yaml.safe_load((tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").read_text())
    wo["completion"] = {"deliverable_hashes": {"src/app.py": di.sha256_file(tmp_path / "src/app.py")}}
    (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").write_text(yaml.safe_dump(wo), encoding="utf-8")
    write_file(tmp_path, "src/app.py", "def register(username):\n    return username.strip() != ''\n")
    patch_integration(monkeypatch)

    decision = di.evaluate_artifact_integrity(tmp_path, ["WO-001"])

    assert not decision.passed
    assert any("stale" in issue for issue in decision.issues)


def test_26_failed_integration_blocks_release(tmp_path: Path, monkeypatch) -> None:
    """Scenario 26: failed integration evidence blocks release readiness."""
    complete_delivery(tmp_path)
    patch_integration(monkeypatch, passed=False, message="route contract mismatch")

    decision = di.evaluate_release_readiness(tmp_path)

    assert not decision.passed
    assert "integration_gate" in failing_names(decision)


# ─────────────────────────────────────────────────────────────────────────────
# Release (scenarios 27–32)
# ─────────────────────────────────────────────────────────────────────────────

def test_27_fully_verified_product_becomes_release_ready(tmp_path: Path, monkeypatch) -> None:
    """Scenario 27: complete planning, artifacts, QA and integration pass release."""
    complete_delivery(tmp_path)
    patch_integration(monkeypatch)

    decision = di.assert_release_ready(tmp_path)

    assert decision.passed
    assert decision.evidence["evidence_sha256"]


def test_28_missing_acceptance_coverage_blocks_release(tmp_path: Path, monkeypatch) -> None:
    """Scenario 28: an unowned mandatory criterion blocks release."""
    complete_delivery(tmp_path)
    write_plan(tmp_path, ["User can register", "User can delete tasks"])
    patch_integration(monkeypatch)

    decision = di.evaluate_release_readiness(tmp_path)

    assert not decision.passed
    assert "planning_gate" in failing_names(decision)


def test_29_missing_qa_evidence_blocks_release(tmp_path: Path, monkeypatch) -> None:
    """Scenario 29: absent QA evidence blocks release."""
    complete_delivery(tmp_path)
    (tmp_path / ".sync" / "reviews" / "qa" / "WO-001.json").unlink()
    patch_integration(monkeypatch)

    assert not di.evaluate_release_readiness(tmp_path).passed


def test_30_missing_integration_evidence_blocks_release(tmp_path: Path, monkeypatch) -> None:
    """Scenario 30: failed/missing integration evidence blocks release."""
    complete_delivery(tmp_path)
    patch_integration(monkeypatch, passed=False, message="no combined-product evidence")

    assert not di.evaluate_release_readiness(tmp_path).passed


def test_31_stale_artifact_evidence_blocks_release(tmp_path: Path, monkeypatch) -> None:
    """Scenario 31: stale artifact hashes block release."""
    complete_delivery(tmp_path)
    wo = yaml.safe_load((tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").read_text())
    wo["completion"] = {"deliverable_hashes": {"src/app.py": "f" * 64}}
    (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").write_text(yaml.safe_dump(wo), encoding="utf-8")
    patch_integration(monkeypatch)

    decision = di.evaluate_release_readiness(tmp_path)

    assert not decision.passed
    assert "artifacts_gate" in failing_names(decision)


def test_32_governance_violations_block_release(tmp_path: Path, monkeypatch) -> None:
    """Scenario 32: a completed implementation without a contract blocks release."""
    complete_delivery(tmp_path)
    (tmp_path / ".sync" / "contracts" / "WO-001.yaml").unlink()
    patch_integration(monkeypatch)

    decision = di.evaluate_release_readiness(tmp_path)

    assert not decision.passed
    assert "release:contract_identity" in failing_names(decision)


# ─────────────────────────────────────────────────────────────────────────────
# Recovery (scenarios 33–40)
# ─────────────────────────────────────────────────────────────────────────────

def test_33_tui_restart_preserves_the_original_run(tmp_path: Path) -> None:
    """Scenario 33: run identity, phase and work-order evidence survive restart."""
    state = _state(tmp_path, completed_wo_ids=["WO-001"], worker_wo_ids=["WO-001", "WO-002"])
    state.phase = Phase.INTEGRATION_REVIEW
    payload = json.loads(json.dumps(state.to_dict()))

    restored = RunState.from_dict(payload)

    assert restored.run_id == state.run_id
    assert restored.session_id == state.session_id
    assert restored.phase == Phase.INTEGRATION_REVIEW
    assert restored.completed_wo_ids == ["WO-001"]


def test_34_daemon_restart_reconstructs_authoritative_operation_state(tmp_path: Path) -> None:
    """Scenario 34: persisted lifecycle evidence reconstructs authoritative state."""
    state = _state(tmp_path)
    state.integration_evidence = {"passed": True, "evidence_sha256": "abc123"}
    state.ignored_operation_ids = ["op-crashed"]
    state.transitions = [{"phase": "EXECUTING", "at": "t0"}]

    restored = RunState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored.integration_evidence == state.integration_evidence
    assert restored.ignored_operation_ids == ["op-crashed"]
    assert restored.transitions == state.transitions


def test_35_planning_recovery_does_not_duplicate_work_orders(tmp_path: Path) -> None:
    """Scenario 35: a recovered planning pass must not republish an existing WO."""
    full_plan(tmp_path)
    write_wo(tmp_path, "WO-001", status="COMPLETED", deliverable="src/app.py")

    assert di.duplicate_work_order_ids(tmp_path) == {"WO-001": ["ACTIVE/WO-001.yaml", "COMPLETED/WO-001.yaml"]}
    assert not di.evaluate_planning_completeness(tmp_path).passed


def test_36_worker_recovery_does_not_redispatch_completed_work(tmp_path: Path) -> None:
    """Scenario 36: a completed work order is not dispatch-eligible again."""
    write_wo(tmp_path, "WO-001", status="COMPLETED")
    state = _state(tmp_path, worker_wo_ids=["WO-001"], completed_wo_ids=["WO-001"])

    decision = evaluate_work_order_eligibility(state, "WO-001", tmp_path)

    assert decision.is_eligible is False
    assert decision.status == "COMPLETED"


def test_37_qa_recovery_preserves_valid_evidence(tmp_path: Path) -> None:
    """Scenario 37: persisted QA evidence remains valid across a reload."""
    complete_delivery(tmp_path)
    reloaded = review_gate.load_qa_verdict(tmp_path, "WO-001")

    assert review_gate.validate_qa_verdict(reloaded, tmp_path, "WO-001")[0] is True
    assert di.qa_status(tmp_path, "WO-001")[0] == "PASSED"


def test_38_integration_recovery_revalidates_changed_artifacts(tmp_path: Path) -> None:
    """Scenario 38: changed artifacts invalidate completion-bound hashes."""
    full_plan(tmp_path)
    write_file(tmp_path, "src/app.py", "def register(username):\n    return bool(username)\n")
    wo = yaml.safe_load((tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").read_text())
    wo["completion"] = {"deliverable_hashes": {"src/app.py": di.sha256_file(tmp_path / "src/app.py")}}
    (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").write_text(yaml.safe_dump(wo), encoding="utf-8")
    write_file(tmp_path, "src/app.py", "def register(username):\n    return True\n")

    decision = di.evaluate_artifact_integrity(tmp_path, ["WO-001"])

    assert not decision.passed


def test_39_unknown_cancellation_outcomes_prevent_unsafe_dispatch(tmp_path: Path) -> None:
    """Scenario 39: an unresolved cancellation outcome blocks duplicate dispatch."""
    state = AutonomousDeliveryState()
    state.operations["op-009"] = OperationNode("op-009", "Turn", status="RUNNING")
    state.cancel_outcomes["op-009"] = CancellationOutcome(
        operation_id="op-009",
        outcome=CANCEL_OUTCOME_UNKNOWN,
        authoritative_status="RUNNING",
    ).to_dict()

    assert unresolved_cancellation_operations(state) == ["op-009"]


def test_40_terminal_state_cannot_be_falsely_revived(tmp_path: Path) -> None:
    """Scenario 40: COMPLETE has no implicit outgoing transition."""
    outgoing = [rule for rule in di.LIFECYCLE_TRANSITIONS if rule.source == "COMPLETE"]
    assert outgoing == []
    rule = next(r for r in di.LIFECYCLE_TRANSITIONS if r.destination == "COMPLETE")
    assert "final_verdict" in rule.evidence
    assert "release_readiness" in rule.evidence


# ─────────────────────────────────────────────────────────────────────────────
# Additional defects discovered during this milestone
# ─────────────────────────────────────────────────────────────────────────────

def test_41_dependency_cycle_is_rejected_by_planning(tmp_path: Path) -> None:
    """A cyclic work-order graph is not an executable plan."""
    full_plan(tmp_path)
    write_wo(tmp_path, "WO-001", deliverable="src/app.py", criteria=["User can register"], deps=["WO-004"])
    write_wo(tmp_path, "WO-004", deliverable="web/index.html", criteria=["User can register"], deps=["WO-001"])

    decision = di.evaluate_planning_completeness(tmp_path)

    assert not decision.passed
    assert "dependency_graph_acyclic" in failing_names(decision)


def test_42_documentation_only_plan_is_rejected_for_software_request(tmp_path: Path) -> None:
    """A docs-only plan cannot satisfy an executable product request."""
    write_plan(tmp_path)
    write_wo(tmp_path, "WO-001", deliverable="docs/guide.md", criteria=["User can register"])
    write_wo(tmp_path, "WO-002", agent="gemma", wo_type="QA", deliverable="docs/qa.md",
             criteria=["Automated tests pass"], title="Q/A Verification Suite")
    write_wo(tmp_path, "WO-003", agent="local-llm", wo_type="GITOPS", deliverable="VERSION.md",
             criteria=["Release documented"], title="GitOps Release")

    decision = di.evaluate_planning_completeness(tmp_path)

    assert not decision.passed
    assert "executable_plan" in failing_names(decision)


def test_43_release_readiness_is_deterministic_and_revision_bound(tmp_path: Path, monkeypatch) -> None:
    """Repeated evaluations produce auditable, stable evidence fingerprints."""
    complete_delivery(tmp_path)
    patch_integration(monkeypatch)

    first = di.evaluate_release_readiness(tmp_path)
    second = di.evaluate_release_readiness(tmp_path)

    assert first.passed and second.passed
    assert first.evidence["evidence_sha256"] == second.evidence["evidence_sha256"]


def test_44_lifecycle_matrix_covers_every_stage_with_evidence(tmp_path: Path) -> None:
    """The transition model documents preconditions, evidence and recovery."""
    matrix = di.lifecycle_transition_matrix()
    destinations = {row["destination"] for row in matrix}

    for stage in ("PLANNING", "AWAITING_APPROVAL", "EXECUTING", "QA_REVIEW",
                  "INTEGRATION_REVIEW", "PRODUCT_READY", "GITOPS", "COMPLETE"):
        assert stage in destinations
    for row in matrix:
        assert row["preconditions"] and row["evidence"]
        assert row["failure_behavior"] and row["recovery_behavior"]
        assert row["audit_event"]


def test_45_blocked_work_order_resolution_is_required_before_release(tmp_path: Path, monkeypatch) -> None:
    """Open blockers in authoritative state block release readiness."""
    complete_delivery(tmp_path)
    patch_integration(monkeypatch)
    state = _state(tmp_path, blocked_wo_ids=["WO-001"])

    decision = di.evaluate_release_readiness(tmp_path, state)

    assert not decision.passed
    assert "release:blockers" in failing_names(decision)
