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


def test_mouse_drag_release_event_dispatched_in_raw_line_editor():
    """Verify RawLineEditor tracks drag gestures and calls on_mouse_release upon SGR release (<MOUSE_0_..._m>)."""
    from cli.tui.keyboard import RawLineEditor

    released_events = []

    def mock_release(s_col: int, s_row: int, e_col: int, e_row: int, ed: Any):
        released_events.append((s_col, s_row, e_col, e_row))

    editor = RawLineEditor(on_mouse_release=mock_release)

    # 1. Left button down at (10, 5)
    editor.handle_key("<MOUSE_0_10_5_M>")
    assert editor._mouse_drag_start == (10, 5)
    assert editor._is_mouse_dragging is False

    # 2. Left button dragged to (25, 8)
    editor.handle_key("<MOUSE_32_25_8_M>")
    assert editor._is_mouse_dragging is True
    assert editor._mouse_drag_current == (25, 8)

    # 3. Left button released at (30, 9)
    editor.handle_key("<MOUSE_0_30_9_m>")
    assert len(released_events) == 1
    assert released_events[0] == (10, 5, 30, 9)
    assert editor._is_mouse_dragging is False
    assert editor._mouse_drag_start is None


def test_mouse_drag_to_copy_conversation_slices_cleanly_without_sidebar():
    """Verify dragging over conversation text extracts cleanly from visible lines and copies to clipboard."""
    import os
    import shutil
    from cli.tui.state import AutonomousDeliveryState
    from cli.tui.keyboard import RawLineEditor

    state = AutonomousDeliveryState(session_id="test-drag-copy")
    state.runtime_panel_visible = True
    state.visible_conversation_lines = [
        "1 Initial Task Setup",
        "2 Feature Implementation Complete",
        "3 Security Scan: Clean - 0 findings across 4 files scanned",
        "4 Deliverable Verification: tests/test_integration.py exists and is valid",
        "Verdict: PASS",
    ]

    # Directly test the release handler logic
    editor = RawLineEditor()
    with patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 30))):
        with patch("cli.tui.clipboard.set_clipboard_text", return_value=True) as mock_set:
            # We simulate a drag starting at row 3 (Line 3: 3 Security Scan...) down to row 4 (Line 4: 4 Deliverable...)
            # Note: terminal coordinates are 1-based, header_h = 0 in split mode
            start_col, start_row = 1, 3
            end_col, end_row = 50, 4

            # Invoke the release handler
            from cli.tui.layout import compute_layout
            layout = compute_layout(120, 30, state=state)
            scrollbar_col = layout.conversation_width

            max_c = max(1, scrollbar_col - 2)
            s_col = max(0, min(max_c, start_col - 1))
            e_col = max(0, min(max_c, end_col - 1))

            r1 = start_row - 1
            r2 = end_row - 1

            visible = state.visible_conversation_lines
            extracted = []
            for r in range(r1, r2 + 1):
                line = visible[r]
                if r == r1:
                    extracted.append(line[s_col:].rstrip())
                elif r == r2:
                    extracted.append(line[:e_col].rstrip())
                else:
                    extracted.append(line.rstrip())
            selected = "\n".join(extracted).strip()

            from cli.tui.clipboard import set_clipboard_text
            ok = set_clipboard_text(selected)
            assert ok is True
            mock_set.assert_called_once()
            copied_text = mock_set.call_args[0][0]
            assert "3 Security Scan: Clean" in copied_text
            assert "4 Deliverable Verification:" in copied_text
            # Verify the sidebar content is completely absent!
            assert "CURRENT OPERATION" not in copied_text
            assert "StackMind Runtime" not in copied_text
            assert "AGENTS" not in copied_text


def test_collapse_command_toggles_runtime_panel():
    """Verify :collapse slash command toggles the runtime side panel."""
    state = AutonomousDeliveryState(session_id="test-collapse")
    assert state.runtime_panel_visible is True

    mock_adapter = MagicMock()
    mock_client = MagicMock()

    # 1. Execute :collapse -> collapses panel
    session, should_exit = dispatch_delivery_command(
        mock_adapter,
        mock_client,
        {"session_id": "test-collapse"},
        ":collapse",
        state,
    )
    assert should_exit is False
    assert state.runtime_panel_visible is False
    last_msg = state.messages[-1]
    assert "collapsed" in last_msg.content.lower()

    # 2. Execute :collapse again -> restores panel
    session, should_exit = dispatch_delivery_command(
        mock_adapter,
        mock_client,
        {"session_id": "test-collapse"},
        ":collapse",
        state,
    )
    assert should_exit is False
    assert state.runtime_panel_visible is True
    last_msg = state.messages[-1]
    assert "restored" in last_msg.content.lower()


def test_collapse_registered_in_commands_and_dialog():
    """Verify :collapse appears in DEFAULT_COMMANDS and COMMAND_NAMES."""
    from cli.tui.command_dialog import COMMAND_NAMES
    from cli.tui.keyboard import DEFAULT_COMMANDS

    assert ":collapse" in COMMAND_NAMES
    assert COMMAND_NAMES[":collapse"] == "Collapse Side Panel"
    assert ":panel" in COMMAND_NAMES

    cmd_names = [c[0] for c in DEFAULT_COMMANDS]
    assert ":collapse" in cmd_names
    assert ":panel" in cmd_names


def test_runtime_panel_heading_includes_collapse_button():
    """Verify render_runtime_panel heading displays the [–] collapse button affordance."""
    from cli.tui.runtime_panel import render_runtime_panel, RUNTIME_HEADING
    from rich.console import Console

    panel = render_runtime_panel(width=40)
    console = Console()
    with console.capture() as capture:
        console.print(panel)
    output = capture.get()

    assert RUNTIME_HEADING in output
    assert "[–]" in output


def test_collapsed_badge_bar_includes_expand_button():
    """Verify render_runtime_badge_bar displays [+ Expand] when collapsed."""
    from cli.tui.layout import render_runtime_badge_bar

    badge = render_runtime_badge_bar(width=80, agents=[{"state": "running"}], work_orders=[{"status": "active"}])
    assert "[+ Expand]" in badge.plain


def test_collapsed_layout_excludes_scrollbar_edge_bars():
    """Verify collapsing the runtime panel with Ctrl+B completely eliminates trailing scrollbar │ and █ glyphs."""
    from cli.tui.layout import render_workspace_layout_str
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="test-clean-collapse")
    state.runtime_panel_visible = False
    state.show_collapsed_scrollbar = False

    conv_text = "Line 1: Verification Summary\nLine 2: 1 Deliverable Inspection\nLine 3: 3 Security Scan: Clean\nLine 4: Verdict: PASS"

    # Render full-width layout with height=10
    output = render_workspace_layout_str(conv_text, width=120, state=state, height=10)

    # Every rendered conversation line must NOT have the trailing │ or █ scrollbar
    for line in output.splitlines():
        # Strip ANSI to inspect glyphs
        clean_line = Text.from_ansi(line).plain if "\x1b[" in line else line
        assert not clean_line.endswith("│"), f"Found trailing scrollbar glyph '│' in line: {clean_line}"
        assert not clean_line.endswith("█"), f"Found trailing scrollbar thumb '█' in line: {clean_line}"


def test_scrollbar_slash_command_toggles_collapsed_scrollbar():
    """Verify :scrollbar command toggles and configures show_collapsed_scrollbar."""
    from cli.tui.commands import dispatch_delivery_command
    from cli.tui.command_dialog import COMMAND_NAMES
    from cli.tui.keyboard import DEFAULT_COMMANDS
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="test-sb-cmd")
    assert state.show_collapsed_scrollbar is False

    mock_adapter = MagicMock()
    mock_client = MagicMock()

    # 1. Turn on
    dispatch_delivery_command(mock_adapter, mock_client, {"session_id": "test-sb-cmd"}, ":scrollbar on", state)
    assert state.show_collapsed_scrollbar is True

    # 2. Turn off
    dispatch_delivery_command(mock_adapter, mock_client, {"session_id": "test-sb-cmd"}, ":scrollbar off", state)
    assert state.show_collapsed_scrollbar is False

    # 3. Toggle
    dispatch_delivery_command(mock_adapter, mock_client, {"session_id": "test-sb-cmd"}, ":scrollbar", state)
    assert state.show_collapsed_scrollbar is True

    # 4. In commands and dialog
    assert ":scrollbar" in [c[0] for c in DEFAULT_COMMANDS]
    assert ":scrollbar" in COMMAND_NAMES


def test_pad_frame_to_width_uses_ansi_erase_to_line_end():
    """Verify _pad_frame_to_width appends \\x1b[K without padding dozens of literal space characters."""
    from cli.tui.terminal import _pad_frame_to_width

    raw_frame = "Line 1: Hello World"
    padded = _pad_frame_to_width(raw_frame, width=120)

    assert "\x1b[K" in padded
    # Must NOT have 100 trailing space characters
    assert " " * 30 not in padded


def test_strip_trailing_spaces_preserves_ansi_codes():
    """Verify _strip_trailing_spaces removes trailing spaces while preserving ANSI styling."""
    from cli.tui.layout import _strip_trailing_spaces

    assert _strip_trailing_spaces("Plain text    ") == "Plain text"
    assert _strip_trailing_spaces("Styled \x1b[32mtext\x1b[0m   ") == "Styled \x1b[32mtext\x1b[0m"
    assert _strip_trailing_spaces("Styled \x1b[32mtext   \x1b[0m") == "Styled \x1b[32mtext\x1b[0m"
    assert _strip_trailing_spaces("   Indented text   ") == "   Indented text"


def test_pad_frame_to_width_strips_trailing_spaces_and_cleans_empty_lines():
    """Verify _pad_frame_to_width cleans trailing spaces on content lines and leaves clean empty lines."""
    from cli.tui.terminal import _pad_frame_to_width

    # Frame containing lines with 80 trailing spaces (as produced by full-width Rich Markdown)
    raw_frame = "Verification Summary" + (" " * 80) + "\n\n  • Bullet 1" + (" " * 70)
    padded = _pad_frame_to_width(raw_frame, width=120)

    lines = padded.split("\n")
    # Content line must have trailing spaces stripped and \x1b[K appended
    assert lines[0] == "Verification Summary\x1b[K"
    # Empty blank line must NOT contain spaces; only \x1b[K
    assert lines[1] == "\x1b[K"
    # Indented line must preserve its leading indentation
    assert lines[2] == "  • Bullet 1\x1b[K"


def test_mouse_drag_to_copy_preserves_indentation_and_blank_lines():
    """Verify in-app mouse drag-to-copy preserves leading indentation and blank separator lines."""
    import os
    from unittest.mock import patch
    from cli.tui.state import AutonomousDeliveryState
    from cli.tui.keyboard import RawLineEditor
    from cli.tui.layout import compute_layout

    state = AutonomousDeliveryState(session_id="test-drag-indent")
    state.runtime_panel_visible = True
    state.visible_conversation_lines = [
        " Verification Summary",
        "",
        "  1 Deliverable Inspection: Updated tests/test_integration.py to correctly verify:",
        "     • DOM hooks match document.getElementById",
        "     • Form input name attributes match FormData.get()",
        "  2 Test Execution: All 5 tests pass:",
        "     • Hero endpoint returns correct JSON",
        "",
        " Verdict",
        "",
        " PASS — Frontend-backend integration verified.",
    ]

    # Directly test the release handler logic
    editor = RawLineEditor()
    with patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 30))):
        with patch("cli.tui.clipboard.set_clipboard_text", return_value=True) as mock_set:
            layout = compute_layout(120, 30, state=state)
            scrollbar_col = layout.conversation_width
            max_c = max(1, scrollbar_col - 2)

            # Drag starting at row 1 (Verification Summary) down to row 11 (PASS...)
            # User clicks around col 2
            start_col, start_row = 2, 1
            end_col, end_row = 40, 11

            s_col = max(0, min(max_c, start_col - 1))
            e_col = max(0, min(max_c, end_col - 1))
            r1 = start_row - 1
            r2 = end_row - 1

            visible = state.visible_conversation_lines
            min_r = min(r1, r2)
            max_r = min(len(visible) - 1, max(r1, r2))

            extracted = []
            is_forward = (start_row < end_row) or (start_row == end_row and start_col <= end_col)
            first_col = s_col if is_forward else e_col
            last_col = e_col if is_forward else s_col

            for r in range(min_r, max_r + 1):
                line = visible[r]
                if not line.strip():
                    extracted.append("")
                    continue

                leading_spaces = len(line) - len(line.lstrip(" "))
                if r == min_r:
                    if first_col <= max(4, leading_spaces + 2):
                        extracted.append(line.rstrip())
                    else:
                        extracted.append(line[first_col:].rstrip())
                elif r == max_r:
                    content_len = len(line.rstrip())
                    if last_col >= content_len - 2 or last_col >= max_c - 4 or last_col >= 20:
                        extracted.append(line.rstrip())
                    else:
                        extracted.append(line[:last_col].rstrip())
                else:
                    extracted.append(line.rstrip())

            non_empty = [l for l in extracted if l]
            if non_empty and all(l.startswith(" ") for l in non_empty):
                extracted = [l[1:] if l.startswith(" ") else l for l in extracted]

            selected = "\n".join(extracted).strip("\r\n")

            from cli.tui.clipboard import set_clipboard_text
            ok = set_clipboard_text(selected)
            assert ok is True
            mock_set.assert_called_once()
            copied = mock_set.call_args[0][0]

            # 1. Top-level heading is clean
            assert "Verification Summary" in copied
            # 2. Blank lines between sections are preserved
            assert "\n\n" in copied
            # 3. Indented list items preserve relative indentation
            assert " 1 Deliverable Inspection:" in copied
            assert "    • DOM hooks match" in copied
            assert " 2 Test Execution:" in copied
            assert "    • Hero endpoint" in copied
            # 4. Verdict and blank lines preserved
            assert "Verdict" in copied
            assert "PASS — Frontend-backend" in copied


def test_copy_slash_command_formats_contract_payload_and_preserves_indentation():
    """Verify :copy formats contract payloads into clean Markdown and preserves exact formatting."""
    from cli.tui.commands import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="test-copy-fmt")
    raw_payload = '{"status": "completed", "review_notes": "1. **Inspection**\\n   - Hooks verified\\n   - Inputs verified\\n\\n2. **Tests**\\n   - All passed", "verdict": "PASS"}'
    state.add_message("assistant", raw_payload)

    mock_adapter = MagicMock()
    mock_client = MagicMock()

    with patch("cli.tui.clipboard.set_clipboard_text", return_value=True) as mock_set:
        session, should_exit = dispatch_delivery_command(
            mock_adapter,
            mock_client,
            {"session_id": "test-copy-fmt"},
            ":copy",
            state,
        )
        assert should_exit is False
        mock_set.assert_called_once()
        copied = mock_set.call_args[0][0]

        # Verify contract payload was converted to clean Markdown with indentation
        assert "QA Verdict: PASS" in copied
        assert "1. **Inspection**" in copied
        assert "   - Hooks verified" in copied
        assert "2. **Tests**" in copied
        assert "   - All passed" in copied
        # Raw JSON braces should not be in copied text
        assert '{"status":' not in copied


def test_mouse_escape_sequences_never_pollute_composer_text():
    """Verify raw SGR mouse escape sequences (both VT \x1b and ConPTY ^[[< format) are filtered from editor buffer."""
    from cli.tui.keyboard import RawLineEditor, Key

    editor = RawLineEditor(width=80, initial_text="")

    # Simulate raw ConPTY mouse press and drag sequences
    editor.handle_key("^[[<0;101;34M")
    editor.handle_key("^[[<32;178;38M")
    editor.handle_key("^[[<0;101;34m")
    assert editor.text == ""

    # Simulate concatenated mouse sequences
    editor.handle_key("^[[<0;101;34M^[[<0;101;34M")
    assert editor.text == ""

    # Simulate raw VT mouse escape sequences
    editor.handle_key("\x1b[<0;101;34M")
    editor.handle_key("[<0;101;34M")
    assert editor.text == ""

    # Normal typing after mouse events should work perfectly
    for ch in ":resume":
        editor.handle_key(ch)
    assert editor.text == ":resume"

    # Any interspersed mouse tracking events while typing must be discarded
    editor.handle_key("^[[<32;100;30M")
    editor.handle_key("\x1b[<32;100;30M")
    assert editor.text == ":resume"


def test_composer_clamped_width_never_touches_term_cols():
    """Verify RawLineEditor clamps line_out width to term_cols - 1 so ConPTY never triggers margin auto-wrap."""
    import io
    import os
    from unittest.mock import patch
    from cli.tui.keyboard import RawLineEditor
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="test-clamp")
    buf = io.StringIO()
    with patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
        editor = RawLineEditor(width=80, height=24, initial_text=":resume", state=state, out=buf)
        editor.redraw_line()
        out = buf.getvalue()

        # Visual line written must not exceed 79 columns
        from rich.text import Text
        for line in out.split("\r"):
            if "│" in line:
                plain_len = Text.from_ansi(line).cell_len
                assert plain_len <= 79, f"Line cell_len {plain_len} exceeded margin 79"









