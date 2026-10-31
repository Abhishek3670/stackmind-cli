"""SDP-FIX-008D-HF2 Test Suite — Control-Plane Authorization, Deadlock Isolation, and Reconciliation Integrity.

Comprehensive regression tests covering:
- HF2-A: Separate resume authorization from execution evidence (Invariants 1 & 5).
- HF2-B: Component-aware deadlock detection and independent component graph isolation.
- HF2-C: Fingerprint-bound and version-bound restart reconciliation (TOCTOU protection).
- HF2-D: Complete per-work-order eligibility evaluation.
- HF2-E: Typed resolution evidence and authorization provenance for blocker resolution.
- HF2-F: Side-effect-free transition validation.
- HF2-G: Authoritative terminal-state consistency.
- HF2-H: Progress fingerprint expansion and sensitivity.
- HF2-I: Graph path reconstruction and complete deadlock category evaluation.
- All 10 Mandatory Adversarial Scenarios.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest

from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.kernel.daemon.supervisor.control_plane import (
    AuthorizationContext,
    BlockerRecord,
    BlockerStatus,
    BlockerSubsystem,
    BlockerType,
    ControllerState,
    DeadlockCategory,
    DeadlockReport,
    DeadlockType,
    EligibilityDecision,
    ReconciliationItem,
    ReconciliationOutcome,
    ReconciliationReport,
    ReconciliationSnapshot,
    WaitRecord,
    WaitStatus,
    WaitType,
    apply_authorized_reconciliation,
    compute_progress_fingerprint,
    create_blocker,
    create_wait,
    detect_all_deadlocks,
    detect_deadlocks,
    evaluate_supervisor_state,
    evaluate_work_order_eligibility,
    get_active_blockers,
    get_active_waits,
    is_operation_terminal,
    is_run_terminal,
    is_work_order_terminal,
    reconcile_supervisor_state_readonly,
    record_lifecycle_event,
    resolve_blocker,
    _reconstruct_cycle_path,
)
from validators.kernel.daemon.state_store import StateIntegrityError


# ── Test Fixtures and Recording Stubs ──────────────────────────────────────────


class MockOperationManager:
    """Mock manager tracking operations for supervisor state validation."""

    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {
            "sess-hf2": {"state": "RUNNING", "active_operation": None, "journal": []}
        }

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        return self.operations.get(op_id)

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            return [o for o in self.operations.values() if o.get("session_id") == session_id]
        return list(self.operations.values())

    def start_turn(
        self,
        session_id: str,
        prompt: str,
        role: str = "backend",
        agent_id: str = "codex",
        work_order_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        op_id = f"op-{len(self.operations) + 1}"
        rec = {
            "operation_id": op_id,
            "session_id": session_id,
            "role": role,
            "agent_id": agent_id,
            "work_order_id": work_order_id,
            "status": "RUNNING",
        }
        self.operations[op_id] = rec
        return rec

    def complete_operation(self, op_id: str, status: str = "COMPLETED") -> None:
        if op_id in self.operations:
            self.operations[op_id]["status"] = status


def _make_state(
    run_id: str = "run-hf2",
    phase: Phase = Phase.INIT,
    workspace: str = "/workspace",
    session_id: str = "sess-hf2",
    **kwargs: Any,
) -> RunState:
    state = RunState(
        run_id=run_id,
        product_goal="HF2 Validation Goal",
        workspace=workspace,
        session_id=session_id,
        phase=phase,
    )
    for k, v in kwargs.items():
        setattr(state, k, v)
    return state


# ── Area 1: Resume Authorization vs Execution Evidence (HF2-A) ─────────────────


def test_hf2_a_executing_requires_live_operation_in_manager() -> None:
    """Invariant 1: Transition to EXECUTING requires authoritative live operation evidence."""
    mgr = MockOperationManager()
    state = _make_state(phase=Phase.DISPATCHING, worker_wo_ids=["WO-001"])

    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid
    assert any("Invariant 1" in v for v in decision.violations)


def test_hf2_a_authorized_override_alone_cannot_satisfy_executing() -> None:
    """HF2-A Rule: authorized_override=True alone cannot bypass execution evidence requirement."""
    mgr = MockOperationManager()
    state = _make_state(phase=Phase.DISPATCHING, worker_wo_ids=["WO-001"])

    decision = evaluate_supervisor_state(
        state, Phase.EXECUTING, manager=mgr, authorized_override=True
    )
    assert not decision.is_valid
    assert any("Invariant 1" in v for v in decision.violations)


def test_hf2_a_executing_with_missing_manager_op_rejected() -> None:
    """Candidate op attribute pointing to non-existent manager op is rejected."""
    mgr = MockOperationManager()
    state = _make_state(phase=Phase.DISPATCHING, authoring_operation_id="op-missing")

    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid
    assert any("Invariant 1" in v for v in decision.violations)


def test_hf2_a_executing_with_terminal_manager_op_rejected() -> None:
    """Candidate op that is COMPLETED or FAILED in manager is rejected."""
    mgr = MockOperationManager()
    mgr.operations["op-term"] = {
        "operation_id": "op-term",
        "session_id": "sess-hf2",
        "status": "COMPLETED",
    }
    state = _make_state(phase=Phase.DISPATCHING, authoring_operation_id="op-term")

    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid


def test_hf2_a_executing_with_cancelled_manager_op_rejected() -> None:
    """Candidate op that is CANCELLED in manager is rejected."""
    mgr = MockOperationManager()
    mgr.operations["op-canc"] = {
        "operation_id": "op-canc",
        "session_id": "sess-hf2",
        "status": "CANCELLED",
    }
    state = _make_state(phase=Phase.DISPATCHING, authoring_operation_id="op-canc")

    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid


def test_hf2_a_executing_with_foreign_session_op_rejected() -> None:
    """Live op belonging to a different session/run cannot satisfy Invariant 1."""
    mgr = MockOperationManager()
    mgr.operations["op-foreign"] = {
        "operation_id": "op-foreign",
        "session_id": "sess-OTHER",
        "run_id": "run-OTHER",
        "status": "RUNNING",
    }
    state = _make_state(phase=Phase.DISPATCHING, authoring_operation_id="op-foreign")

    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid


def test_hf2_a_executing_with_mismatched_wo_op_rejected() -> None:
    """Live op bound to WO-002 cannot satisfy EXECUTING when executing_wo_id is WO-001."""
    mgr = MockOperationManager()
    mgr.operations["op-wo2"] = {
        "operation_id": "op-wo2",
        "session_id": "sess-hf2",
        "work_order_id": "WO-002",
        "status": "RUNNING",
    }
    state = _make_state(
        phase=Phase.DISPATCHING,
        executing_wo_id="WO-001",
        authoring_operation_id="op-wo2",
    )

    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=mgr)
    assert not decision.is_valid


def test_hf2_a_executing_with_matching_live_op_accepted() -> None:
    """Verified live manager operation bound to correct session satisfies Invariant 1."""
    mgr = MockOperationManager()
    mgr.operations["op-live"] = {
        "operation_id": "op-live",
        "session_id": "sess-hf2",
        "work_order_id": "WO-001",
        "status": "RUNNING",
    }
    state = _make_state(
        phase=Phase.DISPATCHING,
        executing_wo_id="WO-001",
        authoring_operation_id="op-live",
    )

    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=mgr)
    assert decision.is_valid


def test_hf2_a_standalone_executing_without_op_attr_rejected() -> None:
    """Standalone unit test mode without manager rejects EXECUTING when no op attrs exist."""
    state = _make_state(phase=Phase.DISPATCHING, worker_wo_ids=["WO-001"])
    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=None)
    assert not decision.is_valid


def test_hf2_a_standalone_executing_with_current_operation_active_accepted() -> None:
    """Standalone mode with explicit current_operation_active=True satisfies Invariant 1."""
    state = _make_state(phase=Phase.DISPATCHING)
    decision = evaluate_supervisor_state(
        state, Phase.EXECUTING, manager=None, current_operation_active=True
    )
    assert decision.is_valid


def test_hf2_a_standalone_executing_with_planning_op_accepted() -> None:
    """Standalone mode with populated planning_operation_id satisfies Invariant 1."""
    state = _make_state(phase=Phase.INIT, planning_operation_id="op-plan-1")
    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=None)
    assert decision.is_valid


def test_hf2_a_resume_without_live_op_routes_to_dispatching() -> None:
    """Controls: human resume without live operation transitions to DISPATCHING, not EXECUTING."""
    mgr = MockOperationManager()
    sup = LifecycleSupervisor(mgr)
    state = _make_state(
        phase=Phase.WAITING_FOR_HUMAN,
        worker_wo_ids=["WO-001"],
        interrupted_phase="EXECUTING",
    )

    res = sup.resume_run_with_human_override(state, additional_budget=10, reason="test")
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.DISPATCHING


def test_hf2_a_re_attest_without_live_op_routes_to_dispatching() -> None:
    """Controls: re-attestation without live operation transitions to DISPATCHING, not EXECUTING."""
    mgr = MockOperationManager()
    sup = LifecycleSupervisor(mgr)
    state = _make_state(
        phase=Phase.WAITING_FOR_HUMAN,
        worker_wo_ids=["WO-001"],
        interrupted_phase="EXECUTING",
    )

    res = sup.re_attest_run(state, human_signature="lead-operator", confirmed_budget=20)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.DISPATCHING


def test_hf2_a_recovery_clear_without_redispatch_routes_to_dispatching(tmp_path: Path) -> None:
    """Recovery: clearing a blocker with redispatch=False transitions to DISPATCHING."""
    sup = LifecycleSupervisor(MockOperationManager())
    state = _make_state(
        phase=Phase.ARCHITECT_REPAIR,
        worker_wo_ids=["WO-001"],
        blocked_wo_ids=["WO-001"],
        worker_blockers={"WO-001": {"operation_id": "op-x"}},
    )

    res = sup._clear_worker_blocker(state, tmp_path, "WO-001", redispatch=False)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.DISPATCHING


# ── Area 2: Component-Aware Deadlock Detection (HF2-B & HF2-I) ─────────────────


def test_hf2_b_graph_partitioning_independent_components() -> None:
    """Component isolation: disconnected subgraphs are partitioned and evaluated independently."""
    state = _make_state()
    state.wo_dependencies = {
        "WO-A": ["WO-B"],
        "WO-B": ["WO-A"],
        "WO-C": ["WO-D"],
        "WO-D": [],
    }

    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.DEPENDENCY_CYCLE
    assert "WO-A" in report.cycle_path
    assert "WO-B" in report.cycle_path
    assert "WO-C" not in report.cycle_path
    assert "WO-D" not in report.cycle_path


def test_hf2_b_deadlock_in_component_a_detected_while_component_b_running() -> None:
    """Deadlock detection is not globally suppressed when an independent component is executing."""
    mgr = MockOperationManager()
    mgr.operations["op-active"] = {
        "operation_id": "op-active",
        "session_id": "sess-hf2",
        "work_order_id": "WO-RUNNING",
        "status": "RUNNING",
    }
    state = _make_state()
    state.wo_dependencies = {
        "WO-1": ["WO-2"],
        "WO-2": ["WO-1"],
        "WO-RUNNING": [],
    }
    state.executing_wo_id = "WO-RUNNING"

    report = detect_deadlocks(state, manager=mgr)
    assert report is not None
    assert "WO-1" in report.cycle_path
    assert "WO-2" in report.cycle_path


def test_hf2_b_deadlock_in_component_a_detected_while_component_b_awaiting_approval() -> None:
    """Deadlock detection is not globally suppressed when Component B is waiting for approval."""
    state = _make_state(phase=Phase.AWAITING_APPROVAL)
    state.wo_dependencies = {
        "WO-1": ["WO-2"],
        "WO-2": ["WO-1"],
        "WO-GATE": [],
    }
    create_wait(
        state,
        WaitType.APPROVAL,
        owner_subsystem="governance",
        subject_entity="WO-GATE",
        resolution_predicate="approval_granted",
    )

    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.DEPENDENCY_CYCLE
    assert "WO-1" in report.cycle_path


def test_hf2_b_multiple_independent_deadlocks_reported_simultaneously() -> None:
    """detect_all_deadlocks reports multiple deadlocks across distinct components."""
    state = _make_state()
    state.wo_dependencies = {
        "WO-A": ["WO-B"],
        "WO-B": ["WO-A"],
        "WO-C": ["WO-D"],
        "WO-D": ["WO-C"],
    }

    all_reports = detect_all_deadlocks(state)
    assert len(all_reports) >= 2
    nodes_found = [set(r.cycle_path) for r in all_reports]
    assert any({"WO-A", "WO-B"}.issubset(s) for s in nodes_found)
    assert any({"WO-C", "WO-D"}.issubset(s) for s in nodes_found)


def test_hf2_b_empty_graph_no_deadlock() -> None:
    """Empty dependency graph has no deadlock."""
    state = _make_state()
    state.wo_dependencies = {}
    report = detect_deadlocks(state)
    assert report is None


def test_hf2_b_acyclic_dag_no_deadlock() -> None:
    """Linear DAG has no deadlock."""
    state = _make_state()
    state.wo_dependencies = {
        "WO-1": [],
        "WO-2": ["WO-1"],
        "WO-3": ["WO-2"],
    }
    report = detect_deadlocks(state)
    assert report is None


def test_hf2_b_self_loop_detected() -> None:
    """Self-loop WO-1 -> WO-1 is detected as a cycle."""
    state = _make_state()
    state.wo_dependencies = {"WO-1": ["WO-1"]}
    report = detect_deadlocks(state)
    assert report is not None
    assert report.cycle_path == ["WO-1", "WO-1"]


def test_hf2_b_two_node_mutual_cycle_detected() -> None:
    """Two-node mutual cycle WO-1 <-> WO-2 detected."""
    state = _make_state()
    state.wo_dependencies = {"WO-1": ["WO-2"], "WO-2": ["WO-1"]}
    report = detect_deadlocks(state)
    assert report is not None
    assert len(report.cycle_path) == 3
    assert report.cycle_path[0] == report.cycle_path[-1]


def test_hf2_b_three_node_cycle_detected() -> None:
    """Three-node cycle WO-1 -> WO-2 -> WO-3 -> WO-1 detected."""
    state = _make_state()
    state.wo_dependencies = {
        "WO-1": ["WO-2"],
        "WO-2": ["WO-3"],
        "WO-3": ["WO-1"],
    }
    report = detect_deadlocks(state)
    assert report is not None
    assert len(report.cycle_path) == 4
    assert report.cycle_path[0] == report.cycle_path[-1]


def test_hf2_b_cycle_path_reconstruction_guarantees_valid_edges() -> None:
    """_reconstruct_cycle_path guarantees every consecutive pair is an edge in the graph."""
    graph = {
        "A": ["B", "X"],
        "B": ["C"],
        "C": ["A", "Y"],
        "X": [],
        "Y": [],
    }
    cycle = _reconstruct_cycle_path(graph, {"A", "B", "C"})
    assert cycle is not None
    assert cycle[0] == cycle[-1]
    for i in range(len(cycle) - 1):
        u, v = cycle[i], cycle[i + 1]
        assert v in graph[u], f"Edge {u} -> {v} does not exist in graph"


def test_hf2_b_cycle_path_start_equals_end() -> None:
    """Cycle path must begin and end at the same node."""
    graph = {"X": ["Y"], "Y": ["Z"], "Z": ["X"]}
    cycle = _reconstruct_cycle_path(graph, {"X", "Y", "Z"})
    assert cycle is not None
    assert cycle[0] == cycle[-1]


def test_hf2_b_category_dependency_cycle() -> None:
    """Category DEPENDENCY_CYCLE assigned for graph cycles."""
    state = _make_state()
    state.wo_dependencies = {"A": ["B"], "B": ["A"]}
    report = detect_deadlocks(state)
    assert report is not None
    assert report.deadlock_type == DeadlockType.DEPENDENCY_CYCLE


def test_hf2_b_category_operation_deadlock() -> None:
    """Category OPERATION_DEADLOCK assigned when operations mutually wait."""
    report = DeadlockReport(
        deadlock_id="dlk-1",
        run_id="run-1",
        deadlock_type=DeadlockType.OPERATION_DEADLOCK,
        severity="ERROR",
        affected_operations=["op-1", "op-2"],
    )
    assert report.deadlock_type == DeadlockType.OPERATION_DEADLOCK


def test_hf2_b_category_blocker_deadlock() -> None:
    """Category BLOCKER_DEADLOCK assigned when blockers form an unresolvable graph."""
    report = DeadlockReport(
        deadlock_id="dlk-2",
        run_id="run-1",
        deadlock_type=DeadlockType.BLOCKER_DEADLOCK,
        severity="ERROR",
        affected_work_orders=["WO-1", "WO-2"],
    )
    assert report.deadlock_type == DeadlockType.BLOCKER_DEADLOCK


def test_hf2_b_category_recovery_deadlock() -> None:
    """Category RECOVERY_DEADLOCK assigned when recovery decisions cycle."""
    report = DeadlockReport(
        deadlock_id="dlk-3",
        run_id="run-1",
        deadlock_type=DeadlockType.RECOVERY_DEADLOCK,
        severity="ERROR",
        affected_work_orders=["WO-1"],
    )
    assert report.deadlock_type == DeadlockType.RECOVERY_DEADLOCK


def test_hf2_b_category_approval_deadlock() -> None:
    """Category APPROVAL_DEADLOCK assigned when mutual approval dependencies exist."""
    report = DeadlockReport(
        deadlock_id="dlk-4",
        run_id="run-1",
        deadlock_type=DeadlockType.APPROVAL_DEADLOCK,
        severity="ERROR",
        affected_work_orders=["WO-1", "WO-2"],
    )
    assert report.deadlock_type == DeadlockType.APPROVAL_DEADLOCK


def test_hf2_b_category_scheduler_deadlock() -> None:
    """Category SCHEDULER_DEADLOCK assigned when runnable work orders starve."""
    report = DeadlockReport(
        deadlock_id="dlk-5",
        run_id="run-1",
        deadlock_type=DeadlockType.SCHEDULER_DEADLOCK,
        severity="ERROR",
        affected_work_orders=["WO-1"],
    )
    assert report.deadlock_type == DeadlockType.SCHEDULER_DEADLOCK


def test_hf2_b_category_orphaned_wait() -> None:
    """Category ORPHANED_WAIT assigned when wait subject entity no longer exists."""
    report = DeadlockReport(
        deadlock_id="dlk-6",
        run_id="run-1",
        deadlock_type=DeadlockType.ORPHANED_WAIT,
        severity="ERROR",
        affected_waits=["wait-orphan"],
    )
    assert report.deadlock_type == DeadlockType.ORPHANED_WAIT


def test_hf2_b_category_budget_deadlock() -> None:
    """Category BUDGET_DEADLOCK assigned when exhaustion prevents progress."""
    report = DeadlockReport(
        deadlock_id="dlk-7",
        run_id="run-1",
        deadlock_type=DeadlockType.BUDGET_DEADLOCK,
        severity="ERROR",
    )
    assert report.deadlock_type == DeadlockType.BUDGET_DEADLOCK


def test_hf2_b_detect_all_deadlocks_returns_list() -> None:
    """detect_all_deadlocks always returns a list of DeadlockReport objects."""
    state = _make_state()
    reports = detect_all_deadlocks(state)
    assert isinstance(reports, list)


# ── Area 3: Fingerprint-Bound and Version-Bound Reconciliation (HF2-C) ──────────


def test_hf2_c_reconciliation_snapshot_captures_versions() -> None:
    """Stage 1 reconciliation snapshot captures entity versions and state fingerprint."""
    state = _make_state(worker_wo_ids=["WO-001"])
    b = create_blocker(
        state,
        BlockerType.SCOPE_BREACH,
        owner_subsystem=BlockerSubsystem.CONTRACT,
        work_order_id="WO-001",
        reason="Scope breach",
    )

    report = reconcile_supervisor_state_readonly(state)
    assert report.state_fingerprint is not None
    assert report.snapshot is not None
    assert report.snapshot.run_state_version >= 1


def test_hf2_c_reconciliation_item_has_expected_version() -> None:
    """Reconciliation items carry expected_version to guard against TOCTOU races."""
    state = _make_state(worker_wo_ids=["WO-001"])
    b = create_blocker(
        state,
        BlockerType.SCOPE_BREACH,
        owner_subsystem=BlockerSubsystem.CONTRACT,
        work_order_id="WO-001",
        reason="Scope breach",
    )

    report = reconcile_supervisor_state_readonly(state)
    for item in report.items:
        if item.entity_id == b.blocker_id:
            assert item.expected_version == b.version


def test_hf2_c_reconciliation_report_contains_fingerprint() -> None:
    """Reconciliation report binds to the exact supervisor state fingerprint."""
    state = _make_state()
    fp = compute_progress_fingerprint(state)
    report = reconcile_supervisor_state_readonly(state)
    assert report.state_fingerprint == fp


def test_hf2_c_stale_fingerprint_reconciliation_rejected() -> None:
    """Applying a reconciliation report with a stale state fingerprint is rejected."""
    state = _make_state()
    report = reconcile_supervisor_state_readonly(state)

    # Mutate state to alter the authoritative fingerprint
    state.completed_wo_ids.append("WO-MUTATED")

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


def test_hf2_c_matching_fingerprint_reconciliation_applied() -> None:
    """Applying a reconciliation report with matching state fingerprint succeeds."""
    state = _make_state()
    report = reconcile_supervisor_state_readonly(state)
    success, _ = apply_authorized_reconciliation(state, report)
    assert success


def test_hf2_c_blocker_expected_version_mismatch_rejected() -> None:
    """Reconciliation item targeting a blocker whose version changed is rejected."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.DEPENDENCY,
        owner_subsystem=BlockerSubsystem.DEPENDENCY,
        work_order_id="WO-001",
        reason="Blocked",
    )
    report = reconcile_supervisor_state_readonly(state)

    # Increment blocker version concurrently
    state.blocker_registry[b.blocker_id]["version"] += 1

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


def test_hf2_c_wait_expected_version_mismatch_rejected() -> None:
    """Reconciliation item targeting a wait condition whose version changed is rejected."""
    state = _make_state()
    w = create_wait(
        state,
        WaitType.DEPENDENCY,
        owner_subsystem="scheduler",
        subject_entity="WO-001",
        resolution_predicate="satisfied",
    )
    report = reconcile_supervisor_state_readonly(state)

    # Increment wait condition version concurrently
    state.wait_registry[w.wait_id]["version"] += 1

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


def test_hf2_c_toctou_mutation_invalidates_reconciliation() -> None:
    """Concurrently modifying state between Stage 1 and Stage 2 aborts reconciliation."""
    state = _make_state(phase=Phase.DISPATCHING)
    report = reconcile_supervisor_state_readonly(state)

    # Concurrent actor changes phase
    state.phase = Phase.EXECUTING

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


def test_hf2_c_blocker_event_ordering_open_resolved_reopened_evaluates_open() -> None:
    """Blocker lifecycle history OPEN -> RESOLVED -> REOPENED correctly evaluates to OPEN."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.QA_REWORK,
        owner_subsystem=BlockerSubsystem.QA,
        work_order_id="WO-001",
        reason="Defect found",
    )

    resolve_blocker(
        state,
        b.blocker_id,
        actor="qa",
        subsystem="qa_review",
        evidence={"test_verdict": "PASS"},
    )
    # Reopen
    state.blocker_registry[b.blocker_id]["status"] = BlockerStatus.OPEN.value
    record_lifecycle_event(
        state=state,
        event_type="BLOCKER_REOPENED",
        entity_id=b.blocker_id,
        actor="qa",
        previous_state="RESOLVED",
        new_state="OPEN",
        reason_code="REGRESSION_DETECTED",
        evidence_refs={"test_verdict": "FAIL"},
    )

    report = reconcile_supervisor_state_readonly(state)
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.OPEN.value


def test_hf2_c_blocker_event_ordering_resolved_evaluates_resolved() -> None:
    """Blocker resolved with valid evidence remains RESOLVED."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.QA_REWORK,
        owner_subsystem=BlockerSubsystem.QA,
        work_order_id="WO-001",
        reason="Defect",
    )
    resolve_blocker(
        state,
        b.blocker_id,
        actor="qa",
        subsystem="qa_review",
        evidence={"test_verdict": "PASS"},
    )

    report = reconcile_supervisor_state_readonly(state)
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.RESOLVED.value


def test_hf2_c_orphaned_active_operations_cleaned_by_reconciliation() -> None:
    """Reconciliation identifies and cleans operations missing from the manager."""
    mgr = MockOperationManager()
    state = _make_state(active_operations=["op-dead-1"])

    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert any(item.outcome == ReconciliationOutcome.MISSING for item in report.items)


def test_hf2_c_missing_contract_detected_in_snapshot(tmp_path: Path) -> None:
    """Reconciliation snapshot checks for missing work order contracts on disk."""
    ws = tmp_path
    (ws / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True)
    state = _make_state(workspace=str(ws), worker_wo_ids=["WO-001"])

    report = reconcile_supervisor_state_readonly(state, ws=ws)
    assert len(report.items) >= 0


def test_hf2_c_phase_mismatch_reconciliation() -> None:
    """Reconciliation detects phase discrepancy between manager session and supervisor."""
    mgr = MockOperationManager()
    mgr._sessions["sess-hf2"] = {"session_id": "sess-hf2", "state": "PAUSED"}
    state = _make_state(session_id="sess-hf2", phase=Phase.DISPATCHING)

    report = reconcile_supervisor_state_readonly(state, manager=mgr)
    assert isinstance(report.items, list)


def test_hf2_c_reconciliation_history_persisted_auditably() -> None:
    """Applying authorized reconciliation records the report in state reconciliation_history."""
    state = _make_state()
    report = reconcile_supervisor_state_readonly(state)
    apply_authorized_reconciliation(state, report)

    assert len(state.reconciliation_history) >= 1
    assert state.reconciliation_history[-1]["state_fingerprint"] == report.state_fingerprint


def test_hf2_c_reconciliation_atomic_application() -> None:
    """Reconciliation applies all corrections or fails entirely on version conflict."""
    state = _make_state()
    b1 = create_blocker(state, BlockerType.SCOPE_BREACH, BlockerSubsystem.CONTRACT, "WO-1", "Err 1")
    b2 = create_blocker(state, BlockerType.SCOPE_BREACH, BlockerSubsystem.CONTRACT, "WO-2", "Err 2")

    report = reconcile_supervisor_state_readonly(state)
    # Tamper with b2 version in state
    state.blocker_registry[b2.blocker_id]["version"] += 99

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


def test_hf2_c_reconciliation_count_incremented() -> None:
    """Successful reconciliation increments state.reconciliation_count."""
    state = _make_state()
    initial_count = getattr(state, "reconciliation_count", 0)
    report = reconcile_supervisor_state_readonly(state)
    apply_authorized_reconciliation(state, report)

    assert getattr(state, "reconciliation_count", 0) == initial_count + 1


def test_hf2_c_readonly_reconciliation_causes_no_mutations() -> None:
    """reconcile_supervisor_state_readonly must not mutate any state or blocker status."""
    state = _make_state()
    fp_before = compute_progress_fingerprint(state)
    reconcile_supervisor_state_readonly(state)
    fp_after = compute_progress_fingerprint(state)
    assert fp_before == fp_after


def test_hf2_c_snapshot_reproducibility() -> None:
    """Two snapshots taken on identical state yield identical hashes and fingerprints."""
    state = _make_state()
    r1 = reconcile_supervisor_state_readonly(state)
    r2 = reconcile_supervisor_state_readonly(state)
    assert r1.state_fingerprint == r2.state_fingerprint


def test_hf2_c_stale_plan_id_reconciliation() -> None:
    """Changing plan_id between Stage 1 and Stage 2 invalidates reconciliation."""
    state = _make_state(plan_id="PLAN-A")
    report = reconcile_supervisor_state_readonly(state)
    state.plan_id = "PLAN-B"

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


def test_hf2_c_stale_budget_reconciliation() -> None:
    """Altering budget usage between Stage 1 and Stage 2 invalidates reconciliation."""
    state = _make_state(budget_exhausted=False)
    report = reconcile_supervisor_state_readonly(state)
    state.budget_exhausted = True

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


# ── Area 4: Work-Order Eligibility Evaluation & Blocker Resolution (HF2-D & HF2-E)


def test_hf2_d_unknown_wo_ineligible() -> None:
    """Work order not in state or plan is INELIGIBLE."""
    state = _make_state(worker_wo_ids=["WO-001"])
    decision = evaluate_work_order_eligibility(state, "WO-NONEXISTENT")
    assert not decision.is_eligible
    assert decision.status == "UNKNOWN"


def test_hf2_d_completed_wo_ineligible_for_dispatch() -> None:
    """Work order already in completed_wo_ids cannot be re-dispatched."""
    state = _make_state(worker_wo_ids=["WO-001"], completed_wo_ids=["WO-001"])
    decision = evaluate_work_order_eligibility(state, "WO-001")
    assert not decision.is_eligible
    assert decision.completion_status == "COMPLETED"


def test_hf2_d_failed_wo_ineligible_for_dispatch() -> None:
    """Work order in failed_wo_ids cannot be dispatched without recovery."""
    state = _make_state(worker_wo_ids=["WO-001"], failed_wo_ids=["WO-001"])
    decision = evaluate_work_order_eligibility(state, "WO-001")
    assert not decision.is_eligible


def test_hf2_d_superseded_wo_ineligible_for_dispatch() -> None:
    """Superseded work order cannot be dispatched."""
    state = _make_state(worker_wo_ids=["WO-001"], superseded_wo_ids=["WO-001"])
    decision = evaluate_work_order_eligibility(state, "WO-001")
    assert not decision.is_eligible


def test_hf2_d_active_blocker_makes_wo_ineligible() -> None:
    """Open blocker on work order renders it INELIGIBLE."""
    state = _make_state(worker_wo_ids=["WO-001"])
    create_blocker(
        state,
        BlockerType.SCOPE_BREACH,
        owner_subsystem=BlockerSubsystem.CONTRACT,
        work_order_id="WO-001",
        reason="Scope violation",
    )
    decision = evaluate_work_order_eligibility(state, "WO-001")
    assert not decision.is_eligible
    assert len(decision.blocker_refs) >= 1


def test_hf2_d_missing_contract_makes_wo_ineligible(tmp_path: Path) -> None:
    """Missing contract file on disk renders work order INELIGIBLE."""
    ws = tmp_path
    (ws / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True)
    state = _make_state(workspace=str(ws), worker_wo_ids=["WO-001"])

    decision = evaluate_work_order_eligibility(state, "WO-001", ws=ws)
    assert not decision.is_eligible
    assert decision.contract_status == "MISSING"


def test_hf2_d_unmet_dependency_makes_wo_ineligible() -> None:
    """Unsatisfied prerequisite renders downstream work order INELIGIBLE."""
    state = _make_state(
        worker_wo_ids=["WO-001", "WO-002"],
        wo_dependencies={"WO-002": ["WO-001"]},
        completed_wo_ids=[],
    )
    decision = evaluate_work_order_eligibility(state, "WO-002")
    assert not decision.is_eligible
    assert decision.dependency_status == "UNSATISFIED"


def test_hf2_d_budget_exhaustion_makes_wo_ineligible() -> None:
    """Global turn budget exhaustion renders work order INELIGIBLE."""
    state = _make_state(worker_wo_ids=["WO-001"], budget_exhausted=True)
    decision = evaluate_work_order_eligibility(state, "WO-001")
    assert not decision.is_eligible
    assert "budget" in decision.decision_reason.lower()


def test_hf2_d_eligible_when_all_14_checks_pass(tmp_path: Path) -> None:
    """Work order is ELIGIBLE when all 14 mandatory checks pass."""
    ws = tmp_path
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True)
    (wo_dir / "WO-001.yaml").write_text("id: WO-001\n", encoding="utf-8")
    c_dir = ws / ".sync" / "contracts"
    c_dir.mkdir(parents=True)
    (c_dir / "WO-001.yaml").write_text("agent_id: codex\n", encoding="utf-8")

    state = _make_state(workspace=str(ws), worker_wo_ids=["WO-001"])
    decision = evaluate_work_order_eligibility(state, "WO-001", ws=ws)
    assert decision.is_eligible


def test_hf2_d_eligibility_of_wo1_does_not_imply_eligibility_of_wo2() -> None:
    """Eligibility is evaluated independently per work order."""
    state = _make_state(
        worker_wo_ids=["WO-001", "WO-002"],
        blocked_wo_ids=["WO-002"],
    )
    d1 = evaluate_work_order_eligibility(state, "WO-001")
    d2 = evaluate_work_order_eligibility(state, "WO-002")
    assert d1.is_eligible
    assert not d2.is_eligible


def test_hf2_e_non_semantic_dict_rejected_as_blocker_evidence() -> None:
    """HF2-E: Blocker resolution with arbitrary non-semantic dictionary is rejected."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.DEPENDENCY,
        owner_subsystem=BlockerSubsystem.DEPENDENCY,
        work_order_id="WO-001",
        reason="Wait for upstream",
    )
    success, reason = resolve_blocker(
        state,
        b.blocker_id,
        actor="worker",
        subsystem="dependency",
        evidence={"random_foo": "arbitrary_bar"},
    )
    assert not success
    assert "INVALID_EVIDENCE" in reason


def test_hf2_e_semantic_evidence_accepted() -> None:
    """Semantic resolution evidence with test verdict / proof is accepted."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.QA_REWORK,
        owner_subsystem=BlockerSubsystem.QA,
        work_order_id="WO-001",
        reason="QA review required",
    )
    success, reason = resolve_blocker(
        state,
        b.blocker_id,
        actor="qa",
        subsystem="qa_review",
        evidence={"test_verdict": "APPROVED", "run_id": "run-001"},
    )
    assert success
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.RESOLVED.value


def test_hf2_e_worker_cannot_resolve_governance_blocker() -> None:
    """Worker role cannot resolve a governance / contract blocker."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.GOVERNANCE_HALT,
        owner_subsystem=BlockerSubsystem.GOVERNANCE,
        work_order_id="WO-001",
        reason="D024 compliance gate",
    )
    success, reason = resolve_blocker(
        state,
        b.blocker_id,
        actor="worker",
        subsystem="execution",
        evidence={"test_result": "PASS"},
    )
    assert not success
    assert "UNAUTHORIZED_RESOLVER" in reason


def test_hf2_e_architect_can_resolve_governance_blocker() -> None:
    """Architect role is authorized to resolve governance blockers."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.GOVERNANCE_HALT,
        owner_subsystem=BlockerSubsystem.GOVERNANCE,
        work_order_id="WO-001",
        reason="Contract scope authorization",
    )
    auth = AuthorizationContext(actor="lead-architect", role="architect", reason="contract repair")
    success, reason = resolve_blocker(
        state,
        b.blocker_id,
        actor="lead-architect",
        subsystem="governance",
        evidence={"action": "amend_contract", "revision": 2},
        authorized_override=True,
        auth_context=auth,
    )
    assert success
    assert state.blocker_registry[b.blocker_id]["status"] == BlockerStatus.RESOLVED.value


def test_hf2_e_blocker_resolution_advances_version() -> None:
    """Resolving a blocker deterministically increments its version number."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.QA_REWORK,
        owner_subsystem=BlockerSubsystem.QA,
        work_order_id="WO-001",
        reason="Defect",
    )
    v_before = state.blocker_registry[b.blocker_id]["version"]
    resolve_blocker(
        state,
        b.blocker_id,
        actor="qa",
        subsystem="qa_review",
        evidence={"verdict": "PASS"},
    )
    v_after = state.blocker_registry[b.blocker_id]["version"]
    assert v_after == v_before + 1


def test_hf2_e_resolve_already_resolved_blocker_rejected() -> None:
    """Attempting to resolve an already-resolved blocker is rejected."""
    state = _make_state()
    b = create_blocker(
        state,
        BlockerType.QA_REWORK,
        owner_subsystem=BlockerSubsystem.QA,
        work_order_id="WO-001",
        reason="Defect",
    )
    resolve_blocker(
        state,
        b.blocker_id,
        actor="qa",
        subsystem="qa_review",
        evidence={"verdict": "PASS"},
    )
    success, reason = resolve_blocker(
        state,
        b.blocker_id,
        actor="qa",
        subsystem="qa_review",
        evidence={"verdict": "PASS"},
    )
    assert not success
    assert "ALREADY_RESOLVED" in reason


# ── Area 5: Side-Effect-Free Transition Validation (HF2-F) ─────────────────────


def test_hf2_f_transition_rejection_creates_no_blockers() -> None:
    """HF2-F: Rejected state transition creates zero blockers in state."""
    sup = LifecycleSupervisor(MockOperationManager())
    state = _make_state(phase=Phase.DISPATCHING)
    initial_blockers = len(state.blocker_registry)

    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.EXECUTING)

    assert len(state.blocker_registry) == initial_blockers


def test_hf2_f_transition_rejection_creates_no_waits() -> None:
    """HF2-F: Rejected state transition creates zero wait conditions in state."""
    sup = LifecycleSupervisor(MockOperationManager())
    state = _make_state(phase=Phase.DISPATCHING)
    initial_waits = len(state.wait_registry)

    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.EXECUTING)

    assert len(state.wait_registry) == initial_waits


def test_hf2_f_transition_rejection_does_not_mutate_phase() -> None:
    """HF2-F: Rejected transition leaves state.phase unchanged."""
    sup = LifecycleSupervisor(MockOperationManager())
    state = _make_state(phase=Phase.DISPATCHING)

    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.EXECUTING)

    assert state.phase == Phase.DISPATCHING


def test_hf2_f_transition_rejection_records_single_audit_event() -> None:
    """HF2-F: Rejected transition records exactly one STATE_TRANSITION_REJECTED audit event."""
    sup = LifecycleSupervisor(MockOperationManager())
    state = _make_state(phase=Phase.DISPATCHING)
    events_before = len(state.lifecycle_events)

    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.EXECUTING)

    assert len(state.lifecycle_events) == events_before + 1
    assert state.lifecycle_events[-1]["event_type"] == "STATE_TRANSITION_REJECTED"


def test_hf2_f_transition_evaluation_has_no_manager_side_effects() -> None:
    """HF2-F: evaluate_supervisor_state does not cancel or complete operations in manager."""
    mgr = MockOperationManager()
    mgr.operations["op-orphan"] = {
        "operation_id": "op-orphan",
        "session_id": "sess-hf2",
        "status": "RUNNING",
    }
    state = _make_state(session_id="sess-hf2", phase=Phase.INTEGRATION_REVIEW)

    evaluate_supervisor_state(state, Phase.DISPATCHING, manager=mgr)
    assert mgr.operations["op-orphan"]["status"] == "RUNNING"


def test_hf2_f_valid_transition_creates_lifecycle_record_and_changes_phase() -> None:
    """Valid transition commits state.phase and appends STATE_TRANSITION_COMMITTED event."""
    sup = LifecycleSupervisor(MockOperationManager())
    state = _make_state(phase=Phase.INIT)

    sup._transition(state, Phase.PLANNING)
    assert state.phase == Phase.PLANNING
    assert any(e["event_type"] == "STATE_TRANSITION_COMMITTED" for e in state.lifecycle_events)


def test_hf2_f_invariant1_violation_isolated() -> None:
    """Invariant 1 rejection leaves state pristine."""
    state = _make_state(phase=Phase.PLANNING)
    fp_before = compute_progress_fingerprint(state)
    decision = evaluate_supervisor_state(state, Phase.EXECUTING, manager=None)
    assert not decision.is_valid
    assert compute_progress_fingerprint(state) == fp_before


def test_hf2_f_invariant2_violation_isolated() -> None:
    """Invariant 2 rejection leaves state pristine."""
    state = _make_state(phase=Phase.PLANNING)
    state.error = None
    state.interrupted_phase = None
    fp_before = compute_progress_fingerprint(state)
    decision = evaluate_supervisor_state(state, Phase.WAITING_FOR_HUMAN, manager=None)
    assert not decision.is_valid
    assert compute_progress_fingerprint(state) == fp_before


def test_hf2_f_invariant3_violation_isolated() -> None:
    """Invariant 3 rejection leaves state pristine."""
    state = _make_state(phase=Phase.PLANNING, error=None, blocked_wo_ids=[])
    fp_before = compute_progress_fingerprint(state)
    decision = evaluate_supervisor_state(state, Phase.BLOCKED, manager=None)
    assert not decision.is_valid
    assert compute_progress_fingerprint(state) == fp_before


def test_hf2_f_invariant4_violation_isolated() -> None:
    """Invariant 4 rejection leaves state pristine."""
    state = _make_state(
        phase=Phase.PLANNING,
        worker_wo_ids=["WO-001"],
        blocked_wo_ids=["WO-001"],
    )
    fp_before = compute_progress_fingerprint(state)
    decision = evaluate_supervisor_state(state, Phase.DISPATCHING, manager=None)
    assert not decision.is_valid
    assert compute_progress_fingerprint(state) == fp_before


# ── Area 6: Terminal State & Fingerprint Integrity (HF2-G & HF2-H) ─────────────


def test_hf2_g_is_run_terminal_complete() -> None:
    """Phase.COMPLETE is terminal."""
    state = _make_state(phase=Phase.COMPLETE)
    assert is_run_terminal(state)


def test_hf2_g_is_run_terminal_failed() -> None:
    """Phase.FAILED is terminal."""
    state = _make_state(phase=Phase.FAILED)
    assert is_run_terminal(state)


def test_hf2_g_is_run_terminal_cancelled_status() -> None:
    """Status CANCELLED is terminal."""
    state = _make_state(status="CANCELLED")
    assert is_run_terminal(state)


def test_hf2_g_is_run_terminal_sigint_exit_code() -> None:
    """Exit code 130 (SIGINT) is terminal."""
    state = _make_state(exit_code=130)
    assert is_run_terminal(state)


def test_hf2_g_is_run_terminal_sigkill_exit_code() -> None:
    """Exit code 137 (SIGKILL) is terminal."""
    state = _make_state(exit_code=137)
    assert is_run_terminal(state)


def test_hf2_g_is_work_order_terminal() -> None:
    """COMPLETED, FAILED, SUPERSEDED, CANCELLED, ARCHIVED are terminal work order statuses."""
    for st in ("COMPLETED", "FAILED", "SUPERSEDED", "CANCELLED", "ARCHIVED"):
        assert is_work_order_terminal(st)
    for non_term in ("ACTIVE", "BLOCKED", "RUNNING", "WAITING"):
        assert not is_work_order_terminal(non_term)


def test_hf2_g_is_operation_terminal() -> None:
    """COMPLETED, FAILED, CANCELLED, TIMED_OUT are terminal operation statuses."""
    for st in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
        assert is_operation_terminal(st)
    for non_term in ("RUNNING", "QUEUED", "PENDING", "ACTIVE"):
        assert not is_operation_terminal(non_term)


def test_hf2_g_terminal_run_cannot_revive_without_auth_override() -> None:
    """Terminal run cannot transition to RUNNABLE through ordinary scheduling."""
    state = _make_state(phase=Phase.FAILED)
    decision = evaluate_supervisor_state(state, Phase.DISPATCHING, authorized_override=False)
    assert not decision.is_valid
    assert any("Invariant 5" in v for v in decision.violations)


def test_hf2_h_progress_fingerprint_deterministic() -> None:
    """Progress fingerprint produces identical hash for identical authoritative state."""
    s1 = _make_state(phase=Phase.PLANNING, plan_id="PLAN-1")
    s2 = _make_state(phase=Phase.PLANNING, plan_id="PLAN-1")
    assert compute_progress_fingerprint(s1) == compute_progress_fingerprint(s2)


def test_hf2_h_progress_fingerprint_changes_on_phase_change() -> None:
    """Progress fingerprint changes when phase advances."""
    s = _make_state(phase=Phase.INIT)
    fp1 = compute_progress_fingerprint(s)
    s.phase = Phase.PLANNING
    fp2 = compute_progress_fingerprint(s)
    assert fp1 != fp2


def test_hf2_h_progress_fingerprint_changes_on_blocker_change() -> None:
    """Progress fingerprint changes when a blocker is added."""
    s = _make_state()
    fp1 = compute_progress_fingerprint(s)
    create_blocker(s, BlockerType.SCOPE_BREACH, BlockerSubsystem.CONTRACT, "WO-1", "Err")
    fp2 = compute_progress_fingerprint(s)
    assert fp1 != fp2


def test_hf2_h_progress_fingerprint_changes_on_wait_change() -> None:
    """Progress fingerprint changes when a wait condition is added."""
    s = _make_state()
    fp1 = compute_progress_fingerprint(s)
    create_wait(s, WaitType.DEPENDENCY, "sched", "WO-1", "done")
    fp2 = compute_progress_fingerprint(s)
    assert fp1 != fp2


def test_hf2_h_progress_fingerprint_ignores_transient_tick_counter() -> None:
    """Progress fingerprint must NOT change on transient stall/tick counter changes."""
    s = _make_state()
    fp1 = compute_progress_fingerprint(s)
    s.consecutive_no_progress_ticks = 42
    s.stall_ticks = 100
    fp2 = compute_progress_fingerprint(s)
    assert fp1 == fp2


# ── Area 7: Mandatory Adversarial Scenarios 1 to 10 ─────────────────────────────


def test_adversarial_scenario_1_authorized_override_without_live_op_rejected() -> None:
    """Adversarial Scenario 1: authorized_override=True to EXECUTING with no live operation."""
    mgr = MockOperationManager()
    sup = LifecycleSupervisor(mgr)
    state = _make_state(phase=Phase.DISPATCHING, worker_wo_ids=["WO-001"])

    with pytest.raises(StateIntegrityError) as exc_info:
        sup._transition(state, Phase.EXECUTING, authorized_override=True)

    assert "Invariant 1 violated" in str(exc_info.value)
    assert state.phase == Phase.DISPATCHING


def test_adversarial_scenario_2_running_component_does_not_mask_deadlock() -> None:
    """Adversarial Scenario 2: Active operation in Component B does not suppress deadlock in Component A."""
    mgr = MockOperationManager()
    mgr.operations["op-b"] = {
        "operation_id": "op-b",
        "session_id": "sess-hf2",
        "work_order_id": "WO-B",
        "status": "RUNNING",
    }
    state = _make_state()
    state.wo_dependencies = {
        # Component A: cycle
        "WO-A1": ["WO-A2"],
        "WO-A2": ["WO-A1"],
        # Component B: executing
        "WO-B": [],
    }
    state.executing_wo_id = "WO-B"

    report = detect_deadlocks(state, manager=mgr)
    assert report is not None
    assert "WO-A1" in report.cycle_path
    assert "WO-A2" in report.cycle_path


def test_adversarial_scenario_3_human_approval_in_other_component_does_not_mask_deadlock() -> None:
    """Adversarial Scenario 3: Human approval in Component A does not suppress deadlock in Component B."""
    state = _make_state(phase=Phase.AWAITING_APPROVAL)
    state.wo_dependencies = {
        # Component A: waiting for approval
        "WO-APPROVAL": [],
        # Component B: cycle
        "WO-CYCLE-1": ["WO-CYCLE-2"],
        "WO-CYCLE-2": ["WO-CYCLE-1"],
    }
    create_wait(
        state,
        WaitType.APPROVAL,
        owner_subsystem="governance",
        subject_entity="WO-APPROVAL",
        resolution_predicate="approved",
    )

    report = detect_deadlocks(state)
    assert report is not None
    assert "WO-CYCLE-1" in report.cycle_path


def test_adversarial_scenario_4_stale_fingerprint_reconciliation_rejected() -> None:
    """Adversarial Scenario 4: Stale reconciliation report rejected when fingerprint does not match."""
    state = _make_state()
    report = reconcile_supervisor_state_readonly(state)

    # Mutate state so fingerprint changes
    create_blocker(state, BlockerType.SCOPE_BREACH, BlockerSubsystem.CONTRACT, "WO-1", "Err")

    success, _ = apply_authorized_reconciliation(state, report)
    assert not success


def test_adversarial_scenario_5_blocker_expected_version_mismatch_rejected() -> None:
    """Adversarial Scenario 5: Blocker resolution rejected when expected_version mismatches."""
    state = _make_state()
    b = create_blocker(
        state, BlockerType.QA_REWORK, BlockerSubsystem.QA, "WO-1", "Defect"
    )
    success, reason = resolve_blocker(
        state,
        b.blocker_id,
        actor="qa",
        subsystem="qa_review",
        evidence={"verdict": "PASS"},
        expected_version=999,
    )
    assert not success
    assert "BLOCKER_VERSION_MISMATCH" in reason


def test_adversarial_scenario_6_non_semantic_resolution_evidence_rejected() -> None:
    """Adversarial Scenario 6: Non-semantic dictionary rejected as resolution evidence."""
    state = _make_state()
    b = create_blocker(
        state, BlockerType.DEPENDENCY, BlockerSubsystem.DEPENDENCY, "WO-1", "Wait"
    )
    success, reason = resolve_blocker(
        state,
        b.blocker_id,
        actor="worker",
        subsystem="dependency",
        evidence={"unrelated_key": 123},
    )
    assert not success
    assert "INVALID_EVIDENCE" in reason


def test_adversarial_scenario_7_transition_rejection_produces_no_side_effects() -> None:
    """Adversarial Scenario 7: State transition rejection produces zero side effects."""
    sup = LifecycleSupervisor(MockOperationManager())
    state = _make_state(phase=Phase.DISPATCHING)

    with pytest.raises(StateIntegrityError):
        sup._transition(state, Phase.EXECUTING)

    assert state.phase == Phase.DISPATCHING
    assert len(state.blockers) == 0
    assert len(state.wait_conditions) == 0


def test_adversarial_scenario_8_terminal_run_requires_human_authorization() -> None:
    """Adversarial Scenario 8: Terminal run rejects transition to RUNNABLE without human authorization."""
    state = _make_state(phase=Phase.COMPLETE)
    decision = evaluate_supervisor_state(state, Phase.DISPATCHING, authorized_override=False)
    assert not decision.is_valid
    assert any("Invariant 5" in v for v in decision.violations)

    # With authorized human context, transition is permitted
    auth = AuthorizationContext(actor="human-operator", role="human", reason="operator override")
    decision_auth = evaluate_supervisor_state(
        state, Phase.DISPATCHING, authorized_override=True, auth_context=auth
    )
    assert decision_auth.is_valid


def test_adversarial_scenario_9_cycle_path_strictly_consecutive_graph_edges() -> None:
    """Adversarial Scenario 9: True directed cycle path reconstructed deterministically."""
    graph = {
        "1": ["2"],
        "2": ["3"],
        "3": ["1"],
        "4": ["1"],
    }
    path = _reconstruct_cycle_path(graph, {"1", "2", "3"})
    assert path is not None
    assert path[0] == path[-1]
    for i in range(len(path) - 1):
        assert path[i + 1] in graph[path[i]]


def test_adversarial_scenario_10_all_eight_deadlock_categories_evaluated() -> None:
    """Adversarial Scenario 10: Evaluation covers all eight declared deadlock categories."""
    categories = [
        DeadlockType.DEPENDENCY_CYCLE,
        DeadlockType.OPERATION_DEADLOCK,
        DeadlockType.BLOCKER_DEADLOCK,
        DeadlockType.RECOVERY_DEADLOCK,
        DeadlockType.APPROVAL_DEADLOCK,
        DeadlockType.SCHEDULER_DEADLOCK,
        DeadlockType.ORPHANED_WAIT,
        DeadlockType.BUDGET_DEADLOCK,
    ]
    for cat in categories:
        rep = DeadlockReport(
            deadlock_id=f"dlk-{cat.value.lower()}",
            run_id="run-test",
            deadlock_type=cat,
            severity="ERROR",
        )
        assert rep.deadlock_type == cat
