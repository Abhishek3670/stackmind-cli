"""Governed prompt/turn execution tests."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from time import monotonic, sleep
from typing import Any

from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui.adapter import StackMindTuiAdapter
from validators.kernel.tui.client import DaemonClient


@dataclass
class _Result:
    status: str
    persisted: bool = False
    task_id: str | None = "turn-task"
    reason: str | None = None


class _SuccessfulRunner:
    def __init__(self) -> None:
        self.cancel_event: Event | None = None
        self.operation_id: str | None = None

    def run_once(self, *, cancel_event: Event, operation_id: str) -> _Result:
        self.cancel_event = cancel_event
        self.operation_id = operation_id
        return _Result("completed", persisted=True)


class _BlockingRunner:
    def __init__(self) -> None:
        self.started = Event()

    def run_once(self, *, cancel_event: Event, operation_id: str) -> _Result:
        del operation_id
        self.started.set()
        cancel_event.wait(timeout=2)
        return _Result("cancelled", reason="operation cancelled")


def _contract() -> dict[str, object]:
    return {"agent_id": "codex", "work_order": "WO-015", "scope": {"allow": ["tests/**"]}}


def _wait_for(client: DaemonClient, operation_id: str, status: str) -> dict[str, Any]:
    deadline = monotonic() + 2
    while monotonic() < deadline:
        operation = client.operation_get(operation_id)
        if operation["status"] == status:
            return operation
        sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not reach {status}")


def test_session_turn_executes_runner_and_emits_structured_events(tmp_path):
    runner = _SuccessfulRunner()
    with LocalDaemon(tmp_path, runner_factory=lambda workspace, agent: runner) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="codex", provider="test", contract=_contract(), workspace="workspace"
        )
        operation = client.turn(session["session_id"], "summarize this")
        completed = _wait_for(client, operation["operation_id"], "COMPLETED")
        events = client.events(session["session_id"])

        assert completed["operation"] == "turn"
        assert completed["metadata"]["prompt"] == "summarize this"
        assert runner.operation_id == operation["operation_id"]
        assert runner.cancel_event is not None
        assert {"operation.started", "turn.started", "event.toolCall", "event.toolResult", "operation.completed"} <= {
            event["name"] for event in events
        }
        tool_result = next(event for event in events if event["name"] == "event.toolResult")
        assert tool_result["payload"]["status"] == "success"
        assert client.get_session(session["session_id"])["state"] == "RUNNING"


def test_session_cancel_cooperatively_cancels_an_active_turn(tmp_path):
    runner = _BlockingRunner()
    with LocalDaemon(tmp_path, runner_factory=lambda workspace, agent: runner) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="codex", provider="test", contract=_contract(), workspace="workspace"
        )
        operation = client.turn(session["session_id"], "wait for cancellation")
        assert runner.started.wait(timeout=2)
        session_after_cancel = client.cancel(session["session_id"])
        cancelled = _wait_for(client, operation["operation_id"], "CANCELLED")

        assert session_after_cancel["state"] == "RUNNING"
        assert cancelled["status"] == "CANCELLED"
        events = client.events(session["session_id"])
        assert any(
            event["name"] == "event.toolResult" and event["payload"]["status"] == "cancelled"
            for event in events
        )
        assert any(event["name"] == "operation.cancelled" for event in events)


def test_operation_turn_alias_and_tui_prompt_routing(tmp_path):
    runner = _SuccessfulRunner()
    with LocalDaemon(tmp_path, runner_factory=lambda workspace, agent: runner) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="codex", provider="test", contract=_contract(), workspace="workspace"
        )
        alias = client.call("operation.turn", session_id=session["session_id"], prompt="alias")
        assert _wait_for(client, alias["operation_id"], "COMPLETED")["metadata"]["prompt"] == "alias"

        adapter = StackMindTuiAdapter(client)
        prompt = adapter.command("from tui", session_id=session["session_id"])
        assert _wait_for(client, prompt["operation_id"], "COMPLETED")["metadata"]["prompt"] == "from tui"
