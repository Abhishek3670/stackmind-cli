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
