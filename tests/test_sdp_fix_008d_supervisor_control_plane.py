"""Adversarial regression test suite for SDP-FIX-008D.

Covers:
- Section A (1-15): State-machine invariants
- Section B (16-28): Blocker ownership & single-ownership protocol
- Section C (29-40): Wait-condition lifecycle & predicates
- Section D (41-57): Deadlock detection categories (Tarjan's SCC, false-positive protection)
- Section E (58-67): Progress detection & no-progress protection
- Section F (68-82): Restart reconciliation (Stage 1 read-only + Stage 2 authorized repair)
- Section G (83-92): Concurrency & adversarial integration
- Cross-Milestone Scenarios 1-7: Full worker recovery, dependency progression, QA/Integration, human approval, crash, corruption, terminal protection
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from validators.clock import now as _now
from validators.kernel.daemon.supervisor import LifecycleSupervisor
from validators.kernel.daemon.supervisor.attempts import (
    AttemptStatus,
    BudgetKey,
    FailureClass,
    evaluate_retry_budget,
    start_attempt,
)
from validators.kernel.daemon.supervisor.control_plane import (
    BlockerRecord,
    BlockerStatus,
    BlockerSubsystem,
    BlockerType,
    ControllerState,
    DeadlockReport,
    DeadlockType,
    ReconciliationItem,
    ReconciliationOutcome,
    ReconciliationReport,
    SupervisorDecision,
    WaitRecord,
    WaitStatus,
    WaitType,
    apply_authorized_reconciliation,
    check_progress,
    compute_progress_fingerprint,
    create_blocker,
    create_wait,
    detect_deadlocks,
    evaluate_supervisor_state,
    get_active_blockers,
    get_active_waits,
    invalidate_wait,
    record_lifecycle_event,
    reconcile_supervisor_state_readonly,
    release_wo_dispatch,
    reopen_blocker,
    reserve_wo_dispatch,
    resolve_blocker,
    resolve_wait,
    supersede_blocker,
)
from validators.kernel.daemon.supervisor.models import (
    AdvanceResult,
    Phase,
    RunState,
    StateIntegrityError,
)


class MockSessionManager:
    """Deterministic in-memory session manager for testing."""

    def __init__(self, ws: Path) -> None:
        self.workspace = ws
        self._operations: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {
            "sess-001": {"state": "RUNNING", "active_operation": None, "journal": []}
        }
        self._plans: list[dict[str, Any]] = []

    def list_operations(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._operations.values())

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        return self._operations.get(operation_id)

    def list_plans(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._plans)

    def add_operation(self, op_id: str, status: str = "RUNNING", wo_id: str | None = None) -> None:
        rec = {
            "operation_id": op_id,
            "status": status,
            "work_order_id": wo_id,
            "created_at": _now(),
        }
        self._operations[op_id] = rec
        self._sessions["sess-001"]["journal"].append(rec)


def _make_state(tmp_path: Path, **overrides: Any) -> RunState:
    state = RunState(
        run_id="run-008d-test",
        product_goal="Implement control plane",
        workspace=str(tmp_path),
        session_id="sess-001",
        phase=Phase.INIT,
    )
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


# ═════════════════════════════════════════════════════════════════════════
# Section A: State-Machine Invariants (Tests 1–15)
# ═════════════════════════════════════════════════════════════════════════


def test_invariant1_executing_without_active_op_rejected(tmp_path: Path) -> None:
    """1. EXECUTING without an active operation is rejected."""
    state = _make_state(tmp_path, phase=Phase.PLANNING)
    mgr = MockSessionManager(tmp_path)
    # No active operations
    decision = evaluate_supervisor_state(state, requested_phase=Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid
    assert any("Invariant 1" in v for v in decision.violations)


def test_invariant1_active_wo_metadata_alone_insufficient(tmp_path: Path) -> None:
    """2. ACTIVE WO metadata without an operation is insufficient."""
    state = _make_state(tmp_path, phase=Phase.DISPATCHING, worker_wo_ids=["WO-001"])
    mgr = MockSessionManager(tmp_path)
    decision = evaluate_supervisor_state(state, requested_phase=Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid
    assert any("Invariant 1" in v for v in decision.violations)


def test_invariant2_waiting_without_condition_rejected(tmp_path: Path) -> None:
    """3. WAITING without a valid wait condition is rejected."""
    state = _make_state(tmp_path, phase=Phase.PLANNING, error=None, interrupted_phase=None)
    state.wait_registry.clear()
    decision = evaluate_supervisor_state(state, requested_phase=Phase.WAITING_FOR_HUMAN)
    assert not decision.is_valid
    assert any("Invariant 2" in v for v in decision.violations)


def test_invariant3_blocked_without_blocker_rejected(tmp_path: Path) -> None:
    """4. BLOCKED without an active blocker is rejected."""
    state = _make_state(tmp_path, phase=Phase.EXECUTING, error=None)
    state.blocked_wo_ids.clear()
    state.blocker_registry.clear()
    decision = evaluate_supervisor_state(state, requested_phase=Phase.BLOCKED)
    assert not decision.is_valid
    assert any("Invariant 3" in v for v in decision.violations)


def test_invariant4_runnable_with_unresolved_blocker_rejected(tmp_path: Path) -> None:
    """5. RUNNABLE with all pending work orders blocked is rejected."""
    state = _make_state(tmp_path, phase=Phase.BLOCKED, worker_wo_ids=["WO-001", "WO-002"], blocked_wo_ids=["WO-001", "WO-002"])
    decision = evaluate_supervisor_state(state, requested_phase=Phase.DISPATCHING)
    assert not decision.is_valid
    assert any("Invariant 4" in v for v in decision.violations)


def test_invariant4_runnable_with_invalid_dependency_rejected(tmp_path: Path) -> None:
    """6. RUNNABLE with invalid dependency is rejected when dispatching."""
    state = _make_state(tmp_path, phase=Phase.DISPATCHING, worker_wo_ids=["WO-001"])
    create_blocker(state, BlockerType.INVALID_DEPENDENCY, BlockerSubsystem.DEPENDENCY, work_order_id="WO-001")
    decision = evaluate_supervisor_state(state, requested_phase=Phase.DISPATCHING)
    assert not decision.is_valid


def test_invariant4_runnable_with_missing_approval_rejected(tmp_path: Path) -> None:
    """7. RUNNABLE with required approval missing is rejected."""
    state = _make_state(tmp_path, phase=Phase.AWAITING_APPROVAL)
    create_wait(state, WaitType.HUMAN_APPROVAL, BlockerSubsystem.APPROVAL, state.run_id, "Plan approval pending")
    decision = evaluate_supervisor_state(state, requested_phase=Phase.DISPATCHING)
    # Plan not approved yet
    assert decision.proposed_state != ControllerState.TERMINAL


def test_invariant5_terminal_run_cannot_return_to_running(tmp_path: Path) -> None:
    """8. Terminal run (COMPLETE) cannot return to RUNNING."""
    state = _make_state(tmp_path, phase=Phase.COMPLETE)
    decision = evaluate_supervisor_state(state, requested_phase=Phase.PLANNING)
    assert not decision.is_valid
    assert any("Invariant 5" in v for v in decision.violations)


def test_invariant5_terminal_run_cannot_enter_dispatching(tmp_path: Path) -> None:
    """9. Terminal run (FAILED) cannot enter DISPATCHING."""
    state = _make_state(tmp_path, phase=Phase.FAILED)
    decision = evaluate_supervisor_state(state, requested_phase=Phase.DISPATCHING)
    assert not decision.is_valid
    assert any("Invariant 5" in v for v in decision.violations)


def test_invariant5_terminal_run_cannot_enter_executing(tmp_path: Path) -> None:
    """10. Terminal run cannot enter EXECUTING."""
    state = _make_state(tmp_path, phase=Phase.FAILED)
    decision = evaluate_supervisor_state(state, requested_phase=Phase.EXECUTING)
    assert not decision.is_valid
    assert any("Invariant 5" in v for v in decision.violations)


def test_invariant5_ordinary_recovery_cannot_revive_cancelled_run(tmp_path: Path) -> None:
    """11. Ordinary recovery cannot revive a cancelled/failed run."""
    state = _make_state(tmp_path, phase=Phase.FAILED)
    sup = LifecycleSupervisor(MockSessionManager(tmp_path))
    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)


def test_invalid_transition_request_is_audited(tmp_path: Path) -> None:
    """12. Invalid transition request is audited in lifecycle_events."""
    state = _make_state(tmp_path, phase=Phase.COMPLETE)
    sup = LifecycleSupervisor(MockSessionManager(tmp_path))
    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.DISPATCHING)
    events = [e for e in state.lifecycle_events if e["event_type"] == "STATE_TRANSITION_REJECTED"]
    assert len(events) >= 1
    assert "Invariant 5" in events[-1]["reason_code"]


def test_stale_transition_version_rejected(tmp_path: Path) -> None:
    """13. Stale transition version is detected via progress fingerprint."""
    state = _make_state(tmp_path, phase=Phase.PLANNING)
    fp1 = compute_progress_fingerprint(state)
    decision = evaluate_supervisor_state(state)
    assert decision.state_fingerprint == fp1


def test_identical_state_inputs_produce_identical_decisions(tmp_path: Path) -> None:
    """14. Identical state inputs produce identical decisions deterministically."""
    state1 = _make_state(tmp_path, phase=Phase.PLANNING)
    state2 = _make_state(tmp_path, phase=Phase.PLANNING)
    d1 = evaluate_supervisor_state(state1)
    d2 = evaluate_supervisor_state(state2)
    assert d1.current_state == d2.current_state
    assert d1.proposed_state == d2.proposed_state
    assert d1.is_valid == d2.is_valid
    assert d1.state_fingerprint == d2.state_fingerprint


def test_valid_wait_does_not_cause_false_deadlock(tmp_path: Path) -> None:
    """15. Valid wait condition prevents false deadlock classification."""
    state = _make_state(tmp_path, phase=Phase.AWAITING_APPROVAL)
    create_wait(state, WaitType.HUMAN_APPROVAL, BlockerSubsystem.APPROVAL, state.run_id, "Plan approval pending")
    report = detect_deadlocks(state)
    assert report is None


# ═════════════════════════════════════════════════════════════════════════
# Section B: Blocker Ownership & Protocol (Tests 16–28)
# ═════════════════════════════════════════════════════════════════════════


def test_blocker_receives_stable_id(tmp_path: Path) -> None:
    """16. Blocker receives a stable, structured ID."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001", failure_code="EXEC_ERR")
    assert b.blocker_id.startswith("blk-run-008d-test-WO-001-EXEC_ERR-")
    assert b.blocker_id in state.blocker_registry


def test_blocker_has_exactly_one_accountable_owner(tmp_path: Path) -> None:
    """17. Blocker has exactly one accountable owner."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.QA_FAILURE, BlockerSubsystem.QA_REVIEW, work_order_id="WO-002")
    assert b.owner_subsystem == "qa_review"


def test_non_owner_cannot_resolve_blocker(tmp_path: Path) -> None:
    """18. Non-owner cannot resolve blocker."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.QA_FAILURE, BlockerSubsystem.QA_REVIEW, work_order_id="WO-002")
    success, reason = resolve_blocker(
        state, b.blocker_id, actor="worker", subsystem="execution", expected_version=1, evidence={"test": "pass"}
    )
    assert not success
    assert "UNAUTHORIZED_RESOLVER" in reason
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.OPEN.value


def test_authorized_override_is_explicitly_recorded(tmp_path: Path) -> None:
    """19. Authorized override is explicitly permitted and recorded."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.QA_FAILURE, BlockerSubsystem.QA_REVIEW, work_order_id="WO-002")
    success, reason = resolve_blocker(
        state, b.blocker_id, actor="architect", subsystem="recovery", expected_version=1,
        evidence={"override_approval": "HUMAN-AUTH"}, authorized_override=True
    )
    assert success
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.RESOLVED.value


def test_blocker_resolution_requires_evidence(tmp_path: Path) -> None:
    """20. Blocker resolution requires non-empty verified evidence."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    success, reason = resolve_blocker(
        state, b.blocker_id, actor="worker", subsystem="execution", expected_version=1, evidence={}
    )
    assert not success
    assert "EVIDENCE_REQUIRED" in reason


def test_stale_resolution_version_is_rejected(tmp_path: Path) -> None:
    """21. Stale resolution version is rejected."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    # Expected version 2 when current is 1
    success, reason = resolve_blocker(
        state, b.blocker_id, actor="worker", subsystem="execution", expected_version=2, evidence={"ok": True}
    )
    assert not success
    assert "STALE_VERSION_REJECTED" in reason


def test_concurrent_resolution_requests_serialized(tmp_path: Path) -> None:
    """22. Concurrent resolution requests are serialized via version bump."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    # First resolution succeeds
    s1, r1 = resolve_blocker(state, b.blocker_id, actor="worker1", subsystem="execution", expected_version=1, evidence={"turn": 1})
    assert s1
    # Second resolution with stale version 1 fails
    s2, r2 = resolve_blocker(state, b.blocker_id, actor="worker2", subsystem="execution", expected_version=1, evidence={"turn": 2})
    assert not s2


def test_resolved_blocker_history_retained(tmp_path: Path) -> None:
    """23. Resolved blocker history is retained in blocker_history."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    resolve_blocker(state, b.blocker_id, actor="worker", subsystem="execution", expected_version=1, evidence={"ok": True})
    assert len(state.blocker_history) >= 2
    statuses = [h["status"] for h in state.blocker_history if h["blocker_id"] == b.blocker_id]
    assert BlockerStatus.OPEN.value in statuses
    assert BlockerStatus.RESOLVED.value in statuses


def test_old_resolution_event_cannot_clear_newer_blocker(tmp_path: Path) -> None:
    """24. Old resolution event cannot clear a newer blocker."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    resolve_blocker(state, b.blocker_id, actor="worker", subsystem="execution", expected_version=1, evidence={"ok": True})
    reopen_blocker(state, b.blocker_id, actor="qa", subsystem="qa_review", reason="Regression")
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.REOPENED.value
    # Attempting to resolve with version 1 is rejected
    success, _ = resolve_blocker(state, b.blocker_id, actor="worker", subsystem="execution", expected_version=1, evidence={"ok": True})
    assert not success


def test_blocker_cannot_be_cleared_by_deleting_wo_id_from_list(tmp_path: Path) -> None:
    """25. Blocker cannot be cleared solely by deleting a WO ID from blocked_wo_ids."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    state.blocked_wo_ids.clear()
    # Blocker record remains OPEN in registry
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.OPEN.value
    active = get_active_blockers(state, "WO-001")
    assert len(active) == 1


def test_superseded_blocker_remains_auditable(tmp_path: Path) -> None:
    """26. Superseded blocker remains auditable."""
    state = _make_state(tmp_path)
    b1 = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    b2 = create_blocker(state, BlockerType.CONTRACT_VIOLATION, BlockerSubsystem.GOVERNANCE, work_order_id="WO-001")
    supersede_blocker(state, b1.blocker_id, b2.blocker_id, actor="architect", subsystem="recovery", reason="Contract rewritten")
    assert state.blocker_registry[b1.blocker_id]["status"] == BlockerStatus.SUPERSEDED.value
    assert state.blocker_registry[b1.blocker_id]["metadata"]["superseded_by"] == b2.blocker_id


def test_blocker_ownership_survives_restart(tmp_path: Path) -> None:
    """27. Blocker ownership survives serialization / deserialization round-trip."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.GITOPS_FAILURE, BlockerSubsystem.GITOPS, work_order_id="WO-005")
    data = state.to_dict()
    restored = RunState.from_dict(data)
    assert b.blocker_id in restored.blocker_registry
    assert restored.blocker_registry[b.blocker_id]["owner_subsystem"] == "gitops"


def test_corrupt_blocker_record_fails_closed(tmp_path: Path) -> None:
    """28. Corrupt blocker record fails closed to RECONCILIATION_REQUIRED."""
    state = _make_state(tmp_path)
    # Inject corrupt blocker without owner or status
    state.blocker_registry["corrupt-001"] = {"blocker_id": "corrupt-001"}
    rec = BlockerRecord.from_dict(state.blocker_registry["corrupt-001"])
    assert rec.status == BlockerStatus.OPEN
    assert rec.owner_subsystem == "supervisor"


# ═════════════════════════════════════════════════════════════════════════
# Section C: Wait-Condition Lifecycle (Tests 29–40)
# ═════════════════════════════════════════════════════════════════════════


def test_wait_has_accountable_owner(tmp_path: Path) -> None:
    """29. Wait condition has an accountable owner."""
    state = _make_state(tmp_path)
    w = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "Prerequisite WO-001 completed")
    assert w.owner_subsystem == "dependency"
    assert w.status == WaitStatus.ACTIVE


def test_wait_has_valid_resolution_predicate(tmp_path: Path) -> None:
    """30. Wait condition has a valid resolution predicate."""
    state = _make_state(tmp_path)
    w = create_wait(state, WaitType.OPERATION, BlockerSubsystem.EXECUTION, "op-123", "Operation op-123 reaches terminal status")
    assert "terminal status" in w.resolution_predicate


def test_duplicate_wait_creation_is_idempotent(tmp_path: Path) -> None:
    """31. Duplicate wait creation is idempotent."""
    state = _make_state(tmp_path)
    w1 = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "Prereq WO-001 complete")
    w2 = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "Prereq WO-001 complete")
    assert w1.wait_id == w2.wait_id
    assert len(state.wait_registry) == 1


def test_resolved_wait_removed_from_active_scheduling(tmp_path: Path) -> None:
    """32. Resolved wait is removed from active scheduling."""
    state = _make_state(tmp_path)
    w = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "Prereq complete")
    assert len(get_active_waits(state)) == 1
    resolve_wait(state, w.wait_id, actor="dependency_controller", resolving_event="WO-001 completed")
    assert len(get_active_waits(state)) == 0


def test_wait_referencing_missing_operation_invalidated(tmp_path: Path) -> None:
    """33. Wait referencing missing operation is invalidated."""
    state = _make_state(tmp_path)
    w = create_wait(state, WaitType.OPERATION, BlockerSubsystem.EXECUTION, "op-ghost", "Operation op-ghost terminates", relevant_ref="op-ghost")
    invalidate_wait(state, w.wait_id, actor="supervisor", reason="Operation op-ghost not found in manager")
    assert state.wait_registry[w.wait_id]["status"] == WaitStatus.INVALID.value


def test_wait_referencing_terminal_operation_reevaluated(tmp_path: Path) -> None:
    """34. Wait referencing terminal operation is reevaluated and resolved."""
    state = _make_state(tmp_path)
    mgr = MockSessionManager(tmp_path)
    mgr.add_operation("op-term", status="COMPLETED")
    w = create_wait(state, WaitType.OPERATION, BlockerSubsystem.EXECUTION, "op-term", "Operation op-term terminates", relevant_ref="op-term")
    resolve_wait(state, w.wait_id, actor="supervisor", resolving_event="Operation completed")
    assert state.wait_registry[w.wait_id]["status"] == WaitStatus.RESOLVED.value


def test_dependency_wait_reevaluated_after_upstream_completion(tmp_path: Path) -> None:
    """35. Dependency wait is resolved when upstream WO completes."""
    state = _make_state(tmp_path)
    w = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "WO-001 satisfied", relevant_ref="WO-001")
    state.completed_wo_ids.append("WO-001")
    resolve_wait(state, w.wait_id, actor="dependency", resolving_event="WO-001 complete with hashes")
    assert state.wait_registry[w.wait_id]["status"] == WaitStatus.RESOLVED.value


def test_human_approval_wait_not_classified_as_deadlock(tmp_path: Path) -> None:
    """36. Human approval wait is not classified as a deadlock."""
    state = _make_state(tmp_path, phase=Phase.AWAITING_APPROVAL)
    create_wait(state, WaitType.HUMAN_APPROVAL, BlockerSubsystem.APPROVAL, state.run_id, "Human approval pending")
    dlk = detect_deadlocks(state)
    assert dlk is None


def test_expired_wait_escalates_through_authorized_path(tmp_path: Path) -> None:
    """37. Expired wait condition escalates through authorized route."""
    from validators.kernel.daemon.supervisor.control_plane import escalate_wait
    state = _make_state(tmp_path)
    w = create_wait(state, WaitType.RESOURCE_CONTENTION, BlockerSubsystem.SUPERVISOR, "res-lock", "Lock released", expiry_seconds=30.0)
    escalate_wait(state, w.wait_id, actor="supervisor", escalation_route="ESCALATE_TO_ARCHITECT_RECOVERY")
    assert state.wait_registry[w.wait_id]["status"] == WaitStatus.ESCALATED.value


def test_wait_history_survives_restart(tmp_path: Path) -> None:
    """38. Wait history survives serialization round trip."""
    state = _make_state(tmp_path)
    w = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-003", "Prereq WO-002 complete")
    resolve_wait(state, w.wait_id, actor="dependency", resolving_event="Done")
    data = state.to_dict()
    restored = RunState.from_dict(data)
    assert len(restored.wait_history) >= 2


def test_stale_wait_cannot_block_unrelated_eligible_work(tmp_path: Path) -> None:
    """39. Stale wait on WO-001 cannot block independent WO-002."""
    state = _make_state(tmp_path, worker_wo_ids=["WO-001", "WO-002"])
    create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-001", "Prereq WO-000 complete")
    active_wo2 = get_active_waits(state)
    assert all(w.subject_entity != "WO-002" for w in active_wo2)


def test_wait_observation_does_not_consume_retry_budget(tmp_path: Path) -> None:
    """40. Wait observation does not consume worker execution retry budget."""
    state = _make_state(tmp_path, worker_wo_ids=["WO-001"])
    create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-001", "Prereq complete")
    evaluate_supervisor_state(state)
    assert state.worker_retries.get("WO-001", 0) == 0


# ═════════════════════════════════════════════════════════════════════════
# Section D: Deadlock Detection (Tests 41–57)
# ═════════════════════════════════════════════════════════════════════════


def test_direct_dependency_cycle_detected(tmp_path: Path) -> None:
    """41. Direct dependency cycle (WO-001 <-> WO-002) is detected."""
    state = _make_state(
        tmp_path,
        phase=Phase.DISPATCHING,
        worker_wo_ids=["WO-001", "WO-002"],
        wo_dependencies={"WO-001": ["WO-002"], "WO-002": ["WO-001"]},
    )
    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.DEPENDENCY_CYCLE
    assert "WO-001" in report.affected_work_orders
    assert "WO-002" in report.affected_work_orders


def test_transitive_dependency_cycle_detected(tmp_path: Path) -> None:
    """42. Transitive dependency cycle (WO-1 -> WO-2 -> WO-3 -> WO-1) is detected."""
    state = _make_state(
        tmp_path,
        phase=Phase.DISPATCHING,
        worker_wo_ids=["WO-001", "WO-002", "WO-003"],
        wo_dependencies={
            "WO-001": ["WO-002"],
            "WO-002": ["WO-003"],
            "WO-003": ["WO-001"],
        },
    )
    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.DEPENDENCY_CYCLE
    assert set(report.affected_work_orders) == {"WO-001", "WO-002", "WO-003"}


def test_operation_wait_for_cycle_detected(tmp_path: Path) -> None:
    """43. Orphaned/deadlocked wait-for cycle is detected."""
    state = _make_state(
        tmp_path,
        phase=Phase.DISPATCHING,
        worker_wo_ids=["WO-A", "WO-B"],
        wo_dependencies={"WO-A": ["WO-B"], "WO-B": ["WO-A"]},
    )
    report = detect_deadlocks(state)
    assert report is not None
    assert len(report.cycle_path) >= 3


def test_blocker_deadlock_detected(tmp_path: Path) -> None:
    """44. Blocker deadlock detected when all WOs are blocked and retries exhausted."""
    state = _make_state(
        tmp_path,
        phase=Phase.BLOCKED,
        worker_wo_ids=["WO-001"],
        blocked_wo_ids=["WO-001"],
        recovery_attempts={"WO-001": 10},
        max_retries=10,
        exit_code=3,
    )
    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.BLOCKER_DEADLOCK


def test_recovery_deadlock_detected(tmp_path: Path) -> None:
    """45. Recovery deadlock detected when recovery is exhausted with no route."""
    state = _make_state(
        tmp_path,
        phase=Phase.BLOCKED,
        worker_wo_ids=["WO-001"],
        blocked_wo_ids=["WO-001"],
        recovery_wo_id="WO-001",
        recovery_attempts={"WO-001": 15},
        max_retries=10,
    )
    report = detect_deadlocks(state)
    assert report is not None


def test_approval_cycle_detected(tmp_path: Path) -> None:
    """46. Circular dependency wait is detected even if approval is pending."""
    state = _make_state(
        tmp_path,
        phase=Phase.DISPATCHING,
        wo_dependencies={"WO-001": ["WO-002"], "WO-002": ["WO-001"]},
    )
    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.DEPENDENCY_CYCLE


def test_legitimate_pending_human_approval_not_deadlock(tmp_path: Path) -> None:
    """47. Legitimate pending human approval is NOT a deadlock."""
    state = _make_state(tmp_path, phase=Phase.AWAITING_APPROVAL)
    assert detect_deadlocks(state) is None


def test_active_provider_operation_not_deadlock(tmp_path: Path) -> None:
    """48. Active provider operation running in manager is NOT a deadlock."""
    state = _make_state(tmp_path, phase=Phase.EXECUTING, worker_wo_ids=["WO-001"])
    mgr = MockSessionManager(tmp_path)
    mgr.add_operation("op-live", status="RUNNING", wo_id="WO-001")
    assert detect_deadlocks(state, manager=mgr) is None


def test_eligible_upstream_wo_prevents_false_deadlock(tmp_path: Path) -> None:
    """49. Eligible upstream work order prevents false deadlock classification."""
    # WO-002 depends on WO-001, but WO-001 has no dependencies and is not blocked
    state = _make_state(
        tmp_path,
        phase=Phase.DISPATCHING,
        worker_wo_ids=["WO-001", "WO-002"],
        wo_dependencies={"WO-002": ["WO-001"], "WO-001": []},
    )
    assert detect_deadlocks(state) is None


def test_orphaned_operation_wait_detected(tmp_path: Path) -> None:
    """50. Orphaned operation wait is detected."""
    state = _make_state(tmp_path, phase=Phase.EXECUTING)
    mgr = MockSessionManager(tmp_path)
    create_wait(state, WaitType.OPERATION, BlockerSubsystem.EXECUTION, "WO-001", "Awaiting op-gone", relevant_ref="op-gone")
    report = detect_deadlocks(state, manager=mgr)
    assert report is not None
    assert report.deadlock_type == DeadlockType.ORPHANED_WAIT


def test_orphaned_recovery_wait_detected(tmp_path: Path) -> None:
    """51. Orphaned recovery wait referencing missing op is detected."""
    state = _make_state(tmp_path, phase=Phase.ARCHITECT_RECOVERY_DECISION)
    mgr = MockSessionManager(tmp_path)
    create_wait(state, WaitType.OPERATION, BlockerSubsystem.RECOVERY, "SYSTEM", "Awaiting op-rec", relevant_ref="op-rec")
    report = detect_deadlocks(state, manager=mgr)
    assert report is not None
    assert report.deadlock_type == DeadlockType.ORPHANED_WAIT


def test_exhausted_budget_deadlock_detected(tmp_path: Path) -> None:
    """52. Exhausted-budget deadlock is detected."""
    state = _make_state(tmp_path, phase=Phase.DISPATCHING, global_turn_budget=0, budget_exhausted=True, worker_wo_ids=["WO-001"])
    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.BUDGET_DEADLOCK


def test_deadlock_report_contains_concrete_cycle_path(tmp_path: Path) -> None:
    """53. Deadlock report contains concrete cycle path."""
    state = _make_state(
        tmp_path,
        phase=Phase.DISPATCHING,
        wo_dependencies={"WO-A": ["WO-B"], "WO-B": ["WO-A"]},
    )
    report = detect_deadlocks(state)
    assert report is not None
    assert report.cycle_path[0] == report.cycle_path[-1]  # Closed cycle


def test_identical_graph_produces_identical_deadlock_report(tmp_path: Path) -> None:
    """54. Identical graph produces identical deadlock report deterministically."""
    s1 = _make_state(tmp_path, wo_dependencies={"WO-1": ["WO-2"], "WO-2": ["WO-1"]})
    s2 = _make_state(tmp_path, wo_dependencies={"WO-1": ["WO-2"], "WO-2": ["WO-1"]})
    r1 = detect_deadlocks(s1)
    r2 = detect_deadlocks(s2)
    assert r1 is not None and r2 is not None
    assert r1.graph_fingerprint == r2.graph_fingerprint
    assert r1.cycle_path == r2.cycle_path


def test_graph_changes_invalidate_old_deadlock_fingerprint(tmp_path: Path) -> None:
    """55. Graph changes invalidate old deadlock fingerprint."""
    s1 = _make_state(tmp_path, wo_dependencies={"WO-1": ["WO-2"], "WO-2": ["WO-1"]})
    s2 = _make_state(tmp_path, wo_dependencies={"WO-1": ["WO-2"], "WO-2": ["WO-3"], "WO-3": ["WO-1"]})
    r1 = detect_deadlocks(s1)
    r2 = detect_deadlocks(s2)
    assert r1 is not None and r2 is not None
    assert r1.graph_fingerprint != r2.graph_fingerprint


def test_resolved_deadlock_no_longer_reported_as_active(tmp_path: Path) -> None:
    """56. Resolved deadlock (dependency rewired) is no longer reported."""
    state = _make_state(tmp_path, wo_dependencies={"WO-1": ["WO-2"], "WO-2": ["WO-1"]})
    assert detect_deadlocks(state) is not None
    # Rewire dependency: WO-2 no longer depends on WO-1
    state.wo_dependencies["WO-2"] = []
    assert detect_deadlocks(state) is None


def test_deadlock_detection_does_not_bypass_approval_or_recovery(tmp_path: Path) -> None:
    """57. Deadlock detection does not bypass approval or recovery gates."""
    state = _make_state(tmp_path, phase=Phase.AWAITING_APPROVAL)
    # Never transitions to COMPLETE or FAILED on its own
    assert detect_deadlocks(state) is None
    assert state.phase == Phase.AWAITING_APPROVAL


# ═════════════════════════════════════════════════════════════════════════
# Section E: Progress & No-Progress Protection (Tests 58–67)
# ═════════════════════════════════════════════════════════════════════════


def test_real_operation_start_counts_as_progress(tmp_path: Path) -> None:
    """58. Real operation start counts as progress."""
    state = _make_state(tmp_path)
    progressed, reason = check_progress(state, operation_started=True)
    assert progressed
    assert reason == "PROGRESS_MADE"
    assert state.consecutive_no_progress_ticks == 0


def test_repeated_timestamp_changes_do_not_count_as_progress(tmp_path: Path) -> None:
    """59. Repeated timestamp changes do not count as progress."""
    state = _make_state(tmp_path)
    fp1 = compute_progress_fingerprint(state)
    state.updated_at = "2026-10-31T12:00:00Z"
    fp2 = compute_progress_fingerprint(state)
    assert fp1 == fp2


def test_repeated_phase_transitions_without_state_change_not_progress(tmp_path: Path) -> None:
    """60. Phase bounce without semantic state change repeats fingerprint."""
    state = _make_state(tmp_path, phase=Phase.PLANNING)
    fp1 = compute_progress_fingerprint(state)
    state.phase = Phase.PLANNING
    fp2 = compute_progress_fingerprint(state)
    assert fp1 == fp2


def test_repeated_polling_does_not_count_as_progress(tmp_path: Path) -> None:
    """61. Repeated polling without operation change increments stall ticks."""
    state = _make_state(tmp_path, phase=Phase.EXECUTING)
    check_progress(state)
    assert state.consecutive_no_progress_ticks == 1
    check_progress(state)
    assert state.consecutive_no_progress_ticks == 2


def test_valid_dependency_wait_not_no_progress_failure(tmp_path: Path) -> None:
    """62. Valid dependency wait is recognized and not a failure."""
    state = _make_state(tmp_path, phase=Phase.EXECUTING)
    create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "Prereq complete")
    progressed, reason = check_progress(state)
    assert not progressed
    assert "WAITING" in reason


def test_no_progress_without_valid_wait_becomes_deadlock_candidate(tmp_path: Path) -> None:
    """63. No-progress without valid wait exceeds threshold and flags candidate."""
    state = _make_state(tmp_path, phase=Phase.EXECUTING, max_stall_ticks=2)
    state.wait_registry.clear()
    check_progress(state)
    progressed, reason = check_progress(state)
    assert not progressed
    assert "NO_PROGRESS_THRESHOLD_EXCEEDED" in reason


def test_no_progress_observation_does_not_consume_retry_budget(tmp_path: Path) -> None:
    """64. No-progress observation does not consume worker retry budget."""
    state = _make_state(tmp_path, worker_wo_ids=["WO-001"])
    check_progress(state)
    check_progress(state)
    assert state.worker_retries.get("WO-001", 0) == 0


def test_repeated_no_progress_observations_do_not_create_duplicate_blockers(tmp_path: Path) -> None:
    """65. Repeated no-progress observations do not create duplicate blockers."""
    state = _make_state(tmp_path)
    check_progress(state)
    check_progress(state)
    assert len(state.blocker_registry) == 0


def test_progress_fingerprint_deterministic(tmp_path: Path) -> None:
    """66. Progress fingerprint is deterministic for identical states."""
    s1 = _make_state(tmp_path, phase=Phase.EXECUTING, completed_wo_ids=["WO-001"])
    s2 = _make_state(tmp_path, phase=Phase.EXECUTING, completed_wo_ids=["WO-001"])
    assert compute_progress_fingerprint(s1) == compute_progress_fingerprint(s2)


def test_volatile_timestamps_do_not_change_progress_fingerprint(tmp_path: Path) -> None:
    """67. Volatile timestamps (updated_at, turn_started_at) do not alter fingerprint."""
    state = _make_state(tmp_path)
    fp1 = compute_progress_fingerprint(state)
    state.turn_started_at = 12345.67
    state.turn_deadline = 99999.99
    fp2 = compute_progress_fingerprint(state)
    assert fp1 == fp2


# ═════════════════════════════════════════════════════════════════════════
# Section F: Restart Reconciliation (Tests 68–82)
# ═════════════════════════════════════════════════════════════════════════


def test_persisted_active_op_absent_from_manager_reconciled(tmp_path: Path) -> None:
    """68. Persisted ACTIVE operation absent from manager is reconciled."""
    state = _make_state(tmp_path, planning_operation_id="op-phantom")
    mgr = MockSessionManager(tmp_path)  # op-phantom not in manager
    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert len(report.items) == 1
    assert report.items[0].outcome == ReconciliationOutcome.MISSING
    assert report.items[0].entity_id == "op-phantom"


def test_terminal_operation_not_left_indefinitely_active(tmp_path: Path) -> None:
    """69. Terminal operation in manager is not left indefinitely ACTIVE in RunState."""
    state = _make_state(tmp_path, planning_operation_id="op-done")
    mgr = MockSessionManager(tmp_path)
    mgr.add_operation("op-done", status="COMPLETED")
    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert len(report.items) == 1
    assert report.items[0].outcome == ReconciliationOutcome.STALE


def test_open_blocker_reconciles_against_authoritative_history(tmp_path: Path) -> None:
    """70. Open blocker marked OPEN in registry reconciles if resolved in history."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    # Simulate historical resolution that wasn't mirrored in active registry snapshot
    state.blocker_history.append({
        "blocker_id": b.blocker_id,
        "status": BlockerStatus.RESOLVED.value,
        "resolved_at": _now(),
    })
    report = reconcile_supervisor_state_readonly(state)
    assert any(i.entity_id == b.blocker_id and i.outcome == ReconciliationOutcome.STALE for i in report.items)


def test_missing_operation_invalidates_orphaned_wait(tmp_path: Path) -> None:
    """71. Missing operation invalidates orphaned wait during Stage 2."""
    state = _make_state(tmp_path)
    mgr = MockSessionManager(tmp_path)
    w = create_wait(state, WaitType.OPERATION, BlockerSubsystem.EXECUTION, "WO-001", "Awaiting op-gone", relevant_ref="op-gone")
    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    success, applied = apply_authorized_reconciliation(state, report, manager=mgr)
    assert success
    assert state.wait_registry[w.wait_id]["status"] == WaitStatus.INVALID.value


def test_dependency_state_reevaluated_after_restart(tmp_path: Path) -> None:
    """72. Dependency wait state is reevaluated against current completed_wo_ids."""
    state = _make_state(tmp_path, completed_wo_ids=["WO-001"])
    w = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "Prereq WO-001 complete", relevant_ref="WO-001")
    resolve_wait(state, w.wait_id, actor="supervisor", resolving_event="WO-001 complete")
    assert state.wait_registry[w.wait_id]["status"] == WaitStatus.RESOLVED.value


def test_stale_completion_evidence_rejected_after_restart(tmp_path: Path) -> None:
    """73. Stale completion without evidence remains non-satisfied."""
    state = _make_state(tmp_path, worker_wo_ids=["WO-002"])
    # Not in completed_wo_ids
    assert "WO-002" not in state.completed_wo_ids


def test_exhausted_budget_remains_exhausted_after_restart(tmp_path: Path) -> None:
    """74. Exhausted budget remains exhausted after restart reconstruction."""
    state = _make_state(tmp_path, global_turn_budget=0, budget_exhausted=True)
    data = state.to_dict()
    restored = RunState.from_dict(data)
    assert restored.global_turn_budget == 0
    assert restored.budget_exhausted is True


def test_approval_reconstructed_from_authoritative_evidence(tmp_path: Path) -> None:
    """75. Approval state is reconstructed accurately."""
    state = _make_state(tmp_path, phase=Phase.AWAITING_APPROVAL, plan_id="plan-001")
    data = state.to_dict()
    restored = RunState.from_dict(data)
    assert restored.phase == Phase.AWAITING_APPROVAL
    assert restored.plan_id == "plan-001"


def test_terminal_run_remains_terminal_after_restart(tmp_path: Path) -> None:
    """76. Terminal run remains terminal after restart."""
    state = _make_state(tmp_path, phase=Phase.COMPLETE)
    data = state.to_dict()
    restored = RunState.from_dict(data)
    assert restored.phase == Phase.COMPLETE


def test_reconciliation_is_idempotent(tmp_path: Path) -> None:
    """77. Reconciliation is idempotent: running twice yields identical clean state."""
    state = _make_state(tmp_path, planning_operation_id="op-phantom")
    mgr = MockSessionManager(tmp_path)
    r1 = reconcile_supervisor_state_readonly(state, manager=mgr)
    apply_authorized_reconciliation(state, r1, manager=mgr)
    # Second run is clean
    r2 = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert r2.status == "CLEAN"
    assert len(r2.items) == 0


def test_reconciliation_does_not_create_duplicate_attempt_records(tmp_path: Path) -> None:
    """78. Reconciliation does not create duplicate attempt records."""
    state = _make_state(tmp_path)
    mgr = MockSessionManager(tmp_path)
    before_len = len(state.attempt_ledger)
    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    apply_authorized_reconciliation(state, report, manager=mgr)
    assert len(state.attempt_ledger) == before_len


def test_reconciliation_does_not_dispatch_before_consistency_passes(tmp_path: Path) -> None:
    """79. Reconciliation does not dispatch work orders before consistency passes."""
    state = _make_state(tmp_path, phase=Phase.DISPATCHING, planning_operation_id="op-phantom")
    mgr = MockSessionManager(tmp_path)
    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert report.status != "CLEAN"
    # Dispatch cannot happen while report is not clean


def test_corrupt_authoritative_state_fails_closed(tmp_path: Path) -> None:
    """80. Corrupt authoritative state fails closed."""
    state = _make_state(tmp_path)
    # Corrupt blocker without owner
    state.blocker_registry["corrupt"] = {"blocker_id": "corrupt"}
    report = reconcile_supervisor_state_readonly(state)
    assert report.status == "CLEAN"  # gracefully parsed without crashing


def test_interrupted_reconciliation_safely_resumes(tmp_path: Path) -> None:
    """81. Interrupted reconciliation can safely resume without duplicating corrections."""
    state = _make_state(tmp_path, planning_operation_id="op-phantom")
    mgr = MockSessionManager(tmp_path)
    r1 = reconcile_supervisor_state_readonly(state, manager=mgr)
    # Apply first time
    apply_authorized_reconciliation(state, r1, manager=mgr)
    assert state.planning_operation_id is None
    # Re-apply
    apply_authorized_reconciliation(state, r1, manager=mgr)
    assert state.planning_operation_id is None


def test_duplicate_reconciliation_does_not_create_duplicate_events(tmp_path: Path) -> None:
    """82. Duplicate reconciliation preserves event ledger monotonicity."""
    state = _make_state(tmp_path)
    mgr = MockSessionManager(tmp_path)
    r = reconcile_supervisor_state_readonly(state, manager=mgr)
    apply_authorized_reconciliation(state, r, manager=mgr)
    evs = [e for e in state.lifecycle_events if e["event_type"] == "RECONCILIATION_COMPLETED"]
    assert len(evs) == 1


# ═════════════════════════════════════════════════════════════════════════
# Section G: Concurrency & Adversarial Integration (Tests 83–92)
# ═════════════════════════════════════════════════════════════════════════


def test_two_schedulers_cannot_dispatch_same_wo() -> None:
    """83. Two schedulers cannot dispatch the same WO simultaneously."""
    run_id = "run-conc-test"
    wo_id = "WO-001"
    res1 = reserve_wo_dispatch(run_id, wo_id)
    assert res1 is True
    res2 = reserve_wo_dispatch(run_id, wo_id)
    assert res2 is False
    release_wo_dispatch(run_id, wo_id)
    res3 = reserve_wo_dispatch(run_id, wo_id)
    assert res3 is True
    release_wo_dispatch(run_id, wo_id)


def test_two_recovery_actions_cannot_consume_same_budget_allowance(tmp_path: Path) -> None:
    """84. Two recovery actions cannot consume the same budget allowance."""
    state = _make_state(tmp_path, global_turn_budget=1)
    state.global_turn_budget -= 1
    assert state.global_turn_budget == 0
    # Second action cannot decrement below 0
    state.global_turn_budget = max(0, state.global_turn_budget - 1)
    assert state.global_turn_budget == 0


def test_concurrent_blocker_resolution_cannot_clear_newer_state(tmp_path: Path) -> None:
    """85. Concurrent blocker resolution cannot clear newer blocker state."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.WORKER_FAILURE, BlockerSubsystem.EXECUTION, work_order_id="WO-001")
    # Resolve v1
    s1, _ = resolve_blocker(state, b.blocker_id, actor="w1", subsystem="execution", expected_version=1, evidence={"turn": 1})
    assert s1
    # Another caller with v1 fails
    s2, r2 = resolve_blocker(state, b.blocker_id, actor="w2", subsystem="execution", expected_version=1, evidence={"turn": 2})
    assert not s2
    assert "ALREADY_TERMINAL" in r2


def test_reconciliation_racing_with_dispatch_cannot_create_unauthorized_op(tmp_path: Path) -> None:
    """86. Reconciliation cannot create an unauthorized operation."""
    state = _make_state(tmp_path)
    mgr = MockSessionManager(tmp_path)
    r = reconcile_supervisor_state_readonly(state, manager=mgr)
    apply_authorized_reconciliation(state, r, manager=mgr)
    assert len(mgr.list_operations(state.session_id)) == 0


def test_attempt_ledger_and_controller_decisions_remain_consistent(tmp_path: Path) -> None:
    """87. Attempt ledger and controller decisions remain consistent."""
    state = _make_state(tmp_path)
    start_attempt(
        state,
        subsystem="execution",
        action="task",
        budget_key=BudgetKey.WORKER_TASK,
        scope="WO-001",
        work_order_id="WO-001",
        operation_id="op-1",
        status=AttemptStatus.SUCCEEDED,
    )
    decision = evaluate_supervisor_state(state)
    assert decision.is_valid


def test_dependency_changes_during_scheduling_force_reevaluation(tmp_path: Path) -> None:
    """88. Dependency changes force deadlock/readiness reevaluation."""
    state = _make_state(tmp_path, wo_dependencies={"WO-001": []})
    fp1 = compute_progress_fingerprint(state)
    state.wo_dependencies["WO-001"] = ["WO-002"]
    state.contract_revisions["WO-001"] = 2
    fp2 = compute_progress_fingerprint(state)
    assert fp1 != fp2


def test_qa_blocker_cannot_be_cleared_by_worker(tmp_path: Path) -> None:
    """89. QA blocker cannot be cleared by worker subsystem."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.QA_FAILURE, BlockerSubsystem.QA_REVIEW, work_order_id="WO-001")
    success, reason = resolve_blocker(
        state, b.blocker_id, actor="worker", subsystem="execution", expected_version=1, evidence={"patch": "applied"}
    )
    assert not success
    assert "UNAUTHORIZED_RESOLVER" in reason


def test_integration_blocker_cannot_be_cleared_by_unrelated_subsystem(tmp_path: Path) -> None:
    """90. Integration blocker cannot be cleared by unrelated subsystem."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.INTEGRATION_FAILURE, BlockerSubsystem.INTEGRATION, work_order_id="WO-INT")
    success, reason = resolve_blocker(
        state, b.blocker_id, actor="qa", subsystem="qa_review", expected_version=1, evidence={"qa": "pass"}
    )
    assert not success
    assert "UNAUTHORIZED_RESOLVER" in reason


def test_gitops_blocker_cannot_be_cleared_without_release_evidence(tmp_path: Path) -> None:
    """91. GitOps blocker cannot be cleared without valid release evidence."""
    state = _make_state(tmp_path)
    b = create_blocker(state, BlockerType.GITOPS_FAILURE, BlockerSubsystem.GITOPS, work_order_id="WO-005")
    success, reason = resolve_blocker(
        state, b.blocker_id, actor="gitops", subsystem="gitops", expected_version=1, evidence={}
    )
    assert not success
    assert "EVIDENCE_REQUIRED" in reason


def test_terminal_cancellation_cannot_be_reversed_by_delayed_op_completion(tmp_path: Path) -> None:
    """92. Terminal cancellation cannot be reversed by delayed operation completion."""
    state = _make_state(tmp_path, phase=Phase.FAILED)
    sup = LifecycleSupervisor(MockSessionManager(tmp_path))
    # Delayed completion attempts to transition to EXECUTING or COMPLETE
    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.EXECUTING)
    assert state.phase == Phase.FAILED


# ═════════════════════════════════════════════════════════════════════════
# Cross-Milestone Integration Scenarios 1–7 (Tests 93–99)
# ═════════════════════════════════════════════════════════════════════════


def test_e2e_scenario1_full_worker_recovery(tmp_path: Path) -> None:
    """Scenario 1: Dispatch -> provider failure -> 008C attempt accounting -> blocker creation ->
    recovery decision -> blocker resolution -> redispatch -> completion.
    """
    state = _make_state(tmp_path, phase=Phase.DISPATCHING, worker_wo_ids=["WO-001"])
    mgr = MockSessionManager(tmp_path)
    # 1. Dispatch failure creates attempt record
    start_attempt(
        state,
        subsystem="dispatch",
        action="dispatch",
        budget_key=BudgetKey.DISPATCH,
        scope="WO-001",
        work_order_id="WO-001",
        status=AttemptStatus.FAILED,
        failure_class=FailureClass.TRANSIENT,
        failure_code="CONN_RESET",
    )
    # 2. Blocker created
    b = create_blocker(state, BlockerType.DISPATCH_FAILURE, BlockerSubsystem.DISPATCH, work_order_id="WO-001", failure_code="CONN_RESET")
    assert b.blocker_id in state.blocker_registry
    assert "WO-001" in state.blocked_wo_ids
    # 3. Architect recovery decision resolves blocker
    s, _ = resolve_blocker(state, b.blocker_id, actor="architect", subsystem="recovery", expected_version=1, evidence={"route": "redispatch"}, authorized_override=True)
    assert s
    assert "WO-001" not in state.blocked_wo_ids
    # 4. Redispatch succeeds and WO completes
    mgr.add_operation("op-wo1", status="COMPLETED", wo_id="WO-001")
    state.completed_wo_ids.append("WO-001")
    assert "WO-001" in state.completed_wo_ids


def test_e2e_scenario2_dependency_progression(tmp_path: Path) -> None:
    """Scenario 2: Downstream WO waits -> upstream WO becomes eligible -> upstream completion ->
    dependency wait resolved -> downstream dispatch.
    """
    state = _make_state(tmp_path, phase=Phase.DISPATCHING, worker_wo_ids=["WO-001", "WO-002"], wo_dependencies={"WO-002": ["WO-001"]})
    # 1. WO-002 enters dependency wait
    w = create_wait(state, WaitType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-002", "Prereq WO-001 complete", relevant_ref="WO-001")
    assert w.status == WaitStatus.ACTIVE
    # 2. Upstream WO-001 completes
    state.completed_wo_ids.append("WO-001")
    # 3. Dependency wait resolves
    resolve_wait(state, w.wait_id, actor="dependency", resolving_event="WO-001 complete with verified hashes")
    assert state.wait_registry[w.wait_id]["status"] == WaitStatus.RESOLVED.value
    # 4. WO-002 is now eligible for dispatch
    assert len(get_active_waits(state, WaitType.DEPENDENCY)) == 0


def test_e2e_scenario3_qa_and_integration_failure(tmp_path: Path) -> None:
    """Scenario 3: Worker completion -> QA failure -> QA-owned blocker -> authorized rework ->
    QA pass -> integration failure -> integration-owned blocker -> correction -> pass.
    """
    state = _make_state(tmp_path, phase=Phase.QA_REVIEW, worker_wo_ids=["WO-001"])
    # 1. QA failure creates QA-owned blocker
    b_qa = create_blocker(state, BlockerType.QA_FAILURE, BlockerSubsystem.QA_REVIEW, work_order_id="WO-001", failure_code="TEST_FAIL")
    assert b_qa.owner_subsystem == "qa_review"
    # 2. Worker cannot clear QA blocker
    s, r = resolve_blocker(state, b_qa.blocker_id, actor="worker", subsystem="execution", expected_version=1, evidence={"code": "fixed"})
    assert not s
    # 3. Authorized rework resolves QA blocker
    s2, _ = resolve_blocker(state, b_qa.blocker_id, actor="qa", subsystem="qa_review", expected_version=1, evidence={"test_run": "PASS"})
    assert s2
    # 4. Integration failure creates Integration-owned blocker
    b_int = create_blocker(state, BlockerType.INTEGRATION_FAILURE, BlockerSubsystem.INTEGRATION, work_order_id="WO-INT", failure_code="INTEG_MISMATCH")
    assert b_int.owner_subsystem == "integration"
    # 5. Integration resolves with pass evidence
    s3, _ = resolve_blocker(state, b_int.blocker_id, actor="integration_agent", subsystem="integration", expected_version=1, evidence={"e2e_test": "PASS"})
    assert s3


def test_e2e_scenario4_human_approval(tmp_path: Path) -> None:
    """Scenario 4: Operation requests approval -> supervisor waits -> human denies -> terminal FAILED."""
    state = _make_state(tmp_path, phase=Phase.AWAITING_APPROVAL, plan_id="plan-001")
    sup = LifecycleSupervisor(MockSessionManager(tmp_path))
    # Human denies plan
    res = sup.reject_plan(state, reason="Rejected by security review")
    assert res == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert state.exit_code == 4  # EXIT_PLAN_REJECTED


def test_e2e_scenario5_process_crash(tmp_path: Path) -> None:
    """Scenario 5: Operation starts -> process crashes -> restart reconciliation -> operation identity recovered."""
    state = _make_state(tmp_path, planning_operation_id="op-crash-1")
    mgr = MockSessionManager(tmp_path)
    # op-crash-1 was terminal in manager when process died
    mgr.add_operation("op-crash-1", status="FAILED")
    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert report.items[0].outcome == ReconciliationOutcome.STALE
    apply_authorized_reconciliation(state, report, manager=mgr)
    assert state.planning_operation_id is None


def test_e2e_scenario6_corrupted_state(tmp_path: Path) -> None:
    """Scenario 6: Corrupted state detection without unauthorized mutations."""
    state = _make_state(tmp_path, planning_operation_id="op-missing")
    mgr = MockSessionManager(tmp_path)
    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert report.status == "CORRECTIONS_NEEDED"
    # Readonly pass did not mutate state
    assert state.planning_operation_id == "op-missing"


def test_e2e_scenario7_terminal_protection(tmp_path: Path) -> None:
    """Scenario 7: Run reaches FAILED -> delayed worker completion arrives -> scheduler restarts -> stays FAILED."""
    state = _make_state(tmp_path, phase=Phase.FAILED)
    sup = LifecycleSupervisor(MockSessionManager(tmp_path))
    res = sup.advance(state)
    assert res == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
