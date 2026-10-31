"""Unified failure classification, attempt ledger and retry-budget policy (SDP-FIX-008C).

One authoritative accounting layer for every governed attempt in the lifecycle
supervisor.  Three primitives, deliberately independent of each other:

* A **typed failure taxonomy** (:class:`FailureClass`) that classifies both
  existing governance failure codes and dispatch exceptions.  Classification
  is table-driven and deterministic — never derived from error-message
  substring matching at runtime.
* A **persistent, append-only attempt ledger** (:class:`AttemptRecord`) with a
  stable per-run identity.  A wait is never recorded as a failed execution
  attempt and a retry always creates a *new* record.
* A **retry-budget policy engine** (:func:`evaluate_retry_budget`,
  :func:`reserve_attempt`).  Each budget dimension keeps its own scoped usage
  counter; waits, contention, scheduler polling and dependency evaluation
  never consume an execution budget.

The engine is deliberately *additive* over the existing per-subsystem counters
(``retry_counts``, ``recovery_attempts``, ``qa_reworks``, ``dispatch_failures``
…): legacy counters keep their validated semantics and remain the storage for
their budgets; the engine refreshes usage from them (monotone reconciliation)
so no failure is counted twice, and new accounting-only reservations live in
``budget_usage``.  No subsystem budget is collapsed into another.

This module never authorises anything: a retry decision can only bound an
attempt that the existing governance gates already allow.  Corrupted ledger
state fails closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger("stackmind.supervisor")


# ── Failure taxonomy ────────────────────────────────────────────────────


class FailureClass(StrEnum):
    """Canonical, typed failure classification (SDP-FIX-008C-A).

    These categories classify *failure evidence*; they never authorise a
    transition by themselves.  ``UNKNOWN`` fails safe: it is never retryable
    through the policy engine.
    """

    TRANSIENT = "TRANSIENT"
    CONTENTION = "CONTENTION"
    DEPENDENCY_WAIT = "DEPENDENCY_WAIT"
    HUMAN_APPROVAL_WAIT = "HUMAN_APPROVAL_WAIT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    DETERMINISTIC_INVALID = "DETERMINISTIC_INVALID"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TERMINAL = "TERMINAL"
    UNKNOWN = "UNKNOWN"


class AttemptStatus(StrEnum):
    """Ledger lifecycle for one attempt record."""

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"
    DEFERRED_CONTENTION = "DEFERRED_CONTENTION"
    DEFERRED_DEPENDENCY_WAIT = "DEFERRED_DEPENDENCY_WAIT"
    DEFERRED_HUMAN_APPROVAL = "DEFERRED_HUMAN_APPROVAL"
    ESCALATED = "ESCALATED"


_DEFERRAL_STATUSES = frozenset(
    {
        AttemptStatus.DEFERRED_CONTENTION,
        AttemptStatus.DEFERRED_DEPENDENCY_WAIT,
        AttemptStatus.DEFERRED_HUMAN_APPROVAL,
    }
)


class BudgetKey(StrEnum):
    """Registered budget dimensions (SDP-FIX-008C-C).

    Every dimension exists because the underlying governed operations are
    genuinely different (see engineering report §E); none is collapsed into a
    global number.
    """

    DISPATCH = "DISPATCH"                          # bounded redispatch after a failed dispatch
    PROVIDER_EXECUTION = "PROVIDER_EXECUTION"      # per-WO execution attempts against the provider
    WORKER_TASK = "WORKER_TASK"                    # bounded automatic worker retries of a WO
    QA_REWORK = "QA_REWORK"                        # bounded developer rework rounds after QA findings
    QA_REVIEW = "QA_REVIEW"                        # bounded independent QA review dispatches
    INTEGRATION_REVIEW = "INTEGRATION_REVIEW"      # bounded integration-review loops
    ARCHITECT_RECOVERY = "ARCHITECT_RECOVERY"      # bounded architect recovery decisions per WO
    PLANNING_REPAIR = "PLANNING_REPAIR"            # bounded planning re-dispatches
    AUTHORING_REPAIR = "AUTHORING_REPAIR"          # bounded architect authoring repairs
    FINAL_REVIEW = "FINAL_REVIEW"                  # bounded final acceptance review rounds
    GITOPS = "GITOPS"                              # bounded release retries
    GLOBAL_TURN = "GLOBAL_TURN"                    # total turn budget of the run


class RetryOutcome(StrEnum):
    """Authoritative retry-decision outcomes (SDP-FIX-008C-C)."""

    ALLOW_ATTEMPT = "ALLOW_ATTEMPT"
    WAIT = "WAIT"
    ESCALATE = "ESCALATE"
    DENY = "DENY"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


# Governance failure codes (from models.GOVERNANCE_FAILURE_CODES and harness
# evidence packets) → canonical classes.  Table-driven, no substring matching.
_CODE_FAILURE_CLASSES: dict[str, FailureClass] = {
    # Governance approval gates — never a transient failure, never an auto retry.
    "D025_DESTRUCTIVE_OPERATION": FailureClass.HUMAN_APPROVAL_WAIT,
    "D024_QA_GATE_BLOCKED": FailureClass.RECOVERY_REQUIRED,
    # Contract / schema / scope violations are deterministic.
    "CONTRACT_FILE_BUDGET_EXCEEDED": FailureClass.DETERMINISTIC_INVALID,
    "CONTRACT_SCOPE_VIOLATION": FailureClass.DETERMINISTIC_INVALID,
    "CONTRACT_SCOPE_DENIED": FailureClass.DETERMINISTIC_INVALID,
    "CONTRACT_READ_ONLY_VIOLATION": FailureClass.DETERMINISTIC_INVALID,
    "CONTRACT_EXPIRED": FailureClass.DETERMINISTIC_INVALID,
    "CONTRACT_VALIDATION_ERROR": FailureClass.DETERMINISTIC_INVALID,
    "STAGED_VALIDATION_FAILED": FailureClass.RECOVERY_REQUIRED,
    "QA_EVIDENCE_MISSING": FailureClass.RECOVERY_REQUIRED,
    # Dispatch classification codes (008A).
    "OPERATION_CONTENTION": FailureClass.CONTENTION,
    "TRANSIENT_PROVIDER_FAILURE": FailureClass.TRANSIENT,
    "BUDGET_EXHAUSTED": FailureClass.BUDGET_EXHAUSTED,
    "PERMISSION_DENIED": FailureClass.DETERMINISTIC_INVALID,
    "PROVIDER_CONFIG_MISSING": FailureClass.DETERMINISTIC_INVALID,
    "CONTRACT_INVALID": FailureClass.DETERMINISTIC_INVALID,
    "INVALID_DISPATCH_INPUT": FailureClass.DETERMINISTIC_INVALID,
    "DISPATCH_FAILED_UNKNOWN": FailureClass.UNKNOWN,
    "DISPATCH_TIMEOUT": FailureClass.TRANSIENT,
    # QA routing / integration.
    "QA_DISPATCH_FAILED": FailureClass.RECOVERY_REQUIRED,
    "QA_REWORK_BUDGET_EXHAUSTED": FailureClass.BUDGET_EXHAUSTED,
    "QA_REVIEW_BUDGET_EXHAUSTED": FailureClass.BUDGET_EXHAUSTED,
    "INTEGRATION_BUDGET_EXHAUSTED": FailureClass.BUDGET_EXHAUSTED,
    "DEPENDENCY_DEADLOCK": FailureClass.RECOVERY_REQUIRED,
    "DEPENDENCY_WAIT_INVALIDATED": FailureClass.RECOVERY_REQUIRED,
    "INVALID_DEPENDENCY": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_METADATA_MISSING": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_METADATA_INVALID": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_METADATA_UNREADABLE": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_ENTRY_INVALID": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_WORK_ORDER_MISSING": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_MISSING": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_CYCLE": FailureClass.DETERMINISTIC_INVALID,
    "SELF_DEPENDENCY": FailureClass.DETERMINISTIC_INVALID,
    "SUPERSEDED_DEPENDENCY": FailureClass.DETERMINISTIC_INVALID,
    "CROSS_PLAN_DEPENDENCY": FailureClass.DETERMINISTIC_INVALID,
    "DEPENDENCY_ERROR": FailureClass.TRANSIENT,
    # Worker evidence codes treated as bounded transient retries by 008A.
    "OUTCOME_NOT_VERIFIED": FailureClass.TRANSIENT,
    "CODE_NOT_VERIFIED": FailureClass.TRANSIENT,
    "STATE_NOT_VERIFIED": FailureClass.TRANSIENT,
    "SECURITY_NOT_VERIFIED": FailureClass.TRANSIENT,
    "VERIFICATION_GATE_FAILED": FailureClass.TRANSIENT,
    "IMPORT_SATISFIABILITY_FAILED": FailureClass.TRANSIENT,
    "TOOL_LOOP_HALTED": FailureClass.TRANSIENT,
    "LOOP_GUARD_TRIPPED": FailureClass.TRANSIENT,
    "HARNESS_DECISION_INVALID": FailureClass.TRANSIENT,
    "DELIVERABLE_NOT_WRITTEN": FailureClass.TRANSIENT,
    "TEST_EXECUTION_FAILED": FailureClass.TRANSIENT,
    "EXECUTION_ERROR": FailureClass.TRANSIENT,
    "PROCESS_FAILED": FailureClass.TRANSIENT,
    # A bare worker block without a recognisable code needs a decision.
    "WORKER_BLOCKED": FailureClass.RECOVERY_REQUIRED,
}

# Exception types → canonical classes (dispatch happens before evidence
# packets exist; classification is by type, matching 008A's contract).
_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    InterruptedError,
)


def classify_failure(
    failure_code: str | None = None,
    exception: BaseException | None = None,
) -> FailureClass:
    """Classify a failure deterministically into the canonical taxonomy.

    Exactly one input drives the classification: the failure evidence code
    when present, otherwise the exception type.  Unrecognised inputs map to
    ``UNKNOWN`` (fail safe — the policy engine never auto-retries them).
    """
    code = str(failure_code or "").strip().upper()
    if code:
        return _CODE_FAILURE_CLASSES.get(code, FailureClass.UNKNOWN)
    if exception is not None:
        from .models import BudgetExhaustedError, OperationContentionError

        if isinstance(exception, OperationContentionError):
            return FailureClass.CONTENTION
        if isinstance(exception, BudgetExhaustedError):
            return FailureClass.BUDGET_EXHAUSTED
        for cls in type(exception).__mro__:
            if cls.__name__ == "ValidationError" and cls.__module__.split(".")[0] in {
                "jsonschema",
                "pydantic",
            }:
                return FailureClass.DETERMINISTIC_INVALID
        if isinstance(exception, PermissionError):
            return FailureClass.DETERMINISTIC_INVALID
        if isinstance(exception, (FileNotFoundError, KeyError)):
            return FailureClass.DETERMINISTIC_INVALID
        if isinstance(exception, _TRANSIENT_EXCEPTIONS):
            return FailureClass.TRANSIENT
        if isinstance(exception, (ValueError, TypeError)):
            return FailureClass.DETERMINISTIC_INVALID
    return FailureClass.UNKNOWN


FAILURE_CLASS_RETRYABLE: dict[FailureClass, bool] = {
    FailureClass.TRANSIENT: True,
    FailureClass.CONTENTION: False,      # wait, never consumes budget
    FailureClass.DEPENDENCY_WAIT: False, # wait, never consumes budget
    FailureClass.HUMAN_APPROVAL_WAIT: False,  # wait, never consumes budget
    FailureClass.RECOVERY_REQUIRED: False,    # routed to authorized recovery
    FailureClass.DETERMINISTIC_INVALID: False,
    FailureClass.BUDGET_EXHAUSTED: False,
    FailureClass.TERMINAL: False,
    FailureClass.UNKNOWN: False,  # fail safe
}


# ── Attempt ledger ──────────────────────────────────────────────────────

_FAILURE = "__008C_LEDGER_CORRUPT__"


class AttemptLedgerError(ValueError):
    """Raised when authoritative attempt-accounting state is inconsistent."""


class AttemptLedgerCorruptionError(AttemptLedgerError):
    """Raised when persisted ledger state cannot be trusted (fail closed)."""


class BudgetRenewalDenied(AttemptLedgerError):
    """Raised when an unauthorized/undocumented budget renewal is refused."""


@dataclass
class AttemptRecord:
    """One governed attempt in the append-only ledger."""

    attempt_id: str
    subsystem: str
    action: str
    budget_key: str
    scope: str
    status: AttemptStatus
    run_id: str = ""
    work_order_id: str | None = None
    operation_id: str | None = None
    attempt_number: int = 0
    failure_class: str | None = None
    failure_code: str | None = None
    retryable: bool = False
    started_at: str = ""
    finished_at: str = ""
    input_fingerprint: str | None = None
    work_order_revision: int | None = None
    contract_revision: int | None = None
    dependency_fingerprint: str | None = None
    evidence_refs: list[Any] | None = None
    recovery_decision_id: str | None = None
    parent_attempt_id: str | None = None
    metadata: dict[str, Any] | None = None
    legacy: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "subsystem": self.subsystem,
            "action": self.action,
            "budget_key": self.budget_key,
            "scope": self.scope,
            "status": self.status.value,
            "run_id": self.run_id,
            "work_order_id": self.work_order_id,
            "operation_id": self.operation_id,
            "attempt_number": self.attempt_number,
            "failure_class": self.failure_class,
            "failure_code": self.failure_code,
            "retryable": bool(self.retryable),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input_fingerprint": self.input_fingerprint,
            "work_order_revision": self.work_order_revision,
            "contract_revision": self.contract_revision,
            "dependency_fingerprint": self.dependency_fingerprint,
            "evidence_refs": list(self.evidence_refs or []),
            "recovery_decision_id": self.recovery_decision_id,
            "parent_attempt_id": self.parent_attempt_id,
            "metadata": dict(self.metadata or {}),
            "legacy": bool(self.legacy),
        }
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AttemptRecord":
        if not isinstance(data, Mapping):
            raise AttemptLedgerCorruptionError(
                f"attempt record is {type(data).__name__}, not a mapping"
            )
        missing = [f for f in ("attempt_id", "subsystem", "action", "budget_key", "scope", "status") if not data.get(f)]
        if missing:
            raise AttemptLedgerCorruptionError(
                f"attempt record missing required fields: {missing}"
            )
        try:
            status = AttemptStatus(str(data["status"]))
        except ValueError as exc:
            raise AttemptLedgerCorruptionError(
                f"attempt record {data['attempt_id']!r} has unknown status {data['status']!r}"
            ) from exc
        refs = data.get("evidence_refs")
        if refs is not None and not isinstance(refs, list):
            raise AttemptLedgerCorruptionError(
                f"attempt record {data['attempt_id']!r} has malformed evidence_refs"
            )
        return cls(
            attempt_id=str(data["attempt_id"]),
            subsystem=str(data["subsystem"]),
            action=str(data["action"]),
            budget_key=str(data["budget_key"]),
            scope=str(data["scope"]),
            status=status,
            run_id=str(data.get("run_id") or ""),
            work_order_id=data.get("work_order_id"),
            operation_id=data.get("operation_id"),
            attempt_number=int(data.get("attempt_number", 0) or 0),
            failure_class=data.get("failure_class"),
            failure_code=data.get("failure_code"),
            retryable=bool(data.get("retryable", False)),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            input_fingerprint=data.get("input_fingerprint"),
            work_order_revision=data.get("work_order_revision"),
            contract_revision=data.get("contract_revision"),
            dependency_fingerprint=data.get("dependency_fingerprint"),
            evidence_refs=list(refs or []),
            recovery_decision_id=data.get("recovery_decision_id"),
            parent_attempt_id=data.get("parent_attempt_id"),
            metadata=dict(data.get("metadata") or {}),
            legacy=bool(data.get("legacy", False)),
        )


# Append-only ledger is capped to bound memory/disk growth on very long runs;
# truncation always keeps the newest records and increments a visible counter.
_MAX_LEDGER_RECORDS = 400
_TRUNCATE_KEEP = 300


def _ledger_append(state: Any, record: dict[str, Any]) -> None:
    """Append one record to the ledger, truncating oldest evidence if over cap.

    Polling idempotency: an already-open attempt bound to the same operation
    is never duplicated — repeated observation reuses the existing record.
    """
    op_id = record.get("operation_id")
    if op_id:
        existing = state.attempt_op_index.get(str(op_id))
        if existing and any(
            rec.get("attempt_id") == existing and rec.get("status") in _OPEN
            for rec in state.attempt_ledger
        ):
            return
    state.attempt_ledger.append(record)
    if op_id:
        state.attempt_op_index[str(op_id)] = record["attempt_id"]
    if len(state.attempt_ledger) > _MAX_LEDGER_RECORDS:
        overflow = len(state.attempt_ledger) - _TRUNCATE_KEEP
        state.attempt_ledger = [dict(rec) for rec in state.attempt_ledger[overflow:]]
        state.ledger_truncated += overflow
        keep_ids = {rec["attempt_id"] for rec in state.attempt_ledger}
        state.attempt_op_index = {
            op: att for op, att in state.attempt_op_index.items() if att in keep_ids
        }


def next_attempt_id(state: Any) -> str:
    """Stable, unique per-run attempt identity (survives restart via RunState)."""
    state.attempt_ledger_seq = int(state.attempt_ledger_seq) + 1
    raw = f"att-{getattr(state, 'run_id', 'run')}-{state.attempt_ledger_seq}"
    return raw + "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def _null_ts() -> str:
    from validators.clock import now

    return now()


def start_attempt(
    state: Any,
    *,
    subsystem: str,
    action: str,
    budget_key: BudgetKey | str,
    scope: str,
    operation_id: str | None = None,
    work_order_id: str | None = None,
    outcome_mode: str = "STARTED",
    status: AttemptStatus | None = None,
    failure_class: FailureClass | str | None = None,
    failure_code: str | None = None,
    input_fingerprint: str | None = None,
    work_order_revision: int | None = None,
    contract_revision: int | None = None,
    dependency_fingerprint: str | None = None,
    evidence_refs: Iterable[Any] | None = None,
    recovery_decision_id: str | None = None,
    parent_attempt_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    attempt_number: int = 0,
    consume: bool = True,
) -> AttemptRecord:
    """Append an attempt record.

    ``status`` STARTED/CLOSED combined with ``consume`` decides whether the
    record is an execution attempt that belongs to a budget (stored as usage
    reservation under ``budget_usage``) or a deferral/observation (never
    consuming).  Usage here is *reservation-scoped*: for budgets backed by a
    legacy counter the caller's existing increment remains the storage of
    record and :func:`_refresh_usage` reconciles on read, so nothing is
    double-counted.
    """
    status = status or AttemptStatus.STARTED
    effective = status
    failure_cls: FailureClass | None
    try:
        failure_cls = FailureClass(str(getattr(failure_class, "value", failure_class)))
    except (ValueError, AttributeError):
        failure_cls = None
    retryable = bool(failure_cls) and FAILURE_CLASS_RETRYABLE.get(failure_cls, False)  # type: ignore[arg-type]
    record = AttemptRecord(
        attempt_id=next_attempt_id(state),
        subsystem=subsystem,
        action=action,
        budget_key=str(getattr(budget_key, "value", budget_key)),
        scope=str(scope),
        status=effective,
        run_id=str(getattr(state, "run_id", "")),
        work_order_id=work_order_id,
        operation_id=operation_id,
        attempt_number=int(attempt_number),
        failure_class=str(getattr(failure_class, "value", failure_class)) if failure_class is not None else None,
        failure_code=failure_code,
        retryable=retryable,
        started_at=_null_ts(),
        input_fingerprint=input_fingerprint,
        work_order_revision=work_order_revision,
        contract_revision=contract_revision,
        dependency_fingerprint=dependency_fingerprint,
        evidence_refs=list(evidence_refs or []),
        recovery_decision_id=recovery_decision_id,
        parent_attempt_id=parent_attempt_id,
        metadata=dict(metadata or {}),
    )
    _ledger_append(state, record.to_dict())
    if consume and effective in {AttemptStatus.STARTED, AttemptStatus.SUCCEEDED}:
        _reserve(state, str(getattr(budget_key, "value", budget_key)), str(scope))
    return record


def record_deferral(
    state: Any,
    *,
    subsystem: str,
    action: str,
    deferral: FailureClass,
    budget_key: BudgetKey | str = BudgetKey.WORKER_TASK,
    scope: str,
    operation_id: str | None = None,
    work_order_id: str | None = None,
    failure_code: str | None = None,
    reason: str = "",
) -> AttemptRecord | None:
    """Record a WAIT/deferral that MUST NOT consume an execution budget.

    Deduplicated per operation so repeated scheduler polling of the same
    operation never grows the ledger.
    """
    status_map = {
        FailureClass.CONTENTION: AttemptStatus.DEFERRED_CONTENTION,
        FailureClass.DEPENDENCY_WAIT: AttemptStatus.DEFERRED_DEPENDENCY_WAIT,
        FailureClass.HUMAN_APPROVAL_WAIT: AttemptStatus.DEFERRED_HUMAN_APPROVAL,
    }
    status = status_map.get(deferral)
    if status is None:
        raise ValueError(f"deferral {deferral} is not a wait-class failure")
    if operation_id:
        existing_id = state.attempt_op_index.get(str(operation_id))
        if existing_id:
            for rec in state.attempt_ledger:
                if rec.get("attempt_id") == existing_id and rec.get("status") == status.value:
                    return None
    return start_attempt(
        state,
        subsystem=subsystem,
        action=action,
        budget_key=budget_key,
        scope=scope,
        operation_id=operation_id,
        work_order_id=work_order_id,
        status=status,
        failure_code=failure_code,
        metadata={"reason": reason} if reason else None,
        consume=False,
    )


def finish_attempt(
    state: Any,
    attempt_id: str,
    *,
    status: AttemptStatus = AttemptStatus.SUCCEEDED,
    failure_class: FailureClass | str | None = None,
    failure_code: str | None = None,
    outcome: str | None = None,
    evidence_refs: Iterable[Any] | None = None,
    recovery_decision_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Close a ledger record.  Returns False when the attempt id is unknown.

    Historical records are never removed: finishing always updates in place
    and keeps every prior field.
    """
    for rec in state.attempt_ledger:
        if rec.get("attempt_id") != attempt_id:
            continue
        if rec.get("status") in _FINAL_STATUSES:
            return False  # idempotent: already closed
        rec = dict(rec)
        rec["status"] = str(getattr(status, "value", status))
        rec["finished_at"] = _null_ts()
        if failure_class is not None:
            rec["failure_class"] = str(getattr(failure_class, "value", failure_class))
            rec["retryable"] = FAILURE_CLASS_RETRYABLE.get(FailureClass(rec["failure_class"]), False)
        if failure_code is not None:
            rec["failure_code"] = failure_code
        if outcome is not None:
            rec.setdefault("metadata", {})
            rec["metadata"]["outcome"] = outcome
        if evidence_refs is not None:
            rec["evidence_refs"] = list(evidence_refs)
        if recovery_decision_id is not None:
            rec["recovery_decision_id"] = recovery_decision_id
        if metadata:
            rec.setdefault("metadata", {})
            rec["metadata"] = {**(rec.get("metadata") or {}), **dict(metadata)}
        _replace(state, attempt_id, rec)
        return True
    return False


# ── Budget usage storage & atomic reservation ───────────────────────────


def _usage_of(state: Any, budget: str, scope: str) -> int:
    return int(state.budget_usage.get(budget, {}).get(scope, 0))


def _refresh_usage(state: Any, budget: str, scope: str) -> int:
    """Reconcile reserved usage with legacy counters (monotone, no double count).

    Budgets backed by legacy counters keep those counters authoritative for
    their usage; the reservation under ``budget_usage`` follows the maximum so
    the engine never under-reads and never increments a legacy counter twice.
    """
    legacy = _legacy_usage(state, budget, scope)
    reserved = _usage_of(state, budget, scope)
    usage = max(legacy, reserved)
    if usage != reserved:
        state.budget_usage.setdefault(budget, {})[scope] = usage
    return usage


def _legacy_usage(state: Any, budget: str, scope: str) -> int:
    """Usage as recorded by the pre-008C per-subsystem counters."""
    try:
        if budget == BudgetKey.DISPATCH.value:
            rec = (getattr(state, "dispatch_failures", {}) or {}).get(scope) or {}
            return int(rec.get("attempts", 0) or 0)
        if budget == BudgetKey.PROVIDER_EXECUTION.value:
            return int((getattr(state, "attempt_counts", {}) or {}).get(scope, 0) or 0)
        if budget == BudgetKey.WORKER_TASK.value:
            return int((getattr(state, "retry_counts", {}) or {}).get(scope, 0) or 0)
        if budget == BudgetKey.QA_REWORK.value:
            return int((getattr(state, "qa_reworks", {}) or {}).get(scope, 0) or 0)
        if budget == BudgetKey.QA_REVIEW.value:
            return int((getattr(state, "qa_review_attempts", {}) or {}).get(scope, 0) or 0)
        if budget == BudgetKey.ARCHITECT_RECOVERY.value:
            return int((getattr(state, "recovery_attempts", {}) or {}).get(scope, 0) or 0)
        if budget == BudgetKey.PLANNING_REPAIR.value:
            return int(getattr(state, "planning_retries", 0) or 0)
        if budget == BudgetKey.AUTHORING_REPAIR.value:
            return int(getattr(state, "authoring_repair_attempts", 0) or 0)
        if budget == BudgetKey.FINAL_REVIEW.value:
            return int(getattr(state, "final_review_rounds", 0) or 0)
        if budget == BudgetKey.GITOPS.value:
            return int(getattr(state, "gitops_retries", 0) or 0)
        if budget == BudgetKey.INTEGRATION_REVIEW.value:
            return int(max(getattr(state, "integration_retries", 0) or 0, getattr(state, "integration_rework_rounds", 0) or 0))
        if budget == BudgetKey.GLOBAL_TURN.value:
            initial = int(getattr(state, "initial_turn_budget", 0) or 0)
            remaining = int(getattr(state, "global_turn_budget", 0) or 0)
            return max(0, initial - remaining)
    except Exception as exc:  # noqa: BLE001 — legacy-shape mismatch is an engine defect, surface it
        raise AttemptLedgerCorruptionError(
            f"budget usage lookup failed for {budget}/{scope}: {type(exc).__name__}: {exc}"
        ) from exc
    return 0


def _increment_legacy(state: Any, budget: str, scope: str) -> None:
    """Keep the legacy counter in sync when the engine is the accounting point."""
    if budget == BudgetKey.PROVIDER_EXECUTION.value:
        state.attempt_counts[scope] = int(state.attempt_counts.get(scope, 0)) + 1
    elif budget == BudgetKey.WORKER_TASK.value:
        state.retry_counts[scope] = int(state.retry_counts.get(scope, 0)) + 1
    elif budget == BudgetKey.QA_REWORK.value:
        state.qa_reworks[scope] = int(state.qa_reworks.get(scope, 0)) + 1
    elif budget == BudgetKey.QA_REVIEW.value:
        state.qa_review_attempts[scope] = int(state.qa_review_attempts.get(scope, 0)) + 1
    elif budget == BudgetKey.ARCHITECT_RECOVERY.value:
        state.recovery_attempts[scope] = int(state.recovery_attempts.get(scope, 0)) + 1
    elif budget == BudgetKey.PLANNING_REPAIR.value:
        state.planning_retries = int(state.planning_retries) + 1
    elif budget == BudgetKey.AUTHORING_REPAIR.value:
        state.authoring_repair_attempts = int(state.authoring_repair_attempts) + 1
    elif budget == BudgetKey.FINAL_REVIEW.value:
        state.final_review_rounds = int(state.final_review_rounds) + 1
    elif budget == BudgetKey.GITOPS.value:
        state.gitops_retries = int(state.gitops_retries) + 1
    elif budget == BudgetKey.INTEGRATION_REVIEW.value:
        state.integration_retries = int(state.integration_retries) + 1


def _reserve(state: Any, budget: str, scope: str) -> None:
    """Atomically consume one unit of the budget (single accounting write point)."""
    usage = _refresh_usage(state, budget, scope) + 1
    state.budget_usage.setdefault(budget, {})[scope] = usage
    _increment_legacy(state, budget, scope)
    state.budget_usage.setdefault(budget, {})[scope] = max(
        usage, _legacy_usage(state, budget, scope)
    )


def budget_limit(state: Any, budget: BudgetKey | str) -> int:
    """Configured limit for a budget, from the state's existing settings."""
    key = str(getattr(budget, "value", budget))
    limits: dict[str, Callable[[], int]] = {
        BudgetKey.DISPATCH.value: lambda: int(getattr(state, "max_dispatch_attempts", 3)),
        BudgetKey.PROVIDER_EXECUTION.value: lambda: int(getattr(state, "max_attempts", 10)),
        BudgetKey.WORKER_TASK.value: lambda: int(getattr(state, "max_retries", 10)),
        BudgetKey.QA_REWORK.value: lambda: int(getattr(state, "max_qa_reworks", 2)),
        BudgetKey.QA_REVIEW.value: lambda: int(getattr(state, "max_qa_review_attempts", 2)),
        BudgetKey.ARCHITECT_RECOVERY.value: lambda: int(getattr(state, "max_retries", 10)),
        BudgetKey.PLANNING_REPAIR.value: lambda: int(getattr(state, "max_planning_retries", 2)),
        BudgetKey.AUTHORING_REPAIR.value: lambda: int(getattr(state, "max_authoring_repairs", 2)),
        BudgetKey.FINAL_REVIEW.value: lambda: int(getattr(state, "max_final_review_rounds", 2)),
        BudgetKey.GITOPS.value: lambda: int(getattr(state, "max_gitops_retries", 2)),
        BudgetKey.INTEGRATION_REVIEW.value: lambda: int(getattr(state, "max_integration_retries", 2)),
        BudgetKey.GLOBAL_TURN.value: lambda: int(getattr(state, "initial_turn_budget", 50)),
    }
    fn = limits.get(key)
    if fn is None:
        raise AttemptLedgerError(f"unregistered budget {budget!r}")
    return fn()


@dataclass(frozen=True)
class RetryContext:
    """Inputs of one authoritative retry-budget evaluation."""

    budget: BudgetKey | str
    scope: str
    failure_class: FailureClass | str | None = None
    failure_code: str | None = None
    input_fingerprint: str | None = None
    operation_id: str | None = None
    run_id: str | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RetryDecision:
    """Authoritative budget evaluation result."""

    outcome: RetryOutcome
    budget: str
    scope: str
    usage: int
    limit: int
    consumes_budget: bool
    reason: str
    failure_class: str | None = None
    failure_code: str | None = None
    attempt_id: str | None = None
    input_fingerprint: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is RetryOutcome.ALLOW_ATTEMPT

    @property
    def wait(self) -> bool:
        return self.outcome is RetryOutcome.WAIT

    def describe(self) -> dict[str, Any]:
        """Structured diagnostics for one decision (008C-H, no secrets)."""
        return {
            "budget": self.budget,
            "scope": self.scope,
            "usage": self.usage,
            "limit": self.limit,
            "decision": self.outcome.value,
            "consumes_budget": self.consumes_budget,
            "reason": self.reason,
            "failure_class": self.failure_class,
            "failure_code": self.failure_code,
            "attempt_id": self.attempt_id,
            "input_fingerprint": self.input_fingerprint,
        }


def evaluate_retry_budget(
    state: Any,
    context: RetryContext,
    *,
    reserve: bool = False,
) -> RetryDecision:
    """Single authoritative policy for whether another attempt may start.

    Never consumes budget unless ``reserve=True`` and the outcome is
    ALLOW_ATTEMPT.  Wait-class and invalid failures are decided before the
    budget is consulted so scheduler polling / dependency waits / approval
    waits can never burn an execution budget.
    """
    budget = str(getattr(context.budget, "value", context.budget))
    scope = str(context.scope)
    failure_class: FailureClass | None = None
    if context.failure_class is not None:
        raw = str(getattr(context.failure_class, "value", context.failure_class))
        try:
            failure_class = FailureClass(raw)
        except ValueError:
            failure_class = FailureClass.UNKNOWN
    elif context.failure_code:
        failure_class = classify_failure(context.failure_code)

    limit = budget_limit(state, budget)
    usage = _refresh_usage(state, budget, scope)

    def decision(
        outcome: RetryOutcome, reason: str, *, consumes: bool = False, attempt_id: str | None = None
    ) -> RetryDecision:
        return RetryDecision(
            outcome=outcome,
            budget=budget,
            scope=scope,
            usage=usage if not attempt_id else _usage_of(state, budget, scope),
            limit=limit,
            consumes_budget=consumes,
            reason=reason,
            failure_class=failure_class.value if failure_class else None,
            failure_code=context.failure_code,
            attempt_id=attempt_id,
            input_fingerprint=context.input_fingerprint,
        )

    if failure_class in (FailureClass.CONTENTION, FailureClass.DEPENDENCY_WAIT, FailureClass.HUMAN_APPROVAL_WAIT):
        return decision(RetryOutcome.WAIT, f"wait-class failure ({failure_class.value}); budget not consumed")
    if failure_class is FailureClass.BUDGET_EXHAUSTED or usage >= limit:
        return decision(
            RetryOutcome.BUDGET_EXHAUSTED,
            f"budget {budget} for {scope} exhausted ({usage}/{limit})",
        )
    if failure_class in (FailureClass.DETERMINISTIC_INVALID, FailureClass.TERMINAL, FailureClass.UNKNOWN):
        label = failure_class.value if failure_class is not None else "?"
        return decision(
            RetryOutcome.DENY,
            f"non-retryable failure ({label}); route to correction/blocker/human decision path",
        )
    if failure_class is FailureClass.RECOVERY_REQUIRED:
        return decision(
            RetryOutcome.ESCALATE,
            "recovery required; route to the authorized recovery decision path",
        )
    if failure_class is FailureClass.TRANSIENT or failure_class is None:
        reason = (
            "transient failure within budget" if failure_class else "no failure evidence; budget available"
        )
        if reserve:
            record = start_attempt(
                state,
                subsystem=str(context.metadata or {}).get("subsystem", "engine") if isinstance(context.metadata, Mapping) else "engine",
                action=f"{budget}:{scope}",
                budget_key=budget,
                scope=scope,
                operation_id=context.operation_id,
                input_fingerprint=context.input_fingerprint,
                failure_class=failure_class,
                failure_code=context.failure_code,
                metadata=dict(context.metadata or {}),
                attempt_number=usage + 1,
            )
            return decision(
                RetryOutcome.ALLOW_ATTEMPT,
                reason,
                consumes=True,
                attempt_id=record.attempt_id,
            )
        return decision(RetryOutcome.ALLOW_ATTEMPT, reason)
    return decision(RetryOutcome.DENY, "unknown policy state; fail closed")


def _zero_legacy(state: Any, budget: str, scope: str) -> None:
    """Zero the legacy backing counter on an explicit, audited renewal.

    History is *not* erased: the renewal entry records prior usage, and the
    append-only ledger keeps every attempt record.  Only the enforcement
    counter for this exact budget+scope is set to a state where a new attempt
    can be counted from 1 again.  GLOBAL_TURN renewal extends the remaining
    budget to its configured initial value.
    """
    try:
        if budget == BudgetKey.DISPATCH.value:
            rec = (getattr(state, "dispatch_failures", {}) or {}).get(scope)
            if isinstance(rec, dict):
                rec["attempts"] = 0  # failure evidence retained; only budget resets
        elif budget == BudgetKey.PROVIDER_EXECUTION.value:
            state.attempt_counts[scope] = 0
        elif budget == BudgetKey.WORKER_TASK.value:
            state.retry_counts[scope] = 0
        elif budget == BudgetKey.QA_REWORK.value:
            state.qa_reworks[scope] = 0
        elif budget == BudgetKey.QA_REVIEW.value:
            state.qa_review_attempts[scope] = 0
        elif budget == BudgetKey.ARCHITECT_RECOVERY.value:
            state.recovery_attempts[scope] = 0
        elif budget == BudgetKey.PLANNING_REPAIR.value:
            state.planning_retries = 0
        elif budget == BudgetKey.AUTHORING_REPAIR.value:
            state.authoring_repair_attempts = 0
        elif budget == BudgetKey.FINAL_REVIEW.value:
            state.final_review_rounds = 0
        elif budget == BudgetKey.GITOPS.value:
            state.gitops_retries = 0
        elif budget == BudgetKey.INTEGRATION_REVIEW.value:
            state.integration_retries = 0
            # integration_rework_rounds is deliberately NOT reset here: it is a
            # final-readiness gate, not the enforcement counter for this budget.
        elif budget == BudgetKey.GLOBAL_TURN.value:
            initial = int(getattr(state, "initial_turn_budget", 0) or 0)
            if initial > 0:
                state.global_turn_budget = initial
    except Exception as exc:  # noqa: BLE001 — renewal must fail loudly, not partially
        raise AttemptLedgerCorruptionError(
            f"budget renewal could not reset legacy counter {budget}/{scope}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def renew_budget(
    state: Any,
    budget: BudgetKey | str,
    scope: str,
    *,
    reason: str,
    fingerprint: str | None = None,
    renewal_kind: str = "authorized_change",
    allow_unchanged_input: bool = False,
    evidence_refs: Iterable[Any] | None = None,
    renewals_performed: int = 0,
) -> dict[str, Any]:
    """Explicit, auditable, scoped budget renewal (008C-D).

    Rules enforced here:
    * Input-bound: same fingerprint as the last renewal for this scope →
      denied unless ``allow_unchanged_input`` (used only by the legacy
      blocker-clearing path, where the recovery decision itself is the new
      authorization and is recorded as ``renewal_kind``).
    * Scoped: only this budget+scope usage resets to 0; unrelated budgets are
      untouched.
    * Auditable: a renewal entry (with prior usage) is appended to
      ``budget_renewals``; historical attempt records are never erased.
    """
    budget = str(getattr(budget, "value", budget))
    key = f"{budget}:{scope}"
    prior_usage = _refresh_usage(state, budget, scope)
    if state.budget_renewal_fingerprints.get(key) == fingerprint and not allow_unchanged_input:
        raise BudgetRenewalDenied(
            f"budget {key} renewal denied: input fingerprint unchanged; exhausted "
            "budgets may only be renewed by a meaningful authorized change"
        )
    state.budget_renewal_fingerprints[key] = fingerprint or ""
    state.budget_usage.setdefault(budget, {})[scope] = 0
    _zero_legacy(state, budget, scope)
    entry: dict[str, Any] = {
        "budget": budget,
        "scope": scope,
        "renewal_kind": renewal_kind,
        "reason": reason,
        "fingerprint": fingerprint or "",
        "prior_usage": prior_usage,
        "renewal_count": _renewal_count_for(state, key) + 1,
        "at": _null_ts(),
        "evidence_refs": list(evidence_refs or []),
    }
    state.budget_renewals.append(entry)
    return entry


def _renewal_count_for(state: Any, key: str) -> int:
    return sum(1 for r in state.budget_renewals if r.get("budget") + ":" + str(r.get("scope")) == key)


# ── Fingerprints (input-bound accounting) ───────────────────────────────


def compute_input_fingerprint(
    state: Any,
    *,
    scope_kind: str,
    wo_id: str | None = None,
    ws: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Subsystem-scoped fingerprint of the meaningful inputs of an attempt.

    Deliberately *narrow* per scope so an unrelated change (e.g. a dependency
    graph edit) cannot renew an unrelated budget.  Never includes secrets,
    prompts or provider payloads — only revisions, hashes and scopes.
    """
    payload: dict[str, Any] = {"kind": scope_kind, "run_id": getattr(state, "run_id", "")}
    if wo_id:
        payload["wo"] = wo_id
    if wo_id and ws is not None:
        try:
            from validators.kernel.work_order_ops import contract_revision, load_work_order

            ref = load_work_order(ws, wo_id)
            if ref is not None:
                payload["wo_revision"] = ref.revision
            payload["contract_revision"] = contract_revision(ws, wo_id)
        except Exception:
            pass
    if wo_id is not None:
        payload["state_revisions"] = {
            "contract_revisions": dict(getattr(state, "contract_revisions", {}) or {}).get(wo_id),
        }
    if extra:
        payload["extra"] = dict(extra)
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── Restart reconciliation ──────────────────────────────────────────────

_OPEN = frozenset({AttemptStatus.STARTED.value})
_FINAL_STATUSES = frozenset(
    {
        AttemptStatus.SUCCEEDED.value,
        AttemptStatus.FAILED.value,
        AttemptStatus.ABANDONED.value,
        AttemptStatus.CANCELLED.value,
    }
)
_OPERATION_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}


def reconcile_attempts_after_restart(
    state: Any,
    get_operation: Callable[[str], Mapping[str, Any] | None],
) -> int:
    """Close STARTED ledger records against actual operation state after restart.

    Reconciliation never creates new usage and never creates new attempt
    records — it only closes records whose operation reached a terminal state
    (or disappeared) while the process was down.  Returns closed count.
    """
    closed = 0
    for rec in list(state.attempt_ledger):
        if rec.get("status") not in _OPEN:
            continue
        op_id = rec.get("operation_id")
        if not op_id:
            # No operation was ever attached: the process died mid-dispatch.
            rec = dict(rec)
            rec["status"] = AttemptStatus.ABANDONED.value
            rec["finished_at"] = _null_ts()
            rec["reconciled_at_restart"] = True
            _replace(state, rec["attempt_id"], rec)
            closed += 1
            continue
        op = get_operation(str(op_id))
        if op is None:
            rec = dict(rec)
            rec["status"] = AttemptStatus.CANCELLED.value  # operation lost
            rec["finished_at"] = _null_ts()
            rec["reconciled_at_restart"] = True
            _replace(state, rec["attempt_id"], rec)
            closed += 1
            continue
        status = str(op.get("status", "")).upper()
        if status in _OPERATION_TERMINAL:
            rec = dict(rec)
            if status == "COMPLETED":
                rec["status"] = AttemptStatus.SUCCEEDED.value
            elif status == "FAILED":
                rec["status"] = AttemptStatus.FAILED.value
            elif status == "CANCELLED":
                rec["status"] = AttemptStatus.CANCELLED.value
            else:  # BLOCKED → escalated evidence
                rec["status"] = AttemptStatus.FAILED.value
                rec["failure_code"] = rec.get("failure_code") or "OPERATION_BLOCKED"
            rec["finished_at"] = _null_ts()
            rec["reconciled_at_restart"] = True
            _replace(state, rec["attempt_id"], rec)
            closed += 1
        # else: operation still active → keep STARTED; the next advance()
        # observation closes it normally.  No duplicate record is created
        # because start/record paths index by operation_id.
    return closed


def _replace(state: Any, attempt_id: str, new_rec: dict[str, Any]) -> None:
    ledger = state.attempt_ledger
    for idx, rec in enumerate(ledger):
        if rec.get("attempt_id") == attempt_id:
            ledger[idx] = new_rec
            return
    ledger.append(new_rec)


def find_active_attempt_for_operation(state: Any, operation_id: str | None) -> dict[str, Any] | None:
    """Open ledger record bound to *operation_id*, if any."""
    if not operation_id:
        return None
    attempt_id = state.attempt_op_index.get(str(operation_id))
    if not attempt_id:
        return None
    for rec in state.attempt_ledger:
        if rec.get("attempt_id") == attempt_id and rec.get("status") in _OPEN:
            return rec
    return None


# ── Serialization validation & legacy migration ─────────────────────────


def validate_ledger(state: Any) -> None:
    """Fail closed on corrupted authoritative accounting state."""
    ledger = getattr(state, "attempt_ledger", None)
    if ledger is None:
        raise AttemptLedgerCorruptionError("attempt ledger field missing")
    if not isinstance(ledger, list):
        raise AttemptLedgerCorruptionError(
            f"attempt ledger is {type(ledger).__name__}, not a list"
        )
    seen_ids: set[str] = set()
    for entry in ledger:
        rec = AttemptRecord.from_dict(entry)  # raises AttemptLedgerCorruptionError
        if rec.attempt_id in seen_ids:
            raise AttemptLedgerCorruptionError(
                f"attempt ledger contains duplicate attempt_id {rec.attempt_id!r}"
            )
        seen_ids.add(rec.attempt_id)
    usage = getattr(state, "budget_usage", None)
    if usage is None or not isinstance(usage, dict):
        raise AttemptLedgerCorruptionError("budget usage state is missing or malformed")
    for budget, scopes in usage.items():
        if not isinstance(scopes, dict):
            raise AttemptLedgerCorruptionError(
                f"budget usage for {budget} is not scope-keyed"
            )
        for scope, value in scopes.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AttemptLedgerCorruptionError(
                    f"budget usage {budget}/{scope} has invalid value {value!r}"
                )


def migrate_legacy_run_state(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]], list[dict[str, Any]]]:
    """Migrate pre-008C serialized RunState into the new accounting model.

    Deterministic and idempotent.  Legacy counters are preserved verbatim and
    additionally projected into ``budget_usage`` with explicit legacy
    provenance annotations (never fabricated attempt details, never treated as
    "unused budget").  Returns ``(attempt_ledger_marked_legacy, budget_usage,
    budget_renewals)``.
    """
    ledger = list(data.get("attempt_ledger") or [])
    usage = dict(data.get("budget_usage") or {})
    renewals = list(data.get("budget_renewals") or [])

    if data.get("attempt_ledger_008c") or data.get("budget_usage_008c"):
        return ledger, usage, renewals  # already migrated — idempotent

    mapped: dict[str, tuple[str, str]] = {
        "retry_counts": (BudgetKey.WORKER_TASK.value, None),
        "attempt_counts": (BudgetKey.PROVIDER_EXECUTION.value, None),
        "recovery_attempts": (BudgetKey.ARCHITECT_RECOVERY.value, None),
        "qa_reworks": (BudgetKey.QA_REWORK.value, None),
        "qa_review_attempts": (BudgetKey.QA_REVIEW.value, None),
    }
    for counter, (budget, _fixed_scope) in mapped.items():
        values = data.get(counter) or {}
        if not isinstance(values, dict):
            continue
        for scope, value in values.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                raise AttemptLedgerCorruptionError(
                    f"legacy counter {counter}[{scope!r}] is not an integer: {value!r}"
                )
            if count < 0:
                raise AttemptLedgerCorruptionError(
                    f"legacy counter {counter}[{scope!r}] is negative: {count}"
                )
            usage.setdefault(budget, {})
            usage[budget][scope] = max(int(usage[budget].get(scope, 0)), count)

    scalar_map: dict[str, tuple[str, str]] = {
        "planning_retries": (BudgetKey.PLANNING_REPAIR.value, "RUN"),
        "authoring_repair_attempts": (BudgetKey.AUTHORING_REPAIR.value, "RUN"),
        "final_review_rounds": (BudgetKey.FINAL_REVIEW.value, "RUN"),
        "gitops_retries": (BudgetKey.GITOPS.value, "RUN"),
        "integration_retries": (BudgetKey.INTEGRATION_REVIEW.value, "RUN"),
    }
    for counter, (budget, scope) in scalar_map.items():
        try:
            count = int(data.get(counter, 0) or 0)
        except (TypeError, ValueError):
            raise AttemptLedgerCorruptionError(
                f"legacy counter {counter} is not an integer: {data.get(counter)!r}"
            )
        usage.setdefault(budget, {})
        usage[budget][scope] = max(int(usage[budget].get(scope, 0)), count)

    # Dispatch failure records carry their own attempt bookkeeping (008A).
    dispatch_failures = data.get("dispatch_failures") or {}
    if isinstance(dispatch_failures, dict):
        for wo_id, rec in dispatch_failures.items():
            if not isinstance(rec, dict):
                continue
            try:
                attempts = int(rec.get("attempts", 0) or 0)
            except (TypeError, ValueError):
                raise AttemptLedgerCorruptionError(
                    f"dispatch_failures[{wo_id!r}]['attempts'] is not an integer"
                )
            usage.setdefault(BudgetKey.DISPATCH.value, {})
            usage[BudgetKey.DISPATCH.value][str(wo_id)] = max(
                int(usage[BudgetKey.DISPATCH.value].get(str(wo_id), 0)), attempts
            )

    # Mark legacy provenance in the ledger (no fabricated historial records).
    migrated_board = []
    for rec in ledger:
        migrated_board.append(rec)
    for budget, scopes in usage.items():
        for scope, value in scopes.items():
            if value <= 0:
                continue
            migrated_board.append(
                {
                    "attempt_id": f"legacy-{budget}-{scope}"[:64],
                    "subsystem": "legacy",
                    "action": f"migrated_usage:{budget}",
                    "budget_key": budget,
                    "scope": scope,
                    "status": AttemptStatus.ABANDONED.value,
                    "legacy": True,
                    "metadata": {
                        "legacy_usage": value,
                        "provenance": "migrated from pre-008C counters; details unavailable",
                    },
                    "started_at": _null_ts(),
                    "finished_at": _null_ts(),
                }
            )
    return migrated_board, usage, renewals


def append_budget_usage(
    state: Any, budget: str, scope: str, value: int, *, take_max: bool = True
) -> None:
    """External reconciliation hook (e.g. legacy counters changed elsewhere)."""
    usage = int(state.budget_usage.get(budget, {}).get(scope, 0))
    if take_max:
        state.budget_usage.setdefault(budget, {})[scope] = max(usage, value)
    else:
        state.budget_usage.setdefault(budget, {})[scope] = usage + int(value)


__all__ = [
    "AttemptLedgerCorruptionError",
    "AttemptLedgerError",
    "AttemptRecord",
    "AttemptStatus",
    "BudgetKey",
    "BudgetRenewalDenied",
    "FailureClass",
    "RetryContext",
    "RetryDecision",
    "RetryOutcome",
    "classify_failure",
    "compute_input_fingerprint",
    "evaluate_retry_budget",
    "finish_attempt",
    "find_active_attempt_for_operation",
    "migrate_legacy_run_state",
    "record_deferral",
    "reconcile_attempts_after_restart",
    "renew_budget",
    "start_attempt",
    "validate_ledger",
]
