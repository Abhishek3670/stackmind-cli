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
    conversation_text: str,
    width: int = 120,
    *,
    agents: list[dict[str, Any]] | None = None,
    work_orders: list[dict[str, Any]] | None = None,
    current_operation: dict[str, Any] | None = None,
    state: Any | None = None,
    scroll: RuntimePanelScroll | None = None,
    conversation_scroll: ConversationScroll | None = None,
) -> str:
    """Render the full workspace layout as plain text."""
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
        )
    )
    return console.export_text().rstrip()


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
    "render_runtime_panel",
    "render_runtime_panel_str",
    "render_workspace_layout",
    "render_workspace_layout_str",
]
