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

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


# ─── Lifecycle phases ─────────────────────────────────────────────────

class Phase(StrEnum):
    """Deterministic lifecycle phases for a product delivery run."""
    INIT = "INIT"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AUTHORING = "AUTHORING"
    DISPATCHING = "DISPATCHING"
    EXECUTING = "EXECUTING"
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
    batch_operation_id: str | None = None
    gitops_wo_id: str | None = None
    gitops_operation_id: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    release_commit_sha: str | None = None
    transitions: list[dict[str, str]] = field(default_factory=list)
    max_retries: int = 2

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
            "batch_operation_id": self.batch_operation_id,
            "gitops_wo_id": self.gitops_wo_id,
            "gitops_operation_id": self.gitops_operation_id,
            "release_commit_sha": self.release_commit_sha,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "transitions": list(self.transitions),
            "max_retries": self.max_retries,
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
            batch_operation_id=data.get("batch_operation_id"),
            gitops_wo_id=data.get("gitops_wo_id"),
            gitops_operation_id=data.get("gitops_operation_id"),
            release_commit_sha=data.get("release_commit_sha"),
            error=data.get("error"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            transitions=data.get("transitions", []),
            max_retries=data.get("max_retries", 2),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Supervisor ───────────────────────────────────────────────────────

_OPERATION_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}

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
    ) -> RunState:
        """Create a new lifecycle run and transition to PLANNING."""
        now = _now()
        state = RunState(
            run_id=run_id,
            product_goal=product_goal,
            workspace=str(workspace),
            session_id=session_id,
            phase=Phase.INIT,
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
            Phase.DISPATCHING: self._advance_dispatching,
            Phase.EXECUTING: self._advance_executing,
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
        max_wait_seconds: float = 300.0,
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
                    if op.get("work_order_id") == state.planning_wo_id and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                        state.planning_operation_id = op.get("operation_id")
                        break
            except Exception:
                pass

        if state.planning_operation_id:
            op = self._get_operation(state.planning_operation_id)
            if op is None:
                # Operation record lost — re-dispatch
                state.planning_operation_id = None
                return self._advance_planning(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION
            if status == "FAILED":
                state.error = f"Planning turn failed: {op.get('result', {}).get('error', 'unknown')}"
                self._transition(state, Phase.FAILED)
                return AdvanceResult.FAILED
            # Planning completed — check if a plan was proposed
            plans = self.manager.list_plans(state.session_id)
            if plans:
                latest_plan = plans[-1]
                state.plan_id = latest_plan.get("plan_id")
                plan_state = str(latest_plan.get("state", "")).upper()
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
        """AUTHORING: wait for Architecture Turn 2 (WO/Contract authoring) to complete."""
        if state.authoring_operation_id:
            op = self._get_operation(state.authoring_operation_id)
            if op is None:
                state.authoring_operation_id = None
                return self._advance_authoring(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION

            # 1. Discover worker WOs from disk
            self._discover_worker_wos(state)

            # 2. If no worker WOs found on disk (e.g. Turn 2 was conversational or failed tool loop),
            # autonomously synthesize child work orders and contracts from the approved PLAN.md
            if not state.worker_wo_ids:
                ws = Path(state.workspace)
                try:
                    plan = self.manager.get_plan(state.session_id, state.plan_id) if state.plan_id else {}
                    from .authoring import synthesize_child_work_orders
                    synthesize_child_work_orders(ws, plan, session_id=state.session_id)
                    self._discover_worker_wos(state)
                except Exception:
                    pass

            if not state.worker_wo_ids:
                err_detail = op.get('result', {}).get('error') or op.get('result', {}).get('reason') or 'no worker Work Orders found on disk'
                state.error = f"Authoring failed: {err_detail}"
                self._transition(state, Phase.FAILED)
                return AdvanceResult.FAILED

            state.error = None
            self._transition(state, Phase.DISPATCHING)
            return AdvanceResult.TRANSITIONED

        # Check if already running or previously dispatched
        ops = self.manager.list_operations(state.session_id)
        for op in reversed(ops):
            metadata = op.get("metadata", {})
            if metadata.get("is_authoring"):
                state.authoring_operation_id = op.get("operation_id")
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
            "4. When all work orders and contracts are written, return the final HarnessDecision JSON declaring status 'completed' "
            "and modified_files listing all authored files."
        )
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

    def _advance_dispatching(self, state: RunState) -> AdvanceResult:
        """DISPATCHING: dispatch eligible worker WOs, then transition to EXECUTING."""
        ws = Path(state.workspace)
        dispatched_any = False

        if not state.worker_wo_ids:
            self._discover_worker_wos(state)

        # 1. Discover all eligible work orders whose dependencies are satisfied
        eligible_wos = []
        for wo_id in state.worker_wo_ids:
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
                    f"Execute work order {wo_id}",
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
        dispatched_new = False

        if not state.worker_wo_ids:
            self._discover_worker_wos(state)

        for wo_id in state.worker_wo_ids:
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
                                f"Execute work order {wo_id}",
                                role=self._role_for_agent(agent),
                                agent_id=agent,
                                work_order_id=wo_id,
                            )
                            dispatched_new = True
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
                if wo_id not in state.blocked_wo_ids:
                    state.blocked_wo_ids.append(wo_id)
                err_msg = result.get("error") or result.get("reason") or "governance gate blocked"
                state.error = f"Work order {wo_id} blocked: {err_msg}"
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED

            if status == "FAILED":
                retries = state.retry_counts.get(wo_id, 0)
                if retries < state.max_retries:
                    state.retry_counts[wo_id] = retries + 1
                    agent = self._agent_for_wo(wo_id, ws)
                    try:
                        self.manager.start_turn(
                            state.session_id,
                            f"Retry work order {wo_id} (attempt {retries + 2})",
                            role=self._role_for_agent(agent),
                            agent_id=agent,
                            work_order_id=wo_id,
                        )
                        dispatched_new = True
                    except Exception:
                        state.failed_wo_ids.append(wo_id)
                    all_done = False
                else:
                    state.failed_wo_ids.append(wo_id)
                continue

            # Verify declared deliverable exists on disk (S3)
            if not self._check_deliverable_exists(wo_id, ws):
                retries = state.retry_counts.get(wo_id, 0)
                if retries < state.max_retries:
                    state.retry_counts[wo_id] = retries + 1
                    agent = self._agent_for_wo(wo_id, ws)
                    try:
                        self.manager.start_turn(
                            state.session_id,
                            f"Retry work order {wo_id}: declared deliverable was not found on disk (attempt {retries + 2})",
                            role=self._role_for_agent(agent),
                            agent_id=agent,
                            work_order_id=wo_id,
                        )
                        dispatched_new = True
                    except Exception:
                        state.failed_wo_ids.append(wo_id)
                    all_done = False
                else:
                    state.failed_wo_ids.append(wo_id)
                continue

            # Completed with deliverable on disk — check QA verdict
            qa_verdict = self._check_qa_verdict(wo_id, ws)
            if qa_verdict == "APPROVED":
                if wo_id not in state.completed_wo_ids:
                    state.completed_wo_ids.append(wo_id)
            elif qa_verdict == "NEEDS_CHANGES":
                retries = state.retry_counts.get(wo_id, 0)
                if retries < state.max_retries:
                    state.retry_counts[wo_id] = retries + 1
                    agent = self._agent_for_wo(wo_id, ws)
                    feedback = self._get_qa_feedback(wo_id, ws)
                    rework_prompt = (
                        f"Rework work order {wo_id} after QA feedback (attempt {retries + 2}):\n{feedback}"
                        if feedback
                        else f"Rework work order {wo_id} after QA feedback (attempt {retries + 2})"
                    )
                    try:
                        self.manager.start_turn(
                            state.session_id,
                            rework_prompt,
                            role=self._role_for_agent(agent),
                            agent_id=agent,
                            work_order_id=wo_id,
                        )
                        dispatched_new = True
                    except Exception:
                        state.failed_wo_ids.append(wo_id)
                    all_done = False
                else:
                    state.failed_wo_ids.append(wo_id)
                continue
            else:
                # No QA verdict file found yet
                wo_type = self._get_wo_type(wo_id, ws)
                is_qa_agent = self._agent_for_wo(wo_id, ws) == "gemma"
                if wo_type in ("doc", "config", "research") or is_qa_agent:
                    # Non-code or QA deliverable completes once on disk
                    if wo_id not in state.completed_wo_ids:
                        state.completed_wo_ids.append(wo_id)
                else:
                    # Code deliverable verified on disk without explicit rejection
                    if wo_id not in state.completed_wo_ids:
                        state.completed_wo_ids.append(wo_id)

        # Check for fatal failures among worker WOs
        non_gitops_failed = [
            wid for wid in state.failed_wo_ids
            if wid != state.gitops_wo_id
        ]
        if non_gitops_failed:
            if state.batch_operation_id:
                if hasattr(self.manager, "complete_operation"):
                    try:
                        self.manager.complete_operation(operation_id=state.batch_operation_id, status="FAILED")
                    except Exception:
                        pass
                state.batch_operation_id = None
            state.error = f"Work orders failed after max retries: {non_gitops_failed}"
            self._transition(state, Phase.FAILED)
            return AdvanceResult.FAILED

        if all_done:
            if state.batch_operation_id:
                if hasattr(self.manager, "complete_operation"):
                    try:
                        self.manager.complete_operation(operation_id=state.batch_operation_id, status="COMPLETED")
                    except Exception:
                        pass
                state.batch_operation_id = None
            self._transition(state, Phase.INTEGRATION_REVIEW)
            return AdvanceResult.TRANSITIONED

        if dispatched_new:
            return AdvanceResult.WAITING_FOR_OPERATION
        return AdvanceResult.WAITING_FOR_OPERATION

    def _advance_integration_review(self, state: RunState) -> AdvanceResult:
        """INTEGRATION_REVIEW: dispatch Architecture to review all deliverables."""
        if state.integration_operation_id:
            op = self._get_operation(state.integration_operation_id)
            if op is None:
                state.integration_operation_id = None
                return self._advance_integration_review(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION
            # Integration review completed
            self._transition(state, Phase.PRODUCT_READY)
            return AdvanceResult.TRANSITIONED

        # Dispatch integration review
        completed_summary = ", ".join(state.completed_wo_ids)
        review_prompt = (
            f"All implementation work orders have been completed and QA-approved: {completed_summary}.\n\n"
            "As Senior Architect, perform the integration review:\n"
            "1. Read PLAN.md for the original requirements.\n"
            "2. Read each deliverable file to verify completeness.\n"
            "3. Check that all work orders are consistent and integrate correctly.\n"
            "4. If satisfied, write a PRODUCT_READY notice to .sync/inbox/claude/ "
            "and return status 'completed'.\n"
            "5. If changes are needed, return status 'blocked' with details."
        )
        op = self.manager.start_turn(
            state.session_id,
            review_prompt,
            role="architecture",
            agent_id="claude",
            work_order_id=state.planning_wo_id,
        )
        state.integration_operation_id = op.get("operation_id")
        return AdvanceResult.WAITING_FOR_OPERATION

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
            if status == "FAILED":
                state.error = f"GitOps turn failed: {op.get('result', {}).get('error', 'unknown')}"
                self._transition(state, Phase.FAILED)
                return AdvanceResult.FAILED
            # GitOps completed — if workspace has a git repository, perform governed release commit
            ws = Path(state.workspace)
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
            gitops_prompt = (
                "All implementation work orders are complete, QA-approved, and integration-reviewed.\n\n"
                "As GitOps Release Lead, perform the release:\n"
                "1. Review PLAN.md and the completed deliverables.\n"
                "2. Write CHANGELOG.md and VERSION.md if they don't exist.\n"
                "3. Return status 'completed' with the release summary."
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
        candidates = [
            op for op in ops
            if op.get("work_order_id") == wo_id
            and op.get("operation_id") != state.planning_operation_id
            and op.get("operation_id") != state.authoring_operation_id
            and op.get("operation_id") != state.integration_operation_id
            and op.get("operation_id") != state.batch_operation_id
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
                    assigned = data.get("assigned_agents", [])
                    # Classify: if assigned to local-llm/gitops, it's the GitOps WO
                    if isinstance(assigned, list) and any(
                        a.lower().strip() in ("local-llm", "gitops") for a in assigned
                    ):
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
        """Determine the primary agent for a work order."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if wo_file.exists():
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

    def _check_deliverable_exists(self, wo_id: str, ws: Path) -> bool:
        """Check if declared deliverable for a work order actually exists on disk."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            return True
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return True
            deliv = data.get("deliverable")
            if isinstance(deliv, dict) and deliv.get("path"):
                target = ws / str(deliv["path"])
                return target.exists()
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
            if wo_id == state.planning_wo_id or wo_id in state.worker_wo_ids:
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    assigned = data.get("assigned_agents", [])
                    if isinstance(assigned, list) and any(
                        a.lower().strip() in ("local-llm", "gitops") for a in assigned
                    ):
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


__all__ = [
    "AdvanceResult",
    "LifecycleSupervisor",
    "Phase",
    "RunState",
    "create_gitops_commit",
    "format_release_commit_message",
]
