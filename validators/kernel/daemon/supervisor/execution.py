"""Work-order dispatch, worker retry, QA gate handling and operation tracking during EXECUTING.

Part of the :mod:`validators.kernel.daemon.supervisor` package.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from validators.clock import now as _now

from .models import (
    _OPERATION_TERMINAL,
    GOVERNANCE_FAILURE_CODES,
    TRANSIENT_RETRYABLE_CODES,
    AdvanceResult,
    OperationContentionError,
    Phase,
    RunState,
)
from .release import (
    archive_completed_work_orders,
)

logger = logging.getLogger("stackmind.supervisor")

class _ExecutionMixin:
    """Work-order dispatch, worker retry, QA gate handling and operation tracking during EXECUTING."""

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
            code = "OUTCOME_NOT_VERIFIED"
            if "code_verified" in err_msg.lower():
                code = "CODE_NOT_VERIFIED"
            elif "state_verified" in err_msg.lower():
                code = "STATE_NOT_VERIFIED"
            elif "security_verified" in err_msg.lower():
                code = "SECURITY_NOT_VERIFIED"
            evidence = {
                "failure_code": code,
                "work_order_id": wo_id,
                "operation_id": op.get("operation_id"),
                "canonical_message": err_msg,
                "scope_evidence": result.get("scope_evidence") or {},
                "contract_revision": state.contract_revisions.get(wo_id, 1),
                "contract_hash": self._sha256_file(ws / ".sync" / "contracts" / f"{wo_id}.yaml"),
            }
            result["failure"] = evidence
            state.worker_blockers[wo_id] = dict(evidence)
        elif evidence is None or not evidence.get("failure_code"):
            code = "WORKER_BLOCKED"
            err_lower = err_msg.lower()
            if "verification gate failed" in err_lower or "outcome_verified" in err_lower:
                code = "OUTCOME_NOT_VERIFIED"
            elif "loop" in err_lower or "halted" in err_lower or "noprogresslooperror" in err_lower:
                code = "LOOP_GUARD_TRIPPED"
            elif "pytest" in err_lower or "test" in err_lower:
                code = "TEST_EXECUTION_FAILED"
            elif "modulenotfounderror" in err_lower or "importerror" in err_lower or "dependency" in err_lower:
                code = "DEPENDENCY_ERROR"
            elif "decision validation failed" in err_lower or "invalid harness output" in err_lower:
                code = "HARNESS_DECISION_INVALID"
            evidence = {
                "failure_code": code,
                "work_order_id": wo_id,
                "operation_id": op.get("operation_id"),
                "canonical_message": err_msg,
                "scope_evidence": result.get("scope_evidence") or {},
                "raw_reason": raw_reason or raw_error or err_msg,
                "contract_revision": state.contract_revisions.get(wo_id, 1),
                "contract_hash": self._sha256_file(ws / ".sync" / "contracts" / f"{wo_id}.yaml"),
            }
            result["failure"] = evidence
            state.worker_blockers[wo_id] = dict(evidence)

        # A QA turn that blocks on failing tests is usually a legitimate defect
        # finding against another work order's deliverable, not a QA failure:
        # the QA contract forbids fixing application code, so retrying QA on
        # the same failing suite can only trip the loop guard again. Route the
        # defect back to the developer who owns the failing deliverable.
        failure_code = str(evidence.get("failure_code")) if evidence else ""
        if (
            self._agent_for_wo(wo_id, ws) == "gemma"
            and failure_code not in GOVERNANCE_FAILURE_CODES
        ):
            routed = self._route_qa_defect_to_developers(
                state, ws, wo_id, op, err_msg, blocker_text,
            )
            if routed is not None:
                return routed

        # Route verified governance failures to an Architect recovery
        # decision — never to an automatic same-contract worker retry.
        # Transient failures keep the bounded auto-retry path until the
        # retry budget is exhausted, then also escalate to the Architect.
        transient_exhausted = (
            (failure_code in TRANSIENT_RETRYABLE_CODES or bool(failure_code))
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
                    state.ignored_operation_ids.append(str(op_id))

                esc_op = self._dispatch_dependency_escalation(state, wo_id, dep_info, ws)
                if esc_op:
                    state.dependency_escalation_op_id = esc_op.get("operation_id")
                    state.dependency_escalation_wo_id = wo_id
                    return AdvanceResult.WAITING_FOR_OPERATION
            else:
                if wo_id not in state.blocked_wo_ids:
                    state.blocked_wo_ids.append(wo_id)
                state.error = (
                    f"Work order {wo_id} blocked: {err_msg} — dependency escalation "
                    f"retries exhausted ({esc_retries}/{state.max_retries})"
                )
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED

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
                except OperationContentionError:
                    # Parallel sibling turns share the session: the nudge cannot
                    # start while a sibling operation is running.  Roll the
                    # retry budget back and let advance() wait — the work order
                    # stays ACTIVE and is re-dispatched once the session frees,
                    # without consuming retry budget on a turn that never ran.
                    state.retry_counts[wo_id] = retries
                    raise
                except Exception as e:
                    logger.error(f"Failed to nudge-retry {wo_id}: {e}", exc_info=True)
                return None  # retry dispatched — keep executing

        # Check if this blockage is a transient outcome_verified, unwritten deliverable, or tool loop halt
        err_lower = err_msg.lower()
        is_transient_blockage = (
            "outcome_verified" in err_lower
            or "security_verified" in err_lower
            or "state_verified" in err_lower
            or ("code_verified" in err_lower and "scope_verified" not in err_lower)
            or "declared deliverable" in err_lower
            or "unfulfilled deliverable" in err_lower
            or "governed tool loop halted" in err_lower
            or "pathological loop" in err_lower
            or "tool loop exhausted" in err_lower
            or "consecutive tool failures" in err_lower
            or "timed out" in err_lower
            or "noprogresslooperror" in err_lower
            or "loop guard" in err_lower
            or "execution error" in err_lower
            or "test" in err_lower
            or "process_status" in err_lower
            or "process_output" in err_lower
            or "modulenotfounderror" in err_lower
            or "importerror" in err_lower
            or "decision validation failed" in err_lower
            or "invalid harness output" in err_lower
            or "harness decision validation failed" in err_lower
            or "release_target is required" in err_lower
        )
        if is_transient_blockage:
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
                            f"Retry work order {wo_id}: previous attempt failed or halted ({err_msg})."
                            f"{evidence_lines} "
                            + (
                                " CRITICAL FIX: Remote image URLs returned 404. Do NOT guess Unsplash/external URLs; use inline SVGs, CSS placeholder containers, or local assets."
                                if ("Remote media asset does not exist" in str(err_msg) or "404 Not Found" in str(err_msg))
                                else ""
                            )
                            + (
                                " CRITICAL FIX: Remove prohibited 'stackmind' references from product files (README, CHANGELOG)."
                                if "Clean delivery gate failed" in str(err_msg)
                                else ""
                            )
                            + " "
                            f"You MUST resolve any reported issues, invoke write_file to author the deliverable{target_str} "
                            "at EXACTLY that path — no renamed or extra-nested folders — and "
                            "declare in modified_files ONLY files you actually wrote with "
                            f"write_file this turn (attempt {retries + 2})"
                        ),
                        role=self._role_for_agent(agent),
                        agent_id=agent,
                        work_order_id=wo_id,
                    )
                    return None
                except OperationContentionError:
                    # Parallel sibling turns share the session: roll the retry
                    # budget back and let advance() wait — the work order stays
                    # ACTIVE and is re-dispatched once the session frees.
                    state.retry_counts[wo_id] = retries
                    raise
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
        """Bounded worker retry: two-tier attempt and retry mechanism.

        Attempts count up to max_attempts (default 10). When attempts are exhausted,
        it counts as 1 retry (1/10 Retry) and resets attempts for the next retry round.
        Exhaustion of both attempts and retries marks the work order failed.
        """
        attempts = state.attempt_counts.get(wo_id, 0) + 1
        retries = state.retry_counts.get(wo_id, 0)

        if retries >= state.max_retries:
            state.failed_wo_ids.append(wo_id)
            return False

        if attempts > state.max_attempts:
            retries += 1
            state.retry_counts[wo_id] = retries
            attempts = 1
            if retries >= state.max_retries:
                state.failed_wo_ids.append(wo_id)
                return False
        elif attempts == state.max_attempts and retries == 0:
            retries = 1
            state.retry_counts[wo_id] = retries

        state.attempt_counts[wo_id] = attempts

        agent = self._agent_for_wo(wo_id, ws)
        try:
            self.manager.start_turn(
                state.session_id,
                make_prompt(attempts + 1, agent),
                role=self._role_for_agent(agent),
                agent_id=agent,
                work_order_id=wo_id,
                parent_operation_id=state.batch_operation_id,
                attempt=attempts,
                max_attempts=state.max_attempts,
                retry=retries,
                max_retries=state.max_retries,
            )
        except OperationContentionError:
            state.attempt_counts[wo_id] = max(0, attempts - 1)
            raise
        except Exception:
            state.failed_wo_ids.append(wo_id)
            return False
        return True

    def _close_batch_operation(self, state: RunState, status: str) -> None:
        batch_id = state.batch_operation_id
        if batch_id:
            if hasattr(self.manager, "complete_operation"):
                try:
                    self.manager.complete_operation(operation_id=batch_id, status=status)
                except Exception:
                    try:
                        self.manager.complete_operation(operation_id=batch_id, status="FAILED")
                    except Exception:
                        pass
            if hasattr(self.manager, "_sessions"):
                sess = getattr(self.manager, "_sessions", {}).get(state.session_id)
                if sess and sess.get("active_operation") == batch_id:
                    sess["active_operation"] = None
            state.batch_operation_id = None

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
            import sys
            os_note = (
                "\nHost OS: Windows. Use list_directory and read_file to inspect application deliverables (server/, src/, index.html); "
                "do NOT run Unix shell commands like 'ls -la'."
                if sys.platform == "win32" else ""
            )
            return (
                f"Execute work order {wo_id} as the QA worker.\n"
                f"1. Inspect existing workspace deliverables (e.g. server/, src/, index.html) with read_file and list_directory to ground your test assertions.{os_note}\n"
                f"2. Author the declared test suite '{deliv_path}' with write_file — thorough "
                f"semantic end-to-end coverage of the deliverables produced by the other work orders, verifying user-requested content and element IDs.\n"
                f"3. Execute the suite end-to-end with the run_tests tool (pytest) and review the results.\n"
                f"3. Run the run_security_scan tool over the deliverables and triage the output; "
                f"report every unresolved finding as a blocker.\n"
                f"4. Do not modify application code; if the suite or the scan exposes defects, report them as blockers.\n"
                f"5. If you write a QA verdict file to .sync/inbox/claude/ (e.g. .sync/inbox/claude/{wo_id}-verdict.txt) with write_file, declare it along with '{deliv_path}' in modified_files. Declare in modified_files ONLY files you actually wrote with write_file.\n"
                "Review against the security checklist: no hardcoded secrets or env-fallback defaults, "
                "debug flags disabled, authentication logic free of obvious flaws, and forms protected "
                "against CSRF/replay where applicable."
            )
        feedback = self._get_qa_feedback(wo_id, ws)
        if feedback:
            return f"Execute work order {wo_id} after QA feedback:\n{feedback}"
        if getattr(state, "integration_blockers", None):
            blockers_str = "\n".join(f"- {b}" for b in state.integration_blockers)
            return (
                f"Execute work order {wo_id} to resolve integration review blockers:\n"
                f"{blockers_str}"
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
            if target.is_dir():
                if not any(f.is_file() for f in target.rglob("*")):
                    return f"declared test suite '{deliv_path}' was not authored"
            elif not target.is_file() or target.stat().st_size == 0:
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
                        f"the test suite '{deliv_path}' with write_file using standard Python "
                        f"(pytest, html.parser, re, pathlib; do NOT import external browser drivers like playwright/selenium), "
                        f"execute it end-to-end with the run_tests tool (pytest), and IMMEDIATELY after run_tests — "
                        f"whatever its outcome — run the run_security_scan tool over the "
                        f"deliverables; never defer or skip the scan because tests failed. "
                        f"If the tests fail because an application deliverable is defective "
                        f"(the test itself is correct), do NOT rewrite or re-run the suite "
                        f"more than once: write a NEEDS_CHANGES verdict to "
                        f".sync/inbox/claude/<TARGET-WORK-ORDER-ID>-qa-verdict.md (e.g. WO-001-qa-verdict.md) naming the "
                        f"defective deliverable, then finalize declaring status 'blocked' "
                        f"with a blocker naming that deliverable path — the supervisor "
                        f"routes the fix to the developer. Declare in modified_files ONLY "
                        f"files you actually wrote with write_file."
                    ),
                    role=self._role_for_agent("gemma"),
                    agent_id="gemma",
                    work_order_id=wo_id,
                )
                return AdvanceResult.WAITING_FOR_OPERATION
            except OperationContentionError:
                # Parallel sibling turn (e.g. developer rework) is active on the session.
                # Roll back retry count and ignored op so advance() waits without consuming a retry.
                state.retry_counts[wo_id] = retries
                if op_id and str(op_id) in state.ignored_operation_ids:
                    state.ignored_operation_ids.remove(str(op_id))
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
            if target_status == "ACTIVE":
                blocked_cand = ws / ".sync" / "work-orders" / "BLOCKED" / f"{wo_id}.yaml"
                if blocked_cand.is_file():
                    try:
                        b_data = yaml.safe_load(blocked_cand.read_text(encoding="utf-8"))
                        if not isinstance(b_data, dict):
                            return False
                        b_data["status"] = "ACTIVE"
                        b_data["updated"] = _now()
                        if clear_error:
                            b_data.pop("error", None)
                            b_data.pop("blocked_reason", None)
                        elif error:
                            b_data["error"] = str(error)
                        wo_file.parent.mkdir(parents=True, exist_ok=True)
                        from validators.kernel.io import durable_write_yaml
                        durable_write_yaml(wo_file, b_data, default_flow_style=False)
                        try:
                            blocked_cand.unlink()
                        except OSError:
                            pass
                        data = b_data
                    except Exception:
                        return False
                else:
                    return False
            else:
                return False
        else:
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
                from validators.kernel.io import durable_write_yaml
                durable_write_yaml(wo_file, data, default_flow_style=False)
            except Exception:
                return False

        # Also keep INDEX.yaml synchronized if present
        index_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
        if index_file.is_file():
            try:
                idx_data = yaml.safe_load(index_file.read_text(encoding="utf-8")) or {}
                orders = idx_data.get("orders", [])
                for o in orders:
                    if isinstance(o, dict) and o.get("id") == wo_id:
                        o["status"] = target_status
                        o["file"] = f"work-orders/ACTIVE/{wo_id}.yaml"
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
                durable_write_yaml(index_file, idx_data)
                tree_file = ws / ".sync" / "runtime" / "TREE.yaml"
                try:
                    from cli.init import ensure_canonical_tree
                    tree_data = ensure_canonical_tree(ws)
                    if "work_orders" in tree_data and isinstance(tree_data["work_orders"], dict):
                        tree_data["work_orders"]["total_active"] = idx_data.get("total_active", 0)
                        tree_data["work_orders"]["total_completed"] = idx_data.get("total_completed", 0)
                        tree_data["work_orders"]["total_blocked"] = idx_data.get("total_blocked", 0)
                        durable_write_yaml(tree_file, tree_data)
                except Exception:
                    pass
            except Exception:
                pass
        return True

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
            and op.get("operation_id") not in set(getattr(state, "qa_review_operations", {}).values())
            and op.get("operation_id") != getattr(state, "final_review_operation_id", None)
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

    def _has_active_operations(self, state: RunState) -> bool:
        """Check if any asynchronous operation or worker thread is actively in-flight."""
        # 1. Active worker threads
        if hasattr(self.manager, "_turn_threads"):
            if any(t.is_alive() for t in getattr(self.manager, "_turn_threads", {}).values()):
                return True

        # 2. Tracked state operation IDs
        tracked_op_ids = [
            state.planning_operation_id,
            state.authoring_operation_id,
            state.architect_repair_operation_id,
            state.recovery_decision_operation_id,
            state.integration_operation_id,
            state.gitops_operation_id,
            state.dependency_escalation_op_id,
            state.batch_operation_id,
        ]
        for op_id in tracked_op_ids:
            if op_id:
                op = self._get_operation(op_id)
                if op and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                    return True

        # 3. Work order running operations
        for wo_id in state.worker_wo_ids:
            if self._is_wo_running(state, wo_id):
                return True

        # 4. Session-level active operation
        if hasattr(self.manager, "_sessions"):
            sess = getattr(self.manager, "_sessions", {}).get(state.session_id)
            if sess and sess.get("active_operation"):
                act_op = self._get_operation(sess.get("active_operation"))
                if act_op and str(act_op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                    return True

        return False

    def _discover_worker_wos(self, state: RunState) -> None:
        """Read .sync/work-orders/ACTIVE/ to find all worker WOs (excluding WO-000)."""
        ws = Path(state.workspace)
        active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        if not active_dir.is_dir():
            return
        wo_ids: list[str] = []
        p_idx = None
        if state.planning_wo_id:
            from validators.harness.authoring_readiness import _parse_wo_index
            p_idx = _parse_wo_index(state.planning_wo_id)
        for wo_file in sorted(active_dir.glob("*.yaml")):
            wo_id = wo_file.stem
            if wo_id in (state.planning_wo_id, "WO-000"):
                continue  # Skip the planning WO
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    st = str(data.get("status", "")).upper()
                    if st in ("COMPLETED", "BLOCKED", "FAILED", "CANCELLED"):
                        continue
                    if p_idx is not None:
                        from validators.harness.authoring_readiness import _parse_wo_index
                        if (_parse_wo_index(wo_id) or 0) <= p_idx:
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
                wo_ids.append(wo_id)
        if wo_ids or not state.worker_wo_ids:
            state.worker_wo_ids = wo_ids

    def _dependencies_met(self, state: RunState, wo_id: str, ws: Path) -> bool:
        """Check that all dependencies of a WO are in completed_wo_ids."""
        if wo_id in state.blocked_by_escalation:
            return False
        # Check explicit dependencies in state
        for dep in state.wo_dependencies.get(wo_id, []):
            dep_str = str(dep).strip()
            if dep_str and dep_str not in state.completed_wo_ids:
                if dep_str != state.planning_wo_id and dep_str != "WO-000" and dep_str != state.gitops_wo_id:
                    return False
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
                    # Also check if the dep is the planning WO or GitOps WO (GitOps runs at milestone end, never blocks worker)
                    if dep_str != state.planning_wo_id and dep_str != "WO-000" and dep_str != state.gitops_wo_id:
                        return False
        except Exception:
            pass
        return True

    def _agent_for_wo(self, wo_id: str, ws: Path) -> str:
        """Determine the primary agent for a work order (ACTIVE, BLOCKED, or COMPLETED)."""
        for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
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
                "imports external module",
                "no dependency manifest",
                "dependency manifest (pyproject.toml",
                "dependency manifest creation is outside",
                "modifying the dependency manifest is outside",
            )
        )
        if not is_dep_error:
            return None

        # Extract module names:
        # Pattern 1: imports undeclared third-party module(s): foo, bar
        # Pattern 2: imports external module(s) ['foo', 'bar']
        modules: list[str] = []
        match = re.search(r"imports undeclared third-party module\(s\):\s*([^.\n]+)", full_text, re.IGNORECASE)
        if match:
            raw_mods = match.group(1).split(",")
            modules = [m.strip().strip("'\"") for m in raw_mods if m.strip()]
        else:
            match_ext = re.search(r"imports external module\(s\)\s*\[([^\]]+)\]", full_text, re.IGNORECASE)
            if match_ext:
                raw_mods = match_ext.group(1).split(",")
                modules = [m.strip().strip("'\"") for m in raw_mods if m.strip()]

        # Extract deliverable path: pattern "Deliverable 'foo/bar.py'"
        deliv_match = re.search(r"Deliverable\s+['\"]([^'\"]+)['\"]", full_text, re.IGNORECASE)
        deliv_path = deliv_match.group(1) if deliv_match else None

        # Detect whether contract permits writing manifests
        manifest_permitted = None
        lower_text = full_text.lower()
        if "your contract permits" in lower_text and "dependency manifest" in lower_text:
            manifest_permitted = True
        elif "outside your assigned contract scope" in lower_text and "dependency manifest" in lower_text:
            manifest_permitted = False

        return {
            "modules": modules,
            "deliverable": deliv_path,
            "manifest_permitted": manifest_permitted,
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

        self._ensure_dependency_escalation_channel(ws, wo_id)
        return self._architect_turn(state, prompt, wo_id)

    def _check_qa_verdict(self, wo_id: str, ws: Path) -> str | None:
        """Check for a QA verdict file for a work order. Returns 'APPROVED', 'NEEDS_CHANGES', or None."""
        # QA work orders produce verdicts; they are never subject to QA review on themselves.
        agent = self._agent_for_wo(wo_id, ws)
        if self._role_for_agent(agent) == "qa":
            return None

        deliv_path = self._get_wo_deliverable_path(wo_id, ws)
        deliv_name = Path(deliv_path).name if deliv_path else None

        search_dirs = [
            ws / ".sync" / "inbox" / "claude",
            ws / ".sync" / "inbox" / "claude" / "_read",
            ws / ".sync" / "reviews",
            ws / ".sync" / "qa" / "verdicts",
        ]
        candidates: list[Path] = []
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and any(k in f.name.lower() for k in ("verdict", "review")):
                    candidates.append(f)

        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

        for f in candidates:
            try:
                raw_text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            content = raw_text.upper()

            # Target isolation: if the file declares another work order ID in its filename,
            # it is strictly a verdict for that other work order and CANNOT match wo_id.
            wos_in_name = self._WO_ID_RE.findall(f.name)
            if wos_in_name and wo_id not in wos_in_name:
                continue

            # If the document header/title explicitly targets another work order, skip it.
            header_wos = self._WO_ID_RE.findall(raw_text[:300])
            if header_wos and wo_id not in header_wos:
                continue

            matches_wo = wo_id in f.name
            if not matches_wo:
                matches_wo = bool(re.search(rf"\b{re.escape(wo_id)}\b", raw_text))
                if not matches_wo:
                    other_wos = [wid for wid in self._WO_ID_RE.findall(raw_text) if wid != wo_id and wid != "WO-000"]
                    if not other_wos:
                        if deliv_path and str(deliv_path).replace("\\", "/") in raw_text.replace("\\", "/"):
                            matches_wo = True
                        elif deliv_name and len(deliv_name) > 6 and deliv_name in raw_text:
                            matches_wo = True
            if matches_wo:
                status_match = re.search(r"status\s*:\s*(APPROVED|NEEDS_CHANGES|NEEDS\s+CHANGES)", content, re.IGNORECASE)
                if status_match:
                    val = status_match.group(1).upper()
                    if "NEEDS" in val:
                        return "NEEDS_CHANGES"
                    return "APPROVED"
                if "NEEDS_CHANGES" in content or "NEEDS CHANGES" in content:
                    return "NEEDS_CHANGES"
                if "APPROVED" in content:
                    return "APPROVED"
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

    def _verify_work_order_deliverables(
        self, wo_id: str, ws: Path
    ) -> tuple[bool, str, dict[str, Any]]:
        """Deterministically verify all declared deliverables for a work order on disk."""
        wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            wo_file = ws / ".sync" / "work-orders" / "COMPLETED" / f"{wo_id}.yaml"
        if not wo_file.is_file():
            return True, "", {}
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return True, "", {}
            from validators.harness.deliverable_verifier import verify_work_order_deliverables
            passed, errors, evidence = verify_work_order_deliverables(ws, data)
            if not passed:
                err_msg = "; ".join(errors) if errors else "declared deliverable was not found or failed verification on disk"
                return False, err_msg, evidence
            return True, "", evidence
        except Exception as e:
            return False, f"Deliverable verification error: {e}", {"error": str(e)}

    def _check_deliverable_exists(self, wo_id: str, ws: Path) -> bool:
        """Check if declared deliverable for a work order actually exists on disk."""
        passed, _, _ = self._verify_work_order_deliverables(wo_id, ws)
        return passed

    def _get_qa_feedback(self, wo_id: str, ws: Path) -> str:
        """Extract QA review feedback text from verdict file if present."""
        agent = self._agent_for_wo(wo_id, ws)
        if self._role_for_agent(agent) == "qa":
            return ""

        deliv_path = self._get_wo_deliverable_path(wo_id, ws)
        deliv_name = Path(deliv_path).name if deliv_path else None

        search_dirs = [
            ws / ".sync" / "inbox" / "claude",
            ws / ".sync" / "inbox" / "claude" / "_read",
            ws / ".sync" / "inbox" / "codex",
            ws / ".sync" / "inbox" / "gemini",
            ws / ".sync" / "reviews",
            ws / ".sync" / "qa" / "verdicts",
        ]
        candidates: list[Path] = []
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and any(k in f.name.lower() for k in ("verdict", "review")):
                    candidates.append(f)

        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

        for f in candidates:
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue

            wos_in_name = self._WO_ID_RE.findall(f.name)
            if wos_in_name and wo_id not in wos_in_name:
                continue

            header_wos = self._WO_ID_RE.findall(text[:300])
            if header_wos and wo_id not in header_wos:
                continue

            matches_wo = wo_id in f.name
            if not matches_wo:
                matches_wo = bool(re.search(rf"\b{re.escape(wo_id)}\b", text))
                if not matches_wo:
                    other_wos = [wid for wid in self._WO_ID_RE.findall(text) if wid != wo_id and wid != "WO-000"]
                    if not other_wos:
                        if deliv_path and str(deliv_path).replace("\\", "/") in text.replace("\\", "/"):
                            matches_wo = True
                        elif deliv_name and len(deliv_name) > 6 and deliv_name in text:
                            matches_wo = True
            if matches_wo:
                if "NEEDS_CHANGES" in text.upper() or "NEEDS CHANGES" in text.upper():
                    return text.strip()
        return ""

    # ── Mandatory QA review gate ──────────────────────────────────────

    def _qa_review_prompt(self, state: RunState, ws: Path, wo_id: str) -> str:
        """Dispatch prompt for an independent QA review of one deliverable."""
        from validators.harness import review_gate

        data = review_gate.load_wo(ws, wo_id)
        deliverables = review_gate.wo_deliverable_paths(data)
        criteria = data.get("acceptance_criteria") or data.get("test_plan") or []
        criteria_text = (
            "\n".join(f"- {c}" for c in criteria)
            or "- (derive from the approved PLAN.md scope and contract)"
        )
        deliv_text = "\n".join(f"- {p}" for p in deliverables) or "- (no declared deliverable)"
        current_hash = review_gate.subject_hash(ws, wo_id)
        verdict_path = review_gate.qa_verdict_path(ws, wo_id)
        try:
            rel_verdict = verdict_path.relative_to(ws).as_posix()
        except Exception:
            rel_verdict = str(verdict_path)
        attempt = state.qa_review_attempts.get(wo_id, 0)
        budget = state.max_qa_reworks

        return (
            f"Mandatory QA review for work order {wo_id}.\n\n"
            "The worker declared completion, but completion claims are not evidence. "
            "Independently verify the deliverables against the approved plan, the "
            "work-order acceptance criteria "
            f"and the contract:\n{deliv_text}\n\n"
            f"Acceptance criteria:\n{criteria_text}\n\n"
            "Required checks (use the real tools, do not infer):\n"
            "1. Read the deliverable files and confirm they implement the declared behaviour.\n"
            "2. Run the relevant test suite with run_tests; run the build/static check when the "
            "project defines one.\n"
            "3. Confirm error handling, input validation and integration points required "
            "by the plan.\n"
            "4. Treat an unavailable tool or missing test as an explicit limitation, "
            "never as a pass.\n\n"
            f"Then persist the structured verdict as JSON to `${rel_verdict}` using write_file. "
            "The verdict MUST be exactly this shape:\n"
            "{\n"
            f'  "schema": "{review_gate.QA_VERDICT_SCHEMA}",\n'
            f'  "work_order_id": "{wo_id}",\n'
            f'  "revision": {attempt + 1},\n'
            '  "reviewer": "gemma",\n'
            '  "reviewer_role": "qa",\n'
            f'  "timestamp": "<ISO-8601 UTC>",\n'
            '  "verdict": "PASS" | "FAIL",\n'
            f'  "subject_hash": "{current_hash}",\n'
            '  "criteria": [{"id": "...", "description": "...", '
            '"outcome": "PASS|FAIL", "evidence": "..."}],\n'
            '  "evidence": [{"kind": "test|build|inspection", "command": "...", '
            '"result": "...", "passed": true}],\n'
            '  "findings": [{"severity": "critical|major|minor", '
            '"detail": "...", "reproduce": "..."}],\n'
            '  "required_actions": ["..."],\n'
            f'  "rework_attempt": {attempt},\n'
            f'  "rework_budget": {budget}\n'
            "}\n\n"
            "Rules: if any mandatory check fails, set verdict FAIL, list reproducible "
            "findings and the corrective actions the owning worker must perform. Do NOT "
            "modify application code or the work-order state; you are an independent "
            "reviewer. Do not claim PASS without test/build "
            "evidence. Finish your turn by declaring status 'completed' after writing the verdict."
        )

    def _handle_qa_gate_pending(
        self,
        state: RunState,
        ws: Path,
        wo_id: str,
        decision: Any,
    ) -> AdvanceResult | None:
        """Drive the QA gate for one work order. Returns a result only when the
        whole run must stop; ``None`` means keep executing on the next tick."""
        from validators.harness import review_gate

        op_id = state.qa_review_operations.get(wo_id)
        if op_id:
            op = self._get_operation(op_id)
            if op is not None and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                return None  # review in flight
            state.qa_review_operations.pop(wo_id, None)
            # Re-evaluate: a terminal review must have produced a verdict.
            decision = review_gate.evaluate_qa_gate(ws, wo_id)
            if decision.passed:
                return None
            if decision.verdict is None:
                state.qa_review_attempts[wo_id] = state.qa_review_attempts.get(wo_id, 0) + 1
                if state.qa_review_attempts[wo_id] >= state.max_qa_review_attempts:
                    state.error = (
                        f"QA review for {wo_id} produced no verdict after "
                        f"{state.qa_review_attempts[wo_id]} attempt(s): {decision.detail}"
                    )
                    state.exit_code = 3
                    self.escalate_wo_to_recovery(state, wo_id, reason=state.error)
                    self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
                    return AdvanceResult.TRANSITIONED

        verdict = decision.verdict
        stale = "stale" in str(decision.detail or "")
        if verdict is not None and (stale or str(verdict.get("verdict", "")).upper() == "FAIL"):
            # A failed or stale verdict can never authorise acceptance: archive it
            # so repair is followed by a genuinely fresh review.
            review_gate.archive_qa_verdict(
                ws, wo_id, suffix="stale" if stale else "rejected"
            )
            if not stale:
                reworks = state.qa_reworks.get(wo_id, 0)
                if reworks >= state.max_qa_reworks:
                    state.error = (
                        f"Work order {wo_id} exceeded QA rework limit "
                        f"({reworks}/{state.max_qa_reworks}); findings: "
                        + "; ".join(decision.findings or (decision.detail or "",))
                    )
                    state.exit_code = 3
                    self.escalate_wo_to_recovery(state, wo_id, reason=state.error)
                    self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
                    return AdvanceResult.TRANSITIONED
                state.qa_reworks[wo_id] = reworks + 1
                correction = "\n".join(f"- {a}" for a in decision.required_actions)
                findings = "\n".join(f"- {f}" for f in decision.findings)

                def _qa_rework_prompt(n: int, agent: str) -> str:
                    return (
                        f"Rework work order {wo_id} after mandatory QA review (attempt {n}).\n"
                        f"QA findings:\n"
                        f"{findings or '- deliverable did not satisfy the acceptance criteria'}\n"
                        + (f"Required corrective actions:\n{correction}\n" if correction else "")
                        + "Restrict your changes to the identified defects. When you finish, the "
                        "deliverable must be re-verified and re-reviewed by QA before it "
                        "can be accepted."
                    )

                if wo_id in state.completed_wo_ids:
                    state.completed_wo_ids.remove(wo_id)
                if self._retry_worker(state, ws, wo_id, _qa_rework_prompt):
                    return None

        # No usable verdict yet → dispatch an independent QA review turn.
        state.qa_review_attempts[wo_id] = state.qa_review_attempts.get(wo_id, 0) + 1
        if state.qa_review_attempts[wo_id] > state.max_qa_review_attempts + state.max_qa_reworks:
            state.error = f"QA review for {wo_id} exhausted its bounded review attempts"
            state.exit_code = 3
            self.escalate_wo_to_recovery(state, wo_id, reason=state.error)
            self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
            return AdvanceResult.TRANSITIONED
        try:
            # No work_order_id: the reviewer runs under the QA agent contract
            # (.sync/agents/gemma.contract.yaml), never under the reviewed
            # work order's contract. The worker therefore cannot inherit the
            # QA write channel and self-approve.
            op = self.manager.start_turn(
                state.session_id,
                self._qa_review_prompt(state, ws, wo_id),
                role="qa",
                agent_id="gemma",
                parent_operation_id=state.batch_operation_id,
            )
            op_id = op.get("operation_id") if isinstance(op, dict) else None
            if op_id:
                state.qa_review_operations[wo_id] = str(op_id)
            state.error = None
        except OperationContentionError:
            return None
        except Exception as exc:
            state.error = f"QA review dispatch failed for {wo_id}: {exc}"
            state.blocked_wo_ids.append(wo_id) if wo_id not in state.blocked_wo_ids else None
        return None
