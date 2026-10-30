"""Human/operator controls: plan approval, resume override, re-attestation, escalation and dependency rewiring.

Part of the :mod:`validators.kernel.daemon.supervisor` package.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from validators.clock import now as _now

from .models import (
    EXIT_BUDGET_EXHAUSTED,
    EXIT_FAILURE,
    EXIT_PLAN_REJECTED,
    EXIT_SUCCESS,
    AdvanceResult,
    Phase,
    RunState,
)

logger = logging.getLogger("stackmind.supervisor")

class _ControlsMixin:
    """Human/operator controls: plan approval, resume override, re-attestation, escalation and dependency rewiring."""

    def reconcile_reopened_work_orders(self, state: RunState) -> list[str]:
        """Re-queue work orders reopened on disk since run state last looked.

        A reopened work order is ACTIVE with `reopened_from` provenance. It is
        removed from `completed_wo_ids`, granted a fresh retry budget, and its
        pre-reopen operation is ignored so a stale completed result cannot
        re-complete it. Returns the IDs reconciled.
        """
        ws = Path(state.workspace)
        active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        if not active_dir.is_dir():
            return []
        reconciled: list[str] = []
        for wo_file in sorted(active_dir.glob("*.yaml")):
            wo_id = wo_file.stem
            if wo_id not in state.completed_wo_ids:
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or not isinstance(data.get("reopened_from"), dict):
                continue
            state.completed_wo_ids.remove(wo_id)
            for bucket in (state.blocked_wo_ids, state.failed_wo_ids):
                if wo_id in bucket:
                    bucket.remove(wo_id)
            state.retry_counts[wo_id] = 0
            prior = self._find_latest_wo_operation(state, wo_id)
            if prior and prior.get("operation_id"):
                prior_id = str(prior["operation_id"])
                if prior_id not in state.ignored_operation_ids:
                    state.ignored_operation_ids.append(prior_id)
            if wo_id not in state.worker_wo_ids:
                state.worker_wo_ids.append(wo_id)
            reconciled.append(wo_id)
        return reconciled

    def unblock_in_flight_work_orders(self, state: RunState) -> None:
        """Reset non-completed work orders on disk back to ACTIVE when resuming a run."""
        state.stall_ticks = 0
        state.blocked_wo_ids.clear()
        state.recovery_wo_id = None
        state.recovery_decision_operation_id = None
        if hasattr(self.manager, "_sessions"):
            sess = getattr(self.manager, "_sessions", {}).get(state.session_id)
            if sess and sess.get("state") in {"CANCELLED", "PAUSED", "WAITING", "BLOCKED", "FAILED"}:
                sess["state"] = "RUNNING"
                sess["updated_at"] = _now()
                if hasattr(self.manager, "_save"):
                    try:
                        self.manager._save()
                    except Exception:
                        pass
        ws = Path(state.workspace)
        candidate_ids: list[str] = []
        if state.planning_wo_id and state.planning_wo_id not in state.completed_wo_ids:
            candidate_ids.append(state.planning_wo_id)
        candidate_ids.extend(state.worker_wo_ids)
        if state.integration_wo_id:
            candidate_ids.append(state.integration_wo_id)
        if state.gitops_wo_id:
            candidate_ids.append(state.gitops_wo_id)
        for qa_id, targets in list(getattr(state, "qa_rework_targets", {}).items()):
            for target_id in targets:
                if target_id not in candidate_ids:
                    candidate_ids.append(target_id)

        seen: set[str] = set(state.completed_wo_ids)
        for wo_id in candidate_ids:
            if wo_id in seen:
                continue
            seen.add(wo_id)
            self._unarchive_work_order(ws, wo_id)
            if wo_id in state.blocked_wo_ids:
                state.blocked_wo_ids.remove(wo_id)

    # ── P0-5 Human Override, Escalation, and Exit Codes ─────────────────

    VALID_RESETTABLE_COUNTERS = {
        "gitops_retries",
        "qa_reworks",
        "worker_retries",
        "global_turn_budget",
    }

    def reject_plan(self, state: RunState, reason: str = "") -> AdvanceResult:
        """Explicit operator rejection of the proposed architecture plan."""
        state.error = f"PLAN_REJECTED: {reason or 'Human operator rejected proposed plan'}"
        state.exit_code = EXIT_PLAN_REJECTED  # 4
        self._transition(state, Phase.FAILED)
        return AdvanceResult.FAILED

    def approve_plan(self, state: RunState) -> AdvanceResult:
        """Explicit operator approval of the proposed architecture plan."""
        self._transition(state, Phase.AUTHORING)
        return AdvanceResult.TRANSITIONED

    def resume_run_with_human_override(
        self,
        state: RunState,
        reason: str,
        reset_counters: list[str] | None = None,
        additional_budget: int | None = None,
        new_budget: int | None = None,
        target_phase: Phase | None = None,
    ) -> AdvanceResult:
        """Resume a halted run via explicit, logged, and bounded human override."""
        if not reason or not str(reason).strip():
            raise ValueError("Human override resume requires an explicit non-empty reason.")

        if state.human_resumes_used >= state.max_human_resumes:
            state.error = (
                f"Maximum human resumes ({state.max_human_resumes}) exceeded. Run permanently failed."
            )
            state.exit_code = EXIT_FAILURE
            self._transition(state, Phase.FAILED)
            return AdvanceResult.FAILED

        counters = list(reset_counters or [])
        # Check for invalid counter names or wildcard "all"
        for c in counters:
            if c not in self.VALID_RESETTABLE_COUNTERS:
                raise ValueError(
                    f"Invalid reset counter '{c}'. Wildcard 'all' is prohibited; "
                    f"must be one of {sorted(self.VALID_RESETTABLE_COUNTERS)}"
                )

        # If budget was exhausted, require additional budget > 0
        budget_grant = 0
        if state.budget_exhausted or state.global_turn_budget <= 0:
            if additional_budget is not None and additional_budget > 0:
                budget_grant = additional_budget
            elif new_budget is not None and new_budget > 0:
                budget_grant = new_budget
            else:
                raise ValueError(
                    "Global turn budget is exhausted; human override resume requires "
                    "additional_budget > 0 to continue."
                )

        state.human_resumes_used += 1
        prev_phase = state.phase.value

        log_entry = {
            "timestamp": _now(),
            "resumes_count": state.human_resumes_used,
            "reason": str(reason).strip(),
            "reset_counters": counters,
            "additional_budget": budget_grant,
            "previous_phase": prev_phase,
        }
        state.human_override_logs.append(log_entry)

        # Record override event in HMAC journal chain
        if hasattr(self, "state_manager") and self.state_manager is not None:
            try:
                self.state_manager.record_journal_event("HUMAN_RESUME_OVERRIDE", log_entry)
            except Exception:
                pass

        # Reset requested named counters
        for c in counters:
            if c == "gitops_retries":
                state.gitops_retries = 0
            elif c == "qa_reworks":
                state.qa_reworks.clear()
            elif c == "worker_retries":
                state.worker_retries.clear()
            elif c == "global_turn_budget":
                state.global_turn_budget = budget_grant if budget_grant > 0 else (new_budget or state.initial_turn_budget)
                state.budget_exhausted = False

        if budget_grant > 0:
            if "global_turn_budget" not in counters:
                state.global_turn_budget += budget_grant
            state.budget_exhausted = False

        self._sync_budget_file(state)
        state.error = None
        state.exit_code = EXIT_SUCCESS
        state.stall_ticks = 0
        if target_phase is not None:
            dest_phase = target_phase
        elif state.interrupted_phase and state.interrupted_phase not in (Phase.WAITING_FOR_HUMAN.value, Phase.FAILED.value, Phase.BLOCKED.value):
            try:
                dest_phase = Phase(state.interrupted_phase)
            except ValueError:
                dest_phase = Phase.DISPATCHING if state.worker_wo_ids else Phase.PLANNING
        else:
            dest_phase = Phase.DISPATCHING if state.worker_wo_ids else Phase.PLANNING
        self._transition(state, dest_phase)
        return AdvanceResult.TRANSITIONED

    human_resume = resume_run_with_human_override

    def re_attest_run(
        self,
        state: RunState,
        human_signature: str,
        confirmed_budget: int | None = None,
        confirmed_counters: dict[str, Any] | None = None,
        target_phase: Phase | None = None,
    ) -> AdvanceResult:
        """Re-attest and resume a run halted due to daemon restart or integrity fail-closed.

        Per Amendment A / Item 3:
        1. On daemon restart, the in-memory HMAC key is lost and re-generated, so pre-restart
           checkpoint files on disk cannot be verified cryptographically and are UNTRUSTED HINTS.
        2. The human supplies the authoritative budget (`confirmed_budget > 0`). If not supplied,
           raises ValueError.
        3. Restored counters MAY NOT BE LOWER than the last values confirmed out-of-band by the human
           (preventing an adversary from editing on-disk checkpoints to silently reset retry counters).
        4. Does NOT increment state.human_resumes_used (daemon restart is not an override of budget/policy).
        5. Returns to the interrupted phase recorded in the checkpoint (or PLANNING if unstarted).
        """
        if not human_signature or not str(human_signature).strip():
            raise ValueError("Human re-attestation requires a valid signature / credential.")

        if confirmed_budget is None or not isinstance(confirmed_budget, int) or confirmed_budget <= 0:
            raise ValueError("Human re-attestation requires an explicit confirmed_budget > 0.")

        # 1. Enforce restart cap to prevent crash-loops from acting as budget refills
        if state.daemon_restarts >= state.max_daemon_restarts:
            state.phase = Phase.FAILED
            state.error = f"MAX_DAEMON_RESTARTS_EXCEEDED: run reached restart cap ({state.max_daemon_restarts})"
            state.exit_code = EXIT_FAILURE
            raise ValueError(f"Run reached maximum allowed daemon restarts ({state.max_daemon_restarts}).")

        # 2. Budget non-increasing across restarts rule:
        # Increasing budget requires consuming an authoritative human resume count.
        prior_budget = state.global_turn_budget
        if confirmed_budget > prior_budget:
            if state.human_resumes_used >= state.max_human_resumes:
                state.phase = Phase.FAILED
                state.error = f"Confirmed budget ({confirmed_budget}) exceeds prior budget ({prior_budget}) and human resume cap is exhausted."
                state.exit_code = EXIT_BUDGET_EXHAUSTED
                raise ValueError(
                    f"Confirmed budget ({confirmed_budget}) exceeds prior budget ({prior_budget}) and "
                    f"human resume cap ({state.max_human_resumes}) is exhausted."
                )
            state.human_resumes_used += 1
            state.human_override_logs.append({
                "timestamp": _now(),
                "action": "re_attest_budget_increase",
                "prior_budget": prior_budget,
                "new_budget": confirmed_budget,
                "human_signature": human_signature,
            })

        state.daemon_restarts += 1
        state.error = None
        state.exit_code = EXIT_SUCCESS
        state.stall_ticks = 0

        # Human supplies authoritative budget:
        state.global_turn_budget = confirmed_budget
        state.budget_exhausted = False

        # Restored counters may not be lower than the confirmed values:
        if confirmed_counters:
            conf_gitops = int(confirmed_counters.get("gitops_retries", 0))
            if state.gitops_retries < conf_gitops:
                state.gitops_retries = conf_gitops

            conf_workers = dict(confirmed_counters.get("worker_retries", {}) or {})
            for wo, retries in conf_workers.items():
                state.worker_retries[wo] = max(state.worker_retries.get(wo, 0), int(retries))

            conf_qa = dict(confirmed_counters.get("qa_reworks", {}) or {})
            for wo, reworks in conf_qa.items():
                state.qa_reworks[wo] = max(state.qa_reworks.get(wo, 0), int(reworks))

        # Mark attested and journal in HMAC chain in state manager if present
        if hasattr(self, "state_manager") and self.state_manager is not None:
            self.state_manager.register_state(state.run_id, state)
            re_attest_meta = {
                "confirmed_budget": confirmed_budget,
                "confirmed_counters": confirmed_counters or {},
                "human_signature": human_signature,
                "daemon_restarts": state.daemon_restarts,
                "human_resumes_used": state.human_resumes_used,
            }
            self.state_manager.re_attest_run(
                state.run_id,
                f"Human re-attestation by {human_signature}",
                metadata=re_attest_meta,
            )
            self.state_manager.create_checkpoint(state, workspace=state.workspace)

        # Determine target resume phase: explicit target, or recorded interrupted phase, or PLANNING
        resume_phase = target_phase
        if resume_phase is None and state.interrupted_phase:
            try:
                resume_phase = Phase(state.interrupted_phase)
            except ValueError:
                resume_phase = None

        if resume_phase is None or resume_phase == Phase.WAITING_FOR_HUMAN:
            resume_phase = Phase.EXECUTING if state.worker_wo_ids else Phase.PLANNING

        self._transition(state, resume_phase)
        return AdvanceResult.TRANSITIONED

    def escalate_wo_to_recovery(self, state: RunState, wo_id: str, reason: str = "") -> None:
        """Escalate a work order to Architect recovery and update blocked dependents."""
        if wo_id not in state.recovery_attempts:
            state.recovery_attempts[wo_id] = 1
        state.recovery_wo_id = wo_id
        state.repair_context = {"work_order_id": wo_id, "reason": reason}
        self.evaluate_blocked_by_escalation(state)

    def set_wo_dependencies(self, state: RunState, wo_id: str, deps: list[str]) -> None:
        """Register explicit dependencies for a work order."""
        state.wo_dependencies[wo_id] = list(deps)

    def evaluate_blocked_by_escalation(self, state: RunState) -> None:
        """Mark dependent work orders as BLOCKED_BY_ESCALATION if their upstream is in recovery."""
        recovering_wos = set()
        if state.recovery_wo_id:
            recovering_wos.add(state.recovery_wo_id)
        for wid, att in state.recovery_attempts.items():
            if att > 0 and wid not in state.completed_wo_ids:
                recovering_wos.add(wid)

        for wo_id in state.worker_wo_ids:
            deps = state.wo_dependencies.get(wo_id, [])
            for d in deps:
                if d in recovering_wos:
                    state.blocked_by_escalation[wo_id] = d
                    if wo_id in state.completed_wo_ids:
                        state.completed_wo_ids.remove(wo_id)
                    break
            else:
                if wo_id in state.blocked_by_escalation:
                    upstream = state.blocked_by_escalation[wo_id]
                    if upstream not in recovering_wos:
                        state.blocked_by_escalation.pop(wo_id, None)

    def resolve_escalation_with_amendment(self, state: RunState, wo_id: str) -> None:
        """Resolve Architect recovery for a work order, updating contracts and unblocking dependents."""
        if state.recovery_wo_id == wo_id:
            state.recovery_wo_id = None
        state.recovery_attempts[wo_id] = 0
        state.worker_retries[wo_id] = 0
        self.evaluate_blocked_by_escalation(state)
