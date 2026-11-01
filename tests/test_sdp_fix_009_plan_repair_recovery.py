"""SDP-FIX-009 — plan-gate repair continuation recovery (supervisor legs).

Reproducible defect observed in the live acceptance run (task-mgmt-009-fresh,
2026-10-31): the planning op wrote PLAN.md but the verification gate BLOCKED it
("not added or modified in this turn"), the manager dispatched an honest
Architecture repair turn that re-authored PLAN.md and completed, yet the
supervisor had already transitioned PLANNING -> FAILED on the first evidence.
The schema-valid plan never reached AWAITING_APPROVAL.

These tests pin the supervisor-side contract:
  1. A BLOCKED plan-gate turn whose repair continuation is in flight must wait
     (WAITING_FOR_OPERATION), not strand the run in FAILED.
  2. A COMPLETED repair continuation must be adopted so the freshly authored
     plan can advance the run to AWAITING_APPROVAL (never fabricated).
  3. A repair continuation that itself terminates non-successfully still fails
     the run with the original planning error evidence (fail-closed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from threading import Event

import pytest  # noqa: F401

from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
)


class _MockManager:
    """Minimal deterministic SessionManager stub for supervisor unit tests."""

    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}
        self.plans: list[dict[str, Any]] = []
        self._op_counter = 0

    def begin_operation(
        self,
        session_id: str,
        operation_name: str,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[Any, str]:
        self._op_counter += 1
        op_id = f"op-{self._op_counter:03d}"
        self.operations[op_id] = {
            "operation_id": op_id,
            "session_id": session_id,
            "operation": operation_name,
            "status": "RUNNING",
            "metadata": metadata or {},
            "children": [],
            "child_results": {},
        }
        return Event(), op_id

    def start_turn(self, session_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self._op_counter += 1
        op_id = f"op-{self._op_counter:03d}"
        op = {
            "operation_id": op_id,
            "session_id": session_id,
            "prompt": prompt,
            "status": "RUNNING",
            "work_order_id": kwargs.get("work_order_id"),
            "role": kwargs.get("role"),
            "agent_id": kwargs.get("agent_id"),
            "metadata": dict(kwargs),
            "result": None,
        }
        self.operations[op_id] = op
        return op

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self.operations[operation_id]

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.operations.values())

    def list_plans(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.plans)

    def get_plan(self, session_id: str, plan_id: str) -> dict[str, Any]:
        for p in reversed(self.plans):
            if p.get("plan_id") == plan_id:
                return p
        raise KeyError(plan_id)

    def propose_plan(
        self,
        session_id: str,
        plan_id: str,
        title: str,
        content: str = "",
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        plan = {
            "plan_id": plan_id,
            "session_id": session_id,
            "title": title,
            "content": content,
            "state": "AWAITING_APPROVAL",
            "metadata": metadata or {},
        }
        self.plans.append(plan)
        return plan

    def approve_plan(self, session_id: str, plan_id: str, reason: str = "") -> None:
        self.get_plan(session_id, plan_id)["state"] = "APPROVED"

    def reject_plan(self, session_id: str, plan_id: str, reason: str = "") -> None:
        p = self.get_plan(session_id, plan_id)
        p["state"] = "REJECTED"
        p["feedback"] = reason

    def complete_operation(
        self,
        operation_id: str | None = None,
        status: str = "COMPLETED",
        result: dict | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        op = self.get_operation(operation_id)
        op["status"] = status
        op["result"] = result or {"status": status.lower(), "summary": "Done"}
        return op


_GATE_ERROR = (
    "PLAN.md verification gate failed: staged artifact PLAN.md "
    "(work-order deliverable) was not added or modified in this turn"
)


def _start_planning_run() -> tuple[LifecycleSupervisor, _MockManager, Any, str]:
    mgr = _MockManager()
    supervisor = LifecycleSupervisor(mgr)
    state = supervisor.start_run("run-009", "Build task app", Path("."), "sess-009")
    supervisor.advance(state)
    planning_op = state.planning_operation_id
    assert planning_op is not None
    return supervisor, mgr, state, planning_op


def _dispatch_repair(mgr: _MockManager, attempt: int = 1) -> str:
    """Mirror the real user_turn repair dispatch (is_authoring, no work_order_id)."""
    op = mgr.start_turn(
        "sess-009",
        "CRITICAL ARCHITECTURE INSTRUCTION: re-author a strictly compliant PLAN.md",
        is_authoring=True,
        plan_repair_attempt=attempt,
    )
    return op["operation_id"]


def test_blocked_plan_gate_with_inflight_repair_waits() -> None:
    """Defect symptom: PLANNING must not flip to FAILED while repair is in flight."""
    supervisor, mgr, state, planning_op = _start_planning_run()
    mgr.complete_operation(planning_op, "BLOCKED", {"error": _GATE_ERROR})
    repair_op = _dispatch_repair(mgr)  # still RUNNING — the honest re-authoring

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.PLANNING
    assert state.error is None
    # The failed attempt must be ignored in favour of the in-flight repair;
    # no duplicate planning turn may be dispatched.
    assert state.planning_operation_id == planning_op
    assert len(mgr.operations) == 2


def test_successful_repair_adopts_plan_awaiting_approval() -> None:
    """Recovery: COMPLETED repair advances to AWAITING_APPROVAL with its plan."""
    supervisor, mgr, state, planning_op = _start_planning_run()
    mgr.complete_operation(planning_op, "BLOCKED", {"error": _GATE_ERROR})
    repair_op = _dispatch_repair(mgr)
    assert supervisor.advance(state) == AdvanceResult.WAITING_FOR_OPERATION

    # Repair completes and the manager (Leg B) proposes the plan it re-authored.
    mgr.complete_operation(repair_op, "COMPLETED")
    mgr.propose_plan(
        "sess-009",
        "PLAN-009",
        "Task App Plan",
        metadata={"operation_id": repair_op},
    )

    res = supervisor.advance(state)

    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL
    assert state.plan_id == "PLAN-009"
    assert planning_op in state.ignored_operation_ids


def test_failed_repair_fails_run_with_planning_evidence() -> None:
    """Fail-closed: a repair that terminates non-successfully still fails the run."""
    supervisor, mgr, state, planning_op = _start_planning_run()
    mgr.complete_operation(planning_op, "BLOCKED", {"error": _GATE_ERROR})
    repair_op = _dispatch_repair(mgr)
    mgr.complete_operation(repair_op, "FAILED")

    res = supervisor.advance(state)

    assert res == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert "PLAN.md" in (state.error or "")


def test_repair_without_plan_still_fails_run() -> None:
    """A completed repair that proposes no plan must fail the run (never fabricate)."""
    supervisor, mgr, state, planning_op = _start_planning_run()
    mgr.complete_operation(planning_op, "BLOCKED", {"error": _GATE_ERROR})
    repair_op = _dispatch_repair(mgr)
    mgr.complete_operation(repair_op, "COMPLETED")
    # No plan proposed by the manager for the repair turn.

    res = supervisor.advance(state)

    assert res == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED


def test_plan_repair_attempt_persists_across_restart() -> None:
    """Serialization idempotency: plan_repair_attempt persists across RunState roundtrip."""
    from validators.kernel.daemon.supervisor.models import RunState

    supervisor, mgr, state, planning_op = _start_planning_run()
    mgr.complete_operation(planning_op, "BLOCKED", {"error": _GATE_ERROR})
    repair_op = _dispatch_repair(mgr, attempt=1)

    assert supervisor.advance(state) == AdvanceResult.WAITING_FOR_OPERATION
    assert state.plan_repair_attempt == 1

    # Simulate supervisor/daemon crash and serialization round-trip
    serialized = state.to_dict()
    assert serialized.get("plan_repair_attempt") == 1

    recovered_state = RunState.from_dict(serialized)
    assert recovered_state.plan_repair_attempt == 1

    # Verify advance on recovered state still halts at WAITING_FOR_OPERATION
    assert supervisor.advance(recovered_state) == AdvanceResult.WAITING_FOR_OPERATION
    assert recovered_state.plan_repair_attempt == 1

    # Repair completes and manager proposes the plan
    mgr.complete_operation(repair_op, "COMPLETED")
    mgr.propose_plan(
        "sess-009",
        "PLAN-009",
        "Task App Plan",
        metadata={"operation_id": repair_op},
    )

    res = supervisor.advance(recovered_state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert recovered_state.phase == Phase.AWAITING_APPROVAL
    assert recovered_state.plan_id == "PLAN-009"
    assert recovered_state.plan_repair_attempt is None

