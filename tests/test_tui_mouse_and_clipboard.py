"""Tests for Mouse Scrollbar Interaction and Clipboard Copy/Paste in StackMind TUI."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from cli.tui.clipboard import get_clipboard_text, set_clipboard_text
from cli.tui.commands import dispatch_delivery_command
from cli.tui.keyboard import Key, RawLineEditor
from cli.tui.state import AutonomousDeliveryState


def test_clipboard_get_and_set_mocked():
    """Verify clipboard get and set work with platform abstraction."""
    with patch("cli.tui.clipboard._set_clipboard_windows", return_value=True) as mock_set:
        with patch("cli.tui.clipboard._get_clipboard_windows", return_value="pasted_content") as mock_get:
            with patch("os.name", "nt"):
                assert set_clipboard_text("test_data") is True
                assert get_clipboard_text() == "pasted_content"
                mock_set.assert_called_once_with("test_data")
                mock_get.assert_called_once()


def test_ctrl_v_pastes_from_clipboard():
    """Verify pressing Ctrl+V pastes clipboard content into the editor buffer."""
    editor = RawLineEditor()
    editor.insert_char("start: ")

    with patch("cli.tui.clipboard.get_clipboard_text", return_value="clipboard_text_here"):
        # Handle Key.CTRL_V
        editor.handle_key(Key.CTRL_V)

    assert editor.text == "start: clipboard_text_here"


def test_ctrl_v_multiline_paste():
    """Verify Ctrl+V properly handles newlines without submitting."""
    editor = RawLineEditor()
    with patch("cli.tui.clipboard.get_clipboard_text", return_value="line 1\r\nline 2\nline 3"):
        editor.handle_key(Key.CTRL_V)

    assert editor.text == "line 1\nline 2\nline 3"


def test_mouse_right_click_pastes_from_clipboard():
    """Verify right-click event (<MOUSE_2_...>) triggers paste from clipboard."""
    editor = RawLineEditor()
    with patch("cli.tui.clipboard.get_clipboard_text", return_value="right_clicked_data"):
        # SGR Right click press
        editor.handle_key("<MOUSE_2_15_20_M>")

    assert editor.text == "right_clicked_data"


def test_mouse_left_click_triggers_callback():
    """Verify left-click on terminal elements invokes on_mouse_click callback."""
    clicked = []

    def mock_click(col: int, row: int, btn: int):
        clicked.append((col, row, btn))

    editor = RawLineEditor(on_mouse_click=mock_click)
    # SGR Left click press at col 60, row 12
    editor.handle_key("<MOUSE_0_60_12_M>")

    assert len(clicked) == 1
    assert clicked[0] == (60, 12, 0)
    # Text buffer must remain completely empty (mouse events never leak into input)
    assert editor.text == ""


def test_copy_slash_command_copies_latest_response():
    """Verify :copy slash command writes the latest assistant message to clipboard."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.add_message("user", "Hello")
    state.add_message("assistant", "This is the generated answer to copy.")

    mock_adapter = MagicMock()
    mock_client = MagicMock()

    with patch("cli.tui.clipboard.set_clipboard_text", return_value=True) as mock_set:
        session, should_exit = dispatch_delivery_command(
            mock_adapter,
            mock_client,
            {"session_id": "test-session"},
            ":copy",
            state,
        )

        assert should_exit is False
        mock_set.assert_called_once_with("This is the generated answer to copy.")
        # Check system notification message was recorded
        last_msg = state.messages[-1]
        assert "Copied" in last_msg.content
        assert "latest response" in last_msg.content


def test_copy_slash_command_copies_full_transcript():
    """Verify :copy all copies the full conversation transcript."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.add_message("user", "Question 1")
    state.add_message("assistant", "Answer 1")

    mock_adapter = MagicMock()
    mock_client = MagicMock()

    with patch("cli.tui.clipboard.set_clipboard_text", return_value=True) as mock_set:
        session, should_exit = dispatch_delivery_command(
            mock_adapter,
            mock_client,
            {"session_id": "test-session"},
            ":copy all",
            state,
        )

        assert should_exit is False
        assert mock_set.call_count == 1
        copied = mock_set.call_args[0][0]
        assert "Question 1" in copied
        assert "Answer 1" in copied


def test_mouse_drag_triggers_callback():
    """Verify dragging the mouse (<MOUSE_32_...>) triggers on_mouse_click with button=32."""
    clicked = []

    def mock_click(col: int, row: int, btn: int):
        clicked.append((col, row, btn))

    editor = RawLineEditor(on_mouse_click=mock_click)
    # SGR drag event with left button held: button 32, col 78, row 15
    editor.handle_key("<MOUSE_32_78_15_M>")

    assert len(clicked) == 1
    assert clicked[0] == (78, 15, 32)
    assert editor.text == ""


def test_scrollbar_drag_scroll_calculation():
    """Verify scrollbar drag and offset calculation works accurately."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.conversation_scroll.total_lines = 100
    state.conversation_scroll.viewport_height = 20
    state.conversation_scroll.scroll_offset = 0

    from cli.tui.layout import compute_layout

    layout = compute_layout(80, 24, show_runtime=getattr(state, "runtime_panel_visible", False))
    scrollbar_col = layout.conversation_width

    # Drag on scrollbar column
    col, row, btn = scrollbar_col, 10, 32
    tot_l = state.conversation_scroll.total_lines
    vp_h = state.conversation_scroll.viewport_height
    thumb_size = max(1, int(round(vp_h * (vp_h / tot_l))))
    max_off = tot_l - vp_h
    travel_height = max(1, vp_h - thumb_size)
    click_r = max(0, min(vp_h - 1, row - 1))
    thumb_pos = max(0, min(click_r, travel_height))
    ratio = thumb_pos / travel_height
    target_start = int(round(ratio * max_off))
    target_off = max(0, min(max_off, max_off - target_start))
    state.set_conversation_scroll_offset(target_off)

    assert 0 < state.conversation_scroll.scroll_offset < max_off


