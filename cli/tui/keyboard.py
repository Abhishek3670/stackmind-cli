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

import ctypes
import os
import re
import sys
import time
from typing import Any, Callable, Iterator, Optional

# Persistent session-wide prompt history
GLOBAL_HISTORY: list[str] = []

# Absolute path to repo root debug_keys.log so all directories log to the same file
_DEFAULT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "debug_keys.log",
)
DEBUG_LOG_FILE = os.path.abspath(os.environ.get("STACKMIND_DEBUG_KEYS_FILE", _DEFAULT_LOG_PATH))
DEBUG_KEYS_ENABLED = os.environ.get("STACKMIND_DEBUG_KEYS") in ("1", "true", "yes") or bool(os.environ.get("STACKMIND_DEBUG_KEYS"))


def set_debug_keys(enabled: bool) -> None:
    """Toggle debug key logging dynamically."""
    global DEBUG_KEYS_ENABLED
    DEBUG_KEYS_ENABLED = enabled
    win32_active = os.environ.get("STACKMIND_WIN32_INPUT") in ("1", "true", "yes")
    reader_name = "win32_console" if win32_active else "legacy_msvcrt"
    _log_debug_key(
        f"=== DEBUG KEYS LOGGING {'ENABLED' if enabled else 'DISABLED'} "
        f"(platform={sys.platform}, os={os.name}, reader={reader_name}, log_file={DEBUG_LOG_FILE}) ==="
    )


def _log_debug_key(msg: str) -> None:
    """Write debug key message strictly to log file (never to stdout/stderr)."""
    if not DEBUG_KEYS_ENABLED:
        return
    entry = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


if DEBUG_KEYS_ENABLED:
    _win32_on = os.environ.get("STACKMIND_WIN32_INPUT") in ("1", "true", "yes")
    _reader_name = "win32_console" if _win32_on else "legacy_msvcrt"
    _log_debug_key(
        f"=== DEBUG KEYS INITIALIZED VIA ENV (platform={sys.platform}, os={os.name}, "
        f"reader={_reader_name}, log_file={DEBUG_LOG_FILE}) ==="
    )


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
    PAGE_UP = "<PAGE_UP>"
    PAGE_DOWN = "<PAGE_DOWN>"
    MOUSE_WHEEL_UP = "<WHEEL_UP>"
    MOUSE_WHEEL_DOWN = "<WHEEL_DOWN>"


# SGR Extended Mouse Mode control sequences (WO-054)
ENABLE_MOUSE_REPORTING_SEQ = "\x1b[?1000h\x1b[?1006h"
DISABLE_MOUSE_REPORTING_SEQ = "\x1b[?1006l\x1b[?1000l"


def enable_mouse_reporting(stream: Any = None) -> None:
    """Enable terminal SGR mouse reporting (WO-054).

    Emits \x1b[?1000h\x1b[?1006h (normal tracking + SGR extended coordinates).
    """
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write(ENABLE_MOUSE_REPORTING_SEQ)
            target.flush()
    except Exception:
        pass


def disable_mouse_reporting(stream: Any = None) -> None:
    """Disable terminal SGR mouse reporting (WO-054).

    Emits \x1b[?1006l\x1b[?1000l.
    """
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write(DISABLE_MOUSE_REPORTING_SEQ)
            target.flush()
    except Exception:
        pass


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

        if self.is_tty:
            enable_mouse_reporting(sys.stdout)

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

        # Unconditionally disable mouse reporting, restore cursor visibility, and reset ANSI attributes
        disable_mouse_reporting(sys.stdout)
        try:
            sys.stdout.write("\x1b[?25h\x1b[0m")
            sys.stdout.flush()
        except Exception:
            pass


def _parse_escape_sequence(seq: str) -> str:
    """Parse an escape sequence (content following \x1b) into a Key code."""
    if not seq:
        return Key.ESCAPE

    # SGR Mouse mode: [<Cb;Cx;CyM (press) or [<Cb;Cx;Cym (release)
    if seq.startswith("[<"):
        match = re.match(r"^\[<(\d+);(\d+);(\d+)([Mm])", seq)
        if match:
            btn = int(match.group(1))
            col = int(match.group(2))
            row = int(match.group(3))
            action = match.group(4)
            if btn == 64:
                return Key.MOUSE_WHEEL_UP
            elif btn == 65:
                return Key.MOUSE_WHEEL_DOWN
            return f"<MOUSE_{btn}_{col}_{row}_{action}>"

    # Legacy X10 / Xterm 1000 mouse: [M<btn+32><col+32><row+32>
    if seq.startswith("[M") and len(seq) >= 5:
        btn = ord(seq[2]) - 32
        col = ord(seq[3]) - 32
        row = ord(seq[4]) - 32
        if btn == 64:
            return Key.MOUSE_WHEEL_UP
        elif btn == 65:
            return Key.MOUSE_WHEEL_DOWN
        return f"<MOUSE_X10_{btn}_{col}_{row}>"

    # Arrow keys
    if seq == "[A":
        return Key.UP
    elif seq == "[B":
        return Key.DOWN
    elif seq == "[C":
        return Key.RIGHT
    elif seq == "[D":
        return Key.LEFT

    # Home / End
    elif seq in ("[H", "[1~"):
        return Key.HOME
    elif seq in ("[F", "[4~"):
        return Key.END

    # Delete
    elif seq == "[3~":
        return Key.DELETE

    # Page Up / Page Down
    elif seq == "[5~":
        return Key.PAGE_UP
    elif seq == "[6~":
        return Key.PAGE_DOWN

    return f"<ESC_{seq}>"


def _read_escape_sequence_windows() -> str:
    """Read buffered escape sequence characters following \x1b on Windows."""
    import msvcrt

    if not msvcrt.kbhit():
        time.sleep(0.015)

    if not msvcrt.kbhit():
        return ""

    seq = ""
    start_time = time.monotonic()
    while time.monotonic() - start_time < 0.1:
        if msvcrt.kbhit():
            c = msvcrt.getwch()
            seq += c
            if seq.startswith("[<") and c in ("M", "m"):
                break
            elif seq.startswith("[M") and len(seq) >= 5:
                break
            elif seq.startswith("[") and not seq.startswith(("[<", "[M")) and c in ("~", "A", "B", "C", "D", "H", "F"):
                break
        else:
            time.sleep(0.005)
            if not msvcrt.kbhit():
                # If we've started an escape sequence but haven't received the terminator, keep waiting up to timeout
                if seq.startswith("[<") and not (seq.endswith("M") or seq.endswith("m")):
                    continue
                if seq.startswith("[") and not (seq.endswith("~") or seq[-1] in "ABCDHF"):
                    continue
                break

    return seq


def _read_escape_sequence_posix(fd: int = 0) -> str:
    """Read buffered escape sequence following \x1b on POSIX."""
    import select

    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return ""

    seq = os.read(fd, 64).decode("utf-8", errors="replace")

    if seq.startswith("[<") and not (seq.endswith("M") or seq.endswith("m")):
        start_time = time.monotonic()
        while time.monotonic() - start_time < 0.05:
            r, _, _ = select.select([fd], [], [], 0.01)
            if r:
                more = os.read(fd, 16).decode("utf-8", errors="replace")
                seq += more
                if seq.endswith("M") or seq.endswith("m"):
                    break
            else:
                break

    return seq


def _read_raw_key_windows() -> str:
    """Read a single decoded key on Windows using msvcrt."""
    import msvcrt

    ch = msvcrt.getwch()
    debug_parts = [f"ch={repr(ch)}(ord={ord(ch) if len(ch) == 1 else [ord(c) for c in ch]})"]
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        debug_parts.append(f"ch2={repr(ch2)}(ord={ord(ch2) if len(ch2) == 1 else [ord(c) for c in ch2]})")
        if ch2 == "H":
            res = Key.UP
        elif ch2 == "P":
            res = Key.DOWN
        elif ch2 == "K":
            res = Key.LEFT
        elif ch2 == "M":
            res = Key.RIGHT
        elif ch2 == "G":
            res = Key.HOME
        elif ch2 == "O":
            res = Key.END
        elif ch2 == "S":
            res = Key.DELETE
        elif ch2 == "I":
            res = Key.PAGE_UP
        elif ch2 == "Q":
            res = Key.PAGE_DOWN
        else:
            res = f"<EXT_{ch2}>"
    elif ch == Key.ESCAPE:
        seq = _read_escape_sequence_windows()
        if seq:
            debug_parts.append(f"esc_seq={repr(seq)}")
            res = _parse_escape_sequence(seq)
        else:
            res = Key.ESCAPE
    else:
        if msvcrt.kbhit():
            debug_parts.append("kbhit=True")
        res = ch

    _log_debug_key(f"[_read_raw_key_windows] {', '.join(debug_parts)} -> decoded={repr(res)}")
    return res


# ── Win32 ReadConsoleInputW Low-Level Reader (Phase 1) ───────────────────────

class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", ctypes.c_int32),
        ("wRepeatCount", ctypes.c_ushort),
        ("wVirtualKeyCode", ctypes.c_ushort),
        ("wVirtualScanCode", ctypes.c_ushort),
        ("UnicodeChar", ctypes.c_wchar),
        ("dwControlKeyState", ctypes.c_uint32),
    ]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", COORD),
        ("dwButtonState", ctypes.c_uint32),
        ("dwControlKeyState", ctypes.c_uint32),
        ("dwEventFlags", ctypes.c_uint32),
    ]


class EVENT_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        ("MouseEvent", MOUSE_EVENT_RECORD),
        ("WindowBufferSizeEvent", COORD),
        ("MenuEvent", ctypes.c_uint32),
        ("FocusEvent", ctypes.c_int32),
    ]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", ctypes.c_ushort),
        ("Event", EVENT_UNION),
    ]


class Win32ConsoleReader:
    """Low-level Windows console input reader using ReadConsoleInputW.

    Directly parses INPUT_RECORD events from the console buffer to distinguish
    real keyboard events from mouse wheel events, eliminating conhost scan-code
    ambiguities and enabling fine-grained wheel scrolling.
    """

    def __init__(self, handle: Any = None) -> None:
        self.handle = handle
        self._surrogate_high: Optional[str] = None
        self._pending_keys: list[str] = []

    def _get_input_handle(self) -> Any:
        if self.handle is not None:
            return self.handle
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if h in (0, -1) or not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            try:
                h = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
            except Exception:
                h = None
        self.handle = h
        return h

    def parse_input_record(self, record: INPUT_RECORD) -> Optional[str]:
        """Parse a single Win32 INPUT_RECORD into a decoded Key code or character."""
        event_type = record.EventType

        # Mouse Events (EventType == 0x0002)
        if event_type == 0x0002:
            mouse = record.Event.MouseEvent
            # MOUSE_WHEELED (vertical scroll) is flag 0x0004
            if mouse.dwEventFlags & 0x0004:
                # High word of dwButtonState is signed 16-bit wheel delta
                raw_delta = (mouse.dwButtonState >> 16) & 0xFFFF
                delta = ctypes.c_short(raw_delta).value
                if delta > 0:
                    return Key.MOUSE_WHEEL_UP
                elif delta < 0:
                    return Key.MOUSE_WHEEL_DOWN
            # Non-wheel mouse events (clicks, motion) -> filtered by handle_key
            btn = mouse.dwButtonState
            return f"<MOUSE_{btn}_{mouse.dwMousePosition.X}_{mouse.dwMousePosition.Y}>"

        # Key Events (EventType == 0x0001)
        if event_type == 0x0001:
            key = record.Event.KeyEvent
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(
                    f"[parse_input_record:KEY_EVENT] bKeyDown={bool(key.bKeyDown)}, "
                    f"vk=0x{key.wVirtualKeyCode:02X}, char={repr(key.UnicodeChar)}(ord={ord(key.UnicodeChar)}), "
                    f"ctrl_state=0x{key.dwControlKeyState:04X}"
                )
            # Only process key-down events (ignore key release)
            if not key.bKeyDown:
                return None

            char = key.UnicodeChar
            vk = key.wVirtualKeyCode

            # 1. Unicode Surrogate Pair Handling
            # UTF-16 High Surrogate range: 0xD800 to 0xDBFF
            if 0xD800 <= ord(char) <= 0xDBFF:
                self._surrogate_high = char
                return None  # Wait for the following low surrogate

            # UTF-16 Low Surrogate range: 0xDC00 to 0xDFFF
            if 0xDC00 <= ord(char) <= 0xDFFF:
                if self._surrogate_high is not None:
                    # Combine surrogate pair into full Unicode codepoint
                    high_ord = ord(self._surrogate_high)
                    low_ord = ord(char)
                    codepoint = 0x10000 + ((high_ord - 0xD800) << 10) + (low_ord - 0xDC00)
                    self._surrogate_high = None
                    full_char = chr(codepoint)
                    if key.wRepeatCount > 1:
                        self._pending_keys.extend([full_char] * (key.wRepeatCount - 1))
                    return full_char
                self._surrogate_high = None
                return None

            # Discard stale high surrogate if followed by a regular char
            self._surrogate_high = None

            # 2. Virtual Keys without Unicode representation (UnicodeChar == '\x00')
            if char == "\x00":
                vk_map = {
                    0x21: Key.PAGE_UP,      # VK_PRIOR
                    0x22: Key.PAGE_DOWN,    # VK_NEXT
                    0x23: Key.END,          # VK_END
                    0x24: Key.HOME,         # VK_HOME
                    0x25: Key.LEFT,         # VK_LEFT
                    0x26: Key.UP,           # VK_UP
                    0x27: Key.RIGHT,        # VK_RIGHT
                    0x28: Key.DOWN,         # VK_DOWN
                    0x2E: Key.DELETE,       # VK_DELETE
                }
                res = vk_map.get(vk)
                if res:
                    if key.wRepeatCount > 1:
                        self._pending_keys.extend([res] * (key.wRepeatCount - 1))
                    return res
                return None

            # 3. Virtual key override for VK_DELETE if it arrived with a char code like '\x7f' or '\x00'
            if vk == 0x2E:
                if key.wRepeatCount > 1:
                    self._pending_keys.extend([Key.DELETE] * (key.wRepeatCount - 1))
                return Key.DELETE

            # 4. Standard character or control character
            if key.wRepeatCount > 1:
                self._pending_keys.extend([char] * (key.wRepeatCount - 1))
            return char

        # Other events (WINDOW_BUFFER_SIZE_EVENT, FOCUS_EVENT, etc.) are ignored
        return None

    def _read_escape_sequence(self, h: Any) -> str:
        """Read buffered escape sequence characters following \x1b from Win32 console buffer."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        rec = INPUT_RECORD()
        read_count = ctypes.c_uint32()
        num_events = ctypes.c_uint32()

        # Check if events are immediately available in the console queue
        if kernel32.GetNumberOfConsoleInputEvents(h, ctypes.byref(num_events)):
            if num_events.value == 0:
                time.sleep(0.015)

        seq = ""
        start_time = time.monotonic()
        while time.monotonic() - start_time < 0.1:
            if not kernel32.GetNumberOfConsoleInputEvents(h, ctypes.byref(num_events)):
                break
            if num_events.value > 0:
                if kernel32.ReadConsoleInputW(h, ctypes.byref(rec), 1, ctypes.byref(read_count)):
                    if read_count.value > 0:
                        if rec.EventType == 0x0001:  # KEY_EVENT
                            key = rec.Event.KeyEvent
                            if key.bKeyDown:
                                c = key.UnicodeChar
                                if c and c != "\x00":
                                    seq += c
                                    # SGR Mouse mode: [<64;...M or [<65;...m
                                    if seq.startswith("[<") and c in ("M", "m"):
                                        break
                                    # Arrow keys / Home / End: [A, [B, [C, [D, [H, [F
                                    if len(seq) == 2 and seq[0] == "[" and seq[1] in ("A", "B", "C", "D", "H", "F"):
                                        break
                                    # Extended keys: [3~ (DEL), [5~ (PGUP), [6~ (PGDN), [1~ (HOME), [4~ (END)
                                    if len(seq) >= 3 and seq.endswith("~"):
                                        break
                                    # Legacy X10 mouse mode: [M...
                                    if len(seq) >= 5 and seq.startswith("[M"):
                                        break
                        elif rec.EventType == 0x0002:  # MOUSE_EVENT
                            m_parsed = self.parse_input_record(rec)
                            if m_parsed:
                                self._pending_keys.append(m_parsed)
            else:
                time.sleep(0.002)
        return seq

    def read_key(self, timeout: float = 0.05) -> Optional[str]:
        """Read a single decoded key or mouse event. Loops until a recognized event or timeout."""
        if self._pending_keys:
            return self._pending_keys.pop(0)

        h = self._get_input_handle()
        if not h or h in (0, -1):
            return None

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        rec = INPUT_RECORD()
        read_count = ctypes.c_uint32()

        start = time.monotonic()
        while True:
            num_events = ctypes.c_uint32()
            if not kernel32.GetNumberOfConsoleInputEvents(h, ctypes.byref(num_events)):
                return None

            if num_events.value > 0:
                if kernel32.ReadConsoleInputW(h, ctypes.byref(rec), 1, ctypes.byref(read_count)):
                    if read_count.value > 0:
                        parsed = self.parse_input_record(rec)
                        if parsed == Key.ESCAPE:
                            seq = self._read_escape_sequence(h)
                            if seq:
                                _log_debug_key(f"[Win32ConsoleReader.read_key] esc_seq={repr(seq)}")
                                parsed = _parse_escape_sequence(seq)
                        if parsed is not None:
                            _log_debug_key(f"[Win32ConsoleReader.read_key] returning parsed={repr(parsed)}")
                            return parsed
            else:
                if timeout <= 0:
                    return None
                if time.monotonic() - start >= timeout:
                    return None
                time.sleep(0.005)


_win32_reader = Win32ConsoleReader()


def _read_raw_key_windows_v2(handle: Any = None) -> str:
    """Read a single decoded key on Windows using Win32 ReadConsoleInputW.

    Replaces msvcrt.getwch() to natively capture MOUSE_EVENT records for
    mouse wheel scrolling and distinguish them from arrow keys.
    """
    global _win32_reader
    if handle is not None and _win32_reader.handle != handle:
        _win32_reader = Win32ConsoleReader(handle)

    while True:
        key = _win32_reader.read_key(timeout=0.1)
        if key is not None:
            _log_debug_key(f"[_read_raw_key_windows_v2] decoded={repr(key)}")
            return key


def _read_raw_key_posix(fd: int = 0) -> str:
    """Read a single decoded key on POSIX using raw stdin read."""
    ch = os.read(fd, 1).decode("utf-8", errors="replace")
    debug_parts = [f"ch={repr(ch)}(ord={ord(ch) if len(ch) == 1 else [ord(c) for c in ch]})"]
    if ch == Key.ESCAPE:
        seq = _read_escape_sequence_posix(fd)
        if seq:
            debug_parts.append(f"seq={repr(seq)}")
            res = _parse_escape_sequence(seq)
        else:
            res = Key.ESCAPE
    else:
        res = ch

    _log_debug_key(f"[_read_raw_key_posix] {', '.join(debug_parts)} -> decoded={repr(res)}")
    return res


def read_next_key(key_stream: Optional[Iterator[str]] = None) -> str:
    """Read next single decoded key from key_stream or platform raw console."""
    if key_stream is not None:
        try:
            res = next(key_stream)
        except StopIteration:
            res = Key.ENTER
        _log_debug_key(f"[read_next_key:key_stream] returned {repr(res)}")
        return res

    if os.name == "nt":
        if os.environ.get("STACKMIND_WIN32_INPUT") in ("1", "true", "yes"):
            res = _read_raw_key_windows_v2()
        else:
            res = _read_raw_key_windows()
    else:
        res = _read_raw_key_posix(fd=0)
    _log_debug_key(f"[read_next_key] final returned {repr(res)}")
    return res


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
        on_page_up: Optional[Callable[[], None]] = None,
        on_page_down: Optional[Callable[[], None]] = None,
        on_wheel_up: Optional[Callable[[], None]] = None,
        on_wheel_down: Optional[Callable[[], None]] = None,
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
        self.on_page_up = on_page_up
        self.on_page_down = on_page_down
        self.on_wheel_up = on_wheel_up
        self.on_wheel_down = on_wheel_down
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
        if DEBUG_KEYS_ENABLED:
            _log_debug_key(f"[handle_key] received key={repr(key)}")

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

        if key == Key.PAGE_UP:
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] MATCHED Key.PAGE_UP (has on_page_up={self.on_page_up is not None})")
            if self.on_page_up:
                self.on_page_up()
            return False

        if key == Key.PAGE_DOWN:
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] MATCHED Key.PAGE_DOWN (has on_page_down={self.on_page_down is not None})")
            if self.on_page_down:
                self.on_page_down()
            return False

        if key == Key.MOUSE_WHEEL_UP:
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] MATCHED Key.MOUSE_WHEEL_UP (has on_wheel_up={self.on_wheel_up is not None})")
            if self.on_wheel_up:
                self.on_wheel_up()
            elif self.on_page_up:
                self.on_page_up()
            return False

        if key == Key.MOUSE_WHEEL_DOWN:
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] MATCHED Key.MOUSE_WHEEL_DOWN (has on_wheel_down={self.on_wheel_down is not None})")
            if self.on_wheel_down:
                self.on_wheel_down()
            elif self.on_page_down:
                self.on_page_down()
            return False

        if key.startswith("<MOUSE_"):
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] Filtered non-wheel mouse event: {key}")
            return False

        # Printable characters (ASCII or Unicode)
        if len(key) == 1 and (key.isprintable() or key == "\t"):
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] inserted printable character: {repr(key)}")
            self.insert_char(key)
            return False

        if DEBUG_KEYS_ENABLED:
            _log_debug_key(f"[handle_key] UNHANDLED key ignored: {repr(key)}")
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
    on_page_up: Optional[Callable[[], None]] = None,
    on_page_down: Optional[Callable[[], None]] = None,
    on_wheel_up: Optional[Callable[[], None]] = None,
    on_wheel_down: Optional[Callable[[], None]] = None,
    key_stream: Optional[Iterator[str]] = None,
    top_border_renderer: Optional[Callable[[bool], str]] = None,
) -> str:
    """Prompt user using low-level raw keyboard interception.

    Handles real Ctrl+K and Ctrl+L bindings, Page Up/Down conversation scroll,
    Arrow Up/Down history, Backspace, and printable characters with full Unicode support.
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

    def wrapped_page_up() -> None:
        """Handle Page Up — scroll conversation viewport up, then restore prompt line."""
        if on_page_up:
            on_page_up()
        editor.redraw_line()

    def wrapped_page_down() -> None:
        """Handle Page Down — scroll conversation viewport down, then restore prompt line."""
        if on_page_down:
            on_page_down()
        editor.redraw_line()

    def wrapped_wheel_up() -> None:
        """Handle Mouse Wheel Up — scroll conversation viewport up, then restore prompt line."""
        if on_wheel_up:
            on_wheel_up()
        elif on_page_up:
            on_page_up()
        editor.redraw_line()

    def wrapped_wheel_down() -> None:
        """Handle Mouse Wheel Down — scroll conversation viewport down, then restore prompt line."""
        if on_wheel_down:
            on_wheel_down()
        elif on_page_down:
            on_page_down()
        editor.redraw_line()

    editor = RawLineEditor(
        prompt_prefix=prompt_prefix,
        initial_text=initial_text,
        history=history if history is not None else GLOBAL_HISTORY,
        state=state,
        on_ctrl_k=wrapped_ctrl_k,
        on_ctrl_l=wrapped_ctrl_l,
        on_page_up=wrapped_page_up,
        on_page_down=wrapped_page_down,
        on_wheel_up=wrapped_wheel_up,
        on_wheel_down=wrapped_wheel_down,
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
