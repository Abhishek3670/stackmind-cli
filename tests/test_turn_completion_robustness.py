"""Regression tests for turn lifecycle robustness in the TUI.

These cover the three failure modes that left the TUI frozen on a
"Working..." frame:

1. The turn could be tracked by the wrong operation id when a tool event
   arrived, so the turn's own terminal event never matched.
2. The daemon finished the turn without the client ever seeing the terminal
   event, so the TUI waited forever on a dead operation.
3. The frame stopped repainting once the model stopped streaming, freezing
   the elapsed timer and making a live turn look dead.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest


def _make_state():
    from cli.tui.state import AutonomousDeliveryState

    return AutonomousDeliveryState(session_id="session-1")


def _capture_redraws(monkeypatch) -> list[dict]:
    """Patch the TUI redraw entry point and record its kwargs."""
    redraw_calls: list[dict] = []

    def capture_redraw(*args, **kwargs):
        redraw_calls.append(kwargs)
        return "frame"

    monkeypatch.setattr("cli.tui.app.redraw_full_screen", capture_redraw)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    return redraw_calls


def test_tool_event_does_not_hijack_the_turn_operation_id(monkeypatch):
    """A tool's operation id must not replace the turn's operation id.

    The tool event carries the id of the operation the tool runs under. The
    turn must still be tracked (and completed) by its own operation id.
    """
    from cli.tui.app import dispatch_delivery_command

    state = _make_state()
    session = {"session_id": "session-1", "workspace": "."}
    adapter = MagicMock()
    adapter.command.return_value = {"operation_id": "op-turn"}

    def stream_events():
        # The tool runs under its own operation id.
        yield {
            "name": "event.toolCall",
            "payload": {
                "tool_name": "lookup",
                "arguments": {"path": "notes.md"},
                "call_id": "g1",
                "operation_id": "op-tool",
            },
        }
        yield {"name": "operation.completed", "payload": {"operation_id": "op-turn"}}

    adapter.stream.return_value = stream_events()
    client = MagicMock()
    _capture_redraws(monkeypatch)

    started = time.monotonic()
    dispatch_delivery_command(adapter, client, session, "Hey", state, client_timeout=5)
    elapsed = time.monotonic() - started

    contents = [m.content for m in state.messages]
    # The turn's own operation id must survive the tool event.
    assert "Turn operation op-turn completed." in contents, contents
    assert not any("No response within client wait time" in c for c in contents), contents
    # The turn finished on its own terminal event, not on the client timeout.
    assert elapsed < 4.0, elapsed


def test_turn_completes_when_daemon_reports_the_operation_finished(monkeypatch):
    """A turn finished by the daemon ends even if no terminal event arrives."""
    from cli.tui.app import dispatch_delivery_command

    state = _make_state()
    session = {"session_id": "session-1", "workspace": "."}
    adapter = MagicMock()
    adapter.command.return_value = {"operation_id": "op-turn"}

    def stream_events():
        yield {"name": "text.delta", "payload": {"delta": "Partial answer"}}
        # The stream ends without operation.completed.

    adapter.stream.return_value = stream_events()
    client = MagicMock()
    client.operation_get.return_value = {"status": "COMPLETED", "operation_id": "op-turn"}
    _capture_redraws(monkeypatch)

    started = time.monotonic()
    dispatch_delivery_command(adapter, client, session, "Hey", state, client_timeout=8)
    elapsed = time.monotonic() - started

    contents = [m.content for m in state.messages]
    assert "Partial answer" in contents, contents
    assert not any("No response within client wait time" in c for c in contents), contents
    assert elapsed < 4.0, elapsed
    assert state.active_working_status is None


def test_heartbeat_keeps_repainting_after_the_model_stops_streaming(monkeypatch):
    """Heartbeats must repaint the frame so the elapsed timer keeps moving."""
    from cli.tui.app import dispatch_delivery_command

    state = _make_state()
    session = {"session_id": "session-1", "workspace": "."}
    adapter = MagicMock()
    adapter.command.return_value = {"operation_id": "op-turn"}

    stop = threading.Event()

    def stream_events():
        yield {"name": "text.delta", "payload": {"delta": "Partial"}}
        while not stop.is_set():
            time.sleep(0.05)
            yield {"_heartbeat": True}

    adapter.stream.return_value = stream_events()
    client = MagicMock()
    client.operation_get.return_value = {"status": "RUNNING"}
    redraw_calls = _capture_redraws(monkeypatch)

    try:
        dispatch_delivery_command(adapter, client, session, "Hey", state, client_timeout=3)
    finally:
        stop.set()

    streaming_frames = [
        call for call in redraw_calls if "conversation_content" in call
    ]
    # One frame from the delta, plus at least one heartbeat repaint ~2s later.
    assert len(streaming_frames) >= 2, redraw_calls
    assert state.active_working_status is None


@pytest.mark.parametrize(
    "event_name",
    ["operation.completed", "operation.cancelled", "operation.failed", "operation.blocked"],
)
def test_terminal_operation_event_clears_the_working_status(event_name):
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.active_working_status = "Working... [22.3s] (385.8k tokens)"

    state.process_event({"name": event_name, "payload": {"operation_id": "op-1"}})

    assert state.active_working_status is None
