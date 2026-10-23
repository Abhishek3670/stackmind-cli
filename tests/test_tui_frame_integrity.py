"""Regression tests for TUI frame integrity.

Covers the two failure modes visible in the duplicated "Working..." composer
border: a status that outlives its turn, and rows that are narrower than the
terminal so the previous frame's glyphs remain to their right.
"""

from __future__ import annotations

import sys

import pytest
from rich.text import Text

from cli.tui.layout import render_full_screen_workspace
from cli.tui.state import AutonomousDeliveryState
from cli.tui.terminal import redraw_full_screen


@pytest.mark.parametrize(
    "event_name",
    ["operation.completed", "operation.cancelled", "operation.failed", "operation.blocked"],
)
def test_working_status_is_cleared_on_terminal_operation_events(event_name):
    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.active_working_status = "Working... [22.3s] (385.8k tokens)"

    state.process_event({"name": event_name, "payload": {"operation_id": "op-1"}})

    assert state.active_working_status is None


def test_completed_frame_never_renders_a_stale_working_status():
    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.connection_status = "online"
    state.active_working_status = "Working... [22.3s] (385.8k tokens)"

    state.process_event({"name": "operation.completed", "payload": {"operation_id": "op-1"}})

    frame = render_full_screen_workspace(
        {"session_id": "sess-1", "agent": "codex"},
        state,
        width=120,
        height=24,
        composer_placeholder="Type a message...",
    )

    assert "Working..." not in frame
    assert "Type a message..." in frame


def test_redraw_pads_every_row_to_terminal_width(monkeypatch):
    class _Capture:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, value: str) -> int:
            self.chunks.append(value)
            return len(value)

        def flush(self) -> None:
            pass

    capture = _Capture()
    monkeypatch.setattr(sys, "stdout", capture)

    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.connection_status = "online"
    state.add_message("user", "hello")

    redraw_full_screen(
        {"session_id": "sess-1", "agent": "codex"},
        state,
        width=120,
        height=24,
        composer_is_active=True,
    )

    written = "".join(capture.chunks)
    assert written.startswith("\x1b[H")
    lines = written[len("\x1b[H") :].split("\n")
    assert len(lines) == 24
    assert all(Text.from_ansi(line).cell_len == 120 for line in lines)


def test_completed_turn_repaints_composer_without_working_status(monkeypatch):
    """A turn that ends via a full response event must still repaint the composer."""
    from cli.tui import commands

    class _Adapter:
        def stream(self, *args, **kwargs):
            return iter(
                [{"name": "turn.completed", "sequence": 1, "payload": {}}]
            )

    class _Client:
        pass

    redraws: list[dict] = []
    monkeypatch.setattr(commands, "redraw_full_screen", lambda *a, **k: redraws.append(k))
    monkeypatch.setattr(commands, "install_resize_handler", lambda: None)
    monkeypatch.setattr(commands, "check_terminal_resize", lambda cols, lines: (False, cols, lines))

    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.active_working_status = "Working... [22.3s] (385.8k tokens)"
    state.record_attempt(attempt=10, max_attempts=10, retry=3, max_retries=10)

    commands._stream_turn_execution(
        _Adapter(),
        _Client(),
        {"session_id": "sess-1"},
        state,
        op_id="turn",
        is_tty=True,
    )

    assert state.active_working_status is None
    assert state.retry_status_display is None
    assert redraws, "expected a final repaint after the turn ended"
    assert redraws[-1]["composer_placeholder"] == "Type a message..."
    assert redraws[-1]["composer_is_active"] is False

    frame = render_full_screen_workspace(
        {"session_id": "sess-1"}, state, width=120, height=24
    )
    assert "Working..." not in frame
    assert "Attempt" not in frame


def test_subsequent_turn_streams_tokens_even_when_project_is_complete(monkeypatch):
    """When a project completed earlier, a subsequent prompt turn must still stream all tokens."""
    from cli.tui import commands
    from cli.tui.state import ProjectPhase

    stream_events = [
        {"name": "turn.started", "sequence": 1, "payload": {"operation_id": "turn-2", "prompt": "fix ball physics"}},
        {"name": "token.delta", "sequence": 2, "payload": {"operation_id": "turn-2", "delta": "Adjusting "}},
        {"name": "token.delta", "sequence": 3, "payload": {"operation_id": "turn-2", "delta": "spring stiffness."}},
        {"name": "turn.completed", "sequence": 4, "payload": {"operation_id": "turn-2"}},
    ]

    class _Adapter:
        def stream(self, *args, **kwargs):
            return iter(stream_events)

    class _Client:
        pass

    redraws: list[dict] = []
    monkeypatch.setattr(commands, "redraw_full_screen", lambda *a, **k: redraws.append(k))
    monkeypatch.setattr(commands, "install_resize_handler", lambda: None)
    monkeypatch.setattr(commands, "check_terminal_resize", lambda cols, lines: (False, cols, lines))

    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.is_complete = True
    state.phase = ProjectPhase.PROJECT_COMPLETE

    commands._stream_turn_execution(
        _Adapter(),
        _Client(),
        {"session_id": "sess-1"},
        state,
        op_id="turn-2",
        is_tty=True,
    )

    # Must contain the streamed response and NOT the autonomous delivery complete message
    assert len(state.messages) == 1
    last_msg = state.messages[-1]
    assert last_msg.role == "assistant"
    assert "Adjusting spring stiffness." in last_msg.content
    assert "Autonomous delivery complete" not in last_msg.content


def test_subsequent_turn_actions_do_not_leak_prior_turn_actions(monkeypatch):
    """A subsequent turn must never attach actions from a prior completed turn."""
    from cli.tui import commands
    from cli.tui.state import TurnAction

    # Stale actions from a prior turn (e.g. Gitops writing files)
    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.current_turn_actions.actions.append(
        TurnAction(action_id="act-1", tool_name="write_file", target="VERSION.md", description="Write(VERSION.md)", status="COMPLETED")
    )
    assert not state.current_turn_actions.is_empty

    class _Adapter:
        def stream(self, *args, **kwargs):
            return iter([
                {"name": "turn.started", "sequence": 1, "payload": {"operation_id": "turn-3"}},
                {"name": "token.delta", "sequence": 2, "payload": {"operation_id": "turn-3", "delta": "Hello"}},
                {"name": "turn.completed", "sequence": 3, "payload": {"operation_id": "turn-3"}},
            ])

    class _Client:
        pass

    monkeypatch.setattr(commands, "redraw_full_screen", lambda *a, **k: None)
    monkeypatch.setattr(commands, "install_resize_handler", lambda: None)
    monkeypatch.setattr(commands, "check_terminal_resize", lambda cols, lines: (False, cols, lines))

    commands._stream_turn_execution(
        _Adapter(),
        _Client(),
        {"session_id": "sess-1"},
        state,
        op_id="turn-3",
        is_tty=True,
    )

    last_msg = state.messages[-1]
    assert last_msg.content == "Hello"
    # Actions should be None or empty, never containing the stale VERSION.md action
    if last_msg.actions is not None:
        assert last_msg.actions.is_empty or not any(a.target == "VERSION.md" for a in last_msg.actions.actions)


def test_process_event_operation_completed_resets_attempts_and_retries():
    """Terminal operation events must reset attempts and retries so idle frame never displays stale counters."""
    state = AutonomousDeliveryState(project_name="demo", session_id="sess-1")
    state.process_event({
        "name": "turn.started",
        "payload": {"attempt": 1, "retry": 0, "max_attempts": 10, "max_retries": 10},
    })
    assert state.retry_status_display == "(1/10 Attempt . 0/10 Retry)"

    state.process_event({
        "name": "operation.completed",
        "payload": {"operation_id": "op-1", "role": "architecture"},
    })
    assert state.retry_status_display is None
    assert state.attempt_count == 0
    assert state.retry_count == 0
