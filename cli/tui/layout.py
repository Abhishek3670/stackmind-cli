"""Two-column workspace layout for the StackMind TUI (Phase 3 / WO-043).

Houses column splitting, width calculation, terminal dimension management,
and the persistent StackMind Runtime panel renderer.

Layout spec (IMPLEMENTATION_PLAN_TUI.md §6, §12):
- Left column (70-75%): Conversation area (landing, chat, composer).
- Right column (25-30%): Persistent 'StackMind Runtime' panel with muted heading.
- Single subtle vertical divider between columns.
- Responsive: hide Runtime panel when terminal width < 100 cols.
"""

from __future__ import annotations

import io
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Mapping

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from cli.tui.chat import _make_capture_console, should_render_ansi

# ── Layout Constants ─────────────────────────────────────────────────────────

# Threshold below which the runtime panel is hidden (§12).
NARROW_THRESHOLD = 100

# Runtime panel practical width bounds (cols).
RUNTIME_MIN_WIDTH = 26
RUNTIME_MAX_WIDTH = 42

# Runtime panel heading.
RUNTIME_HEADING = "StackMind Runtime"

# Section headings for the runtime panel.
RUNTIME_SECTIONS = ("AGENTS", "WORK ORDERS", "CURRENT OPERATION")

# Vertical chrome geometry (§28, §31, WO-009):
# - 3 lines for the pinned bottom composer box
# - 1 line for the bottom status bar
COMPOSER_BOX_HEIGHT = 3
STATUS_BAR_HEIGHT = 1
VERTICAL_CHROME_LINES = COMPOSER_BOX_HEIGHT + STATUS_BAR_HEIGHT  # 4 lines total
MIN_VIEWPORT_HEIGHT = 4


def compute_viewport_height(height: int | None = None) -> int:
    """Calculate the conversation/workspace viewport height for a given terminal height.

    Reserves lines for the pinned bottom composer box (3 lines) and the bottom status bar
    (1 line), ensuring at least MIN_VIEWPORT_HEIGHT rows remain for the workspace.
    This is the single canonical source of truth for vertical viewport sizing in the TUI.
    """
    lines = height if height is not None else shutil.get_terminal_size(fallback=(80, 24)).lines
    return max(MIN_VIEWPORT_HEIGHT, lines - VERTICAL_CHROME_LINES)


# ── Layout Calculation ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ColumnLayout:
    """Resolved column widths for the two-column workspace layout."""

    total_width: int
    conversation_width: int
    runtime_width: int
    show_runtime: bool

    @property
    def divider_width(self) -> int:
        """Width consumed by the vertical divider (1 if runtime visible, else 0)."""
        return 1 if self.show_runtime else 0


def compute_layout(width: int) -> ColumnLayout:
    """Compute the two-column layout for a given terminal width.

    Returns a ColumnLayout indicating widths and whether to show the
    runtime panel.  Follows IMPLEMENTATION_PLAN_TUI.md §6 & §12.
    """
    if width < NARROW_THRESHOLD:
        # Narrow fallback: hide runtime, full width to conversation.
        return ColumnLayout(
            total_width=width,
            conversation_width=width,
            runtime_width=0,
            show_runtime=False,
        )

    # Target runtime at ~28% of total (within 25-30%).
    raw_runtime = int(width * 0.28)
    runtime_w = max(RUNTIME_MIN_WIDTH, min(raw_runtime, RUNTIME_MAX_WIDTH))

    # 1 col for the vertical divider.
    conversation_w = width - runtime_w - 1

    return ColumnLayout(
        total_width=width,
        conversation_width=conversation_w,
        runtime_width=runtime_w,
        show_runtime=True,
    )


from cli.tui.runtime_panel import (
    RuntimePanelScroll,
    get_status_symbol,
    render_runtime_panel,
    render_runtime_panel_str,
)
from cli.tui.state import ConversationScroll


# ── Conversation Column & Activity Indicator ──────────────────────────────────

def render_conversation_column(
    conversation_renderable: RenderableType,
    *,
    scroll: ConversationScroll | None = None,
    state: Any | None = None,
) -> RenderableType:
    """Wrap conversation column with scroll badge when new activity arrived outside viewport (§31)."""
    if scroll is None and state is not None and hasattr(state, "conversation_scroll"):
        scroll = getattr(state, "conversation_scroll")
    if scroll is not None and scroll.has_new_activity:
        return Group(
            conversation_renderable,
            Text(""),
            Text("↓ New activity", style="bold #38bdf8", justify="center"),
        )
    return conversation_renderable


# ── Viewport Slicing & Scroll Offset ──────────────────────────────────────────

def slice_conversation_viewport(
    content: str,
    viewport_height: int,
    scroll: ConversationScroll | int | None = None,
    pad: bool = True,
) -> str:
    """Slice conversation text to fit the viewport height based on scroll offset (§31).

    If scroll is None or follow_bottom is True (default), displays the most recent
    viewport_height lines. If scroll_offset > 0, displays lines scrolled upward.
    Pads with blank lines if total lines < viewport_height to maintain fixed vertical layout.
    """
    if viewport_height <= 0:
        return content

    lines = content.split("\n")
    total_lines = len(lines)

    if isinstance(scroll, int):
        offset = max(0, scroll)
        follow_bottom = scroll <= 0
    elif scroll is not None:
        scroll.viewport_height = viewport_height
        if total_lines > viewport_height:
            max_off = total_lines - viewport_height
            if hasattr(scroll, "max_offset"):
                scroll.max_offset = max_off
            if scroll.scroll_offset > max_off:
                scroll.scroll_offset = max_off
        else:
            if hasattr(scroll, "max_offset"):
                scroll.max_offset = 0
            if hasattr(scroll, "scroll_offset") and scroll.scroll_offset > 0:
                scroll.scroll_offset = 0
        offset = max(0, scroll.scroll_offset)
        follow_bottom = scroll.follow_bottom
    else:
        offset = 0
        follow_bottom = True

    if total_lines <= viewport_height:
        if pad:
            lines = lines + [""] * (viewport_height - total_lines)
        return "\n".join(lines)

    # Content exceeds viewport height
    if follow_bottom or offset <= 0:
        start_idx = total_lines - viewport_height
        visible = lines[start_idx:total_lines]
    else:
        max_offset = total_lines - viewport_height
        effective_offset = min(offset, max_offset)
        end_idx = total_lines - effective_offset
        start_idx = max(0, end_idx - viewport_height)
        visible = lines[start_idx:end_idx]

    return "\n".join(visible)


# ── Two-Column Compositor ────────────────────────────────────────────────────

def render_workspace_layout(
    conversation_renderable: RenderableType,
    width: int = 120,
    *,
    agents: list[dict[str, Any]] | None = None,
    work_orders: list[dict[str, Any]] | None = None,
    current_operation: dict[str, Any] | None = None,
    state: Any | None = None,
    scroll: RuntimePanelScroll | None = None,
    conversation_scroll: ConversationScroll | None = None,
    height: int | None = None,
) -> RenderableType:
    """Compose the full two-column workspace layout.

    If width < NARROW_THRESHOLD, returns just the conversation renderable
    (wrapped with scroll badge if applicable).
    Otherwise, returns a Table grid with conversation | divider | runtime.
    """
    layout = compute_layout(width)

    conv_column = render_conversation_column(
        conversation_renderable,
        scroll=conversation_scroll,
        state=state,
    )

    if not layout.show_runtime:
        return conv_column

    # Build a grid: [conversation] [divider] [runtime]
    grid = Table.grid(padding=0)
    grid.add_column(width=layout.conversation_width)
    grid.add_column(width=1)  # vertical divider
    grid.add_column(width=layout.runtime_width)

    runtime = render_runtime_panel(
        width=layout.runtime_width,
        height=height,
        agents=agents,
        work_orders=work_orders,
        current_operation=current_operation,
        state=state,
        scroll=scroll,
    )

    divider_char = Text("│", style="dim #334155")

    grid.add_row(conv_column, divider_char, runtime)
    return grid


def render_workspace_layout_str(
    conversation_text: str = "",
    width: int = 120,
    *,
    conversation_content: str | None = None,
    agents: list[dict[str, Any]] | None = None,
    work_orders: list[dict[str, Any]] | None = None,
    current_operation: dict[str, Any] | None = None,
    state: Any | None = None,
    scroll: RuntimePanelScroll | None = None,
    conversation_scroll: ConversationScroll | None = None,
    height: int | None = None,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render the full workspace layout as plain or ANSI-styled text."""
    if conversation_content is not None:
        conversation_text = conversation_content
    if height is not None:
        conversation_text = slice_conversation_viewport(
            conversation_text,
            viewport_height=height,
            scroll=conversation_scroll,
            pad=True,
        )

    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    conv_renderable = (
        Text.from_ansi(conversation_text)
        if "\x1b[" in conversation_text
        else Text(conversation_text)
    )
    console.print(
        render_workspace_layout(
            conv_renderable,
            width=width,
            agents=agents,
            work_orders=work_orders,
            current_operation=current_operation,
            state=state,
            scroll=scroll,
            conversation_scroll=conversation_scroll,
            height=height,
        )
    )
    raw = console.export_text(styles=use_ansi)
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n"):
        raw = raw[:-1]

    lines = [line.rstrip("\r") for line in raw.split("\n")]
    if height is not None:
        if len(lines) < height:
            lines.extend([""] * (height - len(lines)))
        elif len(lines) > height:
            lines = lines[:height]
    return "\n".join(lines)


def render_full_screen_workspace(
    session: Mapping[str, Any],
    state: Any,
    width: int = 120,
    height: int = 24,
    *,
    conversation_content: str | None = None,
    composer_content: str | list[str] | None = None,
    composer_is_active: bool = False,
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
    include_composer: bool = True,
    force_color: bool | None = None,
    no_color: bool | None = None,
    composer_placeholder: str = "Type a message...",
) -> str:
    """Render a complete, anchored full-screen terminal frame.

    Layout geometry:
    - Row 0: Top status header bar (1 line)
    - Rows 1 to (height - 4): Two-column workspace layout with viewport slicing
      (Left: conversation viewport; Right: persistent StackMind Runtime panel)
    - Rows (height - 3) to (height - 1): Pinned bottom composer box (3 lines)
      (omitted when include_composer is False to leave rows open for prompt_composer_input).
    """
    from cli.tui.app import (
        render_composer_box_str,
        render_landing_block_str,
        render_top_header_bar_str,
    )
    from cli.tui.chat import render_chat_transcript_str

    layout = compute_layout(width)
    status = getattr(state, "connection_status", "online")
    context_meter = (
        (getattr(state, "context_meter_text", None), getattr(state, "context_warning_level", None))
        if state is not None and hasattr(state, "context_meter_text")
        else None
    )

    # 1. Bottom Status Bar (1 line, placed below composer, matches composer text box width / WO-018)
    status_width = layout.conversation_width if layout.show_runtime else width
    status_bar_str = render_top_header_bar_str(
        session or {},
        width=status_width,
        status=status,
        context_meter=context_meter,
        force_color=force_color,
        no_color=no_color,
    )

    # 2. Viewport height (reserved 1 line for bottom status bar, plus 3 lines for composer box)
    viewport_height = compute_viewport_height(height)

    # 3. Conversation content
    if conversation_content is not None:
        conv_text = conversation_content
    elif hasattr(state, "messages") and state.messages:
        conv_text = render_chat_transcript_str(
            state.messages,
            width=layout.conversation_width,
            force_color=force_color,
            no_color=no_color,
        )
    else:
        conv_text = render_landing_block_str(
            session=session,
            status=status,
            width=layout.conversation_width,
        )

    # 4. Two-column workspace with sliced conversation and persistent runtime panel (starts at row 0)
    workspace_str = render_workspace_layout_str(
        conv_text,
        width=width,
        state=state,
        scroll=getattr(state, "scroll", None),
        conversation_scroll=getattr(state, "conversation_scroll", None),
        height=viewport_height,
        force_color=force_color,
        no_color=no_color,
    )

    if not include_composer:
        return f"{workspace_str}\n{status_bar_str}"

    # 5. Pinned bottom composer box (3 lines) placed above bottom status bar
    comp_width = layout.conversation_width if layout.show_runtime else width
    composer_str = render_composer_box_str(
        placeholder=composer_placeholder,
        shortcuts=shortcuts,
        width=comp_width,
        content=composer_content,
        is_active=composer_is_active,
        has_content=bool(composer_content),
        force_color=force_color,
        no_color=no_color,
    )

    return f"{workspace_str}\n{composer_str}\n{status_bar_str}"


# ── Live Workspace Manager (Rich.Live Integration / WO-003) ───────────────────

class LiveWorkspaceManager:
    """Manages localized rendering of dynamic TUI regions via Rich.Live (§6, §12, WO-003).

    Eliminates screen-clearing redraw flickering during SSE streaming while
    keeping the static panels (top header, runtime panel, composer) aligned.
    Provides automatic fallback for non-TTY / CI environments.
    """

    def __init__(
        self,
        session: Mapping[str, Any] | None = None,
        state: Any | None = None,
        *,
        console: Console | None = None,
        width: int | None = None,
        height: int | None = None,
        auto_refresh: bool = False,
        refresh_per_second: float = 8.0,
        force_color: bool | None = None,
        no_color: bool | None = None,
    ) -> None:
        self.session = session or {}
        self.state = state
        self._custom_console = console
        term_size = shutil.get_terminal_size(fallback=(80, 24))
        self.width = width if width is not None else term_size.columns
        self.height = height if height is not None else term_size.lines
        self.auto_refresh = auto_refresh
        self.refresh_per_second = refresh_per_second
        self.force_color = force_color
        self.no_color = no_color

        self.is_terminal = self._check_is_terminal()
        self._live: Live | None = None
        self._active = False

    def _check_is_terminal(self) -> bool:
        if self._custom_console is not None:
            return bool(getattr(self._custom_console, "is_terminal", False))
        try:
            return bool(sys.stdout.isatty())
        except Exception:
            return False

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def live(self) -> Live | None:
        return self._live

    def build_renderable(
        self,
        conversation_content: str | RenderableType | None = None,
        composer_content: str | None = None,
        composer_is_active: bool = False,
        shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
        include_composer: bool = True,
        force_color: bool | None = None,
        no_color: bool | None = None,
        composer_placeholder: str = "Type a message...",
    ) -> RenderableType:
        """Compose the full screen workspace frame as a RenderableType."""
        fc = self.force_color if force_color is None else force_color
        nc = self.no_color if no_color is None else no_color
        frame_str = render_full_screen_workspace(
            session=self.session,
            state=self.state,
            width=self.width,
            height=self.height,
            conversation_content=conversation_content,
            composer_content=composer_content,
            composer_is_active=composer_is_active,
            shortcuts=shortcuts,
            include_composer=include_composer,
            force_color=fc,
            no_color=nc,
            composer_placeholder=composer_placeholder,
        )
        return Text.from_ansi(frame_str)

    def start(
        self,
        conversation_content: str | RenderableType | None = None,
        include_composer: bool = False,
    ) -> LiveWorkspaceManager:
        """Start the Rich.Live context if running in a real interactive terminal."""
        if not self.is_terminal:
            return self

        renderable = self.build_renderable(
            conversation_content=conversation_content,
            include_composer=include_composer,
        )
        console = self._custom_console or Console(
            force_terminal=True,
            width=self.width,
            height=self.height,
        )
        self._live = Live(
            renderable,
            console=console,
            screen=False,
            auto_refresh=self.auto_refresh,
            refresh_per_second=self.refresh_per_second,
            transient=False,
        )
        try:
            self._live.start()
            self._active = True
        except Exception:
            self._live = None
            self._active = False
        return self

    def stop(self) -> None:
        """Stop the Rich.Live context."""
        if self._live is not None and self._active:
            try:
                self._live.stop()
            except Exception:
                pass
        self._live = None
        self._active = False

    def __enter__(self) -> LiveWorkspaceManager:
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def update(
        self,
        conversation_content: str | RenderableType | None = None,
        composer_content: str | None = None,
        composer_is_active: bool = False,
        shortcuts: str = "Ctrl+K commands | Ctrl+L clear | PgUp/PgDn scroll",
        include_composer: bool = False,
        refresh: bool = True,
        force_color: bool | None = None,
        no_color: bool | None = None,
        composer_placeholder: str = "Type a message...",
    ) -> None:
        """Update the localized workspace renderable without screen clearing."""
        if self._live is not None and self._active:
            renderable = self.build_renderable(
                conversation_content=conversation_content,
                composer_content=composer_content,
                composer_is_active=composer_is_active,
                shortcuts=shortcuts,
                include_composer=include_composer,
                force_color=force_color,
                no_color=no_color,
                composer_placeholder=composer_placeholder,
            )
            self._live.update(renderable, refresh=refresh)

    def handle_resize(self, width: int, height: int) -> None:
        """Dynamically adapt dimensions upon terminal resize."""
        self.width = max(20, width)
        self.height = max(4, height)
        if self._live is not None and self._custom_console is None:
            try:
                self._live.console.width = self.width
                self._live.console.height = self.height
            except Exception:
                pass
        self.update(refresh=True)


def create_live_workspace(
    session: Mapping[str, Any] | None = None,
    state: Any | None = None,
    *,
    console: Console | None = None,
    width: int | None = None,
    height: int | None = None,
    auto_refresh: bool = False,
    refresh_per_second: float = 8.0,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> LiveWorkspaceManager:
    """Convenience factory for LiveWorkspaceManager."""
    return LiveWorkspaceManager(
        session=session,
        state=state,
        console=console,
        width=width,
        height=height,
        auto_refresh=auto_refresh,
        refresh_per_second=refresh_per_second,
        force_color=force_color,
        no_color=no_color,
    )


__all__ = [
    "COMPOSER_BOX_HEIGHT",
    "ConversationScroll",
    "LiveWorkspaceManager",
    "MIN_VIEWPORT_HEIGHT",
    "NARROW_THRESHOLD",
    "RUNTIME_HEADING",
    "RUNTIME_MAX_WIDTH",
    "RUNTIME_MIN_WIDTH",
    "RUNTIME_SECTIONS",
    "STATUS_BAR_HEIGHT",
    "VERTICAL_CHROME_LINES",
    "ColumnLayout",
    "RuntimePanelScroll",
    "compute_layout",
    "compute_viewport_height",
    "create_live_workspace",
    "get_status_symbol",
    "render_conversation_column",
    "render_full_screen_workspace",
    "render_runtime_panel",
    "render_runtime_panel_str",
    "render_workspace_layout",
    "render_workspace_layout_str",
    "slice_conversation_viewport",
]
