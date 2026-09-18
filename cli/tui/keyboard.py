"""Native raw keyboard line editor and shortcut engine for StackMind TUI.

Provides low-level interactive input handling across platforms:
- Windows (os.name == 'nt'): msvcrt.getwch() and msvcrt.kbhit()
- POSIX: termios.tcgetattr, tty.setraw, select.select
- Non-interactive / CI / pipes: clean fallback to readline / input()

Features:
- Real handlers for Ctrl+K (command palette) and Ctrl+L (screen clear / redraw)
- Backspace, Delete, Arrow Left/Right, Home, End
- Arrow Up/Down command history navigation
- Clean Ctrl+C cooperative cancellation and Ctrl+D EOF handling
- Dynamic composer chrome placeholder updates
- Unconditional terminal state restoration in finally blocks

Reference: PLAN_TUI_BUGFIX_ROUND2.md §3.1 & TUI_BUGFIX_2.md §2.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Iterator, Optional

# Persistent session-wide prompt history
GLOBAL_HISTORY: list[str] = []


class Key:
    """Key code constants for decoded terminal input."""

    CTRL_C = "\x03"
    CTRL_D = "\x04"
    BACKSPACE = "\x08"
    BACKSPACE_DEL = "\x7f"
    TAB = "\t"
    ENTER = "\r"
    NEWLINE = "\n"
    CTRL_K = "\x0b"  # Command palette
    CTRL_L = "\x0c"  # Clear / redraw screen
    ESCAPE = "\x1b"

    UP = "<UP>"
    DOWN = "<DOWN>"
    LEFT = "<LEFT>"
    RIGHT = "<RIGHT>"
    HOME = "<HOME>"
    END = "<END>"
    DELETE = "<DELETE>"


class TerminalStateRestorer:
    """Context manager to ensure terminal attributes and cursor are restored unconditionally."""

    def __init__(self, fd: int = 0) -> None:
        self.fd = fd
        self.old_termios = None
        self.is_tty = False

    def __enter__(self) -> TerminalStateRestorer:
        try:
            self.is_tty = sys.stdin.isatty()
        except Exception:
            self.is_tty = False

        if self.is_tty and os.name != "nt":
            try:
                import termios
                import tty

                self.old_termios = termios.tcgetattr(self.fd)
                tty.setraw(self.fd)
            except Exception:
                pass
        return self

    def __exit__(self, *args: Any) -> None:
        if self.old_termios is not None:
            try:
                import termios

                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_termios)
            except Exception:
                pass

        # Unconditionally restore cursor visibility and reset ANSI attributes
        try:
            sys.stdout.write("\x1b[?25h\x1b[0m")
            sys.stdout.flush()
        except Exception:
            pass


def _read_raw_key_windows() -> str:
    """Read a single decoded key on Windows using msvcrt."""
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        if ch2 == "H":
            return Key.UP
        elif ch2 == "P":
            return Key.DOWN
        elif ch2 == "K":
            return Key.LEFT
        elif ch2 == "M":
            return Key.RIGHT
        elif ch2 == "G":
            return Key.HOME
        elif ch2 == "O":
            return Key.END
        elif ch2 == "S":
            return Key.DELETE
        return f"<EXT_{ch2}>"
    return ch


def _read_raw_key_posix(fd: int = 0) -> str:
    """Read a single decoded key on POSIX using raw stdin read."""
    import select

    ch = os.read(fd, 1).decode("utf-8", errors="replace")
    if ch == Key.ESCAPE:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            seq = os.read(fd, 8).decode("utf-8", errors="replace")
            if seq == "[A":
                return Key.UP
            elif seq == "[B":
                return Key.DOWN
            elif seq == "[C":
                return Key.RIGHT
            elif seq == "[D":
                return Key.LEFT
            elif seq in ("[H", "[1~"):
                return Key.HOME
            elif seq in ("[F", "[4~"):
                return Key.END
            elif seq == "[3~":
                return Key.DELETE
            return f"<ESC_{seq}>"
        return Key.ESCAPE
    return ch


def read_next_key(key_stream: Optional[Iterator[str]] = None) -> str:
    """Read next single decoded key from key_stream or platform raw console."""
    if key_stream is not None:
        try:
            return next(key_stream)
        except StopIteration:
            return Key.ENTER

    if os.name == "nt":
        return _read_raw_key_windows()
    return _read_raw_key_posix(fd=0)


class RawLineEditor:
    """In-memory line buffer maintaining cursor position, edit history, and rendering."""

    def __init__(
        self,
        prompt_prefix: str = "│ > ",
        initial_text: str = "",
        history: Optional[list[str]] = None,
        state: Optional[Any] = None,
        on_ctrl_k: Optional[Callable[[], None]] = None,
        on_ctrl_l: Optional[Callable[[], None]] = None,
        on_transition: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.prompt_prefix = prompt_prefix
        self.buffer: list[str] = list(initial_text)
        self.cursor: int = len(self.buffer)
        self.history: list[str] = list(history) if history is not None else list(GLOBAL_HISTORY)
        self.history_index: int = len(self.history)
        self.temp_buffer: str = initial_text
        self.state = state
        self.on_ctrl_k = on_ctrl_k
        self.on_ctrl_l = on_ctrl_l
        self.on_transition = on_transition

        self._update_state()

    @property
    def text(self) -> str:
        return "".join(self.buffer)

    def _update_state(self) -> None:
        if self.state is not None and hasattr(self.state, "composer_buffer"):
            self.state.composer_buffer = self.text

    def insert_char(self, ch: str) -> None:
        was_empty = len(self.buffer) == 0
        self.buffer.insert(self.cursor, ch)
        self.cursor += 1
        self._update_state()
        if was_empty and self.on_transition:
            self.on_transition(True)

    def delete_backspace(self) -> None:
        if self.cursor > 0:
            self.buffer.pop(self.cursor - 1)
            self.cursor -= 1
            self._update_state()
            if len(self.buffer) == 0 and self.on_transition:
                self.on_transition(False)

    def delete_forward(self) -> None:
        if self.cursor < len(self.buffer):
            self.buffer.pop(self.cursor)
            self._update_state()
            if len(self.buffer) == 0 and self.on_transition:
                self.on_transition(False)

    def move_left(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1

    def move_right(self) -> None:
        if self.cursor < len(self.buffer):
            self.cursor += 1

    def move_home(self) -> None:
        self.cursor = 0

    def move_end(self) -> None:
        self.cursor = len(self.buffer)

    def history_prev(self) -> None:
        if not self.history:
            return
        if self.history_index == len(self.history):
            self.temp_buffer = self.text

        if self.history_index > 0:
            was_empty = len(self.buffer) == 0
            self.history_index -= 1
            self.buffer = list(self.history[self.history_index])
            self.cursor = len(self.buffer)
            self._update_state()
            if was_empty and len(self.buffer) > 0 and self.on_transition:
                self.on_transition(True)
            elif not was_empty and len(self.buffer) == 0 and self.on_transition:
                self.on_transition(False)

    def history_next(self) -> None:
        if not self.history:
            return
        was_empty = len(self.buffer) == 0
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.buffer = list(self.history[self.history_index])
            self.cursor = len(self.buffer)
            self._update_state()
        elif self.history_index == len(self.history) - 1:
            self.history_index = len(self.history)
            self.buffer = list(self.temp_buffer)
            self.cursor = len(self.buffer)
            self._update_state()

        if was_empty and len(self.buffer) > 0 and self.on_transition:
            self.on_transition(True)
        elif not was_empty and len(self.buffer) == 0 and self.on_transition:
            self.on_transition(False)

    def handle_key(self, key: str) -> bool:
        """Process a single key. Returns True if Enter was pressed, False otherwise."""
        if key == Key.CTRL_C:
            raise KeyboardInterrupt()

        if key == Key.CTRL_D:
            if not self.buffer:
                raise EOFError()
            self.delete_forward()
            return False

        if key in (Key.ENTER, Key.NEWLINE):
            return True

        if key in (Key.BACKSPACE, Key.BACKSPACE_DEL):
            self.delete_backspace()
            return False

        if key == Key.DELETE:
            self.delete_forward()
            return False

        if key == Key.LEFT:
            self.move_left()
            return False

        if key == Key.RIGHT:
            self.move_right()
            return False

        if key == Key.HOME:
            self.move_home()
            return False

        if key == Key.END:
            self.move_end()
            return False

        if key == Key.UP:
            self.history_prev()
            return False

        if key == Key.DOWN:
            self.history_next()
            return False

        if key == Key.CTRL_K:
            if self.on_ctrl_k:
                self.on_ctrl_k()
            return False

        if key == Key.CTRL_L:
            if self.on_ctrl_l:
                self.on_ctrl_l()
            return False

        # Printable characters (ASCII or Unicode)
        if len(key) == 1 and (key.isprintable() or key == "\t"):
            self.insert_char(key)
            return False

        return False

    def redraw_line(self, out: Any = sys.stdout) -> None:
        """Redraw current line with exact cursor placement."""
        buf_str = "".join(self.buffer)
        # \r to start, prompt, line content, \x1b[K to clear remainder of line
        line_out = f"\r{self.prompt_prefix}{buf_str}\x1b[K"
        out.write(line_out)

        # Move cursor back if inside buffer
        offset_back = len(self.buffer) - self.cursor
        if offset_back > 0:
            out.write(f"\x1b[{offset_back}D")
        out.flush()


def raw_prompt_input(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    width: int = 80,
    initial_text: str = "",
    history: Optional[list[str]] = None,
    state: Optional[Any] = None,
    on_ctrl_k: Optional[Callable[[], None]] = None,
    on_ctrl_l: Optional[Callable[[], None]] = None,
    key_stream: Optional[Iterator[str]] = None,
    top_border_renderer: Optional[Callable[[bool], str]] = None,
) -> str:
    """Prompt user using low-level raw keyboard interception.

    Handles real Ctrl+K and Ctrl+L bindings, Arrow Up/Down history, Backspace,
    and printable characters with full Unicode support.
    """
    # Fallback to plain input() if stdin is not a TTY and no test key_stream provided
    is_tty = False
    try:
        is_tty = sys.stdin.isatty()
    except Exception:
        pass

    if not is_tty and key_stream is None:
        # Non-interactive environment or captured input in tests
        try:
            val = input()
            return val
        except (EOFError, KeyboardInterrupt):
            raise

    if state is not None:
        state.is_typing = True
        state.focus_target = "composer"
        if not initial_text and getattr(state, "composer_buffer", ""):
            initial_text = state.composer_buffer

    prompt_prefix = "\033[90m│\033[0m \033[1;36m>\033[0m " if is_tty else "│ > "

    def handle_transition(has_content: bool) -> None:
        """Dynamically update top border when typing begins or buffer is emptied."""
        if top_border_renderer and is_tty:
            try:
                # Save cursor (\x1b[s), move up 1 row (\x1b[1A), rewrite border, restore cursor (\x1b[u)
                border_str = top_border_renderer(has_content)
                sys.stdout.write(f"\x1b[s\x1b[1A\r{border_str}\x1b[u")
                sys.stdout.flush()
            except Exception:
                pass

    def wrapped_ctrl_k() -> None:
        """Handle Ctrl+K with clean prompt restoration."""
        sys.stdout.write("\n")
        sys.stdout.flush()
        if on_ctrl_k:
            on_ctrl_k()
        if top_border_renderer:
            sys.stdout.write("\n" + top_border_renderer(bool(editor.buffer)) + "\n")
        editor.redraw_line()

    def wrapped_ctrl_l() -> None:
        """Handle Ctrl+L with screen clear and clean prompt restoration."""
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
        if on_ctrl_l:
            on_ctrl_l()
        if top_border_renderer:
            sys.stdout.write(top_border_renderer(bool(editor.buffer)) + "\n")
        editor.redraw_line()

    editor = RawLineEditor(
        prompt_prefix=prompt_prefix,
        initial_text=initial_text,
        history=history if history is not None else GLOBAL_HISTORY,
        state=state,
        on_ctrl_k=wrapped_ctrl_k,
        on_ctrl_l=wrapped_ctrl_l,
        on_transition=handle_transition,
    )

    with TerminalStateRestorer():
        editor.redraw_line()
        while True:
            try:
                k = read_next_key(key_stream=key_stream)
                done = editor.handle_key(k)
                if done:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    break
                editor.redraw_line()
            except (KeyboardInterrupt, EOFError):
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise

    result = editor.text
    if result.strip():
        GLOBAL_HISTORY.append(result)
        if history is not None and result not in history:
            history.append(result)

    if state is not None:
        state.is_typing = False
        state.composer_buffer = ""

    return result
