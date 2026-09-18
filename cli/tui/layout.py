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
from dataclasses import dataclass
from typing import Any

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

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
) -> str:
    """Render the full workspace layout as plain text."""
    if conversation_content is not None:
        conversation_text = conversation_content
    if height is not None:
        conversation_text = slice_conversation_viewport(
            conversation_text,
            viewport_height=height,
            scroll=conversation_scroll,
            pad=True,
        )

    buf = io.StringIO()
    console = Console(
        file=buf, record=True, width=width, force_terminal=False, color_system=None
    )
    console.print(
        render_workspace_layout(
            Text(conversation_text),
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
    return console.export_text().rstrip()


def render_full_screen_workspace(
    session: Mapping[str, Any],
    state: Any,
    width: int = 120,
    height: int = 24,
    *,
    conversation_content: str | None = None,
    composer_content: str | list[str] | None = None,
    composer_is_active: bool = False,
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
) -> str:
    """Render a complete, anchored full-screen terminal frame.

    Layout geometry:
    - Row 0: Top status header bar (1 line)
    - Rows 1 to (height - 4): Two-column workspace layout with viewport slicing
      (Left: conversation viewport; Right: persistent StackMind Runtime panel)
    - Rows (height - 3) to (height - 1): Pinned bottom composer box (3 lines)
    """
    from cli.tui.app import (
        render_composer_box_str,
        render_landing_block_str,
        render_top_header_bar_str,
    )
    from cli.tui.chat import render_chat_transcript_str

    layout = compute_layout(width)
    status = getattr(state, "connection_status", "online")

    # 1. Top Header Bar (1 line)
    header_str = render_top_header_bar_str(session, width=width, status=status)

    # 2. Viewport height (leave 1 line for header, 3 lines for composer box)
    viewport_height = max(4, height - 4)

    # 3. Conversation content
    if conversation_content is not None:
        conv_text = conversation_content
    elif hasattr(state, "messages") and state.messages:
        conv_text = render_chat_transcript_str(state.messages, width=layout.conversation_width)
    else:
        conv_text = render_landing_block_str(
            session=session,
            status=status,
            width=layout.conversation_width,
        )

    # 4. Two-column workspace with sliced conversation and persistent runtime panel
    workspace_str = render_workspace_layout_str(
        conv_text,
        width=width,
        state=state,
        scroll=getattr(state, "scroll", None),
        conversation_scroll=getattr(state, "conversation_scroll", None),
        height=viewport_height,
    )

    # 5. Pinned bottom composer box (3 lines)
    composer_str = render_composer_box_str(
        shortcuts=shortcuts,
        width=width,
        content=composer_content,
        is_active=composer_is_active,
        has_content=bool(composer_content),
    )

    return f"{header_str}\n{workspace_str}\n{composer_str}"


__all__ = [
    "ConversationScroll",
    "NARROW_THRESHOLD",
    "RUNTIME_HEADING",
    "RUNTIME_MAX_WIDTH",
    "RUNTIME_MIN_WIDTH",
    "RUNTIME_SECTIONS",
    "ColumnLayout",
    "RuntimePanelScroll",
    "compute_layout",
    "get_status_symbol",
    "render_conversation_column",
    "render_full_screen_workspace",
    "render_runtime_panel",
    "render_runtime_panel_str",
    "render_workspace_layout",
    "render_workspace_layout_str",
    "slice_conversation_viewport",
]
