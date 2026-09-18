"""Unit tests for TUI Keyboard Engine, Dynamic Composer State & Glyph Table.

Covers:
1. Low-level key decoding, buffer mutation, backspace, and history recall.
2. Real Ctrl+K (command palette) and Ctrl+L (screen clear) dispatching.
3. Cooperative Ctrl+C cancellation and Ctrl+D EOF handling.
4. Dynamic composer placeholder swapping (empty vs. populated state).
5. Glyph selection and automatic ASCII fallback for legacy consoles.
6. Terminal mode restoration in finally blocks.

Reference: PLAN_TUI_BUGFIX_ROUND2.md §3 & WO-052.
"""

from __future__ import annotations

import os
import sys
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from cli.tui.app import (
    render_composer_box,
    render_composer_box_str,
    render_composer_top_border,
    render_composer_top_border_str,
    render_top_header_bar_str,
)
from cli.tui.glyphs import (
    ASCII_GLYPHS,
    UNICODE_GLYPHS,
    disclosure_glyph,
    get_glyph,
    get_glyphs,
    is_ascii_mode,
    sanitize_text,
    set_glyph_mode,
    status_glyph,
    tree_branch,
)
from cli.tui.keyboard import (
    Key,
    RawLineEditor,
    TerminalStateRestorer,
    raw_prompt_input,
)


# ─── 1. GLYPHS AND FALLBACK TABLE TESTS ──────────────────────────────────────


def test_glyph_selection_unicode_mode():
    """Verify standard Unicode glyphs returned when ASCII mode is disabled."""
    set_glyph_mode(False)
    try:
        assert status_glyph("completed") == "✓"
        assert status_glyph("done") == "✓"
        assert status_glyph("running") == "●"
        assert status_glyph("active") == "●"
        assert status_glyph("failed") == "✗"
        assert status_glyph("error") == "✗"
        assert status_glyph("waiting") == "○"
        assert status_glyph("orchestrating") == "●"

        assert tree_branch(is_last=False) == "├─ "
        assert tree_branch(is_last=True) == "└─ "

        assert disclosure_glyph(expanded=False) == "▸"
        assert disclosure_glyph(expanded=True) == "▾"

        assert get_glyph("TOOL") == "⚒"
        assert get_glyph("SPARKLE") == "✦"
    finally:
        set_glyph_mode(None)


def test_glyph_selection_ascii_fallback_mode():
    """Verify deterministic ASCII fallback glyphs for legacy consoles."""
    set_glyph_mode(True)
    try:
        assert status_glyph("completed") == "[OK]"
        assert status_glyph("done") == "[OK]"
        assert status_glyph("running") == "*"
        assert status_glyph("active") == "*"
        assert status_glyph("failed") == "[FAIL]"
        assert status_glyph("error") == "[FAIL]"
        assert status_glyph("waiting") == "o"
        assert status_glyph("orchestrating") == "@"

        assert tree_branch(is_last=False) == "|- "
        assert tree_branch(is_last=True) == "`- "

        assert disclosure_glyph(expanded=False) == ">"
        assert disclosure_glyph(expanded=True) == "v"

        assert get_glyph("TOOL") == "[TOOL]"
        assert get_glyph("SPARKLE") == "*"
    finally:
        set_glyph_mode(None)


def test_is_ascii_mode_environment_variables(monkeypatch: pytest.MonkeyPatch):
    """Verify environment variables force ASCII mode deterministically."""
    set_glyph_mode(None)

    monkeypatch.setenv("STACKMIND_ASCII_GLYPHS", "1")
    assert is_ascii_mode() is True

    monkeypatch.setenv("STACKMIND_ASCII_GLYPHS", "0")
    monkeypatch.setenv("NO_UNICODE", "1")
    assert is_ascii_mode() is True

    monkeypatch.delenv("NO_UNICODE", raising=False)
    monkeypatch.setenv("STACKMIND_ASCII_GLYPHS", "false")
    monkeypatch.setenv("WT_SESSION", "test-guid-1234")
    assert is_ascii_mode() is False


def test_sanitize_text_utility():
    """Verify sanitize_text replaces Unicode characters with ASCII equivalents."""
    raw = "● running | ├─ subtask | ✓ completed | ⚒ build"
    assert sanitize_text(raw, fallback=False) == raw

    sanitized = sanitize_text(raw, fallback=True)
    assert "●" not in sanitized
    assert "✓" not in sanitized
    assert "⚒" not in sanitized
    assert "*" in sanitized
    assert "[OK]" in sanitized
    assert "[TOOL]" in sanitized


def test_top_header_bar_glyph_fallback():
    """Verify render_top_header_bar respects active glyph mode."""
    session = {"session_id": "b49209fd", "agent": "codex", "provider": "daemon"}

    set_glyph_mode(False)
    try:
        unicode_header = render_top_header_bar_str(session, width=90)
        assert "● online" in unicode_header
    finally:
        set_glyph_mode(None)

    set_glyph_mode(True)
    try:
        ascii_header = render_top_header_bar_str(session, width=90)
        assert "* online" in ascii_header
    finally:
        set_glyph_mode(None)


# ─── 2. DYNAMIC COMPOSER CHROME TESTS ────────────────────────────────────────


def test_composer_top_border_empty_state():
    """Verify empty composer displays 'Type a message...' placeholder."""
    top_empty = render_composer_top_border_str(width=80, has_content=False)
    assert "Type a message..." in top_empty
    assert "[Active Input]" not in top_empty
    assert "Ctrl+K commands | Ctrl+L clear" in top_empty
    assert len(top_empty) == 80


def test_composer_top_border_populated_state():
    """Verify composer with content dynamically swaps placeholder to '[Active Input]'."""
    top_active = render_composer_top_border_str(width=80, has_content=True)
    assert "[Active Input]" in top_active
    assert "Type a message..." not in top_active
    assert "Ctrl+K commands | Ctrl+L clear" in top_active
    assert len(top_active) == 80


def test_composer_box_empty_vs_populated():
    """Verify render_composer_box hides placeholder when content is supplied."""
    empty_box = render_composer_box_str(width=80)
    assert "Type a message..." in empty_box

    populated_box = render_composer_box_str(width=80, content="git status")
    assert "git status" in populated_box
    assert "Type a message..." not in populated_box


def test_composer_top_border_empty_placeholder():
    """Verify top border renders cleanly when placeholder is empty."""
    border = render_composer_top_border_str(placeholder="", width=80)
    assert "Ctrl+K commands | Ctrl+L clear" in border
    assert len(border) == 80


# ─── 3. RAW LINE EDITOR MUTATION & BUFFER TESTS ─────────────────────────────


def test_editor_insert_and_enter():
    """Verify printable characters insert sequentially and Enter finishes."""
    editor = RawLineEditor()
    for ch in "hello":
        done = editor.handle_key(ch)
        assert done is False

    assert editor.text == "hello"
    assert editor.cursor == 5

    done = editor.handle_key(Key.ENTER)
    assert done is True


def test_editor_backspace_and_delete():
    """Verify backspace removes previous character and delete removes next character."""
    editor = RawLineEditor(initial_text="abcde")
    assert editor.cursor == 5

    # Backspace 'e'
    editor.handle_key(Key.BACKSPACE)
    assert editor.text == "abcd"
    assert editor.cursor == 4

    # Move left to between 'b' and 'c' (cursor index 2)
    editor.handle_key(Key.LEFT)
    editor.handle_key(Key.LEFT)
    assert editor.cursor == 2

    # Delete 'c'
    editor.handle_key(Key.DELETE)
    assert editor.text == "abd"
    assert editor.cursor == 2


def test_editor_home_end_navigation():
    """Verify Home and End move cursor to line boundaries."""
    editor = RawLineEditor(initial_text="stackmind")
    assert editor.cursor == 9

    editor.handle_key(Key.HOME)
    assert editor.cursor == 0

    editor.handle_key("X")
    assert editor.text == "Xstackmind"
    assert editor.cursor == 1

    editor.handle_key(Key.END)
    assert editor.cursor == 10
    editor.handle_key("Y")
    assert editor.text == "XstackmindY"


def test_editor_history_navigation():
    """Verify Arrow Up/Down recalls previous entries."""
    history = ["cmd-one", "cmd-two", "cmd-three"]
    editor = RawLineEditor(history=history)

    # Press UP: should recall last history item
    editor.handle_key(Key.UP)
    assert editor.text == "cmd-three"

    # Press UP: recalls previous
    editor.handle_key(Key.UP)
    assert editor.text == "cmd-two"

    # Press UP: recalls oldest
    editor.handle_key(Key.UP)
    assert editor.text == "cmd-one"

    # Press DOWN: advances toward newest
    editor.handle_key(Key.DOWN)
    assert editor.text == "cmd-two"

    editor.handle_key(Key.DOWN)
    assert editor.text == "cmd-three"

    # Press DOWN past newest restores initial input (empty)
    editor.handle_key(Key.DOWN)
    assert editor.text == ""


def test_editor_ctrl_c_and_ctrl_d():
    """Verify Ctrl+C raises KeyboardInterrupt and Ctrl+D raises EOFError when empty."""
    editor = RawLineEditor()

    with pytest.raises(KeyboardInterrupt):
        editor.handle_key(Key.CTRL_C)

    with pytest.raises(EOFError):
        editor.handle_key(Key.CTRL_D)

    # Non-empty buffer: Ctrl+D acts like Delete forward
    editor = RawLineEditor(initial_text="test")
    editor.handle_key(Key.HOME)
    editor.handle_key(Key.CTRL_D)
    assert editor.text == "est"


# ─── 4. CTRL+K AND CTRL+L DISPATCHING TESTS ─────────────────────────────────


def test_ctrl_k_command_palette_callback():
    """Verify pressing Ctrl+K executes on_ctrl_k callback without submitting line."""
    k_mock = MagicMock()
    editor = RawLineEditor(on_ctrl_k=k_mock)

    done = editor.handle_key(Key.CTRL_K)
    assert done is False
    k_mock.assert_called_once()


def test_ctrl_l_screen_clear_callback():
    """Verify pressing Ctrl+L executes on_ctrl_l callback without submitting line."""
    l_mock = MagicMock()
    editor = RawLineEditor(on_ctrl_l=l_mock)

    done = editor.handle_key(Key.CTRL_L)
    assert done is False
    l_mock.assert_called_once()


def test_editor_transition_callback():
    """Verify on_transition callback is notified when buffer goes from empty to non-empty."""
    transition_mock = MagicMock()
    editor = RawLineEditor(on_transition=transition_mock)

    # First character typed -> transition to True
    editor.handle_key("a")
    transition_mock.assert_called_with(True)

    # Second character typed -> no transition
    transition_mock.reset_mock()
    editor.handle_key("b")
    transition_mock.assert_not_called()

    # Backspace 'b' -> still has 'a', no transition
    editor.handle_key(Key.BACKSPACE)
    transition_mock.assert_not_called()

    # Backspace 'a' -> buffer emptied, transition to False
    editor.handle_key(Key.BACKSPACE)
    transition_mock.assert_called_with(False)


# ─── 5. RAW PROMPT INPUT END-TO-END SIMULATION ──────────────────────────────


def test_raw_prompt_input_key_stream_simulation():
    """Verify raw_prompt_input executes a stream of mock keystrokes and returns submitted text."""
    keys: Iterator[str] = iter(["s", "t", "a", "t", "u", "s", Key.ENTER])
    result = raw_prompt_input(width=80, key_stream=keys)
    assert result == "status"


def test_raw_prompt_input_with_backspace_and_unicode():
    """Verify raw_prompt_input handles editing and Unicode in simulated session."""
    keys: Iterator[str] = iter(["h", "e", "l", "p", Key.BACKSPACE, "l", "o", " ", "✦", Key.ENTER])
    result = raw_prompt_input(width=80, key_stream=keys)
    assert result == "hello ✦"


def test_raw_prompt_input_invokes_ctrl_k_and_ctrl_l():
    """Verify Ctrl+K and Ctrl+L can be invoked during a prompt session."""
    k_called = False
    l_called = False

    def on_k():
        nonlocal k_called
        k_called = True

    def on_l():
        nonlocal l_called
        l_called = True

    keys: Iterator[str] = iter([Key.CTRL_K, "o", "k", Key.CTRL_L, Key.ENTER])
    result = raw_prompt_input(width=80, key_stream=keys, on_ctrl_k=on_k, on_ctrl_l=on_l)
    assert result == "ok"
    assert k_called is True
    assert l_called is True


# ─── 6. TERMINAL STATE RESTORATION ──────────────────────────────────────────


def test_terminal_state_restorer_clean_exit():
    """Verify TerminalStateRestorer exits safely without crashing."""
    with TerminalStateRestorer():
        pass
