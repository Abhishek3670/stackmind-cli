"""Deterministic lifecycle Supervisor for StackMind product delivery.

Drives the complete multi-agent lifecycle from CEO product request through
GitOps release commit.  This is *deterministic orchestration logic*, NOT an
autonomous LLM agent — it reads disk/session state after each operation
completes and dispatches the next valid action.

State Machine
─────────────
INIT → PLANNING → AWAITING_APPROVAL → AUTHORING → DISPATCHING
→ EXECUTING → INTEGRATION_REVIEW → PRODUCT_READY → GITOPS → COMPLETE

Constraints honoured:
 • Agents run through existing governed Harness/AgentRunner
 • File-based delegation via inbox/work-order/state (IDE-01)
 • All existing gates preserved (D024, Contract, Security, Staged-diff)
 • Human approval gate at plan review
 • Resumable from any state on restart
 • One product request = one identifiable lifecycle run
"""

from __future__ import annotations

import logging
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("stackmind.supervisor")


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from model output used in run-state messages."""
    return re.sub(r"```[a-zA-Z]*\n?", "", str(text or "")).strip()


# ─── Lifecycle phases ─────────────────────────────────────────────────

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
    ARCHITECT_RECOVERY_DECISION = "ARCHITECT_RECOVERY_DECISION"
    INTEGRATION_REVIEW = "INTEGRATION_REVIEW"
    PRODUCT_READY = "PRODUCT_READY"
    GITOPS = "GITOPS"
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


# ─── Run state ────────────────────────────────────────────────────────

@dataclass
class RunState:
    """Serialisable lifecycle state for one product delivery run."""
    run_id: str
    product_goal: str
    workspace: str
    session_id: str
    phase: Phase = Phase.INIT
    planning_wo_id: str = "WO-000"
    planning_operation_id: str | None = None
    authoring_operation_id: str | None = None
    plan_id: str | None = None
    worker_wo_ids: list[str] = field(default_factory=list)
    completed_wo_ids: list[str] = field(default_factory=list)
    failed_wo_ids: list[str] = field(default_factory=list)
    blocked_wo_ids: list[str] = field(default_factory=list)
    retry_counts: dict[str, int] = field(default_factory=dict)
    contention_count: int = 0
    integration_operation_id: str | None = None
    integration_wo_id: str | None = None
    integration_blockers: list[str] = field(default_factory=list)
    batch_operation_id: str | None = None
    gitops_wo_id: str | None = None
    gitops_operation_id: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    release_commit_sha: str | None = None
    transitions: list[dict[str, str]] = field(default_factory=list)
    max_retries: int = 2
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
            "contention_count": self.contention_count,
            "integration_operation_id": self.integration_operation_id,
            "integration_wo_id": self.integration_wo_id,
            "integration_blockers": list(self.integration_blockers),
            "batch_operation_id": self.batch_operation_id,
            "gitops_wo_id": self.gitops_wo_id,
            "gitops_operation_id": self.gitops_operation_id,
            "release_commit_sha": self.release_commit_sha,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "transitions": list(self.transitions),
            "max_retries": self.max_retries,
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
            contention_count=data.get("contention_count", 0),
            integration_operation_id=data.get("integration_operation_id"),
            integration_wo_id=data.get("integration_wo_id"),
            integration_blockers=data.get("integration_blockers", []),
            batch_operation_id=data.get("batch_operation_id"),
            gitops_wo_id=data.get("gitops_wo_id"),
            gitops_operation_id=data.get("gitops_operation_id"),
            release_commit_sha=data.get("release_commit_sha"),
            error=data.get("error"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            transitions=data.get("transitions", []),
            max_retries=data.get("max_retries", 2),
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
        )


from validators.clock import now as _now


# ─── Supervisor ───────────────────────────────────────────────────────

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
    "TOOL_LOOP_HALTED",
    "HARNESS_DECISION_INVALID",
    "DELIVERABLE_NOT_WRITTEN",
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


class LifecycleSupervisor:
    """Deterministic lifecycle driver using SessionManager as execution substrate.

    Usage::

        supervisor = LifecycleSupervisor(session_manager)
        run_state = supervisor.start_run("run-001", "Create a login page", workspace, session_id)
        while True:
            result = supervisor.advance(run_state)
            if result in (AdvanceResult.COMPLETE, AdvanceResult.FAILED, AdvanceResult.BLOCKED):
                break
            if result == AdvanceResult.WAITING_FOR_HUMAN:
                # present plan to user, wait for approve_plan() / reject_plan()
                break
            if result == AdvanceResult.WAITING_FOR_OPERATION:
                # poll or wait for operation completion event
                time.sleep(2)
    """

    def __init__(self, manager: Any, max_contention_retries: int = 50) -> None:
        """Initialise with a SessionManager (or compatible mock)."""
        self.manager = manager
        self.max_contention_retries = max_contention_retries

    # ── Lifecycle entry point ─────────────────────────────────────────

    def start_run(
        self,
        run_id: str,
        product_goal: str,
        workspace: str | Path,
        session_id: str,
        bootstrap_mode: bool = False,
    ) -> RunState:
        """Create a new lifecycle run and transition to PLANNING."""
        now = _now()
        state = RunState(
            run_id=run_id,
            product_goal=product_goal,
            workspace=str(workspace),
            session_id=session_id,
            phase=Phase.INIT,
            bootstrap_mode=bootstrap_mode,
            created_at=now,
            updated_at=now,
        )
        self._transition(state, Phase.PLANNING)
        return state

    # ── Core advance loop ─────────────────────────────────────────────

    def advance(self, state: RunState) -> AdvanceResult:
        """Evaluate the current phase and advance to the next valid state.

        This is the core deterministic dispatch function.  It reads disk and
        session state, determines the next action, and either dispatches it
        or returns a wait/terminal result.

        Each call makes AT MOST one state transition to keep the loop
        predictable and auditable.
        """
        handler = {
            Phase.INIT: self._advance_init,
            Phase.PLANNING: self._advance_planning,
            Phase.AWAITING_APPROVAL: self._advance_awaiting_approval,
            Phase.AUTHORING: self._advance_authoring,
            Phase.AUTHORING_READINESS_GATE: self._advance_authoring_readiness_gate,
            Phase.ARCHITECT_REPAIR: self._advance_architect_repair,
            Phase.DISPATCHING: self._advance_dispatching,
            Phase.EXECUTING: self._advance_executing,
            Phase.ARCHITECT_RECOVERY_DECISION: self._advance_recovery_decision,
            Phase.INTEGRATION_REVIEW: self._advance_integration_review,
            Phase.PRODUCT_READY: self._advance_product_ready,
            Phase.GITOPS: self._advance_gitops,
            Phase.COMPLETE: lambda _s: AdvanceResult.COMPLETE,
            Phase.FAILED: lambda _s: AdvanceResult.FAILED,
            Phase.BLOCKED: lambda _s: AdvanceResult.BLOCKED,
        }.get(state.phase)
        if handler is None:
            return AdvanceResult.FAILED
        try:
            res = handler(state)
            if res != AdvanceResult.WAITING_FOR_OPERATION:
                state.contention_count = 0
            return res
        except OperationContentionError as exc:
            state.contention_count += 1
            if state.contention_count >= self.max_contention_retries:
                state.error = (
                    f"Session operation contention timeout: session remained busy after "
                    f"{state.contention_count} consecutive attempts ({exc})"
                )
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED
            return AdvanceResult.WAITING_FOR_OPERATION
        except ValueError as exc:
            # Fallback for mock session managers or unmigrated call sites
            err_str = str(exc).lower()
            if "running root operation" in err_str or "active operation" in err_str:
                state.contention_count += 1
                if state.contention_count >= self.max_contention_retries:
                    state.error = (
                        f"Session operation contention timeout: session remained busy after "
                        f"{state.contention_count} consecutive attempts ({exc})"
                    )
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                return AdvanceResult.WAITING_FOR_OPERATION
            state.error = f"{type(exc).__name__}: {exc}"
            self._transition(state, Phase.FAILED)
            return AdvanceResult.FAILED
        except Exception as exc:
            state.error = f"{type(exc).__name__}: {exc}"
            self._transition(state, Phase.FAILED)
            return AdvanceResult.FAILED

    def drive(
        self,
        state: RunState,
        max_steps: int = 100,
        poll_interval: float = 0.5,
        max_wait_seconds: float = 1800.0,
        on_transition: Any = None,
    ) -> AdvanceResult:
        """Continuously drive the lifecycle until a pause or terminal phase is reached.

        Halts when:
        - WAITING_FOR_HUMAN (e.g. plan proposed and awaiting operator approval)
        - COMPLETE (GitOps finished and lifecycle complete)
        - BLOCKED (work order or gate blocked)
        - FAILED (terminal error or retries exhausted)

        Waits and polls when WAITING_FOR_OPERATION.
        """
        step = 0
        start_time = time.monotonic()
        while step < max_steps:
            step += 1
            old_phase = state.phase
            result = self.advance(state)
            if on_transition and state.phase != old_phase:
                on_transition(state, old_phase, state.phase)

            if result in (
                AdvanceResult.WAITING_FOR_HUMAN,
                AdvanceResult.COMPLETE,
                AdvanceResult.BLOCKED,
                AdvanceResult.FAILED,
            ):
                return result

            if result == AdvanceResult.WAITING_FOR_OPERATION:
                if time.monotonic() - start_time > max_wait_seconds:
                    state.error = f"Timed out waiting for operations after {max_wait_seconds}s"
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                time.sleep(poll_interval)
                continue

            # TRANSITIONED: continue immediately to the next advance step
            continue

        state.error = f"Exceeded max advance steps ({max_steps})"
        return AdvanceResult.BLOCKED

    # ── Phase handlers ────────────────────────────────────────────────

    def _advance_init(self, state: RunState) -> AdvanceResult:
        """INIT → PLANNING: synthesise bootstrap WO and dispatch Architecture."""
        self._transition(state, Phase.PLANNING)
        return self._advance_planning(state)

    def _advance_planning(self, state: RunState) -> AdvanceResult:
        """PLANNING: dispatch Architecture planning turn if not already running."""
        if not state.planning_operation_id:
            # S8 Crash recovery check: if operation was already started before a crash,
            # discover it from session operations rather than re-dispatching a duplicate turn.
            try:
                for op in reversed(self.manager.list_operations(state.session_id)):
                    op_id = op.get("operation_id")
                    if op_id and op_id in getattr(state, "ignored_operation_ids", []):
                        continue
                    if op.get("work_order_id") == state.planning_wo_id and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                        state.planning_operation_id = op_id
                        break
            except Exception:
                pass

        if state.planning_operation_id:
            if hasattr(state, "ignored_operation_ids") and state.planning_operation_id in state.ignored_operation_ids:
                state.planning_operation_id = None
                return self._advance_planning(state)
            op = self._get_operation(state.planning_operation_id)
            if op is None:
                # Operation record lost — re-dispatch
                state.planning_operation_id = None
                return self._advance_planning(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION
            if status in ("FAILED", "BLOCKED", "CANCELLED"):
                op_result = op.get("result", {}) or {}
                err_detail = (
                    op_result.get("error")
                    or op_result.get("reason")
                    or op.get("error")
                    or "unknown"
                )
                state.error = f"Planning turn failed ({status.lower()}): {err_detail}"
                self._transition(state, Phase.FAILED)
                return AdvanceResult.FAILED
            # Planning completed — check if a plan was proposed for this turn
            plans = self.manager.list_plans(state.session_id)
            matching_plan = None
            if state.planning_operation_id:
                for p in reversed(plans):
                    if p.get("metadata", {}).get("operation_id") == state.planning_operation_id:
                        matching_plan = p
                        break
            if matching_plan is None and plans:
                if str(plans[-1].get("state", "")).upper() == "AWAITING_APPROVAL":
                    matching_plan = plans[-1]

            if matching_plan:
                state.plan_id = matching_plan.get("plan_id")
                if state.planning_wo_id and state.planning_wo_id not in state.completed_wo_ids:
                    state.completed_wo_ids.append(state.planning_wo_id)
                    archive_completed_work_orders(Path(state.workspace), [state.planning_wo_id])
                plan_state = str(matching_plan.get("state", "")).upper()
                if plan_state == "AWAITING_APPROVAL":
                    self._transition(state, Phase.AWAITING_APPROVAL)
                    return AdvanceResult.WAITING_FOR_HUMAN
                if plan_state == "APPROVED":
                    self._transition(state, Phase.AUTHORING)
                    return AdvanceResult.TRANSITIONED
            # No plan proposed but turn completed — this is unexpected
            state.error = "Planning turn completed without proposing a plan"
            self._transition(state, Phase.FAILED)
            return AdvanceResult.FAILED

        # Dispatch planning turn
        op = self.manager.start_turn(
            state.session_id,
            state.product_goal,
            is_goal=True,
            role="architecture",
            agent_id="claude",
            work_order_id=state.planning_wo_id,
            run_id=state.run_id,
        )
        state.planning_operation_id = op.get("operation_id")
        state.planning_wo_id = op.get("work_order_id") or state.planning_wo_id
        return AdvanceResult.WAITING_FOR_OPERATION

    def _advance_awaiting_approval(self, state: RunState) -> AdvanceResult:
        """AWAITING_APPROVAL: check if the plan has been approved or rejected."""
        if not state.plan_id:
            state.error = "No plan_id recorded"
            self._transition(state, Phase.FAILED)
            return AdvanceResult.FAILED

        plan = self.manager.get_plan(state.session_id, state.plan_id)
        plan_state = str(plan.get("state", "")).upper()

        if plan_state == "APPROVED":
            self._transition(state, Phase.AUTHORING)
            return AdvanceResult.TRANSITIONED
        if plan_state == "REJECTED":
            # Re-dispatch planning with feedback
            feedback = plan.get("feedback") or plan.get("reason") or ""
            state.planning_operation_id = None  # reset to re-dispatch
            self._transition(state, Phase.PLANNING)
            # Update product goal with feedback for re-planning
            if feedback:
                state.product_goal = (
                    f"{state.product_goal}\n\n"
                    f"PREVIOUS PLAN REJECTED. Feedback: {feedback}\n"
                    f"Please revise the plan addressing this feedback."
                )
            return AdvanceResult.TRANSITIONED
        # Still awaiting
        return AdvanceResult.WAITING_FOR_HUMAN

    def _advance_authoring(self, state: RunState) -> AdvanceResult:
        """AUTHORING: wait for Architecture Turn 2 (WO/Contract authoring) to complete.

        Fail-closed semantics: a failed or blocked authoring operation, or an
        artifact set that never materialises, routes to ARCHITECT_REPAIR with
        structured evidence.  The supervisor never synthesizes replacement
        business work orders in response to an Architect authoring failure;
        deterministic synthesis is permitted only in explicit operator-approved
        bootstrap mode when the authoring turn merely produced no artifacts.
        """
        if state.authoring_operation_id:
            op = self._get_operation(state.authoring_operation_id)
            if op is None:
                state.authoring_operation_id = None
                return self._advance_authoring(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION

            result = op.get("result", {}) or {}
            authoring_failed = (status in ("FAILED", "BLOCKED")) or (
                isinstance(result, dict)
                and str(result.get("status", "")).lower() in ("blocked", "failed")
            )

            if authoring_failed:
                # Fail closed: never synthesize child WOs after an Architect failure.
                state.authoring_operation_failed = True
                err_detail = (
                    result.get("error")
                    or result.get("reason")
                    or f"authoring operation ended in status {status}"
                )
                state.readiness_issues = [{
                    "code": "AUTHORING_OPERATION_FAILED",
                    "category": "schema",
                    "message": str(err_detail),
                    "work_order_id": None,
                    "artifact_path": None,
                }]
                return self._enter_architect_repair(
                    state,
                    reason=f"Authoring operation failed: {err_detail}",
                )

            # Authoring completed — discover authored artifacts from disk only.
            self._discover_worker_wos(state)
            if not state.worker_wo_ids:
                # Deterministic synthesis is permitted ONLY in explicit bootstrap
                # mode (operator-approved goal dispatch) and never after an
                # authoring failure.
                if state.bootstrap_mode and not state.authoring_operation_failed:
                    from .authoring import synthesize_child_work_orders
                    synthesis_error: str | None = None
                    try:
                        plan = (
                            self.manager.get_plan(state.session_id, state.plan_id)
                            if state.plan_id else {}
                        )
                        synthesize_child_work_orders(
                            Path(state.workspace), plan, session_id=state.session_id
                        )
                    except Exception as exc:
                        synthesis_error = str(exc)
                    if synthesis_error is None:
                        self._discover_worker_wos(state)
                    else:
                        state.readiness_issues = [{
                            "code": "BOOTSTRAP_SYNTHESIS_FAILED",
                            "category": "coverage",
                            "message": f"Bootstrap synthesis failed: {synthesis_error}",
                            "work_order_id": None,
                            "artifact_path": None,
                        }]
                        return self._enter_architect_repair(
                            state,
                            reason=f"Bootstrap synthesis failed: {synthesis_error}",
                        )
                else:
                    state.readiness_issues = [{
                        "code": "NO_WORK_ORDERS_AUTHORED",
                        "category": "coverage",
                        "message": (
                            "Authoring turn completed without authoring any worker work orders"
                        ),
                        "work_order_id": None,
                        "artifact_path": None,
                    }]
                    return self._enter_architect_repair(
                        state,
                        reason=(
                            "Authoring turn completed without authoring any worker work orders; "
                            "dispatch the Architect to author them"
                        ),
                    )

            # Atomic publication: the artifact set must pass the readiness gate
            # before anything can be dispatched.
            self._transition(state, Phase.AUTHORING_READINESS_GATE)
            return AdvanceResult.TRANSITIONED

        # Check if already running or previously dispatched
        ops = self.manager.list_operations(state.session_id)
        for op in reversed(ops):
            op_id = op.get("operation_id")
            if op_id and op_id in getattr(state, "ignored_operation_ids", []):
                continue
            metadata = op.get("metadata", {})
            if (
                metadata.get("is_authoring")
                and not metadata.get("is_repair")
                and not metadata.get("is_recovery_decision")
                and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL
            ):
                state.authoring_operation_id = op_id
                return self._advance_authoring(state)

        # Supervisor directly dispatches Turn 2 (Authoring child WOs and Contracts)
        authoring_prompt = (
            f"The architecture plan '{state.plan_id or 'PLAN-001'}' has been approved by the operator.\n\n"
            "Your task now as Senior Architect is to author the implementation Work Orders and Contracts for the tasks in PLAN.md:\n"
            "1. Review PLAN.md for the approved milestones and tasks.\n"
            "2. For each task, call write_file to write a Work Order YAML file to .sync/work-orders/ACTIVE/<WO-ID>.yaml "
            "(e.g. WO-001.yaml) conforming to schemas/work-order.schema.json.\n"
            "3. For each Work Order, call write_file to write a corresponding Contract YAML file to .sync/contracts/<WO-ID>.yaml "
            "conforming to schemas/contract.schema.json.\n"
            "4. Size each contract budget from the task's expected file set: declare an implementation_estimate "
            "(expected_files and max_files_touched) in the work order and set the contract budget "
            "max_files_touched to cover it.\n"
            "5. QA/verification Work Orders (assigned to gemma) MUST declare an executable code deliverable "
            "under tests/ — the test suite the QA worker will author and execute end-to-end — with tasks "
            "ordered author-suite, execute-suite, record-verdict. Never declare a QA deliverable as a bare "
            "sign-off document, and plan a companion test (e.g. tests/test_<stem>.py) for every code deliverable.\n"
            "6. When all work orders and contracts are written, return the final HarnessDecision JSON declaring status 'completed' "
            "and modified_files listing all authored files."
        )
        # Canonical authoring policy digest + approved milestone table: the
        # architect authors against the same policy the readiness gate
        # enforces, and knows the stable milestone ids to reference.
        try:
            from validators.harness.authoring_policy import authoring_prompt_digest
            from validators.harness.authoring_readiness import load_plan_milestones

            authoring_prompt += "\n\n" + authoring_prompt_digest()
            plan_milestones = load_plan_milestones(Path(state.workspace))
            if plan_milestones:
                milestone_table = "\n".join(
                    f"  - {ref['id']}: '{ref['title']}'"
                    + (f" (agent: {ref['agent']})" if ref.get("agent") else "")
                    for ref in plan_milestones
                )
                authoring_prompt += (
                    "\n\nApproved plan milestones (set `milestone_id` on each work "
                    f"order to the matching id):\n{milestone_table}"
                )
        except Exception:
            pass
        plan = {}
        if state.plan_id:
            try:
                plan = self.manager.get_plan(state.session_id, state.plan_id)
            except Exception:
                pass
        turn_wo_id = plan.get("metadata", {}).get("work_order_id") or "WO-000"
        op = self.manager.start_turn(
            state.session_id,
            authoring_prompt,
            role="architecture",
            agent_id="claude",
            work_order_id=turn_wo_id,
            is_authoring=True,
        )
        state.authoring_operation_id = op.get("operation_id")
        return AdvanceResult.WAITING_FOR_OPERATION

    def _advance_authoring_readiness_gate(self, state: RunState) -> AdvanceResult:
        """AUTHORING_READINESS_GATE: validate the authored artifact set before dispatch.

        Only a fully ready revision is publishable; failures route to
        ARCHITECT_REPAIR with structured evidence persisted for audit.
        """
        ws = Path(state.workspace)
        self._discover_worker_wos(state)
        gate_result = self._run_readiness_gate(state, ws)
        if gate_result.ready:
            state.published_wo_ids = sorted(state.worker_wo_ids)
            state.readiness_issues = []
            state.error = None
            self._transition(state, Phase.DISPATCHING)
            return AdvanceResult.TRANSITIONED
        issue_summary = "; ".join(
            f"{issue.code}: {issue.message}" for issue in gate_result.issues[:5]
        )
        return self._enter_architect_repair(
            state,
            reason=f"Authoring readiness gate failed: {issue_summary}",
            issues=gate_result.issues,
            archive_failed_artifacts=True,
        )

    def _advance_architect_repair(self, state: RunState) -> AdvanceResult:
        """ARCHITECT_REPAIR: wait for the bounded Architect repair turn, then revalidate."""
        if state.architect_repair_operation_id:
            op = self._get_operation(state.architect_repair_operation_id)
            if op is None:
                state.architect_repair_operation_id = None
                return self._advance_architect_repair(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION

            result = op.get("result", {}) or {}
            op_failed = status in ("FAILED", "BLOCKED") or (
                isinstance(result, dict)
                and str(result.get("status", "")).lower() in ("blocked", "failed")
            )
            ws = Path(state.workspace)
            source = str(state.repair_context.get("source", "authoring"))
            wo_id = state.repair_context.get("work_order")
            action = state.repair_context.get("action")

            if op_failed:
                state.authoring_repair_attempts += 1
                if state.authoring_repair_attempts > state.max_authoring_repairs:
                    state.error = (
                        f"Architect repair turn failed ({status}) and repair attempts are exhausted "
                        f"({state.authoring_repair_attempts}/{state.max_authoring_repairs}); "
                        f"operator intervention required"
                    )
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.architect_repair_operation_id = None
                new_op = self._dispatch_repair_turn(state, ws)
                if new_op is None:
                    state.error = "Architect repair turn could not be re-dispatched"
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.architect_repair_operation_id = new_op.get("operation_id")
                return AdvanceResult.WAITING_FOR_OPERATION

            # Repair turn completed — verify action-specific expectations.
            post_check_error = self._verify_repair_outcome(state, ws, action, wo_id)
            if post_check_error:
                state.authoring_repair_attempts += 1
                if state.authoring_repair_attempts > state.max_authoring_repairs:
                    state.error = (
                        f"Architect repair outcome invalid ({post_check_error}) and repair attempts "
                        f"are exhausted; operator intervention required"
                    )
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.repair_context["post_check_error"] = post_check_error
                state.architect_repair_operation_id = None
                new_op = self._dispatch_repair_turn(state, ws)
                if new_op is None:
                    state.error = f"Architect repair turn could not be re-dispatched: {post_check_error}"
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.architect_repair_operation_id = new_op.get("operation_id")
                return AdvanceResult.WAITING_FOR_OPERATION

            # Revalidate the complete artifact set with the readiness gate.
            self._discover_worker_wos(state)
            gate_result = self._run_readiness_gate(state, ws)
            if not gate_result.ready:
                issue_summary = "; ".join(
                    f"{issue.code}: {issue.message}" for issue in gate_result.issues[:5]
                )
                self._archive_rejected_artifacts(state, ws, gate_result.issues)
                state.authoring_repair_attempts += 1
                if state.authoring_repair_attempts > state.max_authoring_repairs:
                    state.error = (
                        f"Authoring readiness gate still failing after repair "
                        f"({issue_summary}); repair attempts exhausted, operator intervention required"
                    )
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.readiness_issues = [i.to_dict() for i in gate_result.issues]
                state.architect_repair_operation_id = None
                new_op = self._dispatch_repair_turn(state, ws)
                if new_op is None:
                    state.error = f"Readiness still failing ({issue_summary}) and repair could not be re-dispatched"
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.architect_repair_operation_id = new_op.get("operation_id")
                return AdvanceResult.WAITING_FOR_OPERATION

            # Repair succeeded — publish the revision and route by repair source.
            state.published_wo_ids = sorted(
                w for w in state.worker_wo_ids if w not in state.superseded_wo_ids
            )
            state.readiness_issues = []
            state.error = None
            state.architect_repair_operation_id = None
            if source == "recovery" and wo_id:
                action = str(action or "")
                if action == "split_work_order":
                    # The blocked WO is superseded by its children; they join the
                    # normal execution loop instead of re-dispatching the original.
                    # The transitional repair-authorization contract is retired
                    # with the superseded work order (archived for audit).
                    self._archive_recovery_artifact(
                        ws, state.run_id, f".sync/contracts/{wo_id}.yaml"
                    )
                    state.repair_context = {}
                    self._clear_worker_blocker(state, ws, str(wo_id), redispatch=False)
                    self._transition(state, Phase.EXECUTING)
                    return AdvanceResult.WAITING_FOR_OPERATION
                if action == "create_dependency_work_order":
                    for dep in state.repair_context.get("replacement_work_orders") or []:
                        self._add_work_order_dependency(ws, str(wo_id), str(dep))
                    state.repair_context = {}
                    return self._clear_worker_blocker(state, ws, str(wo_id), redispatch=True)
                state.repair_context = {}
                return self._clear_worker_blocker(state, ws, str(wo_id), redispatch=True)
            state.repair_context = {}
            self._transition(state, Phase.DISPATCHING)
            return AdvanceResult.TRANSITIONED

        # Repair operation lost (crash/resume) — re-dispatch from stored evidence.
        ws = Path(state.workspace)
        new_op = self._dispatch_repair_turn(state, ws)
        if new_op is None:
            state.error = "Architect repair operation was lost and could not be re-dispatched"
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED
        state.architect_repair_operation_id = new_op.get("operation_id")
        return AdvanceResult.WAITING_FOR_OPERATION

    def _advance_recovery_decision(self, state: RunState) -> AdvanceResult:
        """ARCHITECT_RECOVERY_DECISION: validate and apply the Architect's decision.

        The supervisor validates the machine-readable decision against
        schemas/recovery-decision.schema.json and enforces policy; it never
        infers a decision on the Architect's behalf.
        """
        ws = Path(state.workspace)
        wo_id = state.recovery_wo_id

        if state.recovery_decision_operation_id:
            op = self._get_operation(state.recovery_decision_operation_id)
            if op is None:
                state.recovery_decision_operation_id = None
                return self._advance_recovery_decision(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION

            result = op.get("result", {}) or {}
            op_failed = status in ("FAILED", "BLOCKED") or (
                isinstance(result, dict)
                and str(result.get("status", "")).lower() in ("blocked", "failed")
            )

            decision, decision_error = self._load_recovery_decision(
                ws, str(wo_id), result if isinstance(result, dict) else {}
            )

            if op_failed:
                decision, decision_error = None, f"recovery decision turn failed ({status})"

            if decision_error is None:
                advance, apply_error = self._apply_recovery_decision(
                    state, ws, str(wo_id), decision or {}
                )
                if apply_error is None:
                    return advance if advance is not None else AdvanceResult.WAITING_FOR_OPERATION
                decision_error = apply_error

            # Invalid/failed decision — bounded re-dispatch with corrective feedback.
            attempts = state.recovery_attempts.get(str(wo_id), 0)
            if attempts >= state.max_retries:
                state.error = (
                    f"Architect recovery decision for {wo_id} could not be obtained "
                    f"({decision_error}); recovery attempts exhausted with evidence retained"
                )
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED
            state.recovery_attempts[str(wo_id)] = attempts + 1
            state.recovery_decision_operation_id = None
            new_op = self._dispatch_recovery_decision(state, ws, str(wo_id), decision_error)
            if new_op is None:
                state.error = (
                    f"Architect recovery decision for {wo_id} failed ({decision_error}) "
                    f"and the decision turn could not be re-dispatched"
                )
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED
            state.recovery_decision_operation_id = new_op.get("operation_id")
            return AdvanceResult.WAITING_FOR_OPERATION

        # Decision operation lost (crash/resume) — re-dispatch from stored evidence.
        if wo_id:
            new_op = self._dispatch_recovery_decision(state, ws, str(wo_id), None)
            if new_op is not None:
                state.recovery_decision_operation_id = new_op.get("operation_id")
                return AdvanceResult.WAITING_FOR_OPERATION
        state.error = "Recovery decision operation was lost and could not be re-dispatched"
        self._transition(state, Phase.BLOCKED)
        return AdvanceResult.BLOCKED

    def _advance_dispatching(self, state: RunState) -> AdvanceResult:
        """DISPATCHING: dispatch eligible worker WOs, then transition to EXECUTING.

        Only work orders belonging to the latest publishable authoring revision
        are dispatchable (atomic publication); superseded WOs are never sent.
        """
        ws = Path(state.workspace)
        dispatched_any = False

        if not state.worker_wo_ids:
            self._discover_worker_wos(state)

        published = set(state.published_wo_ids)
        superseded = set(state.superseded_wo_ids)

        # 1. Discover all eligible work orders whose dependencies are satisfied
        eligible_wos = []
        for wo_id in state.worker_wo_ids:
            if wo_id in superseded:
                continue
            if published and wo_id not in published:
                continue  # pending authoring revision — not publishable yet
            if wo_id == state.gitops_wo_id or self._role_for_agent(self._agent_for_wo(wo_id, ws)) == "gitops":
                if not state.gitops_wo_id:
                    state.gitops_wo_id = wo_id
                continue
            if wo_id in state.completed_wo_ids or wo_id in state.failed_wo_ids:
                continue
            if not self._dependencies_met(state, wo_id, ws):
                continue
            if self._is_wo_running(state, wo_id):
                dispatched_any = True
                continue
            eligible_wos.append(wo_id)

        # 2. S6 Concurrency: When multiple sibling work orders are eligible in this pass,
        # open a parent batch operation so SessionManager.begin_operation does not force them sequential.
        parent_op_id = state.batch_operation_id
        if len(eligible_wos) > 1 and not parent_op_id and hasattr(self.manager, "begin_operation"):
            try:
                _, parent_op_id = self.manager.begin_operation(
                    state.session_id,
                    "parallel_dispatch",
                    {"work_orders": eligible_wos},
                    role="supervisor",
                )
                state.batch_operation_id = parent_op_id
            except Exception:
                parent_op_id = None

        # 3. Dispatch each eligible work order (sharing parent_op_id if batch)
        for wo_id in eligible_wos:
            agent = self._agent_for_wo(wo_id, ws)
            try:
                self.manager.start_turn(
                    state.session_id,
                    self._worker_dispatch_prompt(state, wo_id, ws),
                    role=self._role_for_agent(agent),
                    agent_id=agent,
                    work_order_id=wo_id,
                    parent_operation_id=parent_op_id,
                )
                dispatched_any = True
            except Exception as exc:
                # D024 gate or other pre-check might block — record and continue
                if wo_id not in state.blocked_wo_ids:
                    state.blocked_wo_ids.append(wo_id)
                state.error = f"Dispatch failed for {wo_id}: {exc}"

        self._transition(state, Phase.EXECUTING)
        if dispatched_any:
            return AdvanceResult.WAITING_FOR_OPERATION
        if state.blocked_wo_ids and not state.completed_wo_ids:
            state.error = f"All worker WOs are blocked: {state.blocked_wo_ids}"
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED
        return AdvanceResult.TRANSITIONED

    def _advance_executing(self, state: RunState) -> AdvanceResult:
        """EXECUTING: monitor worker operations and QA flow."""
        ws = Path(state.workspace)
        all_done = True

        if state.dependency_escalation_op_id:
            escalation = self._poll_dependency_escalation(state, ws)
            if escalation is not None:
                return escalation

        if not state.worker_wo_ids:
            self._discover_worker_wos(state)

        published = set(state.published_wo_ids)
        superseded = set(state.superseded_wo_ids)

        for wo_id in state.worker_wo_ids:
            if wo_id in superseded:
                continue
            if published and wo_id not in published:
                continue  # pending authoring revision — not publishable yet
            if wo_id == state.gitops_wo_id or self._role_for_agent(self._agent_for_wo(wo_id, ws)) == "gitops":
                if not state.gitops_wo_id:
                    state.gitops_wo_id = wo_id
                continue
            if wo_id in state.completed_wo_ids or wo_id in state.failed_wo_ids:
                continue

            # Check if this WO's operation has completed
            op = self._find_latest_wo_operation(state, wo_id)
            if op is None:
                # Not dispatched yet — check deps and dispatch
                if self._dependencies_met(state, wo_id, ws):
                    if wo_id not in state.blocked_wo_ids:
                        agent = self._agent_for_wo(wo_id, ws)
                        try:
                            self.manager.start_turn(
                                state.session_id,
                                self._worker_dispatch_prompt(state, wo_id, ws),
                                role=self._role_for_agent(agent),
                                agent_id=agent,
                                work_order_id=wo_id,
                            )
                        except OperationContentionError:
                            all_done = False
                            continue
                        except Exception:
                            state.blocked_wo_ids.append(wo_id)
                all_done = False
                continue

            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                all_done = False
                continue

            result = op.get("result", {}) or {}
            run_status = str(result.get("status", "")).lower()

            if status == "BLOCKED" or run_status == "blocked":
                outcome = self._handle_blocked_wo(state, ws, wo_id, op, result)
                if outcome is not None:
                    return outcome
                all_done = False  # retry turn dispatched
                continue

            if status == "FAILED":
                if self._retry_worker(state, ws, wo_id, lambda n, a: f"Retry work order {wo_id} (attempt {n})"):
                    all_done = False
                continue

            # Verify declared deliverable exists on disk (S3)
            if not self._check_deliverable_exists(wo_id, ws):
                def _missing_prompt(n: int, agent: str) -> str:
                    prompt = f"Retry work order {wo_id}: declared deliverable was not found on disk (attempt {n})"
                    if agent == "gemma":
                        deliv_path = self._get_wo_deliverable_path(wo_id, ws) or "the declared test suite"
                        prompt += (
                            f". You MUST author the test suite '{deliv_path}' with write_file and "
                            f"execute it end-to-end with the run_tests tool (pytest) before completing"
                        )
                    return prompt

                if self._retry_worker(state, ws, wo_id, _missing_prompt):
                    all_done = False
                continue

            # Completed with deliverable on disk — QA work orders must also show
            # real execution evidence (authored suite + a recorded test run).
            if self._agent_for_wo(wo_id, ws) == "gemma":
                gap = self._qa_execution_evidence_gap(wo_id, op, ws)
                if gap:
                    handled = self._handle_qa_evidence_gap(state, ws, wo_id, op, gap)
                    if handled is not None:
                        return handled

            # Completed with deliverable on disk — check QA verdict
            if self._check_qa_verdict(wo_id, ws) == "NEEDS_CHANGES":
                def _rework_prompt(n: int, agent: str) -> str:
                    feedback = self._get_qa_feedback(wo_id, ws)
                    head = f"Rework work order {wo_id} after QA feedback (attempt {n})"
                    return f"{head}:\n{feedback}" if feedback else head

                if self._retry_worker(state, ws, wo_id, _rework_prompt):
                    all_done = False
                continue

            # APPROVED, or no verdict yet: a deliverable verified on disk completes
            # the work order (code, doc, config, research and QA alike).
            if wo_id not in state.completed_wo_ids:
                state.completed_wo_ids.append(wo_id)
                self._mark_wo_status_on_disk(ws, wo_id, "COMPLETED")

        # Check for fatal failures among worker WOs
        non_gitops_failed = [
            wid for wid in state.failed_wo_ids
            if wid != state.gitops_wo_id
        ]
        if non_gitops_failed:
            self._close_batch_operation(state, "FAILED")
            state.error = f"Work orders failed after max retries: {non_gitops_failed}"
            self._transition(state, Phase.FAILED)
            return AdvanceResult.FAILED

        if all_done:
            self._close_batch_operation(state, "COMPLETED")
            self._transition(state, Phase.INTEGRATION_REVIEW)
            return AdvanceResult.TRANSITIONED

        return AdvanceResult.WAITING_FOR_OPERATION

    def _poll_dependency_escalation(self, state: RunState, ws: Path) -> AdvanceResult | None:
        """Resolve an active dependency-escalation turn; None means keep executing."""
        esc_op = self._get_operation(state.dependency_escalation_op_id)
        if esc_op is None:
            state.dependency_escalation_op_id = None
            state.dependency_escalation_wo_id = None
            return None
        esc_status = str(esc_op.get("status", "")).upper()
        if esc_status not in _OPERATION_TERMINAL:
            return AdvanceResult.WAITING_FOR_OPERATION

        esc_result = esc_op.get("result", {}) or {}
        esc_run_status = str(esc_result.get("status", "")).lower()
        target_wo = state.dependency_escalation_wo_id
        state.dependency_escalation_op_id = None
        state.dependency_escalation_wo_id = None

        if esc_status == "COMPLETED" or esc_run_status == "completed":
            if target_wo:
                if target_wo in state.blocked_wo_ids:
                    state.blocked_wo_ids.remove(target_wo)
                self._mark_wo_status_on_disk(ws, target_wo, "ACTIVE", clear_error=True)
            state.error = None
        else:
            err_detail = esc_result.get("error") or esc_result.get("reason") or "turn failed"
            state.error = f"Architecture escalation failed to resolve dependency for {target_wo}: {err_detail}"
            if target_wo and target_wo not in state.blocked_wo_ids:
                state.blocked_wo_ids.append(target_wo)
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED
        return None

    def _handle_blocked_wo(
        self, state: RunState, ws: Path, wo_id: str, op: dict[str, Any], result: dict[str, Any],
    ) -> AdvanceResult | None:
        """Route a BLOCKED worker operation.

        Returns the AdvanceResult to hand back to the caller, or None when a
        transient-failure retry turn was dispatched and execution should continue.
        """
        blockers = result.get("blockers") or []
        blocker_text = " ".join(str(b) for b in blockers) if isinstance(blockers, list) else str(blockers)
        summary_text = str(result.get("summary") or "")
        raw_error = str(result.get("error") or "")
        raw_reason = str(result.get("reason") or "")

        # Durable evidence packet from the harness contract gate, if present.
        evidence = result.get("failure") if isinstance(result.get("failure"), dict) else None
        if evidence and evidence.get("failure_code"):
            packet = dict(evidence)
            packet.setdefault("work_order_id", wo_id)
            packet.setdefault("operation_id", op.get("operation_id"))
            state.worker_blockers[wo_id] = packet

        # One canonical message; the raw fields stay nested in the
        # evidence packet and operation result instead of being joined
        # into repeated display strings.
        evidence_msg = str(evidence.get("canonical_message")) if evidence else ""
        detailed_parts = [p for p in (raw_error, raw_reason, blocker_text, summary_text) if p and p.strip().lower() != "blocked"]
        err_msg = (
            evidence_msg
            or (detailed_parts[0] if detailed_parts else "")
            or raw_error or raw_reason or "governance gate blocked"
        )

        # Verification-dimension failures (outcome_verified / scope_verified)
        # block without a contract-gate evidence packet.  Synthesize one so the
        # classification below treats them as bounded transient failures and
        # escalates to the Architect recovery decision on budget exhaustion
        # instead of hard-blocking the whole run.
        if evidence is None and "verification gate failed" in err_msg.lower():
            evidence = {
                "failure_code": "OUTCOME_NOT_VERIFIED",
                "work_order_id": wo_id,
                "operation_id": op.get("operation_id"),
                "canonical_message": err_msg,
                "scope_evidence": result.get("scope_evidence") or {},
            }
            result["failure"] = evidence
            state.worker_blockers[wo_id] = dict(evidence)

        # Route verified governance failures to an Architect recovery
        # decision — never to an automatic same-contract worker retry.
        # Transient failures keep the bounded auto-retry path until the
        # retry budget is exhausted, then also escalate to the Architect.
        failure_code = str(evidence.get("failure_code")) if evidence else ""
        transient_exhausted = (
            failure_code in TRANSIENT_RETRYABLE_CODES
            and state.retry_counts.get(wo_id, 0) >= state.max_retries
        )
        if failure_code in GOVERNANCE_FAILURE_CODES or transient_exhausted:
            attempts = state.recovery_attempts.get(wo_id, 0)
            if attempts >= state.max_retries:
                state.error = (
                    f"Work order {wo_id} blocked: {err_msg} — recovery decision "
                    f"attempts exhausted ({attempts}/{state.max_retries})"
                )
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED
            state.recovery_attempts[wo_id] = attempts + 1
            state.recovery_wo_id = wo_id
            recovery_op = self._dispatch_recovery_decision(state, ws, wo_id)
            if recovery_op is not None:
                state.recovery_decision_operation_id = recovery_op.get("operation_id")
                self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
                return AdvanceResult.WAITING_FOR_OPERATION
            state.error = (
                f"Work order {wo_id} blocked: {err_msg} — recovery decision turn "
                f"could not be dispatched"
            )
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED

        # Check if this blockage is caused by an undeclared third-party dependency
        dep_info = self._parse_undeclared_dependency_error(err_msg, result)
        if dep_info:
            esc_retries = state.dependency_escalation_retries.get(wo_id, 0)
            if esc_retries < state.max_retries:
                state.dependency_escalation_retries[wo_id] = esc_retries + 1

                # Ignore the blocked worker operation so it won't re-block the supervisor
                op_id = op.get("operation_id")
                if op_id and op_id not in state.ignored_operation_ids:
                    state.ignored_operation_ids.append(op_id)

                esc_op = self._dispatch_dependency_escalation(state, wo_id, dep_info, ws)
                if esc_op:
                    state.dependency_escalation_op_id = esc_op.get("operation_id")
                    state.dependency_escalation_wo_id = wo_id
                    return AdvanceResult.WAITING_FOR_OPERATION

        # A turn that declared writes but produced none is a lazy model turn:
        # bounded retry with an explicit write instruction instead of a
        # terminal block. (The block's scope_evidence carries declared vs
        # observed; observed == [] means no tool was ever invoked.)
        scope_evidence = result.get("scope_evidence") if isinstance(result.get("scope_evidence"), dict) else None
        if scope_evidence and scope_evidence.get("observed") == []:
            retries = state.retry_counts.get(wo_id, 0)
            if retries < state.max_retries:
                state.retry_counts[wo_id] = retries + 1
                op_id = op.get("operation_id")
                if op_id and op_id not in state.ignored_operation_ids:
                    state.ignored_operation_ids.append(str(op_id))
                if wo_id in state.blocked_wo_ids:
                    state.blocked_wo_ids.remove(wo_id)
                self._mark_wo_status_on_disk(ws, wo_id, "ACTIVE", clear_error=True)
                state.error = None
                agent = self._agent_for_wo(wo_id, ws)
                declared = list(scope_evidence.get("declared") or [])
                deliv_path = self._get_wo_deliverable_path(wo_id, ws) or (
                    declared[0] if declared else "the declared deliverable"
                )
                if agent == "gemma":
                    nudge = (
                        f"QA work order {wo_id} (attempt {retries + 2}): your previous turn "
                        f"declared '{deliv_path}' but invoked NO tools, so nothing was written. "
                        f"You MUST invoke the write_file tool to create '{deliv_path}' with the "
                        f"full test-suite content, then execute it end-to-end with the run_tests "
                        f"tool (pytest), run the run_security_scan tool over the deliverables, and "
                        f"only then return your final decision declaring the suite in modified_files."
                    )
                else:
                    nudge = (
                        f"Retry work order {wo_id} (attempt {retries + 2}): your previous turn "
                        f"declared {declared} but invoked NO tools, so nothing was written. You "
                        f"MUST invoke the write_file tool to create '{deliv_path}' with the full "
                        f"deliverable content, then return your final decision declaring it."
                    )
                try:
                    self.manager.start_turn(
                        state.session_id,
                        nudge,
                        role=self._role_for_agent(agent),
                        agent_id=agent,
                        work_order_id=wo_id,
                    )
                    return None  # retry dispatched — keep executing
                except Exception as e:
                    logger.error(f"Failed to nudge-retry {wo_id}: {e}", exc_info=True)

        # Check if this blockage is a transient outcome_verified, unwritten deliverable, or tool loop halt
        err_lower = err_msg.lower()
        is_transient_blockage = (
            "outcome_verified" in err_lower
            or "declared deliverable" in err_lower
            or "unfulfilled deliverable" in err_lower
            or "governed tool loop halted" in err_lower
            or "pathological loop" in err_lower
            or "tool loop exhausted" in err_lower
            or "consecutive tool failures" in err_lower
            or "timed out" in err_lower
        )
        if is_transient_blockage:
            retries = state.retry_counts.get(wo_id, 0)
            if retries < state.max_retries:
                state.retry_counts[wo_id] = retries + 1
                op_id = op.get("operation_id")
                if op_id and op_id not in state.ignored_operation_ids:
                    state.ignored_operation_ids.append(op_id)
                if wo_id in state.blocked_wo_ids:
                    state.blocked_wo_ids.remove(wo_id)
                self._mark_wo_status_on_disk(ws, wo_id, "ACTIVE", clear_error=True)
                state.error = None
                agent = self._agent_for_wo(wo_id, ws)
                deliv_path = self._get_wo_deliverable_path(wo_id, ws)
                target_str = f" '{deliv_path}'" if deliv_path else ""
                # Targeted feedback: the scope evidence tells the model exactly
                # which declarations were not written — a blind "author the
                # deliverable" retry converges poorly with small local models.
                evidence_lines = ""
                if isinstance(scope_evidence, dict):
                    declared_list = sorted(scope_evidence.get("declared") or [])
                    observed_list = sorted(scope_evidence.get("observed") or [])
                    mismatch_reason = scope_evidence.get("mismatch_reason")
                    evidence_lines = (
                        f" You declared these files: {declared_list}. "
                        f"You actually wrote: {observed_list}."
                    )
                    if mismatch_reason:
                        evidence_lines += f" Mismatch: {mismatch_reason}."
                try:
                    self.manager.start_turn(
                        state.session_id,
                        (
                            f"Retry work order {wo_id}: previous attempt halted ({err_msg})."
                            f"{evidence_lines} "
                            f"You MUST invoke write_file to author the deliverable{target_str} "
                            "at EXACTLY that path — no renamed or extra-nested folders — and "
                            "declare in modified_files ONLY files you actually wrote with "
                            f"write_file this turn (attempt {retries + 2})"
                        ),
                        role=self._role_for_agent(agent),
                        agent_id=agent,
                        work_order_id=wo_id,
                    )
                    return None
                except Exception as e:
                    logger.error(f"Failed to retry {wo_id}: {e}", exc_info=True)

        if wo_id not in state.blocked_wo_ids:
            state.blocked_wo_ids.append(wo_id)
        state.error = f"Work order {wo_id} blocked: {err_msg}"
        self._transition(state, Phase.BLOCKED)
        return AdvanceResult.BLOCKED

    def _retry_worker(
        self, state: RunState, ws: Path, wo_id: str, make_prompt: Callable[[int, str], str],
    ) -> bool:
        """Bounded worker retry. True when a retry was attempted (budget left).

        Budget exhaustion or a failed dispatch marks the work order failed.
        """
        retries = state.retry_counts.get(wo_id, 0)
        if retries >= state.max_retries:
            state.failed_wo_ids.append(wo_id)
            return False
        state.retry_counts[wo_id] = retries + 1
        agent = self._agent_for_wo(wo_id, ws)
        try:
            self.manager.start_turn(
                state.session_id,
                make_prompt(retries + 2, agent),
                role=self._role_for_agent(agent),
                agent_id=agent,
                work_order_id=wo_id,
            )
        except Exception:
            state.failed_wo_ids.append(wo_id)
        return True

    def _close_batch_operation(self, state: RunState, status: str) -> None:
        if state.batch_operation_id:
            if hasattr(self.manager, "complete_operation"):
                try:
                    self.manager.complete_operation(operation_id=state.batch_operation_id, status=status)
                except Exception:
                    pass
            state.batch_operation_id = None

    def _advance_integration_review(self, state: RunState) -> AdvanceResult:
        """INTEGRATION_REVIEW: dispatch Architecture with dedicated read-only scope to review deliverables."""
        ws = Path(state.workspace)
        if not state.integration_operation_id:
            adopted = self._adopt_running_integration_operation(state, state.integration_wo_id)
            if adopted is not None:
                return self._advance_integration_review(state)

        if state.integration_operation_id:
            if state.integration_operation_id in getattr(state, "ignored_operation_ids", []):
                state.integration_operation_id = None
                return self._advance_integration_review(state)
            op = self._get_operation(state.integration_operation_id)
            if op is None:
                state.integration_operation_id = None
                return self._advance_integration_review(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION

            op_result = op.get("result", {})
            result_status = str(op_result.get("status", "")).lower()

            if status == "BLOCKED" or result_status == "blocked":
                # A valid blocked review routes its blockers — with the
                # Architect's prescribed fixes when provided — back to the
                # implementation and QA workers for bounded rework; the review
                # re-runs after they complete. Terminal BLOCKED only when the
                # rework rounds are exhausted.
                blockers: list[str] = []
                remediations: dict[str, str] = {}
                detail_paths: list[str] = []
                for item in op_result.get("blocker_details") or []:
                    if not isinstance(item, dict):
                        continue
                    finding = str(item.get("finding", "")).strip()
                    if not finding:
                        continue
                    if finding not in blockers:
                        blockers.append(finding)
                    remediation = str(item.get("remediation", "")).strip()
                    if remediation:
                        remediations[finding] = remediation
                    if item.get("path"):
                        detail_paths.append(str(item["path"]))
                for raw in op_result.get("blockers") or []:
                    if str(raw) not in blockers:
                        blockers.append(str(raw))
                if not blockers and op_result.get("reason"):
                    blockers = [op_result["reason"]]
                state.integration_blockers = list(blockers)
                err_msg = (
                    "; ".join(blockers)
                    if blockers
                    else _strip_code_fences(str(op_result.get("summary", "Blockers reported during integration review")))
                )

                reworkable = self._reworkable_integration_wos(
                    state, ws, blockers + detail_paths
                )
                if state.integration_rework_rounds < state.max_retries and reworkable:
                    state.integration_rework_rounds += 1
                    if state.integration_operation_id and state.integration_operation_id not in state.ignored_operation_ids:
                        state.ignored_operation_ids.append(state.integration_operation_id)
                    state.integration_operation_id = None
                    # Per-blocker feedback with the Architect's prescribed fix.
                    feedback_lines = []
                    for b in blockers:
                        line = f"- Blocker: {b}"
                        if b in remediations:
                            line += f"\n  Prescribed fix: {remediations[b]}"
                        feedback_lines.append(line)
                    feedback = (
                        "\n".join(feedback_lines)
                        if feedback_lines
                        else "review could not verify the deliverables"
                    )
                    # S6 concurrency: multiple rework turns dispatch under a
                    # shared batch parent so they don't contend for the root
                    # operation slot.
                    parent_op_id = None
                    if len(reworkable) > 1 and hasattr(self.manager, "begin_operation"):
                        try:
                            _, parent_op_id = self.manager.begin_operation(
                                state.session_id,
                                "parallel_dispatch",
                                {"work_orders": reworkable, "rework_round": state.integration_rework_rounds},
                                role="supervisor",
                            )
                            state.batch_operation_id = parent_op_id
                        except Exception:
                            parent_op_id = None
                    for wo_id in reworkable:
                        self._unarchive_work_order(ws, wo_id)
                        if wo_id in state.completed_wo_ids:
                            state.completed_wo_ids.remove(wo_id)
                        agent = self._agent_for_wo(wo_id, ws)
                        try:
                            self.manager.start_turn(
                                state.session_id,
                                (
                                    f"Rework work order {wo_id} (integration review round "
                                    f"{state.integration_rework_rounds}/{state.max_retries}): the "
                                    f"integration review blocked release with these findings "
                                    f"and prescribed fixes:\n"
                                    f"{feedback}\n"
                                    f"Apply each prescribed fix to your deliverable so every "
                                    f"blocker is resolved, then resubmit."
                                ),
                                role=self._role_for_agent(agent),
                                agent_id=agent,
                                work_order_id=wo_id,
                                parent_operation_id=parent_op_id,
                            )
                        except Exception:
                            state.failed_wo_ids.append(wo_id)
                    state.error = None
                    self._transition(state, Phase.EXECUTING)
                    return AdvanceResult.WAITING_FOR_OPERATION

                state.error = f"Integration review blocked: {err_msg}"
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED

            if status == "FAILED":
                state.error = f"Integration review failed: {op.get('result', {}).get('error', 'unknown')}"
                self._transition(state, Phase.FAILED)
                return AdvanceResult.FAILED

            # Integration review completed cleanly
            self._transition(state, Phase.PRODUCT_READY)
            return AdvanceResult.TRANSITIONED

        if not state.integration_wo_id:
            from .authoring import get_next_work_order_int
            review_wo_id = f"WO-{get_next_work_order_int(ws):03d}"
            state.integration_wo_id = review_wo_id
        else:
            review_wo_id = state.integration_wo_id

        deliverables = self._collect_completed_deliverables(state, ws)
        self._synthesize_integration_review_artifacts(state, ws, review_wo_id, deliverables)
        review_prompt = self._integration_review_prompt(state, review_wo_id, deliverables)

        try:
            op = self.manager.start_turn(
                state.session_id,
                review_prompt,
                role="architecture",
                agent_id="claude",
                work_order_id=review_wo_id,
            )
            state.integration_operation_id = op.get("operation_id")
            state.contention_count = 0
            try:
                self.save_run_state(state, ws)
            except Exception:
                pass
            return AdvanceResult.WAITING_FOR_OPERATION
        except OperationContentionError:
            adopted = self._adopt_running_integration_operation(state, review_wo_id)
            if adopted is not None:
                state.contention_count = 0
                try:
                    self.save_run_state(state, ws)
                except Exception:
                    pass
                return AdvanceResult.WAITING_FOR_OPERATION
            raise

    def _adopt_running_integration_operation(
        self, state: RunState, fallback_wo_id: str | None,
    ) -> str | None:
        """Find a non-terminal architecture operation for the integration review.

        Crash-recovery and contention-fallback scan: returns the operation id
        (recorded on the run state) or None when nothing is running.
        """
        try:
            for op in reversed(self.manager.list_operations(state.session_id)):
                op_id = op.get("operation_id")
                if op_id and op_id in getattr(state, "ignored_operation_ids", []):
                    continue
                op_wo = op.get("work_order_id")
                op_role = str(op.get("role", "")).lower()
                if (op_wo == fallback_wo_id or op_role == "architecture") and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                    state.integration_operation_id = op_id
                    state.integration_wo_id = op_wo or fallback_wo_id
                    return op_id
        except Exception:
            pass
        return None

    def _collect_completed_deliverables(self, state: RunState, ws: Path) -> list[str]:
        """Collect all declared deliverable paths across completed work orders."""
        deliverables: list[str] = []
        for wo_id in state.completed_wo_ids:
            for sub in ("ACTIVE", "COMPLETED"):
                wo_file = ws / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
                if wo_file.is_file():
                    try:
                        wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                        if isinstance(wo_data, dict):
                            deliv = wo_data.get("deliverable")
                            if isinstance(deliv, dict) and deliv.get("path"):
                                p = str(deliv["path"]).replace("\\", "/").strip().lstrip("/")
                                if p and p not in deliverables:
                                    deliverables.append(p)
                    except Exception:
                        pass
                    break
        return deliverables

    def _synthesize_integration_review_artifacts(
        self, state: RunState, ws: Path, review_wo_id: str, deliverables: list[str],
    ) -> None:
        """Synthesize the dedicated read-only review work order, contract, and INDEX entry."""
        review_wo_record = {
            "id": review_wo_id,
            "type": "VALIDATION",
            "title": "Integration Review",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["claude"],
            "dependencies": list(state.completed_wo_ids),
            "deliverable": {
                "type": "doc",
                "description": "Integration review report and decision",
            },
            "description": f"Perform integration review of all completed deliverables for '{state.product_goal}'.",
            "created": _now(),
            "updated": _now(),
        }

        allow_rules: list[dict[str, Any]] = [
            {"module": "PLAN.md"},
            {"module": ".sync/work-orders/**"},
            {"module": ".sync/contracts/**"},
            {"module": ".sync/inbox/claude/**"},
        ]
        for d in deliverables:
            allow_rules.append({"module": d})
        allow_rules.extend([
            {"module": "*.html"},
            {"module": "*.css"},
            {"module": "*.js"},
            {"module": "templates/**"},
            {"module": "static/**"},
            {"module": "tests/**"},
            {"module": "test/**"},
        ])

        seen_mods = set()
        unique_allow: list[dict[str, Any]] = []
        for r in allow_rules:
            mod = r.get("module")
            if mod and mod not in seen_mods:
                seen_mods.add(mod)
                unique_allow.append(r)

        deny_rules = [
            {"module": ".git/**"},
            {"module": ".env"},
            {"module": ".env.*"},
            {"module": ".sync/runtime/**"},
            {"module": ".sync/knowledge/**"},
            {"module": ".sync/snapshots/**"},
            {"module": ".sync/outbox/**"},
            {"module": ".sync/agents/**"},
            {"module": ".sync/inbox/CEO/**"},
            {"module": "__pycache__/**"},
            {"module": ".venv/**"},
            {"module": "node_modules/**"},
        ]

        review_contract_record = {
            "schema_version": 1,
            "agent_id": "claude",
            "work_order": review_wo_id,
            "identity": {
                "role": "architecture",
                "reports_to": "ceo",
            },
            "scope": {
                "allow": unique_allow,
                "deny": deny_rules,
                "write": "read-only",
            },
            "budget": {
                "max_files_touched": 1,
                "max_tokens": 50000,
            },
        }

        # Write dedicated review artifacts to disk
        try:
            from validators.harness.authoring_gate import AuthoringGate
            gate = AuthoringGate(project_root=ws)
            wo_yaml = yaml.safe_dump(review_wo_record, sort_keys=False)
            contract_yaml = yaml.safe_dump(review_contract_record, sort_keys=False)
            rel_wo_path = f".sync/work-orders/ACTIVE/{review_wo_id}.yaml"
            rel_contract_path = f".sync/contracts/{review_wo_id}.yaml"
            gate.validate_artifact_content(rel_wo_path, wo_yaml, agent="architecture", project_root=ws)
            gate.validate_artifact_content(rel_contract_path, contract_yaml, agent="architecture", project_root=ws)

            wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{review_wo_id}.yaml"
            wo_file.parent.mkdir(parents=True, exist_ok=True)
            wo_file.write_text(wo_yaml, encoding="utf-8")

            contract_file = ws / ".sync" / "contracts" / f"{review_wo_id}.yaml"
            contract_file.parent.mkdir(parents=True, exist_ok=True)
            contract_file.write_text(contract_yaml, encoding="utf-8")

            # Update INDEX.yaml
            idx_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
            if idx_file.is_file():
                idx_data = yaml.safe_load(idx_file.read_text(encoding="utf-8"))
                if isinstance(idx_data, dict):
                    orders = idx_data.setdefault("orders", [])
                    if not any(o.get("id") == review_wo_id for o in orders if isinstance(o, dict)):
                        orders.append({
                            "id": review_wo_id,
                            "title": review_wo_record["title"],
                            "status": "ACTIVE",
                            "priority": "P0",
                            "dependencies": list(state.completed_wo_ids),
                            "assigned_agents": ["claude"],
                        })
                    idx_data["next_id"] = max(idx_data.get("next_id", 1), int(review_wo_id.replace("WO-", "")) + 1)
                    idx_file.write_text(yaml.safe_dump(idx_data, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    def _integration_review_prompt(
        self, state: RunState, review_wo_id: str, deliverables: list[str],
    ) -> str:
        """Explicit JSON-format review prompt (Requirement 2)."""
        deliv_lines = "\n".join(f"- {d}" for d in deliverables) if deliverables else "- (none declared)"
        completed_summary = ", ".join(state.completed_wo_ids)
        return (
            f"All implementation work orders have been completed and QA-approved: {completed_summary}.\n\n"
            "As Senior Architect, perform the final integration review:\n"
            "1. Read PLAN.md for the original product requirements.\n"
            f"2. Read each declared deliverable file to verify completeness and correctness:\n{deliv_lines}\n"
            "3. Verify that all components integrate properly and test coverage is satisfactory.\n\n"
            "OUTPUT FORMAT INSTRUCTIONS:\n"
            "Your output must be a single JSON object with these exact fields:\n"
            "If review passes:\n"
            "{\n"
            '  "status": "completed",\n'
            '  "summary": "Integration review passed: all deliverables verified against requirements.",\n'
            '  "report_markdown": "Integration review passed: all deliverables verified against requirements.",\n'
            '  "blockers": []\n'
            "}\n\n"
            "If changes are needed or deliverables cannot be verified:\n"
            "{\n"
            '  "status": "blocked",\n'
            '  "summary": "Unable to verify a deliverable.",\n'
            '  "report_markdown": "Detailed explanation of which deliverable could not be verified and why.",\n'
            '  "blockers": ["Read access to app/rate_limiter.py was denied."],\n'
            '  "blocker_details": [\n'
            '    {"finding": "Hardcoded secret key fallback in src/backend.py",\n'
            '     "remediation": "Replace with SECRET_KEY = os.environ[\'SECRET_KEY\'] so a missing variable fails fast",\n'
            '     "path": "src/backend.py"}\n'
            '  ]\n'
            "}\n\n"
            "CRITICAL RULES:\n"
            "- When status is 'blocked', 'blockers' MUST be a non-empty string array listing each blocker. "
            "Do NOT rely on prose in the summary to satisfy the schema.\n"
            "- When status is 'blocked', 'blocker_details' SHOULD accompany each blocker with a "
            "'remediation': the concrete fix the worker should apply (you are the prescribing "
            "architect — prescribe precisely; workers apply your prescription verbatim).\n"
            "- When status is 'completed', 'blockers' MUST be an empty array [].\n"
            "- Your contract scope is strictly read-only. Do NOT attempt to write or edit application or test files.\n\n"
            "SECURITY & QUALITY CHECKLIST — verify each item while reading the deliverables and list "
            "any unmet item as a blocker:\n"
            "- No hardcoded secrets and no hardcoded env-fallback defaults (e.g. SECRET_KEY = "
            "os.environ.get(..., 'literal') must fail fast instead).\n"
            "- Debug flags are disabled for production (debug=True / DEBUG = True must not appear).\n"
            "- Password verification does not re-derive hashes unnecessarily; salts are stored combined "
            "with the hash (single field), not as separate columns.\n"
            "- State-changing forms/endpoints are protected against CSRF, and authentication endpoints "
            "have rate limiting or lockout.\n"
            "- Client code calls the real API endpoints (no mock/placeholder flows left behind)."
        )

    def _reworkable_integration_wos(
        self, state: RunState, ws: Path, blockers: list[str],
    ) -> list[str]:
        """Implementation and QA work orders that rework should re-run.

        Planning, integration-review, and GitOps work orders are excluded.
        When the blockers reference deliverable paths, only the accused work
        orders (plus QA, which must re-verify after code changes) are
        selected; unmappable blockers fall back to all code-producing WOs.
        """
        excluded = {
            state.planning_wo_id,
            "WO-000",
            state.integration_wo_id,
            state.gitops_wo_id,
        }
        blocker_text = " ".join(str(b) for b in blockers).lower()
        code_wos: list[str] = []
        qa_wos: list[str] = []
        accused: list[str] = []
        for wo_id in state.completed_wo_ids:
            if wo_id in excluded:
                continue
            agent = self._agent_for_wo(wo_id, ws)
            if agent not in ("codex", "gemini", "gemma"):
                continue
            if agent == "gemma":
                qa_wos.append(wo_id)
                continue
            code_wos.append(wo_id)
            # Rework candidates are archived — resolve the deliverable from
            # ACTIVE or COMPLETED.
            deliv = ""
            for sub in ("ACTIVE", "COMPLETED"):
                wo_file = ws / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
                if not wo_file.is_file():
                    continue
                try:
                    wdata = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                    d = wdata.get("deliverable") if isinstance(wdata, dict) else None
                    if isinstance(d, dict) and d.get("path"):
                        deliv = str(d["path"]).replace("\\", "/").strip().lstrip("/").lower()
                except Exception:
                    pass
                break
            if not deliv:
                accused.append(wo_id)  # no mappable footprint — include defensively
                continue
            stem = Path(deliv).stem.lower()
            if (deliv in blocker_text) or (stem and stem in blocker_text):
                accused.append(wo_id)
        if accused:
            return accused + qa_wos
        return code_wos + qa_wos

    def _unarchive_work_order(self, ws: Path, wo_id: str) -> bool:
        """Return a completed work order to ACTIVE so rework can re-dispatch it."""
        active = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if active.is_file():
            return self._mark_wo_status_on_disk(ws, wo_id, "ACTIVE", clear_error=True)
        completed = ws / ".sync" / "work-orders" / "COMPLETED" / f"{wo_id}.yaml"
        if not completed.is_file():
            return False
        try:
            data = yaml.safe_load(completed.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            data["status"] = "ACTIVE"
            data["updated"] = _now()
            data.pop("error", None)
            data.pop("blocked_reason", None)
            active.parent.mkdir(parents=True, exist_ok=True)
            active.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            completed.unlink()
            return True
        except Exception:
            return False

    def _advance_product_ready(self, state: RunState) -> AdvanceResult:
        """PRODUCT_READY: discover or create GitOps WO and dispatch."""
        ws = Path(state.workspace)

        # Find the GitOps WO
        gitops_wo = self._find_gitops_wo(ws, state)
        if gitops_wo:
            state.gitops_wo_id = gitops_wo
            self._transition(state, Phase.GITOPS)
            return AdvanceResult.TRANSITIONED

        # No GitOps WO exists — architecture should have created one.
        # Transition anyway and let GITOPS handle dispatching without a WO.
        self._transition(state, Phase.GITOPS)
        return AdvanceResult.TRANSITIONED

    def _advance_gitops(self, state: RunState) -> AdvanceResult:
        """GITOPS: dispatch GitOps release turn and wait for completion."""
        if state.gitops_operation_id:
            op = self._get_operation(state.gitops_operation_id)
            if op is None:
                state.gitops_operation_id = None
                return self._advance_gitops(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION

            op_result = op.get("result", {}) or {}
            result_status = str(op_result.get("status", "")).lower()

            if status == "BLOCKED" or result_status == "blocked":
                blockers = op_result.get("blockers") or []
                if not blockers and op_result.get("reason"):
                    blockers = [op_result["reason"]]
                err_msg = "; ".join(str(b) for b in blockers) if blockers else op_result.get("error", "GitOps turn blocked")

                # Budget-class blocks are self-inflicted: the release turn is
                # mandated to write the VERSION.md/CHANGELOG.md pair, so the
                # supervisor raises the contract budget to cover the observed
                # footprint and retries (bounded) instead of dead-ending.
                failure = op_result.get("failure") if isinstance(op_result.get("failure"), dict) else None
                failure_code = str(failure.get("failure_code") or "") if failure else ""
                wo_id = state.gitops_wo_id or "WO-GITOPS"
                observed_count = int(failure.get("observed_file_count") or 0) if failure else 0
                if failure_code == "CONTRACT_FILE_BUDGET_EXCEEDED":
                    retries = state.retry_counts.get(wo_id, 0)
                    if retries < state.max_retries:
                        state.retry_counts[wo_id] = retries + 1
                        contract_file = Path(state.workspace) / ".sync" / "contracts" / f"{state.gitops_wo_id}.yaml"
                        if contract_file.is_file():
                            try:
                                c_data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                                if isinstance(c_data, dict):
                                    budget = c_data.setdefault("budget", {})
                                    needed = max(2, observed_count)
                                    current = budget.get("max_files_touched")
                                    if not isinstance(current, int) or current < needed:
                                        budget["max_files_touched"] = needed
                                        contract_file.write_text(
                                            yaml.safe_dump(c_data, sort_keys=False), encoding="utf-8"
                                        )
                            except Exception:
                                pass
                        state.gitops_operation_id = None
                        state.error = None
                        return self._advance_gitops(state)
                    state.error = (
                        f"GitOps turn blocked: {err_msg} — budget retries exhausted "
                        f"({retries}/{state.max_retries})"
                    )
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED

                state.error = f"GitOps turn blocked: {err_msg}"
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED

            if status == "FAILED" or result_status == "failed":
                wo_id = state.gitops_wo_id or "WO-GITOPS"
                retries = state.retry_counts.get(wo_id, 0)
                if retries < state.max_retries:
                    state.retry_counts[wo_id] = retries + 1
                    state.gitops_operation_id = None
                    return self._advance_gitops(state)
                state.error = f"GitOps turn failed: {op_result.get('error', op.get('error', 'unknown'))}"
                self._transition(state, Phase.FAILED)
                return AdvanceResult.FAILED

            # GitOps completed — if workspace has a git repository, perform governed release commit
            ws = Path(state.workspace)
            # Before committing, archive completed work orders to .sync/work-orders/COMPLETED/
            to_archive = list(state.completed_wo_ids)
            if state.integration_wo_id and state.integration_wo_id not in to_archive:
                to_archive.append(state.integration_wo_id)
            if state.gitops_wo_id and state.gitops_wo_id not in to_archive:
                to_archive.append(state.gitops_wo_id)
            try:
                archive_completed_work_orders(ws, to_archive)
            except Exception:
                pass
            if (ws / ".git").is_dir():
                try:
                    sha = create_gitops_commit(
                        workspace=ws,
                        work_order_id=state.gitops_wo_id or "WO-GITOPS",
                        summary=f"feat(release): release product goal '{state.product_goal}'",
                        released_by="local-llm",
                        approved_by="gemma",
                        architect="claude",
                        target_work_orders=state.completed_wo_ids,
                    )
                    state.release_commit_sha = sha
                except Exception:
                    pass
            self._transition(state, Phase.COMPLETE)
            return AdvanceResult.COMPLETE

        # Dispatch GitOps turn
        try:
            ws = Path(state.workspace)
            if state.gitops_wo_id:
                # Ensure the GitOps contract allows reading deliverables and
                # covers the release-documentation pair (VERSION.md +
                # CHANGELOG.md) the release turn is mandated to write.
                contract_file = ws / ".sync" / "contracts" / f"{state.gitops_wo_id}.yaml"
                if contract_file.is_file():
                    try:
                        c_data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                        if isinstance(c_data, dict) and "scope" in c_data:
                            scope_dict = c_data.setdefault("scope", {})
                            allow_list = scope_dict.setdefault("allow", [])
                            existing_mods = {r.get("module") for r in allow_list if isinstance(r, dict)}
                            needed = ["tests/**", "test/**", "src/**", "app/**", "*.py"]
                            updated = False
                            for n in needed:
                                if n not in existing_mods:
                                    allow_list.append({"module": n})
                                    updated = True
                            budget = c_data.setdefault("budget", {})
                            current_budget = budget.get("max_files_touched")
                            if not isinstance(current_budget, int) or current_budget < 2:
                                budget["max_files_touched"] = 2
                                updated = True
                            if updated:
                                contract_file.write_text(yaml.safe_dump(c_data, sort_keys=False), encoding="utf-8")
                    except Exception:
                        pass

            gitops_prompt = (
                "All implementation work orders are complete, QA-approved, and integration-reviewed.\n\n"
                "As GitOps Release Lead, finalize the release documentation:\n"
                "1. Write VERSION.md with version '0.1.0'.\n"
                "2. Write CHANGELOG.md summarizing the completed deliverables and the release.\n"
                "3. Return the final HarnessDecision JSON with status 'completed'. Do not run git commands; the supervisor creates the release commit automatically."
            )
            params: dict[str, Any] = {
                "role": "gitops",
                "agent_id": "local-llm",
            }
            if state.gitops_wo_id:
                params["work_order_id"] = state.gitops_wo_id
            op = self.manager.start_turn(
                state.session_id,
                gitops_prompt,
                **params,
            )
            state.gitops_operation_id = op.get("operation_id")
            return AdvanceResult.WAITING_FOR_OPERATION
        except Exception as exc:
            # D024 gate may block GitOps — this is expected if QA verdicts
            # aren't present in the correct inbox location
            state.error = f"GitOps dispatch blocked: {exc}"
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED

    # ── Authoring readiness & architect recovery helpers ──────────────

    def _worker_dispatch_prompt(self, state: RunState, wo_id: str, ws: Path) -> str:
        """Build the worker dispatch prompt.

        QA workers get explicit author→execute→verdict instructions so a QA
        turn cannot be satisfied by a bare sign-off: the declared test suite
        must be written and actually executed end-to-end.
        """
        agent = self._agent_for_wo(wo_id, ws)
        deliv_path = self._get_wo_deliverable_path(wo_id, ws)
        if agent == "gemma" and deliv_path:
            return (
                f"Execute work order {wo_id} as the QA worker.\n"
                f"1. Author the declared test suite '{deliv_path}' with write_file — thorough "
                f"end-to-end coverage of the deliverables produced by the other work orders.\n"
                f"2. Execute the suite end-to-end with the run_tests tool (pytest) and review the results.\n"
                f"3. Run the run_security_scan tool over the deliverables (src/) and triage the output; "
                f"report every unresolved finding as a blocker.\n"
                f"4. Do not modify application code; if the suite or the scan exposes defects, report them as blockers.\n"
                f"5. Write your QA verdict to .sync/inbox/claude/ and declare '{deliv_path}' in modified_files.\n"
                "Review against the security checklist: no hardcoded secrets or env-fallback defaults, "
                "debug flags disabled, authentication logic free of obvious flaws, and forms protected "
                "against CSRF/replay where applicable."
            )
        return f"Execute work order {wo_id}"

    def _qa_execution_evidence_gap(self, wo_id: str, op: dict[str, Any], ws: Path) -> str | None:
        """Detect a QA turn that completed without authoring/running its test suite.

        Enforcement only applies when the runner reported execution telemetry
        (commands_audit / tool_calls_audit); runners without telemetry keep
        legacy compatibility.
        """
        result = op.get("result") or {}
        if not isinstance(result, dict):
            return None
        if ("commands_audit" not in result) and ("tool_calls_audit" not in result):
            return None  # no execution telemetry reported — legacy provider
        deliv_path = self._get_wo_deliverable_path(wo_id, ws)
        if not deliv_path:
            return None  # sign-off style QA work order — nothing executable declared
        target = ws / deliv_path
        try:
            if not target.is_file() or target.stat().st_size == 0:
                return f"declared test suite '{deliv_path}' was not authored"
        except OSError:
            return f"declared test suite '{deliv_path}' was not authored"
        audit_entries = list(result.get("commands_audit") or []) + list(result.get("tool_calls_audit") or [])

        def _is_test_exec(entry: Any) -> bool:
            try:
                text = (
                    json.dumps(entry, default=str).lower()
                    if not isinstance(entry, str) else entry.lower()
                )
            except Exception:
                text = str(entry).lower()
            return (
                "run_tests" in text
                or "pytest" in text
                or ("run_command" in text and "test" in text)
            )

        if not any(_is_test_exec(e) for e in audit_entries):
            return (
                f"no test execution was recorded for '{deliv_path}' "
                f"(run_tests/pytest absent from the turn audit)"
            )

        # QA must also run the deterministic security scan — the report-class
        # findings (hardcoded secret fallbacks, debug flags) that a model
        # reviewer routinely misses.
        def _is_security_scan(entry: Any) -> bool:
            try:
                text = (
                    json.dumps(entry, default=str).lower()
                    if not isinstance(entry, str) else entry.lower()
                )
            except Exception:
                text = str(entry).lower()
            return (
                "run_security_scan" in text
                or "security_scan" in text
                or "bandit" in text
                or "semgrep" in text
                or "pip-audit" in text
            )

        if not any(_is_security_scan(e) for e in audit_entries):
            return (
                f"no security scan was recorded (run_security_scan absent from "
                f"the turn audit); run it over the deliverables and report any "
                f"finding as a blocker"
            )
        return None

    def _handle_qa_evidence_gap(
        self, state: RunState, ws: Path, wo_id: str, op: dict[str, Any], gap: str,
    ) -> AdvanceResult | None:
        """Bounded retry for a QA evidence gap; exhaustion routes to the Architect."""
        retries = state.retry_counts.get(wo_id, 0)
        if retries < state.max_retries:
            state.retry_counts[wo_id] = retries + 1
            op_id = op.get("operation_id")
            if op_id and op_id not in state.ignored_operation_ids:
                state.ignored_operation_ids.append(str(op_id))
            deliv_path = self._get_wo_deliverable_path(wo_id, ws) or "the declared test suite"
            try:
                self.manager.start_turn(
                    state.session_id,
                    (
                        f"QA work order {wo_id} (attempt {retries + 2}): {gap}. You MUST author "
                        f"the test suite '{deliv_path}' with write_file, execute it end-to-end "
                        f"with the run_tests tool (pytest), and run the run_security_scan tool "
                        f"over the deliverables before completing. Then write your QA verdict "
                        f"to .sync/inbox/claude/ and declare the suite in modified_files."
                    ),
                    role=self._role_for_agent("gemma"),
                    agent_id="gemma",
                    work_order_id=wo_id,
                )
                return AdvanceResult.WAITING_FOR_OPERATION
            except Exception as exc:
                logger.error(f"Failed to retry QA work order {wo_id}: {exc}", exc_info=True)
                state.failed_wo_ids.append(wo_id)
                return AdvanceResult.WAITING_FOR_OPERATION
        # Retries exhausted — route to the Architect with durable evidence.
        state.worker_blockers[wo_id] = {
            "failure_code": "QA_EVIDENCE_MISSING",
            "canonical_message": f"QA_EVIDENCE_MISSING: work order {wo_id}: {gap}",
            "raw_reason": gap,
            "work_order_id": wo_id,
            "operation_id": str(op.get("operation_id")),
            "observed_files": [],
            "contract_hash": self._sha256_file(ws / ".sync" / "contracts" / f"{wo_id}.yaml"),
            "contract_revision": state.contract_revisions.get(wo_id, 1),
        }
        attempts = state.recovery_attempts.get(wo_id, 0)
        if attempts >= state.max_retries:
            state.error = f"Work order {wo_id}: {gap} — recovery decision attempts exhausted"
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED
        state.recovery_attempts[wo_id] = attempts + 1
        state.recovery_wo_id = wo_id
        recovery_op = self._dispatch_recovery_decision(state, ws, wo_id)
        if recovery_op is None:
            state.error = f"Work order {wo_id}: {gap} — recovery decision turn could not be dispatched"
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED
        state.recovery_decision_operation_id = recovery_op.get("operation_id")
        self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
        return AdvanceResult.WAITING_FOR_OPERATION

    @staticmethod
    def _sha256_file(path: Path) -> str | None:
        import hashlib
        if not path.is_file():
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            return None

    def _run_readiness_gate(self, state: RunState, ws: Path) -> Any:
        """Compile the authored set, then run the readiness gate on the canonical result.

        The deterministic compiler runs first: it injects system-owned
        invariants (QA verdict channel, milestone identity) and routes
        non-normalizable intent straight to repair.  The readiness gate then
        validates the canonical set and remains authoritative.
        """
        from validators.harness.authoring_compiler import compile_authoring_artifacts
        from validators.harness.authoring_readiness import (
            AuthoringReadinessResult,
            load_plan_milestones,
            milestone_matches,
            validate_authoring_readiness,
        )
        plan_milestones = load_plan_milestones(ws)

        # Deterministic preflight/normalization pass (SYSTEM-NORMALIZABLE
        # fixes are applied in place; ARCHITECT-DECISION-REQUIRED findings
        # short-circuit into a targeted repair turn).
        compile_result = compile_authoring_artifacts(ws, plan_milestones)
        if compile_result.decision_required:
            result = AuthoringReadinessResult(
                ready=False,
                plan_id=state.plan_id or "",
                issues=tuple(compile_result.decision_required),
            )
            self._persist_readiness_report(state, ws, result)
            return result

        expected = plan_milestones
        if expected and state.milestone_exemptions:
            expected = [
                ref for ref in expected
                if not any(
                    milestone_matches(exempt, ref["title"])
                    or (ref["id"] and ref["id"].lower() == str(exempt).strip().lower())
                    for exempt in state.milestone_exemptions
                )
            ]
        result = validate_authoring_readiness(
            ws,
            plan_id=state.plan_id or "",
            expected_milestones=expected,
            planning_wo_id=state.planning_wo_id,
        )
        self._persist_readiness_report(state, ws, result)
        return result

    def _persist_readiness_report(self, state: RunState, ws: Path, result: Any) -> None:
        """Persist the readiness verdict for audit under the governed report path."""
        try:
            report_dir = ws / ".sync" / "reports" / "authoring" / state.run_id
            report_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "run_id": state.run_id,
                "plan_id": state.plan_id,
                "authoring_revision": state.authoring_revision,
                "phase": state.phase.value,
                "ready": bool(result.ready),
                "artifacts": list(result.artifact_paths),
                "issues": [i.to_dict() for i in result.issues],
                "evaluated_at": _now(),
            }
            (report_dir / f"rev{state.authoring_revision}-readiness.yaml").write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _archive_rejected_artifacts(
        self, state: RunState, ws: Path, issues: Any = None,
    ) -> list[str]:
        """Move failing governed artifacts to the audit path, preserving diagnostics.

        Only artifacts whose own content failed (parse/schema/budget/scope) are
        archived; relational findings (missing contract, dependency problems)
        stay in place so the repair turn can correct them surgically.
        Returns the list of archived artifact paths.
        """
        raw_issues: list[Any] = list(issues) if issues is not None else state.readiness_issues

        def _get(issue: Any, key: str) -> Any:
            if isinstance(issue, dict):
                return issue.get(key)
            return getattr(issue, key, None)

        content_failure_codes = {
            "BUDGET_MISSING", "BUDGET_INVALID", "BUDGET_UNDER_ESTIMATE",
            "DELIVERABLE_OUTSIDE_SCOPE", "OVERWRITE_CONFLICT",
        }
        archived: list[str] = []
        seen_paths: set[str] = set()
        for issue in raw_issues:
            category = str(_get(issue, "category") or "")
            code = str(_get(issue, "code") or "")
            rel = _get(issue, "artifact_path")
            if not rel or not isinstance(rel, str) or rel in seen_paths:
                continue
            if category not in ("yaml", "schema") and code not in content_failure_codes:
                continue
            if ".sync/work-orders/ACTIVE/" not in rel and ".sync/contracts/" not in rel:
                continue
            seen_paths.add(rel)
            source = ws / rel
            if not source.is_file():
                continue
            try:
                audit_dir = (
                    ws / ".sync" / "reports" / "authoring" / state.run_id
                    / f"rev{state.authoring_revision}"
                )
                audit_dir.mkdir(parents=True, exist_ok=True)
                target = audit_dir / Path(rel).name
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                source.unlink()
                archived.append(rel)
            except Exception:
                continue
        if archived:
            try:
                audit_dir = (
                    ws / ".sync" / "reports" / "authoring" / state.run_id
                    / f"rev{state.authoring_revision}"
                )
                (audit_dir / "rejected-artifacts.yaml").write_text(
                    yaml.safe_dump({
                        "archived_at": _now(),
                        "authoring_revision": state.authoring_revision,
                        "artifacts": archived,
                        "issues": [
                            i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in raw_issues
                        ],
                    }, sort_keys=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return archived

    def _enter_architect_repair(
        self,
        state: RunState,
        reason: str,
        issues: Any = None,
        archive_failed_artifacts: bool = False,
    ) -> AdvanceResult:
        """Transition to ARCHITECT_REPAIR and dispatch a bounded repair turn.

        At the retry limit the run transitions to terminal BLOCKED with the
        stored evidence packet instead of looping forever.
        """
        if state.authoring_repair_attempts >= state.max_authoring_repairs:
            state.error = (
                f"{reason} — architect repair attempts exhausted "
                f"({state.authoring_repair_attempts}/{state.max_authoring_repairs}); "
                f"operator intervention required"
            )
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED
        state.error = reason
        if issues is not None:
            state.readiness_issues = [
                i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in issues
            ]
        state.repair_context = {"source": "authoring"}
        ws = Path(state.workspace)
        self._transition(state, Phase.ARCHITECT_REPAIR)
        affected = self._archive_rejected_artifacts(state, ws) if archive_failed_artifacts else []
        state.repair_context["affected_artifacts"] = affected
        state.authoring_repair_attempts += 1
        op = self._dispatch_repair_turn(state, ws)
        if op is None:
            state.error = f"{reason} — failed to dispatch the Architect repair turn"
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED
        state.architect_repair_operation_id = op.get("operation_id")
        try:
            self.save_run_state(state, ws)
        except Exception:
            pass
        return AdvanceResult.WAITING_FOR_OPERATION

    def _dispatch_repair_turn(self, state: RunState, ws: Path) -> dict[str, Any] | None:
        """Dispatch the dedicated Architect repair turn with bounded diagnostics."""
        source = str(state.repair_context.get("source", "authoring"))
        if source == "recovery":
            prompt = self._recovery_repair_prompt(state)
        else:
            prompt = self._authoring_repair_prompt(state)
        return self._architect_turn(
            state, prompt, state.repair_context.get("work_order") or "WO-000", is_repair=True,
        )

    def _recovery_repair_prompt(self, state: RunState) -> str:
        """Bounded repair prompt for an approved recovery decision (amend/split/create)."""
        ctx = state.repair_context
        wo_id = ctx.get("work_order")
        action = ctx.get("action")
        decision_lines = yaml.safe_dump(ctx.get("decision") or {}, sort_keys=False).strip()
        child_ids = ctx.get("replacement_work_orders") or []
        children_lines = "\n".join(f"- {c}" for c in child_ids) if child_ids else "- (none declared)"
        post_check_error = ctx.get("post_check_error")
        return (
            f"ARCHITECT RECOVERY REPAIR for work order {wo_id} (approved action: {action}).\n\n"
            f"Recovery decision:\n{decision_lines}\n\n"
            f"Post-repair validation error: {post_check_error or 'none yet (first application pass)'}\n\n"
            "Apply the decision by authoring governed artifacts with write_file:\n"
            + (
                "- amend_contract: write the amended contract to .sync/contracts/<WO-ID>.yaml "
                "conforming to schemas/contract.schema.json. The budget must be explicitly sized "
                "from the task's real file footprint. A transitional repair contract is already in "
                "place at that path — overwrite it with the amended contract; do not delete it.\n"
                if action == "amend_contract" else ""
            )
            + (
                "- split_work_order: write each replacement child Work Order to "
                ".sync/work-orders/ACTIVE/<WO-ID>.yaml and its contract to .sync/contracts/<WO-ID>.yaml. "
                "Child 1 inherits the original dependencies; each following child depends on the "
                "previous one. Never assign an unbounded file budget — split further instead.\n"
                f"Replacement children:\n{children_lines}\n"
                if action == "split_work_order" else ""
            )
            + (
                "- create_dependency_work_order: write the new prerequisite Work Order and contract "
                "conforming to the schemas. The supervisor will rewire dependents afterwards.\n"
                f"New prerequisite work orders:\n{children_lines}\n"
                if action == "create_dependency_work_order" else ""
            )
            + "\nDo not modify unrelated work orders, contracts, or application code.\n"
            "Return the final HarnessDecision JSON with status 'completed' and modified_files "
            "listing every file you wrote."
        )

    def _authoring_repair_prompt(self, state: RunState) -> str:
        """Bounded authoring-repair prompt from the persisted readiness diagnostics.

        Every diagnostic carries its required_action, and the affected-artifact
        list is derived from ALL issue attachments — relational findings
        (milestone coverage, QA channel, test planning) name their artifacts
        here, so the prompt never claims "affected: none" while demanding fixes.
        """
        issues = [i for i in state.readiness_issues[:8] if isinstance(i, dict)]
        issue_lines = "\n".join(
            f"- [{i.get('code')}] {i.get('message')}"
            + (f"\n    required_action: {i.get('required_action')}" if i.get("required_action") else "")
            for i in issues
        )
        affected: list[str] = []
        for i in state.readiness_issues:
            if not isinstance(i, dict):
                continue
            paths = list(i.get("affected_artifacts") or [])
            if i.get("artifact_path"):
                paths.append(i["artifact_path"])
            for p in paths:
                if p and p not in affected:
                    affected.append(str(p))
        if affected:
            affected_intro = "Artifacts named by the diagnostics (correct these; do not touch others):"
            affected_lines = "\n".join(f"- {p}" for p in sorted(affected))
        else:
            affected_intro = "No individual artifact is named; re-author per the diagnostics above:"
            affected_lines = "- (the failure is artifact-set-wide, e.g. the plan or the full authored set)"
        return (
            f"ARCHITECT AUTHORING REPAIR (revision {state.authoring_revision}, "
            f"attempt {state.authoring_repair_attempts}/{state.max_authoring_repairs}).\n\n"
            f"The previous authoring attempt for plan '{state.plan_id or 'PLAN-001'}' was "
            f"rejected fail-closed and was NOT dispatched to any worker.\n"
            f"Failure: {state.error}\n\n"
            f"Readiness diagnostics (bounded):\n{issue_lines or '- (operation-level failure; no artifact diagnostics)'}\n\n"
            f"{affected_intro}\n{affected_lines}\n\n"
            "Correct each diagnostic by following its required_action:\n"
            "1. Re-write each affected Work Order YAML to .sync/work-orders/ACTIVE/<WO-ID>.yaml "
            "conforming to schemas/work-order.schema.json.\n"
            "2. Re-write each affected or missing Contract YAML to .sync/contracts/<WO-ID>.yaml "
            "conforming to schemas/contract.schema.json.\n"
            "3. Explicitly size every contract budget: declare implementation_estimate "
            "(expected_files, max_files_touched) in the work order and set the contract "
            "max_files_touched to cover it.\n"
            "4. Every code deliverable must declare a `test_plan` mapping it to test "
            "artifact(s) (e.g. tests/test_<stem>.py, or a consolidated suite listing "
            "the sources it covers).\n"
            "5. Return the final HarnessDecision JSON with status 'completed' and modified_files "
            "listing the files you wrote."
        )

    def _architect_turn(
        self, state: RunState, prompt: str, wo_id: str, **flags: Any,
    ) -> dict[str, Any] | None:
        """Start a governed Architect authoring turn; None when it cannot start."""
        try:
            return self.manager.start_turn(
                state.session_id,
                prompt,
                role="architecture",
                agent_id="claude",
                work_order_id=wo_id,
                is_authoring=True,
                **flags,
            )
        except Exception:
            return None

    def _verify_repair_outcome(
        self, state: RunState, ws: Path, action: Any, wo_id: Any,
    ) -> str | None:
        """Enforce action-specific expectations after a recovery repair turn."""
        if not action or not wo_id:
            return None  # plain authoring repair — readiness gate is the arbiter
        wo_id = str(wo_id)
        if action == "amend_contract":
            expected_revision = state.repair_context.get("decision", {}).get("contract_revision")
            current_hash = self._sha256_file(ws / ".sync" / "contracts" / f"{wo_id}.yaml")
            if current_hash is None:
                return f"amended contract for {wo_id} is missing"
            original_hash = (state.worker_blockers.get(wo_id) or {}).get("contract_hash")
            if original_hash and current_hash == original_hash:
                return (
                    f"amend_contract produced an unchanged contract for {wo_id}; "
                    f"a new contract revision (currently {expected_revision or 'N/A'}) is required"
                )
            return None
        if action in ("split_work_order", "create_dependency_work_order"):
            children = state.repair_context.get("replacement_work_orders") or []
            for child in children:
                child = str(child)
                if not (ws / ".sync" / "work-orders" / "ACTIVE" / f"{child}.yaml").is_file():
                    return f"replacement work order {child} was not authored"
                if not (ws / ".sync" / "contracts" / f"{child}.yaml").is_file():
                    return f"contract for replacement work order {child} is missing"
            return None
        return None

    def _ensure_recovery_decision_channel(self, ws: Path, wo_id: str) -> None:
        """Grant the recovery decision channel (.sync/decisions/**) on the contract.

        Deterministic supervisor bookkeeping so the Architect can persist the
        machine-readable decision file; never a worker-performed edit.
        """
        contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
        if not contract_file.is_file():
            return
        try:
            data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            scope = data.setdefault("scope", {})
            allow_list = scope.setdefault("allow", [])
            existing = {
                r.get("module") for r in allow_list if isinstance(r, dict)
            }
            needed = [".sync/decisions/**", ".sync/decisions/*"]
            updated = False
            for n in needed:
                if n not in existing:
                    allow_list.append({"module": n})
                    updated = True
            if updated:
                contract_file.write_text(
                    yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
                )
        except Exception:
            pass

    def _dispatch_recovery_decision(
        self, state: RunState, ws: Path, wo_id: str, corrective: str | None = None,
    ) -> dict[str, Any] | None:
        """Dispatch the Architect turn that produces the machine-readable recovery decision."""
        evidence = state.worker_blockers.get(wo_id, {})
        self._ensure_recovery_decision_channel(ws, wo_id)
        # The decision-channel grant is deterministic supervisor bookkeeping;
        # re-baseline the contract hash AFTER it so retry/amend verification
        # measures only Architect-made changes.
        if isinstance(evidence, dict) and evidence:
            fresh_hash = self._sha256_file(ws / ".sync" / "contracts" / f"{wo_id}.yaml")
            if fresh_hash:
                evidence["contract_hash"] = fresh_hash
        prompt = self._recovery_decision_prompt(state, wo_id, evidence, corrective)
        return self._architect_turn(state, prompt, wo_id, is_recovery_decision=True)

    def _recovery_decision_prompt(
        self, state: RunState, wo_id: str, evidence: dict[str, Any], corrective: str | None,
    ) -> str:
        """Bounded decision prompt with the evidence packet and permitted actions."""
        observed = [str(p) for p in (evidence.get("observed_files") or [])]
        preview = observed[:20]
        more = (
            f"\n  ... and {len(observed) - len(preview)} more (full list retained in the run evidence)"
            if len(observed) > len(preview) else ""
        )
        return (
            f"ARCHITECT RECOVERY DECISION required for work order {wo_id}.\n\n"
            "A governed worker execution was blocked by a verified governance failure. "
            "Bounded evidence packet:\n"
            f"- failure_code: {evidence.get('failure_code', 'UNKNOWN')}\n"
            f"- failure: {evidence.get('canonical_message') or evidence.get('raw_reason') or 'unspecified'}\n"
            f"- contract revision: {evidence.get('contract_revision', 1)} "
            f"(hash {str(evidence.get('contract_hash') or '')[:12]})\n"
            f"- file budget: {evidence.get('file_budget')} ; observed changed files: "
            f"{evidence.get('observed_file_count', len(observed))}\n"
            f"- observed files:\n" + "".join(f"  - {p}\n" for p in preview) + more + "\n\n"
            + (f"Your previous decision was rejected: {corrective}\n\n" if corrective else "")
            + "As Senior Architect you must decide how to recover:\n"
            "1. Write your decision as a JSON file to "
            f".sync/decisions/recovery/{wo_id}.decision.json conforming to "
            "schemas/recovery-decision.schema.json (fields: work_order, action, reason, plus "
            "failure_code/transient/replacement_work_orders/contract_revision as applicable).\n"
            "2. Permitted actions: retry_unchanged (ONLY for transient failures with an unchanged "
            "contract), amend_contract (set contract_revision to a new revision; the amended "
            "contract is authored in the follow-up repair turn), split_work_order (list "
            "replacement_work_orders child IDs), create_dependency_work_order (list new "
            "prerequisite WO IDs), escalate_human, terminal_block.\n"
            "3. Budget, scope, and schema failures must NOT use retry_unchanged.\n"
            "4. Declare the decision file path in modified_files and return the final "
            "HarnessDecision JSON with status 'completed'."
        )

    def _load_recovery_decision(
        self, ws: Path, wo_id: str, result: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Load and schema-validate the recovery decision (file first, summary fallback)."""
        import json as _json
        from jsonschema import Draft7Validator

        from validators.harness.authoring_gate import AuthoringGate

        schema = AuthoringGate(project_root=ws).get_schema("recovery-decision.schema.json")
        validator = Draft7Validator(schema) if schema else None

        def _validate(decision: dict[str, Any]) -> str | None:
            if validator is not None:
                errors = sorted(validator.iter_errors(decision), key=lambda e: str(e.path))
                if errors:
                    return "; ".join(
                        f"{'.'.join(str(p) for p in e.path) or 'root'}: {e.message}"
                        for e in errors[:3]
                    )
            if str(decision.get("work_order", "")) != wo_id:
                return f"decision work_order {decision.get('work_order')!r} does not match {wo_id}"
            return None

        decision_file = ws / ".sync" / "decisions" / "recovery" / f"{wo_id}.decision.json"
        if decision_file.is_file():
            try:
                data = _json.loads(decision_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    err = _validate(data)
                    if err is None:
                        return data, None
                    return None, f"recovery decision file rejected: {err}"
            except Exception as exc:
                return None, f"recovery decision file unreadable: {exc}"

        # Fallback: parse the decision from the turn's final summary JSON.
        summary = str(result.get("summary") or "")
        if summary.strip():
            candidate_text = summary.strip()
            if candidate_text.startswith("```"):
                candidate_text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", candidate_text).rstrip("`").strip()
            parsed: dict[str, Any] | None = None
            try:
                loaded = _json.loads(candidate_text)
                if isinstance(loaded, dict):
                    parsed = loaded
            except Exception:
                decoder = _json.JSONDecoder()
                for idx, ch in enumerate(candidate_text):
                    if ch == "{":
                        try:
                            obj, _end = decoder.raw_decode(candidate_text, idx)
                            if isinstance(obj, dict) and "action" in obj:
                                parsed = obj
                                break
                        except Exception:
                            continue
            if parsed is not None:
                err = _validate(parsed)
                if err is None:
                    return parsed, None
                return None, f"recovery decision from summary rejected: {err}"
        return None, f"no recovery decision found for {wo_id} (expected {decision_file.name} or decision JSON in the turn summary)"

    def _apply_recovery_decision(
        self, state: RunState, ws: Path, wo_id: str, decision: dict[str, Any],
    ) -> tuple[AdvanceResult | None, str | None]:
        """Validate and apply a recovery decision. Returns (advance, error)."""
        action = str(decision.get("action", ""))
        reason = str(decision.get("reason", ""))
        record: dict[str, Any] = {
            "work_order": wo_id,
            "action": action,
            "reason": reason,
            "decided_at": _now(),
            **{k: v for k, v in decision.items() if k not in ("work_order", "action", "reason")},
        }
        evidence = state.worker_blockers.get(wo_id, {})

        if action == "retry_unchanged":
            failure_code = evidence.get("failure_code") or decision.get("failure_code")
            transient_ok = bool(decision.get("transient")) and (
                not failure_code or failure_code in TRANSIENT_RETRYABLE_CODES
            )
            current_hash = self._sha256_file(ws / ".sync" / "contracts" / f"{wo_id}.yaml")
            hash_ok = (
                not evidence.get("contract_hash")
                or current_hash == evidence.get("contract_hash")
            )
            if not transient_ok:
                state.recovery_decisions.append({
                    **record, "applied": False,
                    "rejected_reason": "retry_unchanged requires an explicitly transient failure",
                })
                return None, (
                    "retry_unchanged is permitted only for transient failures; "
                    f"failure_code {failure_code or 'UNKNOWN'} is a governance failure"
                )
            if not hash_ok:
                state.recovery_decisions.append({
                    **record, "applied": False,
                    "rejected_reason": "contract hash changed since the blocker",
                })
                return None, "retry_unchanged requires an unchanged contract hash"
            state.recovery_decisions.append({**record, "applied": True})
            return self._resume_worker_after_recovery(state, ws, wo_id), None

        if action == "amend_contract":
            current_revision = state.contract_revisions.get(wo_id, 1)
            new_revision = decision.get("contract_revision") or current_revision + 1
            if not isinstance(new_revision, int) or new_revision <= current_revision:
                state.recovery_decisions.append({
                    **record, "applied": False,
                    "rejected_reason": f"contract_revision {new_revision!r} does not advance revision {current_revision}",
                })
                return None, (
                    f"amend_contract requires a new contract revision "
                    f"(current {current_revision}, requested {new_revision!r})"
                )
            original_contract: dict[str, Any] = {}
            contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
            if contract_file.is_file():
                try:
                    loaded = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        original_contract = loaded
                except Exception:
                    pass
            self._archive_recovery_artifact(
                ws, state.run_id, f".sync/contracts/{wo_id}.yaml"
            )
            state.contract_revisions[wo_id] = new_revision
            state.repair_context = {
                "source": "recovery",
                "action": action,
                "work_order": wo_id,
                "decision": decision,
                "original_contract": original_contract,
            }
            return self._enter_recovery_repair(state, ws, record)

        if action == "split_work_order":
            children = [str(c) for c in (decision.get("replacement_work_orders") or [])]
            if not children:
                state.recovery_decisions.append({
                    **record, "applied": False,
                    "rejected_reason": "split_work_order requires replacement_work_orders",
                })
                return None, "split_work_order requires a non-empty replacement_work_orders list"
            original_contract: dict[str, Any] = {}
            contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
            if contract_file.is_file():
                try:
                    loaded = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        original_contract = loaded
                except Exception:
                    pass
            self._archive_recovery_artifact(ws, state.run_id, f".sync/work-orders/ACTIVE/{wo_id}.yaml")
            self._archive_recovery_artifact(ws, state.run_id, f".sync/contracts/{wo_id}.yaml")
            if wo_id not in state.superseded_wo_ids:
                state.superseded_wo_ids.append(wo_id)
            self._exempt_split_milestone(state, ws, wo_id)
            self._rewire_dependents_after_split(ws, wo_id, children[-1])
            state.repair_context = {
                "source": "recovery",
                "action": action,
                "work_order": wo_id,
                "decision": decision,
                "replacement_work_orders": children,
                "original_contract": original_contract,
            }
            return self._enter_recovery_repair(state, ws, record)

        if action == "create_dependency_work_order":
            new_wos = [str(c) for c in (decision.get("replacement_work_orders") or [])]
            if not new_wos:
                state.recovery_decisions.append({
                    **record, "applied": False,
                    "rejected_reason": "create_dependency_work_order requires replacement_work_orders",
                })
                return None, "create_dependency_work_order requires a non-empty replacement_work_orders list"
            original_contract: dict[str, Any] = {}
            contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
            if contract_file.is_file():
                try:
                    loaded = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        original_contract = loaded
                except Exception:
                    pass
            state.repair_context = {
                "source": "recovery",
                "action": action,
                "work_order": wo_id,
                "decision": decision,
                "replacement_work_orders": new_wos,
                "original_contract": original_contract,
            }
            return self._enter_recovery_repair(state, ws, record)

        if action == "escalate_human":
            state.recovery_decisions.append({**record, "applied": True})
            state.error = (
                f"Architect escalation for {wo_id}: {reason or 'product/risk authority required'}; "
                f"operator decision needed before the run can continue"
            )
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.WAITING_FOR_HUMAN, None

        if action == "terminal_block":
            state.recovery_decisions.append({**record, "applied": True})
            state.error = (
                f"Work order {wo_id} terminal block by Architect recovery decision: {reason}"
            )
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED, None

        return None, f"unsupported recovery action {action!r}"

    def _enter_recovery_repair(
        self, state: RunState, ws: Path, record: dict[str, Any],
    ) -> tuple[AdvanceResult | None, str | None]:
        """Route a recovery application into the bounded ARCHITECT_REPAIR turn."""
        if state.authoring_repair_attempts >= state.max_authoring_repairs:
            state.error = (
                f"Recovery repair for {record.get('work_order')} could not start — repair attempts "
                f"exhausted ({state.authoring_repair_attempts}/{state.max_authoring_repairs})"
            )
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED, None
        # Deterministic bookkeeping: place/extend the transitional
        # repair-authorization contract so the repair turn passes pre-execution
        # validation (task WO matches contract WO) and is authorized to write
        # the governance artifacts the approved action must re-author.
        self._write_recovery_repair_authorization(
            ws,
            str(state.repair_context.get("work_order") or ""),
            str(state.repair_context.get("action") or ""),
            state.repair_context.get("original_contract") or {},
        )
        state.recovery_decisions.append({**record, "applied": True, "stage": "repair_dispatched"})
        state.error = (
            f"Recovery action '{state.repair_context.get('action')}' for "
            f"{state.repair_context.get('work_order')} requires artifact repair"
        )
        self._transition(state, Phase.ARCHITECT_REPAIR)
        state.authoring_repair_attempts += 1
        op = self._dispatch_repair_turn(state, ws)
        if op is None:
            state.error = "Recovery repair turn could not be dispatched"
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED, None
        state.architect_repair_operation_id = op.get("operation_id")
        return AdvanceResult.WAITING_FOR_OPERATION, None

    def _write_recovery_repair_authorization(
        self, ws: Path, wo_id: str, action: str, original_contract: dict[str, Any],
    ) -> None:
        """Place or extend the transitional repair-authorization contract.

        Deterministic supervisor bookkeeping for recovery repair turns: the
        pre-execution gate matches the task's work order against this contract,
        and the repair turn needs write authorization for the governance
        artifacts the approved action must re-author.  Without it the repair
        turn fail-closed at pre-execution validation ("task work order X does
        not match contract work order Y") and the recovery budget exhausted on
        a deterministically impossible turn.
        """
        if not wo_id:
            return
        from validators.harness.authoring_gate import AuthoringGate

        contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
        original_scope = original_contract.get("scope") if isinstance(original_contract, dict) else None
        allow = list((original_scope or {}).get("allow") or [])
        deny = list((original_scope or {}).get("deny") or [{"module": ".git/**"}])
        budget = original_contract.get("budget") if isinstance(original_contract, dict) else None

        def _ensure(module: str) -> None:
            if module not in {r.get("module") for r in allow if isinstance(r, dict)}:
                allow.append({"module": module})

        _ensure(f".sync/contracts/{wo_id}.yaml")
        _ensure(".sync/decisions/**")
        if action in ("split_work_order", "create_dependency_work_order"):
            _ensure(".sync/work-orders/**")
            _ensure(".sync/contracts/**")

        if action == "create_dependency_work_order" and contract_file.is_file():
            # The original contract stays authoritative for the surviving work
            # order; only extend it with the channels the prerequisite-
            # authoring turn requires.
            try:
                data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    scope = data.setdefault("scope", {})
                    allow_list = scope.setdefault("allow", [])
                    existing = {r.get("module") for r in allow_list if isinstance(r, dict)}
                    for module in (".sync/work-orders/**", ".sync/contracts/**", ".sync/decisions/**"):
                        if module not in existing:
                            allow_list.append({"module": module})
                    data["normalized_by"] = "recovery-repair"
                    contract_file.write_text(
                        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
                    )
                    return
            except Exception:
                pass

        record: dict[str, Any] = {
            "schema_version": 1,
            "agent_id": "claude",
            "work_order": wo_id,
            "identity": {"role": "architecture", "reports_to": "ceo"},
            "scope": {
                "allow": allow,
                "deny": deny,
                "write": "read-write",
            },
            "budget": (
                budget
                if isinstance(budget, dict) and budget.get("max_files_touched")
                else {"max_files_touched": 10, "max_tokens": 50000}
            ),
            "synthesized_by": "recovery-repair",
        }
        gate = AuthoringGate(project_root=ws)
        contract_yaml = yaml.safe_dump(record, sort_keys=False)
        gate_decision = gate.validate_artifact_content(
            f".sync/contracts/{wo_id}.yaml", contract_yaml, agent="ceo"
        )
        if not gate_decision.passed:
            raise ValueError(
                f"Recovery repair contract failed authoring gate: {gate_decision.summary}"
            )
        contract_file.parent.mkdir(parents=True, exist_ok=True)
        contract_file.write_text(contract_yaml, encoding="utf-8")

    def _archive_recovery_artifact(self, ws: Path, run_id: str, rel_path: str) -> None:
        """Preserve a superseded/amended governed artifact under the audit path."""
        source = ws / rel_path
        if not source.is_file():
            return
        try:
            audit_dir = ws / ".sync" / "reports" / "authoring" / run_id / "superseded"
            audit_dir.mkdir(parents=True, exist_ok=True)
            target = audit_dir / f"{Path(rel_path).stem}-{int(time.time() * 1000)}{source.suffix}"
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            source.unlink()
        except Exception:
            pass

    def _exempt_split_milestone(self, state: RunState, ws: Path, wo_id: str) -> None:
        """Mark the milestone covered by a split WO as intentionally decomposed."""
        from validators.harness.authoring_readiness import (
            load_expected_milestones, milestone_matches,
        )
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        title = ""
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                title = str(data.get("title", ""))
        except Exception:
            pass
        expected = load_expected_milestones(ws) or []
        for milestone in expected:
            if title and milestone_matches(milestone, title):
                if milestone not in state.milestone_exemptions:
                    state.milestone_exemptions.append(milestone)
                return
        if title:
            # Keep the exact WO title as a fallback exemption marker
            if title not in state.milestone_exemptions:
                state.milestone_exemptions.append(title)

    def _rewire_dependents_after_split(
        self, ws: Path, superseded_wo_id: str, successor_wo_id: str,
    ) -> None:
        """Replace references to a superseded WO with its last split child."""
        active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        if not active_dir.is_dir():
            return
        for wo_file in active_dir.glob("*.yaml"):
            if wo_file.stem == superseded_wo_id:
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                deps = data.get("dependencies")
                if not isinstance(deps, list) or superseded_wo_id not in [str(d) for d in deps]:
                    continue
                data["dependencies"] = [successor_wo_id if str(d) == superseded_wo_id else d for d in deps]
                data["updated"] = _now()
                wo_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            except Exception:
                continue

    def _clear_worker_blocker(
        self, state: RunState, ws: Path, wo_id: str, redispatch: bool = True,
    ) -> AdvanceResult:
        """Clear the recorded blocker and optionally re-dispatch the worker."""
        evidence = state.worker_blockers.pop(wo_id, None) or {}
        old_op_id = evidence.get("operation_id")
        if old_op_id and old_op_id not in state.ignored_operation_ids:
            state.ignored_operation_ids.append(str(old_op_id))
        if wo_id in state.blocked_wo_ids:
            state.blocked_wo_ids.remove(wo_id)
        if wo_id in state.failed_wo_ids:
            state.failed_wo_ids.remove(wo_id)
        self._mark_wo_status_on_disk(ws, wo_id, "ACTIVE", clear_error=True)
        if redispatch:
            agent = self._agent_for_wo(wo_id, ws)
            try:
                self.manager.start_turn(
                    state.session_id,
                    self._worker_dispatch_prompt(state, wo_id, ws)
                    + f" (recovered by architect recovery decision)",
                    role=self._role_for_agent(agent),
                    agent_id=agent,
                    work_order_id=wo_id,
                )
            except Exception as exc:
                state.blocked_wo_ids.append(wo_id)
                state.error = f"Failed to re-dispatch {wo_id} after recovery: {exc}"
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED
        state.error = None
        self._transition(state, Phase.EXECUTING)
        return AdvanceResult.WAITING_FOR_OPERATION

    def _add_work_order_dependency(self, ws: Path, wo_id: str, dep_id: str) -> None:
        """Deterministically append a prerequisite dependency to a work order file."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            return
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            deps = data.get("dependencies")
            if not isinstance(deps, list):
                deps = []
            if dep_id not in [str(d) for d in deps]:
                deps.append(dep_id)
            data["dependencies"] = deps
            data["updated"] = _now()
            wo_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    def _resume_worker_after_recovery(
        self, state: RunState, ws: Path, wo_id: str,
    ) -> AdvanceResult:
        """Backward-compatible alias: clear the blocker and re-dispatch the worker."""
        return self._clear_worker_blocker(state, ws, wo_id, redispatch=True)

    # ── Helpers ───────────────────────────────────────────────────────

    def _transition(self, state: RunState, new_phase: Phase) -> None:
        """Record a phase transition in the run state."""
        now = _now()
        state.transitions.append({
            "from": state.phase.value,
            "to": new_phase.value,
            "at": now,
        })
        state.phase = new_phase
        state.updated_at = now

        # When the run terminates, mark all in-flight work orders on disk
        # so that rebind and other policy guards don't see stale ACTIVE WOs.
        if new_phase in (Phase.FAILED, Phase.BLOCKED):
            self._finalize_in_flight_work_orders(state, new_phase.value)

    # ── Work-order disk cleanup ───────────────────────────────────────

    @staticmethod
    def _mark_wo_status_on_disk(
        ws: Path, wo_id: str, target_status: str, error: str | None = None, clear_error: bool = False,
    ) -> bool:
        """Update a single work order YAML file's status on disk.

        Returns True if the file was updated, False if it was not found or
        was already in a terminal state.
        """
        if target_status == "COMPLETED":
            archived = archive_completed_work_orders(ws, [wo_id])
            if archived:
                return True
            completed_file = ws / ".sync" / "work-orders" / "COMPLETED" / f"{wo_id}.yaml"
            return completed_file.is_file()

        _WO_TERMINAL = {"COMPLETED", "CANCELLED", "FAILED"}
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            return False
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            current = str(data.get("status", "")).upper()
            if current in _WO_TERMINAL and target_status != "ACTIVE":
                return False  # Already terminal — don't overwrite
            data["status"] = target_status
            data["updated"] = _now()
            if error:
                data["error"] = str(error)
                data["blocked_reason"] = str(error)
            elif clear_error:
                data.pop("error", None)
                data.pop("blocked_reason", None)
            wo_file.write_text(
                yaml.dump(data, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
            # Also keep INDEX.yaml synchronized if present
            index_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
            if index_file.is_file():
                try:
                    idx_data = yaml.safe_load(index_file.read_text(encoding="utf-8")) or {}
                    orders = idx_data.get("orders", [])
                    for o in orders:
                        if isinstance(o, dict) and o.get("id") == wo_id:
                            o["status"] = target_status
                            o["updated"] = data["updated"]
                    idx_data["total_active"] = sum(
                        1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "ACTIVE"
                    )
                    idx_data["total_completed"] = sum(
                        1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "COMPLETED"
                    )
                    idx_data["total_blocked"] = sum(
                        1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "BLOCKED"
                    )
                    idx_data["last_updated"] = data["updated"]
                    index_file.write_text(yaml.safe_dump(idx_data, sort_keys=False), encoding="utf-8")
                    tree_file = ws / ".sync" / "runtime" / "TREE.yaml"
                    if tree_file.is_file():
                        try:
                            tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8")) or {}
                            if "work_orders" in tree_data and isinstance(tree_data["work_orders"], dict):
                                tree_data["work_orders"]["total_active"] = idx_data.get("total_active", 0)
                                tree_data["work_orders"]["total_completed"] = idx_data.get("total_completed", 0)
                                tree_data["work_orders"]["total_blocked"] = idx_data.get("total_blocked", 0)
                                tree_file.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding="utf-8")
                        except Exception:
                            pass
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def _finalize_in_flight_work_orders(
        self, state: RunState, target_status: str,
    ) -> None:
        """Mark all non-terminal work orders associated with this run as
        *target_status* on disk.  Called when the lifecycle reaches FAILED
        or BLOCKED so that downstream policy guards (e.g. :rebind) don't
        see stale ACTIVE records."""
        ws = Path(state.workspace)
        error = state.error

        # Collect every WO ID the run knows about
        candidate_ids: list[str] = []
        if state.planning_wo_id:
            candidate_ids.append(state.planning_wo_id)
        candidate_ids.extend(state.worker_wo_ids)
        if state.integration_wo_id:
            candidate_ids.append(state.integration_wo_id)
        if state.gitops_wo_id:
            candidate_ids.append(state.gitops_wo_id)

        # De-duplicate while preserving order, skip already-completed
        seen: set[str] = set(state.completed_wo_ids)
        for wo_id in candidate_ids:
            if wo_id in seen:
                continue
            seen.add(wo_id)
            self._mark_wo_status_on_disk(ws, wo_id, target_status, error=error)

    def unblock_in_flight_work_orders(self, state: RunState) -> None:
        """Reset non-completed work orders on disk back to ACTIVE when resuming a run."""
        ws = Path(state.workspace)
        candidate_ids: list[str] = []
        if state.planning_wo_id and state.phase == Phase.PLANNING:
            candidate_ids.append(state.planning_wo_id)
        candidate_ids.extend(state.worker_wo_ids)
        if state.integration_wo_id:
            candidate_ids.append(state.integration_wo_id)
        if state.gitops_wo_id:
            candidate_ids.append(state.gitops_wo_id)

        seen: set[str] = set(state.completed_wo_ids)
        for wo_id in candidate_ids:
            if wo_id in seen:
                continue
            seen.add(wo_id)
            self._mark_wo_status_on_disk(ws, wo_id, "ACTIVE", clear_error=True)

    def _get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Safe operation lookup that returns None instead of raising."""
        try:
            return self.manager.get_operation(operation_id)
        except (KeyError, ValueError):
            return None

    def _find_latest_wo_operation(
        self, state: RunState, wo_id: str
    ) -> dict[str, Any] | None:
        """Find the most recent operation for a given work order ID."""
        try:
            ops = self.manager.list_operations(state.session_id)
        except (KeyError, ValueError):
            return None
        ws = Path(state.workspace)
        expected_agent = self._agent_for_wo(wo_id, ws)
        ignored = set(getattr(state, "ignored_operation_ids", []) or [])
        candidates = [
            op for op in ops
            if op.get("work_order_id") == wo_id
            and op.get("operation_id") != state.planning_operation_id
            and op.get("operation_id") != state.authoring_operation_id
            and op.get("operation_id") != state.integration_operation_id
            and op.get("operation_id") != state.batch_operation_id
            and op.get("operation_id") != getattr(state, "dependency_escalation_op_id", None)
            and not (
                expected_agent != "claude"
                and (
                    str(op.get("agent_id", "")).lower() == "claude"
                    or str(op.get("role", "")).lower() == "architecture"
                )
            )
            and op.get("operation_id") not in ignored
        ]
        if not candidates:
            return None
        # Prefer the latest one
        return candidates[-1]

    def _is_wo_running(self, state: RunState, wo_id: str) -> bool:
        """Check if there's a non-terminal operation for this WO."""
        op = self._find_latest_wo_operation(state, wo_id)
        if op is None:
            return False
        return str(op.get("status", "")).upper() not in _OPERATION_TERMINAL

    def _discover_worker_wos(self, state: RunState) -> None:
        """Read .sync/work-orders/ACTIVE/ to find all worker WOs (excluding WO-000)."""
        ws = Path(state.workspace)
        active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        if not active_dir.is_dir():
            return
        wo_ids: list[str] = []
        for wo_file in sorted(active_dir.glob("*.yaml")):
            wo_id = wo_file.stem
            if wo_id in (state.planning_wo_id, "WO-000"):
                continue  # Skip the planning WO
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if str(data.get("status", "")).upper() == "COMPLETED":
                        continue
                    assigned = data.get("assigned_agents", [])
                    if isinstance(assigned, list):
                        assigned_norm = [str(a).lower().strip() for a in assigned]
                    elif isinstance(assigned, str):
                        assigned_norm = [assigned.lower().strip()]
                    else:
                        assigned_norm = []
                    # Architecture-assigned work orders (integration review)
                    # are supervisor-orchestrated, never worker dispatches.
                    if any(a in ("claude", "architecture") for a in assigned_norm):
                        continue
                    is_gitops = any(
                        any(kw in a for kw in ("local-llm", "local_llm", "gitops", "release"))
                        for a in assigned_norm
                    )

                    wo_title = str(data.get("title", "")).lower()
                    wo_type = str(data.get("type", "")).lower()
                    if any(kw in wo_title for kw in ("gitops", "release")) or wo_type in ("release", "gitops"):
                        is_gitops = True

                    if is_gitops:
                        state.gitops_wo_id = wo_id
                    else:
                        wo_ids.append(wo_id)
            except Exception:
                wo_ids.append(wo_id)  # Include even if we can't read it
        state.worker_wo_ids = wo_ids

    def _dependencies_met(self, state: RunState, wo_id: str, ws: Path) -> bool:
        """Check that all dependencies of a WO are in completed_wo_ids."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.exists():
            return True  # No file means no declared dependencies
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return True
            deps = data.get("dependencies", [])
            if not isinstance(deps, list):
                return True
            for dep in deps:
                dep_str = str(dep).strip()
                if dep_str and dep_str not in state.completed_wo_ids:
                    # Also check if the dep is the planning WO (always considered met)
                    if dep_str != state.planning_wo_id and dep_str != "WO-000":
                        return False
        except Exception:
            pass
        return True

    def _agent_for_wo(self, wo_id: str, ws: Path) -> str:
        """Determine the primary agent for a work order (ACTIVE or COMPLETED)."""
        for sub in ("ACTIVE", "COMPLETED"):
            wo_file = ws / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
            if not wo_file.exists():
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    assigned = data.get("assigned_agents", [])
                    if isinstance(assigned, list) and assigned:
                        return str(assigned[0]).lower().strip()
            except Exception:
                pass
        return "codex"

    @staticmethod
    def _role_for_agent(agent: str) -> str:
        """Map agent name to canonical role for SessionManager dispatch."""
        mapping = {
            "claude": "architecture",
            "codex": "backend",
            "gemini": "frontend",
            "gemma": "qa",
            "local-llm": "gitops",
        }
        return mapping.get(agent.lower().strip(), "backend")

    def _parse_undeclared_dependency_error(
        self, err_msg: str, result: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Detect if an operation failure/block is due to undeclared third-party dependencies."""
        combined_texts = [err_msg]
        blockers = result.get("blockers") or []
        if isinstance(blockers, list):
            for b in blockers:
                combined_texts.append(str(b))
        if result.get("reason"):
            combined_texts.append(str(result["reason"]))
        if result.get("error"):
            combined_texts.append(str(result["error"]))

        full_text = " ".join(combined_texts)

        # Check signature phrases from dependency_gate / runner
        is_dep_error = any(
            phrase in full_text.lower()
            for phrase in (
                "imports undeclared third-party",
                "undeclared third-party module",
                "import satisfiability failed",
            )
        )
        if not is_dep_error:
            return None

        # Extract module names: pattern "imports undeclared third-party module(s): foo, bar."
        modules: list[str] = []
        match = re.search(r"imports undeclared third-party module\(s\):\s*([^.\n]+)", full_text, re.IGNORECASE)
        if match:
            raw_mods = match.group(1).split(",")
            modules = [m.strip() for m in raw_mods if m.strip()]

        # Extract deliverable path: pattern "Deliverable 'foo/bar.py'"
        deliv_match = re.search(r"Deliverable\s+['\"]([^'\"]+)['\"]", full_text, re.IGNORECASE)
        deliv_path = deliv_match.group(1) if deliv_match else None

        return {
            "modules": modules,
            "deliverable": deliv_path,
            "raw_error": err_msg,
        }

    def _dispatch_dependency_escalation(
        self,
        state: RunState,
        wo_id: str,
        info: dict[str, Any],
        ws: Path,
    ) -> dict[str, Any] | None:
        """Dispatch Architecture (Claude) to review and resolve undeclared third-party dependencies."""
        agent = self._agent_for_wo(wo_id, ws)
        modules = info.get("modules") or []
        modules_str = ", ".join(modules) if modules else "the imported third-party package"
        deliv = info.get("deliverable") or "deliverable"

        prompt = (
            f"Architecture Dependency Escalation: Work order {wo_id} assigned to {agent} is blocked "
            f"because deliverable '{deliv}' imports undeclared third-party module(s): {modules_str}.\n\n"
            f"As Senior Architect (Claude), resolve this dependency requirement:\n"
            f"1. Review the requirement: Is this dependency acceptable and safe for the project architecture?\n"
            f"2. If ACCEPTABLE: Update `requirements.txt` (or `pyproject.toml`) using `write_file` "
            f"to declare the missing dependency with an appropriate minimum version.\n"
            f"3. If UNACCEPTABLE: Write an inbox instruction to `.sync/inbox/{agent}/` directing the worker "
            f"to replace this import with Python standard library or local modules.\n"
            f"4. Record your architectural decision in your report summary.\n"
            f"Note: Your contract allows updating dependency manifests and inbox notices. "
            f"Do NOT write or edit the worker's application code directly."
        )

        return self._architect_turn(state, prompt, wo_id)

    def _check_qa_verdict(self, wo_id: str, ws: Path) -> str | None:
        """Check for a QA verdict file for a work order. Returns 'APPROVED', 'NEEDS_CHANGES', or None."""
        search_dirs = [
            ws / ".sync" / "inbox" / "claude",
            ws / ".sync" / "inbox" / "claude" / "_read",
            ws / ".sync" / "reviews",
            ws / ".sync" / "qa" / "verdicts",
        ]
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file():
                    continue
                name = f.name
                if wo_id not in name:
                    continue
                if not any(k in name.lower() for k in ("verdict", "review")):
                    continue
                try:
                    content = f.read_text(encoding="utf-8").upper()
                    if "APPROVED" in content:
                        return "APPROVED"
                    if "NEEDS_CHANGES" in content or "NEEDS CHANGES" in content:
                        return "NEEDS_CHANGES"
                except Exception:
                    continue
        return None

    def _get_wo_deliverable_path(self, wo_id: str, ws: Path) -> str | None:
        """Get declared deliverable path for a work order."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            return None
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                deliv = data.get("deliverable")
                if isinstance(deliv, dict) and deliv.get("path"):
                    return str(deliv["path"])
        except Exception:
            pass
        return None

    def _check_deliverable_exists(self, wo_id: str, ws: Path) -> bool:
        """Check if declared deliverable for a work order actually exists on disk."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            wo_file = ws / ".sync" / "work-orders" / "COMPLETED" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            return True
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return True
            deliv = data.get("deliverable")
            if isinstance(deliv, dict) and deliv.get("path"):
                target = ws / str(deliv["path"])
                if target.exists():
                    return True
                # A declared-path deliverable must exist on disk — inbox
                # sign-off notices no longer substitute for it (D024 shift-left:
                # QA suites are real files the gate can verify).
                return False
            # Deliverables without a declared path (sign-off style) and work
            # orders without a deliverable section are verified downstream.
            return True
        except Exception:
            return True
        return True

    def _get_wo_type(self, wo_id: str, ws: Path) -> str:
        """Get deliverable type for a work order (e.g. 'code', 'config', 'doc')."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            return "code"
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                deliv = data.get("deliverable")
                if isinstance(deliv, dict) and deliv.get("type"):
                    return str(deliv["type"]).lower().strip()
                wo_t = data.get("type")
                if wo_t:
                    return str(wo_t).lower().strip()
        except Exception:
            pass
        return "code"

    def _get_qa_feedback(self, wo_id: str, ws: Path) -> str:
        """Extract QA review feedback text from verdict file if present."""
        search_dirs = [
            ws / ".sync" / "inbox" / "claude",
            ws / ".sync" / "inbox" / "claude" / "_read",
            ws / ".sync" / "inbox" / "codex",
            ws / ".sync" / "inbox" / "gemini",
            ws / ".sync" / "reviews",
            ws / ".sync" / "qa" / "verdicts",
        ]
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file() or wo_id not in f.name:
                    continue
                if not any(k in f.name.lower() for k in ("verdict", "review")):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                    if "NEEDS_CHANGES" in text.upper() or "NEEDS CHANGES" in text.upper():
                        return text.strip()
                except Exception:
                    continue
        return ""

    def _find_gitops_wo(self, ws: Path, state: RunState) -> str | None:
        """Find a GitOps work order from disk."""
        if state.gitops_wo_id:
            return state.gitops_wo_id
        active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        if not active_dir.is_dir():
            return None
        for wo_file in sorted(active_dir.glob("*.yaml")):
            wo_id = wo_file.stem
            if wo_id in (state.planning_wo_id, "WO-000"):
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    assigned = data.get("assigned_agents", [])
                    is_gitops = False
                    if isinstance(assigned, list):
                        is_gitops = any(
                            any(kw in str(a).lower().strip() for kw in ("local-llm", "local_llm", "gitops", "release"))
                            for a in assigned
                        )
                    elif isinstance(assigned, str):
                        is_gitops = any(kw in str(assigned).lower().strip() for kw in ("local-llm", "local_llm", "gitops", "release"))
                    
                    wo_title = str(data.get("title", "")).lower()
                    wo_type = str(data.get("type", "")).lower()
                    if any(kw in wo_title for kw in ("gitops", "release")) or wo_type in ("release", "gitops"):
                        is_gitops = True

                    if is_gitops:
                        return wo_id
            except Exception:
                continue
        return None

    # ── Persistence ───────────────────────────────────────────────────

    @staticmethod
    def save_run_state(state: RunState, ws: Path) -> Path:
        """Persist run state to .sync/runtime/supervisor/<run_id>.yaml."""
        state_dir = ws / ".sync" / "runtime" / "supervisor"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"{state.run_id}.yaml"
        path.write_text(yaml.safe_dump(state.to_dict(), sort_keys=False), encoding="utf-8")
        return path

    @staticmethod
    def load_run_state(run_id: str, ws: Path) -> RunState | None:
        """Load a previously persisted run state."""
        path = ws / ".sync" / "runtime" / "supervisor" / f"{run_id}.yaml"
        if not path.exists():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return RunState.from_dict(data)
        except Exception:
            pass
        return None

    @staticmethod
    def list_runs(ws: Path) -> list[str]:
        """List all run IDs with persisted state."""
        state_dir = ws / ".sync" / "runtime" / "supervisor"
        if not state_dir.is_dir():
            return []
        return sorted(f.stem for f in state_dir.glob("*.yaml"))


# ─── Release Commit Provenance ────────────────────────────────────────

def format_release_commit_message(
    summary: str,
    work_order_id: str,
    released_by: str = "local-llm",
    approved_by: str = "gemma",
    architect: str = "claude",
    target_work_orders: list[str] | None = None,
) -> str:
    """Format a release commit message with standard provenance trailers.

    Conforms to Git trailer conventions:
    Work-Order: <id>
    Released-By: <agent>
    Approved-By: <agent>
    Architect: <agent>
    Target-Work-Orders: <ids>
    """
    clean_summary = summary.strip()
    trailers = [
        f"Work-Order: {work_order_id}",
        f"Released-By: {released_by}",
        f"Approved-By: {approved_by}",
        f"Architect: {architect}",
    ]
    if target_work_orders:
        trailers.append(f"Target-Work-Orders: {', '.join(target_work_orders)}")
    return f"{clean_summary}\n\n" + "\n".join(trailers) + "\n"


def create_gitops_commit(
    workspace: Path | str,
    work_order_id: str,
    summary: str = "feat(release): product release",
    released_by: str = "local-llm",
    approved_by: str = "gemma",
    architect: str = "claude",
    target_work_orders: list[str] | None = None,
) -> str:
    """Create a governed GitOps release commit with standard provenance trailers.

    Stages files adhering to .gitignore and commits with trailers.
    Returns the created commit SHA.
    """
    ws = Path(workspace).resolve()
    commit_msg = format_release_commit_message(
        summary=summary,
        work_order_id=work_order_id,
        released_by=released_by,
        approved_by=approved_by,
        architect=architect,
        target_work_orders=target_work_orders,
    )
    subprocess.run(["git", "add", "."], cwd=str(ws), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(ws),
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ws),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def archive_completed_work_orders(
    workspace: Path | str,
    completed_wo_ids: list[str] | None = None,
) -> list[str]:
    """Archive completed work orders from ACTIVE to COMPLETED and update INDEX.yaml/TREE.yaml.

    Returns the list of work order IDs that were successfully archived.
    """
    ws = Path(workspace).resolve()
    sync_dir = ws / ".sync"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    completed_dir = sync_dir / "work-orders" / "COMPLETED"
    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    tree_file = sync_dir / "runtime" / "TREE.yaml"

    if not active_dir.is_dir():
        return []

    completed_dir.mkdir(parents=True, exist_ok=True)
    target_ids = set(completed_wo_ids or [])

    archived: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for wo_file in sorted(active_dir.glob("*.yaml")):
        wo_id = wo_file.stem
        try:
            wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8")) or {}
        except Exception:
            wo_data = {}

        is_completed = (
            wo_id in target_ids
            or str(wo_data.get("status", "")).upper() == "COMPLETED"
        )
        if not is_completed:
            continue

        wo_data["status"] = "COMPLETED"
        wo_data["updated"] = now_iso
        target_file = completed_dir / wo_file.name
        target_file.write_text(yaml.safe_dump(wo_data, sort_keys=False), encoding="utf-8")
        try:
            wo_file.unlink()
        except OSError:
            pass
        archived.append(wo_id)

    if not archived:
        return []

    # Update INDEX.yaml
    index_data: dict[str, Any] = {}
    if index_file.is_file():
        try:
            index_data = yaml.safe_load(index_file.read_text(encoding="utf-8")) or {}
            orders = index_data.get("orders", [])
            for o in orders:
                if isinstance(o, dict) and o.get("id") in archived:
                    o["status"] = "COMPLETED"
                    o["file"] = f"work-orders/COMPLETED/{o.get('id')}.yaml"
                    o["updated"] = now_iso
            index_data["total_active"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "ACTIVE"
            )
            index_data["total_completed"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "COMPLETED"
            )
            index_data["total_blocked"] = sum(
                1 for o in orders if isinstance(o, dict) and str(o.get("status", "")).upper() == "BLOCKED"
            )
            index_data["last_updated"] = now_iso
            index_file.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    # Update TREE.yaml
    if tree_file.is_file():
        try:
            tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8")) or {}
            if "work_orders" in tree_data and isinstance(tree_data["work_orders"], dict):
                if index_file.is_file():
                    tree_data["work_orders"]["total_active"] = index_data.get("total_active", 0)
                    tree_data["work_orders"]["total_completed"] = index_data.get("total_completed", 0)
                    tree_data["work_orders"]["total_blocked"] = index_data.get("total_blocked", 0)
            if "agents" in tree_data and isinstance(tree_data["agents"], dict):
                archived_set = set(archived)
                for _agent_name, agent_info in tree_data["agents"].items():
                    if isinstance(agent_info, dict) and "assigned_work_orders" in agent_info:
                        assigned = agent_info["assigned_work_orders"]
                        if isinstance(assigned, list):
                            agent_info["assigned_work_orders"] = [
                                w for w in assigned if w not in archived_set
                            ]
            tree_data["last_updated"] = now_iso
            tree_file.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    return archived


__all__ = [
    "AdvanceResult",
    "LifecycleSupervisor",
    "Phase",
    "RunState",
    "archive_completed_work_orders",
    "create_gitops_commit",
    "format_release_commit_message",
]
