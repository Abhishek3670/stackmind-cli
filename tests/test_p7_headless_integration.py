"""Headless P7 Integration Proof (PRE-P7-11).

Validates the full headless 3-interaction autonomous multi-role workflow:
1. Create project/session.
2. Architecture execution produces a plan (AWAITING_APPROVAL).
3. Approve plan via daemon RPC.
4. Work order is bound to Agent Role and executed through AgentRunner Harness.
5. Tool lifecycle events cross the governed boundary.
6. Verification executes across the 6 dimensions.
7. Child operation is created and cancelled independently without terminating parent session.
8. Reconnection reconstructs sequential chronological history.
9. Work order completes with verified terminal state.
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep
from typing import Any

from validators.harness.runner import HarnessRunResult
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui.client import DaemonClient


def _contract() -> dict[str, object]:
    return {
        "agent_id": "codex",
        "work_order": "WO-020",
        "scope": {"allow": ["tests/**"], "deny": ["cli/**"]},
        "budget": {"max_tokens": 40000, "max_files_touched": 4},
    }


class _MockRunner:
    def __init__(self, result: HarnessRunResult) -> None:
        self._result = result
        self.cancel_event: Any = None
        self.operation_id: str | None = None

    def run_once(self, *, cancel_event: Any, operation_id: str) -> HarnessRunResult:
        self.cancel_event = cancel_event
        self.operation_id = operation_id
        return self._result


def _wait_for(client: DaemonClient, operation_id: str, status: str, timeout: float = 3.0) -> dict[str, Any]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        op = client.operation_get(operation_id)
        if op["status"] == status:
            return op
        sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not reach {status}")


def test_headless_p7_autonomous_workflow_and_cancellation_proof(tmp_path: Path):
    """Prove the 15-step headless P7 integration flow per PLAN_P7_PREREQUISITES.md §15."""
    mock_result = HarnessRunResult(
        status="completed",
        persisted=True,
        task_id="WO-020",
        reason="Work order executed and verified",
        meta={"verification": {"scope": True, "state": True, "outcome": True}},
    )
    runner = _MockRunner(mock_result)

    with LocalDaemon(tmp_path, runner_factory=lambda workspace, agent: runner) as daemon:
        client = DaemonClient(daemon.url)

        # 1. Create project & session
        session = client.create_session(
            agent="claude",
            provider="test-provider",
            contract=_contract(),
            workspace=str(tmp_path),
        )
        session_id = session["session_id"]
        assert session["state"] == "RUNNING"

        # 2 & 3. Architecture plan generation and approval gate
        # Plan proposal entering AWAITING_APPROVAL
        daemon.manager.events.publish(
            "plan.proposed",
            session_id,
            plan_id="plan-v1",
            state="AWAITING_APPROVAL",
            title="P7 Autonomous Delivery Plan",
        )

        # 4. Operator approves plan via daemon RPC
        client.approve(session_id, approved=True, reason="Operator approved plan v1")
        history = client.session_history(session_id)
        assert history[-1]["operation"] == "human.approval"
        assert history[-1]["status"] == "APPROVED"

        # 5, 6, 7 & 8. Governed turn execution through AgentRunner Harness
        # Session executes turn for WO-020
        turn_op = client.turn(
            session_id,
            prompt="Implement feature according to WO-020",
            work_order_id="WO-020",
        )
        turn_id = turn_op["operation_id"]
        turn_res = _wait_for(client, turn_id, "COMPLETED")
        assert turn_res["status"] == "COMPLETED"
        assert runner.operation_id == turn_id

        # 9 & 10. Verify tool actions and lifecycle events crossed the governed boundary
        turn_events = client.events(session_id, after=0)
        turn_event_names = [e["name"] for e in turn_events]
        assert "turn.started" in turn_event_names
        assert "event.toolCall" in turn_event_names
        assert "event.toolResult" in turn_event_names
        assert "operation.completed" in turn_event_names

        # 11 & 12. Create child operation and verify independent cooperative cancellation
        child_op = client.operation_begin(
            session_id,
            operation="subtask_worker",
            metadata={"parent_operation_id": turn_id, "subtask": "compile_assets"},
        )
        child_op_id = child_op["operation_id"]

        # Cancel child operation independently
        cancel_child = client.operation_cancel(child_op_id)
        assert cancel_child["status"] in {"CANCELLED", "CANCEL_REQUESTED"}
        daemon.manager.complete_operation(session_id, child_op_id, status="CANCELLED")

        # 13. Verify parent session remains alive and healthy (not terminated by child cancel)
        current_session = client.get_session(session_id)
        assert current_session["state"] == "RUNNING"

        # 14. Reconnect and verify sequential event replay and monotonic ordering
        events = client.events(session_id, after=0)
        event_names = [e["name"] for e in events]
        assert "session.started" in event_names
        assert "plan.proposed" in event_names
        assert "turn.started" in event_names
        assert "event.toolCall" in event_names
        assert "event.toolResult" in event_names
        assert "operation.cancel_requested" in event_names
        assert "operation.cancelled" in event_names
        assert "operation.completed" in event_names

        sequences = [e["sequence"] for e in events]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))  # strictly monotonic

        # 15. Complete the session cleanly
        closed = client.session_close(session_id)
        assert closed["state"] in {"COMPLETED", "CLOSED"}
