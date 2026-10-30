"""Architect repair loop, recovery decisions, escalation channels and recovery bookkeeping.

Part of the :mod:`validators.kernel.daemon.supervisor` package.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from validators.clock import now as _now

from .models import (
    _OPERATION_TERMINAL,
    TRANSIENT_RETRYABLE_CODES,
    AdvanceResult,
    OperationContentionError,
    Phase,
    RunState,
    _extract_op_error,
)

logger = logging.getLogger("stackmind.supervisor")

class _RecoveryMixin:
    """Architect repair loop, recovery decisions, escalation channels and recovery bookkeeping."""

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
                    err_cause = _extract_op_error(op)
                    detail = f" [{err_cause}]" if err_cause else f" ({status})"
                    state.error = (
                        f"Architect repair turn failed{detail} and repair attempts are exhausted "
                        f"({state.authoring_repair_attempts}/{state.max_authoring_repairs}); "
                        f"operator intervention required"
                    )
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.architect_repair_operation_id = None
                new_op = self._dispatch_repair_turn(state, ws)
                if new_op is None:
                    state.error = (
                        f"Architect repair turn could not be re-dispatched: "
                        f"{getattr(state, 'last_dispatch_error', None) or 'unknown precondition failure'}"
                    )
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
                    state.error = (
                        f"Architect repair turn could not be re-dispatched: {post_check_error} "
                        f"(dispatch: {getattr(state, 'last_dispatch_error', None) or 'unknown'})"
                    )
                    self._transition(state, Phase.BLOCKED)
                    return AdvanceResult.BLOCKED
                state.architect_repair_operation_id = new_op.get("operation_id")
                return AdvanceResult.WAITING_FOR_OPERATION

            if source == "recovery" and action in ("architect_fix", "resolve_blocker"):
                is_deadlock = (
                    state.repair_context.get("is_dependency_deadlock")
                    or (state.worker_blockers.get(str(wo_id)) or {}).get("failure_code") == "DEPENDENCY_DEADLOCK"
                )
                if is_deadlock:
                    state.repair_context = {}
                    state.error = None
                    state.architect_repair_operation_id = None
                    state.authoring_repair_attempts = 0
                    self._clear_worker_blocker(state, ws, str(wo_id), redispatch=True)
                    self._transition(state, Phase.EXECUTING)
                    return AdvanceResult.WAITING_FOR_OPERATION

                # Deliverable fix verified on disk — complete the work order and return to execution!
                self._mark_wo_status_on_disk(ws, str(wo_id), "COMPLETED", clear_error=True)
                if str(wo_id) not in state.completed_wo_ids:
                    state.completed_wo_ids.append(str(wo_id))
                if str(wo_id) in state.failed_wo_ids:
                    state.failed_wo_ids.remove(str(wo_id))
                if str(wo_id) in state.blocked_wo_ids:
                    state.blocked_wo_ids.remove(str(wo_id))
                state.repair_context = {}
                state.error = None
                state.architect_repair_operation_id = None
                state.authoring_repair_attempts = 0
                self._clear_worker_blocker(state, ws, str(wo_id), redispatch=False)
                self._transition(state, Phase.EXECUTING)
                return AdvanceResult.WAITING_FOR_OPERATION

            # Revalidate the complete artifact set with the readiness gate.
            try:
                from ..authoring import reconcile_active_work_orders_with_plan
                reconcile_active_work_orders_with_plan(ws)
            except Exception:
                pass
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
                    state.error = (
                        f"Readiness still failing ({issue_summary}) and repair could not be re-dispatched: "
                        f"{getattr(state, 'last_dispatch_error', None) or 'unknown'}"
                    )
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
            state.authoring_repair_attempts = 0
            if source == "recovery" and wo_id:
                action = str(action or "")
                if action in ("architect_fix", "resolve_blocker"):
                    is_deadlock = (
                        state.repair_context.get("is_dependency_deadlock")
                        or (state.worker_blockers.get(str(wo_id)) or {}).get("failure_code") == "DEPENDENCY_DEADLOCK"
                    )
                    if is_deadlock:
                        state.repair_context = {}
                        self._clear_worker_blocker(state, ws, str(wo_id), redispatch=True)
                        self._transition(state, Phase.EXECUTING)
                        return AdvanceResult.WAITING_FOR_OPERATION
                    self._mark_wo_status_on_disk(ws, str(wo_id), "COMPLETED", clear_error=True)
                    if str(wo_id) not in state.completed_wo_ids:
                        state.completed_wo_ids.append(str(wo_id))
                    if str(wo_id) in state.failed_wo_ids:
                        state.failed_wo_ids.remove(str(wo_id))
                    if str(wo_id) in state.blocked_wo_ids:
                        state.blocked_wo_ids.remove(str(wo_id))
                    state.repair_context = {}
                    self._clear_worker_blocker(state, ws, str(wo_id), redispatch=False)
                    self._transition(state, Phase.EXECUTING)
                    return AdvanceResult.WAITING_FOR_OPERATION
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
        if not state.repair_context or not state.repair_context.get("work_order_id"):
            if state.recovery_wo_id:
                state.repair_context = {"work_order_id": state.recovery_wo_id}
            elif state.worker_blockers:
                state.repair_context = {"work_order_id": next(iter(state.worker_blockers.keys()))}
            else:
                blocked_dir = ws / ".sync" / "work-orders" / "BLOCKED"
                if blocked_dir.exists():
                    blocked_files = [f.stem for f in blocked_dir.glob("*.yaml") if f.stem.startswith("WO-")]
                    if blocked_files:
                        state.repair_context = {"work_order_id": sorted(blocked_files)[0]}
        new_op = self._dispatch_repair_turn(state, ws)
        if new_op is None:
            state.error = (
                f"Architect repair operation was lost and could not be re-dispatched: "
                f"{getattr(state, 'last_dispatch_error', None) or 'unknown precondition failure'}"
            )
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

        # Decision operation lost (crash/resume) — check if decision file already exists on disk,
        # otherwise re-dispatch from stored evidence.
        if not wo_id:
            if state.repair_context and state.repair_context.get("work_order_id"):
                wo_id = str(state.repair_context["work_order_id"])
                state.recovery_wo_id = wo_id
            elif state.worker_blockers:
                wo_id = next(iter(state.worker_blockers.keys()))
                state.recovery_wo_id = wo_id
            else:
                blocked_dir = ws / ".sync" / "work-orders" / "BLOCKED"
                if blocked_dir.exists():
                    blocked_files = [f.stem for f in blocked_dir.glob("*.yaml") if f.stem.startswith("WO-")]
                    if blocked_files:
                        wo_id = sorted(blocked_files)[0]
                        state.recovery_wo_id = wo_id

        if wo_id:
            decision, decision_error = self._load_recovery_decision(ws, str(wo_id), {})
            if decision and decision_error is None:
                advance, apply_error = self._apply_recovery_decision(
                    state, ws, str(wo_id), decision
                )
                if apply_error is None:
                    return advance if advance is not None else AdvanceResult.WAITING_FOR_OPERATION
            new_op = self._dispatch_recovery_decision(state, ws, str(wo_id), None)
            if new_op is not None:
                state.recovery_decision_operation_id = new_op.get("operation_id")
                return AdvanceResult.WAITING_FOR_OPERATION
            # Clean contention fallback: clear session active_operation and retry once before failing
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
                new_op = self._dispatch_recovery_decision(state, ws, str(wo_id), None)
                if new_op is not None:
                    state.recovery_decision_operation_id = new_op.get("operation_id")
                    return AdvanceResult.WAITING_FOR_OPERATION
            except Exception:
                pass
        state.error = "Recovery decision operation was lost and could not be re-dispatched"
        self._transition(state, Phase.BLOCKED)
        return AdvanceResult.BLOCKED

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
        state.authoring_revision += 1
        affected = self._archive_rejected_artifacts(state, ws) if archive_failed_artifacts else []
        state.repair_context["affected_artifacts"] = affected
        state.authoring_repair_attempts += 1
        op = self._dispatch_repair_turn(state, ws)
        if op is None:
            state.error = (
                f"{reason} — failed to dispatch the Architect repair turn: "
                f"{getattr(state, 'last_dispatch_error', None) or 'unknown'}"
            )
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
            wo_id = state.repair_context.get("work_order")
            action = state.repair_context.get("action")
            if wo_id:
                contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
                needs_auth = not contract_file.is_file()
                if not needs_auth and action == "amend_contract":
                    try:
                        c_data = yaml.safe_load(contract_file.read_text(encoding="utf-8")) or {}
                        if c_data.get("synthesized_by") != "recovery-repair" or c_data.get("agent_id") != "claude":
                            needs_auth = True
                    except Exception:
                        needs_auth = True
                if needs_auth:
                    self._write_recovery_repair_authorization(
                        ws,
                        str(wo_id),
                        str(action or ""),
                        state.repair_context.get("original_contract") or {},
                        state=state,
                    )
            prompt = self._recovery_repair_prompt(state)
        else:
            prompt = self._authoring_repair_prompt(state)
        wo_id = state.repair_context.get("work_order") or state.planning_wo_id or "WO-000"
        return self._architect_turn(
            state, prompt, wo_id, is_repair=True,
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
        orig = ctx.get("original_contract") or {}
        orig_allow = [
            r.get("module") for r in (orig.get("scope", {}).get("allow") or [])
            if isinstance(r, dict) and r.get("module")
        ]
        orig_agent = orig.get("agent_id") or "worker"
        existing_rules_str = (
            f"\nExisting allowed modules for {orig_agent} on {wo_id}:\n"
            + "".join(f"  - {m}\n" for m in orig_allow)
            if orig_allow else ""
        )
        return (
            f"ARCHITECT RECOVERY REPAIR for work order {wo_id} (approved action: {action}).\n\n"
            f"Recovery decision:\n{decision_lines}\n\n"
            f"Post-repair validation error: {post_check_error or 'none yet (first application pass)'}\n\n"
            "Apply the decision by authoring governed artifacts with write_file:\n"
            + (
                f"- architect_fix: You are Lead Architect directly resolving the blocker on {wo_id}.\n"
                f"Blocker details:\n{yaml.safe_dump(ctx.get('evidence') or {}, sort_keys=False).strip()}\n"
                + (
                    "CRITICAL: The blocker is a DEPENDENCY_DEADLOCK. Directly edit the work order YAML in "
                    f".sync/work-orders/ACTIVE/{wo_id}.yaml and update .sync/work-orders/INDEX.yaml using write_file "
                    "to remove or fix the deadlocked dependency, unblocking the worker execution.\n"
                    if (ctx.get("is_dependency_deadlock") or (ctx.get("evidence") or {}).get("failure_code") == "DEPENDENCY_DEADLOCK")
                    else f"Target deliverable: {ctx.get('deliverable_path') or 'declared deliverables'}\nDirectly repair the deliverable and any related workspace files with write_file so that all requirements and tests pass.\n"
                )
                if action in ("architect_fix", "resolve_blocker") else ""
            )
            + (
                f"- amend_contract: write the amended contract to .sync/contracts/<WO-ID>.yaml "
                f"conforming to schemas/contract.schema.json. The budget must be explicitly sized "
                f"from the task's real file footprint (ensure max_files_touched is at least 5 or sized to accommodate all files touched plus test/db fixtures). "
                f"Preserve existing allowed paths (including the work order deliverable) and append the new path requested by the recovery decision.{existing_rules_str}"
                f"Contract schema requirements: schema_version: 1 (integer), agent_id, work_order, identity: {{role: ..., reports_to: ...}}, "
                f"scope: allow must be a list of rule mappings (e.g. - module: 'tests/**'), deny must be a list of rule mappings (e.g. - module: 'gacche.db'), "
                f"write must be the string 'read-write', budget: {{max_files_touched: 10, max_tokens: 50000}}. Optional: contract_revision, guidelines. "
                f"A transitional repair contract is already in place at that path — overwrite it with the amended contract; do not delete it.\n"
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
            + ("\nDo not modify unrelated work orders or contracts.\n" if action in ("architect_fix", "resolve_blocker") else "\nDo not modify unrelated work orders, contracts, or application code.\n")
            + "Return the final HarnessDecision JSON with status 'completed' and modified_files "
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
        milestone_table_block = ""
        try:
            from validators.harness.authoring_readiness import load_plan_milestones

            plan_milestones = load_plan_milestones(Path(state.workspace))
            if plan_milestones:
                milestone_table = "\n".join(
                    f"  - {ref['id']}: '{ref['title']}'"
                    + (f" (agent: {ref['agent']})" if ref.get("agent") else "")
                    for ref in plan_milestones
                )
                milestone_table_block = (
                    "Approved plan milestones (set `milestone_id` on each work "
                    f"order to the matching id):\n{milestone_table}\n\n"
                )
        except Exception:
            pass

        collision_directive = ""
        is_collision = False
        if state.error and ("multiple state directories" in state.error or "does not match INDEX.yaml" in state.error or "exists in multiple" in state.error):
            is_collision = True
        for issue in getattr(state, "readiness_issues", []):
            if isinstance(issue, dict) and any(kw in str(issue.get("message", "")) for kw in ("multiple state directories", "does not match INDEX.yaml", "exists in multiple")):
                is_collision = True
                break
        if is_collision:
            collision_directive = (
                "CRITICAL ANTI-COLLISION DIRECTIVE:\n"
                "The previous authoring attempt wrote Work Order IDs that collide with completed work orders (e.g. WO-001).\n"
                "Work Order IDs must be globally unique across ALL state directories. Use the allocated IDs matching your approved milestones.\n\n"
            )

        return (
            f"ARCHITECT AUTHORING REPAIR (revision {state.authoring_revision}, "
            f"attempt {state.authoring_repair_attempts}/{state.max_authoring_repairs}).\n\n"
            f"The previous authoring attempt for plan '{state.plan_id or 'PLAN-001'}' was "
            f"rejected fail-closed and was NOT dispatched to any worker.\n"
            f"Failure: {state.error}\n\n"
            f"{collision_directive}"
            f"{milestone_table_block}"
            f"Readiness diagnostics (bounded):\n{issue_lines or '- (operation-level failure; no artifact diagnostics)'}\n\n"
            f"{affected_intro}\n{affected_lines}\n\n"
            f"CRITICAL REVISION & SCHEMA SHAPES (follow exactly):\n"
            f"- Set `revision: {state.authoring_revision}` on every rewritten Work Order, and append an amendments entry:\n"
            f"    revision: {state.authoring_revision}\n"
            f"    amendments:\n"
            f"      - revision: {state.authoring_revision}\n"
            f"        reason: \"Address authoring readiness gate findings\"\n"
            f"- Set `contract_revision: {state.authoring_revision}` on every rewritten Contract.\n"
            f"- Work Order `test_plan` MUST be an array of objects mapping sources to tests:\n"
            f"    test_plan:\n"
            f"      - sources:\n"
            f"          - <code_deliverable_path>\n"
            f"        tests:\n"
            f"          - tests/test_<stem>.py\n"
            f"- Work Order `implementation_estimate` MUST declare expected_files and max_files_touched:\n"
            f"    implementation_estimate:\n"
            f"      expected_files:\n"
            f"        - <file1>\n"
            f"        - <file2>\n"
            f"      max_files_touched: <int >= count of expected_files>\n"
            f"- Contract `budget` MUST cover the estimate:\n"
            f"    budget:\n"
            f"      max_files_touched: <int >= implementation_estimate.max_files_touched>\n"
            f"      max_tokens: 60000\n\n"
            f"Correct each diagnostic by following its required_action:\n"
            f"1. Re-write each affected Work Order YAML to .sync/work-orders/ACTIVE/<WO-ID>.yaml "
            f"conforming to schemas/work-order.schema.json (ensure `milestone_id` is set to the matching approved milestone ID and revision={state.authoring_revision}).\n"
            f"2. Re-write each affected or missing Contract YAML to .sync/contracts/<WO-ID>.yaml "
            f"conforming to schemas/contract.schema.json (ensure contract_revision={state.authoring_revision}).\n"
            f"3. Explicitly size every contract budget: declare implementation_estimate "
            f"(expected_files, max_files_touched) in the work order and set the contract "
            f"max_files_touched to cover it.\n"
            f"4. Every code deliverable must declare a `test_plan` mapping it to test "
            f"artifact(s) (e.g. tests/test_<stem>.py, or a consolidated suite listing "
            f"the sources it covers).\n"
            f"5. Return the final HarnessDecision JSON with status 'completed' and modified_files "
            f"listing the files you wrote."
        )

    def _architect_turn(
        self, state: RunState, prompt: str, wo_id: str, **flags: Any,
    ) -> dict[str, Any] | None:
        """Start a governed Architect authoring turn; None when it cannot start.

        Records the concrete dispatch failure reason on *state* so that callers
        can surface an actionable diagnostic instead of a generic "could not be
        dispatched" message.
        """
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
        except Exception as first_exc:
            first_reason = f"{type(first_exc).__name__}: {first_exc}"
            # Operation contention or session lock - clear any orphaned/stale active operation and retry once
            try:
                session = self.manager.get_session(state.session_id)
                if session and session.get("state") in {"CANCELLED", "PAUSED", "WAITING", "BLOCKED", "FAILED"}:
                    session["state"] = "RUNNING"
                    session["updated_at"] = _now()
                active_op = session.get("active_operation")
                if active_op:
                    try:
                        self.manager.cancel_operation(active_op)
                    except Exception:
                        pass
                    if hasattr(self.manager, "_active"):
                        self.manager._active.pop(active_op, None)
                    session["active_operation"] = None
                return self.manager.start_turn(
                    state.session_id,
                    prompt,
                    role="architecture",
                    agent_id="claude",
                    work_order_id=wo_id,
                    is_authoring=True,
                    **flags,
                )
            except Exception as retry_exc:
                first_reason = (
                    f"{type(first_exc).__name__}: {first_exc}; "
                    f"retry failed: {type(retry_exc).__name__}: {retry_exc}"
                )
            # Preserve actionable diagnostic on state for the caller
            state.last_dispatch_error = first_reason
            return None

    def _verify_repair_outcome(
        self, state: RunState, ws: Path, action: Any, wo_id: Any,
    ) -> str | None:
        """Enforce action-specific expectations after a recovery repair turn."""
        if not action or not wo_id:
            return None  # plain authoring repair — readiness gate is the arbiter
        wo_id = str(wo_id)
        if action in ("architect_fix", "resolve_blocker"):
            is_deadlock = (
                state.repair_context.get("is_dependency_deadlock")
                or (state.worker_blockers.get(wo_id) or {}).get("failure_code") == "DEPENDENCY_DEADLOCK"
            )
            if is_deadlock:
                wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
                if not wo_file.is_file():
                    return f"architect_fix expected work order '{wo_id}.yaml' to exist in .sync/work-orders/ACTIVE/"
                return None
            deliv_path = state.repair_context.get("deliverable_path") or self._get_wo_deliverable_path(wo_id, ws)
            if deliv_path:
                target_file = ws / deliv_path
                if target_file.is_dir():
                    if not any(f.is_file() for f in target_file.rglob("*")):
                        return f"architect_fix expected deliverable directory '{deliv_path}' to contain files"
                elif not target_file.is_file():
                    return f"architect_fix expected deliverable '{deliv_path}' to exist on disk"
                elif target_file.stat().st_size == 0:
                    return f"architect_fix deliverable '{deliv_path}' is empty"
            return None
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
            deny_list = scope.get("deny") or []
            filtered_deny = [
                r for r in deny_list
                if not (
                    (isinstance(r, dict) and r.get("module") in (".sync/**", ".sync/*", ".sync", ".sync/decisions/**", ".sync/decisions/*"))
                    or (isinstance(r, str) and r in (".sync/**", ".sync/*", ".sync", ".sync/decisions/**", ".sync/decisions/*"))
                )
            ]
            if len(filtered_deny) != len(deny_list):
                scope["deny"] = filtered_deny
                updated = True
            if updated:
                contract_file.write_text(
                    yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
                )
        except Exception:
            pass

    def _ensure_dependency_escalation_channel(self, ws: Path, wo_id: str) -> None:
        """Grant dependency manifest and inbox channels on the contract for architecture escalation.

        Deterministic supervisor bookkeeping so Architecture (Claude) can update
        requirements.txt / pyproject.toml or direct the worker via .sync/inbox/.
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
            existing_allow = {
                r.get("module") for r in allow_list if isinstance(r, dict)
            }
            deny_list = scope.get("deny") or []
            blocked_deny = {
                "requirements.txt", "pyproject.toml",
                ".sync/**", ".sync/*", ".sync",
                ".sync/inbox/**", ".sync/inbox/*",
            }
            filtered_deny = [
                r for r in deny_list
                if not (isinstance(r, dict) and r.get("module") in blocked_deny)
                and not (isinstance(r, str) and r in blocked_deny)
            ]
            scope["deny"] = filtered_deny
            needed = ["requirements.txt", "pyproject.toml", ".sync/inbox/**"]
            updated = False
            for n in needed:
                if n not in existing_allow:
                    allow_list.append({"module": n})
                    updated = True
            if updated or len(filtered_deny) != len(deny_list):
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
        current_rev = state.contract_revisions.get(wo_id, 1)
        is_deadlock = (evidence.get("failure_code") == "DEPENDENCY_DEADLOCK")
        deadlock_note = (
            "\nCRITICAL: A DEPENDENCY DEADLOCK occurred. As Senior Architect you can:\n"
            "- architect_fix or resolve_blocker: Author repair to .sync/work-orders/ACTIVE/ (and INDEX.yaml) "
            "to remove inverted or circular dependencies or update prerequisite references, unblocking the worker.\n"
            "- cancel_work_order: Cancel an orphaned or obsolete work order.\n"
            "- split_work_order: Decompose the blocked work order into independent tasks.\n\n"
            if is_deadlock else ""
        )
        return (
            f"ARCHITECT RECOVERY DECISION required for work order {wo_id}.\n\n"
            + deadlock_note
            + "A governed worker execution was blocked. "
            "Bounded evidence packet:\n"
            f"- failure_code: {evidence.get('failure_code', 'UNKNOWN')}\n"
            f"- failure: {evidence.get('canonical_message') or evidence.get('raw_reason') or 'unspecified'}\n"
            f"- current contract revision: {current_rev} "
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
            "2. Permitted actions: architect_fix (RECOMMENDED for defects, failing deliverables, or broken tests — the Architect directly repairs the deliverable files in the follow-up repair turn), "
            f"retry_unchanged (ONLY for transient failures with an unchanged contract), "
            f"amend_contract (set contract_revision to {current_rev + 1} or higher; current revision is {current_rev}; the amended contract is authored in the follow-up repair turn), "
            "split_work_order (list replacement_work_orders child IDs), create_dependency_work_order (list new prerequisite WO IDs), "
            "cancel_work_order (cancel invalid or orphaned work order), escalate_human, terminal_block.\n"
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

        if action in ("architect_fix", "resolve_blocker"):
            original_contract: dict[str, Any] = {}
            contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
            if contract_file.is_file():
                try:
                    loaded = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        original_contract = loaded
                except Exception:
                    pass
            if not original_contract:
                original_contract = self._find_latest_contract_for_wo(ws, state.run_id, wo_id)
            deliv_path = self._get_wo_deliverable_path(wo_id, ws)
            state.repair_context = {
                "source": "recovery",
                "action": action,
                "work_order": wo_id,
                "decision": decision,
                "original_contract": original_contract,
                "deliverable_path": deliv_path,
                "evidence": evidence,
                "is_dependency_deadlock": (evidence.get("failure_code") == "DEPENDENCY_DEADLOCK"),
            }
            return self._enter_recovery_repair(state, ws, record)

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
            raw_rev = decision.get("contract_revision")
            if isinstance(raw_rev, int) and raw_rev > current_revision:
                new_revision = raw_rev
            else:
                # If the architect omitted, repeated, or failed to advance the revision,
                # auto-advance monotonically to prevent failing closed on recovery.
                new_revision = current_revision + 1
            original_contract: dict[str, Any] = {}
            contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
            if contract_file.is_file():
                try:
                    loaded = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        original_contract = loaded
                except Exception:
                    pass
            if not original_contract:
                original_contract = self._find_latest_contract_for_wo(ws, state.run_id, wo_id)
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
            # Collision guard: replacement children must not reuse the ids of
            # other active work orders — the split archives the blocked WO's
            # artifacts and re-authors the children, so a colliding id would
            # clobber a live, contracted work order mid-execution.
            active_ids = {
                p.stem for p in (ws / ".sync" / "work-orders" / "ACTIVE").glob("*.yaml")
            } if (ws / ".sync" / "work-orders" / "ACTIVE").is_dir() else set()
            colliding = sorted((set(children) & active_ids) - {wo_id})
            if colliding:
                state.recovery_decisions.append({
                    **record, "applied": False,
                    "rejected_reason": (
                        "replacement work orders collide with existing active work orders: "
                        + ", ".join(colliding)
                    ),
                })
                return None, (
                    f"split_work_order replacement ids {', '.join(colliding)} collide with existing "
                    "active work orders; choose fresh ids (not present in .sync/work-orders/ACTIVE) "
                    "for the replacement children, or use amend_contract to adjust the existing "
                    "work order's scope instead"
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
            # Collision guard: a NEW prerequisite work order must use a fresh id —
            # reusing an active work order's id would clobber it.
            active_ids = {
                p.stem for p in (ws / ".sync" / "work-orders" / "ACTIVE").glob("*.yaml")
            } if (ws / ".sync" / "work-orders" / "ACTIVE").is_dir() else set()
            colliding = sorted(set(new_wos) & active_ids)
            if colliding:
                state.recovery_decisions.append({
                    **record, "applied": False,
                    "rejected_reason": (
                        "new prerequisite work orders collide with existing active work orders: "
                        + ", ".join(colliding)
                    ),
                })
                return None, (
                    f"create_dependency_work_order ids {', '.join(colliding)} collide with existing "
                    "active work orders; prerequisite work orders must use fresh ids"
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
            state.repair_context = {
                "source": "recovery",
                "action": action,
                "work_order": wo_id,
                "decision": decision,
                "replacement_work_orders": new_wos,
                "original_contract": original_contract,
            }
            return self._enter_recovery_repair(state, ws, record)

        if action in ("cancel_work_order", "cancel", "prune_untracked"):
            target_wo = str(decision.get("work_order") or wo_id)
            cancel_reason = reason or "Cancelled by Architect recovery decision"
            self._mark_wo_status_on_disk(ws, target_wo, "CANCELLED", error=cancel_reason)
            self._archive_recovery_artifact(ws, state.run_id, f".sync/contracts/{target_wo}.yaml")
            if target_wo not in state.superseded_wo_ids:
                state.superseded_wo_ids.append(target_wo)
            if target_wo in state.worker_wo_ids:
                state.worker_wo_ids.remove(target_wo)
            if target_wo in state.published_wo_ids:
                state.published_wo_ids.remove(target_wo)
            if target_wo in state.blocked_wo_ids:
                state.blocked_wo_ids.remove(target_wo)
            if target_wo in state.failed_wo_ids:
                state.failed_wo_ids.remove(target_wo)
            evidence = state.worker_blockers.pop(target_wo, None) or {}
            old_op_id = evidence.get("operation_id")
            if old_op_id and old_op_id not in state.ignored_operation_ids:
                state.ignored_operation_ids.append(str(old_op_id))
            state.recovery_decisions.append({**record, "applied": True})
            state.error = None
            self._transition(state, Phase.EXECUTING)
            return AdvanceResult.TRANSITIONED, None

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
        state.authoring_repair_attempts = 0
        # Deterministic bookkeeping: place/extend the transitional
        # repair-authorization contract so the repair turn passes pre-execution
        # validation (task WO matches contract WO) and is authorized to write
        # the governance artifacts the approved action must re-author.
        self._write_recovery_repair_authorization(
            ws,
            str(state.repair_context.get("work_order") or ""),
            str(state.repair_context.get("action") or ""),
            state.repair_context.get("original_contract") or {},
            state=state,
        )
        self._archive_stale_inbox_notices(ws)
        state.recovery_decisions.append({**record, "applied": True, "stage": "repair_dispatched"})
        state.error = (
            f"Recovery action '{state.repair_context.get('action')}' for "
            f"{state.repair_context.get('work_order')} requires artifact repair"
        )
        self._transition(state, Phase.ARCHITECT_REPAIR)
        state.authoring_repair_attempts += 1
        op = self._dispatch_repair_turn(state, ws)
        if op is None:
            state.error = (
                f"Recovery repair turn could not be dispatched: "
                f"{getattr(state, 'last_dispatch_error', None) or 'unknown precondition failure'}"
            )
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED, None
        state.architect_repair_operation_id = op.get("operation_id")
        return AdvanceResult.WAITING_FOR_OPERATION, None

    def _find_latest_contract_for_wo(
        self, ws: Path, run_id: str, wo_id: str
    ) -> dict[str, Any]:
        """Find the most recent contract for a work order across live, superseded, and audit paths."""
        c_path = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
        if c_path.is_file():
            try:
                data = yaml.safe_load(c_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        if run_id:
            superseded_dir = ws / ".sync" / "reports" / "authoring" / run_id / "superseded"
            if superseded_dir.is_dir():
                candidates = sorted(
                    superseded_dir.glob(f"{wo_id}*.yaml"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for cand in candidates:
                    try:
                        data = yaml.safe_load(cand.read_text(encoding="utf-8"))
                        if isinstance(data, dict) and data.get("scope"):
                            return data
                    except Exception:
                        pass
            report_dir = ws / ".sync" / "reports" / "authoring" / run_id
            if report_dir.is_dir():
                for rev_dir in sorted(report_dir.glob("rev*"), reverse=True):
                    cand = rev_dir / f"{wo_id}.yaml"
                    if cand.is_file():
                        try:
                            data = yaml.safe_load(cand.read_text(encoding="utf-8"))
                            if isinstance(data, dict) and data.get("scope"):
                                return data
                        except Exception:
                            pass
        return {}

    def _write_recovery_repair_authorization(
        self,
        ws: Path,
        wo_id: str,
        action: str,
        original_contract: dict[str, Any],
        state: RunState | None = None,
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
        if not original_contract:
            run_id = getattr(state, "run_id", "") if state else ""
            original_contract = self._find_latest_contract_for_wo(ws, run_id, wo_id)

        original_scope = original_contract.get("scope") if isinstance(original_contract, dict) else None
        allow = list((original_scope or {}).get("allow") or [])
        deny = list((original_scope or {}).get("deny") or [{"module": ".git/**"}])
        budget = original_contract.get("budget") if isinstance(original_contract, dict) else None

        def _ensure(module: str) -> None:
            if module not in {r.get("module") for r in allow if isinstance(r, dict)}:
                allow.append({"module": module})

        _ensure(f".sync/contracts/{wo_id}.yaml")
        _ensure(".sync/decisions/**")
        _ensure("schemas/**")
        if action == "amend_contract":
            _ensure(".sync/contracts/**")
            _ensure(".sync/work-orders/**")
        elif action in ("split_work_order", "create_dependency_work_order"):
            _ensure(".sync/work-orders/**")
            _ensure(".sync/contracts/**")
        elif action in ("architect_fix", "resolve_blocker"):
            _ensure(".sync/work-orders/**")
            _ensure(".sync/contracts/**")
            _ensure(".sync/decisions/**")
            _ensure("**")
        deny = [r for r in deny if not (isinstance(r, dict) and "schemas" in str(r.get("module", "")))]

        # Also ensure work order's declared deliverable is allowed
        for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
            wo_cand = ws / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
            if wo_cand.is_file():
                try:
                    wo_data = yaml.safe_load(wo_cand.read_text(encoding="utf-8"))
                    if isinstance(wo_data, dict):
                        deliv = wo_data.get("deliverable")
                        if isinstance(deliv, dict) and deliv.get("path"):
                            _ensure(str(deliv["path"]))
                    break
                except Exception:
                    pass

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

        evidence = (state.worker_blockers.get(wo_id) or {}) if state else {}
        observed_count = int(evidence.get("observed_file_count", 0)) if isinstance(evidence, dict) else 0
        if action in ("architect_fix", "resolve_blocker"):
            repair_budget = {"max_files_touched": 25, "max_tokens": 150000}
        elif action == "amend_contract" or (isinstance(evidence, dict) and evidence.get("failure_code") == "CONTRACT_FILE_BUDGET_EXCEEDED"):
            repair_budget = {"max_files_touched": max(10, observed_count + 5), "max_tokens": 50000}
        else:
            repair_budget = (
                budget
                if isinstance(budget, dict) and budget.get("max_files_touched")
                else {"max_files_touched": 10, "max_tokens": 50000}
            )

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
            "budget": repair_budget,
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

    def _archive_stale_inbox_notices(self, ws: Path) -> None:
        """Move stale completion/review notices out of agent inboxes when their
        work order is no longer active.

        ``discover_next_task`` falls back to the first inbox item when an
        explicit work order cannot be resolved (e.g. after a split archives
        the blocked work order); without this hygiene a stale notice redirects
        recovery repair turns into processing it instead of authoring the
        repair artifacts.
        """
        import shutil

        inbox_root = ws / ".sync" / "inbox"
        if not inbox_root.is_dir():
            return
        active_ids = {
            p.stem for p in (ws / ".sync" / "work-orders" / "ACTIVE").glob("*.yaml")
        } if (ws / ".sync" / "work-orders" / "ACTIVE").is_dir() else set()
        for agent_dir in sorted(inbox_root.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("_") or agent_dir.name == "CEO":
                continue
            read_dir = agent_dir / "_read"
            for notice in sorted(agent_dir.glob("*.md")):
                match = re.search(r"(WO-\d+)", notice.name)
                if not match or match.group(1) in active_ids:
                    continue  # no work-order reference, or the WO is still active
                read_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(notice), str(read_dir / notice.name))
                except OSError:
                    pass

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
            load_expected_milestones,
            milestone_matches,
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
        state.retry_counts[wo_id] = 0
        self._archive_verdicts_for_wo(wo_id, ws)
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
            except OperationContentionError:
                return AdvanceResult.WAITING_FOR_OPERATION
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

    def _capture_terminal_crash_bundle(self, state: RunState) -> None:
        """Capture rescue ref in external store and write crash bundle for any terminal FAILED transition."""
        if getattr(self, "reaper", None) is not None:
            try:
                self.reaper.reap_all()
            except Exception:
                pass

        storage_dir = Path(self.state_manager._storage_dir) if hasattr(self, "state_manager") and self.state_manager else Path(state.workspace).parent / "supervisor_storage"
        bundle_dir = storage_dir / "crash_bundles"
        try:
            bundle_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            bundle_path = bundle_dir / f"crash_bundle_{state.run_id}_{ts}.json"
            
            ckpt_hash = None
            if hasattr(self, "state_manager") and self.state_manager is not None:
                ckpt_hash = getattr(self.state_manager, "latest_checkpoint_hash", None)
                if not ckpt_hash and hasattr(self.state_manager, "_in_memory_chain_hashes"):
                    ckpt_hash = self.state_manager._in_memory_chain_hashes.get(state.run_id)

            err_msg = state.error or f"TERMINAL_FAILURE in phase {state.phase.value}"
            bundle_data = {
                "run_id": state.run_id,
                "phase": state.phase.value,
                "error": str(err_msg),
                "exit_code": state.exit_code,
                "budget": state.global_turn_budget,
                "counters": {
                    "worker_retries": dict(state.worker_retries),
                    "qa_reworks": dict(state.qa_reworks),
                    "gitops_retries": state.gitops_retries,
                    "planning_retries": state.planning_retries,
                    "authoring_repair_attempts": state.authoring_repair_attempts,
                    "daemon_restarts": state.daemon_restarts,
                    "human_resumes_used": state.human_resumes_used,
                },
                "checkpoint_hash": ckpt_hash or "none",
                "state_snapshot": state.to_dict(),
                "transitions": list(state.transitions),
                "timestamp": _now(),
            }
            bundle_path.write_text(json.dumps(bundle_data, indent=2), encoding="utf-8")
        except Exception:
            pass

        store_git = storage_dir / "snapshots.git"
        if store_git.is_dir():
            try:
                from validators.kernel.turn_snapshot import TurnSnapshotManager
                snap_mgr = TurnSnapshotManager(workspace=Path(state.workspace), storage_root=store_git, reaper=getattr(self, "reaper", None))
                rescue_snap = snap_mgr.create_turn_snapshot(f"rescue-{state.run_id}")
                null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
                git_env = {
                    **os.environ,
                    "GIT_CONFIG_GLOBAL": null_dev,
                    "GIT_CONFIG_SYSTEM": null_dev,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG": null_dev,
                }
                refs_to_update = [f"refs/rescue/{state.run_id}/failed"]
                if state.interrupted_phase:
                    refs_to_update.append(f"refs/rescue/{state.run_id}/{state.interrupted_phase.lower()}")
                if state.transitions:
                    last_from = state.transitions[-1].get("from", "").lower()
                    if last_from and last_from != "failed":
                        refs_to_update.append(f"refs/rescue/{state.run_id}/{last_from}")
                for ref_name in set(refs_to_update):
                    subprocess.run(
                        ["git", "--git-dir", str(store_git), "update-ref", ref_name, rescue_snap],
                        capture_output=True,
                        check=True,
                        env=git_env,
                    )
            except Exception:
                pass

    def _escalate_idle_stall_to_recovery(self, state: RunState) -> AdvanceResult | None:
        """Escalate an idle stall (due to dependency deadlock or blocked tasks) to the Architect."""
        ws = Path(state.workspace)
        incomplete_wos = [
            wid for wid in state.worker_wo_ids
            if wid not in state.completed_wo_ids and wid not in state.superseded_wo_ids
        ]
        if not incomplete_wos and state.gitops_wo_id and state.gitops_wo_id not in state.completed_wo_ids:
            incomplete_wos = [state.gitops_wo_id]

        if not incomplete_wos:
            return None

        # Determine unmet dependencies for each incomplete work order
        unmet_map: dict[str, list[str]] = {}
        for wid in incomplete_wos:
            deps: list[str] = []
            if wid in state.wo_dependencies:
                deps.extend(state.wo_dependencies[wid])
            wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wid}.yaml"
            if wo_file.is_file():
                try:
                    data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        file_deps = data.get("dependencies") or []
                        if isinstance(file_deps, list):
                            for fd in file_deps:
                                fd_str = str(fd).strip()
                                if fd_str and fd_str not in deps:
                                    deps.append(fd_str)
                except Exception:
                    pass
            unmet = [
                d for d in deps
                if d not in state.completed_wo_ids
                and d != state.planning_wo_id
                and d != "WO-000"
                and d != state.gitops_wo_id
            ]
            unmet_map[wid] = unmet

        # Pick candidate stalled work order: prefer one with unmet dependencies, else first incomplete
        candidates = [wid for wid in incomplete_wos if unmet_map.get(wid)]
        target_wo = candidates[0] if candidates else incomplete_wos[0]

        if not re.match(r"^WO-[0-9]{3,}$", target_wo):
            return None

        attempts = state.recovery_attempts.get("IDLE_STALL", 0) + 1
        state.recovery_attempts["IDLE_STALL"] = attempts
        state.recovery_wo_id = target_wo

        unmet_detail = unmet_map.get(target_wo, [])
        state.worker_blockers[target_wo] = {
            "failure_code": "DEPENDENCY_DEADLOCK",
            "canonical_message": (
                f"Work order {target_wo} is stalled due to unmet dependencies ({unmet_detail}). "
                f"Active incomplete work orders: {incomplete_wos}."
            ),
            "raw_reason": (
                f"IDLE_STALL: Phase {state.phase.value} stalled for {state.stall_ticks} consecutive ticks. "
                f"Incomplete work orders: {incomplete_wos}. Unmet dependencies map: {unmet_map}"
            ),
            "stalled_phase": state.phase.value,
            "unmet_dependencies": unmet_detail,
            "all_unmet_dependencies": unmet_map,
        }
        state.stall_ticks = 0
        state.error = (
            f"IDLE_STALL in phase {state.phase.value}: Work order {target_wo} stalled "
            f"with unmet dependencies ({unmet_detail}). Escalating to Architect recovery decision "
            f"(attempt {attempts}/{state.max_retries})."
        )
        self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
        return AdvanceResult.TRANSITIONED
