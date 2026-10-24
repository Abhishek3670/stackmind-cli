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
import shutil
import sys
import time
from typing import Any, Callable, Iterator, Optional

# Persistent session-wide prompt history
GLOBAL_HISTORY: list[str] = []
_ANSI_STRIP_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

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

    CTRL_B = "\x02"  # Toggle runtime sidebar (full-width view)
    CTRL_C = "\x03"
    CTRL_D = "\x04"
    CTRL_G = "\x07"  # Auto-fill / adopt Architect suggested goal
    BACKSPACE = "\x08"
    BACKSPACE_DEL = "\x7f"
    TAB = "\t"
    ENTER = "\r"
    NEWLINE = "\n"
    CTRL_K = "\x0b"  # Command palette
    CTRL_L = "\x0c"  # Clear / redraw screen
    CTRL_O = "\x0f"  # Toggle thinking block
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
    PASTE_START = "<PASTE_START>"
    PASTE_END = "<PASTE_END>"
    SHIFT_ENTER = "<SHIFT_ENTER>"
    ALT_ENTER = "<ALT_ENTER>"
    CTRL_ENTER = "<CTRL_ENTER>"
    ALT_V = "<ALT_V>"
    CTRL_V = "\x16"  # Paste from clipboard (ASCII SYN / ord 22)
    CTRL_W = "\x17"  # Toggle scroll focus (Conversation <-> Work Orders)
    SHIFT_INSERT = "<SHIFT_INSERT>"
    ALT_UP = "<ALT_UP>"
    ALT_DOWN = "<ALT_DOWN>"
    ALT_PAGE_UP = "<ALT_PAGE_UP>"
    ALT_PAGE_DOWN = "<ALT_PAGE_DOWN>"


# SGR Extended Mouse Mode control sequences (WO-054)
ENABLE_MOUSE_REPORTING_SEQ = ""
DISABLE_MOUSE_REPORTING_SEQ = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"


def enable_mouse_reporting(stream: Any = None) -> None:
    """Enable terminal SGR mouse reporting (WO-054).

    Emits \\x1b[?1000h\\x1b[?1002h\\x1b[?1006h (button event tracking + SGR extended coordinates).
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

    Emits \\x1b[?1006l\\x1b[?1002l\\x1b[?1000l.
    """
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write(DISABLE_MOUSE_REPORTING_SEQ)
            target.flush()
    except Exception:
        pass


class TerminalStateRestorer:
    """Context manager to ensure terminal attributes, mouse reporting, and bracketed paste are restored unconditionally."""

    def __init__(self, fd: int = 0, state: Any = None) -> None:
        self.fd = fd
        self.state = state
        self.old_termios = None
        self.old_win_mode = None
        self.is_tty = False

    def __enter__(self) -> TerminalStateRestorer:
        try:
            self.is_tty = sys.stdin.isatty()
        except Exception:
            self.is_tty = False

        if self.is_tty:
            mouse_enabled = getattr(self.state, "mouse_reporting_enabled", False) if self.state is not None else False
            if os.environ.get("STACKMIND_MOUSE", "").strip().lower() in ("1", "true", "yes"):
                mouse_enabled = True
            if mouse_enabled:
                enable_mouse_reporting(sys.stdout)
            else:
                disable_mouse_reporting(sys.stdout)
            try:
                sys.stdout.write("\x1b[?2004h")
                sys.stdout.flush()
            except Exception:
                pass

        if self.is_tty and os.name == "nt":
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                h = kernel32.GetStdHandle(-10)
                if h not in (0, -1):
                    mode = ctypes.c_uint32()
                    if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                        self.old_win_mode = mode.value
                        # Ensure QuickEdit mode (0x0040) and extended flags (0x0080) are enabled
                        # so native terminal text selection & copy always works smoothly without Shift.
                        new_mode = mode.value | 0x0040 | 0x0080
                        kernel32.SetConsoleMode(h, new_mode)
            except Exception:
                pass

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

        if self.is_tty and os.name == "nt" and self.old_win_mode is not None:
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                h = kernel32.GetStdHandle(-10)
                if h not in (0, -1):
                    kernel32.SetConsoleMode(h, self.old_win_mode)
            except Exception:
                pass

        # Unconditionally disable mouse reporting, disable bracketed paste, restore cursor visibility, and reset ANSI attributes
        disable_mouse_reporting(sys.stdout)
        try:
            sys.stdout.write("\x1b[?2004l\x1b[?25h\x1b[0m")
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

    # Alt+Up / Alt+Down / Alt+PgUp / Alt+PgDn (xterm / vt sequences)
    elif seq in ("[1;3A", "[1;5A"):
        return Key.ALT_UP
    elif seq in ("[1;3B", "[1;5B"):
        return Key.ALT_DOWN
    elif seq in ("[5;3~", "[5;5~"):
        return Key.ALT_PAGE_UP
    elif seq in ("[6;3~", "[6;5~"):
        return Key.ALT_PAGE_DOWN

    # Bracketed paste
    elif seq == "[200~":
        return Key.PASTE_START
    elif seq == "[201~":
        return Key.PASTE_END

    # Shift+Enter / Alt+Enter / Ctrl+Enter
    elif seq in (
        "[13;2u",
        "[27;2;13~",
        "[13;2~",
        "[10;2u",
        "[27;2;10~",
        "[10;2~",
        "OM",
    ):
        return Key.SHIFT_ENTER
    elif seq in (
        "\r",
        "\n",
        "\r\n",
        "[13;3u",
        "[27;3;13~",
        "[13;4u",
        "[27;4;13~",
    ):
        return Key.ALT_ENTER
    elif seq in ("[13;5u", "[27;5;13~", "[10;5u", "[27;5;10~"):
        return Key.CTRL_ENTER

    # Alt+V (Paste image from clipboard)
    elif seq in ("v", "V", "[118;3u", "[27;3;118~", "[118;5u"):
        return Key.ALT_V

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
            elif seq.startswith("[") and not seq.startswith(("[<", "[M")) and c in ("~", "A", "B", "C", "D", "H", "F", "u"):
                break
            elif seq in ("\r", "\n", "\r\n", "v", "V"):
                break
        else:
            time.sleep(0.005)
            if not msvcrt.kbhit():
                # If we've started an escape sequence but haven't received the terminator, keep waiting up to timeout
                if seq.startswith("[<") and not (seq.endswith("M") or seq.endswith("m")):
                    continue
                if seq.startswith("[") and not (seq.endswith("~") or seq[-1] in "ABCDHFu"):
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


def _read_raw_key_windows(timeout: Optional[float] = None) -> Optional[str]:
    """Read a single decoded key on Windows using msvcrt."""
    import msvcrt

    if timeout is not None:
        start_t = time.monotonic()
        while time.monotonic() - start_t < timeout:
            if msvcrt.kbhit():
                break
            time.sleep(0.01)
        else:
            return None

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
        elif ch2 in ("\xa6", "\x1c"):
            res = Key.ALT_ENTER
        elif ch2 in ("\x2f", "\xaf"):
            res = Key.ALT_V
        else:
            res = f"<EXT_{ch2}>"
    elif ch == Key.ESCAPE:
        seq = _read_escape_sequence_windows()
        if seq:
            debug_parts.append(f"esc_seq={repr(seq)}")
            res = _parse_escape_sequence(seq)
        else:
            res = Key.ESCAPE
    elif ch == Key.CTRL_V:
        res = Key.CTRL_V
    elif ch == Key.CTRL_B:
        res = Key.CTRL_B
    elif ch == Key.CTRL_G:
        res = Key.CTRL_G
    elif ch in ("\r", "\n"):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if user32.GetKeyState(0x10) < 0:  # VK_SHIFT
                res = Key.SHIFT_ENTER
            elif user32.GetKeyState(0x11) < 0:  # VK_CONTROL
                res = Key.CTRL_ENTER
            else:
                res = ch
        except Exception:
            res = ch
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
            self.last_mouse_col = mouse.dwMousePosition.X
            self.last_mouse_row = mouse.dwMousePosition.Y
            # MOUSE_WHEELED (vertical scroll) is flag 0x0004
            if mouse.dwEventFlags & 0x0004:
                # High word of dwButtonState is signed 16-bit wheel delta
                raw_delta = (mouse.dwButtonState >> 16) & 0xFFFF
                delta = ctypes.c_short(raw_delta).value
                if delta > 0:
                    return Key.MOUSE_WHEEL_UP
                elif delta < 0:
                    return Key.MOUSE_WHEEL_DOWN
            # Filter out mouse motion with no buttons pressed to avoid event queue spam
            if (mouse.dwEventFlags & 0x0001) and mouse.dwButtonState == 0:
                return None
            # If left mouse button is held while moving (MOUSE_MOVED), report as drag (btn=32)
            if (mouse.dwEventFlags & 0x0001) and (mouse.dwButtonState & 0x0001):
                btn = 32
            else:
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

            # 2. Control Key Combinations (Ctrl+O, Ctrl+K, Ctrl+L, Ctrl+C, Ctrl+D, Ctrl+V)
            # Handle control keys regardless of whether UnicodeChar is null ('\x00') or letter
            is_ctrl = bool(key.dwControlKeyState & (0x0004 | 0x0008))
            is_shift = bool(key.dwControlKeyState & 0x0010)
            is_alt = bool(key.dwControlKeyState & (0x0001 | 0x0002))

            if is_ctrl:
                ctrl_vk_map = {
                    0x42: Key.CTRL_B,  # VK_B -> '\x02' (Toggle sidebar)
                    0x47: Key.CTRL_G,  # VK_G -> '\x07' (Adopt suggested goal)
                    0x4F: Key.CTRL_O,  # VK_O -> '\x0f' (Toggle thinking)
                    0x4B: Key.CTRL_K,  # VK_K -> '\x0b' (Command palette)
                    0x4C: Key.CTRL_L,  # VK_L -> '\x0c' (Clear screen)
                    0x43: Key.CTRL_C,  # VK_C -> '\x03' (Cancel / exit)
                    0x44: Key.CTRL_D,  # VK_D -> '\x04' (EOF / exit)
                    0x56: Key.CTRL_V,  # VK_V -> '\x16' (Paste clipboard)
                    0x57: Key.CTRL_W,  # VK_W -> '\x17' (Toggle scroll focus)
                    0x0D: Key.CTRL_ENTER,  # VK_RETURN
                    0x0A: Key.CTRL_ENTER,  # VK_RETURN (LF)
                }
                res = ctrl_vk_map.get(vk)
                if res:
                    if key.wRepeatCount > 1:
                        self._pending_keys.extend([res] * (key.wRepeatCount - 1))
                    return res

            if is_alt:
                alt_vk_map = {
                    0x26: Key.ALT_UP,         # VK_UP
                    0x28: Key.ALT_DOWN,       # VK_DOWN
                    0x21: Key.ALT_PAGE_UP,    # VK_PRIOR
                    0x22: Key.ALT_PAGE_DOWN,  # VK_NEXT
                    0x0D: Key.ALT_ENTER,      # VK_RETURN (Enter)
                    0x0A: Key.ALT_ENTER,      # VK_RETURN (LF)
                    0x56: Key.ALT_V,          # VK_V (Paste Image)
                }
                res = alt_vk_map.get(vk)
                if res:
                    if key.wRepeatCount > 1:
                        self._pending_keys.extend([res] * (key.wRepeatCount - 1))
                    return res

            # Virtual key override for Ctrl+V / Shift+Insert (paste from clipboard)
            if (vk == 0x2D and is_shift) or char == Key.CTRL_V:
                if key.wRepeatCount > 1:
                    self._pending_keys.extend([Key.CTRL_V] * (key.wRepeatCount - 1))
                return Key.CTRL_V

            # 3. Virtual Keys without Unicode representation (UnicodeChar == '\x00')
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

            # 4. Virtual key override for VK_DELETE if it arrived with a char code like '\x7f' or '\x00'
            if vk == 0x2E:
                if key.wRepeatCount > 1:
                    self._pending_keys.extend([Key.DELETE] * (key.wRepeatCount - 1))
                return Key.DELETE

            # Virtual key override for VK_RETURN when Shift, Ctrl, or Alt is held
            if vk in (0x0D, 0x0A):
                if is_shift:
                    if key.wRepeatCount > 1:
                        self._pending_keys.extend([Key.SHIFT_ENTER] * (key.wRepeatCount - 1))
                    return Key.SHIFT_ENTER
                if is_ctrl:
                    if key.wRepeatCount > 1:
                        self._pending_keys.extend([Key.CTRL_ENTER] * (key.wRepeatCount - 1))
                    return Key.CTRL_ENTER
                if is_alt:
                    if key.wRepeatCount > 1:
                        self._pending_keys.extend([Key.ALT_ENTER] * (key.wRepeatCount - 1))
                    return Key.ALT_ENTER

            # 5. Standard character or control character
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
                                    # Alt+Enter / Alt+V / Kitty u protocol
                                    if c in ("\r", "\n", "v", "V") or (len(seq) >= 3 and c == "u"):
                                        break
                                elif key.wVirtualKeyCode in (0x0D, 0x0A):
                                    seq += "\r"
                                    break
                                elif key.wVirtualKeyCode == 0x56:  # VK_V
                                    seq += "v"
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


def _read_raw_key_windows_v2(handle: Any = None, timeout: Optional[float] = None) -> Optional[str]:
    """Read a single decoded key on Windows using Win32 ReadConsoleInputW.

    Replaces msvcrt.getwch() to natively capture MOUSE_EVENT records for
    mouse wheel scrolling and distinguish them from arrow keys.
    """
    global _win32_reader
    if handle is not None and _win32_reader.handle != handle:
        _win32_reader = Win32ConsoleReader(handle)

    err_count = 0
    start_t = time.monotonic()
    t_step = min(0.1, timeout) if timeout is not None else 0.1
    while True:
        key = _win32_reader.read_key(timeout=t_step)
        if key is not None:
            _log_debug_key(f"[_read_raw_key_windows_v2] decoded={repr(key)}")
            return key
        if timeout is not None and time.monotonic() - start_t >= timeout:
            return None
        h = _win32_reader._get_input_handle()
        if not h or h in (0, -1):
            return _read_raw_key_windows(timeout=timeout)
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            num_events = ctypes.c_uint32()
            if not kernel32.GetNumberOfConsoleInputEvents(h, ctypes.byref(num_events)):
                err_count += 1
                if err_count > 3:
                    return _read_raw_key_windows(timeout=timeout)
            else:
                err_count = 0
        except Exception:
            return _read_raw_key_windows(timeout=timeout)


def _read_raw_key_posix(fd: int = 0, timeout: Optional[float] = None) -> Optional[str]:
    """Read a single decoded key on POSIX using raw stdin read."""
    if timeout is not None:
        import select

        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return None
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


def read_next_key(
    key_stream: Optional[Iterator[str]] = None,
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Read next single decoded key from key_stream or platform raw console."""
    if key_stream is not None:
        try:
            res = next(key_stream)
        except StopIteration:
            res = Key.ENTER
        _log_debug_key(f"[read_next_key:key_stream] returned {repr(res)}")
        return res

    if os.name == "nt":
        use_v2 = False
        opt = os.environ.get("STACKMIND_WIN32_INPUT")
        if opt is not None:
            use_v2 = opt.strip().lower() in ("1", "true", "yes", "on")
        else:
            # Default to Win32ConsoleReader if an interactive Win32 console handle is available
            try:
                h = _win32_reader._get_input_handle()
                if h and h not in (0, -1):
                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    mode = ctypes.c_uint32()
                    if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                        use_v2 = True
            except Exception:
                use_v2 = False

        if use_v2:
            try:
                res = _read_raw_key_windows_v2(timeout=timeout)
            except Exception:
                res = _read_raw_key_windows(timeout=timeout)
        else:
            res = _read_raw_key_windows(timeout=timeout)
    else:
        res = _read_raw_key_posix(fd=0, timeout=timeout)
    if res is not None:
        _log_debug_key(f"[read_next_key] final returned {repr(res)}")
    return res


def has_pending_input() -> bool:
    """Check if unread input events are immediately available in the console buffer."""
    if os.name == "nt":
        try:
            if getattr(_win32_reader, "_pending_keys", None):
                return True
            import msvcrt
            if msvcrt.kbhit():
                return True
            h = _win32_reader._get_input_handle()
            if h and h not in (0, -1):
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                num_events = ctypes.c_uint32()
                if kernel32.GetNumberOfConsoleInputEvents(h, ctypes.byref(num_events)):
                    return num_events.value > 0
        except Exception:
            pass
    else:
        try:
            import select
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
            return bool(r)
        except Exception:
            pass
    return False


DEFAULT_COMMANDS: list[tuple[str, str, bool]] = [
    (":resume", "Resume active session", False),
    (":status", "Display current Session & Delivery View", False),
    (":plan", "Display Plan Approval surface & history", False),
    (":pause", "Pause active session turn", False),
    (":wo", "Display Work Orders table and progress", False),
    (":goal", "Submit product goal to Architecture (or adopt suggested goal)", True),
    (":adopt", "Adopt suggested goal from Architecture", False),
    (":panel", "Toggle runtime sidebar panel (Ctrl+B)", False),
    (":sidebar", "Toggle runtime sidebar panel (Ctrl+B)", False),
    (":approve", "Submit Plan approval or HITL approval", False),
    (":reject", "Submit Plan rejection feedback", False),
    (":roles", "Interactive Agent Roles & Execution Backend bindings panel", False),
    (":rebind", "Rebind agent role to backend (use :roles)", True),
    (":config", "Configure StackMind execution profile, governance & system policies", False),
    (":provider", "Configure cloud & local LLM providers, API keys & base URLs", False),
    (":providers", "Alias for :provider", False),
    (":tree", "Display Hierarchical Operation Tree", False),
    (":agents", "Display Hierarchical Agent Tree", False),
    (":completion", "Display Project Complete checklist", False),
    (":cancel", "Cancel in-flight turn, agent, or operation", False),
    (":contract", "Display Contract Boundary HUD & permissions", False),
    (":diff", "Display Unified Diff viewer", False),
    (":matrix", "Display 6-Dimensional Verification Matrix", False),
    (":events", "Stream incremental sequenced events", False),
    (":reconnect", "Reconnect to daemon & replay missed events", False),
    (":chat", "Display conversation transcript", False),
    (":actions", "Toggle/expand/collapse turn Actions group", False),
    (":thinking", "Toggle thinking block expansion (Ctrl+O)", False),
    (":govern", "Configure governance verbosity: tree | flat | compact", False),
    (":compact", "Compact older transcript turns", False),
    (":timeout", "Get or set client turn wait timeout", False),
    (":landing", "Display branded landing block", False),
    (":skills", "Display available procedural skills & runbooks", False),
    (":skill", "Display available procedural skills & runbooks", False),
    (":consume-skills", "Install/import procedural skills from file or directory", True),
    (":consume-skill", "Install/import procedural skill from file or directory", True),
    (":copy", "Copy latest response (or :copy all for transcript) to clipboard", False),
    (":projects", "List registered projects and runtime home", False),
    (":help", "Show help menu with all commands", False),
    (":debugkeys", "Toggle debug key logging to file", False),
    (":exit", "Gracefully stop daemon and exit", False),
]


def format_dropdown_lines(
    matches: list[tuple[str, str, bool]],
    selected_index: int,
    width: int,
    max_visible: int = 6,
) -> list[str]:
    """Format dropdown lines with borders, selection marker, and colors."""
    if not matches:
        return []

    total = len(matches)
    selected_index = max(0, min(selected_index, total - 1))

    if total <= max_visible:
        start_idx = 0
        end_idx = total
    else:
        half = max_visible // 2
        if selected_index < half:
            start_idx = 0
            end_idx = max_visible
        elif selected_index >= total - half:
            start_idx = total - max_visible
            end_idx = total
        else:
            start_idx = selected_index - half
            end_idx = start_idx + max_visible

    box_w = max(20, width)
    inner_w = box_w - 4

    border_color = "\033[90m"
    cyan_bold = "\033[1;36m"
    white_bold = "\033[1;37m"
    dim_color = "\033[90m"
    reset = "\033[0m"

    hint = "↑/↓ nav  Tab/Enter select  Esc close"
    header_title = " Commands "
    top_mid = f"──{header_title}─"
    rem = box_w - 5 - len(top_mid) - len(hint)
    if rem >= 0:
        top_line = f"{border_color}┌{top_mid}{'─' * rem} {dim_color}{hint}{border_color} ─┐{reset}"
    else:
        rem_short = max(0, box_w - 2 - len(top_mid))
        top_line = f"{border_color}┌{top_mid}{'─' * rem_short}┐{reset}"

    lines = [top_line]
    visible_matches = matches[start_idx:end_idx]
    max_cmd_len = max(len(m[0]) for m in visible_matches) if visible_matches else 14
    if inner_w < 32:
        cmd_col_w = min(16, max(8, inner_w // 2))
    else:
        cmd_col_w = max(16, max_cmd_len + 1)

    for i in range(start_idx, end_idx):
        cmd, desc, _ = matches[i]
        is_sel = (i == selected_index)

        cmd_padded = cmd.ljust(cmd_col_w)
        if len(cmd_padded) > max(8, inner_w - 4):
            cmd_padded = cmd_padded[:max(8, inner_w - 4)]

        avail_desc_w = max(0, inner_w - len(cmd_padded) - 2)
        if len(desc) > avail_desc_w:
            if avail_desc_w >= 4:
                desc_str = desc[:avail_desc_w - 3] + "..."
            else:
                desc_str = desc[:avail_desc_w]
        else:
            desc_str = desc

        padding_w = max(0, inner_w - (2 + len(cmd_padded) + len(desc_str)))

        if is_sel:
            marker = f"{cyan_bold}> {reset}"
            cmd_part = f"{white_bold}{cmd_padded}{reset}"
            desc_part = f"{cyan_bold}{desc_str}{reset}"
            fill = " " * padding_w
            line = f"{border_color}│ {reset}{marker}{cmd_part}{desc_part}{fill} {border_color}│{reset}"
        else:
            marker = "  "
            cmd_part = f"{white_bold}{cmd_padded}{reset}"
            desc_part = f"{dim_color}{desc_str}{reset}"
            fill = " " * padding_w
            line = f"{border_color}│ {reset}{marker}{cmd_part}{desc_part}{fill} {border_color}│{reset}"
        lines.append(line)

    footer_info = f" ({selected_index + 1}/{total}) "
    fill_bot = max(0, box_w - 3 - len(footer_info))
    bot_line = f"{border_color}└─{footer_info}{'─' * fill_bot}┘{reset}"
    lines.append(bot_line)

    return lines


def wrap_composer_paragraph(para: str, width: int) -> list[tuple[int, int]]:
    """Return list of (start, end) index spans in para for wrapping within width."""
    if not para:
        return [(0, 0)]
    spans: list[tuple[int, int]] = []
    start = 0
    n = len(para)
    while start < n:
        if n - start <= width:
            spans.append((start, n))
            break
        candidate_end = start + width
        # Look for the last space within width to break cleanly at word boundary
        space_idx = para.rfind(" ", start, candidate_end + 1)
        if space_idx > start:
            end = space_idx + 1
        else:
            # Word exceeds width, hard-break at width
            end = candidate_end
        spans.append((start, end))
        start = end
    return spans


def wrap_composer_text_with_cursor(
    text: str, width: int, cursor: int
) -> tuple[list[str], list[int], int, int]:
    """Wrap composer text to width and compute visual cursor coordinates.

    Returns:
        (visual_lines, line_starts, cursor_line_idx, cursor_col_idx)
    """
    if width <= 0:
        width = 80
    paras = text.split("\n")
    visual_lines: list[str] = []
    line_starts: list[int] = []
    g_offset = 0
    c_line = 0
    c_col = 0
    found_cursor = False

    for p_idx, para in enumerate(paras):
        spans = wrap_composer_paragraph(para, width)
        for s_idx, (s, e) in enumerate(spans):
            v_line_idx = len(visual_lines)
            visual_lines.append(para[s:e])
            g_start = g_offset + s
            g_end = g_offset + e
            line_starts.append(g_start)

            if not found_cursor:
                is_last_span_of_para = (s_idx == len(spans) - 1)
                is_last_para = (p_idx == len(paras) - 1)

                if g_start <= cursor < g_end:
                    c_line = v_line_idx
                    c_col = cursor - g_start
                    found_cursor = True
                elif cursor == g_end:
                    if is_last_span_of_para:
                        if is_last_para or cursor < g_offset + len(para) + 1:
                            c_line = v_line_idx
                            c_col = e - s
                            found_cursor = True
        g_offset += len(para) + 1

    if not found_cursor:
        c_line = max(0, len(visual_lines) - 1)
        c_col = len(visual_lines[-1]) if visual_lines else 0

    return visual_lines, line_starts, c_line, c_col


def wrap_composer_text(content: str | list[str] | None, width: int) -> list[str]:
    """Wrap composer text or lines into visual lines respecting width without ellipsis truncation."""
    if not content:
        return [""]
    if isinstance(content, list):
        text = "\n".join(content)
    else:
        text = str(content)
    lines, _, _, _ = wrap_composer_text_with_cursor(text, width, cursor=0)
    return lines or [""]


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
        on_ctrl_o: Optional[Callable[[], None]] = None,
        on_ctrl_b: Optional[Callable[[], None]] = None,
        on_ctrl_g: Optional[Callable[[], None]] = None,
        on_ctrl_w: Optional[Callable[[], None]] = None,
        on_alt_up: Optional[Callable[[], None]] = None,
        on_alt_down: Optional[Callable[[], None]] = None,
        on_alt_page_up: Optional[Callable[[], None]] = None,
        on_alt_page_down: Optional[Callable[[], None]] = None,
        on_page_up: Optional[Callable[[], None]] = None,
        on_page_down: Optional[Callable[[], None]] = None,
        on_wheel_up: Optional[Callable[[], None]] = None,
        on_wheel_down: Optional[Callable[[], None]] = None,
        on_transition: Optional[Callable[[bool], None]] = None,
        width: int = 80,
        right_border: Optional[str] = None,
        commands: Optional[list[tuple[str, str, bool]]] = None,
        on_clear_dropdown: Optional[Callable[[], Any]] = None,
        out: Any = sys.stdout,
        bottom_border_renderer: Optional[Callable[[Optional[str]], str]] = None,
        on_alert: Optional[Callable[[Optional[str]], None]] = None,
        on_height_change: Optional[Callable[[Any], None]] = None,
        on_mouse_click: Optional[Callable[[int, int, int], None]] = None,
    ) -> None:
        self.out = out or sys.stdout
        self.prompt_prefix = prompt_prefix
        self.buffer: list[str] = list(initial_text)
        self.cursor: int = len(self.buffer)
        self.displayed_cursor: int = self.cursor
        self.history: list[str] = list(history) if history is not None else list(GLOBAL_HISTORY)
        self.history_index: int = len(self.history)
        self.temp_buffer: str = initial_text
        self.state = state
        self.on_ctrl_k = on_ctrl_k
        self.on_ctrl_l = on_ctrl_l
        self.on_ctrl_o = on_ctrl_o
        self.on_ctrl_b = on_ctrl_b
        self.on_ctrl_g = on_ctrl_g
        self.on_ctrl_w = on_ctrl_w
        self.on_alt_up = on_alt_up
        self.on_alt_down = on_alt_down
        self.on_alt_page_up = on_alt_page_up
        self.on_alt_page_down = on_alt_page_down
        self.last_mouse_col: Optional[int] = None
        self.last_mouse_row: Optional[int] = None
        self.on_page_up = on_page_up
        self.on_page_down = on_page_down
        self.on_wheel_up = on_wheel_up
        self.on_wheel_down = on_wheel_down
        self.on_mouse_click = on_mouse_click
        self.on_transition = on_transition
        self.on_height_change = on_height_change
        self.width = width
        self.has_borders = "│" in self.prompt_prefix
        if right_border is not None:
            self.right_border = right_border
        elif "\033[" in self.prompt_prefix:
            self.right_border = "\033[90m│\033[0m"
        else:
            self.right_border = "│"

        self.commands: list[tuple[str, str, bool]] = list(commands) if commands is not None else list(DEFAULT_COMMANDS)
        self.dropdown_active: bool = False
        self.dropdown_index: int = 0
        self.dropdown_matches: list[tuple[str, str, bool]] = []
        self._last_dropdown_lines_count: int = 0
        self._dropdown_dismissed: bool = False
        self.on_clear_dropdown = on_clear_dropdown

        self._ctrl_c_timestamp: Optional[float] = None
        self._ctrl_c_timeout: float = 3.0
        self._confirmed_exit: bool = False
        self.alert: Optional[str] = None
        self.bottom_border_renderer = bottom_border_renderer
        self.on_alert = on_alert
        self.attached_images: list[str] = (
            list(getattr(state, "attached_images", []))
            if state is not None and getattr(state, "attached_images", None)
            else []
        )

        self.in_bracketed_paste: bool = False
        self._last_was_cr: bool = False
        self._last_line_count: int = max(1, "".join(self.buffer).count("\n") + 1)

        self._update_state()
        self._update_dropdown()

    def paste_from_clipboard(self) -> bool:
        """Paste text from system clipboard directly into the composer buffer."""
        from cli.tui.clipboard import get_clipboard_text

        text = get_clipboard_text()
        if not text:
            return False

        for ch in text:
            if ch == "\r":
                self._last_was_cr = True
                self.insert_char("\n")
            elif ch == "\n":
                if self._last_was_cr:
                    self._last_was_cr = False
                    continue
                self._last_was_cr = False
                self.insert_char("\n")
            else:
                self._last_was_cr = False
                if ch.isprintable() or ch == "\t":
                    self.insert_char(ch)
        self._notify_height_change()
        self._update_state()
        return True

    def paste_image_from_clipboard(self) -> bool:
        """Paste image from system clipboard directly as [IMAGE<sequence_no>.<format>]."""
        from cli.tui.clipboard import get_clipboard_image

        result = get_clipboard_image()
        if not result:
            self.alert = "no image in clipboard"
            if self.state is not None and hasattr(self.state, "composer_alert"):
                self.state.composer_alert = self.alert
            if self.on_alert:
                try:
                    self.on_alert(self.alert)
                except Exception:
                    pass
            self.redraw_line()
            return False

        img_bytes, ext = result
        ext = ext.lower().lstrip(".") or "png"
        seq_num = len(self.attached_images) + 1
        tag = f"[IMAGE{seq_num}.{ext}]"

        try:
            workspace_dir = None
            if self.state is not None:
                workspace_dir = getattr(self.state, "workspace_dir", None) or getattr(self.state, "project_path", None)
            if not workspace_dir:
                workspace_dir = os.getcwd()
            from pathlib import Path
            from validators.ingest.store import AttachmentStore

            ws_path = Path(workspace_dir)
            store = AttachmentStore(ws_path)
            store.ingest_bytes(img_bytes, filename=f"IMAGE{seq_num}.{ext}")

            pasted_dir = ws_path / ".sync" / "attachments" / "pasted"
            pasted_dir.mkdir(parents=True, exist_ok=True)
            (pasted_dir / f"IMAGE{seq_num}.{ext}").write_bytes(img_bytes)
        except Exception:
            pass

        self.attached_images.append(tag)
        if self.state is not None:
            self.state.attached_images = list(self.attached_images)
        self.alert = None
        if self.state is not None and hasattr(self.state, "composer_alert"):
            self.state.composer_alert = None
        if self.on_alert:
            try:
                self.on_alert(None)
            except Exception:
                pass

        self._notify_height_change()
        self.redraw_line()
        return True

    @property
    def text(self) -> str:
        return "".join(self.buffer)

    def _get_wrap_width(self) -> int:
        visible_prefix_len = len(_ANSI_STRIP_RE.sub("", self.prompt_prefix))
        visible_right_len = len(_ANSI_STRIP_RE.sub("", self.right_border))
        inner_available = max(1, self.width - visible_prefix_len - visible_right_len)
        return max(1, min(inner_available, self.width - 6)) if self.has_borders else max(10, self.width - 6)

    def _get_visual_lines(self) -> tuple[list[str], list[int], int, int]:
        wrap_width = self._get_wrap_width()
        return wrap_composer_text_with_cursor(self.text, wrap_width, self.cursor)

    def _notify_height_change(self) -> None:
        v_lines, _, _, _ = self._get_visual_lines()
        lines_count = max(1, len(v_lines))
        if lines_count != self._last_line_count:
            self._last_line_count = lines_count
            if getattr(self, "on_height_change", None):
                try:
                    self.on_height_change(self)
                except Exception:
                    pass

    def _force_full_redraw(self, clear: bool = True) -> None:
        """Trigger a clean full-screen redraw when dialogs or panels close."""
        if getattr(self, "on_height_change", None):
            try:
                self.on_height_change(self, clear=clear)
                return
            except TypeError:
                try:
                    self.on_height_change(self)
                    return
                except Exception:
                    pass
            except Exception:
                pass
        if getattr(self, "on_clear_dropdown", None):
            try:
                self.on_clear_dropdown()
            except Exception:
                pass

    def _get_line_and_col(self, cursor: int | None = None) -> tuple[int, int, list[str]]:
        """Return (line_index, col_index, list_of_lines) for cursor in buffer."""
        cur = self.cursor if cursor is None else cursor
        wrap_width = self._get_wrap_width()
        v_lines, _, c_line, c_col = wrap_composer_text_with_cursor(self.text, wrap_width, cur)
        return c_line, c_col, v_lines

    @property
    def current_line_and_col(self) -> tuple[int, int]:
        l_idx, col, _ = self._get_line_and_col()
        return l_idx, col

    @property
    def current_visible_line_index(self) -> int:
        from cli.tui.layout import get_composer_line_limits

        l_idx, _, lines = self._get_line_and_col()
        total_lines = len(lines)
        max_box_h, max_input_lines = get_composer_line_limits()
        if total_lines > max_input_lines:
            window_start = total_lines - max_input_lines
            return max(0, l_idx - window_start)
        return l_idx

    def move_up(self) -> bool:
        """Move cursor up one visual line within buffer. Returns True if handled."""
        v_lines, line_starts, c_line, c_col = self._get_visual_lines()
        if c_line > 0:
            target_line = c_line - 1
            target_col = min(c_col, len(v_lines[target_line]))
            self.cursor = line_starts[target_line] + target_col
            return True
        return False

    def move_down(self) -> bool:
        """Move cursor down one visual line within buffer. Returns True if handled."""
        v_lines, line_starts, c_line, c_col = self._get_visual_lines()
        if c_line < len(v_lines) - 1:
            target_line = c_line + 1
            target_col = min(c_col, len(v_lines[target_line]))
            self.cursor = line_starts[target_line] + target_col
            return True
        return False

    def _update_state(self) -> None:
        if self.state is not None and hasattr(self.state, "composer_buffer"):
            self.state.composer_buffer = self.text

    def _update_dropdown(self) -> None:
        text = self.text
        if text.startswith(":") and " " not in text and not self._dropdown_dismissed:
            prefix = text.lower()
            self.dropdown_matches = [c for c in self.commands if c[0].lower().startswith(prefix)]
            if self.dropdown_matches:
                self.dropdown_active = True
                if self.dropdown_index >= len(self.dropdown_matches):
                    self.dropdown_index = 0
                return
        self.dropdown_active = False
        self.dropdown_matches = []
        self.dropdown_index = 0

    def _erase_dropdown(self, out: Any = None) -> None:
        target = out if out is not None else getattr(self, "out", sys.stdout)
        if self._last_dropdown_lines_count <= 0:
            return
        H = self._last_dropdown_lines_count
        self._last_dropdown_lines_count = 0
        cleared_via_callback = False
        if getattr(self, "on_clear_dropdown", None):
            try:
                res = self.on_clear_dropdown()
                cleared_via_callback = (res is True)
            except Exception:
                pass
        if not cleared_via_callback:
            offset_up = H + (1 if self.has_borders else 0)
            try:
                target.write(f"\x1b[{offset_up}A")
                for _ in range(H):
                    target.write("\r\x1b[2K\x1b[1B")
                down_steps = 1 if self.has_borders else 0
                if down_steps > 0:
                    target.write(f"\x1b[{down_steps}B")
                visible_prefix_len = len(_ANSI_STRIP_RE.sub("", self.prompt_prefix))
                cursor_col = visible_prefix_len + getattr(self, "displayed_cursor", self.cursor) + 1
                target.write(f"\x1b[{cursor_col}G")
                target.flush()
            except Exception:
                pass
        else:
            try:
                visible_prefix_len = len(_ANSI_STRIP_RE.sub("", self.prompt_prefix))
                cursor_col = visible_prefix_len + getattr(self, "displayed_cursor", self.cursor) + 1
                target.write(f"\x1b[{cursor_col}G")
                target.flush()
            except Exception:
                pass

    def insert_char(self, ch: str) -> None:
        was_empty = len(self.buffer) == 0
        self.buffer.insert(self.cursor, ch)
        self.cursor += 1
        self._update_state()
        self._dropdown_dismissed = False
        self._update_dropdown()
        if was_empty and self.on_transition:
            self.on_transition(True)
        self._notify_height_change()

    def delete_backspace(self) -> None:
        if self.cursor > 0:
            removed = self.buffer.pop(self.cursor - 1)
            self.cursor -= 1
            self._update_state()
            self._dropdown_dismissed = False
            self._update_dropdown()
            if len(self.buffer) == 0 and self.on_transition:
                self.on_transition(False)
            self._notify_height_change()
        elif self.cursor == 0 and not self.buffer and self.attached_images:
            self.attached_images.pop()
            if self.state is not None:
                self.state.attached_images = list(self.attached_images)
            self._notify_height_change()
            self.redraw_line()

    def delete_forward(self) -> None:
        if self.cursor < len(self.buffer):
            removed = self.buffer.pop(self.cursor)
            self._update_state()
            self._dropdown_dismissed = False
            self._update_dropdown()
            if len(self.buffer) == 0 and self.on_transition:
                self.on_transition(False)
            self._notify_height_change()

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
            self._dropdown_dismissed = False
            self._update_dropdown()
            if was_empty and len(self.buffer) > 0 and self.on_transition:
                self.on_transition(True)
            elif not was_empty and len(self.buffer) == 0 and self.on_transition:
                self.on_transition(False)
            self._notify_height_change()

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

        self._dropdown_dismissed = False
        self._update_dropdown()

        if was_empty and len(self.buffer) > 0 and self.on_transition:
            self.on_transition(True)
        elif not was_empty and len(self.buffer) == 0 and self.on_transition:
            self.on_transition(False)
        self._notify_height_change()

    def handle_key(self, key: str) -> bool:
        """Process a single key. Returns True if Enter was pressed, False otherwise."""
        if DEBUG_KEYS_ENABLED:
            _log_debug_key(f"[handle_key] received key={repr(key)}")

        if key == Key.CTRL_C:
            now = time.monotonic()
            if self._ctrl_c_timestamp is not None and (now - self._ctrl_c_timestamp) <= self._ctrl_c_timeout:
                self._ctrl_c_timestamp = None
                self.alert = None
                self._confirmed_exit = True
                if self.state is not None and hasattr(self.state, "composer_alert"):
                    self.state.composer_alert = None
                if self.on_alert:
                    try:
                        self.on_alert(None)
                    except Exception:
                        pass
                raise KeyboardInterrupt()
            else:
                self._ctrl_c_timestamp = now
                self.alert = "press ctrl + C again"
                self._confirmed_exit = False
                if self.state is not None and hasattr(self.state, "composer_alert"):
                    self.state.composer_alert = self.alert
                if self.on_alert:
                    try:
                        self.on_alert(self.alert)
                    except Exception:
                        pass
                return False

        if self.alert is not None:
            self.alert = None
            self._ctrl_c_timestamp = None
            self._confirmed_exit = False
            if self.state is not None and hasattr(self.state, "composer_alert"):
                self.state.composer_alert = None
            if self.on_alert:
                try:
                    self.on_alert(None)
                except Exception:
                    pass
            if self.bottom_border_renderer and self.has_borders:
                try:
                    border_str = self.bottom_border_renderer(None, self.attached_images)
                    target = getattr(self, "out", sys.stdout)
                    target.write(f"\x1b[s\x1b[1B\r{border_str}\x1b[u")
                    target.flush()
                except TypeError:
                    try:
                        border_str = self.bottom_border_renderer(None)
                        target = getattr(self, "out", sys.stdout)
                        target.write(f"\x1b[s\x1b[1B\r{border_str}\x1b[u")
                        target.flush()
                    except Exception:
                        pass
                except Exception:
                    pass

        if key == Key.CTRL_D:
            if not self.buffer:
                raise EOFError()
            self.delete_forward()
            return False

        if self.dropdown_active and self.dropdown_matches:
            if key == Key.DOWN:
                self.dropdown_index = (self.dropdown_index + 1) % len(self.dropdown_matches)
                return False
            if key == Key.UP:
                self.dropdown_index = (self.dropdown_index - 1) % len(self.dropdown_matches)
                return False
            if key in (Key.TAB, "\t"):
                cmd, desc, req_args = self.dropdown_matches[self.dropdown_index]
                self.buffer = list(cmd)
                if req_args:
                    self.buffer.append(" ")
                self.cursor = len(self.buffer)
                self._update_state()
                self.dropdown_active = False
                self._erase_dropdown()
                return False
            if key in (Key.ENTER, Key.NEWLINE):
                cmd, desc, req_args = self.dropdown_matches[self.dropdown_index]
                self.buffer = list(cmd)
                self.cursor = len(self.buffer)
                self._update_state()
                self.dropdown_active = False
                self._erase_dropdown()
                if req_args:
                    self.buffer.append(" ")
                    self.cursor += 1
                    self._update_state()
                    return False
                return True
            if key == Key.ESCAPE:
                self._dropdown_dismissed = True
                self.dropdown_active = False
                self._erase_dropdown()
                if self.state is not None and getattr(self.state, "command_dialog_open", False):
                    self.state.close_command_dialog()
                    self._force_full_redraw(clear=True)
                return False
            if key == Key.RIGHT and self.cursor == len(self.buffer):
                cmd, desc, req_args = self.dropdown_matches[self.dropdown_index]
                self.buffer = list(cmd)
                if req_args:
                    self.buffer.append(" ")
                self.cursor = len(self.buffer)
                self._update_state()
                self.dropdown_active = False
                self._erase_dropdown()
                return False

        if key == Key.PASTE_START:
            self.in_bracketed_paste = True
            return False

        if key == Key.PASTE_END:
            self.in_bracketed_paste = False
            self._notify_height_change()
            return False

        if key == Key.ALT_V:
            self.paste_image_from_clipboard()
            return False

        if key in (Key.SHIFT_ENTER, Key.ALT_ENTER, Key.CTRL_ENTER):
            self.insert_char("\n")
            return False

        if key == Key.ESCAPE:
            if self.state is not None and getattr(self.state, "command_dialog_open", False):
                self.state.close_command_dialog()
                self._force_full_redraw(clear=True)
                return False

        if key in (Key.ENTER, Key.NEWLINE):
            if not self.buffer and not self.in_bracketed_paste and self.state is not None and getattr(self.state, "command_dialog_open", False):
                self.state.close_command_dialog()
                self._force_full_redraw(clear=True)
                return False
            if self.in_bracketed_paste or has_pending_input():

                if key == "\r":
                    self._last_was_cr = True
                    self.insert_char("\n")
                elif key == "\n":
                    if self._last_was_cr:
                        self._last_was_cr = False
                        return False
                    self._last_was_cr = False
                    self.insert_char("\n")
                else:
                    self._last_was_cr = False
                    self.insert_char("\n")
                return False
            self._last_was_cr = False
            return True

        self._last_was_cr = False

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
            if self.move_up():
                self._update_state()
                self._update_dropdown()
                return False
            if has_pending_input():
                if self.on_page_up:
                    self.on_page_up()
                elif self.state is not None and hasattr(self.state, "scroll_conversation_up"):
                    self.state.scroll_conversation_up(3)
                return False
            self.history_prev()
            return False

        if key == Key.DOWN:
            if self.move_down():
                self._update_state()
                self._update_dropdown()
                return False
            if has_pending_input():
                if self.on_page_down:
                    self.on_page_down()
                elif self.state is not None and hasattr(self.state, "scroll_conversation_down"):
                    self.state.scroll_conversation_down(3)
                return False
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

        if key == Key.CTRL_O:
            if self.on_ctrl_o:
                try:
                    self.on_ctrl_o(self)
                except TypeError:
                    self.on_ctrl_o()
            elif self.state is not None:
                if hasattr(self.state, "toggle_disclosures"):
                    self.state.toggle_disclosures()
                else:
                    if hasattr(self.state, "toggle_thinking"):
                        self.state.toggle_thinking()
                    if hasattr(self.state, "toggle_actions"):
                        self.state.toggle_actions()
                try:
                    from cli.tui.chat import clear_transcript_cache

                    clear_transcript_cache()
                except Exception:
                    pass
                self._force_full_redraw(clear=False)
            return False

        if key == Key.CTRL_B:
            if self.on_ctrl_b:
                self.on_ctrl_b()
            elif self.state is not None and hasattr(self.state, "toggle_runtime_panel"):
                self.state.toggle_runtime_panel()
                self._force_full_redraw(clear=True)
            return False

        if key == Key.CTRL_G:
            if self.state is not None:
                goal = getattr(self.state, "get_latest_suggested_goal", lambda: None)()
                if goal:
                    self.buffer = list(f":goal {goal}")
                    self.cursor = len(self.buffer)
                    self._notify_height_change()
                    self._update_state()
                    return False
            if self.on_ctrl_g:
                self.on_ctrl_g()
            return False

        if key in (Key.CTRL_W, Key.ALT_UP, Key.ALT_DOWN, Key.ALT_PAGE_UP, Key.ALT_PAGE_DOWN):
            cb = {
                Key.CTRL_W: self.on_ctrl_w,
                Key.ALT_UP: self.on_alt_up,
                Key.ALT_DOWN: self.on_alt_down,
                Key.ALT_PAGE_UP: self.on_alt_page_up or self.on_alt_up,
                Key.ALT_PAGE_DOWN: self.on_alt_page_down or self.on_alt_down,
            }.get(key)
            if cb:
                try:
                    cb(self)
                except TypeError:
                    cb()
            elif self.state:
                if key == Key.CTRL_W:
                    getattr(self.state, "toggle_scroll_focus", lambda: None)()
                else:
                    fn = getattr(self.state, "scroll_runtime_up" if "UP" in key else "scroll_runtime_down", None)
                    if fn:
                        fn(6 if "PAGE" in key else 2)
                self._force_full_redraw(clear=False)
            return False

        if key == Key.PAGE_UP:
            if self.state is not None and getattr(self.state, "scroll_focus", "") == "work_orders":
                if hasattr(self.state, "scroll_runtime_up"):
                    self.state.scroll_runtime_up(4)
                self._force_full_redraw(clear=False)
                return False
            if self.state is not None and getattr(self.state, "command_dialog_open", False):
                self.state.scroll_command_dialog_up(5)
                self._force_full_redraw(clear=False)
                return False
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] MATCHED Key.PAGE_UP (has on_page_up={self.on_page_up is not None})")
            if self.on_page_up:
                self.on_page_up()
            return False

        if key == Key.PAGE_DOWN:
            if self.state is not None and getattr(self.state, "scroll_focus", "") == "work_orders":
                if hasattr(self.state, "scroll_runtime_down"):
                    self.state.scroll_runtime_down(4)
                self._force_full_redraw(clear=False)
                return False
            if self.state is not None and getattr(self.state, "command_dialog_open", False):
                self.state.scroll_command_dialog_down(5)
                self._force_full_redraw(clear=False)
                return False
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

        if key == Key.CTRL_V:
            self.paste_from_clipboard()
            return False

        if key.startswith("<MOUSE_"):
            parts = key.strip("<>").split("_")
            if len(parts) >= 4:
                try:
                    btn = int(parts[1])
                    col = int(parts[2])
                    row = int(parts[3])
                    self.last_mouse_col = col
                    self.last_mouse_row = row
                    action = parts[4] if len(parts) > 4 else "M"
                    if action not in ("m",):
                        # Right click (button 2 in SGR or flag 0x0002 in Win32) -> paste from clipboard
                        if btn == 2 or (btn & 0x0002 != 0):
                            self.paste_from_clipboard()
                            return False
                        if self.on_mouse_click:
                            self.on_mouse_click(col, row, btn)
                except Exception:
                    pass
            if DEBUG_KEYS_ENABLED:
                _log_debug_key(f"[handle_key] Handled or filtered mouse event: {key}")
            return False

        if len(key) > 1 and not key.startswith(("<", "\x1b")):
            for ch in key:
                if ch == "\r":
                    self._last_was_cr = True
                    self.insert_char("\n")
                elif ch == "\n":
                    if self._last_was_cr:
                        self._last_was_cr = False
                        continue
                    self._last_was_cr = False
                    self.insert_char("\n")
                else:
                    self._last_was_cr = False
                    if ch.isprintable() or ch == "\t":
                        self.insert_char(ch)
            self._notify_height_change()
            return False

        if key in (Key.TAB, "\t"):
            cur_text = "".join(self.buffer).strip()
            if (not cur_text or cur_text in (":goal", ":adopt", ":g")) and self.state is not None:
                goal = getattr(self.state, "get_latest_suggested_goal", lambda: None)()
                if goal:
                    self.buffer = list(f":goal {goal}")
                    self.cursor = len(self.buffer)
                    self._notify_height_change()
                    self._update_state()
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

    def redraw_line(self, out: Any = None) -> None:
        """Redraw current line with exact cursor placement and composer border preservation."""
        if getattr(self, "_in_redraw", False):
            return
        self._in_redraw = True
        try:
            self._do_redraw_line(out=out)
        finally:
            self._in_redraw = False

    def _do_redraw_line(self, out: Any = None) -> None:
        target = out if out is not None else getattr(self, "out", sys.stdout)
        buf_str = "".join(self.buffer)
        v_lines, _, c_line, c_col = self._get_visual_lines()
        self.displayed_cursor = c_col

        # If multiline or wrapped, trigger full-screen height handler if available
        if len(v_lines) > 1:
            if getattr(self, "on_height_change", None):
                try:
                    self.on_height_change(self)
                    return
                except Exception:
                    pass

            cur_line_text = v_lines[c_line]
            prefix = self.prompt_prefix if c_line == 0 else (
                "\033[90m│\033[0m   " if "\033[" in self.prompt_prefix else "│   "
            )
            visible_prefix_len = len(_ANSI_STRIP_RE.sub("", prefix))
            visible_right_len = len(_ANSI_STRIP_RE.sub("", self.right_border))
            inner_available = max(0, self.width - visible_prefix_len - visible_right_len)
            displayed = cur_line_text
            self.displayed_cursor = c_col
            spaces_count = max(0, inner_available - len(displayed))
            line_out = f"\r{prefix}{displayed}{' ' * spaces_count}{self.right_border}"
            target.write(line_out)
            cursor_col = visible_prefix_len + c_col + 1
            target.write(f"\x1b[{cursor_col}G")
            if getattr(self, "bottom_border_renderer", None) and getattr(self, "has_borders", False):
                try:
                    border_str = self.bottom_border_renderer(self.alert, self.attached_images)
                    target.write(f"\x1b[s\x1b[1B\r{border_str}\x1b[u")
                except TypeError:
                    try:
                        border_str = self.bottom_border_renderer(self.alert)
                        target.write(f"\x1b[s\x1b[1B\r{border_str}\x1b[u")
                    except Exception:
                        pass
                except Exception:
                    pass
            target.flush()
            return

        if not getattr(self, "has_borders", False):
            line_out = f"\r{self.prompt_prefix}{buf_str}\x1b[K"
            target.write(line_out)
            offset_back = len(self.buffer) - self.cursor
            if offset_back > 0:
                target.write(f"\x1b[{offset_back}D")
            target.flush()
            return

        visible_prefix_len = len(_ANSI_STRIP_RE.sub("", self.prompt_prefix))
        visible_right_len = len(_ANSI_STRIP_RE.sub("", self.right_border))
        inner_available = max(0, self.width - visible_prefix_len - visible_right_len)

        displayed = buf_str
        self.displayed_cursor = self.cursor

        spaces_count = max(0, inner_available - len(displayed))
        line_out = f"\r{self.prompt_prefix}{displayed}{' ' * spaces_count}{self.right_border}"
        target.write(line_out)

        # Move cursor to active edit position inside composer box
        cursor_col = visible_prefix_len + self.displayed_cursor + 1
        target.write(f"\x1b[{cursor_col}G")

        if getattr(self, "bottom_border_renderer", None) and getattr(self, "has_borders", False):
            try:
                border_str = self.bottom_border_renderer(self.alert, self.attached_images)
                target.write(f"\x1b[s\x1b[1B\r{border_str}\x1b[u")
            except TypeError:
                try:
                    border_str = self.bottom_border_renderer(self.alert)
                    target.write(f"\x1b[s\x1b[1B\r{border_str}\x1b[u")
                except Exception:
                    pass
            except Exception:
                pass

        # Dropdown management
        prev_H = self._last_dropdown_lines_count

        if not self.dropdown_active or not self.dropdown_matches:
            if prev_H > 0:
                self._erase_dropdown(target)
        else:
            dropdown_lines = format_dropdown_lines(
                self.dropdown_matches,
                self.dropdown_index,
                width=self.width,
            )
            if dropdown_lines:
                new_H = len(dropdown_lines)
                self._last_dropdown_lines_count = new_H
                try:
                    # If previous dropdown was taller, clear the excess top lines first
                    if prev_H > new_H:
                        offset_top = prev_H + (1 if self.has_borders else 0)
                        target.write(f"\x1b[{offset_top}A")
                        excess = prev_H - new_H
                        for _ in range(excess):
                            target.write("\r\x1b[2K\x1b[1B")
                    else:
                        offset_up = new_H + (1 if self.has_borders else 0)
                        target.write(f"\x1b[{offset_up}A")

                    for d_line in dropdown_lines:
                        target.write(f"\r{d_line}\x1b[K\x1b[1B")

                    down_steps = 1 if self.has_borders else 0
                    if down_steps > 0:
                        target.write(f"\x1b[{down_steps}B")
                    target.write(f"\x1b[{cursor_col}G")
                except Exception:
                    pass
            elif prev_H > 0:
                self._erase_dropdown(target)

        target.flush()


LineEditor = RawLineEditor


def raw_prompt_input(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    width: int = 80,
    initial_text: str = "",
    history: Optional[list[str]] = None,
    state: Optional[Any] = None,
    on_ctrl_k: Optional[Callable[[], None]] = None,
    on_ctrl_l: Optional[Callable[[], None]] = None,
    on_ctrl_o: Optional[Callable[[], None]] = None,
    on_ctrl_b: Optional[Callable[[], None]] = None,
    on_ctrl_g: Optional[Callable[[], None]] = None,
    on_ctrl_w: Optional[Callable[[], None]] = None,
    on_alt_up: Optional[Callable[[], None]] = None,
    on_alt_down: Optional[Callable[[], None]] = None,
    on_alt_page_up: Optional[Callable[[], None]] = None,
    on_alt_page_down: Optional[Callable[[], None]] = None,
    on_page_up: Optional[Callable[[], None]] = None,
    on_page_down: Optional[Callable[[], None]] = None,
    on_wheel_up: Optional[Callable[[], None]] = None,
    on_wheel_down: Optional[Callable[[], None]] = None,
    on_clear_dropdown: Optional[Callable[[], None]] = None,
    commands: Optional[list[tuple[str, str, bool]]] = None,
    key_stream: Optional[Iterator[str]] = None,
    top_border_renderer: Optional[Callable[[bool], str]] = None,
    bottom_border_renderer: Optional[Callable[[Optional[str]], str]] = None,
    on_alert: Optional[Callable[[Optional[str]], None]] = None,
    on_idle: Optional[Callable[[Any], None]] = None,
    on_height_change: Optional[Callable[[Any], None]] = None,
    on_resize: Optional[Callable[..., None]] = None,
    on_mouse_click: Optional[Callable[..., None]] = None,
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
        if on_ctrl_l:
            try:
                on_ctrl_l(editor)
            except TypeError:
                on_ctrl_l()
        else:
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()
            if top_border_renderer:
                sys.stdout.write(top_border_renderer(bool(editor.buffer)) + "\n")
        editor.redraw_line()

    def wrapped_ctrl_o() -> None:
        """Handle Ctrl+O — toggle thinking block, then restore prompt line."""
        if on_ctrl_o:
            try:
                on_ctrl_o(editor)
            except TypeError:
                on_ctrl_o()
        editor.redraw_line()

    def wrapped_ctrl_b() -> None:
        """Handle Ctrl+B — toggle runtime sidebar, then restore prompt line."""
        if on_ctrl_b:
            try:
                on_ctrl_b(editor)
            except TypeError:
                on_ctrl_b()
        elif state is not None and hasattr(state, "toggle_runtime_panel"):
            state.toggle_runtime_panel()
            editor._force_full_redraw(clear=True)
        editor.redraw_line()

    def wrapped_ctrl_g() -> None:
        """Handle Ctrl+G — adopt/auto-fill Architect's suggested goal."""
        if on_ctrl_g:
            try:
                on_ctrl_g(editor)
            except TypeError:
                on_ctrl_g()
        elif state is not None and hasattr(state, "get_latest_suggested_goal"):
            goal = state.get_latest_suggested_goal()
            if goal:
                editor.buffer = list(f":goal {goal}")
                editor.cursor = len(editor.buffer)
                editor._notify_height_change()
                editor._update_state()
        editor.redraw_line()

    def wrapped_page_up() -> None:
        """Handle Page Up — scroll conversation viewport up, then restore prompt line."""
        if on_page_up:
            try:
                on_page_up(editor)
            except TypeError:
                on_page_up()

    def wrapped_page_down() -> None:
        """Handle Page Down — scroll conversation viewport down, then restore prompt line."""
        if on_page_down:
            try:
                on_page_down(editor)
            except TypeError:
                on_page_down()

    def wrapped_wheel_up() -> None:
        """Handle Mouse Wheel Up — scroll conversation viewport up, then restore prompt line."""
        if on_wheel_up:
            try:
                on_wheel_up(editor)
            except TypeError:
                on_wheel_up()
        elif on_page_up:
            try:
                on_page_up(editor)
            except TypeError:
                on_page_up()

    def wrapped_wheel_down() -> None:
        """Handle Mouse Wheel Down — scroll conversation viewport down, then restore prompt line."""
        if on_wheel_down:
            try:
                on_wheel_down(editor)
            except TypeError:
                on_wheel_down()
        elif on_page_down:
            try:
                on_page_down(editor)
            except TypeError:
                on_page_down()

    def wrapped_mouse_click(col: int, row: int, button: int) -> None:
        """Handle mouse clicks on layout elements like the vertical scrollbar."""
        if on_mouse_click:
            try:
                on_mouse_click(col, row, button, editor)
            except TypeError:
                on_mouse_click(col, row, button)

    editor = RawLineEditor(
        prompt_prefix=prompt_prefix,
        initial_text=initial_text,
        history=history if history is not None else GLOBAL_HISTORY,
        state=state,
        on_ctrl_k=wrapped_ctrl_k,
        on_ctrl_l=wrapped_ctrl_l,
        on_ctrl_o=wrapped_ctrl_o,
        on_ctrl_b=wrapped_ctrl_b,
        on_ctrl_g=wrapped_ctrl_g,
        on_ctrl_w=on_ctrl_w,
        on_alt_up=on_alt_up,
        on_alt_down=on_alt_down,
        on_alt_page_up=on_alt_page_up,
        on_alt_page_down=on_alt_page_down,
        on_page_up=wrapped_page_up,
        on_page_down=wrapped_page_down,
        on_wheel_up=wrapped_wheel_up,
        on_wheel_down=wrapped_wheel_down,
        on_mouse_click=wrapped_mouse_click,
        on_transition=handle_transition,
        width=width,
        commands=commands,
        on_clear_dropdown=on_clear_dropdown,
        bottom_border_renderer=bottom_border_renderer,
        on_alert=on_alert,
        on_height_change=on_height_change,
    )

    try:
        with TerminalStateRestorer(state=state):
            editor.redraw_line()
            last_term_size = shutil.get_terminal_size(fallback=(width, 24))
            while True:
                try:
                    read_timeout = 0.05 if key_stream is None else None
                    k = read_next_key(key_stream=key_stream, timeout=read_timeout)

                    term_size = shutil.get_terminal_size(fallback=(width, 24))
                    if (term_size.columns, term_size.lines) != (last_term_size.columns, last_term_size.lines):
                        last_term_size = term_size
                        if on_resize is not None:
                            try:
                                on_resize(term_size.columns, term_size.lines, editor)
                            except TypeError:
                                try:
                                    on_resize(term_size.columns, term_size.lines)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        else:
                            editor.width = term_size.columns
                            editor.redraw_line()

                    if k is None:
                        if editor.alert is not None and editor._ctrl_c_timestamp is not None:
                            if (time.monotonic() - editor._ctrl_c_timestamp) > editor._ctrl_c_timeout:
                                editor.alert = None
                                editor._ctrl_c_timestamp = None
                                if editor.state is not None and hasattr(editor.state, "composer_alert"):
                                    editor.state.composer_alert = None
                                if editor.on_alert:
                                    try:
                                        editor.on_alert(None)
                                    except Exception:
                                        pass
                                editor.redraw_line()
                        if on_idle:
                            try:
                                on_idle(editor)
                            except Exception:
                                pass
                        continue
                    done = editor.handle_key(k)
                    if done:
                        if editor._last_dropdown_lines_count > 0:
                            editor._erase_dropdown()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        break
                    editor.redraw_line()
                except KeyboardInterrupt:
                    if getattr(editor, "_confirmed_exit", False):
                        if editor._last_dropdown_lines_count > 0:
                            editor._erase_dropdown()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise
                    try:
                        editor.handle_key(Key.CTRL_C)
                        editor.redraw_line()
                        continue
                    except KeyboardInterrupt:
                        if editor._last_dropdown_lines_count > 0:
                            editor._erase_dropdown()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise
                except EOFError:
                    if editor._last_dropdown_lines_count > 0:
                        editor._erase_dropdown()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise
    finally:
        if state is not None:
            state.is_typing = False
            state.composer_buffer = ""
            if hasattr(state, "composer_alert"):
                state.composer_alert = None

    result = editor.text
    if editor.attached_images:
        for tag in editor.attached_images:
            if tag not in result:
                result = f"{result}\n{tag}".strip() if result.strip() else tag
        editor.attached_images.clear()
        if state is not None and hasattr(state, "attached_images"):
            state.attached_images = []
    if result.strip():
        GLOBAL_HISTORY.append(result)
        if history is not None and result not in history:
            history.append(result)

    return result


def poll_scroll_key() -> str | None:
    """Non-blocking poll for scroll keys (PgUp/PgDn/WheelUp/WheelDown).

    Returns Key.PAGE_UP, Key.PAGE_DOWN, Key.MOUSE_WHEEL_UP, Key.MOUSE_WHEEL_DOWN,
    or None if no scroll key is pending. Consumes non-scroll keys silently.
    Used during SSE streaming to allow users to scroll the viewport.
    """
    SCROLL_KEYS = {Key.PAGE_UP, Key.PAGE_DOWN, Key.MOUSE_WHEEL_UP, Key.MOUSE_WHEEL_DOWN}
    if os.name == "nt":
        import msvcrt
        if not msvcrt.kbhit():
            return None
        key = _read_raw_key_windows(timeout=0.02)
        if key in SCROLL_KEYS:
            return key
        return None
    else:
        import select
        fd = sys.stdin.fileno()
        r, _, _ = select.select([fd], [], [], 0.0)
        if not r:
            return None
        ch = os.read(fd, 1).decode("utf-8", errors="replace")
        if ch == "\x1b":
            seq = _read_escape_sequence_posix(fd)
            if seq:
                key = _parse_escape_sequence(seq)
                if key in SCROLL_KEYS:
                    return key
        return None
