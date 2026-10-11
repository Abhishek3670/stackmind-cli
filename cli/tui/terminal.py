"""Terminal Chrome, Screen Buffers, Input, and Resize Controls for StackMind TUI.

Contains terminal lifecycle management (alternate screen, mouse reporting,
VT processing, cleanup), responsive header/footer bars, composer input box
rendering and raw interactive input capture.
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

import click
from rich import box
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from cli.tui.chat import _make_capture_console, should_render_ansi
from cli.tui.events import render_connection_status_str
from cli.tui.glyphs import status_glyph
from cli.tui.keyboard import (
    _ANSI_STRIP_RE,
    DISABLE_MOUSE_REPORTING_SEQ,
    ENABLE_MOUSE_REPORTING_SEQ,
    raw_prompt_input,
)
from cli.tui.layout import (
    compute_layout,
    format_responsive_path,
    render_full_screen_workspace,
)


def _get_version() -> str:
    try:
        from cli import __version__

        return f"v{__version__}"
    except Exception:
        return "v3.1.0"


def format_session_header(
    session: Mapping[str, Any],
    width: int = 80,
    status: str | None = None,
) -> str:
    """Format session header responsively depending on terminal width."""
    sid = str(session.get("session_id", "session"))
    state_val = str(session.get("state", "RUNNING"))
    provider = str(session.get("provider", "daemon"))
    agent = session.get("agent")
    workspace = session.get("workspace")

    status_suffix = f" | {render_connection_status_str(status)}" if status else ""
    phase_val = None
    if "run" in session and isinstance(session["run"], Mapping):
        phase_val = session["run"].get("phase")
    if not phase_val and "phase" in session:
        phase_val = session.get("phase")
    phase_info = f" | phase: {phase_val}" if phase_val else ""

    if width >= 80:
        agent_info = f" | agent: {agent}" if agent else ""
        ws_info = f" | workspace: {workspace}" if workspace else ""
        return f"Session {sid} | {state_val} | provider: {provider}{agent_info}{ws_info}{phase_info}{status_suffix}"
    elif width >= 55:
        ws_short = Path(str(workspace)).name if workspace else ""
        agent_info = f" | {agent}" if agent else ""
        ws_info = f" | ws: {ws_short}" if ws_short else ""
        display_sid = sid if len(sid) <= 12 else sid[:8] + "..."
        return (
            f"Session {display_sid} | {state_val}{phase_info}{agent_info}{ws_info}{status_suffix}"
        )
    else:
        display_sid = sid if len(sid) <= 8 else sid[:6] + ".."
        return f"Session {display_sid} | {state_val}{phase_info}{status_suffix}"


def render_top_header_bar(
    session: Mapping[str, Any],
    width: int = 80,
    status: str | None = "online",
    context_meter: tuple[str, str] | None = None,
) -> RenderableType:
    """Render a single-line, width-safe top status bar.

    Progressively compacts / drops fields as width becomes constrained:
    session id and connection status are retained; optional provider/context
    metadata yields first.
    """
    sid = str(session.get("session_id", "session"))
    short_sid = sid if len(sid) <= 8 else sid[:8]
    agent = str(session.get("agent") or "claude")
    provider = str(session.get("provider") or "daemon")
    workspace = session.get("workspace")

    st = str(status or "online").lower()
    if st == "online":
        glyph, glyph_style, st_name = status_glyph("online"), "bold #22c55e", "online"
    elif st in {"reconnecting", "reconnect"}:
        glyph, glyph_style, st_name = status_glyph("waiting"), "bold yellow", "reconnecting"
    else:
        glyph, glyph_style, st_name = status_glyph("failed"), "bold red", "offline"

    status_item = f"{glyph} {st_name}"

    def context_item(compact: bool = False) -> str:
        if not context_meter:
            return ""
        value, _level = context_meter
        if compact:
            value = (
                value.replace("Context: ", "ctx ")
                .replace(" / ", "/")
                .replace(" (", " ")
                .replace(")", "")
            )
        return value

    right_items: list[str] = []
    if width >= 90:
        right_items.extend([f"session: {short_sid}", f"agent: {agent}", f"provider: {provider}"])
    elif width >= 65:
        right_items.extend([f"session: {short_sid}", f"agent: {agent}"])
    elif width >= 45:
        right_items.append(f"session: {short_sid}")

    if context_meter and width >= 80:
        ctx = context_item(compact=width < 118)
        if ctx:
            right_items.append(ctx)

    def right_text(items: list[str]) -> str:
        return " | ".join(items + [status_item]) if items else status_item

    left_prefix = "stackmind  |  dev  "
    if workspace:
        p = Path(str(workspace))
        ws_display = str(p) if width >= 90 else f".../{p.name}"
    else:
        p = None
        ws_display = "~/projects/stackmind"

    def fit_left(items: list[str]) -> tuple[str, str]:
        """Return a workspace label that fits alongside right-side metadata."""
        right_plain = right_text(items)
        available = width - len(left_prefix) - len(right_plain) - 2
        if available <= 0:
            return "", right_plain

        return format_responsive_path(ws_display, available), right_plain

    candidates = list(right_items)
    ws_fit, right_plain = fit_left(candidates)

    if len(left_prefix) + len(ws_fit) + len(right_plain) + 2 > width:
        candidates = [
            item
            for item in candidates
            if item != context_item(compact=True) and item != context_item(compact=False)
        ]
        ws_fit, right_plain = fit_left(candidates)

    if len(left_prefix) + len(ws_fit) + len(right_plain) + 2 > width and width < 118:
        candidates = [item for item in candidates if not item.startswith("provider: ")]
        ws_fit, right_plain = fit_left(candidates)

    if len(left_prefix) + len(ws_fit) + len(right_plain) + 2 > width:
        candidates = [item for item in candidates if not item.startswith("agent: ")]
        ws_fit, right_plain = fit_left(candidates)

    left_plain = left_prefix + ws_fit
    spaces_count = max(1, width - len(left_plain) - len(right_plain))

    res = Text(no_wrap=True, overflow="crop")
    res.append("stackmind", style="bold #22c55e")
    res.append("  |  ", style="dim #475569")
    res.append("dev  ", style="dim white")
    res.append(ws_fit, style="dim #64748b")
    res.append(" " * spaces_count)

    for item in candidates:
        if context_meter and item in {context_item(True), context_item(False)}:
            level = context_meter[1]
            context_style = {
                "green": "bold #22c55e",
                "yellow": "bold yellow",
                "red": "bold red",
            }.get(level, "dim white")
            res.append(item, style=context_style)
        else:
            res.append(item, style="dim white")
        res.append(" | ", style="dim #475569")
    res.append(status_item, style=glyph_style)

    return res


def render_top_header_bar_str(
    session: Mapping[str, Any],
    width: int = 80,
    status: str | None = "online",
    context_meter: tuple[str, str] | None = None,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render top header bar as plain or ANSI-styled string using in-memory capture."""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(
        render_top_header_bar(session, width=width, status=status, context_meter=context_meter)
    )
    return console.export_text(styles=use_ansi).rstrip()


def render_composer_box(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
    width: int = 80,
    content: str | list[str] | None = None,
    is_active: bool = False,
    has_content: bool = False,
) -> Panel:
    """Render the rounded input composer box."""
    inner_width = max(10, width - 4)
    border_color = "#60a5fa" if is_active else "#475569"

    shortcut_candidates = [
        shortcuts,
        "Ctrl+K commands | Ctrl+L clear",
        "Ctrl+K | Ctrl+L | PgUp/PgDn",
        "Ctrl+K | Ctrl+L",
    ]
    seen = set()
    shortcut_candidates = [
        sc for sc in shortcut_candidates if sc and not (sc in seen or seen.add(sc))
    ]

    if not content and not has_content:
        if is_active:
            line = Text(no_wrap=True)
            line.append("> ", style="bold #38bdf8")
            body: RenderableType = line
        else:
            max_p_len = max(0, inner_width - 2)
            display_placeholder = placeholder
            if len(display_placeholder) > max_p_len:
                display_placeholder = display_placeholder[: max(0, max_p_len - 3)] + "..."
            left_plain = f"> {display_placeholder}"

            available_shortcuts_width = inner_width - len(left_plain) - 2
            right_plain = ""
            for sc in shortcut_candidates:
                if available_shortcuts_width >= len(sc):
                    right_plain = sc
                    break

            spaces_count = (
                max(2, inner_width - len(left_plain) - len(right_plain)) if right_plain else 0
            )

            line = Text(no_wrap=True)
            line.append("> ", style="bold #38bdf8")
            line.append(display_placeholder, style="dim #94a3b8")
            if right_plain:
                line.append(" " * spaces_count)
                line.append(right_plain, style="dim #64748b")
            body = line
    else:
        if isinstance(content, str):
            lines = content.splitlines() or [""]
        elif content:
            lines = list(content) or [""]
        else:
            lines = [""]

        rendered_lines: list[Text] = []
        first_text = lines[0]

        max_text_width = max(0, inner_width - 2)

        left_plain = f"> {first_text}"
        available_shortcuts_width = inner_width - len(left_plain) - 2
        right_plain = ""
        for sc in shortcut_candidates:
            if available_shortcuts_width >= len(sc):
                right_plain = sc
                break

        if len(first_text) > max_text_width:
            first_text = first_text[: max(0, max_text_width - 3)] + "..."
            left_plain = f"> {first_text}"
            right_plain = ""

        spaces_count = (
            max(2, inner_width - len(left_plain) - len(right_plain)) if right_plain else 0
        )

        line1 = Text(no_wrap=True)
        line1.append("> ", style="bold #38bdf8")
        line1.append(first_text, style="bold white")
        if right_plain:
            line1.append(" " * spaces_count)
            line1.append(right_plain, style="dim #64748b")
        rendered_lines.append(line1)

        for sub_line in lines[1:]:
            if len(sub_line) > max_text_width:
                sub_line = sub_line[: max(0, max_text_width - 3)] + "..."
            line_n = Text()
            line_n.append("  ", style="dim #38bdf8")
            line_n.append(sub_line, style="white")
            rendered_lines.append(line_n)

        body = Group(*rendered_lines)

    return Panel(
        body,
        box=box.ROUNDED,
        border_style=border_color,
        padding=(0, 1),
    )


def render_composer_box_str(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
    width: int = 80,
    content: str | list[str] | None = None,
    is_active: bool = False,
    has_content: bool = False,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render input composer box as plain or ANSI-styled string using in-memory capture."""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(
        render_composer_box(
            placeholder=placeholder,
            shortcuts=shortcuts,
            width=width,
            content=content,
            is_active=is_active,
            has_content=has_content,
        )
    )
    return console.export_text(styles=use_ansi).rstrip()


def _compute_cursor_col(editor: Any, default: int = 5) -> int:
    """Compute the visible cursor column for the composer prompt line."""
    if editor is None:
        return default
    cursor = getattr(editor, "displayed_cursor", None)
    if not isinstance(cursor, int):
        cursor = getattr(editor, "cursor", None)
    prefix = getattr(editor, "prompt_prefix", None)
    if not isinstance(cursor, int) or not isinstance(prefix, str):
        return default
    v_prefix_len = len(_ANSI_STRIP_RE.sub("", prefix))
    return v_prefix_len + cursor + 1


def restore_composer_focus(state: Any | None = None) -> bool:
    """Restore terminal focus to the composer after an operation completes."""
    if state is None:
        return True
    if getattr(state, "is_typing", False):
        return False
    state.focus_target = "composer"
    return True


def preserve_composer_buffer(state: Any | None, text: str) -> None:
    """Preserve partially typed composer buffer during live events or renders."""
    if state is not None and hasattr(state, "composer_buffer"):
        state.composer_buffer = text


def enter_alternate_screen(stream: Any = None) -> None:
    """Enter alternate screen buffer, clear screen, and home cursor (WO-053)."""
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write("\x1b[?1049h\x1b[2J\x1b[H")
            target.flush()
    except Exception:
        pass


def enable_mouse_reporting(stream: Any = None) -> None:
    """Enable terminal SGR mouse reporting (WO-054)."""
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write(ENABLE_MOUSE_REPORTING_SEQ)
            target.flush()
    except Exception:
        pass


def disable_mouse_reporting(stream: Any = None) -> None:
    """Disable terminal SGR mouse reporting (WO-054)."""
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write(DISABLE_MOUSE_REPORTING_SEQ)
            target.flush()
    except Exception:
        pass


def exit_alternate_screen(stream: Any = None) -> None:
    """Exit alternate screen buffer and disable mouse reporting (WO-053)."""
    disable_mouse_reporting(stream)
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write("\x1b[?1049l")
            target.flush()
    except Exception:
        pass


def restore_terminal_state(stream: Any = None) -> None:
    """Restore terminal cursor, mouse modes, and attributes cleanly on exit."""
    try:
        Console().show_cursor(True)
    except Exception:
        pass
    disable_mouse_reporting(stream)
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write("\x1b[?1049l\x1b[?25h\x1b[0m")
            target.flush()
    except Exception:
        pass

    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.WinDLL("kernel32", use_last_error=True)
            for h in (k.GetStdHandle(-10), None):
                target_h = h
                if target_h is None:
                    try:
                        target_h = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
                    except Exception:
                        target_h = 0
                if target_h not in (0, -1):
                    m = ctypes.c_uint32()
                    if k.GetConsoleMode(target_h, ctypes.byref(m)):
                        k.SetConsoleMode(target_h, m.value | 0x0040 | 0x0080)
        except Exception:
            pass


_terminal_resized: bool = False


def _sigwinch_handler(signum: int, frame: Any) -> None:
    global _terminal_resized
    _terminal_resized = True


def install_resize_handler() -> Any:
    """Install safe SIGWINCH handler on platforms that support it (POSIX), no-op on Windows."""
    if hasattr(signal, "SIGWINCH"):
        try:
            return signal.signal(signal.SIGWINCH, _sigwinch_handler)
        except Exception:
            return None
    return None


def check_terminal_resize(current_cols: int, current_lines: int) -> tuple[bool, int, int]:
    """Check if terminal dimensions have changed either via SIGWINCH or periodic dimension check."""
    global _terminal_resized
    app_resized = False
    app_mod = sys.modules.get("cli.tui.app")
    if app_mod is not None:
        app_resized = getattr(app_mod, "_terminal_resized", False)
        if app_resized:
            app_mod._terminal_resized = False
    term_size = shutil.get_terminal_size(fallback=(80, 24))
    cols = term_size.columns
    lines = term_size.lines
    resized = _terminal_resized or app_resized or (cols != current_cols or lines != current_lines)
    _terminal_resized = False
    return resized, cols, lines


def _wrap_text_to_width(text: str, width: int) -> str:
    """Pre-wrap raw text to width columns matching settled Rich Markdown messages."""
    use_ansi = should_render_ansi()
    console, _ = _make_capture_console(width=width)
    console.print(Markdown(text, code_theme="monokai"), end="")
    return console.export_text(styles=use_ansi).rstrip("\n")


def redraw_full_screen(
    session: Mapping[str, Any] | None = None,
    state: Any | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
    clear: bool = False,
    stream: Any = None,
    conversation_content: str | RenderableType | None = None,
    composer_content: str | None = None,
    composer_is_active: bool = False,
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
    include_composer: bool = True,
    live_manager: Any | None = None,
    force_color: bool | None = None,
    no_color: bool | None = None,
    composer_placeholder: str = "Type a message...",
) -> str:
    """Redraw the full-screen TUI workspace."""
    term_size = shutil.get_terminal_size(fallback=(80, 24))
    cols = width if width is not None else term_size.columns
    lines = height if height is not None else term_size.lines

    if composer_content is None and state is not None and getattr(state, "composer_buffer", ""):
        composer_content = state.composer_buffer

    frame = render_full_screen_workspace(
        session=session,
        state=state,
        conversation_content=conversation_content,
        width=cols,
        height=lines,
        composer_content=composer_content,
        composer_is_active=composer_is_active,
        shortcuts=shortcuts,
        include_composer=include_composer,
        force_color=force_color,
        no_color=no_color,
        composer_placeholder=composer_placeholder,
    )

    if live_manager is not None and getattr(live_manager, "is_active", False):
        try:
            live_manager.update(
                conversation_content=conversation_content,
                composer_content=composer_content,
                composer_is_active=composer_is_active,
                shortcuts=shortcuts,
                include_composer=include_composer,
                force_color=force_color,
                no_color=no_color,
                composer_placeholder=composer_placeholder,
            )
        except Exception:
            pass
        return frame

    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            prefix = "\x1b[2J\x1b[H" if clear else "\x1b[H"
            target.write(f"{prefix}{frame}\n" if not include_composer else f"{prefix}{frame}")
            target.flush()
    except Exception:
        pass

    return frame


def render_composer_top_border(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
    width: int = 80,
    is_active: bool = False,
    has_content: bool = False,
    content: str | list[str] | None = None,
) -> RenderableType:
    """Render top border of composer box with placeholder and shortcuts."""
    border_color = "#60a5fa" if is_active else "#475569"
    active_content = has_content or bool(content)

    if active_content:
        label = "[Active Input]" if placeholder == "Type a message..." else placeholder
    else:
        label = placeholder

    shortcut_candidates = [
        shortcuts,
        "Ctrl+K commands | Ctrl+L clear",
        "Ctrl+K | Ctrl+L | PgUp/PgDn",
        "Ctrl+K | Ctrl+L",
    ]
    seen = set()
    shortcut_candidates = [
        sc for sc in shortcut_candidates if sc and not (sc in seen or seen.add(sc))
    ]

    if label:
        chosen_sc = None
        for sc in shortcut_candidates:
            fixed_len = len("╭─ ") + len(label) + len(" ") + len(" ") + len(sc) + len(" ─╮")
            if width >= fixed_len + 2:
                chosen_sc = sc
                break

        if chosen_sc is not None:
            fixed_len = len("╭─ ") + len(label) + len(" ") + len(" ") + len(chosen_sc) + len(" ─╮")
            fill_count = width - fixed_len
            text = Text()
            text.append("╭─ ", style=border_color)
            text.append(label, style="bold #38bdf8" if active_content else "dim #94a3b8")
            text.append(" " + "─" * fill_count + " ", style=border_color)
            text.append(chosen_sc, style="dim #64748b")
            text.append(" ─╮", style=border_color)
            return text

        fixed_len_label_only = len("╭─ ") + len(label) + len(" ") + len("─╮")
        if width >= fixed_len_label_only + 2:
            fill_count = width - fixed_len_label_only
            text = Text()
            text.append("╭─ ", style=border_color)
            text.append(label, style="bold #38bdf8" if active_content else "dim #94a3b8")
            text.append(" " + "─" * fill_count + "─╮", style=border_color)
            return text

        if width >= 12:
            max_label = width - 8
            truncated_label = label[: max(0, max_label - 3)] + "..."
            fixed_len_trunc = len("╭─ ") + len(truncated_label) + len(" ") + len("─╮")
            fill_count = width - fixed_len_trunc
            text = Text()
            text.append("╭─ ", style=border_color)
            text.append(truncated_label, style="bold #38bdf8" if active_content else "dim #94a3b8")
            text.append(" " + "─" * fill_count + "─╮", style=border_color)
            return text
    else:
        for sc in shortcut_candidates:
            fixed_len = len("╭─") + len(" ") + len(sc) + len(" ─╮")
            if width >= fixed_len + 2:
                fill_count = width - fixed_len
                text = Text()
                text.append("╭─" + "─" * fill_count + " ", style=border_color)
                text.append(sc, style="dim #64748b")
                text.append(" ─╮", style=border_color)
                return text

    text = Text()
    text.append("╭" + "─" * max(2, width - 2) + "╮", style=border_color)
    return text


def render_composer_top_border_str(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
    width: int = 80,
    is_active: bool = False,
    has_content: bool = False,
    content: str | list[str] | None = None,
) -> str:
    """Render top border of composer box as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(
        render_composer_top_border(
            placeholder=placeholder,
            shortcuts=shortcuts,
            width=width,
            is_active=is_active,
            has_content=has_content,
            content=content,
        )
    )
    return console.export_text().rstrip()


def render_composer_bottom_border(width: int = 80, is_active: bool = False) -> RenderableType:
    """Render the bottom rounded border of the composer box."""
    border_color = "#60a5fa" if is_active else "#475569"
    text = Text()
    text.append("╰" + "─" * max(2, width - 2) + "╯", style=border_color)
    return text


def render_composer_bottom_border_str(width: int = 80, is_active: bool = False) -> str:
    """Render bottom border of composer box as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_composer_bottom_border(width=width, is_active=is_active))
    return console.export_text().rstrip()


def prompt_composer_input(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
    width: int = 80,
    initial_text: str = "",
    multiline: bool = False,
    state: Any | None = None,
    on_ctrl_k: Callable[[], None] | None = None,
    on_ctrl_l: Callable[[], None] | None = None,
    on_page_up: Callable[[], None] | None = None,
    on_page_down: Callable[[], None] | None = None,
    on_wheel_up: Callable[[], None] | None = None,
    on_wheel_down: Callable[[], None] | None = None,
    history: list[str] | None = None,
    show_footer: bool = True,
    live_manager: Any | None = None,
    session: Mapping[str, Any] | None = None,
    terminal_width: int | None = None,
    full_screen: bool = False,
    key_stream: Optional[Iterator[str]] = None,
    on_idle: Optional[Callable[[Any], None]] = None,
) -> str:
    """Prompt user for input inside styled composer box border."""
    term_width = terminal_width if terminal_width is not None else width
    layout = compute_layout(term_width)
    if width != term_width:
        comp_width = width
    else:
        comp_width = layout.conversation_width if layout.show_runtime else term_width

    if state is not None:
        if not initial_text and getattr(state, "composer_buffer", ""):
            initial_text = state.composer_buffer
        state.is_typing = True
        state.focus_target = "composer"

    is_live_active = live_manager is not None and getattr(live_manager, "is_active", False)
    is_full_screen = full_screen or getattr(state, "full_screen", False)

    is_tty = False
    try:
        is_tty = sys.stdin.isatty() or (key_stream is not None)
    except Exception:
        is_tty = key_stream is not None

    if is_tty:
        if is_live_active:
            live_manager.update(
                composer_content=initial_text,
                composer_is_active=True,
                shortcuts=shortcuts,
                include_composer=True,
            )
        elif not is_full_screen:
            click.echo(
                render_composer_top_border_str(
                    placeholder=placeholder,
                    shortcuts=shortcuts,
                    width=comp_width,
                    is_active=True,
                    has_content=bool(initial_text),
                )
            )
            status_bar_line = ""
            if session is not None:
                status = (
                    getattr(state, "connection_status", "online") if state is not None else "online"
                )
                context_meter = (
                    (
                        getattr(state, "context_meter_text", None),
                        getattr(state, "context_warning_level", None),
                    )
                    if state is not None and hasattr(state, "context_meter_text")
                    else None
                )
                status_bar_line = render_top_header_bar_str(
                    session, width=comp_width, status=status, context_meter=context_meter
                )

            bottom_border = render_composer_bottom_border_str(width=comp_width, is_active=True)
            if status_bar_line:
                sys.stdout.write(f"\n{bottom_border}\n{status_bar_line}\x1b[2A\r")
            else:
                sys.stdout.write(f"\n{bottom_border}\x1b[1A\r")
            sys.stdout.flush()

        def _clear_dropdown() -> None:
            if is_full_screen and session is not None and state is not None:
                try:
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=True,
                        composer_content=getattr(state, "composer_buffer", ""),
                        live_manager=live_manager,
                    )
                except Exception:
                    pass

        try:
            _raw_prompt_fn = getattr(
                sys.modules.get("cli.tui.app"), "raw_prompt_input", raw_prompt_input
            )
            val = _raw_prompt_fn(
                placeholder=placeholder,
                shortcuts=shortcuts,
                width=comp_width,
                initial_text=initial_text,
                history=history,
                state=state,
                on_ctrl_k=on_ctrl_k,
                on_ctrl_l=on_ctrl_l,
                on_page_up=on_page_up,
                on_page_down=on_page_down,
                on_wheel_up=on_wheel_up,
                on_wheel_down=on_wheel_down,
                on_clear_dropdown=_clear_dropdown,
                key_stream=key_stream,
                top_border_renderer=lambda has_c: render_composer_top_border_str(
                    placeholder=placeholder,
                    shortcuts=shortcuts,
                    width=comp_width,
                    is_active=True,
                    has_content=has_c,
                ),
                on_idle=on_idle,
            )
        finally:
            if state is not None:
                state.is_typing = False
                state.composer_buffer = ""

        if is_live_active:
            live_manager.update(
                composer_content=val,
                composer_is_active=False,
                shortcuts=shortcuts,
                include_composer=False,
            )
        elif not is_full_screen:
            click.echo(render_composer_bottom_border_str(width=comp_width, is_active=False))
            if show_footer:
                click.echo(render_bottom_footer_bar_str(width=comp_width))
            if session is not None:
                status = (
                    getattr(state, "connection_status", "online") if state is not None else "online"
                )
                context_meter = (
                    (
                        getattr(state, "context_meter_text", None),
                        getattr(state, "context_warning_level", None),
                    )
                    if state is not None and hasattr(state, "context_meter_text")
                    else None
                )
                click.echo(
                    render_top_header_bar_str(
                        session, width=comp_width, status=status, context_meter=context_meter
                    )
                )
        return val

    # Non-TTY / test fallback: standard input() with multiline support
    if is_live_active:
        live_manager.update(
            composer_content=initial_text,
            composer_is_active=True,
            shortcuts=shortcuts,
            include_composer=True,
        )
    else:
        click.echo(
            render_composer_top_border_str(
                placeholder=placeholder,
                shortcuts=shortcuts,
                width=comp_width,
                is_active=True,
                has_content=bool(initial_text),
            )
        )
    prompt_str = "│ > "
    cont_prompt_str = "│   "

    lines: list[str] = []
    try:
        first_line = input(prompt_str)
        if initial_text and not first_line.startswith(initial_text):
            first_line = initial_text + first_line
        lines.append(first_line)

        if multiline:
            while True:
                next_line = input(cont_prompt_str)
                if not next_line.strip():
                    break
                lines.append(next_line)
        elif first_line.endswith("\\"):
            lines[0] = first_line[:-1]
            while True:
                next_line = input(cont_prompt_str)
                if next_line.endswith("\\"):
                    lines.append(next_line[:-1])
                else:
                    lines.append(next_line)
                    break
    finally:
        if state is not None:
            state.is_typing = False
            state.composer_buffer = ""

    if is_live_active:
        live_manager.update(
            composer_content="\n".join(lines),
            composer_is_active=False,
            shortcuts=shortcuts,
            include_composer=False,
        )
    else:
        click.echo(render_composer_bottom_border_str(width=comp_width, is_active=False))
        if show_footer:
            click.echo(render_bottom_footer_bar_str(width=comp_width))
        if session is not None:
            status = (
                getattr(state, "connection_status", "online") if state is not None else "online"
            )
            context_meter = (
                (
                    getattr(state, "context_meter_text", None),
                    getattr(state, "context_warning_level", None),
                )
                if state is not None and hasattr(state, "context_meter_text")
                else None
            )
            click.echo(
                render_top_header_bar_str(
                    session, width=comp_width, status=status, context_meter=context_meter
                )
            )
    return "\n".join(lines)


def render_bottom_footer_bar(
    version: str | None = None,
    width: int = 80,
) -> RenderableType:
    """Render commands accepted by the active colon-command dispatcher."""
    commands = [":help", ":status", ":diff", ":events", ":roles", ":landing"]
    left_plain = "   ".join(commands)
    ver = version or _get_version()
    right_plain = f"StackMind {ver}"
    spaces_count = max(2, width - len(left_plain) - len(right_plain))

    line = Text()
    for i, cmd in enumerate(commands):
        line.append(cmd, style="dim #94a3b8")
        if i < len(commands) - 1:
            line.append("   ", style="dim #475569")
    line.append(" " * spaces_count)
    line.append(right_plain, style="dim #64748b")
    return line


def render_bottom_footer_bar_str(
    version: str | None = None,
    width: int = 80,
) -> str:
    """Render bottom footer bar as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_bottom_footer_bar(version=version, width=width))
    return console.export_text().rstrip()


def _ensure_utf8() -> None:
    """Enable UTF-8 and Windows VT processing before emitting TUI ANSI sequences."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_std_handle = kernel32.GetStdHandle
        get_console_mode = kernel32.GetConsoleMode
        set_console_mode = kernel32.SetConsoleMode

        for handle_id in (-11, -12):
            handle = get_std_handle(handle_id)
            if handle in (0, -1):
                continue

            mode = ctypes.c_uint32()
            if not get_console_mode(handle, ctypes.byref(mode)):
                continue

            set_console_mode(handle, mode.value | 0x0004)

        handles_to_configure = []
        std_in = get_std_handle(-10)
        if std_in not in (0, -1):
            handles_to_configure.append(std_in)
        try:
            conin = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
            if conin not in (0, -1):
                handles_to_configure.append(conin)
        except Exception:
            pass

        for h in handles_to_configure:
            in_mode = ctypes.c_uint32()
            if get_console_mode(h, ctypes.byref(in_mode)):
                new_in_mode = (in_mode.value | 0x0200 | 0x0010 | 0x0080) & ~0x0040
                set_console_mode(h, new_in_mode)
    except Exception:
        pass
