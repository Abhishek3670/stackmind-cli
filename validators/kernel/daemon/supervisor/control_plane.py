"""Unified supervisor control plane, blocker ownership, deadlock detection, and state reconciliation (SDP-FIX-008D / HF2).

One authoritative controller that reconciles all lifecycle states and guarantees
that every non-terminal run has a valid progress path, an explicit wait condition,
or an actionable blocker.

Provides:
- Canonical supervisor state machine & invariant evaluation (008D-A / 008D-B / HF2-A)
- Canonical blocker registry with strict single-ownership & versioning (008D-C / HF2-E)
- Canonical wait-condition registry with lifecycle & predicates (008D-D)
- Deterministic deadlock detector distinguishing true cycles from legitimate waits (008D-E / HF2-B / HF2-I)
- Comprehensive per-work-order dispatch eligibility evaluation (HF2-D)
- Meaningful progress monitor and no-progress protection (008D-F / HF2-H)
- Two-stage fingerprint-bound restart reconciliation (008D-G / HF2-C)
- Side-effect-free transition validation (HF2-F)
- Authoritative terminal state model (HF2-G)
- Event-sourced lifecycle audit logging (008D-I)
- Concurrency, ownership, and dispatch mutexes (008D-J / HF2-J)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping

from validators.clock import now as _now

from .attempts import (
    AttemptStatus,
    BudgetKey,
    FailureClass,
    evaluate_retry_budget,
)
from .models import (
    _OPERATION_TERMINAL,
    GOVERNANCE_FAILURE_CODES,
    TRANSIENT_RETRYABLE_CODES,
    Phase,
    RunState,
    StateIntegrityError,
)

logger = logging.getLogger("stackmind.supervisor.control_plane")

# ── 1. Canonical State & Subsystem Enums ──────────────────────────────────


class ControllerState(StrEnum):
    """Authoritative coarse lifecycle states for the supervisor control plane."""

    RUNNABLE = "RUNNABLE"
    EXECUTING = "EXECUTING"
    WAITING = "WAITING"
    RECOVERABLE_BLOCKED = "RECOVERABLE_BLOCKED"
    TERMINAL = "TERMINAL"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class BlockerStatus(StrEnum):
    """Lifecycle states for a canonical blocker record."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLUTION_REQUESTED = "RESOLUTION_REQUESTED"
    RESOLVED = "RESOLVED"
    REOPENED = "REOPENED"
    SUPERSEDED = "SUPERSEDED"


class BlockerType(StrEnum):
    """Categorical taxonomy of lifecycle blockers."""

    WORKER_FAILURE = "WORKER_FAILURE"
    DISPATCH_FAILURE = "DISPATCH_FAILURE"
    INVALID_DEPENDENCY = "INVALID_DEPENDENCY"
    DEPENDENCY_DEADLOCK = "DEPENDENCY_DEADLOCK"
    QA_FAILURE = "QA_FAILURE"
    QA_REJECTION = "QA_REJECTION"
    INTEGRATION_FAILURE = "INTEGRATION_FAILURE"
    GITOPS_FAILURE = "GITOPS_FAILURE"
    HUMAN_APPROVAL_PENDING = "HUMAN_APPROVAL_PENDING"
    RECOVERY_DECISION_PENDING = "RECOVERY_DECISION_PENDING"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    CONTRACT_SCOPE_DENIED = "CONTRACT_SCOPE_DENIED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    PLAN_AMENDMENT_REQUIRED = "PLAN_AMENDMENT_REQUIRED"
    DELIVERABLE_HASH_MISMATCH = "DELIVERABLE_HASH_MISMATCH"
    CIRCULAR_DEPENDENCY = "CIRCULAR_DEPENDENCY"
    PREREQUISITE_FAILED = "PREREQUISITE_FAILED"
    EXECUTION_CRASH = "EXECUTION_CRASH"
    OPERATION_TIMEOUT = "OPERATION_TIMEOUT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_PROGRESS_LOOP = "NO_PROGRESS_LOOP"
    SCHEDULER_DEADLOCK = "SCHEDULER_DEADLOCK"
    RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"
    SCOPE_BREACH = "SCOPE_BREACH"
    DEPENDENCY = "DEPENDENCY"
    QA_REWORK = "QA_REWORK"
    GOVERNANCE_HALT = "GOVERNANCE_HALT"


class BlockerSubsystem(StrEnum):
    """Accountable owning subsystems for blocker resolution."""

    EXECUTION = "execution"
    DISPATCH = "dispatch"
    DEPENDENCY = "dependency"
    SUPERVISOR = "supervisor"
    QA_REVIEW = "qa_review"
    QA = "qa_review"
    INTEGRATION = "integration"
    GITOPS = "gitops"
    APPROVAL = "approval"
    RECOVERY = "recovery"
    PROVIDER = "provider"
    GOVERNANCE = "governance"
    ARCHITECTURE = "architecture"
    VALIDATION = "validation"
    RESOURCE = "resource"
    CONTRACT = "contract"


class WaitStatus(StrEnum):
    """Lifecycle states of an explicit wait condition."""

    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    INVALID = "INVALID"
    EXPIRED = "EXPIRED"
    ESCALATED = "ESCALATED"


class WaitType(StrEnum):
    """Categorical types of supervisor wait conditions."""

    OPERATION = "OPERATION"
    DEPENDENCY = "DEPENDENCY"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    APPROVAL = "HUMAN_APPROVAL"
    RECOVERY_DECISION = "RECOVERY_DECISION"
    RESOURCE_CONTENTION = "RESOURCE_CONTENTION"
    PROVIDER_EXECUTION = "PROVIDER_EXECUTION"
    COOLDOWN_DELAY = "COOLDOWN_DELAY"
    WAITING_FOR_OPERATION = "WAITING_FOR_OPERATION"


class DeadlockType(StrEnum):
    """Categorical taxonomy of lifecycle deadlock conditions."""

    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    OPERATION_DEADLOCK = "OPERATION_DEADLOCK"
    BLOCKER_DEADLOCK = "BLOCKER_DEADLOCK"
    RECOVERY_DEADLOCK = "RECOVERY_DEADLOCK"
    APPROVAL_DEADLOCK = "APPROVAL_DEADLOCK"
    SCHEDULER_DEADLOCK = "SCHEDULER_DEADLOCK"
    ORPHANED_WAIT = "ORPHANED_WAIT"
    BUDGET_DEADLOCK = "BUDGET_DEADLOCK"


# Category alias for architectural parity with 008D-HF2 specifications
DeadlockCategory = DeadlockType


class ReconciliationOutcome(StrEnum):
    """Outcome of reconciling a single persisted lifecycle entity."""

    CONSISTENT = "CONSISTENT"
    STALE = "STALE"
    MISSING = "MISSING"
    CONTRADICTORY = "CONTRADICTORY"
    CORRUPT = "CORRUPT"
    UNVERIFIABLE = "UNVERIFIABLE"


# ── 2. Data Models ───────────────────────────────────────────────────────


@dataclass
class AuthorizationContext:
    """Explicit typed authorization provenance for privileged lifecycle transitions."""

    actor: str
    role: str
    source: str = "supervisor"
    action: str = "override"
    reason: str = ""
    decision_id: str | None = None
    run_id: str | None = None
    expected_version: int | None = None
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    authorized_at: str = ""
    event_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "role": self.role,
            "source": self.source,
            "action": self.action,
            "reason": self.reason,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "expected_version": self.expected_version,
            "evidence_refs": dict(self.evidence_refs),
            "authorized_at": self.authorized_at,
            "event_seq": self.event_seq,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuthorizationContext":
        return cls(
            actor=str(data.get("actor", "unknown")),
            role=str(data.get("role", "unknown")),
            source=str(data.get("source", "supervisor")),
            action=str(data.get("action", "override")),
            reason=str(data.get("reason", "")),
            decision_id=data.get("decision_id"),
            run_id=data.get("run_id"),
            expected_version=int(data["expected_version"]) if data.get("expected_version") is not None else None,
            evidence_refs=dict(data.get("evidence_refs", {}) or {}),
            authorized_at=str(data.get("authorized_at", "")),
            event_seq=int(data.get("event_seq", 0)),
        )


@dataclass
class SupervisorDecision:
    """Authoritative decision produced by evaluate_supervisor_state."""

    current_state: ControllerState
    proposed_state: ControllerState
    decision_reason: str
    required_preconditions: list[str] = field(default_factory=list)
    active_operation_refs: list[str] = field(default_factory=list)
    active_blocker_refs: list[str] = field(default_factory=list)
    wait_condition_refs: list[str] = field(default_factory=list)
    recovery_eligibility: bool = False
    relevant_wo_ids: list[str] = field(default_factory=list)
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    decision_timestamp: str = ""
    event_seq: int = 0
    state_fingerprint: str = ""
    is_valid: bool = True
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_state": self.current_state.value,
            "proposed_state": self.proposed_state.value,
            "decision_reason": self.decision_reason,
            "required_preconditions": list(self.required_preconditions),
            "active_operation_refs": list(self.active_operation_refs),
            "active_blocker_refs": list(self.active_blocker_refs),
            "wait_condition_refs": list(self.wait_condition_refs),
            "recovery_eligibility": bool(self.recovery_eligibility),
            "relevant_wo_ids": list(self.relevant_wo_ids),
            "evidence_refs": dict(self.evidence_refs),
            "decision_timestamp": self.decision_timestamp,
            "event_seq": self.event_seq,
            "state_fingerprint": self.state_fingerprint,
            "is_valid": bool(self.is_valid),
            "violations": list(self.violations),
        }


@dataclass
class BlockerRecord:
    """Authoritative record for an open or historical blocker."""

    blocker_id: str
    run_id: str
    work_order_id: str | None
    owner_subsystem: str
    owner_actor: str
    blocker_type: str
    failure_class: str
    failure_code: str
    status: BlockerStatus
    severity: str = "ERROR"
    created_at: str = ""
    updated_at: str = ""
    resolution_condition: str = ""
    resolution_actor: str | None = None
    resolution_event_id: str | None = None
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    attempt_id: str | None = None
    operation_id: str | None = None
    dependency_refs: list[str] = field(default_factory=list)
    input_fingerprint: str | None = None
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocker_id": self.blocker_id,
            "run_id": self.run_id,
            "work_order_id": self.work_order_id,
            "owner_subsystem": str(self.owner_subsystem),
            "owner_actor": self.owner_actor,
            "blocker_type": str(self.blocker_type),
            "failure_class": str(self.failure_class),
            "failure_code": self.failure_code,
            "status": self.status.value if isinstance(self.status, BlockerStatus) else str(self.status),
            "severity": self.severity,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolution_condition": self.resolution_condition,
            "resolution_actor": self.resolution_actor,
            "resolution_event_id": self.resolution_event_id,
            "evidence_refs": dict(self.evidence_refs),
            "attempt_id": self.attempt_id,
            "operation_id": self.operation_id,
            "dependency_refs": list(self.dependency_refs),
            "input_fingerprint": self.input_fingerprint,
            "version": int(self.version),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BlockerRecord":
        status_val = data.get("status", BlockerStatus.OPEN.value)
        status = BlockerStatus(status_val) if status_val in BlockerStatus._value2member_map_ else BlockerStatus.OPEN
        return cls(
            blocker_id=str(data.get("blocker_id", "unknown_blocker")),
            run_id=str(data.get("run_id", "unknown_run")),
            work_order_id=data.get("work_order_id"),
            owner_subsystem=str(data.get("owner_subsystem", BlockerSubsystem.SUPERVISOR.value)),
            owner_actor=str(data.get("owner_actor", "system")),
            blocker_type=str(data.get("blocker_type", BlockerType.WORKER_FAILURE.value)),
            failure_class=str(data.get("failure_class", "TRANSIENT")),
            failure_code=str(data.get("failure_code", "FAILURE")),
            status=status,
            severity=str(data.get("severity", "ERROR")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            resolution_condition=str(data.get("resolution_condition", "")),
            resolution_actor=data.get("resolution_actor"),
            resolution_event_id=data.get("resolution_event_id"),
            evidence_refs=dict(data.get("evidence_refs", {}) or {}),
            attempt_id=data.get("attempt_id"),
            operation_id=data.get("operation_id"),
            dependency_refs=list(data.get("dependency_refs", []) or []),
            input_fingerprint=data.get("input_fingerprint"),
            version=int(data.get("version", 1)),
            metadata=dict(data.get("metadata", {}) or {}),
        )



@dataclass
class WaitRecord:
    """Authoritative record for an explicit supervisor wait condition."""

    wait_id: str
    run_id: str
    wait_type: WaitType
    owner_subsystem: str
    subject_entity: str
    status: WaitStatus
    resolution_predicate: str
    last_evaluation_event: str = ""
    last_evaluation_result: str = ""
    relevant_ref: str | None = None
    expiry_seconds: float | None = None
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wait_id": self.wait_id,
            "run_id": self.run_id,
            "wait_type": self.wait_type.value if isinstance(self.wait_type, WaitType) else str(self.wait_type),
            "owner_subsystem": str(self.owner_subsystem),
            "subject_entity": self.subject_entity,
            "status": self.status.value if isinstance(self.status, WaitStatus) else str(self.status),
            "resolution_predicate": self.resolution_predicate,
            "last_evaluation_event": self.last_evaluation_event,
            "last_evaluation_result": self.last_evaluation_result,
            "relevant_ref": self.relevant_ref,
            "expiry_seconds": self.expiry_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": int(self.version),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WaitRecord":
        wtype_val = data.get("wait_type", WaitType.OPERATION.value)
        wtype = WaitType(wtype_val) if wtype_val in WaitType._value2member_map_ else WaitType.OPERATION
        status_val = data.get("status", WaitStatus.ACTIVE.value)
        status = WaitStatus(status_val) if status_val in WaitStatus._value2member_map_ else WaitStatus.ACTIVE
        return cls(
            wait_id=str(data.get("wait_id", "unknown_wait")),
            run_id=str(data.get("run_id", "unknown_run")),
            wait_type=wtype,
            owner_subsystem=str(data.get("owner_subsystem", BlockerSubsystem.SUPERVISOR.value)),
            subject_entity=str(data.get("subject_entity", "")),
            status=status,
            resolution_predicate=str(data.get("resolution_predicate", "")),
            last_evaluation_event=str(data.get("last_evaluation_event", "")),
            last_evaluation_result=str(data.get("last_evaluation_result", "")),
            relevant_ref=data.get("relevant_ref"),
            expiry_seconds=data.get("expiry_seconds"),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            version=int(data.get("version", 1)),
            metadata=dict(data.get("metadata", {}) or {}),
        )




WaitConditionRecord = WaitRecord

@dataclass
class DeadlockReport:
    """Authoritative structured diagnostic for a detected deadlock."""

    deadlock_id: str
    run_id: str
    deadlock_type: DeadlockType
    severity: str
    affected_work_orders: list[str] = field(default_factory=list)
    affected_operations: list[str] = field(default_factory=list)
    affected_blockers: list[str] = field(default_factory=list)
    affected_waits: list[str] = field(default_factory=list)
    cycle_path: list[str] = field(default_factory=list)
    root_conditions: list[str] = field(default_factory=list)
    unavailable_resolution_paths: list[str] = field(default_factory=list)
    suggested_authorized_actions: list[str] = field(default_factory=list)
    detected_at: str = ""
    graph_fingerprint: str = ""
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    sub_reports: list["DeadlockReport"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deadlock_id": self.deadlock_id,
            "run_id": self.run_id,
            "deadlock_type": self.deadlock_type.value if isinstance(self.deadlock_type, DeadlockType) else str(self.deadlock_type),
            "severity": self.severity,
            "affected_work_orders": list(self.affected_work_orders),
            "affected_operations": list(self.affected_operations),
            "affected_blockers": list(self.affected_blockers),
            "affected_waits": list(self.affected_waits),
            "cycle_path": list(self.cycle_path),
            "root_conditions": list(self.root_conditions),
            "unavailable_resolution_paths": list(self.unavailable_resolution_paths),
            "suggested_authorized_actions": list(self.suggested_authorized_actions),
            "detected_at": self.detected_at,
            "graph_fingerprint": self.graph_fingerprint,
            "evidence_refs": dict(self.evidence_refs),
            "sub_reports": [sr.to_dict() for sr in self.sub_reports],
        }


@dataclass
class ReconciliationItem:
    """Discrepancy identified during Stage 1 read-only reconciliation."""

    entity_type: str
    entity_id: str
    outcome: ReconciliationOutcome
    persisted_value: Any
    authoritative_value: Any
    discrepancy_reason: str
    requires_authorization: bool = False
    suggested_correction: str | None = None
    applied_correction: str | None = None
    expected_version: int | None = None
    entity_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "outcome": self.outcome.value if isinstance(self.outcome, ReconciliationOutcome) else str(self.outcome),
            "persisted_value": self.persisted_value,
            "authoritative_value": self.authoritative_value,
            "discrepancy_reason": self.discrepancy_reason,
            "requires_authorization": bool(self.requires_authorization),
            "suggested_correction": self.suggested_correction,
            "applied_correction": self.applied_correction,
            "expected_version": self.expected_version,
            "entity_fingerprint": self.entity_fingerprint,
        }


@dataclass
class ReconciliationSnapshot:
    """Immutable state snapshot identity binding Stage 1 analysis to Stage 2 repair."""

    snapshot_id: str
    run_id: str
    state_fingerprint: str
    run_state_version: int
    operation_graph_fingerprint: str
    blocker_registry_fingerprint: str
    wait_registry_fingerprint: str
    dependency_graph_fingerprint: str
    attempt_ledger_fingerprint: str
    event_sequence: int
    created_at: str
    schema_version: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "state_fingerprint": self.state_fingerprint,
            "run_state_version": self.run_state_version,
            "operation_graph_fingerprint": self.operation_graph_fingerprint,
            "blocker_registry_fingerprint": self.blocker_registry_fingerprint,
            "wait_registry_fingerprint": self.wait_registry_fingerprint,
            "dependency_graph_fingerprint": self.dependency_graph_fingerprint,
            "attempt_ledger_fingerprint": self.attempt_ledger_fingerprint,
            "event_sequence": self.event_sequence,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReconciliationSnapshot":
        return cls(
            snapshot_id=str(data.get("snapshot_id", "")),
            run_id=str(data.get("run_id", "")),
            state_fingerprint=str(data.get("state_fingerprint", "")),
            run_state_version=int(data.get("run_state_version", 1)),
            operation_graph_fingerprint=str(data.get("operation_graph_fingerprint", "")),
            blocker_registry_fingerprint=str(data.get("blocker_registry_fingerprint", "")),
            wait_registry_fingerprint=str(data.get("wait_registry_fingerprint", "")),
            dependency_graph_fingerprint=str(data.get("dependency_graph_fingerprint", "")),
            attempt_ledger_fingerprint=str(data.get("attempt_ledger_fingerprint", "")),
            event_sequence=int(data.get("event_sequence", 0)),
            created_at=str(data.get("created_at", "")),
            schema_version=str(data.get("schema_version", "2.0")),
        )


@dataclass
class ReconciliationReport:
    """Authoritative outcome of Stage 1 or Stage 2 restart reconciliation."""

    reconciliation_id: str
    run_id: str
    stage: str
    status: str
    items: list[ReconciliationItem] = field(default_factory=list)
    created_at: str = ""
    fingerprint: str = ""
    state_fingerprint: str = ""
    schema_version: str = "2.0"
    run_state_version: int = 1
    event_sequence: int = 0
    snapshot: ReconciliationSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
            "state_fingerprint": self.state_fingerprint,
            "schema_version": self.schema_version,
            "run_state_version": self.run_state_version,
            "event_sequence": self.event_sequence,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }


@dataclass
class EligibilityDecision:
    """Comprehensive per-work-order dispatch eligibility decision."""

    work_order_id: str
    is_eligible: bool
    status: str
    decision_reason: str
    contract_status: str
    dependency_status: str
    completion_status: str
    blocker_refs: list[str] = field(default_factory=list)
    wait_refs: list[str] = field(default_factory=list)
    active_op_refs: list[str] = field(default_factory=list)
    approval_required: bool = False
    attempt_budget_remaining: int = 1
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    state_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "is_eligible": bool(self.is_eligible),
            "status": str(self.status),
            "decision_reason": self.decision_reason,
            "contract_status": self.contract_status,
            "dependency_status": self.dependency_status,
            "completion_status": self.completion_status,
            "blocker_refs": list(self.blocker_refs),
            "wait_refs": list(self.wait_refs),
            "active_op_refs": list(self.active_op_refs),
            "approval_required": bool(self.approval_required),
            "attempt_budget_remaining": int(self.attempt_budget_remaining),
            "evidence_refs": dict(self.evidence_refs),
            "state_fingerprint": self.state_fingerprint,
        }


@dataclass
class LifecycleEvent:
    """Append-only audit event for supervisor lifecycle decisions."""

    event_id: str
    run_id: str
    entity_id: str
    actor: str
    event_type: str
    previous_state: str | None
    new_state: str
    event_seq: int
    reason_code: str
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    timestamp: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "entity_id": self.entity_id,
            "actor": self.actor,
            "event_type": self.event_type,
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "event_seq": self.event_seq,
            "reason_code": self.reason_code,
            "evidence_refs": dict(self.evidence_refs),
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
        }


# ── Terminal State Recognition Helpers ────────────────────────────────────


def is_run_terminal(state: RunState, manager: Any = None) -> bool:
    """Check if the supervisor run is in an authoritative terminal state."""
    if state.phase in (Phase.COMPLETE, Phase.FAILED):
        return True
    if str(getattr(state, "status", "")).upper() in ("COMPLETE", "COMPLETED", "FAILED", "CANCELLED", "TERMINATED"):
        return True
    if getattr(state, "exit_code", None) in (130, 137):
        return True
    if manager is not None:
        try:
            sessions_dict = getattr(manager, "_sessions", None)
            if isinstance(sessions_dict, dict):
                sess = sessions_dict.get(state.session_id)
                if sess and str(sess.get("state", "")).upper() in ("CANCELLED", "COMPLETED", "FAILED", "TERMINATED"):
                    return True
        except Exception:
            pass
    return False


def is_work_order_terminal(status: str | None) -> bool:
    """Check if a work order status represents finality."""
    if not status:
        return False
    return str(status).upper() in ("COMPLETED", "COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED", "ARCHIVED")


def is_operation_terminal(status: str | None) -> bool:
    """Check if an operation status represents finality."""
    if not status:
        return False
    st = str(status).upper()
    return st in _OPERATION_TERMINAL or st in ("TIMED_OUT", "TIMEOUT", "CANCEL_REQUESTED")


# ── 3. Lifecycle Audit Event Logger ─────────────────────────────────────


def record_lifecycle_event(
    state: RunState,
    event_type: str,
    entity_id: str,
    actor: str,
    previous_state: str | None,
    new_state: str,
    reason_code: str,
    evidence_refs: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> LifecycleEvent:
    """Append a structured lifecycle event to state.lifecycle_events monotonically."""
    if not hasattr(state, "lifecycle_events") or state.lifecycle_events is None:
        state.lifecycle_events = []
    if not hasattr(state, "lifecycle_event_seq") or state.lifecycle_event_seq is None:
        state.lifecycle_event_seq = 0

    state.lifecycle_event_seq += 1
    seq = state.lifecycle_event_seq
    event_id = f"evt-{state.run_id}-{seq:06d}"
    now = _now()

    event = LifecycleEvent(
        event_id=event_id,
        run_id=state.run_id,
        entity_id=entity_id,
        actor=actor,
        event_type=event_type,
        previous_state=previous_state,
        new_state=new_state,
        event_seq=seq,
        reason_code=reason_code,
        evidence_refs=dict(evidence_refs or {}),
        correlation_id=correlation_id,
        timestamp=now,
    )
    state.lifecycle_events.append(event.to_dict())
    return event


# ── 4. Canonical Blocker Registry & Single Ownership ────────────────────

_BLOCKER_OWNER_MAP: dict[str, str] = {
    BlockerType.WORKER_FAILURE: BlockerSubsystem.EXECUTION,
    BlockerType.DISPATCH_FAILURE: BlockerSubsystem.DISPATCH,
    BlockerType.INVALID_DEPENDENCY: BlockerSubsystem.DEPENDENCY,
    BlockerType.DEPENDENCY_DEADLOCK: BlockerSubsystem.SUPERVISOR,
    BlockerType.QA_FAILURE: BlockerSubsystem.QA_REVIEW,
    BlockerType.QA_REJECTION: BlockerSubsystem.VALIDATION,
    BlockerType.INTEGRATION_FAILURE: BlockerSubsystem.INTEGRATION,
    BlockerType.GITOPS_FAILURE: BlockerSubsystem.GITOPS,
    BlockerType.HUMAN_APPROVAL_PENDING: BlockerSubsystem.APPROVAL,
    BlockerType.RECOVERY_DECISION_PENDING: BlockerSubsystem.RECOVERY,
    BlockerType.PROVIDER_FAILURE: BlockerSubsystem.PROVIDER,
    BlockerType.CONTRACT_VIOLATION: BlockerSubsystem.GOVERNANCE,
    BlockerType.CONTRACT_SCOPE_DENIED: BlockerSubsystem.GOVERNANCE,
    BlockerType.POLICY_VIOLATION: BlockerSubsystem.GOVERNANCE,
    BlockerType.PLAN_AMENDMENT_REQUIRED: BlockerSubsystem.ARCHITECTURE,
    BlockerType.DELIVERABLE_HASH_MISMATCH: BlockerSubsystem.VALIDATION,
    BlockerType.CIRCULAR_DEPENDENCY: BlockerSubsystem.DEPENDENCY,
    BlockerType.PREREQUISITE_FAILED: BlockerSubsystem.DEPENDENCY,
    BlockerType.EXECUTION_CRASH: BlockerSubsystem.EXECUTION,
    BlockerType.OPERATION_TIMEOUT: BlockerSubsystem.EXECUTION,
    BlockerType.BUDGET_EXHAUSTED: BlockerSubsystem.RESOURCE,
    BlockerType.NO_PROGRESS_LOOP: BlockerSubsystem.EXECUTION,
    BlockerType.SCHEDULER_DEADLOCK: BlockerSubsystem.SUPERVISOR,
    BlockerType.RECONCILIATION_FAILURE: BlockerSubsystem.SUPERVISOR,
}

_BLOCKER_MUTEX = threading.Lock()

_SEMANTIC_EVIDENCE_KEYS = {
    "evidence_id",
    "action",
    "reason",
    "receipt",
    "verifier",
    "decision_id",
    "resolution_action",
    "patch_id",
    "route",
    "amendment_id",
    "approved_revision",
    "cleared_paths",
    "contract_path",
    "qa_verdict",
    "test_receipt",
    "verified_hashes",
    "test_output",
    "dependency_evaluation",
    "rewired_dependencies",
    "prerequisite_evidence",
    "recovery_decision_id",
    "operation_receipt",
    "exit_code",
    "governance_approval",
    "scope_authorization",
    "override_token",
    "override_approval",
    "turn",
    "ok",
    "test_run",
    "code",
    "status",
    "result",
    "verdict",
    "fix",
}


def create_blocker(
    state: RunState,
    blocker_type: str,
    owner_subsystem: str | None = None,
    owner_actor: str = "system",
    work_order_id: str | None = None,
    failure_class: str = "TRANSIENT",
    failure_code: str = "FAILURE",
    resolution_condition: str = "",
    evidence_refs: dict[str, Any] | None = None,
    attempt_id: str | None = None,
    operation_id: str | None = None,
    dependency_refs: list[str] | None = None,
    input_fingerprint: str | None = None,
    metadata: dict[str, Any] | None = None,
    reason: str = "",
    **kwargs: Any,
) -> BlockerRecord:
    """Create a canonical BlockerRecord in state.blocker_registry with single accountable ownership."""
    if reason:
        if failure_code == "FAILURE":
            failure_code = reason
        if not resolution_condition:
            resolution_condition = reason
    with _BLOCKER_MUTEX:
        if not hasattr(state, "blocker_registry") or state.blocker_registry is None:
            state.blocker_registry = {}
        if not hasattr(state, "blocker_history") or state.blocker_history is None:
            state.blocker_history = []

        subsystem = owner_subsystem or _BLOCKER_OWNER_MAP.get(blocker_type, BlockerSubsystem.SUPERVISOR)
        scope = work_order_id or "run"
        now = _now()
        seq = len(state.blocker_history) + 1
        blocker_id = f"blk-{state.run_id}-{scope}-{failure_code}-{seq:04d}"

        rec = BlockerRecord(
            blocker_id=blocker_id,
            run_id=state.run_id,
            work_order_id=work_order_id,
            owner_subsystem=str(subsystem),
            owner_actor=owner_actor,
            blocker_type=str(blocker_type),
            failure_class=str(failure_class),
            failure_code=failure_code,
            status=BlockerStatus.OPEN,
            created_at=now,
            updated_at=now,
            resolution_condition=resolution_condition,
            evidence_refs=dict(evidence_refs or {}),
            attempt_id=attempt_id,
            operation_id=operation_id,
            dependency_refs=list(dependency_refs or []),
            input_fingerprint=input_fingerprint,
            version=1,
            metadata=dict(metadata or {}),
        )

        state.blocker_registry[blocker_id] = rec.to_dict()
        state.blocker_history.append(rec.to_dict())

        # Backwards compatibility: keep blocked_wo_ids in sync
        if work_order_id and work_order_id not in state.blocked_wo_ids:
            state.blocked_wo_ids.append(work_order_id)

        record_lifecycle_event(
            state=state,
            event_type="BLOCKER_CREATED",
            entity_id=blocker_id,
            actor=owner_actor,
            previous_state=None,
            new_state=BlockerStatus.OPEN.value,
            reason_code=failure_code,
            evidence_refs=evidence_refs,
        )
        return rec


def resolve_blocker(
    state: RunState,
    blocker_id: str,
    actor: str,
    subsystem: str,
    expected_version: int | None = None,
    evidence: dict[str, Any] | None = None,
    resolution_event_id: str | None = None,
    authorized_override: bool = False,
    auth_context: AuthorizationContext | dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[bool, str]:
    """Resolve a blocker with single-ownership enforcement, version-bound concurrency, and typed evidence."""
    if isinstance(expected_version, Mapping):
        evidence = dict(expected_version)
        expected_version = kwargs.get("expected_version", None)
    elif evidence is None and "evidence" in kwargs:
        evidence = kwargs["evidence"]

    with _BLOCKER_MUTEX:
        if not hasattr(state, "blocker_registry") or blocker_id not in state.blocker_registry:
            return False, f"BLOCKER_NOT_FOUND: Blocker {blocker_id} does not exist in registry"

        raw = state.blocker_registry[blocker_id]
        rec = BlockerRecord.from_dict(raw)

        if rec.status in (BlockerStatus.RESOLVED, BlockerStatus.SUPERSEDED):
            return False, f"ALREADY_TERMINAL / ALREADY_RESOLVED: Blocker {blocker_id} is already {rec.status.value}"

        # 1. Version-bound concurrency check
        if expected_version is not None and rec.version != expected_version:
            return False, (
                f"STALE_VERSION_REJECTED / BLOCKER_VERSION_MISMATCH: Blocker {blocker_id} version is {rec.version}, "
                f"expected {expected_version}"
            )

        # 2. Authorization provenance resolution
        parsed_auth: AuthorizationContext | None = None
        if auth_context is not None:
            if isinstance(auth_context, AuthorizationContext):
                parsed_auth = auth_context
            elif isinstance(auth_context, Mapping):
                parsed_auth = AuthorizationContext.from_dict(auth_context)

        # 3. Single accountable ownership check
        normalized_subsystem = str(subsystem).lower()
        owner_subsystem = str(rec.owner_subsystem).lower()
        actor_role = str(parsed_auth.role if parsed_auth else actor).lower()

        # Worker role cannot resolve governance blockers
        if owner_subsystem in ("governance", "architecture") and actor_role in ("worker", "backend", "frontend", "dev"):
            return False, (
                f"UNAUTHORIZED_RESOLVER: Role '{actor_role}' is not authorized to resolve "
                f"{owner_subsystem.upper()} blocker {blocker_id}"
            )

        # Check subsystem match
        if normalized_subsystem != owner_subsystem:
            has_override_authority = (
                (authorized_override and parsed_auth is not None and parsed_auth.role in ("architect", "human", "admin", "governance", "supervisor"))
                or (authorized_override and actor_role in ("architect", "human", "admin", "supervisor", "recovery"))
            )
            if not has_override_authority:
                return False, (
                    f"UNAUTHORIZED_RESOLVER: Blocker {blocker_id} is owned by '{rec.owner_subsystem}', "
                    f"cannot be resolved by '{subsystem}' without valid override authorization provenance"
                )

        # 4. Mandatory typed resolution evidence
        if not evidence or not isinstance(evidence, Mapping) or len(evidence) == 0:
            return False, "EVIDENCE_REQUIRED: Blocker resolution requires non-empty verified evidence"

        # Check evidence contains semantic proof keys (reject arbitrary non-semantic dicts)
        has_semantic_key = any(
            k in _SEMANTIC_EVIDENCE_KEYS
            or any(s in k.lower() for s in ("test", "verdict", "run", "receipt", "result", "evidence", "proof", "report", "approval", "hash", "revision", "action", "status", "turn", "patch", "diff"))
            for k in evidence.keys()
        )
        if not has_semantic_key:
            return False, (
                f"INVALID_EVIDENCE: Resolution evidence lacks recognized semantic verification keys. "
                f"Received keys: {list(evidence.keys())}"
            )

        # Check work-order ID consistency
        ev_wo = evidence.get("work_order_id")
        if ev_wo and rec.work_order_id and str(ev_wo) != str(rec.work_order_id):
            return False, (
                f"EVIDENCE_MISMATCH: Evidence references work order {ev_wo}, "
                f"expected {rec.work_order_id}"
            )

        # Check revision freshness if revision is declared in evidence
        ev_rev = evidence.get("revision")
        if ev_rev is not None and rec.work_order_id:
            current_rev = (getattr(state, "contract_revisions", {}) or {}).get(rec.work_order_id, 1)
            if int(ev_rev) < current_rev:
                return False, (
                    f"STALE_REVISION_EVIDENCE: Evidence revision {ev_rev} is older than "
                    f"current work order revision {current_rev}"
                )

        # 5. Apply mutation atomically
        now = _now()
        rec.status = BlockerStatus.RESOLVED
        rec.resolution_actor = actor
        rec.resolution_event_id = resolution_event_id
        rec.evidence_refs.update(evidence)
        if parsed_auth:
            rec.metadata["authorization_provenance"] = parsed_auth.to_dict()
        rec.updated_at = now
        rec.version += 1

        state.blocker_registry[blocker_id] = rec.to_dict()
        state.blocker_history.append(rec.to_dict())

        # Backwards compatibility sync: remove from blocked_wo_ids if no other open blocker exists
        if rec.work_order_id and rec.work_order_id in state.blocked_wo_ids:
            other_active = False
            for bid, bdata in state.blocker_registry.items():
                if bid != blocker_id and bdata.get("work_order_id") == rec.work_order_id:
                    if bdata.get("status") in (
                        BlockerStatus.OPEN.value,
                        BlockerStatus.ACKNOWLEDGED.value,
                        BlockerStatus.RESOLUTION_REQUESTED.value,
                        BlockerStatus.REOPENED.value,
                    ):
                        other_active = True
                        break
            if not other_active:
                state.blocked_wo_ids.remove(rec.work_order_id)

        record_lifecycle_event(
            state=state,
            event_type="BLOCKER_RESOLVED",
            entity_id=blocker_id,
            actor=actor,
            previous_state=BlockerStatus.OPEN.value,
            new_state=BlockerStatus.RESOLVED.value,
            reason_code="RESOLVED_WITH_EVIDENCE",
            evidence_refs=evidence,
        )
        return True, "BLOCKER_RESOLVED"


def reopen_blocker(
    state: RunState,
    blocker_id: str,
    actor: str,
    subsystem: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Reopen a previously resolved blocker upon regression or failed rework."""
    with _BLOCKER_MUTEX:
        if not hasattr(state, "blocker_registry") or blocker_id not in state.blocker_registry:
            return False, f"BLOCKER_NOT_FOUND: Blocker {blocker_id} does not exist"
        raw = state.blocker_registry[blocker_id]
        rec = BlockerRecord.from_dict(raw)

        if rec.status != BlockerStatus.RESOLVED:
            return False, f"CANNOT_REOPEN: Blocker {blocker_id} is in status {rec.status.value}"

        now = _now()
        rec.status = BlockerStatus.REOPENED
        rec.updated_at = now
        rec.version += 1
        if evidence:
            rec.evidence_refs.update(evidence)

        state.blocker_registry[blocker_id] = rec.to_dict()
        state.blocker_history.append(rec.to_dict())

        if rec.work_order_id and rec.work_order_id not in state.blocked_wo_ids:
            state.blocked_wo_ids.append(rec.work_order_id)

        record_lifecycle_event(
            state=state,
            event_type="BLOCKER_REOPENED",
            entity_id=blocker_id,
            actor=actor,
            previous_state=BlockerStatus.RESOLVED.value,
            new_state=BlockerStatus.REOPENED.value,
            reason_code=reason,
            evidence_refs=evidence,
        )
        return True, "BLOCKER_REOPENED"


def supersede_blocker(
    state: RunState,
    blocker_id: str,
    new_blocker_id: str,
    actor: str,
    subsystem: str,
    reason: str,
) -> tuple[bool, str]:
    """Mark an old blocker SUPERSEDED when replaced by a new authoritative blocker."""
    with _BLOCKER_MUTEX:
        if not hasattr(state, "blocker_registry") or blocker_id not in state.blocker_registry:
            return False, f"BLOCKER_NOT_FOUND: Blocker {blocker_id} does not exist"
        raw = state.blocker_registry[blocker_id]
        rec = BlockerRecord.from_dict(raw)

        now = _now()
        rec.status = BlockerStatus.SUPERSEDED
        rec.updated_at = now
        rec.version += 1
        rec.metadata["superseded_by"] = new_blocker_id
        rec.metadata["superseded_reason"] = reason

        state.blocker_registry[blocker_id] = rec.to_dict()
        state.blocker_history.append(rec.to_dict())

        record_lifecycle_event(
            state=state,
            event_type="BLOCKER_SUPERSEDED",
            entity_id=blocker_id,
            actor=actor,
            previous_state=raw.get("status"),
            new_state=BlockerStatus.SUPERSEDED.value,
            reason_code=reason,
        )
        return True, "BLOCKER_SUPERSEDED"


def get_active_blockers(state: RunState, work_order_id: str | None = None) -> list[BlockerRecord]:
    """Retrieve all active (OPEN/ACKNOWLEDGED/RESOLUTION_REQUESTED/REOPENED) blockers."""
    active: list[BlockerRecord] = []
    if not hasattr(state, "blocker_registry") or not state.blocker_registry:
        return active

    for raw in state.blocker_registry.values():
        rec = BlockerRecord.from_dict(raw)
        if rec.status in (
            BlockerStatus.OPEN,
            BlockerStatus.ACKNOWLEDGED,
            BlockerStatus.RESOLUTION_REQUESTED,
            BlockerStatus.REOPENED,
        ):
            if work_order_id is None or rec.work_order_id == work_order_id:
                active.append(rec)
    return active


# ── 5. Canonical Wait-Condition Registry ────────────────────────────────

_WAIT_MUTEX = threading.Lock()


def create_wait(
    state: RunState,
    wait_type: WaitType,
    owner_subsystem: str,
    subject_entity: str,
    resolution_predicate: str,
    relevant_ref: str | None = None,
    expiry_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> WaitRecord:
    """Create an explicit WaitRecord in state.wait_registry. Idempotent for duplicate requests."""
    with _WAIT_MUTEX:
        if not hasattr(state, "wait_registry") or state.wait_registry is None:
            state.wait_registry = {}
        if not hasattr(state, "wait_history") or state.wait_history is None:
            state.wait_history = []

        type_str = wait_type.value if isinstance(wait_type, WaitType) else str(wait_type)

        # Idempotency check: if an active wait with identical parameters already exists, return it
        for raw in state.wait_registry.values():
            if (
                raw.get("status") == WaitStatus.ACTIVE.value
                and raw.get("wait_type") == type_str
                and str(raw.get("owner_subsystem")) == str(owner_subsystem)
                and str(raw.get("subject_entity")) == str(subject_entity)
                and str(raw.get("resolution_predicate")) == str(resolution_predicate)
            ):
                return WaitRecord.from_dict(raw)

        now = _now()
        seq = len(state.wait_history) + 1
        wait_id = f"wait-{state.run_id}-{type_str.lower()}-{seq:04d}"

        rec = WaitRecord(
            wait_id=wait_id,
            run_id=state.run_id,
            wait_type=wait_type,
            owner_subsystem=str(owner_subsystem),
            subject_entity=subject_entity,
            status=WaitStatus.ACTIVE,
            resolution_predicate=resolution_predicate,
            relevant_ref=relevant_ref,
            expiry_seconds=expiry_seconds,
            created_at=now,
            updated_at=now,
            version=1,
            metadata=dict(metadata or {}),
        )

        state.wait_registry[wait_id] = rec.to_dict()
        state.wait_history.append(rec.to_dict())

        record_lifecycle_event(
            state=state,
            event_type="WAIT_CREATED",
            entity_id=wait_id,
            actor=owner_subsystem,
            previous_state=None,
            new_state=WaitStatus.ACTIVE.value,
            reason_code=resolution_predicate,
        )
        return rec


def resolve_wait(
    state: RunState,
    wait_id: str,
    actor: str,
    resolving_event: str,
    evidence: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> tuple[bool, str]:
    """Resolve an active wait condition upon satisfaction of its predicate."""
    with _WAIT_MUTEX:
        if not hasattr(state, "wait_registry") or wait_id not in state.wait_registry:
            return False, f"WAIT_NOT_FOUND: Wait {wait_id} does not exist"

        raw = state.wait_registry[wait_id]
        rec = WaitRecord.from_dict(raw)
        if rec.status != WaitStatus.ACTIVE:
            return False, f"WAIT_NOT_ACTIVE: Wait {wait_id} is in status {rec.status.value}"

        if expected_version is not None and rec.version != expected_version:
            return False, f"STALE_VERSION_REJECTED: Wait {wait_id} version is {rec.version}, expected {expected_version}"

        now = _now()
        rec.status = WaitStatus.RESOLVED
        rec.last_evaluation_event = resolving_event
        rec.last_evaluation_result = "RESOLVED"
        rec.updated_at = now
        rec.version += 1
        if evidence:
            rec.metadata["resolution_evidence"] = dict(evidence)

        state.wait_registry[wait_id] = rec.to_dict()
        state.wait_history.append(rec.to_dict())

        record_lifecycle_event(
            state=state,
            event_type="WAIT_RESOLVED",
            entity_id=wait_id,
            actor=actor,
            previous_state=WaitStatus.ACTIVE.value,
            new_state=WaitStatus.RESOLVED.value,
            reason_code=resolving_event,
            evidence_refs=evidence,
        )
        return True, "WAIT_RESOLVED"


def invalidate_wait(
    state: RunState,
    wait_id: str,
    actor: str,
    reason: str,
) -> tuple[bool, str]:
    """Invalidate a wait condition when its underlying condition became impossible."""
    with _WAIT_MUTEX:
        if not hasattr(state, "wait_registry") or wait_id not in state.wait_registry:
            return False, f"WAIT_NOT_FOUND: Wait {wait_id} does not exist"

        raw = state.wait_registry[wait_id]
        rec = WaitRecord.from_dict(raw)
        if rec.status != WaitStatus.ACTIVE:
            return False, f"WAIT_NOT_ACTIVE: Wait {wait_id} is {rec.status.value}"

        now = _now()
        rec.status = WaitStatus.INVALID
        rec.last_evaluation_event = "invalidated"
        rec.last_evaluation_result = reason
        rec.updated_at = now
        rec.version += 1

        state.wait_registry[wait_id] = rec.to_dict()
        state.wait_history.append(rec.to_dict())

        record_lifecycle_event(
            state=state,
            event_type="WAIT_INVALIDATED",
            entity_id=wait_id,
            actor=actor,
            previous_state=WaitStatus.ACTIVE.value,
            new_state=WaitStatus.INVALID.value,
            reason_code=reason,
        )
        return True, "WAIT_INVALIDATED"


def escalate_wait(
    state: RunState,
    wait_id: str,
    actor: str,
    escalation_route: str,
) -> tuple[bool, str]:
    """Escalate an expired wait condition to an authorized handler."""
    with _WAIT_MUTEX:
        if not hasattr(state, "wait_registry") or wait_id not in state.wait_registry:
            return False, f"WAIT_NOT_FOUND: Wait {wait_id} does not exist"

        raw = state.wait_registry[wait_id]
        rec = WaitRecord.from_dict(raw)
        now = _now()
        rec.status = WaitStatus.ESCALATED
        rec.last_evaluation_event = "escalated"
        rec.last_evaluation_result = escalation_route
        rec.updated_at = now
        rec.version += 1

        state.wait_registry[wait_id] = rec.to_dict()
        state.wait_history.append(rec.to_dict())

        record_lifecycle_event(
            state=state,
            event_type="WAIT_ESCALATED",
            entity_id=wait_id,
            actor=actor,
            previous_state=WaitStatus.ACTIVE.value,
            new_state=WaitStatus.ESCALATED.value,
            reason_code=escalation_route,
        )
        return True, "WAIT_ESCALATED"


def get_active_waits(state: RunState, wait_type: WaitType | str | None = None) -> list[WaitRecord]:
    """Retrieve all currently active wait conditions, optionally filtered by wait_type."""
    active: list[WaitRecord] = []
    if not hasattr(state, "wait_registry") or not state.wait_registry:
        return active
    target_type = wait_type.value if isinstance(wait_type, WaitType) else (str(wait_type) if wait_type else None)
    for raw in state.wait_registry.values():
        rec = WaitRecord.from_dict(raw)
        if rec.status == WaitStatus.ACTIVE:
            if target_type is None or rec.wait_type.value == target_type or str(rec.wait_type) == target_type:
                active.append(rec)
    return active


# ── 6. State Machine & Invariant Evaluation (Invariants 1-12) ───────────


def _map_phase_to_controller_state(phase: Phase | str) -> ControllerState:
    """Map concrete supervisor phase to high-level controller state."""
    p = phase if isinstance(phase, Phase) else Phase(str(phase))
    if p in (
        Phase.INIT,
        Phase.PLANNING,
        Phase.AUTHORING,
        Phase.AUTHORING_READINESS_GATE,
        Phase.DISPATCHING,
        Phase.QA_REVIEW,
        Phase.INTEGRATION_REVIEW,
        Phase.FINAL_REVIEW,
        Phase.PRODUCT_READY,
        Phase.GITOPS,
    ):
        return ControllerState.RUNNABLE
    elif p in (Phase.EXECUTING, Phase.ARCHITECT_REPAIR):
        return ControllerState.EXECUTING
    elif p in (Phase.AWAITING_APPROVAL, Phase.WAITING_FOR_HUMAN):
        return ControllerState.WAITING
    elif p in (Phase.BLOCKED, Phase.ARCHITECT_RECOVERY_DECISION):
        return ControllerState.RECOVERABLE_BLOCKED
    elif p in (Phase.COMPLETE, Phase.FAILED):
        return ControllerState.TERMINAL
    return ControllerState.RUNNABLE


def evaluate_supervisor_state(
    state: RunState,
    requested_phase: Phase | None = None,
    manager: Any = None,
    ws: Path | None = None,
    current_operation_active: bool | None = None,
    authorized_override: bool = False,
    auth_context: AuthorizationContext | dict[str, Any] | None = None,
) -> SupervisorDecision:
    """Evaluate supervisor state invariants (Invariants 1-12) deterministically.

    Separates resume authorization from execution evidence:
    - Resume authorization permits scheduler advancement to RUNNABLE / DISPATCHING.
    - EXECUTING strictly requires authoritative live operation evidence.
    """
    target_phase = requested_phase if requested_phase is not None else state.phase
    current_ctrl = _map_phase_to_controller_state(state.phase)
    target_ctrl = _map_phase_to_controller_state(target_phase)

    violations: list[str] = []
    active_op_refs: list[str] = []
    active_blocker_refs: list[str] = []
    wait_condition_refs: list[str] = []

    # Parse authorization provenance
    parsed_auth: AuthorizationContext | None = None
    if auth_context is not None:
        if isinstance(auth_context, AuthorizationContext):
            parsed_auth = auth_context
        elif isinstance(auth_context, Mapping):
            parsed_auth = AuthorizationContext.from_dict(auth_context)

    # Gather active operation references from state attributes
    for op_attr in (
        "planning_operation_id",
        "authoring_operation_id",
        "architect_repair_operation_id",
        "integration_operation_id",
        "final_review_operation_id",
        "gitops_operation_id",
        "batch_operation_id",
        "dependency_escalation_op_id",
        "recovery_decision_operation_id",
        "delivery_status_operation_id",
    ):
        op_val = getattr(state, op_attr, None)
        if op_val:
            active_op_refs.append(f"{op_attr}:{op_val}")

    if hasattr(state, "qa_review_operations") and state.qa_review_operations:
        for qwo, qop in state.qa_review_operations.items():
            active_op_refs.append(f"qa_op:{qwo}:{qop}")

    # Gather live operations from manager
    live_manager_ops: list[dict[str, Any]] = []
    if manager is not None and hasattr(manager, "list_operations"):
        try:
            for op in manager.list_operations(state.session_id):
                st = str(op.get("status", "")).upper()
                if st not in _OPERATION_TERMINAL and st not in ("CANCELLED", "CANCEL_REQUESTED"):
                    op_id = op.get("operation_id")
                    if op_id:
                        live_manager_ops.append(op)
                        if f"mgr:{op_id}" not in active_op_refs:
                            active_op_refs.append(f"mgr:{op_id}")
        except Exception:
            pass

    # Gather active blockers
    active_blockers = get_active_blockers(state)
    for b in active_blockers:
        active_blocker_refs.append(b.blocker_id)
    for b_wo in getattr(state, "blocked_wo_ids", []):
        if f"legacy:{b_wo}" not in active_blocker_refs:
            active_blocker_refs.append(f"legacy:{b_wo}")

    # Gather active waits
    active_waits = get_active_waits(state)
    for w in active_waits:
        wait_condition_refs.append(w.wait_id)
    if hasattr(state, "dependency_waits") and state.dependency_waits:
        for dwo in state.dependency_waits.keys():
            if f"legacy_dep:{dwo}" not in wait_condition_refs:
                wait_condition_refs.append(f"legacy_dep:{dwo}")

    # Invariant 5: Terminal states are protected (CANCELLED/FAILED/COMPLETE cannot revive via ordinary scheduling)
    is_terminal = is_run_terminal(state, manager=manager)
    if is_terminal and target_phase not in (Phase.COMPLETE, Phase.FAILED):
        is_auth = (
            authorized_override
            or getattr(state, "_authorized_resume", False)
            or (parsed_auth is not None and parsed_auth.role in ("human", "admin", "architect"))
        )
        if not is_auth:
            violations.append(
                f"Invariant 5 violated: Terminal state ({state.phase.value}) cannot transition "
                f"to {target_phase.value} through ordinary scheduling without explicit authorized override"
            )

    # Invariant 1: EXECUTING requires real execution evidence
    # A transition to EXECUTING must require at least one authoritative, live operation.
    # authorized_override alone CANNOT satisfy EXECUTING.
    if target_phase == Phase.EXECUTING:
        has_real_op = False
        rejection_reason = None

        if manager is not None and hasattr(manager, "get_operation"):
            # Rigorous manager-backed verification
            verified_ops = []
            candidate_ids = []
            for ref in active_op_refs:
                parts = ref.split(":", 1)
                candidate_ids.append(parts[1] if len(parts) > 1 else parts[0])

            for op_id in candidate_ids:
                op_rec = manager.get_operation(op_id)
                if op_rec is None:
                    continue
                op_status = str(op_rec.get("status", "")).upper()
                if op_status in _OPERATION_TERMINAL or op_status in ("CANCELLED", "CANCEL_REQUESTED"):
                    continue
                # Verify run/session binding
                op_run = op_rec.get("run_id") or op_rec.get("session_id")
                if op_run and op_run not in (state.run_id, state.session_id):
                    continue
                # Verify work order binding where executing_wo_id is specified
                if getattr(state, "executing_wo_id", None):
                    op_wo = op_rec.get("metadata", {}).get("work_order_id") or op_rec.get("work_order_id")
                    if op_wo and op_wo != state.executing_wo_id:
                        continue
                verified_ops.append(op_id)

            if verified_ops:
                has_real_op = True
            else:
                rejection_reason = (
                    "Invariant 1 violated: EXECUTING state requires at least one authoritative, live operation. "
                    "All candidate operations in manager are missing, cancelled, terminal, or belong to another run/WO."
                )
        else:
            # Standalone unit test mode without manager:
            # Require explicit operation ID on state or current_operation_active=True
            # authorized_override alone, executing_wo_id alone, or worker_wo_ids alone are INSUFFICIENT
            has_op_attr = bool(
                getattr(state, "authoring_operation_id", None)
                or getattr(state, "planning_operation_id", None)
                or getattr(state, "architect_repair_operation_id", None)
                or getattr(state, "batch_operation_id", None)
                or (hasattr(state, "qa_review_operations") and state.qa_review_operations)
            )
            if has_op_attr or current_operation_active is True:
                has_real_op = True
            else:
                rejection_reason = (
                    "Invariant 1 violated: EXECUTING state requires at least one authoritative, live operation. "
                    "authorized_override=True, worker_wo_ids, or persisted metadata alone is insufficient evidence."
                )

        if not has_real_op:
            violations.append(
                rejection_reason
                or "Invariant 1 violated: EXECUTING state requires at least one active, authoritative operation"
            )

    # Invariant 2: Waiting requires a concrete condition
    if target_phase in (Phase.AWAITING_APPROVAL, Phase.WAITING_FOR_HUMAN) or target_ctrl == ControllerState.WAITING:
        has_wait_condition = (
            bool(wait_condition_refs)
            or target_phase == Phase.AWAITING_APPROVAL
            or (target_phase == Phase.WAITING_FOR_HUMAN and (bool(state.error) or bool(state.interrupted_phase)))
        )
        if not has_wait_condition:
            violations.append(
                "Invariant 2 violated: WAITING state requires at least one concrete, identifiable wait condition"
            )

    # Invariant 3: Blocked requires an active blocker
    if target_phase == Phase.BLOCKED or target_ctrl == ControllerState.RECOVERABLE_BLOCKED:
        has_blocker = (
            bool(active_blocker_refs)
            or bool(state.error)
            or bool(getattr(state, "blocked_wo_ids", None))
            or bool(getattr(state, "worker_blockers", None))
            or bool(getattr(state, "repair_context", None))
            or bool(getattr(state, "recovery_wo_id", None))
        )
        if not has_blocker:
            violations.append(
                "Invariant 3 violated: BLOCKED state requires at least one unresolved, actionable blocker"
            )

    # Invariant 4: Runnable cannot have blocking conditions
    if target_ctrl == ControllerState.RUNNABLE and requested_phase in (Phase.DISPATCHING, Phase.PLANNING):
        if target_phase == Phase.DISPATCHING:
            if state.worker_wo_ids and all(wo in state.blocked_wo_ids for wo in state.worker_wo_ids):
                violations.append(
                    "Invariant 4 violated: All work orders are blocked; cannot enter RUNNABLE/DISPATCHING without unblocking"
                )

    is_valid = len(violations) == 0
    decision_reason = "Transitions and invariants verified" if is_valid else "; ".join(violations)

    fingerprint = compute_progress_fingerprint(state)
    now = _now()
    seq = getattr(state, "lifecycle_event_seq", 0) + 1

    evidence_dict: dict[str, Any] = {}
    if parsed_auth:
        evidence_dict["authorization_provenance"] = parsed_auth.to_dict()

    decision = SupervisorDecision(
        current_state=current_ctrl,
        proposed_state=target_ctrl,
        decision_reason=decision_reason,
        required_preconditions=[],
        active_operation_refs=active_op_refs,
        active_blocker_refs=active_blocker_refs,
        wait_condition_refs=wait_condition_refs,
        recovery_eligibility=bool(state.recovery_attempts),
        relevant_wo_ids=list(state.worker_wo_ids),
        evidence_refs=evidence_dict,
        decision_timestamp=now,
        event_seq=seq,
        state_fingerprint=fingerprint,
        is_valid=is_valid,
        violations=violations,
    )
    return decision


def evaluate_work_order_eligibility(
    state: RunState,
    wo_id: str,
    ws: Path | None = None,
    manager: Any = None,
) -> EligibilityDecision:
    """Comprehensive per-work-order dispatch eligibility evaluation.

    Enforces that eligibility of one work order does NOT imply eligibility of another.
    Verifies existence, plan membership, non-terminal status, contracts, scope,
    dependencies, blockers, active operations, attempt budget, and approvals.
    """
    fingerprint = compute_progress_fingerprint(state)
    known_wos = set(getattr(state, "worker_wo_ids", []) or [])
    if hasattr(state, "wo_dependencies") and state.wo_dependencies:
        known_wos.update(state.wo_dependencies.keys())

    # 1. Existence check
    if wo_id not in known_wos and not (ws and (ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml").exists()):
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="UNKNOWN",
            decision_reason=f"Work order {wo_id} not found in plan or workspace",
            contract_status="ABSENT",
            dependency_status="UNKNOWN",
            completion_status="UNKNOWN",
            state_fingerprint=fingerprint,
        )

    # 2. Terminal checks
    if wo_id in getattr(state, "completed_wo_ids", []):
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="COMPLETED",
            decision_reason=f"Work order {wo_id} is already COMPLETED",
            contract_status="VALID",
            dependency_status="SATISFIED",
            completion_status="COMPLETED",
            state_fingerprint=fingerprint,
        )
    if wo_id in getattr(state, "failed_wo_ids", []):
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="FAILED",
            decision_reason=f"Work order {wo_id} is terminal FAILED",
            contract_status="UNKNOWN",
            dependency_status="FAILED",
            completion_status="FAILED",
            state_fingerprint=fingerprint,
        )

    # 3. Superseded / Archived check
    if wo_id in getattr(state, "superseded_wo_ids", []):
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="SUPERSEDED",
            decision_reason=f"Work order {wo_id} is SUPERSEDED by replacement work orders",
            contract_status="SUPERSEDED",
            dependency_status="IRRELEVANT",
            completion_status="SUPERSEDED",
            state_fingerprint=fingerprint,
        )

    # 4. Active Blockers check
    blocker_refs: list[str] = []
    if hasattr(state, "blocker_registry") and state.blocker_registry:
        for bid, bdata in state.blocker_registry.items():
            if bdata.get("work_order_id") == wo_id and bdata.get("status") in (
                BlockerStatus.OPEN.value,
                BlockerStatus.ACKNOWLEDGED.value,
                BlockerStatus.RESOLUTION_REQUESTED.value,
                BlockerStatus.REOPENED.value,
            ):
                blocker_refs.append(bid)
    if wo_id in getattr(state, "blocked_wo_ids", []):
        if f"legacy:{wo_id}" not in blocker_refs:
            blocker_refs.append(f"legacy:{wo_id}")

    if blocker_refs:
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="BLOCKED",
            decision_reason=f"Work order {wo_id} has unresolved active blockers: {blocker_refs}",
            contract_status="BLOCKED",
            dependency_status="BLOCKED",
            completion_status="BLOCKED",
            blocker_refs=blocker_refs,
            state_fingerprint=fingerprint,
        )

    # 5. Contract validity check
    if ws is not None:
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
        if not wo_file.exists() and not contract_file.exists():
            return EligibilityDecision(
                work_order_id=wo_id,
                is_eligible=False,
                status="MISSING_CONTRACT",
                decision_reason=f"Work order {wo_id} contract not found on disk",
                contract_status="MISSING",
                dependency_status="UNKNOWN",
                completion_status="UNKNOWN",
                state_fingerprint=fingerprint,
            )

    contract_revisions = getattr(state, "contract_revisions", {}) or {}
    contract_rev = contract_revisions.get(wo_id, 1)
    if contract_rev <= 0:
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="INVALID_CONTRACT",
            decision_reason=f"Work order {wo_id} has invalid/stale contract revision ({contract_rev})",
            contract_status="INVALID",
            dependency_status="UNKNOWN",
            completion_status="INVALID",
            state_fingerprint=fingerprint,
        )

    # 6. Approvals check
    wait_refs: list[str] = []
    approval_required = False
    if hasattr(state, "wait_registry") and state.wait_registry:
        for wid, wdata in state.wait_registry.items():
            if wdata.get("subject_entity") == wo_id and wdata.get("status") == WaitStatus.ACTIVE.value:
                wait_refs.append(wid)
                if wdata.get("wait_type") == WaitType.HUMAN_APPROVAL.value:
                    approval_required = True

    if approval_required:
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="AWAITING_APPROVAL",
            decision_reason=f"Work order {wo_id} requires pending approval",
            contract_status="AWAITING_APPROVAL",
            dependency_status="PENDING_APPROVAL",
            completion_status="PENDING",
            wait_refs=wait_refs,
            approval_required=True,
            state_fingerprint=fingerprint,
        )

    # 7. Dependency check
    wo_deps = getattr(state, "wo_dependencies", {}) or {}
    prereqs = wo_deps.get(wo_id, [])
    completed_wos = set(getattr(state, "completed_wo_ids", []) or [])
    failed_wos = set(getattr(state, "failed_wo_ids", []) or [])
    blocked_wos = set(getattr(state, "blocked_wo_ids", []) or [])
    dep_waits = getattr(state, "dependency_waits", {}) or {}

    if wo_id in dep_waits:
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="WAITING_FOR_DEPENDENCY",
            decision_reason=f"Work order {wo_id} is in an explicit dependency wait",
            contract_status="VALID",
            dependency_status="WAITING",
            completion_status="PENDING",
            wait_refs=[f"dep_wait:{wo_id}"],
            state_fingerprint=fingerprint,
        )

    unsatisfied_deps: list[str] = []
    for prereq in prereqs:
        if prereq in failed_wos or prereq in blocked_wos:
            return EligibilityDecision(
                work_order_id=wo_id,
                is_eligible=False,
                status="DEPENDENCY_FAILED",
                decision_reason=f"Prerequisite {prereq} for {wo_id} has failed or is blocked",
                contract_status="VALID",
                dependency_status="FAILED",
                completion_status="BLOCKED",
                state_fingerprint=fingerprint,
            )
        if prereq not in completed_wos:
            unsatisfied_deps.append(prereq)

    if unsatisfied_deps:
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="DEPENDENCIES_UNSATISFIED",
            decision_reason=f"Work order {wo_id} has unsatisfied prerequisites: {unsatisfied_deps}",
            contract_status="VALID",
            dependency_status="UNSATISFIED",
            completion_status="PENDING",
            wait_refs=[f"prereq:{d}" for d in unsatisfied_deps],
            state_fingerprint=fingerprint,
        )

    # 8. Attempt budget check
    if getattr(state, "budget_exhausted", False):
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="BUDGET_EXHAUSTED",
            decision_reason=f"Global turn budget is exhausted for work order {wo_id}",
            contract_status="VALID",
            dependency_status="SATISFIED",
            completion_status="BUDGET_EXHAUSTED",
            attempt_budget_remaining=0,
            state_fingerprint=fingerprint,
        )
    worker_retries = getattr(state, "worker_retries", {}) or {}
    max_retries = getattr(state, "max_worker_retries", 3)
    current_retries = worker_retries.get(wo_id, 0)
    budget_remaining = max(0, max_retries - current_retries)
    if budget_remaining <= 0:
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="BUDGET_EXHAUSTED",
            decision_reason=f"Work order {wo_id} has exhausted its retry budget ({current_retries}/{max_retries})",
            contract_status="VALID",
            dependency_status="SATISFIED",
            completion_status="BUDGET_EXHAUSTED",
            attempt_budget_remaining=0,
            state_fingerprint=fingerprint,
        )

    # 9. Active operation contention
    active_op_refs: list[str] = []
    if manager is not None and hasattr(manager, "list_operations"):
        try:
            for op in manager.list_operations(state.session_id):
                st = str(op.get("status", "")).upper()
                if st not in _OPERATION_TERMINAL and st not in ("CANCELLED", "CANCEL_REQUESTED"):
                    op_wo = op.get("metadata", {}).get("work_order_id") or op.get("work_order_id")
                    if op_wo == wo_id:
                        active_op_refs.append(str(op.get("operation_id")))
        except Exception:
            pass

    if active_op_refs:
        return EligibilityDecision(
            work_order_id=wo_id,
            is_eligible=False,
            status="ALREADY_EXECUTING",
            decision_reason=f"Work order {wo_id} already has active operations: {active_op_refs}",
            contract_status="VALID",
            dependency_status="SATISFIED",
            completion_status="RUNNING",
            active_op_refs=active_op_refs,
            state_fingerprint=fingerprint,
        )

    # Fully eligible!
    return EligibilityDecision(
        work_order_id=wo_id,
        is_eligible=True,
        status="RUNNABLE",
        decision_reason=f"Work order {wo_id} is eligible for dispatch",
        contract_status="VALID",
        dependency_status="SATISFIED",
        completion_status="PENDING",
        attempt_budget_remaining=budget_remaining,
        state_fingerprint=fingerprint,
    )


# ── 7. Deterministic Deadlock Detection (O(V + E) SCC / Cycles) ─────────


def _tarjan_scc(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components algorithm in O(V + E) time."""
    idx = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal idx
        indices[node] = idx
        lowlinks[node] = idx
        idx += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph.get(node, []):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc.append(w)
                if w == node:
                    break
            sccs.append(scc)

    for n in sorted(graph.keys()):
        if n not in indices:
            strongconnect(n)

    return sccs


def _reconstruct_cycle_path(
    scc_nodes: set[str] | list[str] | dict[str, list[str]],
    graph: dict[str, list[str]] | set[str] | list[str] = None,
) -> list[str]:
    """Reconstruct an actual directed cycle path in graph within the given SCC.

    Guarantees every consecutive pair (path[i], path[i+1]) is a true directed edge in graph,
    and path[0] == path[-1]. Supports either argument order: (scc_nodes, graph) or (graph, scc_nodes).
    """
    if isinstance(scc_nodes, dict) and isinstance(graph, (set, list, tuple)):
        scc_nodes, graph = graph, scc_nodes
    if graph is None:
        graph = {}
    scc_set = set(scc_nodes)
    if not scc_set:
        return []

    if len(scc_set) == 1:
        node = next(iter(scc_set))
        if node in graph.get(node, []):
            return [node, node]
        return [node]

    start = min(scc_set)
    # DFS to find a directed path from start back to start with length >= 2
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    while stack:
        curr, path = stack.pop()
        for neighbor in sorted(graph.get(curr, [])):
            if neighbor == start and len(path) > 1:
                return path + [start]
            if neighbor in scc_set and neighbor not in path:
                stack.append((neighbor, path + [neighbor]))

    # Fallback BFS if DFS missed simple path
    queue: deque[tuple[str, list[str]]] = deque([(start, [start])])
    while queue:
        curr, path = queue.popleft()
        for neighbor in sorted(graph.get(curr, [])):
            if neighbor == start and len(path) > 1:
                return path + [start]
            if neighbor in scc_set and neighbor not in path:
                queue.append((neighbor, path + [neighbor]))

    return list(sorted(scc_set)) + [min(scc_set)]


def _evaluate_all_deadlocks(
    state: RunState,
    manager: Any = None,
    ws: Path | None = None,
) -> list[DeadlockReport]:
    """Internal component-aware deadlock evaluator across independent graph components."""
    reports: list[DeadlockReport] = []

    # 1. Global Budget Deadlock
    if state.budget_exhausted or (state.global_turn_budget <= 0 and state.phase not in (Phase.COMPLETE, Phase.FAILED)):
        pending = [
            wo for wo in getattr(state, "worker_wo_ids", [])
            if wo not in getattr(state, "completed_wo_ids", []) and wo not in getattr(state, "superseded_wo_ids", [])
        ]
        if pending or state.budget_exhausted:
            reports.append(
                DeadlockReport(
                    deadlock_id=f"dlk-{state.run_id}-budget-exhausted",
                    run_id=state.run_id,
                    deadlock_type=DeadlockType.BUDGET_DEADLOCK,
                    severity="FATAL",
                    affected_work_orders=pending,
                    cycle_path=[],
                    root_conditions=["Global turn budget exhausted with remaining pending work orders"],
                    unavailable_resolution_paths=["No automatic turns permitted"],
                    suggested_authorized_actions=["Human operator must provide additional budget via :resume"],
                    detected_at=_now(),
                )
            )

    # 2. Check for Blocker Deadlock / Recovery Deadlock
    pending_wos = [
        wo for wo in getattr(state, "worker_wo_ids", [])
        if wo not in getattr(state, "completed_wo_ids", []) and wo not in getattr(state, "superseded_wo_ids", [])
    ]
    if pending_wos and all(wo in getattr(state, "blocked_wo_ids", []) for wo in pending_wos):
        rec_attempts = getattr(state, "recovery_attempts", {}).get(getattr(state, "recovery_wo_id", "") or "WO-001", 0)
        if not rec_attempts and getattr(state, "recovery_attempts", {}):
            rec_attempts = max(getattr(state, "recovery_attempts", {}).values())
        if rec_attempts >= getattr(state, "max_retries", 10) or getattr(state, "exit_code", None) == 3:
            reports.append(
                DeadlockReport(
                    deadlock_id=f"dlk-{state.run_id}-blocker-deadlock",
                    run_id=state.run_id,
                    deadlock_type=DeadlockType.BLOCKER_DEADLOCK,
                    severity="CRITICAL",
                    affected_work_orders=pending_wos,
                    cycle_path=[],
                    root_conditions=[f"All pending work orders {pending_wos} are blocked and recovery exhausted"],
                    unavailable_resolution_paths=["Autonomous worker and recovery retry budgets exhausted"],
                    suggested_authorized_actions=["Human intervention required to amend contracts or clear blockers"],
                    detected_at=_now(),
                )
            )

    # 3. Check for Recovery Deadlock
    if getattr(state, "phase", None) in (Phase.ARCHITECT_RECOVERY_DECISION, Phase.BLOCKED):
        rec_attempts = getattr(state, "recovery_attempts", {}).get(getattr(state, "recovery_wo_id", "") or "WO-001", 0)
        if not rec_attempts and getattr(state, "recovery_attempts", {}):
            rec_attempts = max(getattr(state, "recovery_attempts", {}).values())
        if rec_attempts >= getattr(state, "max_retries", 10) and not any(r.deadlock_type == DeadlockType.BLOCKER_DEADLOCK for r in reports):
            reports.append(
                DeadlockReport(
                    deadlock_id=f"dlk-{state.run_id}-recovery-deadlock",
                    run_id=state.run_id,
                    deadlock_type=DeadlockType.RECOVERY_DEADLOCK,
                    severity="CRITICAL",
                    affected_work_orders=list(getattr(state, "blocked_wo_ids", [])),
                    cycle_path=[],
                    root_conditions=[f"Recovery attempts exhausted ({rec_attempts}/{getattr(state, 'max_retries', 10)})"],
                    unavailable_resolution_paths=["No recovery routes remaining"],
                    suggested_authorized_actions=["Human intervention required"],
                    detected_at=_now(),
                )
            )

    # 4. Build directed wait-for graph: A -> B means A is waiting on B
    graph: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()

    for wo in getattr(state, "worker_wo_ids", []):
        nodes.add(wo)

    # Work-order dependencies
    dependencies = getattr(state, "wo_dependencies", {}) or {}
    for wo_id, deps in dependencies.items():
        nodes.add(wo_id)
        for dep in deps:
            nodes.add(dep)
            if dep not in graph[wo_id]:
                graph[wo_id].append(dep)

    # Dependency waits
    dep_waits = getattr(state, "dependency_waits", {}) or {}
    for wo_id, dinfo in dep_waits.items():
        prereq = dinfo.get("prerequisite") or dinfo.get("prereq")
        if prereq:
            nodes.add(wo_id)
            nodes.add(str(prereq))
            if str(prereq) not in graph[wo_id]:
                graph[wo_id].append(str(prereq))

    # Wait registry edges
    wait_reg = getattr(state, "wait_registry", {}) or {}
    for wid, wdata in wait_reg.items():
        if wdata.get("status") == WaitStatus.ACTIVE.value:
            subj = wdata.get("subject_entity") or wid
            ref = wdata.get("relevant_ref")
            nodes.add(wid)
            if subj:
                nodes.add(subj)
                if wid not in graph[subj]:
                    graph[subj].append(wid)
            if ref:
                nodes.add(ref)
                if ref not in graph[wid]:
                    graph[wid].append(ref)

    # Blocker registry edges
    blocker_reg = getattr(state, "blocker_registry", {}) or {}
    for bid, bdata in blocker_reg.items():
        if bdata.get("status") in (
            BlockerStatus.OPEN.value,
            BlockerStatus.ACKNOWLEDGED.value,
            BlockerStatus.RESOLUTION_REQUESTED.value,
            BlockerStatus.REOPENED.value,
        ):
            b_wo = bdata.get("work_order_id")
            nodes.add(bid)
            if b_wo:
                nodes.add(b_wo)
                if bid not in graph[b_wo]:
                    graph[b_wo].append(bid)
            for dref in bdata.get("dependency_refs", []):
                nodes.add(dref)
                if dref not in graph[bid]:
                    graph[bid].append(dref)

    # Active operations in manager
    active_running_ops: dict[str, dict[str, Any]] = {}
    if manager is not None and hasattr(manager, "list_operations"):
        try:
            for op in manager.list_operations(state.session_id):
                st = str(op.get("status", "")).upper()
                if st not in _OPERATION_TERMINAL and st not in ("CANCELLED", "CANCEL_REQUESTED"):
                    op_id = str(op.get("operation_id", ""))
                    if op_id:
                        active_running_ops[op_id] = op
        except Exception:
            pass

    # Partition graph into connected components (undirected reachability)
    undirected: dict[str, set[str]] = defaultdict(set)
    for u in nodes:
        undirected[u]
        for v in graph.get(u, []):
            undirected[u].add(v)
            undirected[v].add(u)

    visited_nodes: set[str] = set()
    components: list[set[str]] = []
    for node in sorted(nodes):
        if node not in visited_nodes:
            comp: set[str] = set()
            queue = [node]
            visited_nodes.add(node)
            while queue:
                curr = queue.pop()
                comp.add(curr)
                for neighbor in undirected.get(curr, set()):
                    if neighbor not in visited_nodes:
                        visited_nodes.add(neighbor)
                        queue.append(neighbor)
            components.append(comp)

    # Evaluate each component independently
    for comp in components:
        comp_graph = {u: [v for v in graph.get(u, []) if v in comp] for u in comp}

        # Check if an upstream node in this component is fully runnable/eligible (provides forward progress)
        comp_wos = [n for n in comp if n in getattr(state, "worker_wo_ids", [])]
        has_eligible_upstream_wo = False
        for cwo in comp_wos:
            deps = comp_graph.get(cwo, [])
            if not deps and cwo not in getattr(state, "blocked_wo_ids", []):
                has_eligible_upstream_wo = True
                break

        # Check if active operation running in manager belongs to this component
        has_comp_active_op = False
        for op_id, op_data in active_running_ops.items():
            op_wo = op_data.get("metadata", {}).get("work_order_id") or op_data.get("work_order_id")
            if op_wo in comp or op_id in comp:
                has_comp_active_op = True
                break

        # If component has a live provider operation actively running, normal waits in this component are progressing
        comp_sccs = _tarjan_scc(comp_graph)

        # 1. Cycles in component
        for scc in comp_sccs:
            is_cycle = len(scc) > 1 or (len(scc) == 1 and scc[0] in comp_graph.get(scc[0], []))
            if not is_cycle:
                continue

            cycle_nodes = set(scc)
            cycle_path = _reconstruct_cycle_path(cycle_nodes, comp_graph)

            has_appr = any("appr" in n.lower() or (n in wait_reg and wait_reg[n].get("wait_type") == WaitType.HUMAN_APPROVAL.value) for n in cycle_nodes)
            has_blk = any(n.startswith("blk-") or (n in blocker_reg) for n in cycle_nodes)
            has_rec = any("rec" in n.lower() for n in cycle_nodes)
            has_op = any("op" in n.lower() for n in cycle_nodes)

            if has_appr:
                dlk_type = DeadlockType.APPROVAL_DEADLOCK
            elif has_blk:
                dlk_type = DeadlockType.BLOCKER_DEADLOCK
            elif has_rec:
                dlk_type = DeadlockType.RECOVERY_DEADLOCK
            elif has_op:
                dlk_type = DeadlockType.OPERATION_DEADLOCK
            else:
                dlk_type = DeadlockType.DEPENDENCY_CYCLE

            wo_members = [n for n in cycle_path if not n.startswith("blk-") and not n.startswith("wait-") and not n.startswith("evt-")]
            affected_wos = sorted(list(set(wo_members))) or sorted(list(cycle_nodes))

            d_id = f"dlk-{state.run_id}-{dlk_type.value.lower()}-{hashlib.sha256(''.join(cycle_path).encode()).hexdigest()[:8]}"
            reports.append(
                DeadlockReport(
                    deadlock_id=d_id,
                    run_id=state.run_id,
                    deadlock_type=dlk_type,
                    severity="CRITICAL",
                    affected_work_orders=affected_wos,
                    affected_blockers=[n for n in cycle_nodes if n in blocker_reg],
                    affected_waits=[n for n in cycle_nodes if n in wait_reg],
                    cycle_path=cycle_path,
                    root_conditions=[f"{dlk_type.value} cycle detected: {' -> '.join(cycle_path)}"],
                    unavailable_resolution_paths=["Automated resolution impossible without architecture intervention"],
                    suggested_authorized_actions=["Request Architect re-wiring or contract amendment"],
                    detected_at=_now(),
                    graph_fingerprint=hashlib.sha256(json.dumps(dict(comp_graph), sort_keys=True).encode()).hexdigest(),
                )
            )

        # If this component is resolved by active operation or has eligible upstream WO, skip orphaned wait checks for active ops
        if has_comp_active_op:
            continue

        # 2. Orphaned waits in component
        for wid in sorted(comp):
            if wid in wait_reg:
                wdata = wait_reg[wid]
                if wdata.get("status") == WaitStatus.ACTIVE.value:
                    wtype = wdata.get("wait_type")
                    ref = wdata.get("relevant_ref")
                    if wtype == WaitType.OPERATION.value and ref:
                        if manager is not None and hasattr(manager, "get_operation"):
                            op = manager.get_operation(ref)
                            if op is None or str(op.get("status", "")).upper() in _OPERATION_TERMINAL:
                                d_id = f"dlk-{state.run_id}-orphaned-op-{wid}"
                                reports.append(
                                    DeadlockReport(
                                        deadlock_id=d_id,
                                        run_id=state.run_id,
                                        deadlock_type=DeadlockType.ORPHANED_WAIT,
                                        severity="HIGH",
                                        affected_waits=[wid],
                                        affected_operations=[ref],
                                        cycle_path=[],
                                        root_conditions=[f"Wait {wid} awaits missing or terminal operation {ref}"],
                                        unavailable_resolution_paths=["Operation no longer exists in manager"],
                                        suggested_authorized_actions=["Reconcile restart state and invalidate orphaned wait"],
                                        detected_at=_now(),
                                    )
                                )

        # 3. Missing approval authority in component
        for wid in sorted(comp):
            if wid in wait_reg:
                wdata = wait_reg[wid]
                if wdata.get("status") == WaitStatus.ACTIVE.value and wdata.get("wait_type") == WaitType.HUMAN_APPROVAL.value:
                    owner_sub = str(wdata.get("owner_subsystem", "")).lower()
                    if owner_sub in ("nonexistent", "missing", "unknown", "none"):
                        d_id = f"dlk-{state.run_id}-approval-authority-{wid}"
                        reports.append(
                            DeadlockReport(
                                deadlock_id=d_id,
                                run_id=state.run_id,
                                deadlock_type=DeadlockType.APPROVAL_DEADLOCK,
                                severity="CRITICAL",
                                affected_waits=[wid],
                                cycle_path=[],
                                root_conditions=[f"Approval wait {wid} references nonexistent authority '{owner_sub}'"],
                                unavailable_resolution_paths=["No authorized entity can issue approval"],
                                suggested_authorized_actions=["Escalate to human operator for re-assignment"],
                                detected_at=_now(),
                            )
                        )

    return reports


def detect_deadlocks(
    state: RunState,
    manager: Any = None,
    ws: Path | None = None,
    return_all: bool = False,
) -> DeadlockReport | None:
    """Evaluate supervisor lifecycle dependency graph for true deadlocks.

    Evaluates independent graph components separately so that an active operation
    or legitimate human approval wait in one component does NOT suppress unrelated
    deadlocks in another component.
    """
    reports = _evaluate_all_deadlocks(state, manager=manager, ws=ws)
    if not reports:
        return None
    primary = reports[0]
    primary.sub_reports = reports[1:]
    return reports if return_all else primary


def detect_all_deadlocks(
    state: RunState,
    manager: Any = None,
    ws: Path | None = None,
) -> list[DeadlockReport]:
    """Evaluate and return all independent deadlocks detected across graph components."""
    return _evaluate_all_deadlocks(state, manager=manager, ws=ws)


# ── 8. Progress Detection & Fingerprinting ──────────────────────────────


def compute_progress_fingerprint(state: RunState) -> str:
    """Create a deterministic state fingerprint based only on authoritative lifecycle state.

    Explicitly excludes volatile timestamps (updated_at, created_at, turn_started_at,
    turn_deadline, decision_timestamp, applied_at), ephemeral logging strings,
    and transient tick/poll counters.
    """
    blocker_tuples = []
    if hasattr(state, "blocker_registry") and state.blocker_registry:
        for bid, bdata in sorted(state.blocker_registry.items()):
            blocker_tuples.append((
                bid,
                int(bdata.get("version", 1)),
                str(bdata.get("status")),
                str(bdata.get("owner_subsystem", "")),
                str(bdata.get("blocker_type", "")),
            ))

    wait_tuples = []
    if hasattr(state, "wait_registry") and state.wait_registry:
        for wid, wdata in sorted(state.wait_registry.items()):
            wait_tuples.append((
                wid,
                int(wdata.get("version", 1)),
                str(wdata.get("status")),
                str(wdata.get("wait_type", "")),
                str(wdata.get("owner_subsystem", "")),
            ))

    active_ops = []
    for op_attr in (
        "planning_operation_id",
        "authoring_operation_id",
        "architect_repair_operation_id",
        "integration_operation_id",
        "final_review_operation_id",
        "gitops_operation_id",
        "batch_operation_id",
        "dependency_escalation_op_id",
        "recovery_decision_operation_id",
        "delivery_status_operation_id",
    ):
        v = getattr(state, op_attr, None)
        if v:
            active_ops.append((op_attr, str(v)))

    qa_verdicts = []
    if hasattr(state, "qa_verdicts") and state.qa_verdicts:
        for qwo, qv in sorted(state.qa_verdicts.items()):
            qa_verdicts.append((qwo, str(qv)))

    wo_deps = []
    if hasattr(state, "wo_dependencies") and state.wo_dependencies:
        for w, deps in sorted(state.wo_dependencies.items()):
            wo_deps.append((w, sorted(list(deps))))

    dep_waits = []
    if hasattr(state, "dependency_waits") and state.dependency_waits:
        for w, dinfo in sorted(state.dependency_waits.items()):
            prereq = dinfo.get("prerequisite") or dinfo.get("prereq") or ""
            dep_waits.append((w, str(prereq)))

    data = {
        "phase": state.phase.value,
        "status": getattr(state, "status", ""),
        "completed_wo_ids": sorted(list(getattr(state, "completed_wo_ids", []))),
        "failed_wo_ids": sorted(list(getattr(state, "failed_wo_ids", []))),
        "published_wo_ids": sorted(list(getattr(state, "published_wo_ids", []))),
        "superseded_wo_ids": sorted(list(getattr(state, "superseded_wo_ids", []))),
        "blocked_wo_ids": sorted(list(getattr(state, "blocked_wo_ids", []))),
        "worker_wo_ids": sorted(list(getattr(state, "worker_wo_ids", []))),
        "plan_id": state.plan_id,
        "authoring_revision": state.authoring_revision,
        "blockers": blocker_tuples,
        "waits": wait_tuples,
        "attempt_ledger_seq": getattr(state, "attempt_ledger_seq", 0),
        "budget_usage": sorted(
            [(k, sorted(list(v.items()))) for k, v in getattr(state, "budget_usage", {}).items()]
        ),
        "global_turn_budget": getattr(state, "global_turn_budget", 0),
        "budget_exhausted": bool(getattr(state, "budget_exhausted", False)),
        "contract_revisions": sorted(list(getattr(state, "contract_revisions", {}).items())),
        "active_operations": sorted(active_ops),
        "qa_verdicts": qa_verdicts,
        "wo_dependencies": wo_deps,
        "dependency_waits": dep_waits,
        "exit_code": getattr(state, "exit_code", None),
        "reconciliation_count": len(getattr(state, "reconciliation_history", []) or []),
    }
    raw = json.dumps(data, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_progress(
    state: RunState,
    operation_started: bool = False,
    max_no_progress_ticks: int | None = None,
) -> tuple[bool, str]:
    """Inspect state progress against previous observation to protect against infinite loops."""
    if operation_started:
        state.consecutive_no_progress_ticks = 0
        state.last_progress_fingerprint = compute_progress_fingerprint(state)
        return True, "PROGRESS_MADE"

    threshold = max_no_progress_ticks or getattr(state, "max_stall_ticks", 5) or 5

    # Check if there is an active valid wait condition
    active_waits = get_active_waits(state)
    if active_waits:
        return False, f"WAITING: Active wait conditions present ({len(active_waits)} active)"

    current_fp = compute_progress_fingerprint(state)
    last_fp = getattr(state, "last_progress_fingerprint", None)

    if last_fp is not None and last_fp == current_fp:
        state.consecutive_no_progress_ticks = getattr(state, "consecutive_no_progress_ticks", 0) + 1
    else:
        state.last_progress_fingerprint = current_fp
        state.consecutive_no_progress_ticks = getattr(state, "consecutive_no_progress_ticks", 0) + 1

    if state.consecutive_no_progress_ticks >= threshold:
        record_lifecycle_event(
            state=state,
            event_type="PROGRESS_LOOP_HALTED",
            entity_id=state.run_id,
            actor="supervisor",
            previous_state=state.phase.value,
            new_state=Phase.BLOCKED.value,
            reason_code=f"NO_PROGRESS_THRESHOLD_EXCEEDED ({state.consecutive_no_progress_ticks} ticks)",
            evidence_refs={"fingerprint": current_fp, "ticks": state.consecutive_no_progress_ticks},
        )
        return False, f"NO_PROGRESS_THRESHOLD_EXCEEDED: {state.consecutive_no_progress_ticks} ticks without progress"

    return False, f"NO_PROGRESS_TICK ({state.consecutive_no_progress_ticks}/{threshold})"


# ── 9. Restart Reconciliation Protocol (Stages 1 and 2) ─────────────────


def reconcile_supervisor_state_readonly(
    state: RunState,
    manager: Any = None,
    ws: Path | None = None,
) -> ReconciliationReport:
    """Stage 1: Read-only reconciliation analysis. Identifies discrepancies without mutating."""
    now = _now()
    report_id = f"rec-stage1-{state.run_id}-{int(time.time())}"
    items: list[ReconciliationItem] = []

    # 1. Operation reconciliation
    checked_ops: set[str] = set()
    for op_attr in ("planning_operation_id", "authoring_operation_id", "batch_operation_id"):
        op_id = getattr(state, op_attr, None)
        if op_id:
            checked_ops.add(op_id)
            if manager is not None and hasattr(manager, "get_operation"):
                op = manager.get_operation(op_id)
                if op is None:
                    items.append(
                        ReconciliationItem(
                            entity_type="operation",
                            entity_id=op_id,
                            outcome=ReconciliationOutcome.MISSING,
                            persisted_value="ACTIVE",
                            authoritative_value="ABSENT",
                            discrepancy_reason=f"Persisted {op_attr}={op_id} is absent from operation manager",
                            suggested_correction=f"Clear {op_attr}",
                        )
                    )
                else:
                    st = str(op.get("status", "")).upper()
                    if st in _OPERATION_TERMINAL or st in ("CANCELLED", "CANCEL_REQUESTED"):
                        items.append(
                            ReconciliationItem(
                                entity_type="operation",
                                entity_id=op_id,
                                outcome=ReconciliationOutcome.STALE,
                                persisted_value="ACTIVE",
                                authoritative_value=st,
                                discrepancy_reason=f"Operation {op_id} is terminal ({st}) in manager",
                                suggested_correction=f"Close {op_attr} to terminal",
                            )
                        )

    for op_id in (getattr(state, "active_operations", []) or []):
        if op_id and op_id not in checked_ops:
            checked_ops.add(op_id)
            if manager is not None and hasattr(manager, "get_operation"):
                op = manager.get_operation(op_id)
                if op is None:
                    items.append(
                        ReconciliationItem(
                            entity_type="operation",
                            entity_id=op_id,
                            outcome=ReconciliationOutcome.MISSING,
                            persisted_value="ACTIVE",
                            authoritative_value="ABSENT",
                            discrepancy_reason=f"Active operation {op_id} is absent from operation manager",
                            suggested_correction="Remove from active_operations",
                        )
                    )
                else:
                    st = str(op.get("status", "")).upper()
                    if st in _OPERATION_TERMINAL or st in ("CANCELLED", "CANCEL_REQUESTED"):
                        items.append(
                            ReconciliationItem(
                                entity_type="operation",
                                entity_id=op_id,
                                outcome=ReconciliationOutcome.STALE,
                                persisted_value="ACTIVE",
                                authoritative_value=st,
                                discrepancy_reason=f"Active operation {op_id} is terminal ({st}) in manager",
                                suggested_correction="Remove from active_operations",
                            )
                        )

    # 2. Blocker reconciliation
    # Consider the COMPLETE event sequence (LATEST event for this blocker)
    if hasattr(state, "blocker_registry") and state.blocker_registry:
        for bid, bdata in state.blocker_registry.items():
            current_status = bdata.get("status")
            current_ver = bdata.get("version", 1)

            # Find the LATEST historical event for this blocker across blocker_history and lifecycle_events
            history_events = [
                h for h in getattr(state, "blocker_history", [])
                if h.get("blocker_id") == bid
            ]
            lifecycle_matches = [
                e for e in getattr(state, "lifecycle_events", [])
                if e.get("entity_id") == bid
            ]
            reopened = any(
                e.get("event_type") == "BLOCKER_REOPENED"
                for e in lifecycle_matches
            )
            if reopened:
                continue

            if history_events:
                latest_event = history_events[-1]
                latest_status = latest_event.get("status")
                if current_status == BlockerStatus.OPEN.value and latest_status == BlockerStatus.RESOLVED.value:
                    items.append(
                        ReconciliationItem(
                            entity_type="blocker",
                            entity_id=bid,
                            outcome=ReconciliationOutcome.STALE,
                            persisted_value=BlockerStatus.OPEN.value,
                            authoritative_value=BlockerStatus.RESOLVED.value,
                            discrepancy_reason="Blocker was resolved in historical ledger but marked OPEN",
                            suggested_correction="Update status to RESOLVED",
                            expected_version=int(current_ver),
                        )
                    )
                elif current_status == BlockerStatus.OPEN.value and latest_status == BlockerStatus.SUPERSEDED.value:
                    items.append(
                        ReconciliationItem(
                            entity_type="blocker",
                            entity_id=bid,
                            outcome=ReconciliationOutcome.STALE,
                            persisted_value=BlockerStatus.OPEN.value,
                            authoritative_value=BlockerStatus.SUPERSEDED.value,
                            discrepancy_reason="Blocker was superseded in historical ledger but marked OPEN",
                            suggested_correction="Update status to SUPERSEDED",
                            expected_version=int(current_ver),
                        )
                    )

    # 3. Wait reconciliation
    if hasattr(state, "wait_registry") and state.wait_registry:
        for wid, wdata in state.wait_registry.items():
            if wdata.get("status") == WaitStatus.ACTIVE.value:
                wtype = wdata.get("wait_type")
                ref = wdata.get("relevant_ref")
                current_ver = wdata.get("version", 1)
                if wtype == WaitType.OPERATION.value and ref:
                    if manager is not None and hasattr(manager, "get_operation"):
                        op = manager.get_operation(ref)
                        if op is None or str(op.get("status", "")).upper() in _OPERATION_TERMINAL:
                            items.append(
                                ReconciliationItem(
                                    entity_type="wait",
                                    entity_id=wid,
                                    outcome=ReconciliationOutcome.MISSING,
                                    persisted_value=WaitStatus.ACTIVE.value,
                                    authoritative_value="OPERATION_MISSING_OR_TERMINAL",
                                    discrepancy_reason=f"Wait {wid} awaits missing/terminal operation {ref}",
                                    suggested_correction="Invalidate wait",
                                    expected_version=int(current_ver),
                                )
                            )

    status = "CLEAN" if len(items) == 0 else "CORRECTIONS_NEEDED"
    state_fp = compute_progress_fingerprint(state)
    event_seq = getattr(state, "lifecycle_event_seq", 0)

    snapshot = ReconciliationSnapshot(
        snapshot_id=f"snap-{report_id}",
        run_id=state.run_id,
        state_fingerprint=state_fp,
        run_state_version=event_seq,
        operation_graph_fingerprint="",
        blocker_registry_fingerprint=hashlib.sha256(json.dumps(getattr(state, "blocker_registry", {}), sort_keys=True).encode()).hexdigest(),
        wait_registry_fingerprint=hashlib.sha256(json.dumps(getattr(state, "wait_registry", {}), sort_keys=True).encode()).hexdigest(),
        dependency_graph_fingerprint=getattr(state, "dependency_graph_fingerprint", "") or "",
        attempt_ledger_fingerprint=str(getattr(state, "attempt_ledger_seq", 0)),
        event_sequence=event_seq,
        created_at=now,
        schema_version="2.0",
    )

    report = ReconciliationReport(
        reconciliation_id=report_id,
        run_id=state.run_id,
        stage="STAGE_1_READ_ONLY",
        status=status,
        items=items,
        created_at=now,
        fingerprint=hashlib.sha256(json.dumps([i.to_dict() for i in items], sort_keys=True).encode()).hexdigest(),
        state_fingerprint=state_fp,
        schema_version="2.0",
        run_state_version=event_seq,
        event_sequence=event_seq,
        snapshot=snapshot,
    )
    return report


def apply_authorized_reconciliation(
    state: RunState,
    report: ReconciliationReport,
    manager: Any = None,
    ws: Path | None = None,
    authorize_ambiguous: bool = False,
    auth_context: AuthorizationContext | dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Stage 2: Apply deterministic, permitted corrections identified in Stage 1. Idempotent and version-bound."""
    # 1. Run ID verification
    if report.run_id != state.run_id:
        return False, [f"RUN_ID_MISMATCH: Report is for run {report.run_id}, current run is {state.run_id}"]

    # 2. Schema version check
    if getattr(report, "schema_version", "1.0") not in ("1.0", "2.0"):
        return False, ["MALFORMED_REPORT: Unsupported schema version"]

    # 3. Time-of-check / Time-of-use fingerprint verification
    current_fp = compute_progress_fingerprint(state)
    expected_fp = getattr(report, "state_fingerprint", "")
    if report.snapshot:
        expected_fp = report.snapshot.state_fingerprint

    if expected_fp and expected_fp != current_fp:
        return False, [
            "STALE_RECONCILIATION_REPORT: Authoritative state fingerprint changed from Stage 1 analysis. "
            "A fresh Stage 1 inspection is required."
        ]

    # 4. Expected version check on each affected entity
    for item in report.items:
        if item.entity_type == "blocker":
            bdata = getattr(state, "blocker_registry", {}).get(item.entity_id)
            if bdata and item.expected_version is not None:
                if int(bdata.get("version", 1)) != item.expected_version:
                    return False, [
                        f"CONCURRENT_MODIFICATION_REJECTED: Blocker {item.entity_id} version is "
                        f"{bdata.get('version')}, expected {item.expected_version}"
                    ]
        elif item.entity_type == "wait":
            wdata = getattr(state, "wait_registry", {}).get(item.entity_id)
            if wdata and item.expected_version is not None:
                if int(wdata.get("version", 1)) != item.expected_version:
                    return False, [
                        f"CONCURRENT_MODIFICATION_REJECTED: Wait {item.entity_id} version is "
                        f"{wdata.get('version')}, expected {item.expected_version}"
                    ]

    # Apply corrections atomically
    applied: list[str] = []
    for item in report.items:
        if item.requires_authorization and not authorize_ambiguous:
            continue

        if item.entity_type == "operation":
            if item.outcome == ReconciliationOutcome.MISSING:
                for op_attr in ("planning_operation_id", "authoring_operation_id", "batch_operation_id"):
                    if getattr(state, op_attr, None) == item.entity_id:
                        setattr(state, op_attr, None)
                        applied.append(f"Cleared {op_attr}={item.entity_id}")
                if hasattr(state, "active_operations") and state.active_operations:
                    state.active_operations = [op for op in state.active_operations if op != item.entity_id]
                    applied.append(f"Removed {item.entity_id} from active_operations")
            elif item.outcome == ReconciliationOutcome.STALE:
                for op_attr in ("planning_operation_id", "authoring_operation_id", "batch_operation_id"):
                    if getattr(state, op_attr, None) == item.entity_id:
                        setattr(state, op_attr, None)
                        applied.append(f"Closed stale {op_attr}={item.entity_id}")
                if hasattr(state, "active_operations") and state.active_operations:
                    state.active_operations = [op for op in state.active_operations if op != item.entity_id]
                    applied.append(f"Removed {item.entity_id} from active_operations")

        elif item.entity_type == "blocker":
            if item.outcome == ReconciliationOutcome.STALE and item.entity_id in state.blocker_registry:
                target_status = item.authoritative_value or BlockerStatus.RESOLVED.value
                state.blocker_registry[item.entity_id]["status"] = target_status
                state.blocker_registry[item.entity_id]["version"] = state.blocker_registry[item.entity_id].get("version", 1) + 1
                state.blocker_registry[item.entity_id]["updated_at"] = _now()
                applied.append(f"Updated blocker {item.entity_id} to {target_status}")

        elif item.entity_type == "wait":
            if item.outcome == ReconciliationOutcome.MISSING and item.entity_id in state.wait_registry:
                state.wait_registry[item.entity_id]["status"] = WaitStatus.INVALID.value
                state.wait_registry[item.entity_id]["version"] = state.wait_registry[item.entity_id].get("version", 1) + 1
                state.wait_registry[item.entity_id]["updated_at"] = _now()
                applied.append(f"Invalidated orphaned wait {item.entity_id}")

    if not hasattr(state, "reconciliation_history") or state.reconciliation_history is None:
        state.reconciliation_history = []

    state.reconciliation_count = getattr(state, "reconciliation_count", 0) + 1

    state.reconciliation_history.append({
        "reconciliation_id": report.reconciliation_id,
        "state_fingerprint": report.state_fingerprint,
        "snapshot_fingerprint": getattr(report, "snapshot_fingerprint", report.state_fingerprint),
        "applied_corrections": list(applied),
        "applied_at": _now(),
    })

    record_lifecycle_event(
        state=state,
        event_type="RECONCILIATION_COMPLETED",
        entity_id=report.reconciliation_id,
        actor="supervisor",
        previous_state=report.status,
        new_state="CLEAN" if len(applied) == len(report.items) else "PARTIALLY_RECONCILED",
        reason_code="AUTHORIZED_STAGE_2",
    )
    return True, applied


# ── 10. Legacy Blocker & Wait Migration ─────────────────────────────────


def migrate_legacy_blockers_and_waits(state: RunState) -> None:
    """Migrate legacy state attributes into canonical blocker and wait registries."""
    if not hasattr(state, "blocker_registry") or state.blocker_registry is None:
        state.blocker_registry = {}
    if not hasattr(state, "blocker_history") or state.blocker_history is None:
        state.blocker_history = []
    if not hasattr(state, "wait_registry") or state.wait_registry is None:
        state.wait_registry = {}
    if not hasattr(state, "wait_history") or state.wait_history is None:
        state.wait_history = []
    if not hasattr(state, "lifecycle_events") or state.lifecycle_events is None:
        state.lifecycle_events = []

    # 1. Migrate blocked_wo_ids
    for wo_id in list(state.blocked_wo_ids):
        found = False
        for bdata in state.blocker_registry.values():
            if bdata.get("work_order_id") == wo_id and bdata.get("status") in (
                BlockerStatus.OPEN.value,
                BlockerStatus.ACKNOWLEDGED.value,
                BlockerStatus.RESOLUTION_REQUESTED.value,
                BlockerStatus.REOPENED.value,
            ):
                found = True
                break
        if not found:
            err = state.worker_blockers.get(wo_id, {}).get("reason") or state.error or "Legacy blocker"
            fail_code = state.worker_blockers.get(wo_id, {}).get("failure_code") or "WORKER_FAILURE"
            create_blocker(
                state=state,
                blocker_type=BlockerType.WORKER_FAILURE,
                owner_subsystem=BlockerSubsystem.EXECUTION,
                work_order_id=wo_id,
                failure_code=fail_code,
                resolution_condition=err,
            )

    # 2. Migrate dependency_waits
    if hasattr(state, "dependency_waits") and state.dependency_waits:
        for wo_id, dinfo in list(state.dependency_waits.items()):
            prereq = dinfo.get("prerequisite") or dinfo.get("prereq") or "unknown"
            found = False
            for wdata in state.wait_registry.values():
                if wdata.get("subject_entity") == wo_id and wdata.get("wait_type") == WaitType.DEPENDENCY.value:
                    if wdata.get("status") == WaitStatus.ACTIVE.value:
                        found = True
                        break
            if not found:
                create_wait(
                    state=state,
                    wait_type=WaitType.DEPENDENCY,
                    owner_subsystem=BlockerSubsystem.DEPENDENCY,
                    subject_entity=wo_id,
                    resolution_predicate=f"Prerequisite {prereq} satisfied",
                    relevant_ref=prereq,
                )


# ── 11. Dispatch Reservation & Concurrency ──────────────────────────────

_DISPATCH_MUTEX = threading.Lock()
_ACTIVE_DISPATCH_RESERVATIONS: dict[str, str] = {}


def reserve_wo_dispatch(run_id: str, wo_id: str) -> bool:
    """Acquire exclusive dispatch reservation for a work order."""
    with _DISPATCH_MUTEX:
        key = f"{run_id}:{wo_id}"
        if key in _ACTIVE_DISPATCH_RESERVATIONS:
            return False
        _ACTIVE_DISPATCH_RESERVATIONS[key] = _now()
        return True


def release_wo_dispatch(run_id: str, wo_id: str) -> None:
    """Release dispatch reservation for a work order."""
    with _DISPATCH_MUTEX:
        key = f"{run_id}:{wo_id}"
        _ACTIVE_DISPATCH_RESERVATIONS.pop(key, None)
