"""SDP-FIX-008C — unified failure classification, retry policy, attempt accounting.

Deterministic tests (no live providers, no wall-clock dependence) over:

1.  The canonical failure taxonomy (``classify_failure`` — table driven).
2.  The append-only attempt ledger (identity, closing, deferrals, dedupe).
3.  The authoritative retry-budget policy engine (per-budget limits, waits,
    polling, atomic reservation, no double counting).
4.  Input fingerprints and scoped, auditable budget renewal.
5.  Operation-identity integration and restart reconciliation.
6.  RunState persistence, legacy migration and fail-closed validation.
7.  Cross-subsystem supervisor flows (dispatch / recovery / QA / integration /
    dependencies) driven through the real ``LifecycleSupervisor``.

Engine-level tests construct a ``RunState`` directly so every counter and
budget field starts from its production default.  Supervisor-level tests use
the same ``RecordingManager`` fixture that SDP-FIX-008A's suite uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from validators.kernel.daemon.supervisor import (
    BudgetExhaustedError,
    OperationContentionError,
    RunState,
)
from validators.kernel.daemon.supervisor.attempts import (
    AttemptLedgerCorruptionError,
    AttemptStatus,
    BudgetKey,
    BudgetRenewalDenied,
    FailureClass,
    RetryContext,
    RetryOutcome,
    append_budget_usage,
    classify_failure,
    compute_input_fingerprint,
    evaluate_retry_budget,
    find_active_attempt_for_operation,
    finish_attempt,
    migrate_legacy_run_state,
    record_deferral,
    reconcile_attempts_after_restart,
    renew_budget,
    start_attempt,
    validate_ledger,
)


# ─── Engine fixtures ─────────────────────────────────────────────────────


def _state(**overrides: Any) -> RunState:
    """A RunState with production defaults plus optional field overrides."""
    state = RunState(
        run_id="run-008c",
        product_goal="Test goal",
        workspace="ws-008c",
        session_id="sess-008c",
    )
    state.max_dispatch_attempts = 3
    state.max_attempts = 5
    state.max_retries = 4
    state.max_qa_reworks = 2
    state.max_integration_retries = 2
    state.max_planning_retries = 2
    state.initial_turn_budget = 10
    state.global_turn_budget = 10
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _budget(b: BudgetKey, scope: str, **kw: Any) -> RetryContext:
    return RetryContext(budget=b, scope=scope, **kw)


def _records(state: RunState) -> list[dict[str, Any]]:
    return state.attempt_ledger


# ─── 1. Failure classification (008C-A) ─────────────────────────────────


def test_transient_provider_code_maps_to_transient() -> None:
    assert classify_failure("TRANSIENT_PROVIDER_FAILURE") is FailureClass.TRANSIENT


def test_operation_contention_code_maps_to_contention() -> None:
    assert classify_failure("OPERATION_CONTENTION") is FailureClass.CONTENTION


def test_dependency_wait_is_a_wait_in_policy() -> None:
    decision = evaluate_retry_budget(_state(), _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.DEPENDENCY_WAIT))
    assert decision.outcome is RetryOutcome.WAIT


def test_d025_is_human_approval_wait_never_transient() -> None:
    assert classify_failure("D025_DESTRUCTIVE_OPERATION") is FailureClass.HUMAN_APPROVAL_WAIT


def test_worker_blocked_requires_recovery() -> None:
    assert classify_failure("WORKER_BLOCKED") is FailureClass.RECOVERY_REQUIRED


def test_contract_validation_error_is_deterministic_invalid() -> None:
    assert classify_failure("CONTRACT_VALIDATION_ERROR") is FailureClass.DETERMINISTIC_INVALID


def test_permission_denied_is_deterministic_invalid() -> None:
    assert classify_failure("PERMISSION_DENIED") is FailureClass.DETERMINISTIC_INVALID
    decision = evaluate_retry_budget(
        _state(), _budget(BudgetKey.DISPATCH, "WO-1", failure_code="PERMISSION_DENIED")
    )
    assert decision.outcome is RetryOutcome.DENY


def test_missing_provider_config_is_deterministic_invalid() -> None:
    assert classify_failure("PROVIDER_CONFIG_MISSING") is FailureClass.DETERMINISTIC_INVALID


def test_budget_exhausted_exception_maps_directly() -> None:
    assert classify_failure(exception=BudgetExhaustedError("no budget")) is FailureClass.BUDGET_EXHAUSTED


def test_terminal_class_is_denied() -> None:
    decision = evaluate_retry_budget(
        _state(), _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TERMINAL)
    )
    assert decision.outcome is RetryOutcome.DENY


def test_unknown_exception_fails_safe() -> None:
    class WeirdError(Exception):
        pass

    assert classify_failure(exception=WeirdError("boom")) is FailureClass.UNKNOWN


def test_governance_codes_map_to_canonical_classes() -> None:
    assert classify_failure("D024_QA_GATE_BLOCKED") is FailureClass.RECOVERY_REQUIRED
    assert classify_failure("CONTRACT_SCOPE_VIOLATION") is FailureClass.DETERMINISTIC_INVALID
    assert classify_failure("STAGED_VALIDATION_FAILED") is FailureClass.RECOVERY_REQUIRED
    assert classify_failure("QA_EVIDENCE_MISSING") is FailureClass.RECOVERY_REQUIRED


def test_unrecognized_code_fails_safe() -> None:
    assert classify_failure("_totally_new_future_code") is FailureClass.UNKNOWN
    decision = evaluate_retry_budget(
        _state(), _budget(BudgetKey.WORKER_TASK, "WO-1", failure_code="_totally_new_future_code")
    )
    assert decision.outcome is RetryOutcome.DENY


# ─── 2. Attempt ledger (008C-B) ──────────────────────────────────────────


def test_start_attempt_persists_record_with_identity() -> None:
    state = _state()
    rec = start_attempt(
        state,
        subsystem="worker",
        action="execute",
        budget_key=BudgetKey.WORKER_TASK,
        scope="WO-1",
        operation_id="op-001",
        work_order_id="WO-1",
        attempt_number=1,
    )
    assert rec.attempt_id.startswith("att-run-008c-") and rec.attempt_id
    assert len(_records(state)) == 1
    stored = _records(state)[0]
    assert stored["status"] == AttemptStatus.STARTED.value
    assert stored["operation_id"] == "op-001"
    assert state.attempt_op_index["op-001"] == rec.attempt_id


def test_successful_attempt_is_recorded() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1")
    assert finish_attempt(state, rec.attempt_id, status=AttemptStatus.SUCCEEDED)
    assert _records(state)[0]["status"] == AttemptStatus.SUCCEEDED.value
    assert _records(state)[0]["finished_at"]


def test_failed_attempt_is_recorded_with_evidence() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="provider", action="execute",
                        budget_key=BudgetKey.PROVIDER_EXECUTION, scope="WO-2")
    assert finish_attempt(
        state, rec.attempt_id, status=AttemptStatus.FAILED,
        failure_class=FailureClass.TRANSIENT, failure_code="EXECUTION_ERROR",
    )
    stored = _records(state)[0]
    assert stored["status"] == AttemptStatus.FAILED.value
    assert stored["failure_class"] == FailureClass.TRANSIENT.value
    assert stored["failure_code"] == "EXECUTION_ERROR"
    assert stored["retryable"] is True


def test_cancelled_attempt_is_terminal_and_idempotent() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1")
    assert finish_attempt(state, rec.attempt_id, status=AttemptStatus.CANCELLED)
    closed = _records(state)[0]["finished_at"]
    # Second close must not reopen or rewrite the terminal record.
    first = len(_records(state))
    assert finish_attempt(state, rec.attempt_id, status=AttemptStatus.SUCCEEDED) is False
    assert len(_records(state)) == first
    assert _records(state)[0]["finished_at"] == closed


def test_deferral_is_recorded_but_consumes_no_budget() -> None:
    state = _state()
    record_deferral(
        state,
        subsystem="worker",
        action="execute",
        deferral=FailureClass.DEPENDENCY_WAIT,
        scope="WO-1",
        work_order_id="WO-1",
        reason="upstream WO-0 still running",
    )
    assert any(
        r["status"] == AttemptStatus.DEFERRED_DEPENDENCY_WAIT.value for r in _records(state)
    )
    decision = evaluate_retry_budget(state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT))
    assert decision.usage == 0
    assert decision.outcome is RetryOutcome.ALLOW_ATTEMPT


def test_duplicate_observation_does_not_duplicate_attempt() -> None:
    state = _state()
    start_attempt(state, subsystem="worker", action="execute",
                  budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                  operation_id="op-007", attempt_number=1)
    before = len(_records(state))
    # Same operation observed again (scheduler poll) → deduplicated.
    start_attempt(state, subsystem="worker", action="execute",
                  budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                  operation_id="op-007", attempt_number=1)
    assert len(_records(state)) == before


def test_retry_creates_a_new_attempt_identity() -> None:
    state = _state()
    first = start_attempt(state, subsystem="worker", action="execute",
                          budget_key=BudgetKey.WORKER_TASK, scope="WO-1")
    finish_attempt(state, first.attempt_id, status=AttemptStatus.FAILED,
                   failure_class=FailureClass.TRANSIENT)
    second = start_attempt(state, subsystem="worker", action="execute",
                           budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                           attempt_number=2, parent_attempt_id=first.attempt_id)
    assert second.attempt_id != first.attempt_id
    assert len(_records(state)) == 2
    assert _records(state)[0]["status"] == AttemptStatus.FAILED.value  # history kept


def test_attempt_operation_mapping_survives_roundtrip() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                        operation_id="op-009")
    revived = RunState.from_dict(state.to_dict())
    assert revived.attempt_op_index["op-009"] == rec.attempt_id
    assert revived.attempt_ledger[0]["attempt_id"] == rec.attempt_id


# ─── 3. Budget policy (008C-C) ───────────────────────────────────────────


def _exhaust(engine_budget: BudgetKey, setup: Any) -> RunState:
    state = _state(**setup)
    limit = state.max_qa_reworks if engine_budget is BudgetKey.QA_REWORK else (
        state.max_integration_retries if engine_budget is BudgetKey.INTEGRATION_REVIEW else (
            state.max_planning_retries if engine_budget is BudgetKey.PLANNING_REPAIR else None
        )
    )
    assert limit is not None
    for _ in range(limit):
        assert evaluate_retry_budget(
            state,
            RetryContext(budget=engine_budget, scope="WO-1",
                         failure_class=FailureClass.TRANSIENT, input_fingerprint=f"fp-{_}"),
            reserve=True,
        ).outcome is RetryOutcome.ALLOW_ATTEMPT
    return state


def test_dispatch_budget_exhaustion_is_explicit() -> None:
    state = _state(max_dispatch_attempts=2)
    for i in range(2):
        d = evaluate_retry_budget(
            state, _budget(BudgetKey.DISPATCH, "WO-1", failure_class=FailureClass.TRANSIENT),
            reserve=True,
        )
        assert d.outcome is RetryOutcome.ALLOW_ATTEMPT
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.DISPATCH, "WO-1", failure_class=FailureClass.TRANSIENT)
    )
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    assert d.usage == 2 and d.limit == 2


def test_provider_budget_exhaustion_is_explicit() -> None:
    state = _state(max_attempts=1)
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.PROVIDER_EXECUTION, "WO-1", failure_class=FailureClass.TRANSIENT),
        reserve=True,
    ).outcome is RetryOutcome.ALLOW_ATTEMPT
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.PROVIDER_EXECUTION, "WO-1", failure_class=FailureClass.TRANSIENT)
    )
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    assert state.attempt_counts["WO-1"] == 1  # legacy counter stayed in sync


def test_qa_rework_budget_exhaustion_is_explicit() -> None:
    state = _exhaust(BudgetKey.QA_REWORK, {"max_qa_reworks": 2})
    d = evaluate_retry_budget(state, _budget(BudgetKey.QA_REWORK, "WO-1", failure_class=FailureClass.TRANSIENT))
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED


def test_integration_budget_exhaustion_is_explicit() -> None:
    state = _exhaust(BudgetKey.INTEGRATION_REVIEW, {"max_integration_retries": 2})
    d = evaluate_retry_budget(state, _budget(BudgetKey.INTEGRATION_REVIEW, "RUN", failure_class=FailureClass.TRANSIENT))
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    d = evaluate_retry_budget(state, _budget(BudgetKey.INTEGRATION_REVIEW, "RUN", failure_class=FailureClass.TRANSIENT))
    assert d.limit == 2


def test_recovery_budget_exhaustion_is_explicit() -> None:
    state = _state(max_retries=2, recovery_attempts={"WO-1": 2})
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.ARCHITECT_RECOVERY, "WO-1", failure_class=FailureClass.RECOVERY_REQUIRED)
    )
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    assert d.usage == 2  # reconciled from the legacy counter, not fabricated


def test_planning_budget_exhaustion_is_explicit() -> None:
    state = _exhaust(BudgetKey.PLANNING_REPAIR, {"max_planning_retries": 2})
    d = evaluate_retry_budget(state, _budget(BudgetKey.PLANNING_REPAIR, "RUN", failure_class=FailureClass.TRANSIENT))
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED


def test_global_run_budget_exhaustion_is_explicit() -> None:
    state = _state(initial_turn_budget=5, global_turn_budget=0)
    d = evaluate_retry_budget(state, _budget(BudgetKey.GLOBAL_TURN, "RUN", failure_class=FailureClass.TRANSIENT))
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    assert d.usage == 5


def test_contention_never_consumes_budget() -> None:
    state = _state()
    before = state.max_dispatch_attempts
    d1 = evaluate_retry_budget(
        state, _budget(BudgetKey.DISPATCH, "WO-1", failure_class=FailureClass.CONTENTION)
    )
    assert d1.outcome is RetryOutcome.WAIT and d1.consumes_budget is False
    assert classify_failure(exception=OperationContentionError("busy")) is FailureClass.CONTENTION
    d2 = evaluate_retry_budget(
        state, _budget(BudgetKey.DISPATCH, "WO-1", failure_class=FailureClass.CONTENTION)
    )
    assert d2.outcome is RetryOutcome.WAIT and d2.usage == 0
    assert before == state.max_dispatch_attempts


def test_dependency_wait_never_consumes_budget() -> None:
    state = _state()
    for _ in range(10):
        d = evaluate_retry_budget(
            state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.DEPENDENCY_WAIT)
        )
        assert d.outcome is RetryOutcome.WAIT
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).usage == 0


def test_human_approval_wait_never_consumes_budget() -> None:
    state = _state()
    for _ in range(10):
        d = evaluate_retry_budget(
            state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.HUMAN_APPROVAL_WAIT)
        )
        assert d.outcome is RetryOutcome.WAIT and not d.consumes_budget
    assert state.budget_usage.get("WORKER_TASK", {}).get("WO-1", 0) == 0


def test_repeated_polling_consumes_no_budget() -> None:
    state = _state()
    for _ in range(20):
        d = evaluate_retry_budget(
            state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
        )
        assert d.outcome is RetryOutcome.ALLOW_ATTEMPT
    assert state.budget_usage.get("WORKER_TASK", {}).get("WO-1", 0) == 0


def test_same_failure_is_not_counted_twice() -> None:
    state = _state(retry_counts={"WO-1": 1})  # legacy counter already at 1
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT),
        reserve=True,
    )
    assert d.outcome is RetryOutcome.ALLOW_ATTEMPT
    assert d.usage == 2
    assert state.retry_counts["WO-1"] == 2  # one increment, not two


def test_unknown_failure_class_is_denied_even_with_budget() -> None:
    state = _state()
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.UNKNOWN)
    )
    assert d.outcome is RetryOutcome.DENY


# ─── 4. Fingerprints & renewal (008C-D) ─────────────────────────────────


def test_unchanged_input_cannot_renew_exhausted_budget() -> None:
    state = _state(max_retries=1)
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT),
        reserve=True,
    ).outcome is RetryOutcome.ALLOW_ATTEMPT
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.BUDGET_EXHAUSTED
    # A first renewal is legitimate: it records its input fingerprint and resets.
    renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                 reason="authorized amendment rev-2", fingerprint="fp-A")
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT),
        reserve=True,
    ).outcome is RetryOutcome.ALLOW_ATTEMPT
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.BUDGET_EXHAUSTED
    # NOW re-submission of the same fingerprint is refused: unchanged input
    # cannot renew an exhausted budget twice.
    with pytest.raises(BudgetRenewalDenied):
        renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                     reason="same input again", fingerprint="fp-A")
    with pytest.raises(BudgetRenewalDenied):
        renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                     reason="same input again", fingerprint="fp-A")
    # A genuinely meaningful authorized change does renew.
    renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                 reason="authorized amendment rev-3", fingerprint="fp-B")
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.ALLOW_ATTEMPT


def test_work_order_amendment_renews_only_affected_budget() -> None:
    state = _state(max_retries=1, recovery_attempts={"WO-1": 4})
    evaluate_retry_budget(state, _budget(BudgetKey.WORKER_TASK, "WO-1",
                                         failure_class=FailureClass.TRANSIENT), reserve=True)
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.BUDGET_EXHAUSTED

    renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                 reason="approved WO amendment rev-2", fingerprint="amend-rev-2")

    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.ALLOW_ATTEMPT
    # Unrelated budget untouched.
    assert state.recovery_attempts["WO-1"] == 4
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.ARCHITECT_RECOVERY, "WO-1", failure_class=FailureClass.RECOVERY_REQUIRED)
    )
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED


def test_contract_change_renews_scoped_budget() -> None:
    state = _state(max_qa_reworks=1)
    evaluate_retry_budget(state, _budget(BudgetKey.QA_REWORK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    renew_budget(state, BudgetKey.QA_REWORK, "WO-1",
                 reason="approved contract revision 3", fingerprint="contract-rev-3")
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.QA_REWORK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.ALLOW_ATTEMPT
    # Other scope unaffected.
    assert state.budget_usage.get("QA_REWORK", {}).get("WO-2", 0) == 0


def test_deliverable_hash_change_produces_new_fingerprint() -> None:
    state = _state()
    fp1 = compute_input_fingerprint(state, scope_kind="worker", wo_id="WO-1",
                                    extra={"deliverable_hash": "aaa"})
    fp2 = compute_input_fingerprint(state, scope_kind="worker", wo_id="WO-1",
                                    extra={"deliverable_hash": "bbb"})
    assert fp1 != fp2


def test_dependency_change_does_not_renew_provider_budget() -> None:
    state = _state(max_attempts=1)
    evaluate_retry_budget(
        state, _budget(BudgetKey.PROVIDER_EXECUTION, "WO-1", failure_class=FailureClass.TRANSIENT),
        reserve=True,
    )
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.PROVIDER_EXECUTION, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.BUDGET_EXHAUSTED
    # A dependency-graph change renews a dependency-scoped budget only.
    renew_budget(state, BudgetKey.INTEGRATION_REVIEW, "RUN",
                 reason="dependency graph fingerprint changed", fingerprint="depgraph-9")
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.PROVIDER_EXECUTION, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.BUDGET_EXHAUSTED


def test_recovery_renewal_is_scoped_per_work_order() -> None:
    state = _state(max_retries=1)
    evaluate_retry_budget(state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                 reason="scoped recovery decision R-1", fingerprint="recovery-R-1")
    assert state.budget_usage.get("WORKER_TASK", {}).get("WO-2", 0) == 0
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.ALLOW_ATTEMPT


def test_provider_config_change_renews_when_policy_permits() -> None:
    state = _state(max_attempts=1)
    fp_before = compute_input_fingerprint(
        state, scope_kind="provider", wo_id="WO-1", extra={"provider_config_rev": 1}
    )
    fp_after = compute_input_fingerprint(
        state, scope_kind="provider", wo_id="WO-1", extra={"provider_config_rev": 2}
    )
    assert fp_before != fp_after
    evaluate_retry_budget(state, _budget(BudgetKey.PROVIDER_EXECUTION, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    renew_budget(state, BudgetKey.PROVIDER_EXECUTION, "WO-1",
                 reason="provider/model configuration changed", fingerprint=fp_after)
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.PROVIDER_EXECUTION, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).outcome is RetryOutcome.ALLOW_ATTEMPT


def test_renewal_history_is_auditable() -> None:
    state = _state(max_retries=1)
    evaluate_retry_budget(state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    entry = renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                         reason="approved amendment", fingerprint="fp-amend",
                         evidence_refs=[".sync/decisions/R-9"])
    assert entry["prior_usage"] == 1
    assert entry["renewal_kind"] == "authorized_change"


def test_renewal_history_is_auditable_count() -> None:
    state = _state(max_retries=1)
    evaluate_retry_budget(state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    entry = renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                         reason="approved amendment", fingerprint="fp-amend")
    assert entry["renewal_kind"] == "authorized_change"
    assert entry["renewal_count"] == 1
    assert len(state.budget_renewals) == 1
    # Historical attempt records are never erased by a renewal.
    assert _records(state)


def test_fingerprint_is_stable_for_identical_inputs() -> None:
    state = _state()
    a = compute_input_fingerprint(state, scope_kind="dispatch", wo_id="WO-1")
    b = compute_input_fingerprint(state, scope_kind="dispatch", wo_id="WO-1")
    assert a == b and a


# ─── 5. Operation identity & restart reconciliation (008C-F) ────────────


def _op(op_id: str, status: str) -> dict[str, Any]:
    return {"operation_id": op_id, "status": status}


def test_active_attempt_is_discoverable_for_dedup() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                        operation_id="op-100")
    found = find_active_attempt_for_operation(state, "op-100")
    assert found and found["attempt_id"] == rec.attempt_id
    assert find_active_attempt_for_operation(state, "op-404") is None


def test_restart_does_not_duplicate_active_attempt() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                        operation_id="op-100")
    revived = RunState.from_dict(state.to_dict())
    closed = reconcile_attempts_after_restart(
        revived, lambda op_id: _op(op_id, "RUNNING") if op_id == "op-100" else None
    )
    assert closed == 0  # still running → record stays open, nothing duplicated
    assert any(r["attempt_id"] == rec.attempt_id for r in revived.attempt_ledger)
    start_attempt(revived, subsystem="worker", action="execute",
                  budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                  operation_id="op-100")  # poll after restart
    assert len(revived.attempt_ledger) == 1


def test_terminal_operation_permits_eligible_retry() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                        operation_id="op-101")
    finish_attempt(state, rec.attempt_id, status=AttemptStatus.SUCCEEDED)
    assert find_active_attempt_for_operation(state, "op-101") is None
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    )
    assert d.outcome is RetryOutcome.ALLOW_ATTEMPT


def test_unresolved_operation_survives_reconciliation_without_closing() -> None:
    state = _state()
    start_attempt(state, subsystem="worker", action="execute",
                  budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                  operation_id="op-102")
    closed = reconcile_attempts_after_restart(state, lambda op_id: _op(op_id, "RUNNING"))
    assert closed == 0
    assert _records(state)[0]["status"] == AttemptStatus.STARTED.value


def test_cancellation_reconciliation_is_idempotent() -> None:
    state = _state()
    start_attempt(state, subsystem="worker", action="execute",
                  budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                  operation_id="op-103")
    assert reconcile_attempts_after_restart(state, lambda op_id: _op(op_id, "CANCELLED")) == 1
    assert _records(state)[0]["status"] == AttemptStatus.CANCELLED.value
    assert reconcile_attempts_after_restart(state, lambda op_id: _op(op_id, "CANCELLED")) == 0


def test_abandoned_attempt_is_preserved_not_erased() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1")  # no operation bound
    assert reconcile_attempts_after_restart(state, lambda op_id: None) == 1
    stored = next(r for r in _records(state) if r["attempt_id"] == rec.attempt_id)
    assert stored["status"] == AttemptStatus.ABANDONED.value
    assert stored.get("reconciled_at_restart") is True


def test_deferral_polling_deduped_per_operation() -> None:
    state = _state()
    first = record_deferral(state, subsystem="worker", action="execute",
                            deferral=FailureClass.CONTENTION, scope="WO-1",
                            operation_id="op-104", reason="busy")
    assert first is not None
    assert record_deferral(state, subsystem="worker", action="execute",
                           deferral=FailureClass.CONTENTION, scope="WO-1",
                           operation_id="op-104", reason="busy") is None
    assert len(_records(state)) == 1


def test_restart_completed_operation_closes_attempt_successfully() -> None:
    state = _state()
    start_attempt(state, subsystem="worker", action="execute",
                  budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                  operation_id="op-105")
    assert reconcile_attempts_after_restart(state, lambda op_id: _op(op_id, "COMPLETED")) == 1
    assert _records(state)[0]["status"] == AttemptStatus.SUCCEEDED.value


def test_ledger_truncation_keeps_newest_and_counts_overflow() -> None:
    state = _state()
    first_id: str | None = None
    for i in range(405):
        rec = start_attempt(state, subsystem="worker", action="tick",
                            budget_key=BudgetKey.WORKER_TASK, scope=f"W-{i}")
        if first_id is None:
            first_id = rec.attempt_id
    # Bounded hysteresis: truncate at 400 down to the newest 300; ~5 appends
    # past the second cycle are retained but the cap is respected.
    assert len(state.attempt_ledger) <= 400
    assert state.ledger_truncated == 101
    assert first_id not in {r["attempt_id"] for r in _records(state)}


# ─── 6. Restart & migration (008C-G) ─────────────────────────────────────


def test_ledger_persists_across_restart() -> None:
    state = _state()
    rec = start_attempt(state, subsystem="worker", action="execute",
                        budget_key=BudgetKey.WORKER_TASK, scope="WO-1")
    finish_attempt(state, rec.attempt_id, status=AttemptStatus.FAILED,
                   failure_class=FailureClass.TRANSIENT, failure_code="EXECUTION_ERROR")
    revived = RunState.from_dict(state.to_dict())
    assert len(revived.attempt_ledger) == len(_records(state))
    assert revived.attempt_ledger[0]["attempt_id"] == rec.attempt_id
    assert revived.attempt_ledger[0]["failure_code"] == "EXECUTION_ERROR"
    assert revived.attempt_ledger_seq == state.attempt_ledger_seq


def test_budget_usage_persists_across_restart() -> None:
    state = _state(max_retries=1)
    evaluate_retry_budget(state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    evaluate_retry_budget(state, _budget(BudgetKey.PLANNING_REPAIR, "RUN", failure_class=FailureClass.TRANSIENT), reserve=True)
    revived = RunState.from_dict(state.to_dict())
    assert revived.budget_usage["WORKER_TASK"]["WO-1"] == 1
    assert revived.budget_usage["PLANNING_REPAIR"]["RUN"] == 1
    assert revived.retry_counts["WO-1"] == state.retry_counts["WO-1"]


def test_renewal_history_persists_across_restart() -> None:
    state = _state(max_retries=1)
    evaluate_retry_budget(state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    renew_budget(state, BudgetKey.WORKER_TASK, "WO-1",
                 reason="approved amendment", fingerprint="fp-42")
    revived = RunState.from_dict(state.to_dict())
    assert len(revived.budget_renewals) == 1
    assert revived.budget_renewals[0]["fingerprint"] == "fp-42"
    assert revived.budget_renewal_fingerprints["WORKER_TASK:WO-1"] == "fp-42"


_LEGACY = {
    "run_id": "old-run",
    "product_goal": "Old goal",
    "workspace": "old-ws",
    "session_id": "old-sess",
    "retry_counts": {"WO-1": 2, "WO-2": 1},
    "attempt_counts": {"WO-1": 5},
    "recovery_attempts": {"WO-1": 1},
    "qa_reworks": {"WO-3": 2},
    "planning_retries": 1,
    "integration_retries": 2,
    "gitops_retries": 1,
    "dispatch_failures": {"WO-4": {"attempts": 2, "failure_class": "TRANSIENT_PROVIDER_FAILURE"}},
}


def test_legacy_counters_migrate_deterministically() -> None:
    ledger, usage, renewals = migrate_legacy_run_state(_LEGACY)
    assert not renewals
    assert usage["WORKER_TASK"]["WO-1"] == 2
    assert usage["PROVIDER_EXECUTION"]["WO-1"] == 5
    assert usage["ARCHITECT_RECOVERY"]["WO-1"] == 1
    assert usage["QA_REWORK"]["WO-3"] == 2
    assert usage["PLANNING_REPAIR"]["RUN"] == 1
    assert usage["INTEGRATION_REVIEW"]["RUN"] == 2
    assert usage["GITOPS"]["RUN"] == 1
    assert usage["DISPATCH"]["WO-4"] == 2
    # Legacy provenance markers exist; no fabricated attempt details.
    legacy_marks = [
        r for r in ledger
        if r.get("legacy") and r.get("metadata", {}).get("provenance", "").startswith("migrated")
    ]
    assert any(r["budget_key"] == "WORKER_TASK" and r["scope"] == "WO-1" for r in legacy_marks)


def test_migration_preserves_original_counters() -> None:
    ledger, usage, renewals = migrate_legacy_run_state(dict(_LEGACY))
    # The source counters stay exactly as they were consumed from.
    assert _LEGACY["retry_counts"] == {"WO-1": 2, "WO-2": 1}


def test_migration_is_idempotent() -> None:
    data = dict(_LEGACY)
    first = migrate_legacy_run_state(data)
    data["attempt_ledger"], data["budget_usage"], data["budget_renewals"] = first
    data["budget_usage_008c"] = True  # marker set by the persisted 008C model
    second = migrate_legacy_run_state(data)
    assert second[1] == first[1]
    assert len(second[0]) == len(first[0])
    assert second[0] == first[0]


def test_migrated_state_roundtrips_through_from_dict() -> None:
    state = RunState.from_dict(dict(_LEGACY))
    assert state.budget_usage["WORKER_TASK"]["WO-1"] == 2
    assert state.retry_counts["WO-1"] == 2  # original counters preserved
    assert any(r.get("legacy") for r in state.attempt_ledger)


def test_corrupt_ledger_fails_closed() -> None:
    data = dict(_LEGACY)
    data["run_id"] = "corrupt-run"
    data["product_goal"] = "g"
    data["workspace"] = "w"
    data["session_id"] = "s"
    data["attempt_ledger"] = [{"attempt_id": "x", "subsystem": "worker"}]  # missing fields
    with pytest.raises(AttemptLedgerCorruptionError):
        RunState.from_dict(data)


def test_duplicate_attempt_id_is_detected() -> None:
    state = _state()
    rec = {"attempt_id": "att-dup", "subsystem": "worker", "action": "a",
           "budget_key": "WORKER_TASK", "scope": "WO-1", "status": "STARTED"}
    state.attempt_ledger = [dict(rec), dict(rec)]
    with pytest.raises(AttemptLedgerCorruptionError):
        validate_ledger(state)


def test_negative_budget_usage_fails_closed() -> None:
    state = _state()
    state.attempt_ledger = []
    state.budget_usage = {"WORKER_TASK": {"WO-1": -1}}
    with pytest.raises(AttemptLedgerCorruptionError):
        validate_ledger(state)


def test_corrupt_legacy_counter_fails_closed() -> None:
    data = dict(_LEGACY)
    data["planning_retries"] = "not-a-number"
    with pytest.raises(AttemptLedgerCorruptionError):
        migrate_legacy_run_state(data)


def test_clean_state_validates() -> None:
    state = _state()
    validate_ledger(state)
    append_budget_usage(state, BudgetKey.DISPATCH.value, "WO-1", 2)
    assert state.budget_usage["DISPATCH"]["WO-1"] == 2


def test_malformed_operation_mapping_survives_fail_closed() -> None:
    """An op-index pointing at a missing attempt is not fatal evidence: the
    reconcile pass must still close records and not fabricate new ones.
    """
    state = _state()
    start_attempt(state, subsystem="worker", action="execute",
                  budget_key=BudgetKey.WORKER_TASK, scope="WO-1",
                  operation_id="op-200")
    state.attempt_op_index["ghost-op"] = "att-does-not-exist"  # inconsistent mapping
    closed = reconcile_attempts_after_restart(
        state, lambda op_id: _op(op_id, "FAILED") if op_id == "op-200" else None
    )
    assert closed == 1
    assert len(state.attempt_ledger) == 1
    assert _records(state)[0]["status"] == AttemptStatus.FAILED.value


# ─── 7. Cross-subsystem integration (008C-E) ─────────────────────────────


def _svc_state(tmp_path: Path, **overrides: Any) -> RunState:
    from tests.test_sdp_fix_008a_dispatch_reliability import _write_wo  # noqa: F401

    return _state(workspace=str(tmp_path), **overrides)


def test_dispatch_failure_records_ledger_then_retry_allowed(tmp_path: Path) -> None:
    """Rule 55: dispatch → provider failure → retry, with ledger evidence."""
    from tests.test_sdp_fix_008a_dispatch_reliability import RecordingManager, _write_wo

    _write_wo(tmp_path)
    mgr = RecordingManager(tmp_path, failures=[ConnectionError("provider down")])
    from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase, AdvanceResult

    sup = LifecycleSupervisor(mgr)
    state = sup.start_run("run-008c-svc", "Goal", tmp_path, "sess-008c-svc")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.published_wo_ids = ["WO-001"]
    state.global_turn_budget = 20

    sup.advance(state)  # dispatch fails transiently
    failed = [r for r in state.attempt_ledger
              if r["subsystem"] == "dispatch" and r["status"] == AttemptStatus.FAILED.value]
    assert failed, "failed dispatch must produce ledger evidence"
    assert failed[0]["failure_code"] == "TRANSIENT_PROVIDER_FAILURE"
    assert failed[0]["work_order_id"] == "WO-001"

    sup.advance(state)  # retry dispatch succeeds
    assert "WO-001" in mgr.dispatched
    succeeded = [r for r in state.attempt_ledger
                 if r["subsystem"] == "dispatch"
                 and r["status"] == AttemptStatus.SUCCEEDED.value
                 and r["scope"] == "WO-001"]
    assert succeeded, "successful dispatch must be bound to its attempt"


def test_worker_block_escalates_to_architect_recovery(tmp_path: Path) -> None:
    """Rule 56: worker failure → Architect recovery escalation with evidence."""
    from tests.test_authoring_recovery import blocked_result, make_evidence
    from tests.test_lifecycle_supervisor import MockSessionManager, write_ready_artifact_set
    from validators.kernel.daemon.supervisor import AdvanceResult, LifecycleSupervisor, Phase

    mgr = MockSessionManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = sup.start_run("run-008c-rec", "Goal", tmp_path, "sess-008c-rec")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.max_retries = 2
    state.retry_counts = {"WO-001": 2}  # transient budget exhausted
    write_ready_artifact_set(tmp_path, [("WO-001", "codex", "backend", "code", "src/app.py")])

    res = sup.advance(state)
    op = next(o for o in mgr.operations.values() if o.get("work_order_id") == "WO-001")
    evidence = make_evidence("WO-001", tmp_path)
    mgr.complete_operation(op["operation_id"], "BLOCKED", result=blocked_result(evidence))
    res = sup.advance(state)

    assert res in (AdvanceResult.WAITING_FOR_OPERATION, AdvanceResult.BLOCKED)
    escalated = [r for r in state.attempt_ledger if r["status"] == AttemptStatus.ESCALATED.value]
    assert escalated, "recovery escalation must be recorded in the ledger"
    assert escalated[0]["subsystem"] == "recovery"
    assert state.recovery_attempts["WO-001"] == 1


def test_clear_worker_blocker_renews_execution_budget_auditably(tmp_path: Path) -> None:
    """Rule: clearing a blocker resents the *execution* budget only, with a
    renewal entry whose provenance identifies the authorized recovery path.
    """
    from validators.harness import review_gate  # noqa: F401  (import parity with 008A)

    state = _svc_state(tmp_path, retry_counts={"WO-1": 4}, recovery_attempts={"WO-1": 1},
                       blocked_wo_ids=["WO-1"], worker_blockers={"WO-1": {"operation_id": "op-x"}})
    from validators.kernel.daemon.supervisor import LifecycleSupervisor

    sup = LifecycleSupervisor(_RecordingStub(tmp_path))
    result = sup._clear_worker_blocker(state, tmp_path, "WO-1", redispatch=False)

    assert result is not None
    assert state.retry_counts["WO-1"] == 0
    assert "WO-1" not in state.blocked_wo_ids
    assert state.recovery_attempts["WO-1"] == 1  # recovery history preserved
    renewals = [r for r in state.budget_renewals if r["budget"] == "WORKER_TASK" and r["scope"] == "WO-1"]
    assert len(renewals) == 1
    assert renewals[0]["renewal_kind"] == "recovery_decision_clear"
    # Engine now sees zero usage for this scope.
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.WORKER_TASK, "WO-1", failure_class=FailureClass.TRANSIENT)
    ).usage == 0


class _RecordingStub:
    """Minimal manager stub for accounting-only supervisor calls."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def start_turn(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("test must not dispatch in accounting-only flow")

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        return None


def _scenario() -> RunState:
    return _state()


def test_worker_task_budget_ends_in_explicit_state(tmp_path: Path) -> None:
    """Worker retries exhaust into failed_wo_ids via the policy engine, exactly once."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor

    state = _svc_state(tmp_path, max_retries=1)
    state.max_attempts = 10  # keep the legacy tier from short-circuiting first
    sup = LifecycleSupervisor(_RecordingStub(tmp_path))

    def make_prompt(attempt: int, agent: str) -> str:
        return f"attempt {attempt} for {agent}"

    assert sup._retry_worker(state, tmp_path, "WO-zzz", make_prompt) is False
    assert "WO-zzz" in state.failed_wo_ids
    # Exactly one consumption — calling again must not double-spend.
    usage = state.budget_usage.get("WORKER_TASK", {}).get("WO-zzz", 0)
    sup._retry_worker(state, tmp_path, "WO-zzz", make_prompt)
    assert state.budget_usage.get("WORKER_TASK", {}).get("WO-zzz", 0) == usage


def test_qa_rework_policy_hierarchy(tmp_path: Path) -> None:
    """Rules 23+57: QA failure → authorized rework while budget remains, then
    explicit exhaustion; D024 evidence is classified as RECOVERY_REQUIRED.
    """
    state = _svc_state(tmp_path, max_qa_reworks=2)
    d = evaluate_retry_budget(state, _budget(BudgetKey.QA_REWORK, "WO-1", failure_code="D024_QA_GATE_BLOCKED"))
    assert d.outcome is RetryOutcome.ESCALATE
    evaluate_retry_budget(state, _budget(BudgetKey.QA_REWORK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    evaluate_retry_budget(state, _budget(BudgetKey.QA_REWORK, "WO-1", failure_class=FailureClass.TRANSIENT), reserve=True)
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.QA_REWORK, "WO-1", failure_code="QA_REWORK_BUDGET_EXHAUSTED")
    )
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    assert d.reason.startswith("budget")


def test_integration_rework_flow_renewal_scoped(tmp_path: Path) -> None:
    """Rule 58: integration failure → correction → renewed scoped review attempts."""
    state = _svc_state(tmp_path, max_integration_retries=1)
    rec = start_attempt(state, subsystem="integration", action="review",
                        budget_key=BudgetKey.INTEGRATION_REVIEW, scope="RUN")
    finish_attempt(state, rec.attempt_id, status=AttemptStatus.FAILED,
                   failure_class=FailureClass.RECOVERY_REQUIRED, failure_code="STAGED_VALIDATION_FAILED")
    d = evaluate_retry_budget(state, _budget(BudgetKey.INTEGRATION_REVIEW, "RUN", failure_class=FailureClass.TRANSIENT))
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    renew_budget(state, BudgetKey.INTEGRATION_REVIEW, "RUN",
                 reason="authorized correction commit delivered", fingerprint="corr-c1")
    d2 = evaluate_retry_budget(state, _budget(BudgetKey.INTEGRATION_REVIEW, "RUN", failure_class=FailureClass.TRANSIENT))
    assert d2.outcome is RetryOutcome.ALLOW_ATTEMPT


def test_dependency_wait_then_upstream_completion_allows_dispatch(tmp_path: Path) -> None:
    """Rule 59: DEPENDENCY_WAIT defers without consuming; once the wait clears
    the same scope dispatches inside an untouched budget."""
    state = _svc_state(tmp_path)
    record_deferral(state, subsystem="dispatch", action="dispatch",
                    deferral=FailureClass.DEPENDENCY_WAIT, scope="WO-2",
                    work_order_id="WO-2", reason="upstream WO-1 running")
    assert evaluate_retry_budget(
        state, _budget(BudgetKey.DISPATCH, "WO-2", failure_class=FailureClass.DEPENDENCY_WAIT)
    ).outcome is RetryOutcome.WAIT
    d = evaluate_retry_budget(
        state, _budget(BudgetKey.DISPATCH, "WO-2", failure_class=FailureClass.TRANSIENT)
    )
    assert d.outcome is RetryOutcome.ALLOW_ATTEMPT and d.usage == 0
    assert all(r["budget_key"] == "WORKER_TASK" or r["status"].startswith("DEFERRED")
               for r in _records(state) if r["scope"] == "WO-2")
    evaluate_retry_budget(state, _budget(BudgetKey.DISPATCH, "WO-2", failure_class=FailureClass.TRANSIENT), reserve=True)
    assert state.budget_usage["DISPATCH"]["WO-2"] == 1


def test_global_budget_exhaustion_gates_further_attempts(tmp_path: Path) -> None:
    """Rule 60: the global run budget is the last gate, reported explicitly."""
    state = _svc_state(tmp_path, initial_turn_budget=3, global_turn_budget=0)
    d = evaluate_retry_budget(state, _budget(BudgetKey.GLOBAL_TURN, "RUN", failure_class=FailureClass.TRANSIENT))
    assert d.outcome is RetryOutcome.BUDGET_EXHAUSTED
    described = d.describe()
    assert described["decision"] == RetryOutcome.BUDGET_EXHAUSTED.value
    assert described["budget"] == "GLOBAL_TURN"
    assert described["usage"] == 3
