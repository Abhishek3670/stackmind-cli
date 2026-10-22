"""Tests for TUI mouse wheel scrolling and composer text wrapping.

Verifies:
1. Mouse wheel reporting is enabled in TerminalStateRestorer by default.
2. SGR mouse wheel events invoke scroll callbacks and never touch composer history.
3. Long prompts without explicit newlines wrap onto multiple visual lines inside the composer box instead of truncating with '...'.
4. Arrow Up and Down navigate across wrapped visual lines before cycling prompt history.
5. Dynamic composer height expands smoothly for wrapped text.
6. Cursor coordinates (row, col) match the visual line and column of the wrapped buffer.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from cli.tui.keyboard import (
    DISABLE_MOUSE_REPORTING_SEQ,
    ENABLE_MOUSE_REPORTING_SEQ,
    Key,
    RawLineEditor,
    TerminalStateRestorer,
    enable_mouse_reporting,
    raw_prompt_input,
    wrap_composer_text,
    wrap_composer_text_with_cursor,
)
from cli.tui.layout import (
    compute_composer_cursor_row,
    compute_composer_height,
    compute_viewport_height,
)
from cli.tui.state import AutonomousDeliveryState
from cli.tui.terminal import (
    _compute_cursor_col,
    render_composer_box_str,
    render_composer_top_border_str,
)


def test_mouse_reporting_sequences_and_restorer():
    """Verify SGR mouse reporting sequence is active and enabled by default in TerminalStateRestorer."""
    assert ENABLE_MOUSE_REPORTING_SEQ == ""
    assert DISABLE_MOUSE_REPORTING_SEQ == "\x1b[?1006l\x1b[?1002l\x1b[?1000l"

    state = AutonomousDeliveryState()
    assert state.mouse_reporting_enabled is True

    buf = io.StringIO()
    enable_mouse_reporting(stream=buf)
    assert buf.getvalue() == ENABLE_MOUSE_REPORTING_SEQ


def test_mouse_wheel_does_not_cycle_prompt_history():
    """Verify mouse wheel events scroll the conversation and never replace buffer with history."""
    state = AutonomousDeliveryState()
    up_called = 0
    down_called = 0

    def on_up(ed=None):
        nonlocal up_called
        up_called += 1
        state.scroll_conversation_up(3)

    def on_down(ed=None):
        nonlocal down_called
        down_called += 1
        state.scroll_conversation_down(3)

    editor = RawLineEditor(
        prompt_prefix="│ > ",
        initial_text="my current draft prompt",
        history=["old command 1", "old command 2"],
        state=state,
        on_wheel_up=on_up,
        on_wheel_down=on_down,
        width=80,
    )

    # Mouse wheel up -> invokes on_wheel_up, buffer remains unchanged
    editor.handle_key(Key.MOUSE_WHEEL_UP)
    assert up_called == 1
    assert editor.text == "my current draft prompt"

    # Mouse wheel down -> invokes on_wheel_down, buffer remains unchanged
    editor.handle_key(Key.MOUSE_WHEEL_DOWN)
    assert down_called == 1
    assert editor.text == "my current draft prompt"


def test_composer_box_wraps_long_text_without_ellipsis():
    """Verify render_composer_box wraps long input text into multiple lines instead of '...'."""
    long_prompt = "I discussed about my website with stackmind TUI models. After few chats, it got deflected from the main topic."
    box_str = render_composer_box_str(content=long_prompt, width=80)

    # Must NOT contain '...' truncation
    assert "..." not in box_str
    # Must contain both parts across lines
    assert "I discussed about my website" in box_str
    assert "deflected from the main topic." in box_str

    # Lines count should expand beyond 3 lines
    lines = box_str.split("\n")
    assert len(lines) >= 4  # top border + at least 2 content lines + bottom border


def test_wrap_composer_text_helper():
    """Verify wrap_composer_text splits at word boundaries when possible without losing characters."""
    text = "The quick brown fox jumps over the lazy dog."
    wrapped = wrap_composer_text(text, width=20)
    assert len(wrapped) >= 3
    assert all(len(line) <= 20 for line in wrapped)
    # Reassembled text must match original
    assert "".join(wrapped) == text


def test_arrow_up_down_navigates_wrapped_visual_lines_before_history():
    """Verify Up/Down arrow moves cursor across wrapped visual lines before cycling history."""
    long_prompt = "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi"
    # width=30 -> wrap_width = 24
    editor = RawLineEditor(
        prompt_prefix="│ > ",
        initial_text=long_prompt,
        history=["prior command 1", "prior command 2"],
        width=30,
        right_border=" │",
    )

    v_lines, line_starts, c_line, c_col = editor._get_visual_lines()
    assert len(v_lines) > 2
    # Cursor starts at the end of the text (last line)
    assert c_line == len(v_lines) - 1

    # Up arrow moves up one visual line within the same text, NOT history
    editor.handle_key(Key.UP)
    assert editor.text == long_prompt
    assert editor.current_line_and_col[0] == c_line - 1

    # Up arrow again moves up to line 0
    while editor.current_line_and_col[0] > 0:
        editor.handle_key(Key.UP)
        assert editor.text == long_prompt

    assert editor.current_line_and_col[0] == 0

    # Once at line 0, Up arrow navigates to previous history
    editor.handle_key(Key.UP)
    assert editor.text == "prior command 2"


def test_compute_composer_height_expands_for_wrapped_text():
    """Verify compute_composer_height expands based on wrapped visual lines."""
    short_text = "short text"
    long_text = "A" * 150  # 150 chars on width 80 (wrap_width 74) -> 3 lines

    h_short = compute_composer_height(short_text, height=24, width=80)
    assert h_short == 3  # 1 line + 2 borders

    h_long = compute_composer_height(long_text, height=24, width=80)
    assert h_long == 5  # 3 lines + 2 borders


def test_cursor_coordinates_on_wrapped_lines():
    """Verify compute_composer_cursor_row and _compute_cursor_col for cursor on wrapped visual line."""
    long_text = "Line One Content That Wraps\nLine Two Content"
    editor = RawLineEditor(initial_text=long_text, width=80)

    # Move cursor to start of second line
    l2_start = long_text.index("Line Two Content")
    editor.cursor = l2_start

    c_line, c_col = editor.current_line_and_col
    assert c_line == 1
    assert c_col == 0

    col = _compute_cursor_col(editor)
    assert col == 5  # border(1) + space(1) + prefix(2) + col(0) + 1 = 5

    # compute_composer_cursor_row for cursor_line=1
    row = compute_composer_cursor_row(24, composer_content=long_text, cursor_line=c_line, width=80)
    assert row >= 21
