"""Composed lifecycle supervisor.

Behaviour lives in focused mixin modules; this module assembles them into the public
``LifecycleSupervisor``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from validators.clock import now as _now

from .controls import _ControlsMixin
from .execution import _ExecutionMixin
from .gitops import _GitopsMixin
from .models import (
    DEFAULT_TIMEOUTS,
    EXIT_BUDGET_EXHAUSTED,
    EXIT_FAILURE,
    EXIT_HEADLESS_TIMEOUT,
    EXIT_INTEGRITY_VIOLATION,
    EXIT_PLAN_REJECTED,
    EXIT_RETRIES_EXHAUSTED,
    EXIT_SUCCESS,
    AdvanceResult,
    BudgetExhaustedError,
    OperationContentionError,
    Phase,
    RunState,
    SupervisorStateManager,
)
from .phases import _PhasesMixin
from .recovery import _RecoveryMixin
from .release import (
    archive_blocked_work_orders,
)
from .review import _ReviewMixin
from .status import _StatusMixin

logger = logging.getLogger("stackmind.supervisor")

class LifecycleSupervisor(
    _PhasesMixin,
    _ExecutionMixin,
    _ReviewMixin,
    _RecoveryMixin,
    _GitopsMixin,
    _StatusMixin,
    _ControlsMixin,
):
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

    def __init__(
        self,
        manager: Any,
        max_contention_retries: int = 50,
        state_manager: SupervisorStateManager | None = None,
        reaper: Any | None = None,
        timeouts: dict[Any, float] | None = None,
        ci_wait_timeout: float | None = None,
        ci_run_timeout: float | None = None,
        headless: bool | None = None,
    ) -> None:
        """Initialise with a SessionManager (or compatible mock)."""
        self.manager = manager
        self.max_contention_retries = max_contention_retries
        self.state_manager = state_manager or SupervisorStateManager()
        self._current_state: RunState | None = None
        if reaper is not None:
            self.reaper = reaper
        else:
            try:
                from validators.kernel.process_reaper import get_global_reaper
                self.reaper = get_global_reaper()
            except Exception:
                self.reaper = None

        self.timeouts = dict(DEFAULT_TIMEOUTS)
        if timeouts:
            self.timeouts.update(timeouts)
        self.ci_wait_timeout = float(
            ci_wait_timeout
            if ci_wait_timeout is not None
            else os.environ.get("SUPERVISOR_CI_WAIT_TIMEOUT", self.timeouts.get("ci_wait_timeout", 60.0))
        )
        self._ci_run_timeout_explicit = (
            ci_run_timeout is not None or "SUPERVISOR_CI_RUN_TIMEOUT" in os.environ
        )
        self.ci_run_timeout = float(
            ci_run_timeout
            if ci_run_timeout is not None
            else os.environ.get("SUPERVISOR_CI_RUN_TIMEOUT", self.timeouts.get("ci_run_timeout", 1800.0))
        )
        self.headless = (
            headless
            if headless is not None
            else (os.environ.get("SUPERVISOR_HEADLESS", os.environ.get("CI", "0")).lower() in ("1", "true"))
        )
        self._wrap_manager_for_budget()

    def get_timeout_for_phase(self, phase: Phase | str) -> float:
        """Retrieve the configured timeout for a given phase."""
        if phase in self.timeouts:
            return float(self.timeouts[phase])
        if isinstance(phase, Phase):
            if phase.value in self.timeouts:
                return float(self.timeouts[phase.value])
            if phase.name.lower() in self.timeouts:
                return float(self.timeouts[phase.name.lower()])
            env_key = f"SUPERVISOR_TIMEOUT_{phase.name.upper()}"
            if env_key in os.environ:
                try:
                    return float(os.environ[env_key])
                except ValueError:
                    pass
        elif isinstance(phase, str):
            for k, v in self.timeouts.items():
                if isinstance(k, Phase) and (k.value == phase or k.name.lower() == phase.lower()):
                    return float(v)
                if isinstance(k, str) and k.lower() == phase.lower():
                    return float(v)
            env_key = f"SUPERVISOR_TIMEOUT_{phase.upper()}"
            if env_key in os.environ:
                try:
                    return float(os.environ[env_key])
                except ValueError:
                    pass
        return float(DEFAULT_TIMEOUTS.get(phase, 600.0))

    def handle_rejection_without_reworkable(
        self,
        state: RunState,
        rejecting_role: str,
        reason: str,
        affected_paths: list[str] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> AdvanceResult:
        """Route QA/GitOps rejection with empty reworkable to ARCHITECT_RECOVERY with structured diagnostic context."""
        state.repair_context = {
            "source_phase": state.phase.value,
            "rejecting_role": rejecting_role,
            "error": "UNIDENTIFIABLE_REWORKABLE_WO",
            "reason": reason,
            "affected_paths": list(affected_paths or []),
            "diagnostics": dict(diagnostics or {}),
            "timestamp": _now(),
        }
        wo_id = "SYSTEM_UNIDENTIFIABLE"
        state.recovery_wo_id = wo_id
        attempts = state.recovery_attempts.get(wo_id, 0) + 1
        state.recovery_attempts[wo_id] = attempts
        if attempts >= state.max_retries:
            state.error = f"MAX_RECOVERY_ATTEMPTS_EXHAUSTED: Unidentifiable rejection cap reached ({attempts}/{state.max_retries})"
            state.exit_code = EXIT_RETRIES_EXHAUSTED
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN
        self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
        return AdvanceResult.TRANSITIONED

    def handle_invalid_turn_output(
        self,
        state: RunState,
        role: str,
        wo_id: str | None = None,
        raw_output: str | None = None,
        error_type: str = "EMPTY_OUTPUT",
    ) -> AdvanceResult:
        """Handle empty, malformed, or schema-invalid agent output as an explicit failure outcome."""
        state.global_turn_budget = max(0, state.global_turn_budget - 1)
        self._sync_budget_file(state)

        if state.global_turn_budget <= 0:
            state.budget_exhausted = True
            state.error = f"BUDGET_EXHAUSTED: Invalid output ({error_type}) exhausted global budget."
            state.exit_code = EXIT_BUDGET_EXHAUSTED
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN

        role_lower = str(role).lower()
        if role_lower in ("backend", "frontend", "worker") or state.phase == Phase.EXECUTING:
            wid = wo_id or (state.worker_wo_ids[0] if state.worker_wo_ids else "WO-001")
            retries = state.worker_retries.get(wid, 0) + 1
            state.worker_retries[wid] = retries
            if retries >= state.max_worker_retries:
                self.escalate_wo_to_recovery(state, wid, reason=f"Worker returned {error_type}")
                self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
            else:
                state.error = f"WORKER_INVALID_OUTPUT: {error_type} on {wid} (retry {retries}/{state.max_worker_retries})"
            return AdvanceResult.TRANSITIONED

        elif role_lower == "qa" or state.phase in (
            Phase.INTEGRATION_REVIEW, Phase.QA_REVIEW, Phase.FINAL_REVIEW
        ):
            wid = wo_id or (state.worker_wo_ids[0] if state.worker_wo_ids else "WO-001")
            reworks = state.qa_reworks.get(wid, 0) + 1
            state.qa_reworks[wid] = reworks
            if reworks >= state.max_qa_reworks:
                self.escalate_wo_to_recovery(state, wid, reason=f"QA returned {error_type}")
                self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
            else:
                state.error = f"QA_INVALID_OUTPUT: {error_type} on {wid} (round {reworks}/{state.max_qa_reworks})"
            return AdvanceResult.TRANSITIONED

        elif role_lower == "gitops" or state.phase == Phase.GITOPS:
            state.gitops_retries += 1
            if state.gitops_retries >= state.max_gitops_retries:
                state.error = f"MAX_GITOPS_RETRIES_EXHAUSTED: GitOps returned {error_type}"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"GITOPS_INVALID_OUTPUT: {error_type} (retry {state.gitops_retries}/{state.max_gitops_retries})"
            return AdvanceResult.TRANSITIONED

        elif role_lower in ("architecture", "architect", "lead_architect") or state.phase in (Phase.AUTHORING, Phase.ARCHITECT_REPAIR):
            state.authoring_repair_attempts += 1
            if state.authoring_repair_attempts >= state.max_authoring_repairs:
                state.error = f"MAX_AUTHORING_REPAIRS_EXHAUSTED: Architecture returned {error_type}"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"ARCHITECTURE_INVALID_OUTPUT: {error_type} in {state.phase.value}"
            return AdvanceResult.TRANSITIONED

        elif state.phase == Phase.PLANNING:
            state.planning_retries += 1
            if state.planning_retries >= state.max_planning_retries:
                state.error = f"MAX_PLANNING_RETRIES_EXHAUSTED: Planning returned {error_type}"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"PLANNING_INVALID_OUTPUT: {error_type} (retry {state.planning_retries}/{state.max_planning_retries})"
            return AdvanceResult.TRANSITIONED

        elif state.phase == Phase.ARCHITECT_RECOVERY_DECISION:
            wo_id = str(state.recovery_wo_id or "SYSTEM_UNIDENTIFIABLE")
            attempts = state.recovery_attempts.get(wo_id, 0) + 1
            state.recovery_attempts[wo_id] = attempts
            if attempts >= state.max_retries:
                state.error = f"MAX_RECOVERY_ATTEMPTS_EXHAUSTED: Recovery decision for {wo_id} returned {error_type}"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"RECOVERY_INVALID_OUTPUT: {error_type} in {wo_id} (attempt {attempts}/{state.max_retries})"
            return AdvanceResult.TRANSITIONED

        else:
            state.error = f"INVALID_OUTPUT: {error_type} in phase {state.phase.value}"
            state.exit_code = EXIT_RETRIES_EXHAUSTED
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN

    def _handle_turn_timeout(self, state: RunState) -> AdvanceResult:
        """Supervisor monotonic deadline expired: terminate process tree, consume counter, and transition."""
        if self.reaper is not None:
            try:
                self.reaper.reap_all()
            except Exception:
                pass

        timeout_phase = state.phase
        timeout_val = self.get_timeout_for_phase(timeout_phase)
        state.turn_deadline = None
        state.turn_started_at = None

        if hasattr(self, "manager") and self.manager is not None:
            try:
                session = self.manager.get_session(state.session_id)
                active_op = session.get("active_operation")
                if active_op:
                    try:
                        self.manager.cancel_operation(active_op)
                    except Exception:
                        pass
                    if hasattr(self.manager, "_active"):
                        self.manager._active.pop(active_op, None)
                    session["active_operation"] = None
            except Exception:
                pass

        # Note: self._wrap_manager_for_budget already decrements global_turn_budget upon turn dispatch.
        # No duplicate decrement here to avoid double-charging (Item 4).

        if timeout_phase == Phase.EXECUTING:
            wo_id = state.executing_wo_id or (state.worker_wo_ids[0] if state.worker_wo_ids else "WO-001")
            retries = state.worker_retries.get(wo_id, 0) + 1
            state.worker_retries[wo_id] = retries
            if retries >= state.max_worker_retries:
                self.escalate_wo_to_recovery(state, wo_id, reason=f"Worker turn timed out after {timeout_val:.1f}s")
                self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
            else:
                state.error = f"WORKER_TIMEOUT: Turn timed out after {timeout_val:.1f}s (retry {retries}/{state.max_worker_retries})"
            return AdvanceResult.TRANSITIONED

        elif timeout_phase in (Phase.INTEGRATION_REVIEW, Phase.QA_REVIEW, Phase.FINAL_REVIEW):
            wo_id = state.integration_wo_id or (state.worker_wo_ids[0] if state.worker_wo_ids else "WO-001")
            reworks = state.qa_reworks.get(wo_id, 0) + 1
            state.qa_reworks[wo_id] = reworks
            if reworks >= state.max_qa_reworks:
                self.escalate_wo_to_recovery(state, wo_id, reason=f"QA review timed out after {timeout_val:.1f}s")
                self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
            else:
                state.error = f"QA_TIMEOUT: Review timed out after {timeout_val:.1f}s (round {reworks}/{state.max_qa_reworks})"
            return AdvanceResult.TRANSITIONED

        elif timeout_phase == Phase.GITOPS:
            state.gitops_retries += 1
            if state.gitops_retries >= state.max_gitops_retries:
                state.error = f"MAX_GITOPS_RETRIES_EXHAUSTED: GitOps release timed out after {timeout_val:.1f}s"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"GITOPS_TIMEOUT: Release timed out after {timeout_val:.1f}s (retry {state.gitops_retries}/{state.max_gitops_retries})"
            return AdvanceResult.TRANSITIONED

        elif timeout_phase == Phase.PLANNING:
            state.planning_retries += 1
            if state.planning_retries >= state.max_planning_retries:
                state.error = f"MAX_PLANNING_RETRIES_EXHAUSTED: Planning turn timed out after {timeout_val:.1f}s"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"PLANNING_TIMEOUT: Planning timed out after {timeout_val:.1f}s (retry {state.planning_retries}/{state.max_planning_retries})"
            return AdvanceResult.TRANSITIONED

        elif timeout_phase in (Phase.AUTHORING, Phase.ARCHITECT_REPAIR):
            state.authoring_repair_attempts += 1
            if state.authoring_repair_attempts >= state.max_authoring_repairs:
                state.error = f"MAX_AUTHORING_REPAIRS_EXHAUSTED: Authoring state {timeout_phase.value} timed out after {timeout_val:.1f}s"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"ARCHITECTURE_TIMEOUT: {timeout_phase.value} timed out after {timeout_val:.1f}s"
            return AdvanceResult.TRANSITIONED

        elif timeout_phase == Phase.ARCHITECT_RECOVERY_DECISION:
            wo_id = str(state.recovery_wo_id or "SYSTEM_UNIDENTIFIABLE")
            attempts = state.recovery_attempts.get(wo_id, 0) + 1
            state.recovery_attempts[wo_id] = attempts
            if attempts >= state.max_retries:
                state.error = f"MAX_RECOVERY_ATTEMPTS_EXHAUSTED: Recovery decision for {wo_id} timed out after {timeout_val:.1f}s"
                state.exit_code = EXIT_RETRIES_EXHAUSTED
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.error = f"RECOVERY_TIMEOUT: Recovery timed out after {timeout_val:.1f}s (attempt {attempts}/{state.max_retries})"
            return AdvanceResult.TRANSITIONED

        elif timeout_phase == Phase.AUTHORING_READINESS_GATE:
            state.error = f"GATE_TIMEOUT: Authoring readiness gate timed out after {timeout_val:.1f}s"
            self._transition(state, Phase.ARCHITECT_REPAIR)
            return AdvanceResult.TRANSITIONED

        elif timeout_phase == Phase.DISPATCHING:
            state.error = f"DISPATCH_TIMEOUT: Dispatching timed out after {timeout_val:.1f}s"
            self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
            return AdvanceResult.TRANSITIONED

        else:
            state.error = f"STATE_TIMEOUT: Phase {timeout_phase.value} timed out after {timeout_val:.1f}s"
            state.exit_code = EXIT_RETRIES_EXHAUSTED
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN

    def _handle_ci_timeout(self, state: RunState, reason: str | None = None) -> AdvanceResult:
        """Headless CI run abort on timeout: write crash bundle and rescue ref, terminate FAILED (137)."""
        err_msg = reason or f"HEADLESS_CI_TIMEOUT: Timeout exceeded {self.ci_wait_timeout}s."
        state.exit_code = EXIT_HEADLESS_TIMEOUT  # 137
        state.error = err_msg
        self._transition(state, Phase.FAILED)
        return AdvanceResult.FAILED

    def _wrap_manager_for_budget(self) -> None:
        """Intercept manager.start_turn to enforce global turn budget decrement."""
        original_start_turn = getattr(self.manager, "start_turn", None)
        if original_start_turn is None:
            return

        def hooked_start_turn(*args: Any, **kwargs: Any) -> Any:
            state = self._current_state
            if state is not None:
                if state.global_turn_budget <= 0:
                    state.budget_exhausted = True
                    state.error = "BUDGET_EXHAUSTED: Global turn budget (B) reached 0."
                    state.exit_code = EXIT_BUDGET_EXHAUSTED
                    self._transition(state, Phase.WAITING_FOR_HUMAN)
                    raise BudgetExhaustedError("Global turn budget exhausted.")

            result = original_start_turn(*args, **kwargs)
            if state is not None:
                state.global_turn_budget = max(0, state.global_turn_budget - 1)
                self._sync_budget_file(state)
            return result

        self.manager.start_turn = hooked_start_turn

    def _sync_budget_file(self, state: RunState) -> None:
        """Persist remaining and initial turn budget to external checkpoint and shadow file."""
        if hasattr(self, "state_manager") and self.state_manager is not None:
            try:
                self.state_manager.create_checkpoint(state, workspace=state.workspace)
            except Exception:
                pass
        try:
            ws = Path(state.workspace)
            b_file = ws / ".sync" / "runtime" / "supervisor" / "budget.json"
            b_file.parent.mkdir(parents=True, exist_ok=True)
            b_data = {
                "_comment": "SHADOW READ-ONLY COPY -- NOT AN INPUT TO SUPERVISOR ADVANCE LOOP",
                "remaining_turn_budget": state.global_turn_budget,
                "initial_turn_budget": state.initial_turn_budget,
                "budget_exhausted": bool(state.budget_exhausted),
                "updated_at": _now(),
            }
            b_file.write_text(json.dumps(b_data, indent=2), encoding="utf-8")
        except Exception:
            pass

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
        from validators.kernel.settings import load_settings
        try:
            profile_val = load_settings(Path(workspace)).execution_profile
        except Exception:
            profile_val = "medium"
        try:
            archive_blocked_work_orders(Path(workspace))
        except Exception:
            pass
        # Resolve any work-order mutation that was interrupted before its audit
        # record landed. Starting a run must not silently build on an unknown
        # revision: unresolved records stay pending and block consequential work.
        try:
            from validators.kernel.work_order_ops import mark_dependents_stale, reconcile_operations

            def _repair(record: dict[str, Any]) -> bool:
                result = mark_dependents_stale(
                    Path(workspace),
                    upstream_id=str(record.get("work_order") or ""),
                    upstream_revision=int(record.get("target_revision") or 0),
                    reason=str(record.get("reason") or "recovered amendment"),
                    fields=[str(f) for f in record.get("changed_fields") or []],
                    actor="runtime-recovery",
                )
                return not result["failed"]

            reconcile_operations(Path(workspace), apply_stale=_repair)
        except Exception:
            pass
        initial_budget = 18 + 5 * 1  # 23 turns default for initial planning
        state = RunState(
            run_id=run_id,
            product_goal=product_goal,
            workspace=str(workspace),
            session_id=session_id,
            phase=Phase.INIT,
            bootstrap_mode=bootstrap_mode,
            execution_profile=profile_val,
            global_turn_budget=initial_budget,
            initial_turn_budget=initial_budget,
            created_at=now,
            updated_at=now,
        )
        if hasattr(self, "state_manager") and self.state_manager is not None:
            self.state_manager.register_state(run_id, state)
        state.run_started_at = time.monotonic()
        self._current_state = state
        self._sync_budget_file(state)
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
        self._current_state = state
        now_mono = time.monotonic()

        # 1. Monotonic human wait timeout for headless runs
        if self.headless and state.phase == Phase.WAITING_FOR_HUMAN:
            entered_at = state.waiting_for_human_entered_at or state.run_started_at or now_mono
            if (now_mono - entered_at) > self.ci_wait_timeout:
                return self._handle_ci_timeout(
                    state,
                    reason=f"HEADLESS_CI_TIMEOUT: Human wait timeout exceeded {self.ci_wait_timeout}s.",
                )

        # 2. Monotonic total run wall-clock timeout check
        effective_ci_run_timeout = self.ci_run_timeout
        if not getattr(self, "_ci_run_timeout_explicit", False):
            # Derive from the actual budget x largest turn timeout, not a hard-coded 40
            budget = getattr(state, "initial_turn_budget", None) or getattr(state, "global_turn_budget", None) or 50
            turn_timeouts = [
                v for k, v in self.timeouts.items()
                if k not in ("ci_wait_timeout", "ci_run_timeout", "gate") and isinstance(v, (int, float))
            ]
            largest_turn_timeout = max(turn_timeouts) if turn_timeouts else 600.0
            effective_ci_run_timeout = float(budget * largest_turn_timeout)

        if self.headless and state.run_started_at is not None and (now_mono - state.run_started_at) > effective_ci_run_timeout:
            return self._handle_ci_timeout(
                state,
                reason=f"HEADLESS_RUN_TIMEOUT: Total run wall-clock time exceeded {effective_ci_run_timeout}s.",
            )

        # 2. Monotonic turn deadline check
        if state.turn_deadline is not None and now_mono > state.turn_deadline:
            return self._handle_turn_timeout(state)

        if hasattr(self, "state_manager") and self.state_manager is not None:
            if not self.state_manager.is_run_attested(state.run_id):
                if not state.transitions and state.run_id not in getattr(self.state_manager, "_in_memory_states", {}):
                    self.state_manager.register_state(state.run_id, state)
                else:
                    state.error = (
                        "DAEMON_RESTART_FAIL_CLOSED: In-memory session key lost on daemon restart. "
                        "Human re-attestation required."
                    )
                    state.exit_code = EXIT_INTEGRITY_VIOLATION
                    self._transition(state, Phase.WAITING_FOR_HUMAN)
                    return AdvanceResult.WAITING_FOR_HUMAN

        if state.global_turn_budget <= 0 and state.phase not in (Phase.COMPLETE, Phase.FAILED, Phase.WAITING_FOR_HUMAN):
            state.budget_exhausted = True
            state.error = "BUDGET_EXHAUSTED: Global turn budget (B) reached 0."
            state.exit_code = EXIT_BUDGET_EXHAUSTED
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN

        terminal_or_waiting = (
            Phase.COMPLETE,
            Phase.FAILED,
            Phase.BLOCKED,
            Phase.WAITING_FOR_HUMAN,
            Phase.AWAITING_APPROVAL,
        )
        if state.turn_deadline is None and state.phase not in terminal_or_waiting:
            timeout = self.get_timeout_for_phase(state.phase)
            state.turn_deadline = now_mono + timeout
            state.turn_started_at = now_mono

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
            Phase.QA_REVIEW: self._advance_integration_review,
            Phase.INTEGRATION_REVIEW: self._advance_integration_review,
            Phase.FINAL_REVIEW: self._advance_final_review,
            Phase.PRODUCT_READY: self._advance_product_ready,
            Phase.GITOPS: self._advance_gitops,
            Phase.WAITING_FOR_HUMAN: lambda _s: AdvanceResult.WAITING_FOR_HUMAN,
            Phase.COMPLETE: self._advance_complete,
            Phase.FAILED: lambda _s: AdvanceResult.FAILED,
            Phase.BLOCKED: lambda _s: AdvanceResult.BLOCKED,
        }.get(state.phase)
        if handler is None:
            return AdvanceResult.FAILED
        # An Architect may reopen a completed work order mid-run; run state must
        # not keep treating it as completed (or its old result as authoritative).
        self.reconcile_reopened_work_orders(state)
        old_phase = state.phase
        old_budget = state.global_turn_budget
        try:
            res = handler(state)
            state.contention_count = 0

            # Generalized C_stall: Check non-terminal, non-waiting phases for stalled progress
            terminal_or_waiting = (
                Phase.COMPLETE,
                Phase.FAILED,
                Phase.BLOCKED,
                Phase.WAITING_FOR_HUMAN,
                Phase.AWAITING_APPROVAL,
            )
            if state.phase not in terminal_or_waiting:
                phase_changed = (state.phase != old_phase)
                turn_dispatched = (state.global_turn_budget < old_budget)
                active_op = self._has_active_operations(state)
                if phase_changed or turn_dispatched or active_op:
                    state.stall_ticks = 0
                else:
                    state.stall_ticks += 1
                    if state.stall_ticks >= state.max_stall_ticks:
                        # Attempt autonomous Architect recovery escalation if retry budget remains
                        recovery_attempts = state.recovery_attempts.get("IDLE_STALL", 0)
                        if (
                            state.phase in (
                                Phase.EXECUTING,
                                Phase.DISPATCHING,
                                Phase.INTEGRATION_REVIEW,
                                Phase.FINAL_REVIEW,
                            )
                            and recovery_attempts < state.max_retries
                            and state.global_turn_budget > 0
                        ):
                            stall_res = self._escalate_idle_stall_to_recovery(state)
                            if stall_res is not None:
                                return stall_res

                        state.error = (
                            f"IDLE_STALL (C_stall): Phase {state.phase.value} stalled for "
                            f"{state.stall_ticks} consecutive ticks with no active operations "
                            "and no eligible transitions. Stalled awaiting human intervention."
                        )
                        state.exit_code = EXIT_RETRIES_EXHAUSTED
                        self._transition(state, Phase.WAITING_FOR_HUMAN)
                        return AdvanceResult.WAITING_FOR_HUMAN
            else:
                state.stall_ticks = 0

            return res
        except BudgetExhaustedError as exc:
            state.budget_exhausted = True
            state.stall_ticks = 0
            state.error = f"BUDGET_EXHAUSTED: {exc}"
            state.exit_code = EXIT_BUDGET_EXHAUSTED
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN
        except OperationContentionError as exc:
            state.stall_ticks = 0
            threads_alive = False
            if hasattr(self.manager, "_turn_threads"):
                threads_alive = any(t.is_alive() for t in getattr(self.manager, "_turn_threads", {}).values())
            if threads_alive:
                state.contention_count = 0
                return AdvanceResult.WAITING_FOR_OPERATION

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
                state.stall_ticks = 0
                threads_alive = False
                if hasattr(self.manager, "_turn_threads"):
                    threads_alive = any(t.is_alive() for t in getattr(self.manager, "_turn_threads", {}).values())
                if threads_alive:
                    state.contention_count = 0
                    return AdvanceResult.WAITING_FOR_OPERATION

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

    def _transition(self, state: RunState, new_phase: Phase) -> None:
        """Record a phase transition in the run state."""
        state.turn_deadline = None
        state.turn_started_at = None
        now = _now()
        if new_phase == Phase.WAITING_FOR_HUMAN and state.phase != Phase.WAITING_FOR_HUMAN:
            state.interrupted_phase = state.phase.value
            state.waiting_for_human_entered_at = time.monotonic()
        elif new_phase != Phase.WAITING_FOR_HUMAN:
            state.waiting_for_human_entered_at = None
        state.transitions.append({
            "from": state.phase.value,
            "to": new_phase.value,
            "at": now,
        })
        state.phase = new_phase
        if new_phase == Phase.FAILED:
            self._capture_terminal_crash_bundle(state)
        state.updated_at = now

        # When the run terminates, mark all in-flight work orders on disk
        # so that rebind and other policy guards don't see stale ACTIVE WOs.
        if new_phase in (Phase.FAILED, Phase.BLOCKED):
            self._finalize_in_flight_work_orders(state, new_phase.value)

    # ── Persistence ───────────────────────────────────────────────────

    @classmethod
    def save_run_state(
        cls,
        state: RunState,
        ws: Path,
        state_manager: SupervisorStateManager | None = None,
    ) -> Path:
        """Persist authoritative state in memory/HMAC store and mirror shadow telemetry.

        Per Amendment A: Authoritative state is kept in memory and signed checkpoints outside
        the workspace. Workspace copy is write-only shadow telemetry.
        """
        if state_manager is not None:
            state_manager.register_state(state.run_id, state)
            state_manager.create_checkpoint(state, workspace=ws)

        state_dir = ws / ".sync" / "runtime" / "supervisor"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / f"{state.run_id}.yaml"
        header = (
            "# =====================================================================\n"
            "# SHADOW TELEMETRY COPY ONLY -- NOT AN INPUT TO SUPERVISOR ADVANCE LOOP\n"
            "# Authoritative state is maintained in supervisor memory with HMAC chain.\n"
            "# Any edits made here by agents or child processes will be ignored.\n"
            "# =====================================================================\n"
        )
        path.write_text(header + yaml.safe_dump(state.to_dict(), sort_keys=False), encoding="utf-8")
        return path

    @classmethod
    def load_run_state(
        cls,
        run_id: str,
        ws: Path | None = None,
        state_manager: SupervisorStateManager | None = None,
    ) -> RunState | None:
        """Load authoritative run state from supervisor memory.

        Per Amendment A: Workspace copies are never inputs. Authoritative state lives strictly
        in supervisor memory or signed external checkpoints.
        """
        sm = state_manager or getattr(cls, "state_manager", None)
        if sm is not None:
            s = sm.get_state(run_id)
            if s is not None:
                return s
        if ws is not None:
            state_file = Path(ws) / ".sync" / "runtime" / "supervisor" / f"{run_id}.yaml"
            if state_file.is_file():
                try:
                    data = yaml.safe_load(state_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        state = RunState.from_dict(data)
                        if sm is not None:
                            sm.register_state(run_id, state)
                        return state
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

    def get_exit_code(self, state: RunState, is_headless_abort: bool = False) -> int:
        """Map current run state to standard StackMind exit code table."""
        if is_headless_abort:
            return EXIT_HEADLESS_TIMEOUT  # 137
        if state.exit_code == EXIT_INTEGRITY_VIOLATION or any(
            kw in str(state.error).lower() for kw in ("daemon_restart", "replay", "integrity", "tamper")
        ):
            return EXIT_INTEGRITY_VIOLATION  # 5
        if state.phase == Phase.COMPLETE:
            return EXIT_SUCCESS  # 0
        if state.phase == Phase.FAILED:
            return EXIT_FAILURE  # 1
        if state.phase in (Phase.WAITING_FOR_HUMAN, Phase.BLOCKED):
            if state.exit_code in (EXIT_BUDGET_EXHAUSTED, EXIT_RETRIES_EXHAUSTED, EXIT_PLAN_REJECTED, EXIT_INTEGRITY_VIOLATION):
                return state.exit_code
            if state.budget_exhausted or state.global_turn_budget <= 0:
                return EXIT_BUDGET_EXHAUSTED  # 2
            if state.gitops_retries >= state.max_gitops_retries:
                return EXIT_RETRIES_EXHAUSTED  # 3
            for w, r in state.qa_reworks.items():
                if r >= state.max_qa_reworks:
                    return EXIT_RETRIES_EXHAUSTED  # 3
            for w, r in state.worker_retries.items():
                if r >= state.max_worker_retries:
                    return EXIT_RETRIES_EXHAUSTED  # 3
            if state.phase == Phase.AWAITING_APPROVAL or "plan" in str(state.error).lower():
                return EXIT_PLAN_REJECTED  # 4
            return state.exit_code or EXIT_FAILURE
        if state.phase == Phase.AWAITING_APPROVAL:
            return EXIT_PLAN_REJECTED  # 4
        return state.exit_code
