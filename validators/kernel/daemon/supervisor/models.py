"""Data model and shared constants for the lifecycle supervisor.

Pure data: phases, results, errors, exit codes, run state and module-level constants.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..state_store import (  # noqa: F401  (re-exported for the historical module surface)
    DaemonRestartFailClosedError,
    ReplayAttackError,
    StateIntegrityError,
    SupervisorStateManager,
)

def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from model output used in run-state messages."""
    return re.sub(r"```[a-zA-Z]*\n?", "", str(text or "")).strip()

def _extract_op_error(op: Any) -> str | None:
    """Extract a concise human-readable error or reason from an operation record."""
    if not isinstance(op, Mapping):
        return None
    res = op.get("result")
    candidates = []
    if isinstance(res, Mapping):
        candidates.extend([res.get("error"), res.get("reason"), res.get("message"), res.get("summary")])
    candidates.extend([op.get("error"), op.get("reason")])
    for c in candidates:
        if c:
            if isinstance(c, Mapping):
                msg = c.get("message") or c.get("error") or c.get("reason")
                if msg:
                    return str(msg).strip()
            elif isinstance(c, str) and c.strip():
                s = c.strip()
                if s.lower() not in {"failed", "blocked", "completed", "cancelled", "unknown"}:
                    return s
    return None

logger = logging.getLogger("stackmind.supervisor")

class Phase(StrEnum):
    """Deterministic lifecycle phases for a product delivery run."""
    INIT = "INIT"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AUTHORING = "AUTHORING"
    AUTHORING_READINESS_GATE = "AUTHORING_READINESS_GATE"
    ARCHITECT_REPAIR = "ARCHITECT_REPAIR"
    DISPATCHING = "DISPATCHING"
    EXECUTING = "EXECUTING"
    QA_REVIEW = "QA_REVIEW"
    ARCHITECT_RECOVERY_DECISION = "ARCHITECT_RECOVERY_DECISION"
    INTEGRATION_REVIEW = "INTEGRATION_REVIEW"
    FINAL_REVIEW = "FINAL_REVIEW"
    PRODUCT_READY = "PRODUCT_READY"
    GITOPS = "GITOPS"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"

class AdvanceResult(StrEnum):
    """Outcome of a single advance() call."""
    TRANSITIONED = "TRANSITIONED"
    WAITING_FOR_OPERATION = "WAITING_FOR_OPERATION"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"

class OperationContentionError(ValueError):
    """Raised when an operation cannot be started because another root operation is active."""
    pass

class BudgetExhaustedError(Exception):
    """Raised when the global turn budget reaches 0 and no further turns can be dispatched."""
    pass

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_BUDGET_EXHAUSTED = 2
EXIT_RETRIES_EXHAUSTED = 3
EXIT_PLAN_REJECTED = 4
EXIT_INTEGRITY_VIOLATION = 5
EXIT_HEADLESS_TIMEOUT = 137

DEFAULT_TIMEOUTS: dict[Any, float] = {
    Phase.PLANNING: 60.0,
    Phase.AUTHORING: 600.0,
    Phase.AUTHORING_READINESS_GATE: 60.0,
    Phase.DISPATCHING: 60.0,
    Phase.EXECUTING: 180.0,
    Phase.QA_REVIEW: 180.0,
    Phase.INTEGRATION_REVIEW: 180.0,
    Phase.FINAL_REVIEW: 180.0,
    Phase.ARCHITECT_REPAIR: 180.0,
    Phase.ARCHITECT_RECOVERY_DECISION: 300.0,
    Phase.GITOPS: 60.0,
    "gate": 60.0,
    "ci_wait_timeout": 60.0,
    "ci_run_timeout": 1800.0,
}

@dataclass
class RunState:
    """Serialisable lifecycle state for one product delivery run."""
    run_id: str
    product_goal: str
    workspace: str
    session_id: str
    phase: Phase = Phase.INIT
    planning_wo_id: str | None = "WO-000"
    planning_operation_id: str | None = None
    authoring_operation_id: str | None = None
    plan_id: str | None = None
    worker_wo_ids: list[str] = field(default_factory=list)
    completed_wo_ids: list[str] = field(default_factory=list)
    failed_wo_ids: list[str] = field(default_factory=list)
    blocked_wo_ids: list[str] = field(default_factory=list)
    retry_counts: dict[str, int] = field(default_factory=dict)
    attempt_counts: dict[str, int] = field(default_factory=dict)
    contention_count: int = 0
    integration_operation_id: str | None = None
    integration_wo_id: str | None = None
    integration_blockers: list[str] = field(default_factory=list)
    integration_evidence: dict[str, Any] = field(default_factory=dict)
    batch_operation_id: str | None = None
    gitops_wo_id: str | None = None
    gitops_operation_id: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    release_commit_sha: str | None = None
    transitions: list[dict[str, str]] = field(default_factory=list)
    max_retries: int = 10
    max_attempts: int = 10
    ignored_operation_ids: list[str] = field(default_factory=list)
    dependency_escalation_op_id: str | None = None
    dependency_escalation_wo_id: str | None = None
    dependency_escalation_retries: dict[str, int] = field(default_factory=dict)
    # ── Authoring readiness / architect recovery (fail-closed lifecycle) ──
    # Explicit operator-approved bootstrap mode: the only condition under which
    # deterministic child-WO synthesis may run, and never after an Architect
    # authoring failure.
    bootstrap_mode: bool = False
    authoring_revision: int = 1
    max_authoring_repairs: int = 2
    authoring_repair_attempts: int = 0
    authoring_operation_failed: bool = False
    # Work-order IDs belonging to the latest publishable authoring revision.
    published_wo_ids: list[str] = field(default_factory=list)
    readiness_issues: list[dict[str, Any]] = field(default_factory=list)
    architect_repair_operation_id: str | None = None
    repair_context: dict[str, Any] = field(default_factory=dict)
    # Per-WO durable worker-blocker evidence packets (failure_code, observed files…)
    worker_blockers: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_decision_operation_id: str | None = None
    recovery_wo_id: str | None = None
    recovery_attempts: dict[str, int] = field(default_factory=dict)
    recovery_decisions: list[dict[str, Any]] = field(default_factory=list)
    superseded_wo_ids: list[str] = field(default_factory=list)
    milestone_exemptions: list[str] = field(default_factory=list)
    contract_revisions: dict[str, int] = field(default_factory=dict)
    integration_rework_rounds: int = 0
    # QA defect routing: QA work order → developer work order IDs whose
    # rework must re-complete before the QA work order may be re-dispatched.
    qa_rework_targets: dict[str, list[str]] = field(default_factory=dict)
    # Mandatory QA review gate: work order ID → in-flight QA review operation ID.
    qa_review_operations: dict[str, str] = field(default_factory=dict)
    qa_review_attempts: dict[str, int] = field(default_factory=dict)
    max_qa_review_attempts: int = 2
    # Architect final acceptance gate (runs against the approved plan).
    final_review_operation_id: str | None = None
    final_review_rounds: int = 0
    max_final_review_rounds: int = 2
    final_review_history: list[dict[str, Any]] = field(default_factory=list)
    # Bounds the prerequisite bounce between FINAL_REVIEW and EXECUTING so a
    # permanently unsatisfiable prerequisite cannot loop forever.
    final_prerequisite_rounds: int = 0
    execution_profile: str = "medium"
    gitops_feedback: str | None = None
    # ── P0-5 Counters and Global Turn Budget ─────────────────────────────
    worker_retries: dict[str, int] = field(default_factory=dict)
    qa_reworks: dict[str, int] = field(default_factory=dict)
    integration_retries: int = 0
    max_integration_retries: int = 2
    gitops_retries: int = 0
    max_gitops_retries: int = 2
    max_worker_retries: int = 2
    max_qa_reworks: int = 2
    global_turn_budget: int = 50
    initial_turn_budget: int = 50
    budget_exhausted: bool = False
    human_resumes_used: int = 0
    max_human_resumes: int = 2
    human_override_logs: list[dict[str, Any]] = field(default_factory=list)
    blocked_by_escalation: dict[str, str] = field(default_factory=dict)
    wo_dependencies: dict[str, list[str]] = field(default_factory=dict)
    exit_code: int = 0
    stall_ticks: int = 0
    max_stall_ticks: int = 3
    interrupted_phase: str | None = None
    daemon_restarts: int = 0
    max_daemon_restarts: int = 3
    executing_wo_id: str | None = None
    planning_retries: int = 0
    max_planning_retries: int = 2
    delivery_status_delivered: bool = False
    delivery_status_summary: str | None = None
    delivery_status_operation_id: str | None = None
    # In-memory only monotonic deadlines for state timeouts (never persisted to disk)
    turn_deadline: float | None = None
    turn_started_at: float | None = None
    run_started_at: float | None = None
    waiting_for_human_entered_at: float | None = None
    # Last dispatch failure diagnostic (set by _architect_turn on failure)
    last_dispatch_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "product_goal": self.product_goal,
            "workspace": self.workspace,
            "session_id": self.session_id,
            "phase": self.phase.value,
            "planning_wo_id": self.planning_wo_id,
            "planning_operation_id": self.planning_operation_id,
            "authoring_operation_id": self.authoring_operation_id,
            "plan_id": self.plan_id,
            "worker_wo_ids": list(self.worker_wo_ids),
            "completed_wo_ids": list(self.completed_wo_ids),
            "failed_wo_ids": list(self.failed_wo_ids),
            "blocked_wo_ids": list(self.blocked_wo_ids),
            "retry_counts": dict(self.retry_counts),
            "attempt_counts": dict(self.attempt_counts),
            "contention_count": self.contention_count,
            "integration_operation_id": self.integration_operation_id,
            "integration_wo_id": self.integration_wo_id,
            "integration_blockers": list(self.integration_blockers),
            "integration_evidence": dict(self.integration_evidence),
            "batch_operation_id": self.batch_operation_id,
            "gitops_wo_id": self.gitops_wo_id,
            "gitops_operation_id": self.gitops_operation_id,
            "release_commit_sha": self.release_commit_sha,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "transitions": list(self.transitions),
            "max_retries": self.max_retries,
            "max_attempts": self.max_attempts,
            "ignored_operation_ids": list(self.ignored_operation_ids),
            "dependency_escalation_op_id": self.dependency_escalation_op_id,
            "dependency_escalation_wo_id": self.dependency_escalation_wo_id,
            "dependency_escalation_retries": dict(self.dependency_escalation_retries),
            "bootstrap_mode": bool(self.bootstrap_mode),
            "authoring_revision": self.authoring_revision,
            "max_authoring_repairs": self.max_authoring_repairs,
            "authoring_repair_attempts": self.authoring_repair_attempts,
            "authoring_operation_failed": bool(self.authoring_operation_failed),
            "published_wo_ids": list(self.published_wo_ids),
            "readiness_issues": list(self.readiness_issues),
            "architect_repair_operation_id": self.architect_repair_operation_id,
            "repair_context": dict(self.repair_context),
            "worker_blockers": dict(self.worker_blockers),
            "recovery_decision_operation_id": self.recovery_decision_operation_id,
            "recovery_wo_id": self.recovery_wo_id,
            "recovery_attempts": dict(self.recovery_attempts),
            "recovery_decisions": list(self.recovery_decisions),
            "superseded_wo_ids": list(self.superseded_wo_ids),
            "milestone_exemptions": list(self.milestone_exemptions),
            "contract_revisions": dict(self.contract_revisions),
            "integration_rework_rounds": self.integration_rework_rounds,
            "qa_rework_targets": dict(self.qa_rework_targets),
            "qa_review_operations": dict(self.qa_review_operations),
            "qa_review_attempts": dict(self.qa_review_attempts),
            "max_qa_review_attempts": self.max_qa_review_attempts,
            "final_review_operation_id": self.final_review_operation_id,
            "final_review_rounds": self.final_review_rounds,
            "max_final_review_rounds": self.max_final_review_rounds,
            "final_review_history": list(self.final_review_history),
            "final_prerequisite_rounds": self.final_prerequisite_rounds,
            "execution_profile": self.execution_profile,
            "gitops_feedback": self.gitops_feedback,
            "worker_retries": dict(self.worker_retries),
            "qa_reworks": dict(self.qa_reworks),
            "integration_retries": self.integration_retries,
            "max_integration_retries": self.max_integration_retries,
            "gitops_retries": self.gitops_retries,
            "max_gitops_retries": self.max_gitops_retries,
            "max_worker_retries": self.max_worker_retries,
            "max_qa_reworks": self.max_qa_reworks,
            "global_turn_budget": self.global_turn_budget,
            "initial_turn_budget": self.initial_turn_budget,
            "budget_exhausted": bool(self.budget_exhausted),
            "human_resumes_used": self.human_resumes_used,
            "max_human_resumes": self.max_human_resumes,
            "human_override_logs": list(self.human_override_logs),
            "blocked_by_escalation": dict(self.blocked_by_escalation),
            "wo_dependencies": {k: list(v) for k, v in self.wo_dependencies.items()},
            "exit_code": self.exit_code,
            "stall_ticks": self.stall_ticks,
            "max_stall_ticks": self.max_stall_ticks,
            "interrupted_phase": self.interrupted_phase,
            "daemon_restarts": self.daemon_restarts,
            "max_daemon_restarts": self.max_daemon_restarts,
            "executing_wo_id": self.executing_wo_id,
            "planning_retries": self.planning_retries,
            "max_planning_retries": self.max_planning_retries,
            "delivery_status_delivered": bool(self.delivery_status_delivered),
            "delivery_status_summary": self.delivery_status_summary,
            "delivery_status_operation_id": self.delivery_status_operation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        return cls(
            run_id=data["run_id"],
            product_goal=data["product_goal"],
            workspace=data["workspace"],
            session_id=data["session_id"],
            phase=Phase(data.get("phase", "INIT")),
            planning_wo_id=data.get("planning_wo_id", "WO-000"),
            planning_operation_id=data.get("planning_operation_id"),
            authoring_operation_id=data.get("authoring_operation_id"),
            plan_id=data.get("plan_id"),
            worker_wo_ids=data.get("worker_wo_ids", []),
            completed_wo_ids=data.get("completed_wo_ids", []),
            failed_wo_ids=data.get("failed_wo_ids", []),
            blocked_wo_ids=data.get("blocked_wo_ids", []),
            retry_counts=data.get("retry_counts", {}),
            attempt_counts=data.get("attempt_counts", {}),
            contention_count=data.get("contention_count", 0),
            integration_operation_id=data.get("integration_operation_id"),
            integration_wo_id=data.get("integration_wo_id"),
            integration_blockers=data.get("integration_blockers", []),
            integration_evidence=dict(data.get("integration_evidence", {}) or {}),
            batch_operation_id=data.get("batch_operation_id"),
            gitops_wo_id=data.get("gitops_wo_id"),
            gitops_operation_id=data.get("gitops_operation_id"),
            release_commit_sha=data.get("release_commit_sha"),
            error=data.get("error"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            transitions=data.get("transitions", []),
            max_retries=data.get("max_retries", 10),
            max_attempts=data.get("max_attempts", 10),
            ignored_operation_ids=list(data.get("ignored_operation_ids", [])),
            dependency_escalation_op_id=data.get("dependency_escalation_op_id"),
            dependency_escalation_wo_id=data.get("dependency_escalation_wo_id"),
            dependency_escalation_retries=data.get("dependency_escalation_retries", {}),
            bootstrap_mode=bool(data.get("bootstrap_mode", False)),
            authoring_revision=int(data.get("authoring_revision", 1)),
            max_authoring_repairs=int(data.get("max_authoring_repairs", 2)),
            authoring_repair_attempts=int(data.get("authoring_repair_attempts", 0)),
            authoring_operation_failed=bool(data.get("authoring_operation_failed", False)),
            published_wo_ids=list(data.get("published_wo_ids", [])),
            readiness_issues=list(data.get("readiness_issues", [])),
            architect_repair_operation_id=data.get("architect_repair_operation_id"),
            repair_context=dict(data.get("repair_context", {}) or {}),
            worker_blockers=dict(data.get("worker_blockers", {}) or {}),
            recovery_decision_operation_id=data.get("recovery_decision_operation_id"),
            recovery_wo_id=data.get("recovery_wo_id"),
            recovery_attempts=dict(data.get("recovery_attempts", {}) or {}),
            recovery_decisions=list(data.get("recovery_decisions", [])),
            superseded_wo_ids=list(data.get("superseded_wo_ids", [])),
            milestone_exemptions=list(data.get("milestone_exemptions", [])),
            contract_revisions=dict(data.get("contract_revisions", {}) or {}),
            integration_rework_rounds=int(data.get("integration_rework_rounds", 0)),
            qa_rework_targets=dict(data.get("qa_rework_targets", {}) or {}),
            qa_review_operations=dict(data.get("qa_review_operations", {}) or {}),
            qa_review_attempts=dict(data.get("qa_review_attempts", {}) or {}),
            max_qa_review_attempts=int(data.get("max_qa_review_attempts", 2)),
            final_review_operation_id=data.get("final_review_operation_id"),
            final_review_rounds=int(data.get("final_review_rounds", 0)),
            max_final_review_rounds=int(data.get("max_final_review_rounds", 2)),
            final_review_history=list(data.get("final_review_history", []) or []),
            final_prerequisite_rounds=int(data.get("final_prerequisite_rounds", 0)),
            execution_profile=str(data.get("execution_profile", "medium")),
            gitops_feedback=data.get("gitops_feedback"),
            worker_retries=dict(data.get("worker_retries", {}) or {}),
            qa_reworks=dict(data.get("qa_reworks", {}) or {}),
            integration_retries=int(data.get("integration_retries", 0)),
            max_integration_retries=int(data.get("max_integration_retries", 2)),
            gitops_retries=int(data.get("gitops_retries", 0)),
            max_gitops_retries=int(data.get("max_gitops_retries", 2)),
            max_worker_retries=int(data.get("max_worker_retries", 2)),
            max_qa_reworks=int(data.get("max_qa_reworks", 2)),
            global_turn_budget=int(data.get("global_turn_budget", 50)),
            initial_turn_budget=int(data.get("initial_turn_budget", 50)),
            budget_exhausted=bool(data.get("budget_exhausted", False)),
            human_resumes_used=int(data.get("human_resumes_used", 0)),
            max_human_resumes=int(data.get("max_human_resumes", 2)),
            human_override_logs=list(data.get("human_override_logs", []) or []),
            blocked_by_escalation=dict(data.get("blocked_by_escalation", {}) or {}),
            wo_dependencies=dict(data.get("wo_dependencies", {}) or {}),
            exit_code=int(data.get("exit_code", 0)),
            stall_ticks=int(data.get("stall_ticks", 0)),
            max_stall_ticks=int(data.get("max_stall_ticks", 3)),
            interrupted_phase=data.get("interrupted_phase"),
            daemon_restarts=int(data.get("daemon_restarts", 0)),
            max_daemon_restarts=int(data.get("max_daemon_restarts", 3)),
            executing_wo_id=data.get("executing_wo_id"),
            planning_retries=int(data.get("planning_retries", 0)),
            max_planning_retries=int(data.get("max_planning_retries", 2)),
            delivery_status_delivered=bool(data.get("delivery_status_delivered", False)),
            delivery_status_summary=data.get("delivery_status_summary"),
            delivery_status_operation_id=data.get("delivery_status_operation_id"),
        )

_OPERATION_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}

# Governance failure codes (from harness evidence packets) that must route to
# an Architect recovery decision instead of an automatic same-contract retry.
GOVERNANCE_FAILURE_CODES = {
    "CONTRACT_FILE_BUDGET_EXCEEDED",
    "CONTRACT_SCOPE_VIOLATION",
    "CONTRACT_SCOPE_DENIED",
    "CONTRACT_READ_ONLY_VIOLATION",
    "CONTRACT_EXPIRED",
    "CONTRACT_VALIDATION_ERROR",
    "D024_QA_GATE_BLOCKED",
    "D025_DESTRUCTIVE_OPERATION",
    "STAGED_VALIDATION_FAILED",
    "QA_EVIDENCE_MISSING",
}

# Failure classes for which retrying the worker with an unchanged contract is
# permissible — exclusively transient execution problems, never scope/budget/
# schema governance failures.
TRANSIENT_RETRYABLE_CODES = {
    "OUTCOME_NOT_VERIFIED",
    "CODE_NOT_VERIFIED",
    "STATE_NOT_VERIFIED",
    "SECURITY_NOT_VERIFIED",
    "VERIFICATION_GATE_FAILED",
    "IMPORT_SATISFIABILITY_FAILED",
    "TOOL_LOOP_HALTED",
    "HARNESS_DECISION_INVALID",
    "DELIVERABLE_NOT_WRITTEN",
    "WORKER_BLOCKED",
    "LOOP_GUARD_TRIPPED",
    "TEST_EXECUTION_FAILED",
    "EXECUTION_ERROR",
    "PROCESS_FAILED",
    "DEPENDENCY_ERROR",
}

# Role-mapping constants (duplicated from manager to avoid circular concerns)
_ROLE_TO_PRIMARY_AGENT = {
    "architecture": "claude",
    "backend": "codex",
    "frontend": "gemini",
    "qa": "gemma",
    "gitops": "local-llm",
}

_AGENT_ROLE_KEYWORDS = {
    "gemini": ("ui", "frontend", "view", "component", "screen", "css", "html", "react", "client"),
    "gemma": ("qa", "test", "verification", "audit", "review"),
    "local-llm": ("release", "git", "deploy", "packaging", "version", "gitops"),
}
