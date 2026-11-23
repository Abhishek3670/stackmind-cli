"""Tests for Architecture Review hardening:
- Breaking FINAL_REVIEW prerequisite loops cleanly at WAITING_FOR_HUMAN
- Preserving valid QA verdicts during retry_unchanged for already-completed work orders
- Whitelisting GitOps release documentation from stale evidence false-positives
- Dependent pruning/rewiring on work order cancellation
- Plan deviation escalation from FINAL_REVIEW to ARCHITECT_RECOVERY_DECISION
"""

import pytest
import yaml
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_lifecycle_supervisor import MockSessionManager
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.harness import review_gate
from validators.harness.delivery_integrity import (
    evaluate_artifact_integrity,
    evaluate_release_readiness,
)


def _make_state(tmp_path: Path, worker_wo_ids: list[str]) -> tuple[LifecycleSupervisor, MockSessionManager, RunState]:
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-test-arch", "Build test app", tmp_path, "sess-test-arch")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = list(worker_wo_ids)
    state.gitops_wo_id = "WO-005"
    return supervisor, mock_mgr, state


def test_final_review_prerequisite_exhaustion_halts_at_waiting_for_human(tmp_path: Path) -> None:
    """When prerequisites are unmet and rounds are exhausted, FINAL_REVIEW must transition
    cleanly to WAITING_FOR_HUMAN (not bounce through BLOCKED to ARCHITECT_RECOVERY)."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.FINAL_REVIEW
    state.final_prerequisite_rounds = state.max_final_review_rounds + 5  # exhausted

    # PLAN.md exists
    (tmp_path / ".sync" / "playbook").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sync" / "playbook" / "PLAN.md").write_text("# Plan\n\n- Task 1", encoding="utf-8")

    res = supervisor._advance_final_review(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert "FINAL_ACCEPTANCE_BLOCKED" in state.error


def test_recovery_retry_unchanged_preserves_valid_qa_verdict(tmp_path: Path) -> None:
    """When an already-completed work order is cleared via retry_unchanged, its valid QA verdict
    must NOT be deleted/archived, preserving acceptance evidence for FINAL_REVIEW."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    wo_data = {
        "id": "WO-001",
        "status": "COMPLETED",
        "title": "Backend API",
        "assigned_agents": ["codex"],
        "completion": {"verified_at_revision": 1, "deliverable_hashes": {"app.py": "abc"}},
        "deliverable": {"type": "code", "path": "app.py"},
    }
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")
    (tmp_path / "app.py").write_text("print('hello')", encoding="utf-8")

    reviews = tmp_path / ".sync" / "reviews" / "qa"
    reviews.mkdir(parents=True, exist_ok=True)
    verdict_path = reviews / "WO-001.json"
    verdict_path.write_text(
        '{"schema": "stackmind.qa_verdict.v1", "work_order_id": "WO-001", "verdict": "PASS", "criteria": [{"name": "tests", "passed": true}]}',
        encoding="utf-8",
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.completed_wo_ids = ["WO-001"]

    # Clear worker blocker with redispatch=False (unchanged completed recovery)
    supervisor._clear_worker_blocker(state, tmp_path, "WO-001", redispatch=False)

    # The QA verdict must STILL be present
    assert verdict_path.is_file()
    assert "WO-001" in state.completed_wo_ids


def test_work_order_cancellation_prunes_active_dependencies(tmp_path: Path) -> None:
    """When a work order is cancelled in recovery, active work orders depending on it
    must have the cancelled dependency removed or rewired to prevent DEPENDENCY_WAIT_INVALIDATED."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({"id": "WO-001", "status": "ACTIVE", "dependencies": []}),
        encoding="utf-8",
    )
    (wo_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({"id": "WO-002", "status": "ACTIVE", "dependencies": ["WO-001"]}),
        encoding="utf-8",
    )

    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001", "WO-002"])
    state.wo_dependencies["WO-002"] = ["WO-001"]

    # Cancel WO-001 through recovery decision application
    record = {
        "work_order": "WO-001",
        "action": "cancel_work_order",
        "reason": "Superseded duplicate",
    }
    supervisor._apply_recovery_decision(state, tmp_path, "WO-001", record)

    # Check WO-002 dependencies on disk
    wo2_data = yaml.safe_load((wo_dir / "WO-002.yaml").read_text(encoding="utf-8"))
    assert "WO-001" not in wo2_data["dependencies"]
    assert "WO-001" not in state.wo_dependencies.get("WO-002", [])


def test_gitops_documentation_not_flagged_as_stale_artifact(tmp_path: Path) -> None:
    """Release documentation (VERSION.md, CHANGELOG.md) updated at release boundary must not
    be flagged as stale evidence by evaluate_artifact_integrity."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-005.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-005",
            "type": "GITOPS",
            "status": "COMPLETED",
            "deliverable": {"type": "doc", "path": "VERSION.md"},
            "completion": {
                "deliverable_hashes": {"VERSION.md": "old_hash_123"},
            },
        }),
        encoding="utf-8",
    )
    # Write new VERSION.md
    (tmp_path / "VERSION.md").write_text("# Version\n\n1.0.0\n", encoding="utf-8")

    decision = evaluate_artifact_integrity(tmp_path, ["WO-005"])
    assert decision.passed is True
    assert not any("stale" in issue.lower() for issue in decision.issues)


def test_final_review_plan_deviation_escalates_to_architect_recovery(tmp_path: Path) -> None:
    """When final review discovers findings that cannot be mapped to any existing work order,
    it must escalate to ARCHITECT_RECOVERY_DECISION (PLAN_DEVIATION) instead of dead-ending."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.FINAL_REVIEW
    state.final_review_rounds = 0
    state.max_final_review_rounds = 3
    state.global_turn_budget = 20

    verdict = {
        "status": "REJECTED",
        "findings": ["Plan requires OAuth login but no work order was authored for OAuth"],
        "required_actions": ["Decompose OAuth work order"],
    }

    res = supervisor._route_final_review_failure(
        state, tmp_path, verdict, "Plan deviation: missing OAuth component"
    )

    assert res in (AdvanceResult.TRANSITIONED, AdvanceResult.WAITING_FOR_OPERATION)
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.recovery_wo_id.startswith("PLAN_DEV")
    assert "FINAL_REVIEW_PLAN_DEVIATION" in state.error


def test_authoring_repair_exhaustion_halts_at_waiting_for_human(tmp_path: Path) -> None:
    """When authoring repairs exceed max_authoring_repairs, supervisor halts at WAITING_FOR_HUMAN with exit_code=3."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.ARCHITECT_REPAIR
    state.max_authoring_repairs = 2
    state.authoring_repair_attempts = 3  # exceeded

    res = supervisor._advance_architect_repair(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 3
    assert "exhausted" in (state.error or "").lower()


def test_recovery_attempts_exhaustion_halts_at_waiting_for_human(tmp_path: Path) -> None:
    """When recovery attempts for a blocked work order exceed max_retries, it halts at WAITING_FOR_HUMAN with exit_code=3."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.ARCHITECT_RECOVERY_DECISION
    state.recovery_wo_id = "WO-001"
    state.max_retries = 2
    state.recovery_attempts["WO-001"] = 2  # at max limit

    res = supervisor._advance_recovery_decision(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 3
    assert "exhausted" in (state.error or "").lower()


def test_integration_plan_deviation_escalates_to_architect_recovery(tmp_path: Path) -> None:
    """When integration review detects plan deviation / missing work orders, it routes to ARCHITECT_RECOVERY_DECISION."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.INTEGRATION_REVIEW
    state.integration_wo_id = "WO-020"
    state.completed_wo_ids = ["WO-001"]
    state.max_retries = 3
    state.global_turn_budget = 20

    # Mock completed review op with plan deviation blocker
    op = mock_mgr.start_turn("sess-test-arch", "Run review", work_order_id="WO-020")
    state.integration_operation_id = op["operation_id"]
    mock_mgr.complete_operation(
        op["operation_id"],
        status="COMPLETED",
        result={
            "status": "failed",
            "findings": ["Plan requires database migrations but no work order was authored for migrations"],
        },
    )

    res = supervisor._advance_integration_review(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.recovery_wo_id.startswith("PLAN_DEV")


def test_qa_infra_error_preserves_rework_budget(tmp_path: Path) -> None:
    """QA harness/environment errors (ERROR, BLOCKED, FLAKY) must NOT increment developer qa_reworks budget."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.EXECUTING

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": "main.py"},
    }), encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")

    op = mock_mgr.start_turn(
        state.session_id,
        "Run WO-001",
        work_order_id="WO-001",
        agent_id="codex",
    )
    mock_mgr.complete_operation(op["operation_id"], status="COMPLETED")

    # Write a QA verdict with ERROR (test runner crashed / infra failure)
    reviews_dir = tmp_path / ".sync" / "reviews" / "qa"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "WO-001.json").write_text(yaml.safe_dump({
        "schema": "stackmind.qa-verdict.v1",
        "work_order_id": "WO-001",
        "verdict": "ERROR",
        "reason": "Vitest crashed: out of memory (exit code 137)",
    }), encoding="utf-8")

    # Check verdict recognized as INFRA_ERROR
    verdict = supervisor._check_qa_verdict("WO-001", tmp_path)
    assert verdict == "INFRA_ERROR"

    # Advance executing must not increment qa_reworks
    supervisor._advance_executing(state)
    assert state.qa_reworks.get("WO-001", 0) == 0


def test_retry_unchanged_rejects_modified_deliverable(tmp_path: Path) -> None:
    """retry_unchanged must reject when the deliverable on disk does not match the evidence subject_hash."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001", "assigned_agents": ["codex"], "deliverable": {"type": "code", "path": "main.py"}
    }), encoding="utf-8")
    (tmp_path / "main.py").write_text("print('modified')", encoding="utf-8")

    record = {
        "work_order": "WO-001",
        "action": "retry_unchanged",
        "transient": True,
        "failure_code": "WORKER_BLOCKED",
    }
    # Provide a mismatched subject hash in evidence
    evidence = {
        "failure_code": "WORKER_BLOCKED",
        "subject_hash": "different_older_hash_12345",
    }
    state.worker_blockers["WO-001"] = evidence

    advance, err = supervisor._apply_recovery_decision(state, tmp_path, "WO-001", record)
    assert advance is None
    assert "unchanged deliverable subject hash" in (err or "").lower()


def test_breaker_exhaustion_and_resume_progress_matrix(tmp_path: Path) -> None:
    """Verify that every breaker halts at WAITING_FOR_HUMAN and resume_run resets the counter,
    returning to the interrupted phase and making forward progress without immediately re-tripping."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.max_human_resumes = 10

    # 1. Recovery attempts breaker
    state.phase = Phase.ARCHITECT_RECOVERY_DECISION
    state.interrupted_phase = Phase.ARCHITECT_RECOVERY_DECISION.value
    state.recovery_wo_id = "WO-001"
    state.max_retries = 2
    state.recovery_attempts["WO-001"] = 2
    res = supervisor._advance_recovery_decision(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 3

    # Human resume clears recovery_attempts and resumes to ARCHITECT_RECOVERY_DECISION
    resume_res = supervisor.resume_run(state, reason="Operator approved recovery re-attempt")
    assert state.recovery_attempts.get("WO-001", 0) == 0
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.exit_code == 0

    # 2. Integration rework breaker
    state.phase = Phase.INTEGRATION_REVIEW
    state.interrupted_phase = Phase.INTEGRATION_REVIEW.value
    state.integration_rework_rounds = 5
    state.max_retries = 2
    # Resume resets integration_rework_rounds and resumes to INTEGRATION_REVIEW
    supervisor.resume_run(state, reason="Reset integration rounds")
    assert state.integration_rework_rounds == 0
    assert state.phase == Phase.INTEGRATION_REVIEW

    # 3. GitOps retries breaker
    state.phase = Phase.GITOPS
    state.interrupted_phase = Phase.GITOPS.value
    state.gitops_retries = 5
    state.max_gitops_retries = 2
    supervisor.resume_run(state, reason="Reset gitops retries")
    assert state.gitops_retries == 0
    assert state.phase == Phase.GITOPS


def test_amend_plan_routes_to_awaiting_approval(tmp_path: Path) -> None:
    """When the Architect amends the plan in recovery, it MUST route to AWAITING_APPROVAL for operator approval."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.ARCHITECT_REPAIR
    state.repair_context = {
        "source": "recovery",
        "action": "amend_plan",
        "work_order": "WO-001",
    }
    # Mock completed repair turn
    op = mock_mgr.start_turn("sess-test-arch", "Amend plan", is_authoring=True, work_order_id="WO-000")
    state.architect_repair_operation_id = op["operation_id"]
    mock_mgr.complete_operation(op["operation_id"], status="COMPLETED")

    res = supervisor._advance_architect_repair(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL


def test_qa_infra_retries_capped_at_max(tmp_path: Path) -> None:
    """When QA test runner repeatedly crashes or fails with infra errors, it must halt at WAITING_FOR_HUMAN."""
    supervisor, mock_mgr, state = _make_state(tmp_path, ["WO-001"])
    state.phase = Phase.EXECUTING
    state.max_infra_retries = 2
    state.infra_retries["WO-001"] = 2  # at max limit

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": "main.py"},
    }), encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")

    op = mock_mgr.start_turn(
        state.session_id,
        "Run WO-001",
        work_order_id="WO-001",
        agent_id="codex",
    )
    mock_mgr.complete_operation(op["operation_id"], status="COMPLETED")

    reviews_dir = tmp_path / ".sync" / "reviews" / "qa"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "WO-001.json").write_text(yaml.safe_dump({
        "schema": "stackmind.qa-verdict.v1",
        "work_order_id": "WO-001",
        "verdict": "ERROR",
        "reason": "Test runner crashed with exit code 137",
    }), encoding="utf-8")

    res = supervisor._advance_executing(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 3
    assert "infrastructure" in (state.error or "").lower()


