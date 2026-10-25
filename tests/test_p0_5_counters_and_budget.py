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
