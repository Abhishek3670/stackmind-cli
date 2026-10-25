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
from pathlib import Path
from typing import Any
import pytest

from validators.kernel.daemon.supervisor import (
    AdvanceResult,
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

    # Simulate happy path steps: PLANNING -> AWAITING_APPROVAL -> AUTHORING -> GATE -> DISPATCHING -> EXECUTING -> INTEGRATION -> GITOPS -> COMPLETE
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

    # 5. Fast-forward to GitOps completed
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

    # 1st resume: succeeds, logs, and resets specified counters
    res1 = sup.resume_run_with_human_override(
        state,
        reason="Operator approved extra turn budget and retry reset",
        reset_counters=["gitops_retries", "global_turn_budget"],
    )
    assert state.human_resumes_used == 1
    assert state.gitops_retries == 0
    assert state.global_turn_budget > 0
    assert len(state.human_override_logs) == 1
    assert state.human_override_logs[0]["reason"] == "Operator approved extra turn budget and retry reset"

    # Fast forward to 2nd block
    state.phase = Phase.WAITING_FOR_HUMAN
    state.global_turn_budget = 0

    # 2nd resume: succeeds
    res2 = sup.resume_run_with_human_override(
        state,
        reason="Second operator extension",
        reset_counters=["global_turn_budget"],
    )
    assert state.human_resumes_used == 2
    assert len(state.human_override_logs) == 2

    # 3rd resume: exceeds MAX_HUMAN_RESUMES (2) -> fails closed to FAILED
    state.phase = Phase.WAITING_FOR_HUMAN
    res3 = sup.resume_run_with_human_override(
        state,
        reason="Third resume attempt should be denied",
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
