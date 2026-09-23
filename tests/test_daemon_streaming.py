"""SSE transport, replay, and structured tool-event tests."""

from __future__ import annotations

from queue import Queue
from threading import Thread
from time import sleep

from validators.kernel.daemon import LocalDaemon
from validators.kernel.daemon.events import EventDispatcher
from validators.kernel.tui.client import DaemonClient


def _contract() -> dict[str, object]:
    return {"agent_id": "codex", "work_order": "WO-014", "scope": {"allow": ["tests/**"]}}


def _create_session(client: DaemonClient) -> dict:
    return client.create_session(
        agent="codex", provider="test", contract=_contract(), workspace="workspace"
    )


def test_event_dispatcher_pushes_to_subscribers_and_unsubscribes():
    events = EventDispatcher()
    received = []
    unsubscribe = events.subscribe(received.append)
    first = events.publish("test.first", "session-1")
    unsubscribe()
    events.publish("test.second", "session-1")

    assert received == [first]


def test_sse_replays_history_in_order_and_resumes_after_sequence(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        client = DaemonClient(daemon.url)
        session = _create_session(client)
        session_id = session["session_id"]
        first_stream = client.stream_events(session_id)
        first = next(first_stream)
        first_stream.close()

        daemon.manager.record_verification(session_id, {"passed": True})
        resumed = client.stream_events(session_id, after=first["sequence"])
        replay = [next(resumed) for _ in range(4)]
        resumed.close()

        assert [event["sequence"] for event in replay] == list(
            range(first["sequence"] + 1, first["sequence"] + 5)
        )
        assert [event["name"] for event in replay][-2:] == [
            "verification.started",
            "verification.completed",
        ]


def test_sse_pushes_a_new_event_and_client_disconnect_does_not_deadlock(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        client = DaemonClient(daemon.url)
        session_id = _create_session(client)["session_id"]
        after = daemon.manager.events.events(session_id)[-1].sequence
        stream = client.stream_events(session_id, after=after)
        delivered: Queue[dict] = Queue()
        reader = Thread(target=lambda: delivered.put(next(stream)), daemon=True)
        reader.start()
        sleep(0.1)
        daemon.manager.events.publish("test.live", session_id, value="now")
        event = delivered.get(timeout=2)
        stream.close()
        reader.join(timeout=2)

        assert event["name"] == "test.live"
        assert event["payload"] == {"value": "now"}


def test_tool_events_have_standard_names_and_outcome_statuses():
    events = EventDispatcher()
    call = events.tool_call("session-1", "shell", "call-1", {"command": "pwd"}, "op-1")
    results = [
        events.tool_result("session-1", "shell", "call-1", status, operation_id="op-1")
        for status in ("success", "failure", "cancelled", "denied", "approval_required")
    ]

    assert call.name == "event.toolCall"
    assert call.payload == {
        "status": "running",
        "tool_name": "shell",
        "call_id": "call-1",
        "arguments": {"command": "pwd"},
        "operation_id": "op-1",
    }
    assert [event.name for event in results] == ["event.toolResult"] * 5
    assert [event.payload["status"] for event in results] == [
        "success",
        "failure",
        "cancelled",
        "denied",
        "approval_required",
    ]
