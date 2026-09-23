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

import io
import os
import sys
from typing import Iterator
from unittest.mock import MagicMock, patch

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


# ─── 7. PAGE UP / PAGE DOWN CONVERSATION SCROLL BINDINGS ────────────────────


def test_page_up_page_down_callbacks_in_editor():
    """Verify pressing Page Up/Down executes on_page_up/on_page_down callbacks without submitting."""
    up_mock = MagicMock()
    down_mock = MagicMock()
    editor = RawLineEditor(on_page_up=up_mock, on_page_down=down_mock)

    done_up = editor.handle_key(Key.PAGE_UP)
    assert done_up is False
    up_mock.assert_called_once()

    done_down = editor.handle_key(Key.PAGE_DOWN)
    assert done_down is False
    down_mock.assert_called_once()


def test_raw_prompt_input_invokes_page_up_and_page_down():
    """Verify Page Up and Page Down can be invoked during a prompt session."""
    up_called = 0
    down_called = 0

    def on_up():
        nonlocal up_called
        up_called += 1

    def on_down():
        nonlocal down_called
        down_called += 1

    keys: Iterator[str] = iter([Key.PAGE_UP, "h", "i", Key.PAGE_DOWN, Key.ENTER])
    result = raw_prompt_input(
        width=80,
        key_stream=keys,
        on_page_up=on_up,
        on_page_down=on_down,
    )
    assert result == "hi"
    assert up_called == 1
    assert down_called == 1


def test_keyboard_decodes_page_up_and_page_down():
    """Verify raw key decoding for Page Up and Page Down on Windows and POSIX."""
    from cli.tui.keyboard import _read_raw_key_posix, _read_raw_key_windows
    from unittest.mock import patch

    # Windows: \xe0 followed by 'I' (Page Up) and 'Q' (Page Down)
    with patch("msvcrt.getwch", side_effect=["\xe0", "I"]):
        assert _read_raw_key_windows() == Key.PAGE_UP

    with patch("msvcrt.getwch", side_effect=["\xe0", "Q"]):
        assert _read_raw_key_windows() == Key.PAGE_DOWN

    # POSIX: \x1b followed by '[5~' (Page Up) and '[6~' (Page Down)
    with patch("os.read", side_effect=[b"\x1b", b"[5~"]):
        with patch("select.select", return_value=([0], [], [])):
            assert _read_raw_key_posix(fd=0) == Key.PAGE_UP

    with patch("os.read", side_effect=[b"\x1b", b"[6~"]):
        with patch("select.select", return_value=([0], [], [])):
            assert _read_raw_key_posix(fd=0) == Key.PAGE_DOWN


def test_debugkeys_logging_mode(tmp_path, monkeypatch):
    """Verify set_debug_keys enables logging to file without affecting editor input."""
    import cli.tui.keyboard as kb

    log_file = tmp_path / "test_keys.log"
    monkeypatch.setattr(kb, "DEBUG_LOG_FILE", str(log_file))
    kb.set_debug_keys(True)

    try:
        keys: Iterator[str] = iter(["a", Key.PAGE_UP, Key.ENTER])
        res = kb.raw_prompt_input(width=80, key_stream=keys)
        assert res == "a"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "DEBUG KEYS LOGGING ENABLED" in content
        assert "PAGE_UP" in content
    finally:
        kb.set_debug_keys(False)


# ─── 8. MOUSE WHEEL SCROLL & SGR ESCAPE SEQUENCE TESTS ──────────────────────


def test_sgr_and_x10_mouse_wheel_parsing():
    """Verify SGR (<64 / <65) and X10 mouse sequences parse into MOUSE_WHEEL_UP/DOWN."""
    from cli.tui.keyboard import _parse_escape_sequence

    # SGR format: [<Cb;Cx;CyM (press) or [<Cb;Cx;Cym (release)
    # Button 64 = Wheel Up, 65 = Wheel Down
    assert _parse_escape_sequence("[<64;15;25M") == Key.MOUSE_WHEEL_UP
    assert _parse_escape_sequence("[<64;15;25m") == Key.MOUSE_WHEEL_UP
    assert _parse_escape_sequence("[<65;15;25M") == Key.MOUSE_WHEEL_DOWN
    assert _parse_escape_sequence("[<65;15;25m") == Key.MOUSE_WHEEL_DOWN

    # Non-wheel mouse clicks must NOT parse as wheel keys
    click_res = _parse_escape_sequence("[<0;15;25M")
    assert click_res == "<MOUSE_0_15_25_M>"
    assert click_res.startswith("<MOUSE_")

    # Legacy X10 format: [M<btn+32><col+32><row+32>
    x10_up = f"[M{chr(64 + 32)}{chr(10 + 32)}{chr(20 + 32)}"
    assert _parse_escape_sequence(x10_up) == Key.MOUSE_WHEEL_UP

    x10_down = f"[M{chr(65 + 32)}{chr(10 + 32)}{chr(20 + 32)}"
    assert _parse_escape_sequence(x10_down) == Key.MOUSE_WHEEL_DOWN

    x10_click = f"[M{chr(0 + 32)}{chr(10 + 32)}{chr(20 + 32)}"
    assert _parse_escape_sequence(x10_click) == "<MOUSE_X10_0_10_20>"


def test_mouse_wheel_callbacks_in_editor():
    """Verify mouse wheel events invoke on_wheel_up and on_wheel_down callbacks without submitting."""
    up_mock = MagicMock()
    down_mock = MagicMock()
    editor = RawLineEditor(on_wheel_up=up_mock, on_wheel_down=down_mock)

    done_up = editor.handle_key(Key.MOUSE_WHEEL_UP)
    assert done_up is False
    up_mock.assert_called_once()

    done_down = editor.handle_key(Key.MOUSE_WHEEL_DOWN)
    assert done_down is False
    down_mock.assert_called_once()


def test_mouse_wheel_fallback_to_page_callbacks():
    """Verify mouse wheel falls back to on_page_up/on_page_down if on_wheel_* not provided."""
    up_mock = MagicMock()
    down_mock = MagicMock()
    editor = RawLineEditor(on_page_up=up_mock, on_page_down=down_mock)

    editor.handle_key(Key.MOUSE_WHEEL_UP)
    up_mock.assert_called_once()

    editor.handle_key(Key.MOUSE_WHEEL_DOWN)
    down_mock.assert_called_once()


def test_non_wheel_mouse_events_filtered_from_buffer():
    """Verify mouse clicks and motion (<MOUSE_...) never leak characters into composer buffer."""
    editor = RawLineEditor()
    editor.insert_char("a")

    # Normal clicks and drag events must be silently swallowed
    assert editor.handle_key("<MOUSE_0_10_20_M>") is False
    assert editor.handle_key("<MOUSE_0_10_20_m>") is False
    assert editor.handle_key("<MOUSE_X10_0_10_20>") is False

    # Buffer must remain strictly "a"
    assert editor.text == "a"


def test_raw_prompt_input_mouse_wheel():
    """Verify raw_prompt_input forwards mouse wheel events to callbacks."""
    wheel_up_count = 0
    wheel_down_count = 0

    def on_wup():
        nonlocal wheel_up_count
        wheel_up_count += 1

    def on_wdown():
        nonlocal wheel_down_count
        wheel_down_count += 1

    keys: Iterator[str] = iter([Key.MOUSE_WHEEL_UP, "g", "o", Key.MOUSE_WHEEL_DOWN, Key.ENTER])
    result = raw_prompt_input(
        width=80,
        key_stream=keys,
        on_wheel_up=on_wup,
        on_wheel_down=on_wdown,
    )
    assert result == "go"
    assert wheel_up_count == 1
    assert wheel_down_count == 1


def test_terminal_state_restorer_disables_mouse():
    """Verify TerminalStateRestorer exits with mouse reporting disabled."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        with TerminalStateRestorer():
            pass
    assert "\x1b[?1006l\x1b[?1000l" in buf.getvalue()


# ─── 9. WIN32 DIRECT INPUT RECORD READER TESTS (PHASE 1) ────────────────────


def _make_key_record(char: str = "\x00", vk: int = 0, down: bool = True, repeat: int = 1):
    from cli.tui.keyboard import INPUT_RECORD
    rec = INPUT_RECORD()
    rec.EventType = 0x0001
    rec.Event.KeyEvent.bKeyDown = 1 if down else 0
    rec.Event.KeyEvent.wRepeatCount = repeat
    rec.Event.KeyEvent.wVirtualKeyCode = vk
    rec.Event.KeyEvent.wVirtualScanCode = 0
    rec.Event.KeyEvent.UnicodeChar = char
    rec.Event.KeyEvent.dwControlKeyState = 0
    return rec


def _make_mouse_record(event_flags: int = 0, button_state: int = 0, x: int = 0, y: int = 0):
    from cli.tui.keyboard import INPUT_RECORD
    rec = INPUT_RECORD()
    rec.EventType = 0x0002
    rec.Event.MouseEvent.dwMousePosition.X = x
    rec.Event.MouseEvent.dwMousePosition.Y = y
    rec.Event.MouseEvent.dwButtonState = button_state
    rec.Event.MouseEvent.dwControlKeyState = 0
    rec.Event.MouseEvent.dwEventFlags = event_flags
    return rec


def test_win32_reader_mouse_wheel():
    """Verify Win32ConsoleReader parses MOUSE_WHEELED records into MOUSE_WHEEL_UP/DOWN."""
    from cli.tui.keyboard import Key, Win32ConsoleReader

    reader = Win32ConsoleReader(handle=1)

    # Mouse Wheel Up: dwEventFlags = 0x0004 (MOUSE_WHEELED), high word of dwButtonState = +120 (0x00780000)
    wheel_up_rec = _make_mouse_record(event_flags=0x0004, button_state=0x00780000)
    assert reader.parse_input_record(wheel_up_rec) == Key.MOUSE_WHEEL_UP

    # Mouse Wheel Down: dwEventFlags = 0x0004 (MOUSE_WHEELED), high word of dwButtonState = -120 (0xFF880000)
    wheel_down_rec = _make_mouse_record(event_flags=0x0004, button_state=0xFF880000)
    assert reader.parse_input_record(wheel_down_rec) == Key.MOUSE_WHEEL_DOWN

    # Non-wheel click event (left click = 1) -> must NOT parse as wheel key
    click_rec = _make_mouse_record(event_flags=0x0000, button_state=0x0001, x=10, y=5)
    parsed_click = reader.parse_input_record(click_rec)
    assert parsed_click == "<MOUSE_1_10_5>"
    assert parsed_click.startswith("<MOUSE_")


def test_win32_reader_keyboard_events():
    """Verify Win32ConsoleReader parses standard key down, control keys, and ignores key up."""
    from cli.tui.keyboard import Key, Win32ConsoleReader

    reader = Win32ConsoleReader(handle=1)

    # Standard keydown
    assert reader.parse_input_record(_make_key_record(char="a", vk=0x41, down=True)) == "a"

    # Key up (bKeyDown=False) must be ignored (returns None)
    assert reader.parse_input_record(_make_key_record(char="a", vk=0x41, down=False)) is None

    # Control keys
    assert reader.parse_input_record(_make_key_record(char=Key.CTRL_C, vk=0x43, down=True)) == Key.CTRL_C
    assert reader.parse_input_record(_make_key_record(char=Key.CTRL_K, vk=0x4B, down=True)) == Key.CTRL_K
    assert reader.parse_input_record(_make_key_record(char=Key.CTRL_L, vk=0x4C, down=True)) == Key.CTRL_L
    assert reader.parse_input_record(_make_key_record(char="\r", vk=0x0D, down=True)) == "\r"
    assert reader.parse_input_record(_make_key_record(char="\x08", vk=0x08, down=True)) == "\x08"


def test_win32_reader_virtual_keys():
    """Verify Win32ConsoleReader maps virtual key codes when UnicodeChar is null."""
    from cli.tui.keyboard import Key, Win32ConsoleReader

    reader = Win32ConsoleReader(handle=1)

    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x21)) == Key.PAGE_UP
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x22)) == Key.PAGE_DOWN
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x26)) == Key.UP
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x28)) == Key.DOWN
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x25)) == Key.LEFT
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x27)) == Key.RIGHT
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x24)) == Key.HOME
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x23)) == Key.END
    assert reader.parse_input_record(_make_key_record(char="\x00", vk=0x2E)) == Key.DELETE


def test_win32_reader_unicode_surrogate_pairs():
    """Verify UTF-16 surrogate pairs split across two separate KEY_EVENT records combine into a single character."""
    from cli.tui.keyboard import Win32ConsoleReader

    reader = Win32ConsoleReader(handle=1)

    # Crab emoji: U+1F980 -> UTF-16 surrogate pair 0xD83E (high), 0xDD80 (low)
    high_rec = _make_key_record(char="\ud83e")
    low_rec = _make_key_record(char="\udd80")

    # Record 1 (high surrogate) must buffer and return None
    res1 = reader.parse_input_record(high_rec)
    assert res1 is None
    assert reader._surrogate_high == "\ud83e"

    # Record 2 (low surrogate) must combine with buffered high surrogate to yield the single full codepoint
    res2 = reader.parse_input_record(low_rec)
    assert res2 == "\U0001f980"
    assert ord(res2) == 0x1F980
    assert len(res2) == 1
    assert reader._surrogate_high is None

    # Incomplete surrogate followed by regular key must recover cleanly
    reader.parse_input_record(_make_key_record(char="\ud83e"))
    assert reader._surrogate_high == "\ud83e"
    regular = reader.parse_input_record(_make_key_record(char="b"))
    assert regular == "b"
    assert reader._surrogate_high is None


def test_win32_reader_repeat_count():
    """Verify wRepeatCount > 1 buffers repeated characters in pending queue."""
    from cli.tui.keyboard import Win32ConsoleReader

    reader = Win32ConsoleReader(handle=1)
    repeat_rec = _make_key_record(char="k", repeat=3)

    first = reader.parse_input_record(repeat_rec)
    assert first == "k"
    assert len(reader._pending_keys) == 2
    assert reader.read_key() == "k"
    assert reader.read_key() == "k"
    assert len(reader._pending_keys) == 0


def test_win32_reader_escape_sequences():
    """Verify Win32ConsoleReader buffers and decodes escape sequences from KEY_EVENT records."""
    import ctypes
    from cli.tui.keyboard import Key, Win32ConsoleReader, _parse_escape_sequence

    reader = Win32ConsoleReader(handle=1)

    def make_mocks(chars):
        char_list = list(chars)
        def _mock_events(h, cnt):
            cnt._obj.value = len(char_list)
            return 1

        def _mock_read(h, rec_ptr, length, count_ptr):
            if not char_list:
                count_ptr._obj.value = 0
                return 1
            c = char_list.pop(0)
            rec = rec_ptr._obj
            rec.EventType = 0x0001
            rec.Event.KeyEvent.bKeyDown = 1
            rec.Event.KeyEvent.UnicodeChar = c
            count_ptr._obj.value = 1
            return 1
        return _mock_events, _mock_read

    # 1. SGR Mouse Wheel Up: \x1b followed by [<64;10;20M
    mock_events, mock_read = make_mocks("[<64;10;20M")
    with patch("ctypes.WinDLL") as mock_dll:
        instance = mock_dll.return_value
        instance.GetNumberOfConsoleInputEvents.side_effect = mock_events
        instance.ReadConsoleInputW.side_effect = mock_read
        seq = reader._read_escape_sequence(h=1)
        assert seq == "[<64;10;20M"
        assert _parse_escape_sequence(seq) == Key.MOUSE_WHEEL_UP

    # 2. SGR Mouse Wheel Down: \x1b followed by [<65;10;20M
    mock_events, mock_read = make_mocks("[<65;10;20M")
    with patch("ctypes.WinDLL") as mock_dll:
        instance = mock_dll.return_value
        instance.GetNumberOfConsoleInputEvents.side_effect = mock_events
        instance.ReadConsoleInputW.side_effect = mock_read
        seq = reader._read_escape_sequence(h=1)
        assert seq == "[<65;10;20M"
        assert _parse_escape_sequence(seq) == Key.MOUSE_WHEEL_DOWN

    # 3. Arrow Up: \x1b followed by [A
    mock_events, mock_read = make_mocks("[A")
    with patch("ctypes.WinDLL") as mock_dll:
        instance = mock_dll.return_value
        instance.GetNumberOfConsoleInputEvents.side_effect = mock_events
        instance.ReadConsoleInputW.side_effect = mock_read
        seq = reader._read_escape_sequence(h=1)
        assert seq == "[A"
        assert _parse_escape_sequence(seq) == Key.UP

    # 4. Page Up: \x1b followed by [5~
    mock_events, mock_read = make_mocks("[5~")
    with patch("ctypes.WinDLL") as mock_dll:
        instance = mock_dll.return_value
        instance.GetNumberOfConsoleInputEvents.side_effect = mock_events
        instance.ReadConsoleInputW.side_effect = mock_read
        seq = reader._read_escape_sequence(h=1)
        assert seq == "[5~"
        assert _parse_escape_sequence(seq) == Key.PAGE_UP


def test_win32_reader_default_and_opt_in_flag():
    """Verify read_next_key defaults to legacy reader on NT, opting in to Win32 if STACKMIND_WIN32_INPUT=1."""
    import os
    from unittest.mock import patch
    from cli.tui.keyboard import read_next_key

    with patch("os.name", "nt"):
        # Default: calls _read_raw_key_windows
        with patch.dict(os.environ, {}, clear=True), \
             patch("cli.tui.keyboard._read_raw_key_windows_v2", return_value="v2_key") as mock_v2, \
             patch("cli.tui.keyboard._read_raw_key_windows", return_value="legacy_key") as mock_legacy:
            assert read_next_key() == "legacy_key"
            mock_legacy.assert_called_once()
            mock_v2.assert_not_called()

        # Opt-in: STACKMIND_WIN32_INPUT=1 calls _read_raw_key_windows_v2
        with patch.dict(os.environ, {"STACKMIND_WIN32_INPUT": "1"}), \
             patch("cli.tui.keyboard._read_raw_key_windows_v2", return_value="v2_key") as mock_v2, \
             patch("cli.tui.keyboard._read_raw_key_windows", return_value="legacy_key") as mock_legacy:
            assert read_next_key() == "v2_key"
            mock_v2.assert_called_once()
            mock_legacy.assert_not_called()




