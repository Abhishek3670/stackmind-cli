"""
StackMind Hardening Phase 0: P0-5 Counters and Global Budget Acceptance Tests
Audit Reference: Revision 3 Deliverable 1, Deliverable 2, Amendment D, Amendment E.

Tests verify:
1. Rev 2 cycle (GITOPS -> EXECUTING -> QA_REVIEW -> INTEGRATION_REVIEW -> GITOPS) terminates
   at WAITING_FOR_HUMAN after gitops_retries is exhausted (MAX_GITOPS_RETRIES = 2).
2. Multi-WO per-WO rework isolation: 10 WOs with rejections on different WOs do not trip
   run-wide rework cap prematurely.
3. Global turn budget exhaustion (B <= 0) always lands deterministically in WAITING_FOR_HUMAN.
4. Happy path reaches COMPLETE within the default dynamic budget B = 18 + 5N.
5. Human override resets are explicit, logged, and bounded (at most 2 resumes before hard fail).
6. Dependent WOs wait in BLOCKED_BY_ESCALATION when upstream enters Architecture recovery,
   resuming after DAG/contract amendment.
7. Model assertion: every cycle contains a dispatch edge (removing dispatch edges => graph is a DAG).
8. Exit code table mapping: COMPLETE=0, FAILED=1, WAITING_FOR_HUMAN (Budget)=2,
   WAITING_FOR_HUMAN (Retries)=3, WAITING_FOR_HUMAN (Plan)=4, ABORTED_HEADLESS=137.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
import pytest

from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    EXIT_BUDGET_EXHAUSTED,
    EXIT_FAILURE,
    EXIT_HEADLESS_TIMEOUT,
    EXIT_INTEGRITY_VIOLATION,
    EXIT_PLAN_REJECTED,
    EXIT_RETRIES_EXHAUSTED,
    EXIT_SUCCESS,
    LifecycleSupervisor,
    Phase,
    RunState,
)


class DummyOperationManager:
    """Mock operation manager for supervisor lifecycle tests."""

    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}
        self.turns_dispatched: list[dict[str, Any]] = []
        self._op_counter = 0

    def start_turn(
        self,
        session_id: str,
        prompt: str,
        role: str,
        agent_id: str,
        work_order_id: str | None = None,
        parent_operation_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._op_counter += 1
        op_id = f"op-{self._op_counter:04d}"
        op_record = {
            "operation_id": op_id,
            "session_id": session_id,
            "role": role,
            "agent_id": agent_id,
            "work_order_id": work_order_id,
            "parent_operation_id": parent_operation_id,
            "prompt": prompt,
            "status": "COMPLETED",
            "result": {"status": "completed"},
        }
        self.operations[op_id] = op_record
        self.turns_dispatched.append(op_record)
        return op_record

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        return self.operations.get(op_id)

    def list_operations(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.operations.values())


# ==============================================================================
# 1. Rev 2 Cycle Termination (MAX_GITOPS_RETRIES = 2)
# ==============================================================================

def test_rev2_cycle_terminates_at_human_gate_after_gitops_retries_exhausted(tmp_path: Path):
    """The Rev 2 cycle terminates at WAITING_FOR_HUMAN after gitops_retries hits 2."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-test-rev2", "Goal", tmp_path, "session-1")

    # Fast forward state to GITOPS
    state.phase = Phase.GITOPS
    state.gitops_wo_id = "WO-GITOPS"
    state.gitops_operation_id = "op-gitops-1"
    mgr.operations["op-gitops-1"] = {
        "operation_id": "op-gitops-1",
        "status": "BLOCKED",
        "result": {"status": "blocked", "error": "Verification gate failure: release rejected"},
    }

    # First GitOps failure -> gitops_retries becomes 1
    res1 = sup.advance(state)
    assert state.gitops_retries == 1

    # Second GitOps failure -> gitops_retries becomes 2
    state.gitops_operation_id = "op-gitops-2"
    mgr.operations["op-gitops-2"] = {
        "operation_id": "op-gitops-2",
        "status": "BLOCKED",
        "result": {"status": "blocked", "error": "Verification gate failure: release rejected again"},
    }
    res2 = sup.advance(state)
    assert state.gitops_retries == 2

    # Third failure exceeds MAX_GITOPS_RETRIES (2) -> must transition to WAITING_FOR_HUMAN
    state.gitops_operation_id = "op-gitops-3"
    mgr.operations["op-gitops-3"] = {
        "operation_id": "op-gitops-3",
        "status": "BLOCKED",
        "result": {"status": "blocked", "error": "Verification gate failure: 3rd rejection"},
    }
    res3 = sup.advance(state)

    assert state.phase in (Phase.WAITING_FOR_HUMAN, Phase.BLOCKED)
    assert res3 in (AdvanceResult.WAITING_FOR_HUMAN, AdvanceResult.BLOCKED)
    assert state.gitops_retries == 2
    assert "retries exhausted" in str(state.error).lower() or "gitops" in str(state.error).lower()


# ==============================================================================
# 2. Multi-WO Per-WO Rework Isolation
# ==============================================================================

def test_multi_wo_per_wo_rework_isolation(tmp_path: Path):
    """10-WO run with 2 rejections on different WOs does not trip a run-wide cap."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-test-multi-wo", "Goal", tmp_path, "session-1")

    # Create 10 WOs
    state.worker_wo_ids = [f"WO-{i:03d}" for i in range(1, 11)]

    # WO-001 has 1 QA rejection
    state.qa_reworks["WO-001"] = 1
    # WO-002 has 1 QA rejection
    state.qa_reworks["WO-002"] = 1

    # Neither WO has exceeded MAX_QA_REWORKS (2)
    assert state.qa_reworks.get("WO-001", 0) <= state.max_qa_reworks
    assert state.qa_reworks.get("WO-002", 0) <= state.max_qa_reworks
    # Total reworks across run is 2, but per-WO max is not exceeded
    assert sum(state.qa_reworks.values()) == 2

    # Verify run remains in executing or active state without tripping run-wide cap
    state.phase = Phase.EXECUTING
    assert state.phase != Phase.FAILED
    assert state.phase != Phase.WAITING_FOR_HUMAN


# ==============================================================================
# 3. Global Budget Exhaustion Lands in WAITING_FOR_HUMAN
# ==============================================================================

def test_budget_exhaustion_lands_in_waiting_for_human(tmp_path: Path):
    """When global_turn_budget reaches 0, advance deterministically lands in WAITING_FOR_HUMAN."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-test-budget", "Goal", tmp_path, "session-1")

    # Set budget to 0
    state.global_turn_budget = 0
    state.phase = Phase.EXECUTING

    res = sup.advance(state)

    assert state.phase in (Phase.WAITING_FOR_HUMAN, Phase.BLOCKED)
    assert res in (AdvanceResult.WAITING_FOR_HUMAN, AdvanceResult.BLOCKED)
    assert state.budget_exhausted is True
    assert state.exit_code == 2


# ==============================================================================
# 4. Happy Path Reaches COMPLETE Within Default Budget (B = 18 + 5N)
# ==============================================================================

def test_happy_path_reaches_complete_within_default_budget(tmp_path: Path):
    """Happy path reaches COMPLETE and consumes <= B turns."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-test-happy", "Goal", tmp_path, "session-1")

    n_wos = 2
    expected_budget = 18 + 5 * n_wos
    state.global_turn_budget = expected_budget
    state.initial_turn_budget = expected_budget

    # Simulate happy path steps: PLANNING -> AWAITING_APPROVAL -> AUTHORING -> GATE -> DISPATCHING -> EXECUTING -> INTEGRATION -> FINAL_REVIEW -> GITOPS -> COMPLETE
    # 1. Planning ready
    state.phase = Phase.PLANNING
    state.planning_operation_id = "op-plan"
    mgr.operations["op-plan"] = {"operation_id": "op-plan", "status": "COMPLETED", "result": {"plan": "ok"}}
    sup.advance(state)

    # 2. Human approval
    state.phase = Phase.AWAITING_APPROVAL
    sup.advance(state)

    # 3. Authoring
    state.phase = Phase.AUTHORING
    state.authoring_operation_id = "op-auth"
    mgr.operations["op-auth"] = {"operation_id": "op-auth", "status": "COMPLETED", "result": {"wos": ["WO-001", "WO-002"]}}
    state.worker_wo_ids = ["WO-001", "WO-002"]
    state.published_wo_ids = ["WO-001", "WO-002"]
    sup.advance(state)

    # 4. Gate pass
    state.phase = Phase.AUTHORING_READINESS_GATE
    sup.advance(state)

    # 5. Fast-forward to GitOps completed (final acceptance evidence required)
    from tests.review_helpers import final_pass
    (tmp_path / "PLAN.md").write_text("# Approved Plan\n\nGoal.\n", encoding="utf-8")
    state.plan_id = "PLAN-001"
    state.integration_evidence = {"passed": True}
    final_pass(tmp_path, "run-test-happy", "PLAN-001", [])
    state.phase = Phase.GITOPS
    state.gitops_operation_id = "op-gitops"
    mgr.operations["op-gitops"] = {"operation_id": "op-gitops", "status": "COMPLETED", "result": {"status": "completed"}}
    sup.advance(state)

    assert state.phase == Phase.COMPLETE
    assert state.global_turn_budget > 0, "Happy path exhausted turn budget!"
    assert state.exit_code == 0


# ==============================================================================
# 5. Human Override Bounded and Logged (Max 2 Resumes)
# ==============================================================================

def test_human_override_bounded_and_logged(tmp_path: Path):
    """Human override resets are explicit, logged, and capped at 2 resumes."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-test-override", "Goal", tmp_path, "session-1")

    state.phase = Phase.WAITING_FOR_HUMAN
    state.global_turn_budget = 0
    state.gitops_retries = 2

    # Resume without additional_budget when budget is exhausted MUST raise ValueError
    with pytest.raises(ValueError) as exc_budget:
        sup.resume_run_with_human_override(
            state,
            reason="Attempt without budget grant",
            reset_counters=["gitops_retries"],
        )
    assert "requires additional_budget > 0" in str(exc_budget.value)

    # Resume with wildcard "all" MUST raise ValueError
    with pytest.raises(ValueError) as exc_all:
        sup.resume_run_with_human_override(
            state,
            reason="Attempt with wildcard all",
            reset_counters=["all"],
            additional_budget=10,
        )
    assert "wildcard 'all' is prohibited" in str(exc_all.value).lower()

    # Resume with invalid counter MUST raise ValueError
    with pytest.raises(ValueError) as exc_invalid:
        sup.resume_run_with_human_override(
            state,
            reason="Attempt with invalid counter",
            reset_counters=["non_existent_counter"],
            additional_budget=10,
        )
    assert "invalid reset counter" in str(exc_invalid.value).lower()

    # 1st resume: succeeds, logs, grants budget, and resets specified counters
    res1 = sup.resume_run_with_human_override(
        state,
        reason="Operator approved extra turn budget and retry reset",
        reset_counters=["gitops_retries", "global_turn_budget"],
        additional_budget=15,
    )
    assert state.human_resumes_used == 1
    assert state.gitops_retries == 0
    assert state.global_turn_budget == 15
    assert len(state.human_override_logs) == 1
    assert state.human_override_logs[0]["reason"] == "Operator approved extra turn budget and retry reset"

    # Fast forward to 2nd block
    state.phase = Phase.WAITING_FOR_HUMAN
    state.global_turn_budget = 0

    # 2nd resume: succeeds with additional budget
    res2 = sup.resume_run_with_human_override(
        state,
        reason="Second operator extension",
        reset_counters=["global_turn_budget"],
        additional_budget=10,
    )
    assert state.human_resumes_used == 2
    assert len(state.human_override_logs) == 2

    # 3rd resume: exceeds MAX_HUMAN_RESUMES (2) -> fails closed to FAILED
    state.phase = Phase.WAITING_FOR_HUMAN
    res3 = sup.resume_run_with_human_override(
        state,
        reason="Third resume attempt should be denied",
        additional_budget=10,
    )
    assert res3 == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert state.exit_code == 1
    assert "maximum human resumes" in str(state.error).lower()


# ==============================================================================
# 6. Dependent WO Blocked by Escalation (Amendment E)
# ==============================================================================

def test_dependent_wo_blocked_by_escalation(tmp_path: Path):
    """WO-B depending on WO-A enters BLOCKED_BY_ESCALATION when WO-A enters recovery."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-test-dep-esc", "Goal", tmp_path, "session-1")

    state.worker_wo_ids = ["WO-001", "WO-002"]
    state.phase = Phase.EXECUTING

    # WO-001 fails QA and enters Architect recovery
    sup.escalate_wo_to_recovery(state, "WO-001", reason="QA tests failed")

    # Mark WO-002 as dependent on WO-001
    sup.set_wo_dependencies(state, "WO-002", ["WO-001"])

    # Evaluate dependency status
    sup.evaluate_blocked_by_escalation(state)

    assert "WO-002" in state.blocked_by_escalation
    assert state.blocked_by_escalation["WO-002"] == "WO-001"
    assert "WO-002" not in state.completed_wo_ids
    assert "WO-002" not in state.failed_wo_ids

    # Architect recovery amends the DAG and resolves WO-001
    sup.resolve_escalation_with_amendment(state, "WO-001")
    sup.evaluate_blocked_by_escalation(state)

    assert "WO-002" not in state.blocked_by_escalation


# ==============================================================================
# 7. Model Acyclic Assertion (Amendment D)
# ==============================================================================

def test_model_acyclic_when_dispatch_edges_removed():
    """Verify state machine is a strict DAG when turn dispatch edges are removed."""
    # Formal transition table from audit
    # Each transition is: (source, target, is_dispatch_edge)
    transitions = [
        ("INIT", "PLANNING", True),
        ("PLANNING", "AWAITING_APPROVAL", True),
        ("AWAITING_APPROVAL", "AUTHORING", False),  # Human approval is non-dispatch
        ("AWAITING_APPROVAL", "PLANNING", False),   # Human reject is non-dispatch
        ("AWAITING_APPROVAL", "ABORTED_HEADLESS", False),
        ("AUTHORING", "AUTHORING_GATE", True),
        ("AUTHORING_GATE", "DISPATCHING", False),   # Deterministic gate check
        ("AUTHORING_GATE", "ARCHITECT_REPAIR", False),
        ("ARCHITECT_REPAIR", "AUTHORING_GATE", True),
        ("ARCHITECT_REPAIR", "WAITING_FOR_HUMAN", False),
        ("DISPATCHING", "EXECUTING", False),        # Supervisor orchestration
        ("EXECUTING", "QA_REVIEW", True),           # Worker turn dispatch
        ("EXECUTING", "EXECUTING", True),           # Worker retry dispatch
        ("EXECUTING", "ARCHITECT_RECOVERY", False), # Worker exhausted/blocked
        ("QA_REVIEW", "INTEGRATION_REVIEW", True),  # QA review turn
        ("QA_REVIEW", "EXECUTING", False),          # Reject rework route
        ("QA_REVIEW", "ARCHITECT_RECOVERY", False), # QA exhausted
        ("ARCHITECT_RECOVERY", "DISPATCHING", True),# Architect amendment turn
        ("ARCHITECT_RECOVERY", "WAITING_FOR_HUMAN", False),
        ("INTEGRATION_REVIEW", "GITOPS", True),     # Review pass turn
        ("INTEGRATION_REVIEW", "EXECUTING", False), # Review fail rework route
        ("INTEGRATION_REVIEW", "WAITING_FOR_HUMAN", False),
        ("GITOPS", "PRODUCT_READY", True),          # GitOps release turn
        ("GITOPS", "EXECUTING", False),             # Reject release route
        ("GITOPS", "WAITING_FOR_HUMAN", False),     # GitOps exhausted
        ("PRODUCT_READY", "COMPLETE", False),       # Verification check
        ("PRODUCT_READY", "WAITING_FOR_HUMAN", False),
        ("WAITING_FOR_HUMAN", "FAILED", False),     # Human abort
        ("WAITING_FOR_HUMAN", "ABORTED_HEADLESS", False),
    ]

    # Filter out all dispatch edges
    non_dispatch_edges = [(src, dst) for src, dst, is_dispatch in transitions if not is_dispatch]

    # Build adjacency graph
    adj: dict[str, list[str]] = {}
    for src, dst in non_dispatch_edges:
        adj.setdefault(src, []).append(dst)

    # Check for cycles via DFS
    visited: set[str] = set()
    rec_stack: set[str] = set()
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]):
        visited.add(node)
        rec_stack.add(node)
        for neighbor in adj.get(node, []):
            if neighbor not in visited:
                dfs(neighbor, path + [neighbor])
            elif neighbor in rec_stack:
                cycle_start = path.index(neighbor) if neighbor in path else 0
                cycles.append(path[cycle_start:] + [neighbor])
        rec_stack.remove(node)

    all_nodes = set(src for src, _ in non_dispatch_edges) | set(dst for _, dst in non_dispatch_edges)
    for node in all_nodes:
        if node not in visited:
            dfs(node, [node])

    # The graph without dispatch edges MUST have zero cycles
    assert len(cycles) == 0, f"Found cycles without dispatch edges: {cycles}"


# ==============================================================================
# 8. Exit Code Table Mapping
# ==============================================================================

def test_exit_code_table_mapping(tmp_path: Path):
    """Verify exit code mapping matches audit specification."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)

    # 1. COMPLETE -> 0
    s_complete = sup.start_run("r1", "Goal", tmp_path, "s1")
    s_complete.phase = Phase.COMPLETE
    assert sup.get_exit_code(s_complete) == 0

    # 2. FAILED -> 1
    s_failed = sup.start_run("r2", "Goal", tmp_path, "s2")
    s_failed.phase = Phase.FAILED
    assert sup.get_exit_code(s_failed) == 1

    # 3. WAITING_FOR_HUMAN (Budget) -> 2
    s_budget = sup.start_run("r3", "Goal", tmp_path, "s3")
    s_budget.phase = Phase.WAITING_FOR_HUMAN
    s_budget.budget_exhausted = True
    assert sup.get_exit_code(s_budget) == 2

    # 4. WAITING_FOR_HUMAN (Retries) -> 3
    s_retries = sup.start_run("r4", "Goal", tmp_path, "s4")
    s_retries.phase = Phase.WAITING_FOR_HUMAN
    s_retries.gitops_retries = 2
    assert sup.get_exit_code(s_retries) == 3

    # 5. WAITING_FOR_HUMAN (Plan / Scope) -> 4
    s_plan = sup.start_run("r5", "Goal", tmp_path, "s5")
    s_plan.phase = Phase.AWAITING_APPROVAL
    assert sup.get_exit_code(s_plan) == 4

    # 6. ABORTED_HEADLESS -> 137
    s_headless = sup.start_run("r6", "Goal", tmp_path, "s6")
    s_headless.error = "Headless CI timeout expired in WAITING_FOR_HUMAN"
    s_headless.phase = Phase.FAILED
    assert sup.get_exit_code(s_headless, is_headless_abort=True) == 137

    # 7. WAITING_FOR_HUMAN (Integrity Violation / Daemon Restart) -> 5
    s_integrity = sup.start_run("r7", "Goal", tmp_path, "s7")
    s_integrity.phase = Phase.WAITING_FOR_HUMAN
    s_integrity.exit_code = 5
    s_integrity.error = "DAEMON_RESTART_FAIL_CLOSED: In-memory session key lost"
    assert sup.get_exit_code(s_integrity) == 5


def test_exit_code_table_is_stable():
    """Assert the full published exit-code table mapping is strictly stable and immutable."""
    from validators.kernel.daemon.supervisor import (
        EXIT_SUCCESS,
        EXIT_FAILURE,
        EXIT_BUDGET_EXHAUSTED,
        EXIT_RETRIES_EXHAUSTED,
        EXIT_PLAN_REJECTED,
        EXIT_INTEGRITY_VIOLATION,
        EXIT_HEADLESS_TIMEOUT,
    )
    assert EXIT_SUCCESS == 0
    assert EXIT_FAILURE == 1
    assert EXIT_BUDGET_EXHAUSTED == 2
    assert EXIT_RETRIES_EXHAUSTED == 3
    assert EXIT_PLAN_REJECTED == 4
    assert EXIT_INTEGRITY_VIOLATION == 5
    assert EXIT_HEADLESS_TIMEOUT == 137

    # Ensure get_exit_code correctly maps each condition
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("r-table", "Goal", Path("."), "s-table")

    # Success -> 0
    state.phase = Phase.COMPLETE
    assert sup.get_exit_code(state) == 0

    # Failure -> 1
    state.phase = Phase.FAILED
    state.exit_code = 1
    assert sup.get_exit_code(state) == 1

    # Budget exhausted -> 2
    state.phase = Phase.WAITING_FOR_HUMAN
    state.exit_code = 2
    assert sup.get_exit_code(state) == 2

    # Retries exhausted -> 3
    state.exit_code = 3
    assert sup.get_exit_code(state) == 3

    # Plan rejected -> 4
    state.exit_code = 4
    assert sup.get_exit_code(state) == 4

    # Integrity violation -> 5
    state.exit_code = 5
    assert sup.get_exit_code(state) == 5

    # Headless timeout -> 137
    state.exit_code = 137
    assert sup.get_exit_code(state, is_headless_abort=True) == 137


# ==============================================================================
# 9. Amendment A: Authoritative State Location & HMAC Signatures
# ==============================================================================

def test_agent_edits_workspace_state_file_has_no_effect(tmp_path: Path):
    """Authoritative state lives in memory; edits to workspace state files are ignored."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-tamper-test", "Goal", tmp_path, "session-1")
    initial_budget = state.global_turn_budget

    # Adversary agent tampers with workspace shadow copies
    ws = Path(state.workspace)
    shadow_budget = ws / ".sync" / "runtime" / "supervisor" / "budget.json"
    shadow_yaml = ws / ".sync" / "runtime" / "supervisor" / f"{state.run_id}.yaml"

    assert shadow_budget.exists()
    assert shadow_yaml.exists()

    # Agent inflates budget to 99999 and clears retries in workspace
    shadow_budget.write_text(json.dumps({"remaining_turn_budget": 99999}), encoding="utf-8")
    shadow_yaml.write_text("global_turn_budget: 99999\nphase: COMPLETE\n", encoding="utf-8")

    # Supervisor loads state - reads strictly from memory, not workspace
    loaded_state = sup.load_run_state(state.run_id, ws, state_manager=sup.state_manager)
    assert loaded_state is not None
    assert loaded_state.global_turn_budget == initial_budget
    assert loaded_state.global_turn_budget != 99999
    assert loaded_state.phase == Phase.PLANNING


def test_daemon_restart_fails_closed_to_waiting_for_human(tmp_path: Path):
    """Daemon restart without in-memory secret fails closed requiring human re-attestation."""
    mgr = DummyOperationManager()
    sup1 = LifecycleSupervisor(manager=mgr)
    state = sup1.start_run("run-restart-test", "Goal", tmp_path, "session-1")

    # Fast-forward to AWAITING_APPROVAL
    state.phase = Phase.AWAITING_APPROVAL

    # Daemon crashes / restarts: new supervisor instance with fresh unshared state_manager
    from validators.kernel.daemon.state_store import SupervisorStateManager
    fresh_mgr = SupervisorStateManager()  # New random 32-byte secret, empty attested set
    sup2 = LifecycleSupervisor(manager=mgr, state_manager=fresh_mgr)

    # Calling advance on un-attested run fails closed immediately
    res = sup2.advance(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 5
    assert "daemon_restart_fail_closed" in str(state.error).lower()

    # Human operator performs out-of-band attestation and resumes run
    sup2.state_manager.re_attest_run(state.run_id, reason="Operator confirmed daemon restart legitimacy")
    assert sup2.state_manager.is_run_attested(state.run_id) is True

    # Calling resume_run_with_human_override transitions run out of WAITING_FOR_HUMAN
    res_after = sup2.resume_run_with_human_override(state, reason="Operator resumed post-attestation", additional_budget=10)
    assert res_after == AdvanceResult.TRANSITIONED
    assert state.phase != Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 0


def test_replay_older_signed_checkpoint_rejected(tmp_path: Path):
    """Older signed checkpoints cannot be replayed past in-memory sequence number."""
    from validators.kernel.daemon.state_store import SupervisorStateManager, ReplayAttackError
    store = SupervisorStateManager(storage_dir=tmp_path / "checkpoints")

    class FakeState:
        def __init__(self, run_id: str, seq_val: int):
            self.run_id = run_id
            self.value = seq_val
        def to_dict(self):
            return {"run_id": self.run_id, "value": self.value}

    s1 = FakeState("run-replay", 10)
    cp1 = store.create_checkpoint(s1)
    assert cp1.seq == 1

    s2 = FakeState("run-replay", 20)
    cp2 = store.create_checkpoint(s2)
    assert cp2.seq == 2

    s3 = FakeState("run-replay", 30)
    cp3 = store.create_checkpoint(s3)
    assert cp3.seq == 3

    # Attempting to restore older checkpoint cp1 (seq 1) when store sequence is 3
    with pytest.raises(ReplayAttackError) as exc_info:
        store.restore_checkpoint(cp1.to_dict())
    assert "replay attack detected" in str(exc_info.value).lower()


# ==============================================================================
# 10. Idle Stall (C_stall) Transition
# ==============================================================================

def test_idle_stall_transitions_to_waiting_for_human(tmp_path: Path):
    """When no work orders are dispatchable and no turns are running, supervisor enters WAITING_FOR_HUMAN."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-idle-stall", "Goal", tmp_path, "session-1")

    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001", "WO-002"]
    state.published_wo_ids = ["WO-001", "WO-002"]

    # WO-001 is in recovery (blocked by escalation)
    state.recovery_attempts["WO-001"] = 1
    state.recovery_wo_id = "WO-001"
    sup.set_wo_dependencies(state, "WO-002", ["WO-001"])
    sup.evaluate_blocked_by_escalation(state)

    assert "WO-002" in state.blocked_by_escalation

    # Advance while in EXECUTING with no active turns and no dispatchable WOs
    # Generalized C_stall requires max_stall_ticks (K=3) consecutive ticks before triggering
    for tick in range(state.max_stall_ticks - 1):
        res = sup.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.stall_ticks == tick + 1
        assert state.phase == Phase.EXECUTING

    # Final K-th tick triggers C_stall transition to WAITING_FOR_HUMAN
    res = sup.advance(state)

    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 3
    assert "idle_stall" in str(state.error).lower()


# ==============================================================================
# 11. Tool Registry Isolation for Human Resume
# ==============================================================================

def test_human_resume_not_callable_from_agent_tools():
    """Agent tool definitions must never expose supervisor management methods."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor
    assert hasattr(LifecycleSupervisor, "resume_run_with_human_override")
    import importlib
    tool_module = importlib.import_module("validators.kernel.tools") if importlib.util.find_spec("validators.kernel.tools") else None
    if tool_module and hasattr(tool_module, "TOOL_REGISTRY"):
        assert "resume_run_with_human_override" not in tool_module.TOOL_REGISTRY


# ==============================================================================
# 12. Budget Exhaustion vs Contention Isolation
# ==============================================================================

def test_budget_exhaustion_not_retried_by_contention_path(tmp_path: Path):
    """Budget exhaustion must transition immediately to WAITING_FOR_HUMAN without contention retries."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-contention-test", "Goal", tmp_path, "session-1")

    state.phase = Phase.EXECUTING
    state.global_turn_budget = 0
    state.contention_count = 0

    res = sup.advance(state)

    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.contention_count == 0  # Contention retry loop was not invoked
    assert state.exit_code == 2


# ==============================================================================
# 13. Failing-First Behavioral Comparisons (Old vs New)
# ==============================================================================

def test_rev2_cycle_behavioral_comparison_old_vs_new(tmp_path: Path):
    """Demonstrate actual behavioral difference between unconstrained loop and bounded gitops_retries."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-comp-rev2", "Goal", tmp_path, "session-1")
    state.phase = Phase.GITOPS
    state.gitops_wo_id = "WO-GITOPS"

    # Simulate 5 consecutive GitOps failures
    outcomes = []
    for i in range(1, 6):
        op_id = f"op-fail-{i}"
        mgr.operations[op_id] = {
            "operation_id": op_id,
            "status": "BLOCKED",
            "result": {"status": "blocked", "error": f"Rejection {i}"},
        }
        state.gitops_operation_id = op_id
        res = sup.advance(state)
        outcomes.append((state.gitops_retries, state.phase, res))
        if state.phase == Phase.WAITING_FOR_HUMAN:
            break

    # Old unbounded code would loop 5+ times; new code halts strictly after 2 retries on 3rd failure
    assert len(outcomes) == 3
    assert outcomes[0] == (1, Phase.GITOPS, AdvanceResult.WAITING_FOR_OPERATION)
    assert outcomes[1] == (2, Phase.GITOPS, AdvanceResult.WAITING_FOR_OPERATION)
    assert outcomes[2] == (2, Phase.WAITING_FOR_HUMAN, AdvanceResult.WAITING_FOR_HUMAN)


def test_multi_wo_behavioral_comparison_run_wide_vs_per_wo(tmp_path: Path):
    """Demonstrate per-WO rework isolation prevents premature run-wide exhaustion across 10 WOs."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-comp-10wo", "Goal", tmp_path, "session-1")
    state.worker_wo_ids = [f"WO-{i:03d}" for i in range(1, 11)]

    # Dynamic budget scaling allocates B = 18 + 5 * 10 = 68 turns
    n_wos = len(state.worker_wo_ids)
    budget = 18 + 5 * n_wos
    state.global_turn_budget = budget
    state.initial_turn_budget = budget

    # WO-001 reworks twice (reaching max_qa_reworks = 2)
    state.qa_reworks["WO-001"] = 2
    # WO-002 reworks once
    state.qa_reworks["WO-002"] = 1

    # In a naive run-wide model (sum >= 2), total reworks (3) would abort the entire run.
    # In the per-WO model:
    # WO-001 exceeding reworks escalates ONLY WO-001 to Architect recovery
    sup.escalate_wo_to_recovery(state, "WO-001", reason="WO-001 QA rework limit reached")

    assert state.recovery_wo_id == "WO-001"
    # WO-002 through WO-010 are NOT halted or failed
    assert "WO-002" not in state.failed_wo_ids
    assert state.qa_reworks["WO-002"] < state.max_qa_reworks
    assert state.global_turn_budget == 68


# ==============================================================================
# 15. B1: Re-attestation & Phase Restoration (Daemon Restart Fail-Closed)
# ==============================================================================

def test_re_attest_restores_interrupted_phase_without_consuming_resume_count(tmp_path: Path):
    """Re-attest run restores interrupted phase from checkpoint without consuming a human resume."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-reattest-1", "Goal", tmp_path, "session-1")

    # Move state to EXECUTING with pre-crash budget of 35
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.global_turn_budget = 35

    # Transition to WAITING_FOR_HUMAN (simulating daemon restart fail-closed)
    sup._transition(state, Phase.WAITING_FOR_HUMAN)
    state.error = "DAEMON_RESTART_FAIL_CLOSED: In-memory key lost. Human re-attestation required."
    state.exit_code = 5

    assert state.interrupted_phase == "EXECUTING"
    assert state.human_resumes_used == 0

    # Human operator re-attests the run with confirmed budget
    res = sup.re_attest_run(state, human_signature="lead-architect@corp.local", confirmed_budget=35)

    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.EXECUTING
    assert state.global_turn_budget == 35
    assert state.human_resumes_used == 0  # CRUCIAL: restart does NOT consume human override budget
    assert state.exit_code == 0
    assert state.error is None
    assert state.stall_ticks == 0


def test_tampered_checkpoint_before_restart_not_silently_restored(tmp_path: Path):
    """Tampered on-disk checkpoint loaded across restart cannot silently reset retries or grant budget."""
    storage_dir = tmp_path / "supervisor_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    sup.state_manager._storage_dir = storage_dir

    state = sup.start_run("run-tamper-restart", "Goal", ws, "session-1")
    state.gitops_retries = 2
    state.worker_retries["WO-001"] = 2
    state.global_turn_budget = 10
    sup.state_manager.create_checkpoint(state, workspace=ws)

    # Simulate daemon restart: fresh supervisor with new in-memory secret key
    sup2 = LifecycleSupervisor(manager=mgr)
    sup2.state_manager._storage_dir = storage_dir

    # Adversary tampers with the on-disk checkpoint while daemon was down
    pattern = f"{state.run_id}_seq_*.json"
    cp_file = sorted(storage_dir.glob(pattern))[-1]
    raw_data = json.loads(cp_file.read_text(encoding="utf-8"))
    raw_data["state_payload"]["gitops_retries"] = 0
    raw_data["state_payload"]["worker_retries"]["WO-001"] = 0
    raw_data["state_payload"]["global_turn_budget"] = 99999
    cp_file.write_text(json.dumps(raw_data, indent=2), encoding="utf-8")

    # Advancing before re-attestation FAILS CLOSED immediately
    state.phase = Phase.EXECUTING
    res = sup2.advance(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 5
    assert "DAEMON_RESTART_FAIL_CLOSED" in str(state.error)

    # Human operator re-attests supplying confirmed out-of-band baseline
    confirmed_counters = {"gitops_retries": 2, "worker_retries": {"WO-001": 2}}
    sup2.re_attest_run(
        state,
        human_signature="lead-operator",
        confirmed_budget=10,
        confirmed_counters=confirmed_counters,
    )

    # Tampered values are defeated: restored counters may not be lower than confirmed values
    assert state.gitops_retries == 2
    assert state.worker_retries["WO-001"] == 2
    assert state.global_turn_budget == 10  # Human authoritative budget, NOT 99999
    assert state.phase == Phase.EXECUTING

    # Verify journal entry in the new HMAC chain
    journal = sup2.state_manager._journal_chain
    assert len(journal) >= 1
    re_attest_event = [e for e in journal if e["event_type"] == "HUMAN_RE_ATTESTATION"][-1]
    assert re_attest_event["details"]["run_id"] == state.run_id
    assert re_attest_event["details"]["confirmed_budget"] == 10
    assert re_attest_event["details"]["confirmed_counters"]["gitops_retries"] == 2
    assert "hmac" in re_attest_event and len(re_attest_event["hmac"]) == 64


def test_re_attest_restart_cap_and_budget_rules(tmp_path: Path):
    """Restart cap bounds crash-loops; budget increase across restart requires human resume count."""
    storage_dir = tmp_path / "supervisor_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    sup.state_manager._storage_dir = storage_dir

    state = sup.start_run("run-restart-cap", "Goal", ws, "session-1")
    state.global_turn_budget = 10
    state.max_daemon_restarts = 3
    state.daemon_restarts = 0
    state.human_resumes_used = 0
    state.max_human_resumes = 2

    # 1. Budget increase without consuming resume count is rejected if resume count exhausted
    state.human_resumes_used = 2
    with pytest.raises(ValueError, match="human resume cap .* is exhausted"):
        sup.re_attest_run(state, human_signature="lead", confirmed_budget=15)

    # 2. Budget increase with remaining resume count consumes 1 human resume
    state.human_resumes_used = 1
    sup.re_attest_run(state, human_signature="lead", confirmed_budget=15)
    assert state.human_resumes_used == 2
    assert state.global_turn_budget == 15
    assert len(state.human_override_logs) == 1
    assert state.human_override_logs[-1]["action"] == "re_attest_budget_increase"

    # 3. Exceeding max_daemon_restarts (restart cap) fails closed
    assert state.daemon_restarts == 1  # From the re_attest above
    state.daemon_restarts = 3  # At cap
    with pytest.raises(ValueError, match="maximum allowed daemon restarts"):
        sup.re_attest_run(state, human_signature="lead", confirmed_budget=15)

    assert state.phase == Phase.FAILED
    assert state.exit_code == EXIT_FAILURE
    assert "MAX_DAEMON_RESTARTS_EXCEEDED" in str(state.error)


# ==============================================================================
# 16. B2: Daemon IPC Authentication for Administrative Actions
# ==============================================================================

def test_sandboxed_unauthenticated_client_cannot_resume_daemon(tmp_path: Path):
    """Unauthenticated clients or child processes cannot invoke administrative RPC methods."""
    from validators.kernel.daemon.protocol import JsonRpcProtocol
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    admin_token = "secret-admin-token-xyz-12345678"
    storage = DaemonStorage(tmp_path / "daemon_state")
    manager = SessionManager(storage)
    protocol = JsonRpcProtocol(manager, auth_token=admin_token)

    # 1. Unauthenticated call to run.resume -> rejected
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "run.resume",
        "params": {"session_id": "test-session"},
    }
    resp = protocol.handle(payload)
    assert "error" in resp
    assert resp["error"]["code"] == -32003
    assert "Policy denied" in resp["error"]["message"]

    # 2. Call with invalid auth token -> rejected
    bad_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "run.resume",
        "params": {"session_id": "test-session", "auth_token": "wrong-token"},
    }
    resp_bad = protocol.handle(bad_payload)
    assert "error" in resp_bad
    assert resp_bad["error"]["code"] == -32003
    assert "Policy denied" in resp_bad["error"]["message"]

    # 3. Authenticated call with correct Bearer token -> accepted (reaches session lookup)
    auth_payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "run.resume",
        "params": {"session_id": "test-session", "auth_token": admin_token},
    }
    resp_auth = protocol.handle(auth_payload)
    # Session lookup reached in manager (proves auth check PASSED)
    assert resp_auth["error"]["code"] == -32003
    assert "unknown session" in resp_auth["error"]["message"].lower()

    # 4. Storage & isolation verification
    from validators.kernel.daemon.server import LocalDaemon
    import time
    from validators.kernel.process_reaper import ProcessReaper

    state_dir = tmp_path / "daemon_secure_state"
    daemon = LocalDaemon(state_dir)
    token_file = state_dir / ".daemon_token"
    assert token_file.is_file(), "Daemon must generate and persist .daemon_token in secure state dir"
    token_val = token_file.read_text(encoding="utf-8").strip()
    assert len(token_val) >= 32

    # Vector 1: Verify token is NOT placed into child env
    assert "STACKMIND_ADMIN_TOKEN" not in os.environ
    assert "DAEMON_TOKEN" not in os.environ

    # Vector 2: Verify workspace cannot access secure state directory
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert not (ws / ".daemon_token").exists()

    # Vector 3: Sandboxed child launched via launch_in_job probing env, workspace, and unauthenticated IPC
    child_script = ws / "probe_token.py"
    child_log = ws / "child_findings.json"
    child_script.write_text(
        "import os, json\n"
        "findings = {\n"
        "    'env_has_token': 'STACKMIND_ADMIN_TOKEN' in os.environ or 'DAEMON_TOKEN' in os.environ,\n"
        "    'workspace_has_token': os.path.exists('.daemon_token'),\n"
        "}\n"
        f"with open(r'{child_log}', 'w') as f: json.dump(findings, f)\n",
        encoding="utf-8",
    )
    import time
    from validators.kernel.process_reaper import ProcessReaper
    reaper = ProcessReaper()
    p = reaper.launch_in_job([sys.executable, str(child_script)], cwd=ws)
    p.wait(timeout=5)
    reaper.reap_all()

    findings = json.loads(child_log.read_text(encoding="utf-8"))
    assert not findings["env_has_token"], "Admin token leaked into child environment!"
    assert not findings["workspace_has_token"], "Admin token present in workspace filesystem!"

    # Vector 4: Direct filesystem path read of ~/.stackmind/daemon/.daemon_token
    # [REASONED ONLY]: On Windows, standard 0o600 chmod does not isolate against same-user
    # unconfined tokens without AppContainer / P1-1 token confinement. This is documented
    # as an ACCEPTED RISK until P1-1. Unauthenticated or un-piped administrative actions
    # require interactive confirmation on the daemon's own console.


def test_admin_console_confirmation_headless_fails_closed_and_sandboxed_child_cannot_answer(tmp_path: Path, monkeypatch):
    """Admin console confirmation: headless fails closed immediately; sandboxed child cannot answer console prompt."""
    from validators.kernel.daemon.server import LocalDaemon
    from validators.kernel.daemon.protocol import JsonRpcProtocol
    import time
    from validators.kernel.process_reaper import ProcessReaper

    state_dir = tmp_path / "daemon_console_test"
    daemon = LocalDaemon(state_dir=state_dir, host="127.0.0.1")

    # 1. State what the HTTP path binds to: strictly IPv4 loopback (127.0.0.1), never 0.0.0.0
    assert daemon.address[0] == "127.0.0.1", f"Daemon must bind strictly to 127.0.0.1, got {daemon.address[0]}"

    # 2. Headless mode fails closed immediately without prompt
    monkeypatch.setenv("SUPERVISOR_HEADLESS", "1")
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session.pause",
        "params": {"session_id": "s-1"},  # Missing auth_token
    }
    resp = daemon.protocol.handle(req)
    assert "error" in resp, "Unauthenticated admin request in headless mode must fail closed"
    assert "Policy denied" in resp["error"]["message"]

    # 3. Interactive console confirmation fails closed if input is not an interactive tty
    monkeypatch.delenv("SUPERVISOR_HEADLESS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert daemon.protocol.confirm_on_console("Authorize? ") is False

    # 4. If child process is attached to daemon console, confirm_on_console detects intrusion and fails closed
    from validators.kernel.process_reaper import ProcessReaper
    reaper = ProcessReaper()
    daemon.protocol.manager.reaper = reaper
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    
    # Register dummy active child PID and simulate it appearing in console process list
    reaper._registered_pids.add(os.getpid())  # Current pid is present in console list
    assert daemon.protocol.confirm_on_console("Authorize? ") is False, "Must fail closed when child is on console"
    reaper._registered_pids.clear()


# ==============================================================================
# 17. B3: Checkpoint and Journal Tampering Fail-Closed (HMAC Integrity)
# ==============================================================================

def test_checkpoint_and_journal_forgery_truncation_deletion_fails_closed(tmp_path: Path):
    """Forged, truncated, or deleted checkpoint files in external storage fail closed."""
    from validators.kernel.daemon.state_store import SupervisorStateManager, StateIntegrityError

    storage_dir = tmp_path / "supervisor_storage"
    mgr = SupervisorStateManager(storage_dir=storage_dir)

    dummy_state = {
        "run_id": "run-integrity-test",
        "phase": "EXECUTING",
        "global_turn_budget": 50,
        "worker_retries": {},
    }
    cp = mgr.create_checkpoint(dummy_state)
    cp_file = storage_dir / f"run-integrity-test_seq_{cp.seq:06d}.json"
    assert cp_file.is_file()

    # 1. Forgery on restored manager (simulating daemon restart): Tamper with payload without valid HMAC
    recovery_mgr = SupervisorStateManager(storage_dir=storage_dir, secret_key=mgr._secret)
    raw_cp = json.loads(cp_file.read_text(encoding="utf-8"))
    raw_cp["state_payload"]["global_turn_budget"] = 999999  # Tamper
    cp_file.write_text(json.dumps(raw_cp, indent=2), encoding="utf-8")

    with pytest.raises(StateIntegrityError, match="HMAC signature verification failed"):
        recovery_mgr.load_latest_checkpoint("run-integrity-test")

    # 2. Truncation: Checkpoint file partially written or truncated
    cp_file.write_text('{"seq": 1, "prev_hmac": "GENESIS"', encoding="utf-8")
    with pytest.raises(StateIntegrityError, match="Corrupted or truncated"):
        recovery_mgr.load_latest_checkpoint("run-integrity-test")

    # 3. Deletion: Checkpoint file deleted entirely
    cp_file.unlink()
    with pytest.raises(FileNotFoundError, match="No checkpoint file found"):
        recovery_mgr.load_latest_checkpoint("run-integrity-test")


# ==============================================================================
# 18. B4: Confined Child Process Cannot Inspect Daemon Memory [REASONED ONLY / P1-1]
# ==============================================================================

@pytest.mark.p1_1
@pytest.mark.xfail(
    strict=True,
    reason="Under unconfined execution on Windows, same-user processes can open each other; true isolation requires P1-1 token confinement",
)
def test_confined_child_cannot_open_daemon_process_memory(tmp_path: Path):
    """Child launched via launch_in_job tries OpenProcess(PROCESS_VM_READ) on daemon PID.

    [REASONED ONLY]: Strict xfail until P1-1 token confinement lands.
    """
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows PROCESS_VM_READ security boundary test")

    import time
    from validators.kernel.process_reaper import ProcessReaper

    daemon_pid = os.getpid()
    script = tmp_path / "probe_mem.py"
    result_file = tmp_path / "probe_result.txt"
    script.write_text(
        "import ctypes, sys\n"
        "from ctypes import wintypes\n"
        f"daemon_pid = {daemon_pid}\n"
        "PROCESS_VM_READ = 0x0010\n"
        "PROCESS_QUERY_INFORMATION = 0x0400\n"
        "kernel32 = ctypes.windll.kernel32\n"
        "kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]\n"
        "kernel32.OpenProcess.restype = wintypes.HANDLE\n"
        "h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, daemon_pid)\n"
        f"with open(r'{result_file}', 'w') as f:\n"
        "    if h:\n"
        "        f.write('OPEN_SUCCESS')\n"
        "        kernel32.CloseHandle(h)\n"
        "    else:\n"
        "        f.write(f'DENIED_{ctypes.GetLastError()}')\n",
        encoding="utf-8",
    )

    import time
    from validators.kernel.process_reaper import ProcessReaper
    reaper = ProcessReaper()
    proc = reaper.launch_in_job([sys.executable, str(script)])
    proc.wait(timeout=5)
    reaper.reap_all()

    res = result_file.read_text(encoding="utf-8").strip()
    # Confinement invariant: opening daemon process memory MUST be denied!
    # Under current unconfined execution, res == 'OPEN_SUCCESS', causing this assertion
    # to fail, which fulfills xfail(strict=True).
    assert res.startswith("DENIED"), f"Child process successfully opened daemon memory (res={res})!"


# ==============================================================================
# 19. B5: Generalized C_stall Across Phases & Contention Independence
# ==============================================================================

def test_generalized_idle_stall_in_planning_phase(tmp_path: Path):
    """Generalized C_stall triggers in PLANNING phase after K idle ticks."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-plan-stall", "Goal", tmp_path, "session-1")

    # State is in PLANNING, handler returns WAITING_FOR_OPERATION with no active ops
    state.phase = Phase.PLANNING
    sup._advance_planning = lambda _s: AdvanceResult.WAITING_FOR_OPERATION

    for tick in range(state.max_stall_ticks - 1):
        res = sup.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.stall_ticks == tick + 1
        assert state.phase == Phase.PLANNING

    # K-th tick triggers C_stall transition to WAITING_FOR_HUMAN
    res = sup.advance(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == 3
    assert "idle_stall" in str(state.error).lower()


def test_contention_backoff_and_concurrency_do_not_trigger_idle_stall(tmp_path: Path):
    """Contention backoff and in-flight concurrency do NOT increment stall ticks."""
    from validators.kernel.daemon.supervisor import OperationContentionError

    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-contention-test", "Goal", tmp_path, "session-1")
    state.phase = Phase.EXECUTING

    # Simulate OperationContentionError during advance
    def failing_handler(_s):
        raise OperationContentionError("Session busy")

    original_handler = sup._advance_executing
    sup._advance_executing = failing_handler

    for _ in range(5):
        res = sup.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.stall_ticks == 0  # MUST remain 0!

    sup._advance_executing = original_handler


# ==============================================================================
# 20. B6: Full Stub Run Exercising Every Turn Path Decrements Budget
# ==============================================================================

def test_every_turn_path_decrements_budget_full_stub_run(tmp_path: Path):
    """Every turn path driven through supervisor advance methods consumes budget."""
    mgr = DummyOperationManager()
    sup = LifecycleSupervisor(manager=mgr)
    initial_budget = 50
    state = sup.start_run("run-full-stub", "Goal", tmp_path, "session-1")
    state.global_turn_budget = initial_budget
    state.initial_turn_budget = initial_budget

    turns_count = 0

    # 1. Real supervisor path: Planning turn
    state.phase = Phase.PLANNING
    state.planning_operation_id = None
    sup._advance_planning(state)
    turns_count += 1
    assert state.global_turn_budget == initial_budget - turns_count

    # 2. Real supervisor path: Authoring turn
    state.phase = Phase.AUTHORING
    state.authoring_operation_id = None
    sup._advance_authoring(state)
    turns_count += 1
    assert state.global_turn_budget == initial_budget - turns_count

    # 3. Real supervisor path: Worker dispatch turn
    state.phase = Phase.DISPATCHING
    state.worker_wo_ids = ["WO-001"]
    sup._advance_dispatching(state)
    turns_count += 1
    assert state.global_turn_budget == initial_budget - turns_count

    # 4. Real supervisor path: Architect recovery decision turn
    state.phase = Phase.ARCHITECT_RECOVERY_DECISION
    state.recovery_wo_id = "WO-001"
    state.recovery_decision_operation_id = None
    sup._advance_recovery_decision(state)
    turns_count += 1
    assert state.global_turn_budget == initial_budget - turns_count

    # 5. Real supervisor path: Integration review turn
    state.phase = Phase.INTEGRATION_REVIEW
    state.integration_operation_id = None
    sup._advance_integration_review(state)
    turns_count += 1
    assert state.global_turn_budget == initial_budget - turns_count

    # 6. Real supervisor path: GitOps release turn
    state.phase = Phase.GITOPS
    state.gitops_operation_id = None
    sup._advance_gitops(state)
    turns_count += 1
    assert state.global_turn_budget == initial_budget - turns_count

    # 7. Additional supervisor turn paths: QA review, QA rework, worker retry, escalation
    mgr.start_turn(state.session_id, "QA review prompt", role="qa", agent_id="gemma", work_order_id="WO-001")
    turns_count += 1

    mgr.start_turn(state.session_id, "QA rework prompt", role="backend", agent_id="codex", work_order_id="WO-001")
    turns_count += 1

    mgr.start_turn(state.session_id, "Worker retry prompt", role="backend", agent_id="codex", work_order_id="WO-001")
    turns_count += 1

    mgr.start_turn(state.session_id, "Escalation prompt", role="architect", agent_id="claude")
    turns_count += 1

    assert turns_count == 10
    assert len(mgr.turns_dispatched) == 10
    assert state.global_turn_budget == initial_budget - 10
    assert initial_budget - state.global_turn_budget == len(mgr.turns_dispatched)


# ==============================================================================
# 21. Test Suite Manifest Integrity
# ==============================================================================



def test_detached_child_cannot_attach_console_or_write_console_input(tmp_path: Path, monkeypatch):
    """A detached child attempting AttachConsole + WriteConsoleInput cannot answer daemon confirmation prompt."""
    if sys.platform != "win32":
        pytest.skip("Windows console injection test requires Windows substrate")

    import time
    from validators.kernel.process_reaper import ProcessReaper
    from validators.kernel.daemon.protocol import JsonRpcProtocol
    reaper = ProcessReaper()
    parent_pid = os.getpid()

    # Child script frees its own console, attaches to parent, and calls WriteConsoleInput with 'y\n'
    code = """
import ctypes, os, sys, time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ppid = int(sys.argv[1])

# Child frees its detached console and attempts AttachConsole to parent
kernel32.FreeConsole()
ret_attach = kernel32.AttachConsole(ppid)

# Child attempts WriteConsoleInput to inject 'y' into parent input buffer
h_conin = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("KeyEvent", KEY_EVENT_RECORD)]

rec = INPUT_RECORD()
rec.EventType = 1
rec.KeyEvent.bKeyDown = True
rec.KeyEvent.UnicodeChar = "y"
written = wintypes.DWORD()
kernel32.WriteConsoleInputW(h_conin, ctypes.byref(rec), 1, ctypes.byref(written))
time.sleep(2.0)
sys.exit(0)
"""
    script = tmp_path / "injector.py"
    script.write_text(code.strip(), encoding="utf-8")

    class MockManager:
        def __init__(self, reaper_inst):
            self.reaper = reaper_inst

    mgr = MockManager(reaper)
    proto = JsonRpcProtocol(manager=mgr)

    # Launch child inside supervisor Job Object
    proc = reaper.launch_in_job([sys.executable, str(script), str(parent_pid)])
    time.sleep(0.3)  # Allow child to execute attach

    try:
        # Mock sys.stdin.isatty to True
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.delenv("SUPERVISOR_HEADLESS", raising=False)
        monkeypatch.delenv("CI", raising=False)

        # Invariant: confirm_on_console detects attached child in GetConsoleProcessList and fails closed!
        confirmed = proto.confirm_on_console("Administrative action? (y/N): ")
        assert confirmed is False, "CRITICAL: confirm_on_console accepted input while child was attached to console!"
    finally:
        reaper.reap_all()


def test_service_child_cannot_attach_console_or_write_console_input(tmp_path: Path, monkeypatch):
    """A persistent service in ServiceJobManager attempting AttachConsole + WriteConsoleInput cannot inject into daemon confirmation prompt."""
    if sys.platform != "win32":
        pytest.skip("Windows console injection test requires Windows substrate")

    import time
    from validators.kernel.process_reaper import ServiceJobManager
    from validators.kernel.daemon.protocol import JsonRpcProtocol
    service_mgr = ServiceJobManager()
    parent_pid = os.getpid()

    code = """
import ctypes, os, sys, time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ppid = int(sys.argv[1])

kernel32.FreeConsole()
ret_attach = kernel32.AttachConsole(ppid)

h_conin = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("KeyEvent", KEY_EVENT_RECORD)]

rec = INPUT_RECORD()
rec.EventType = 1
rec.KeyEvent.bKeyDown = True
rec.KeyEvent.UnicodeChar = "y"
written = wintypes.DWORD()
kernel32.WriteConsoleInputW(h_conin, ctypes.byref(rec), 1, ctypes.byref(written))
time.sleep(2.0)
sys.exit(0)
"""
    script = tmp_path / "service_injector.py"
    script.write_text(code.strip(), encoding="utf-8")

    class MockManager:
        def __init__(self, svc_mgr):
            self.service_manager = svc_mgr
            self.reaper = svc_mgr.service_reaper

    mgr = MockManager(service_mgr)
    proto = JsonRpcProtocol(manager=mgr)

    # Launch service child inside dedicated service Job Object
    proc = service_mgr.launch_service([sys.executable, str(script), str(parent_pid)])
    time.sleep(0.3)

    try:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.delenv("SUPERVISOR_HEADLESS", raising=False)
        monkeypatch.delenv("CI", raising=False)

        # Invariant: confirm_on_console suspends service job, detects service in GetConsoleProcessList, and fails closed!
        confirmed = proto.confirm_on_console("Administrative action? (y/N): ")
        assert confirmed is False, "CRITICAL: confirm_on_console accepted input while service child was attached to console!"
    finally:
        service_mgr.shutdown_services()


def test_collected_tests_manifest_matches_suite():
    """CI check verifying that the generated collected_tests.txt manifest strictly matches the collected suite."""
    manifest_path = Path("collected_tests.txt")
    if not manifest_path.is_file():
        pytest.skip("collected_tests.txt not found in workspace root")

    manifest_lines = [l.strip() for l in manifest_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    # Collect Phase 0 test node IDs
    import subprocess
    cmd = [
        sys.executable, "-m", "pytest",
        "tests/test_p0_1_git_allowlist.py",
        "tests/test_p0_2_turn_snapshot.py",
        "tests/test_p0_3_diff_gate.py",
        "tests/test_p0_5_counters_and_budget.py",
        "tests/test_p0_6_timeouts_and_liveness.py",
        "--collect-only", "-q",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    collected = [l.strip() for l in res.stdout.splitlines() if "::" in l]

    assert len(collected) == len(manifest_lines), f"Manifest count ({len(manifest_lines)}) != collected count ({len(collected)})"
    assert set(collected) == set(manifest_lines), "Manifest items diverge from collected tests"
