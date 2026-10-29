"""Tests for TUI System Command Dialog above composer text area."""

from __future__ import annotations

from typing import Any
from rich.panel import Panel

from cli.tui.app import dispatch_delivery_command
from cli.tui.chat import render_chat_transcript_str
from cli.tui.command_dialog import (
    compute_command_dialog_height,
    render_command_dialog,
    render_command_dialog_str,
)
from cli.tui.layout import compute_viewport_height, render_full_screen_workspace
from cli.tui.state import AutonomousDeliveryState, RoleStatus, WorkOrderItem


class _MockAdapter:
    def __init__(self):
        self.commands_sent: list[tuple[str, dict[str, Any]]] = []

    def command(self, cmd: str, **kwargs: Any) -> Any:
        self.commands_sent.append((cmd, kwargs))
        if cmd == ":status":
            return {"session_id": "sess-test", "state": "IDLE", "workspace": "."}
        if cmd == ":resume":
            return {"session_id": "sess-test", "state": "RUNNING", "workspace": "."}
        if cmd == ":diff":
            return "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new"
        return {"session_id": "sess-test"}


class _MockClient:
    def events(self, session_id: str, after: int = 0) -> list[Any]:
        return []

    def run_resume(self, session_id: str, run_id: str | None = None) -> dict[str, Any]:
        return {"run_id": "run-001", "phase": "AUTONOMOUS_EXECUTION"}


def test_command_dialog_height_and_rendering():
    state = AutonomousDeliveryState(session_id="sess-1")
    # 1. Closed dialog has 0 height
    assert compute_command_dialog_height(state, height=24) == 0

    # 2. Open dialog with 2 lines output
    state.show_command_dialog(":resume", "Session RUNNING\nSupervisor run resumed")
    assert state.command_dialog_open is True
    assert state.command_dialog_command == ":resume"

    h = compute_command_dialog_height(state, height=24)
    # 2 content lines + 2 border rows = 4 lines
    assert h == 4

    # 3. Rendered panel contains command title and output
    panel = render_command_dialog(state, width=80, height=24)
    assert isinstance(panel, Panel)
    panel_str = render_command_dialog_str(state, width=80, height=24, no_color=True)
    assert "Resume" in panel_str
    assert "⚙" not in panel_str
    assert "Session RUNNING" in panel_str
    assert "Supervisor run resumed" in panel_str


def test_status_command_output_routes_to_dialog_and_does_not_clutter_chat():
    adapter = _MockAdapter()
    client = _MockClient()
    session = {"session_id": "sess-test", "workspace": "."}
    state = AutonomousDeliveryState(session_id="sess-test")
    state.roles["Backend"] = RoleStatus("Backend", "Codex", state="ACTIVE")
    state.work_orders.append(WorkOrderItem(id="WO-001", title="Backend service", status="ACTIVE"))

    # Initial chat messages
    state.add_message("user", "Hello assistant")
    state.add_message("assistant", "Hello! How can I help you today?")

    # Run :status
    dispatch_delivery_command(adapter, client, session, ":status", state)

    # 1. Dialog must be open with :status output
    assert state.command_dialog_open is True
    assert state.command_dialog_command == ":status"
    assert "sess-test" in state.command_dialog_output

    # 2. Chat transcript must NOT contain :status or the status output dump
    chat_str = render_chat_transcript_str(state.messages, width=80, no_color=True)
    assert "Hello assistant" in chat_str
    assert "Hello! How can I help you today?" in chat_str
    assert ":status" not in chat_str
    assert "PROJECT DELIVERY VIEW" not in chat_str

    # 3. State messages still retain the record for test/session history compatibility
    assert state.messages[-2].content == ":status"
    assert state.messages[-2].is_command is True
    assert state.messages[-1].is_command is True


def test_resume_command_output_routes_to_dialog_and_does_not_clutter_chat():
    adapter = _MockAdapter()
    client = _MockClient()
    session = {"session_id": "sess-test", "workspace": "."}
    state = AutonomousDeliveryState(session_id="sess-test")

    # Run :resume
    dispatch_delivery_command(adapter, client, session, ":resume", state)

    assert state.command_dialog_open is True
    assert state.command_dialog_command == ":resume"
    assert "Session RUNNING" in state.command_dialog_output

    chat_str = render_chat_transcript_str(state.messages, width=80, no_color=True)
    assert ":resume" not in chat_str
    assert "Session RUNNING" not in chat_str


def test_dialog_positioned_just_above_composer_in_workspace():
    session = {"session_id": "sess-test", "workspace": "."}
    state = AutonomousDeliveryState(session_id="sess-test")
    state.show_command_dialog(":status", "Session: sess-test\nStatus: ACTIVE")

    frame = render_full_screen_workspace(
        session=session,
        state=state,
        width=100,
        height=30,
        no_color=True,
    )

    # The command dialog must be present in the full-screen frame with friendly name and no gear icon
    assert "Status" in frame
    assert "⚙" not in frame
    assert "Session: sess-test" in frame
    # The composer box placeholder must also be present
    assert "Type a message..." in frame

    # Verify vertical ordering: Command dialog appears before (above) composer box
    idx_dialog = frame.find("Status")
    idx_composer = frame.find("Type a message...")
    assert idx_dialog != -1
    assert idx_composer != -1
    assert idx_dialog < idx_composer, "Command dialog must be positioned ABOVE composer text area"


def test_dialog_scroll_and_dismissal():
    state = AutonomousDeliveryState(session_id="sess-1")
    lines = [f"Line {i}" for i in range(50)]
    state.show_command_dialog(":help", "\n".join(lines))
    assert state.command_dialog_scroll == 0

    state.scroll_command_dialog_down(lines=5)
    assert state.command_dialog_scroll == 5

    state.scroll_command_dialog_up(lines=2)
    assert state.command_dialog_scroll == 3

    # Dismissal
    state.close_command_dialog()
    assert state.command_dialog_open is False
    assert state.command_dialog_output == ""
    assert compute_command_dialog_height(state, height=24) == 0


def test_normal_prompt_submission_closes_command_dialog():
    adapter = _MockAdapter()
    client = _MockClient()
    session = {"session_id": "sess-test", "workspace": "."}
    state = AutonomousDeliveryState(session_id="sess-test")

    # First open dialog via command
    dispatch_delivery_command(adapter, client, session, ":status", state)
    assert state.command_dialog_open is True

    # User enters regular message (not starting with :)
    state.add_message("user", "Please implement the API")
    assert state.command_dialog_open is False


def test_escape_key_closes_dialog_and_triggers_full_redraw():
    from cli.tui.keyboard import Key, LineEditor

    state = AutonomousDeliveryState(session_id="sess-1")
    state.show_command_dialog(":status", "Session online")
    assert state.command_dialog_open is True

    redraw_calls = []

    def mock_height_change(editor, clear=False):
        redraw_calls.append(("height_change", clear))

    def mock_clear_dropdown():
        redraw_calls.append(("clear_dropdown", True))

    editor = LineEditor(
        state=state,
        on_height_change=mock_height_change,
        on_clear_dropdown=mock_clear_dropdown,
    )

    # Pressing ESCAPE must close the dialog and trigger a full screen redraw with clear=True
    editor.handle_key(Key.ESCAPE)
    assert state.command_dialog_open is False
    assert state.command_dialog_output == ""
    assert ("height_change", True) in redraw_calls


def test_page_up_and_down_scrolls_dialog():
    from cli.tui.keyboard import Key, LineEditor

    state = AutonomousDeliveryState(session_id="sess-1")
    state.show_command_dialog(
        ":help", "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10"
    )
    assert state.command_dialog_scroll == 0

    editor = LineEditor(state=state)
    editor.handle_key(Key.PAGE_DOWN)
    assert state.command_dialog_scroll == 5

    editor.handle_key(Key.PAGE_UP)
    assert state.command_dialog_scroll == 0

def test_command_dialog_docked_layout_and_no_vacated_space():
    """Verify dialog is docked inside conversation column, side panel retains full height, and composer preserves text area layout."""
    session = {"session_id": "sess-test", "workspace": "."}
    state = AutonomousDeliveryState(session_id="sess-test")
    state.roles["Backend"] = RoleStatus("Backend", "Codex", state="ACTIVE")
    state.show_command_dialog(":status", "Session: sess-test\nStatus: ACTIVE")

    frame = render_full_screen_workspace(
        session=session,
        state=state,
        width=120,
        height=30,
        no_color=True,
    )

    lines = frame.split("\n")
    assert len(lines) == 30

    # Workspace rows (0-25) span full 120 width: left has chat/dialog, right has runtime side panel
    for idx in range(26):
        assert len(lines[idx]) == 120, f"Workspace line {idx} had length {len(lines[idx])} != 120 (vacated right space detected)"

    # Composer rows (26-28) and status bar (29) are strictly aligned with conversation column (width 86)
    for idx in range(26, 30):
        assert len(lines[idx]) == 86, f"Bottom line {idx} had length {len(lines[idx])} != 86 (text area layout distorted)"

    # Dialog title, side panel, and composer must be present
    assert "Status" in frame
    assert "Backend" in frame
    assert "Type a message..." in frame
