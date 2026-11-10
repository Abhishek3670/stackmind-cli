"""Tests for Mouse Scrollbar Interaction and Clipboard Copy/Paste in StackMind TUI."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import io
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


def test_copy_slash_command_copies_suggested_goal():
    """Verify :copy goal extracts Architect's suggested :goal command to clipboard."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.add_message("user", "Hello")
    state.add_message(
        "assistant",
        "Send this command to set our goal:\n"
        ":goal Build a cozy, rustic static landing page for a crochet artist using React and Tailwind.\n"
        "Let me know once ready!",
    )

    mock_adapter = MagicMock()
    mock_client = MagicMock()

    with patch("cli.tui.clipboard.set_clipboard_text", return_value=True) as mock_set:
        session, should_exit = dispatch_delivery_command(
            mock_adapter,
            mock_client,
            {"session_id": "test-session"},
            ":copy goal",
            state,
        )

        assert should_exit is False
        expected_goal = ":goal Build a cozy, rustic static landing page for a crochet artist using React and Tailwind."
        mock_set.assert_called_once_with(expected_goal)
        last_msg = state.messages[-1]
        assert "Copied" in last_msg.content
        assert "suggested :goal" in last_msg.content


def test_adopt_slash_command_executes_suggested_goal():
    """Verify :adopt automatically adopts and routes the Architect's suggested goal."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.add_message(
        "assistant",
        "To start the build, run:\n`:goal Build a cozy landing page with React and Tailwind`",
    )

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = {"operation_id": "op-submit-1"}
    mock_adapter.stream.return_value = iter([])

    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"status": "COMPLETED"}

    session, should_exit = dispatch_delivery_command(
        mock_adapter,
        mock_client,
        {"session_id": "test-session"},
        ":adopt",
        state,
        client_timeout=0.1,
    )

    assert any("[AUTO-ADOPT]" in m.content for m in state.messages)
    assert any(":goal Build a cozy landing page with React and Tailwind" in m.content for m in state.messages)


def test_goal_command_auto_adopts_when_no_args():
    """Verify :goal with no arguments auto-adopts the Architect's suggested goal."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.add_message(
        "assistant",
        "Please confirm goal:\n:goal Build a rustic website with Tailwind\nReady?",
    )

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = {"operation_id": "op-submit-2"}
    mock_adapter.stream.return_value = iter([])

    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"status": "COMPLETED"}

    session, should_exit = dispatch_delivery_command(
        mock_adapter,
        mock_client,
        {"session_id": "test-session"},
        ":goal",
        state,
        client_timeout=0.1,
    )

    # Check that it adopted the goal and recorded it
    assert any("[AUTO-ADOPT]" in m.content for m in state.messages)
    assert any(":goal Build a rustic website with Tailwind" in m.content for m in state.messages)


def test_toggle_runtime_panel_layout():
    """Verify toggling runtime panel collapses and restores 100% full-width layout."""
    from cli.tui.layout import compute_layout

    state = AutonomousDeliveryState(session_id="test-session")
    assert state.runtime_panel_visible is True

    # Default wide layout: 2 columns
    layout_default = compute_layout(120, state=state)
    assert layout_default.show_runtime is True
    assert layout_default.conversation_width < 120

    # Toggle sidebar off (Ctrl+B)
    state.toggle_runtime_panel()
    assert state.runtime_panel_visible is False

    # Layout is now 100% full-width, zero right panel
    layout_collapsed = compute_layout(120, state=state)
    assert layout_collapsed.show_runtime is False
    assert layout_collapsed.conversation_width == 120
    assert layout_collapsed.runtime_width == 0

    # Toggle sidebar back on
    state.toggle_runtime_panel()
    assert state.runtime_panel_visible is True
    layout_restored = compute_layout(120, state=state)
    assert layout_restored.show_runtime is True


def test_tab_and_ctrl_g_auto_fill_suggested_goal_in_editor():
    """Verify Tab and Ctrl+G in RawLineEditor automatically populate suggested goal."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.add_message(
        "assistant",
        "Run this command:\n:goal Build a cozy crochet portfolio with React\nThanks!",
    )

    # 1. Pressing Tab in empty editor
    editor = RawLineEditor(state=state)
    assert editor.text == ""
    editor.handle_key(Key.TAB)
    assert editor.text == ":goal Build a cozy crochet portfolio with React"

    # 2. Pressing Ctrl+G
    editor2 = RawLineEditor(state=state)
    editor2.handle_key(Key.CTRL_G)
    assert editor2.text == ":goal Build a cozy crochet portfolio with React"

    # 3. Pressing Tab when buffer starts with :goal
    editor3 = RawLineEditor(state=state, initial_text=":goal ")
    editor3.handle_key(Key.TAB)
    assert editor3.text == ":goal Build a cozy crochet portfolio with React"


def test_ctrl_b_toggles_runtime_panel_in_editor():
    """Verify Ctrl+B in RawLineEditor toggles runtime sidebar visibility."""
    state = AutonomousDeliveryState(session_id="test-session")
    assert state.runtime_panel_visible is True

    editor = RawLineEditor(state=state)
    editor.handle_key(Key.CTRL_B)
    assert state.runtime_panel_visible is False

    editor.handle_key(Key.CTRL_B)
    assert state.runtime_panel_visible is True


def test_mouse_command_toggles_and_sets_mode():
    """Verify :mouse slash command configures mouse_reporting_enabled and emits SGR controls."""
    state = AutonomousDeliveryState(session_id="test-session")
    state.mouse_reporting_enabled = True

    mock_adapter = MagicMock()
    mock_client = MagicMock()

    # 1. :mouse off
    with patch("sys.stdout", io.StringIO()) as buf:
        session, should_exit = dispatch_delivery_command(
            mock_adapter, mock_client, {"session_id": "test-session"}, ":mouse off", state
        )
        assert should_exit is False
        assert state.mouse_reporting_enabled is False
        assert "disabled" in state.messages[-1].content
        assert "\x1b[?1000l" in buf.getvalue()

    # 2. :mouse on
    with patch("sys.stdout", io.StringIO()) as buf:
        session, should_exit = dispatch_delivery_command(
            mock_adapter, mock_client, {"session_id": "test-session"}, ":mouse on", state
        )
        assert should_exit is False
        assert state.mouse_reporting_enabled is True
        assert "enabled" in state.messages[-1].content
        assert "\x1b[?1000h" in buf.getvalue()

    # 3. :mouse toggle
    with patch("sys.stdout", io.StringIO()) as buf:
        session, should_exit = dispatch_delivery_command(
            mock_adapter, mock_client, {"session_id": "test-session"}, ":mouse", state
        )
        assert should_exit is False
        assert state.mouse_reporting_enabled is False
        assert "OFF" in state.messages[-1].content

        session, should_exit = dispatch_delivery_command(
            mock_adapter, mock_client, {"session_id": "test-session"}, ":mouse", state
        )
        assert state.mouse_reporting_enabled is True
        assert "ON" in state.messages[-1].content


def test_terminal_state_restorer_windows_console_modes():
    """Verify Windows console input mode correctly enables mouse input and disables quick-edit."""
    import ctypes
    from cli.tui.keyboard import TerminalStateRestorer

    mock_kernel32 = MagicMock()
    # Initial mode with QuickEdit mode (0x0040)
    initial_mode = 0x0040 | 0x0001 | 0x0002

    def fake_get_console_mode(h, ref):
        ref._obj.value = initial_mode
        return 1

    mock_kernel32.GetStdHandle.return_value = 100
    mock_kernel32.GetConsoleMode.side_effect = fake_get_console_mode

    # Test with mouse enabled
    state_mouse_on = AutonomousDeliveryState(session_id="test-s1")
    state_mouse_on.mouse_reporting_enabled = True

    with patch("os.name", "nt"), patch("sys.stdin.isatty", return_value=True), patch("ctypes.WinDLL", return_value=mock_kernel32):
        with TerminalStateRestorer(state=state_mouse_on):
            # Check SetConsoleMode was called with ENABLE_MOUSE_INPUT (0x0010) set and QuickEdit (0x0040) cleared
            set_call = mock_kernel32.SetConsoleMode.call_args[0]
            new_mode = set_call[1]
            assert new_mode & 0x0010 != 0  # ENABLE_MOUSE_INPUT
            assert new_mode & 0x0040 == 0  # ENABLE_QUICK_EDIT_MODE cleared

        # On exit, original mode restored
        restore_call = mock_kernel32.SetConsoleMode.call_args[0]
        assert restore_call[1] == initial_mode

    # Test with mouse disabled
    state_mouse_off = AutonomousDeliveryState(session_id="test-s2")
    state_mouse_off.mouse_reporting_enabled = False

    with patch("os.name", "nt"), patch("sys.stdin.isatty", return_value=True), patch("ctypes.WinDLL", return_value=mock_kernel32):
        with TerminalStateRestorer(state=state_mouse_off):
            set_call = mock_kernel32.SetConsoleMode.call_args[0]
            new_mode = set_call[1]
            assert new_mode & 0x0010 == 0  # ENABLE_MOUSE_INPUT cleared
            assert new_mode & 0x0040 != 0  # ENABLE_QUICK_EDIT_MODE preserved


def test_poll_stream_key_mouse_events_windows():
    """Verify poll_stream_key correctly yields mouse wheel events on Windows."""
    from cli.tui.keyboard import Key, poll_stream_key

    with patch("os.name", "nt"):
        with patch("cli.tui.keyboard.has_pending_input", return_value=True):
            with patch("cli.tui.keyboard.read_next_key", return_value=Key.MOUSE_WHEEL_UP):
                assert poll_stream_key() == Key.MOUSE_WHEEL_UP

        with patch("cli.tui.keyboard.has_pending_input", return_value=False):
            assert poll_stream_key() is None


def test_mouse_command_in_dropdown_and_command_dialog():
    """Verify :mouse appears in autocomplete dropdown and command dialog."""
    from cli.tui.command_dialog import COMMAND_NAMES
    from cli.tui.keyboard import DEFAULT_COMMANDS, RawLineEditor

    # 1. Verify in COMMAND_NAMES
    assert ":mouse" in COMMAND_NAMES
    assert COMMAND_NAMES[":mouse"] == "Mouse Tracking"

    # 2. Verify in DEFAULT_COMMANDS
    cmd_names = [c[0] for c in DEFAULT_COMMANDS]
    assert ":mouse" in cmd_names

    # 3. Verify dropdown matching when user types ':m'
    editor = RawLineEditor()
    editor.insert_char(":")
    editor.insert_char("m")
    assert editor.dropdown_active is True
    matches = [c[0] for c in editor.dropdown_matches]
    assert ":matrix" in matches
    assert ":mouse" in matches





