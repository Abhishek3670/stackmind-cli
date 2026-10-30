"""Linear phase handlers (INIT through PRODUCT_READY) of the delivery state machine.

Part of the :mod:`validators.kernel.daemon.supervisor` package.
"""

from __future__ import annotations

import logging
from pathlib import Path

from validators.clock import now as _now

from .models import (
    _OPERATION_TERMINAL,
    AdvanceResult,
    OperationContentionError,
    Phase,
    RunState,
    _extract_op_error,
)
from .release import (
    archive_completed_work_orders,
)

logger = logging.getLogger("stackmind.supervisor")

class _PhasesMixin:
    """Linear phase handlers (INIT through PRODUCT_READY) of the delivery state machine."""

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
                err_detail = _extract_op_error(op) or op.get("error") or "unknown"
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
        state.planning_wo_id = op.get("work_order_id") or state.planning_wo_id or "WO-000"
        return AdvanceResult.WAITING_FOR_OPERATION

    def _advance_awaiting_approval(self, state: RunState) -> AdvanceResult:
        """AWAITING_APPROVAL: check if the plan has been approved or rejected."""
        if not state.plan_id:
            plans = self.manager.list_plans(state.session_id)
            for p in reversed(plans):
                if str(p.get("state", "")).upper() == "AWAITING_APPROVAL":
                    state.plan_id = p.get("plan_id")
                    break
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
            state.planning_wo_id = None  # reset so next planning turn allocates a fresh WO ID
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
                    from ..authoring import synthesize_child_work_orders
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

        ws = Path(state.workspace)
        try:
            from ..authoring import get_next_work_order_int
            start_wo_idx = get_next_work_order_int(ws)
            example_wo = f"WO-{start_wo_idx:03d}"
        except Exception:
            example_wo = "WO-001"

        # Supervisor directly dispatches Turn 2 (Authoring child WOs and Contracts)
        authoring_prompt = (
            f"The architecture plan '{state.plan_id or 'PLAN-001'}' has been approved by the operator.\n\n"
            "Your task now as Senior Architect is to author the implementation Work Orders and Contracts for the tasks in PLAN.md:\n"
            "1. Review PLAN.md for the approved milestones and tasks.\n"
            f"2. For each task, call write_file to write a Work Order YAML file to .sync/work-orders/ACTIVE/<WO-ID>.yaml "
            f"(e.g. {example_wo}.yaml) conforming to schemas/work-order.schema.json.\n"
            f"3. For each Work Order, call write_file to write a corresponding Contract YAML file to .sync/contracts/<WO-ID>.yaml "
            f"(e.g. {example_wo}.yaml) conforming to schemas/contract.schema.json.\n"
            "4. Size each contract budget from the task's expected file set: declare an implementation_estimate "
            "(expected_files and max_files_touched) in the work order and set the contract budget "
            "max_files_touched to cover it.\n"
            "4b. Populate `acceptance_criteria` in each Work Order with machine-checkable statements "
            "(e.g. \"average([]) returns 0\", \"letter_grade(95) returns 'A'\") — QA verifies the "
            "delivery against them, so keep them objective and testable.\n"
            "5. Do NOT mandate or explicitly specify TDD companion test deliverables for Backend or Frontend work orders. "
            "Let Backend and Frontend focus on their assigned deliverables according to their work orders (they are permitted to author "
            "unit tests to self-verify if they choose). QA/verification Work Orders (assigned to gemma) MUST declare an executable code "
            "deliverable under tests/ — when QA's turn arrives, QA will examine existing deliverables and any developer unit tests, "
            "then author an independent end-to-end test suite for further code quality and verification. "
            "Tests must use standard Python and pytest (never require external browser automation like playwright or selenium unless explicitly scaffolded).\n"
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
                milestone_table_lines = []
                for idx, ref in enumerate(plan_milestones):
                    line = f"  - {ref['id']}: '{ref['title']}'"
                    if ref.get("agent"):
                        line += f" (agent: {ref['agent']})"
                    if idx < len(active_ids):
                        line += f" -> ALLOCATED WORK ORDER ID: {active_ids[idx]}"
                    milestone_table_lines.append(line)
                milestone_table = "\n".join(milestone_table_lines)
                authoring_prompt += (
                    "\n\nApproved plan milestones (set `milestone_id` on each work "
                    f"order to the matching id):\n{milestone_table}\n"
                    f"\nCRITICAL ANTI-COLLISION RULE: You MUST use the allocated Work Order IDs ({', '.join(active_ids)}). "
                    "Do NOT reuse earlier or completed Work Order IDs (e.g. WO-001)."
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
        try:
            from ..authoring import reconcile_active_work_orders_with_plan
            reconcile_active_work_orders_with_plan(ws)
        except Exception:
            pass
        self._discover_worker_wos(state)
        gate_result = self._run_readiness_gate(state, ws)
        if gate_result.ready:
            state.published_wo_ids = sorted(state.worker_wo_ids)
            state.readiness_issues = []
            state.error = None
            # Reconcile planning WO-000 to COMPLETED upon publishing worker orders
            for p_id in (state.planning_wo_id, "WO-000"):
                if p_id:
                    if p_id not in state.completed_wo_ids:
                        state.completed_wo_ids.insert(0, p_id)
                    self._mark_wo_status_on_disk(ws, p_id, "COMPLETED")
            archive_completed_work_orders(ws, [state.planning_wo_id or "WO-000", "WO-000"])
            # Dynamic budget scaling: B = 18 + 5N (Amendment D)
            n_wos = max(1, len(state.published_wo_ids or state.worker_wo_ids))
            dynamic_budget = 18 + 5 * n_wos
            state.initial_turn_budget = dynamic_budget
            if state.global_turn_budget < dynamic_budget:
                state.global_turn_budget = dynamic_budget
            self._sync_budget_file(state)
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
            except OperationContentionError:
                # Session is currently occupied by another turn. Let advance() wait.
                pass
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
        turns_dispatched = 0
        active_in_flight_ops = 0

        # Check turn threads on manager if available
        if hasattr(self.manager, "_turn_threads"):
            active_in_flight_ops += sum(1 for t in getattr(self.manager, "_turn_threads", {}).values() if t.is_alive())

        # S6 Concurrency: Reconcile batch operation if one is tracked.
        # If all its child operations have reached terminal state, finalize the container
        # operation so the session active operation is cleared before subsequent dispatches.
        if state.batch_operation_id:
            batch_op = self._get_operation(state.batch_operation_id)
            if batch_op is None or batch_op.get("status") in _OPERATION_TERMINAL:
                state.batch_operation_id = None
            else:
                children = batch_op.get("children", [])
                if children:
                    all_term = True
                    any_fail = False
                    for cid in children:
                        cop = self._get_operation(cid)
                        if cop is None or cop.get("status") not in _OPERATION_TERMINAL:
                            all_term = False
                            break
                        if cop.get("status") == "FAILED":
                            any_fail = True
                    if all_term:
                        self._close_batch_operation(state, "FAILED" if any_fail else "COMPLETED")

        if state.dependency_escalation_op_id:
            escalation = self._poll_dependency_escalation(state, ws)
            if escalation is not None:
                return escalation
            if state.dependency_escalation_op_id:
                active_in_flight_ops += 1

        if not state.worker_wo_ids:
            self._discover_worker_wos(state)

        # Ensure any uncompleted QA rework targets are tracked as worker WOs
        # and unarchived on disk if they were completed previously.
        for qa_id, targets in list(state.qa_rework_targets.items()):
            for target_id in targets:
                if target_id not in state.completed_wo_ids:
                    if target_id not in state.worker_wo_ids:
                        state.worker_wo_ids.append(target_id)
                    if state.published_wo_ids and target_id not in state.published_wo_ids:
                        state.published_wo_ids.append(target_id)
                    self._unarchive_work_order(ws, target_id)

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
                if (
                    wo_id in state.blocked_by_escalation
                    or wo_id in state.blocked_wo_ids
                    or wo_id == state.recovery_wo_id
                    or state.recovery_attempts.get(wo_id, 0) > 0
                ):
                    all_done = False
                    continue
                pending_rework = state.qa_rework_targets.get(wo_id)
                if pending_rework:
                    if all(t in state.completed_wo_ids for t in pending_rework):
                        # Developer rework landed — the QA work order may
                        # re-run against the fixed deliverables.
                        state.qa_rework_targets.pop(wo_id, None)
                    else:
                        # QA defect rework still in flight: re-running QA on
                        # the unfixed deliverables would just re-find the bug.
                        all_done = False
                        continue
                if self._dependencies_met(state, wo_id, ws):
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
                    if wo_id not in state.blocked_wo_ids:
                        if active_in_flight_ops > 0 and not state.batch_operation_id:
                            all_done = False
                            continue
                        agent = self._agent_for_wo(wo_id, ws)
                        try:
                            self.manager.start_turn(
                                state.session_id,
                                self._worker_dispatch_prompt(state, wo_id, ws),
                                role=self._role_for_agent(agent),
                                agent_id=agent,
                                work_order_id=wo_id,
                                parent_operation_id=state.batch_operation_id,
                            )
                            turns_dispatched += 1
                        except OperationContentionError:
                            all_done = False
                            continue
                        except Exception:
                            state.blocked_wo_ids.append(wo_id)
                all_done = False
                continue

            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                active_in_flight_ops += 1
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
                retries = state.worker_retries.get(wo_id, 0)
                if retries >= state.max_worker_retries:
                    state.error = f"Work order {wo_id} exceeded worker retries ({retries}/{state.max_worker_retries})"
                    state.exit_code = 3
                    self.escalate_wo_to_recovery(state, wo_id, reason=state.error)
                    self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
                    return AdvanceResult.TRANSITIONED
                state.worker_retries[wo_id] = retries + 1
                if self._retry_worker(state, ws, wo_id, lambda n, a: f"Retry work order {wo_id} (attempt {n})"):
                    turns_dispatched += 1
                    all_done = False
                continue

            # Verify declared deliverable exists and is valid on disk (S3)
            deliv_passed, deliv_err, deliv_evidence = self._verify_work_order_deliverables(wo_id, ws)
            if not deliv_passed:
                if deliv_evidence:
                    state.worker_blockers[wo_id] = {
                        "failure_code": "DELIVERABLE_VERIFICATION_FAILED",
                        "work_order_id": wo_id,
                        "canonical_message": deliv_err,
                        "evidence": deliv_evidence,
                    }
                def _missing_prompt(n: int, agent: str) -> str:
                    prompt = f"Retry work order {wo_id}: {deliv_err} (attempt {n})"
                    if agent == "gemma":
                        deliv_path = self._get_wo_deliverable_path(wo_id, ws) or "the declared test suite"
                        prompt += (
                            f". You MUST author the test suite '{deliv_path}' with write_file using standard Python "
                            f"(pytest, html.parser, re, pathlib; do NOT import external browser drivers like playwright/selenium), "
                            f"asserting semantic goal requirements (user text, IDs, classes) rather than superficial file existence, "
                            f"execute it end-to-end with the run_tests tool (pytest), and immediately "
                            f"after run_tests run the run_security_scan tool over the deliverables. "
                            f"If the tests fail because an application deliverable is defective, do NOT "
                            f"rewrite the suite repeatedly: write a NEEDS_CHANGES verdict to "
                            f".sync/inbox/claude/<TARGET-WORK-ORDER-ID>-qa-verdict.md (e.g. WO-001-qa-verdict.md) naming the defective "
                            f"deliverable, then finalize declaring status 'blocked' with a blocker "
                            f"naming that deliverable path"
                        )
                    return prompt

                if self._retry_worker(state, ws, wo_id, _missing_prompt):
                    turns_dispatched += 1
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

                # If QA authored defect verdicts targeting other deliverables,
                # route rework to the owning developers even if the turn was
                # finalized as 'completed'.
                verdict_hits = self._find_qa_defect_verdicts(ws, wo_id, state=state)
                if verdict_hits:
                    routed = self._route_qa_defect_to_developers(
                        state, ws, wo_id, op, "", "",
                    )
                    if routed is not None:
                        return routed

            # Completed with deliverable on disk — check QA verdict (developer work orders only;
            # QA work orders produce verdicts and are never subject to QA review on themselves).
            if (
                self._role_for_agent(self._agent_for_wo(wo_id, ws)) != "qa"
                and self._check_qa_verdict(wo_id, ws) == "NEEDS_CHANGES"
            ):
                reworks = state.qa_reworks.get(wo_id, 0)
                if reworks >= state.max_qa_reworks:
                    state.error = f"Work order {wo_id} exceeded QA rework limit ({reworks}/{state.max_qa_reworks})"
                    state.exit_code = 3
                    self.escalate_wo_to_recovery(state, wo_id, reason=state.error)
                    self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
                    return AdvanceResult.TRANSITIONED

                state.qa_reworks[wo_id] = reworks + 1

                def _rework_prompt(n: int, agent: str) -> str:
                    feedback = self._get_qa_feedback(wo_id, ws)
                    head = f"Rework work order {wo_id} after QA feedback (attempt {n})"
                    return f"{head}:\n{feedback}" if feedback else head

                if self._retry_worker(state, ws, wo_id, _rework_prompt):
                    turns_dispatched += 1
                    all_done = False
                continue

            # ── Mandatory QA review gate ──────────────────────────────────
            # A backend/frontend deliverable that produces application code or
            # user-facing behaviour is NEVER accepted on the worker's completion
            # claim alone.  The persisted, revision-bound QA verdict is
            # authoritative; the worker cannot write it (its contract does not
            # authorise the review channel).
            from validators.harness import review_gate as _review_gate
            if (
                _review_gate.requires_qa(ws, wo_id)
                and self._role_for_agent(self._agent_for_wo(wo_id, ws)) != "qa"
            ):
                qa_decision = _review_gate.evaluate_qa_gate(ws, wo_id)
                if not qa_decision.passed:
                    qa_outcome = self._handle_qa_gate_pending(state, ws, wo_id, qa_decision)
                    if qa_outcome is not None:
                        return qa_outcome
                    all_done = False
                    continue

            # QA gate passed (or not applicable): the verified deliverable
            # completes the work order.
            if wo_id not in state.completed_wo_ids:
                state.completed_wo_ids.append(wo_id)
                self._mark_wo_status_on_disk(ws, wo_id, "COMPLETED")
            state.qa_review_operations.pop(wo_id, None)
            state.worker_blockers.pop(wo_id, None)

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
            # Reconcile planning WO-000 to COMPLETED before entering INTEGRATION_REVIEW
            for p_id in (state.planning_wo_id, "WO-000"):
                if p_id:
                    if p_id not in state.completed_wo_ids:
                        state.completed_wo_ids.insert(0, p_id)
                    self._mark_wo_status_on_disk(ws, p_id, "COMPLETED")
            archive_completed_work_orders(ws, [state.planning_wo_id or "WO-000", "WO-000"])
            self._transition(state, Phase.INTEGRATION_REVIEW)
            return AdvanceResult.TRANSITIONED

        # Idle stall condition (C_stall):
        # When all remaining work orders are blocked or waiting on unmet dependencies with no turns
        # in progress, return WAITING_FOR_OPERATION so the generalized C_stall in advance() tracks
        # consecutive ticks and transitions to WAITING_FOR_HUMAN upon reaching max_stall_ticks.
        if not all_done and turns_dispatched == 0 and active_in_flight_ops == 0 and not state.dependency_escalation_op_id:
            return AdvanceResult.WAITING_FOR_OPERATION

        return AdvanceResult.WAITING_FOR_OPERATION

    def _advance_complete(self, state: RunState) -> AdvanceResult:
        """COMPLETE is a terminal state only while the acceptance invariant holds.

        Enforced at the authoritative completion boundary rather than in a
        reporting layer: if a completed run's plan, deliverables, QA verdicts or
        final-review evidence are missing or stale, the run is not successful.
        """
        ws = Path(state.workspace)
        ok, _report, why = self._final_acceptance_status(state, ws)
        if ok:
            return AdvanceResult.COMPLETE
        state.error = f"FINAL_ACCEPTANCE_INVARIANT_VIOLATION: {why}"
        state.final_review_history.append(
            {
                "event": "complete_invariant_violation",
                "reason": why,
                "phase": Phase.COMPLETE.value,
                "timestamp": _now(),
            }
        )
        self._transition(state, Phase.FINAL_REVIEW)
        return AdvanceResult.TRANSITIONED

    def _advance_product_ready(self, state: RunState) -> AdvanceResult:
        """PRODUCT_READY: discover or create GitOps WO and dispatch."""
        ws = Path(state.workspace)
        self._ensure_release_documentation(ws, state)

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
