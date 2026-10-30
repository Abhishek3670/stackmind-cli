"""SDP-FIX-008A — dispatch eligibility, failure classification and QA dispatch reliability.

Deterministic tests (fake operation manager, no live providers) covering:

1. Recovery history must never permanently suppress dispatch.
2. Dispatch failures are classified instead of uniformly blocking a work order.
3. DISPATCHING must not enter EXECUTING with no work and no valid wait.
4. QA dispatch contention/failure must not strand or duplicate work.
5. Operation ownership: no replacement dispatch while an operation is active.
6. Restart roundtrip preserves eligibility, blockers and failure history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.test_lifecycle_supervisor import MockSessionManager
from validators.harness import review_gate
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    OperationContentionError,
    Phase,
    RunState,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


def _write_wo(
    ws: Path,
    wo_id: str = "WO-001",
    agent: str = "codex",
    deps: list[str] | None = None,
) -> None:
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": wo_id,
        "title": f"Task {wo_id}",
        "assigned_agents": [agent],
        "dependencies": list(deps or []),
    }
    (wo_dir / f"{wo_id}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


class RecordingManager(MockSessionManager):
    """Mock operation manager that can be scripted to fail dispatches."""

    def __init__(self, workspace: Path, failures: list[BaseException] | None = None) -> None:
        super().__init__(workspace)
        self.failures: list[BaseException] = list(failures or [])
        self.dispatched: list[Any] = []

    def start_turn(self, session_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if self.failures:
            raise self.failures.pop(0)
        record = super().start_turn(session_id, prompt, **kwargs)
        self.dispatched.append(kwargs.get("work_order_id"))
        return record


def _state(
    sup: LifecycleSupervisor,
    ws: Path,
    *,
    phase: Phase = Phase.EXECUTING,
    wos: tuple[str, ...] = ("WO-001",),
    **overrides: Any,
) -> RunState:
    state = sup.start_run("run-008a", "Goal", ws, "sess-008a")
    state.phase = phase
    state.worker_wo_ids = list(wos)
    state.published_wo_ids = list(wos)
    state.global_turn_budget = 20
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _wo_ops(mgr: RecordingManager, wo_id: str = "WO-001") -> list[dict[str, Any]]:
    return [op for op in mgr.operations.values() if op.get("work_order_id") == wo_id]


# ─── 1. Recovery and redispatch ───────────────────────────────────────


def test_historical_recovery_attempt_does_not_suppress_dispatch(tmp_path: Path) -> None:
    """A work order with recovery history still dispatches when eligible."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, recovery_attempts={"WO-001": 3})

    res = sup.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" in mgr.dispatched
    # History is preserved (budget/audit), it just no longer gates dispatch.
    assert state.recovery_attempts["WO-001"] == 3


def test_successful_recovery_restores_redispatch_and_clears_suppression(tmp_path: Path) -> None:
    """`_clear_worker_blocker` clears live suppression but keeps history."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(
        sup,
        tmp_path,
        recovery_attempts={"WO-001": 2},
        recovery_wo_id="WO-001",
        blocked_wo_ids=["WO-001"],
    )

    res = sup._clear_worker_blocker(state, tmp_path, "WO-001", redispatch=True)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.recovery_wo_id is None
    assert "WO-001" not in state.blocked_wo_ids
    assert state.recovery_attempts["WO-001"] == 2  # not blindly reset
    assert mgr.dispatched == ["WO-001"]


def test_dispatch_contention_after_recovery_keeps_work_order_eligible(tmp_path: Path) -> None:
    """Contention after recovery consumes no retry and does not block the WO."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path, failures=[OperationContentionError("session busy")])
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, recovery_attempts={"WO-001": 1})

    res = sup.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert "WO-001" not in state.blocked_wo_ids
    assert state.dispatch_failures == {}
    assert state.recovery_attempts["WO-001"] == 1

    res2 = sup.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" in mgr.dispatched


def test_transient_dispatch_failure_is_retried_within_budget(tmp_path: Path) -> None:
    """A transient provider failure is retried and then cleared on success."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path, failures=[ConnectionError("provider unreachable")])
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)

    res = sup.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" not in state.blocked_wo_ids
    rec = state.dispatch_failures["WO-001"]
    assert rec["failure_class"] == "TRANSIENT_PROVIDER_FAILURE"
    assert rec["retryable"] is True
    assert rec["attempts"] == 1

    res2 = sup.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" in mgr.dispatched
    assert "WO-001" not in state.dispatch_failures  # resolved by dispatch


def test_transient_dispatch_failure_exhausts_bounded_attempts(tmp_path: Path) -> None:
    """Transient retries are bounded; exhaustion blocks with an explicit reason."""
    _write_wo(tmp_path)
    mgr = RecordingManager(
        tmp_path,
        failures=[ConnectionError("down"), ConnectionError("down"), ConnectionError("down")],
    )
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, max_dispatch_attempts=2)

    sup.advance(state)  # attempt 1
    sup.advance(state)  # attempt 2 → exhausted

    assert "WO-001" in state.blocked_wo_ids
    assert state.dispatch_failures["WO-001"]["attempts"] == 2
    assert "attempts exhausted" in str(state.error)

    # No further dispatch attempt is made for the exhausted work order.
    sup.advance(state)
    assert state.dispatch_failures["WO-001"]["attempts"] == 2
    assert len(mgr.failures) == 1


# ─── 2. Dispatch failure classification ───────────────────────────────


def test_permission_denial_is_not_retried_and_records_resolution(tmp_path: Path) -> None:
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path, failures=[PermissionError("denied"), PermissionError("denied")])
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, phase=Phase.DISPATCHING)

    res = sup.advance(state)

    assert res == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "WO-001" in state.blocked_wo_ids
    rec = state.dispatch_failures["WO-001"]
    assert rec["failure_class"] == "PERMISSION_DENIED"
    assert rec["human_required"] is True
    assert "never retry" in rec["resolution"]
    assert rec["attempts"] == 1  # deterministic failure was not retried
    assert len(mgr.failures) == 1


def test_unknown_dispatch_failure_escalates_once_without_loop(tmp_path: Path) -> None:
    """An unrecognised exception is captured and escalated, never retried forever."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path, failures=[RuntimeError("unexpected boom")])
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, phase=Phase.DISPATCHING)

    res = sup.advance(state)

    assert res == AdvanceResult.BLOCKED
    rec = state.dispatch_failures["WO-001"]
    assert rec["failure_class"] == "DISPATCH_FAILED_UNKNOWN"
    assert rec["exception_type"] == "RuntimeError"
    assert rec["last_message"] == "unexpected boom"
    assert rec["history"][-1]["message"] == "unexpected boom"

    state.phase = Phase.EXECUTING
    sup.advance(state)
    sup.advance(state)
    assert rec["attempts"] == 1
    assert mgr.failures == []


def test_missing_provider_configuration_records_config_blocker(tmp_path: Path) -> None:
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path, failures=[FileNotFoundError("agent config missing")])
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, phase=Phase.DISPATCHING)

    res = sup.advance(state)

    assert res == AdvanceResult.BLOCKED
    rec = state.dispatch_failures["WO-001"]
    assert rec["failure_class"] == "PROVIDER_CONFIG_MISSING"
    assert rec["human_required"] is True
    assert "Configure" in rec["resolution"]


# ─── 3. Lifecycle correctness ─────────────────────────────────────────


def test_dispatching_does_not_enter_executing_without_executable_work(tmp_path: Path) -> None:
    """No dispatch, no active operation, no valid wait → explicit blocked state."""
    # WO-002 depends on an unfinished prerequisite that cannot run.
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, phase=Phase.DISPATCHING, wos=("WO-002",))

    res = sup.advance(state)

    # SDP-FIX-008B supersedes the flat block for a *missing* prerequisite: the
    # defect is now routed to authorized dependency repair.  The 008A invariant
    # still holds — no work was dispatched, so EXECUTING is never entered.
    assert res in (AdvanceResult.BLOCKED, AdvanceResult.TRANSITIONED)
    assert state.phase in (Phase.BLOCKED, Phase.ARCHITECT_RECOVERY_DECISION)
    assert state.phase is not Phase.EXECUTING
    assert state.phase is not Phase.COMPLETE
    assert "INVALID_DEPENDENCY" in str(state.error) or "DISPATCH_NO_EXECUTABLE_WORK" in str(state.error)


def test_completed_work_order_does_not_hide_blocked_work_order(tmp_path: Path) -> None:
    """Progress on one WO must not mask an unresolved blocked WO."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path, failures=[PermissionError("denied")])
    sup = LifecycleSupervisor(mgr)
    state = _state(
        sup,
        tmp_path,
        phase=Phase.DISPATCHING,
        wos=("WO-000", "WO-001"),
        completed_wo_ids=["WO-000"],
    )

    res = sup.advance(state)

    assert res == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "WO-001" in state.blocked_wo_ids


def test_live_operation_is_not_redispatched(tmp_path: Path) -> None:
    """An active operation is never replaced by a duplicate dispatch."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)

    mgr.start_turn("sess-008a", "Run WO-001", work_order_id="WO-001", agent_id="codex")

    assert sup._dispatch_block_reason(state, "WO-001") == "OPERATION_ACTIVE"
    sup.advance(state)
    assert len(_wo_ops(mgr)) == 1


def test_amendment_renews_recovery_budget_with_audit_trail(tmp_path: Path) -> None:
    """Recovery budget may be renewed, but the prior attempts stay auditable."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, recovery_attempts={"WO-001": 4}, recovery_wo_id="WO-001")

    sup.resolve_escalation_with_amendment(state, "WO-001")

    assert state.recovery_wo_id is None
    assert state.recovery_attempts["WO-001"] == 0
    last = state.recovery_decisions[-1]
    assert last["action"] == "recovery_budget_renewed"
    assert last["prior_recovery_attempts"] == 4


# ─── 4. QA dispatch ───────────────────────────────────────────────────


def _patch_qa_prompt(monkeypatch, sup: LifecycleSupervisor) -> None:
    monkeypatch.setattr(
        type(sup), "_qa_review_prompt", lambda self, state, ws, wo: "qa review prompt"
    )


def test_qa_dispatch_contention_creates_no_duplicate_operation(
    tmp_path: Path, monkeypatch
) -> None:
    _write_wo(tmp_path, agent="codex")
    mgr = RecordingManager(tmp_path, failures=[OperationContentionError("session busy")])
    sup = LifecycleSupervisor(mgr)
    _patch_qa_prompt(monkeypatch, sup)
    state = _state(sup, tmp_path)
    decision = review_gate.evaluate_qa_gate(tmp_path, "WO-001")

    res = sup._handle_qa_gate_pending(state, tmp_path, "WO-001", decision)

    assert res is None
    assert "WO-001" not in state.qa_review_operations
    assert state.qa_review_attempts.get("WO-001", 0) == 0
    assert "WO-001" not in state.blocked_wo_ids

    res2 = sup._handle_qa_gate_pending(state, tmp_path, "WO-001", decision)
    assert res2 is None
    qa_ops = [op for op in mgr.operations.values() if op.get("role") == "qa"]
    assert len(qa_ops) == 1
    assert state.qa_review_operations["WO-001"] == qa_ops[0]["operation_id"]


def test_deterministic_qa_dispatch_failure_is_surfaced(tmp_path: Path, monkeypatch) -> None:
    """A deterministic QA dispatch failure is recorded and escalated, never silent."""
    _write_wo(tmp_path, agent="codex")
    mgr = RecordingManager(tmp_path, failures=[PermissionError("denied")])
    sup = LifecycleSupervisor(mgr)
    _patch_qa_prompt(monkeypatch, sup)
    state = _state(sup, tmp_path)
    decision = review_gate.evaluate_qa_gate(tmp_path, "WO-001")

    res = sup._handle_qa_gate_pending(state, tmp_path, "WO-001", decision)

    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    blocker = state.worker_blockers["WO-001"]
    assert blocker["failure_code"] == "QA_DISPATCH_FAILED"
    assert blocker["dispatch_failure_class"] == "PERMISSION_DENIED"
    assert "WO-001" not in state.qa_review_operations
    # QA was never dispatched, so the work order cannot be considered accepted.
    assert "WO-001" not in state.completed_wo_ids


def test_failed_qa_retry_never_reports_wait_without_an_operation(tmp_path: Path) -> None:
    _write_wo(tmp_path, agent="codex")
    mgr = RecordingManager(tmp_path, failures=[RuntimeError("dispatch exploded")])
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)
    op = {"operation_id": "op-qa-gap", "status": "COMPLETED", "result": {}}

    res = sup._handle_qa_evidence_gap(state, tmp_path, "WO-001", op, "missing test suite")

    assert res != AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" not in state.failed_wo_ids
    assert state.worker_blockers["WO-001"]["failure_code"] == "QA_DISPATCH_FAILED"
    assert state.retry_counts.get("WO-001", 0) == 0  # turn never ran
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION


# ─── 5. Persistence and restart ───────────────────────────────────────


def test_restart_preserves_recovery_history_and_eligibility(tmp_path: Path) -> None:
    """A restart after recovery but before redispatch keeps eligibility and history."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(
        sup,
        tmp_path,
        recovery_attempts={"WO-001": 2},
        recovery_decisions=[{"work_order": "WO-001", "action": "architect_fix"}],
    )

    restored = RunState.from_dict(state.to_dict())
    assert restored.recovery_attempts == {"WO-001": 2}
    assert restored.recovery_decisions[0]["action"] == "architect_fix"

    mgr2 = RecordingManager(tmp_path)
    sup2 = LifecycleSupervisor(mgr2)

    # A freshly restarted supervisor still fails closed until a human re-attests.
    assert sup2.advance(restored) == AdvanceResult.WAITING_FOR_HUMAN
    assert "DAEMON_RESTART_FAIL_CLOSED" in str(restored.error)
    assert mgr2.dispatched == []

    sup2.re_attest_run(restored, human_signature="lead-operator", confirmed_budget=20)
    assert restored.recovery_attempts == {"WO-001": 2}

    res = sup2.advance(restored)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" in mgr2.dispatched


def test_restart_preserves_dispatch_failure_attempts(tmp_path: Path) -> None:
    """A restart must not reset the bounded dispatch-attempt budget."""
    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(
        sup,
        tmp_path,
        dispatch_failures={
            "WO-001": {
                "failure_class": "TRANSIENT_PROVIDER_FAILURE",
                "attempts": 2,
                "retryable": True,
                "human_required": False,
                "last_message": "down",
                "resolution": "bounded retry",
                "history": [{"failure_class": "TRANSIENT_PROVIDER_FAILURE"}],
            }
        },
    )

    restored = RunState.from_dict(state.to_dict())

    assert restored.dispatch_failures["WO-001"]["attempts"] == 2
    assert restored.dispatch_failures["WO-001"]["failure_class"] == "TRANSIENT_PROVIDER_FAILURE"
    assert restored.max_dispatch_attempts == state.max_dispatch_attempts
