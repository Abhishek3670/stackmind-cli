"""Tests for Multiline Paste and Flexible Composer Height.

Verifies:
1. Multiline paste with newlines does not prematurely submit on the first newline.
2. Bracketed paste mode captures all lines and newlines into the buffer.
3. Fallback burst paste captures newlines without submitting.
4. Shift+Enter inserts newlines without submitting.
5. Flexible composer height expands from 3 lines up to ~60% of terminal height.
6. Total full-screen frame height remains strictly equal to terminal height (no screen overflow).
7. When text lines exceed ~60% limit, composer height is capped and displays the
   `[XX lines written]` indicator in the top border and box title.
8. Arrow Up/Down navigates lines within multiline buffer before moving prompt history.
9. Interactive submit via Enter submits the entire multiline text buffer.
"""

from __future__ import annotations

import io
import os
from unittest.mock import MagicMock

import pytest

from cli.tui.keyboard import Key, RawLineEditor, raw_prompt_input
from cli.tui.layout import (
    COMPOSER_BOX_HEIGHT,
    compute_composer_cursor_row,
    compute_composer_height,
    compute_viewport_height,
    get_composer_line_limits,
    render_full_screen_workspace,
)
from cli.tui.terminal import (
    enter_alternate_screen,
    exit_alternate_screen,
    prompt_composer_input,
    render_composer_box,
    render_composer_box_str,
    render_composer_top_border,
    render_composer_top_border_str,
    restore_terminal_state,
)


# ─── 1. MULTILINE PASTE & NEWLINE CAPTURE ─────────────────────────────────────


def test_bracketed_paste_multiline_does_not_prematurely_submit():
    """Verify that pasting multiline text inside bracketed paste retains all lines."""
    keys = iter([
        Key.PASTE_START,
        "line 1",
        Key.ENTER,
        "line 2",
        Key.NEWLINE,
        "line 3",
        Key.PASTE_END,
        Key.ENTER,  # actual submission
    ])
    result = raw_prompt_input(key_stream=keys)
    assert result == "line 1\nline 2\nline 3"


def test_bracketed_paste_with_crlf():
    """Verify Windows CRLF (\r\n) in bracketed paste does not insert duplicate newlines."""
    keys = iter([
        Key.PASTE_START,
        "def foo():",
        "\r",
        "\n",
        "    return 42",
        Key.PASTE_END,
        Key.ENTER,
    ])
    result = raw_prompt_input(key_stream=keys)
    assert result == "def foo():\n    return 42"


def test_shift_enter_inserts_newline_without_submitting():
    """Verify Shift+Enter inserts a newline character rather than submitting."""
    keys = iter([
        "first",
        Key.SHIFT_ENTER,
        "second",
        Key.ENTER,
    ])
    result = raw_prompt_input(key_stream=keys)
    assert result == "first\nsecond"


def test_burst_paste_fallback_with_pending_input(monkeypatch):
    """Verify that when unread input is pending, Enter is treated as newline insertion."""
    pending_state = [True, True, False]

    def mock_has_pending():
        if pending_state:
            return pending_state.pop(0)
        return False

    monkeypatch.setattr("cli.tui.keyboard.has_pending_input", mock_has_pending)

    keys = iter([
        "alpha",
        Key.ENTER,  # pending -> newline
        "beta",
        Key.ENTER,  # pending -> newline
        "gamma",
        Key.ENTER,  # not pending -> submit
    ])
    result = raw_prompt_input(key_stream=keys)
    assert result == "alpha\nbeta\ngamma"


def test_multicharacter_paste_chunk_with_newlines():
    """Verify multi-character chunks with embedded newlines are parsed correctly."""
    keys = iter([
        "line one\nline two\nline three",
        Key.ENTER,
    ])
    result = raw_prompt_input(key_stream=keys)
    assert result == "line one\nline two\nline three"


# ─── 2. FLEXIBLE COMPOSER HEIGHT & 60% SCREEN LIMIT ──────────────────────────


def test_composer_line_limits_calculation():
    """Verify 60% terminal height limits calculation."""
    # Height 24: 60% = 14 lines max box, 12 input lines
    max_box, max_input = get_composer_line_limits(24)
    assert max_box == 14
    assert max_input == 12

    # Height 50: 60% = 30 lines max box, 28 input lines
    max_box_50, max_input_50 = get_composer_line_limits(50)
    assert max_box_50 == 30
    assert max_input_50 == 28

    # Small terminal (height 10): 60% = 6 lines max box, 4 input lines
    max_box_10, max_input_10 = get_composer_line_limits(10)
    assert max_box_10 == 6
    assert max_input_10 == 4


def test_dynamic_composer_height_expansion():
    """Verify composer box height expands with content lines up to ~60% limit."""
    # 1 line -> standard 3 lines
    assert compute_composer_height("single line", height=24) == 3

    # 3 lines -> 5 lines box (3 input + 2 borders)
    assert compute_composer_height("line 1\nline 2\nline 3", height=24) == 5

    # 10 lines -> 12 lines box
    text_10 = "\n".join(f"line {i}" for i in range(10))
    assert compute_composer_height(text_10, height=24) == 12

    # 12 lines -> exactly 14 lines (60% cap for height 24)
    text_12 = "\n".join(f"line {i}" for i in range(12))
    assert compute_composer_height(text_12, height=24) == 14

    # 20 lines -> capped at 14 lines (60% of height 24)
    text_20 = "\n".join(f"line {i}" for i in range(20))
    assert compute_composer_height(text_20, height=24) == 14


def test_viewport_height_adjusts_proportionally_to_composer():
    """Verify conversation viewport height shrinks proportionally as composer grows."""
    # Height 24:
    # 1 line composer (3 rows) + 1 status bar = 4 chrome -> viewport = 20
    assert compute_viewport_height(24, composer_content="hello") == 20

    # 3 lines composer (5 rows) + 1 status bar = 6 chrome -> viewport = 18
    assert compute_viewport_height(24, composer_content="1\n2\n3") == 18

    # 12 lines composer (14 rows) + 1 status bar = 15 chrome -> viewport = 9
    text_12 = "\n".join(f"{i}" for i in range(12))
    assert compute_viewport_height(24, composer_content=text_12) == 9

    # 25 lines composer (capped at 14 rows) + 1 status bar = 15 chrome -> viewport = 9
    text_25 = "\n".join(f"{i}" for i in range(25))
    assert compute_viewport_height(24, composer_content=text_25) == 9


def test_full_screen_frame_height_remains_strictly_constant():
    """Verify that expanding composer does NOT cause terminal height overflow."""
    session = {"session_id": "sess-test", "agent": "architect"}
    state = None

    for num_lines in (1, 2, 5, 10, 15, 25):
        content = "\n".join(f"Line {i}: test text" for i in range(num_lines))
        frame = render_full_screen_workspace(
            session=session,
            state=state,
            width=100,
            height=24,
            composer_content=content,
        )
        lines = frame.split("\n")
        assert len(lines) == 24, f"Frame height deviated for {num_lines} lines: {len(lines)}"


# ─── 3. INDICATOR WHEN EXCEEDING 60% SCREEN HEIGHT ───────────────────────────


def test_indicator_shown_when_exceeding_60_percent_in_top_border():
    """Verify top border displays '[X lines written]' when content exceeds 60%."""
    # Height 24 -> max 12 input lines
    # Below 60%: normal label
    text_5 = "\n".join(f"line {i}" for i in range(5))
    border_5 = render_composer_top_border_str(content=text_5, height=24, width=80)
    assert "lines written" not in border_5

    # Above 60%: 15 lines -> shows [15 lines written]
    text_15 = "\n".join(f"line {i}" for i in range(15))
    border_15 = render_composer_top_border_str(content=text_15, height=24, width=80)
    assert "[15 lines written]" in border_15


def test_indicator_shown_when_exceeding_60_percent_in_composer_box():
    """Verify composer box title displays '[X lines written]' and clamps visible lines."""
    text_18 = "\n".join(f"item {i}" for i in range(18))
    box_str = render_composer_box_str(content=text_18, height=24, width=80)

    # Box title includes lines written count
    assert "18 lines written" in box_str

    # Box height is capped at 14 lines (60% of 24)
    box_lines = box_str.split("\n")
    assert len(box_lines) == 14

    # Visible lines show the tail (e.g. item 17 is visible, item 0 is scrolled off)
    assert "item 17" in box_str


# ─── 4. MULTILINE ARROW NAVIGATION ───────────────────────────────────────────


def test_multiline_arrow_up_down_navigation():
    """Verify Arrow Up/Down moves cursor between lines, only triggering history at ends."""
    editor = RawLineEditor(initial_text="line A\nline B\nline C", history=["prior command"])

    # Cursor starts at the end of "line C" (index 20, line index 2)
    l_idx, col = editor.current_line_and_col
    assert l_idx == 2
    assert col == 6

    # Arrow UP -> moves to "line B"
    editor.handle_key(Key.UP)
    l_idx, col = editor.current_line_and_col
    assert l_idx == 1
    assert col == 6

    # Arrow UP -> moves to "line A"
    editor.handle_key(Key.UP)
    l_idx, col = editor.current_line_and_col
    assert l_idx == 0
    assert col == 6

    # Arrow UP from line 0 -> triggers history_prev ("prior command")
    editor.handle_key(Key.UP)
    assert editor.text == "prior command"

    # Arrow DOWN -> restores temp buffer
    editor.handle_key(Key.DOWN)
    assert editor.text == "line A\nline B\nline C"


def test_multiline_cursor_row_calculation():
    """Verify compute_composer_cursor_row calculates exact row for multiline cursor."""
    # Height 24, show_footer=True -> bottom status bar at row 24, bottom border at 23.
    # 3 lines composer (comp_h = 5):
    # input rows are at 21 (line 0), 22 (line 1), 23 is bottom border?
    # Wait, comp_h = 5:
    # input_lines = 3. rows are 20 (line 0), 21 (line 1), 22 (line 2). Bottom border at 23.
    content = "a\nb\nc"
    row_0 = compute_composer_cursor_row(24, composer_content=content, cursor_line=0)
    row_1 = compute_composer_cursor_row(24, composer_content=content, cursor_line=1)
    row_2 = compute_composer_cursor_row(24, composer_content=content, cursor_line=2)

    assert row_0 == 20
    assert row_1 == 21
    assert row_2 == 22


# ─── 5. BRACKETED PASTE SEQUENCES IN ALTERNATE SCREEN ────────────────────────


def test_alternate_screen_bracketed_paste_lifecycle():
    """Verify enter/exit alternate screen enables and disables bracketed paste."""
    enter_buf = io.StringIO()
    enter_alternate_screen(stream=enter_buf)
    assert "\x1b[?2004h" in enter_buf.getvalue()

    exit_buf = io.StringIO()
    exit_alternate_screen(stream=exit_buf)
    assert "\x1b[?2004l" in exit_buf.getvalue()

    restore_buf = io.StringIO()
    restore_terminal_state(stream=restore_buf)
    assert "\x1b[?2004l" in restore_buf.getvalue()
