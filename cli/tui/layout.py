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
from enum import Enum
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

# 5-Tier Breakpoint Boundaries (WO-019)
VERY_NARROW_BREAKPOINT = 80
NARROW_BREAKPOINT = 100
WIDE_BREAKPOINT = 140
VERY_WIDE_BREAKPOINT = 180

# Practical width bounds for runtime panel (cols).
RUNTIME_MIN_WIDTH = 26
RUNTIME_MAX_WIDTH = 42

# Very Wide (>=180) runtime panel width (WO-022 AC-1): conversation stretches fully to
# width - runtime - 1, flush at column 0. No centered-deck gutters or side paddings.
VERY_WIDE_RUNTIME_WIDTH = 38

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


# ── Layout Calculation & Tiers ───────────────────────────────────────────────

class LayoutTier(str, Enum):
    """5-tier responsive breakpoint tiers (WO-019)."""

    VERY_NARROW = "VERY_NARROW"  # < 80 cols: single-col full-width, compact status, runtime hidden
    NARROW = "NARROW"            # 80-99 cols: single-col full-width, 1-line top badge bar
    NORMAL = "NORMAL"            # 100-139 cols: 2-column standard split (~72% / ~28%)
    WIDE = "WIDE"                # 140-179 cols: 2-column flex (72% / 28%, runtime max 38)
    VERY_WIDE = "VERY_WIDE"      # >= 180 cols: full-stretch 2-column split (WO-022)


@dataclass(frozen=True)
class ColumnLayout:
    """Resolved column widths and geometry for responsive workspace layouts."""

    total_width: int
    conversation_width: int
    runtime_width: int
    show_runtime: bool
    tier: LayoutTier = LayoutTier.NORMAL
    left_margin: int = 0
    right_margin: int = 0
    show_runtime_badge: bool = False

    @property
    def divider_width(self) -> int:
        """Width consumed by the vertical divider (1 if runtime visible, else 0)."""
        return 1 if self.show_runtime else 0


def compute_layout(width: int) -> ColumnLayout:
    """Compute the multi-tier responsive layout for a given terminal width (WO-019).

    Supports 5 breakpoint tiers:
    - VERY_NARROW (<80 cols): Single-column full-width, runtime panel hidden into status indicators.
    - NARROW (80-99 cols): Single-column conversation, runtime collapsed into 1-line top badge bar.
    - NORMAL (100-139 cols): 2-column standard split with ~28% runtime panel.
    - WIDE (140-179 cols): 2-column proportional flex with runtime capped at 38 cols.
    - VERY_WIDE (>=180 cols): Full-stretch 2-column split, flush at column 0 (WO-022).
    """
    if width < VERY_NARROW_BREAKPOINT:
        return ColumnLayout(
            total_width=width,
            conversation_width=width,
            runtime_width=0,
            show_runtime=False,
            tier=LayoutTier.VERY_NARROW,
            left_margin=0,
            right_margin=0,
            show_runtime_badge=False,
        )

    if width < NARROW_BREAKPOINT:
        # Narrow (80-99 cols): Full-width conversation + 1-line runtime badge bar
        return ColumnLayout(
            total_width=width,
            conversation_width=width,
            runtime_width=0,
            show_runtime=False,
            tier=LayoutTier.NARROW,
            left_margin=0,
            right_margin=0,
            show_runtime_badge=True,
        )

    if width < WIDE_BREAKPOINT:
        # Normal (100-139 cols): Standard 2-column split (~28% runtime)
        raw_runtime = int(width * 0.28)
        runtime_w = max(RUNTIME_MIN_WIDTH, min(raw_runtime, RUNTIME_MAX_WIDTH))
        conversation_w = width - runtime_w - 1
        return ColumnLayout(
            total_width=width,
            conversation_width=conversation_w,
            runtime_width=runtime_w,
            show_runtime=True,
            tier=LayoutTier.NORMAL,
            left_margin=0,
            right_margin=0,
            show_runtime_badge=False,
        )

    if width < VERY_WIDE_BREAKPOINT:
        # Wide (140-179 cols): Proportional 2-column split with max runtime width RUNTIME_MAX_WIDTH (42)
        raw_runtime = int(width * 0.28)
        runtime_w = max(RUNTIME_MIN_WIDTH, min(raw_runtime, RUNTIME_MAX_WIDTH))
        conversation_w = width - runtime_w - 1
        return ColumnLayout(
            total_width=width,
            conversation_width=conversation_w,
            runtime_width=runtime_w,
            show_runtime=True,
            tier=LayoutTier.WIDE,
            left_margin=0,
            right_margin=0,
            show_runtime_badge=False,
        )

    # Very Wide (>=180 cols): Full-stretch 2-column split, flush at column 0 (WO-022 AC-1).
    # No centered-deck gutters: conversation consumes all remaining width after the
    # fixed-width runtime panel and divider.
    runtime_w = VERY_WIDE_RUNTIME_WIDTH
    conversation_w = width - runtime_w - 1

    return ColumnLayout(
        total_width=width,
        conversation_width=conversation_w,
        runtime_width=runtime_w,
        show_runtime=True,
        tier=LayoutTier.VERY_WIDE,
        left_margin=0,
        right_margin=0,
        show_runtime_badge=False,
    )


# ── Adaptive String Truncation Helpers (WO-019 AC-3) ─────────────────────────

def format_responsive_path(path: str, max_width: int) -> str:
    """Adaptively truncate a filesystem path to fit within max_width using middle truncation.

    Preserves root/drive and trailing leaf components while collapsing intermediate segments
    into ellipsis ('...'). Avoids arbitrary mid-token chops whenever possible (WO-019 AC-3).
    """
    if not path or max_width <= 0:
        return ""
    if len(path) <= max_width:
        return path
    if max_width <= 3:
        return "..."[:max_width]

    # Detect primary path separator
    sep = "\\" if "\\" in path else "/"
    parts = [p for p in path.split(sep) if p]

    prefix = ""
    if path.startswith(sep):
        prefix = sep
    elif len(parts) > 0 and ":" in parts[0]:
        prefix = parts[0] + sep
        parts = parts[1:]

    if len(parts) >= 2:
        head = parts[0]
        tail = parts[-1]
        min_middle = f"{prefix}{head}{sep}...{sep}{tail}" if head != tail else f"{prefix}...{sep}{tail}"
        if len(min_middle) <= max_width:
            left_parts = [head]
            right_parts = [tail]
            remaining = parts[1:-1]
            while remaining:
                next_tail = remaining[-1]
                candidate_right = [next_tail] + right_parts
                candidate = f"{prefix}{sep.join(left_parts)}{sep}...{sep}{sep.join(candidate_right)}"
                if len(candidate) <= max_width:
                    right_parts = candidate_right
                    remaining = remaining[:-1]
                else:
                    break
            while remaining:
                next_head = remaining[0]
                candidate_left = left_parts + [next_head]
                candidate = f"{prefix}{sep.join(candidate_left)}{sep}...{sep}{sep.join(right_parts)}"
                if len(candidate) <= max_width:
                    left_parts = candidate_left
                    remaining = remaining[1:]
                else:
                    break
            return f"{prefix}{sep.join(left_parts)}{sep}...{sep}{sep.join(right_parts)}"

        min_leaf = f"{prefix}...{sep}{tail}"
        if len(min_leaf) <= max_width:
            return min_leaf

    avail = max_width - 3
    if avail <= 0:
        return "..."[:max_width]
    head_len = avail // 2
    tail_len = avail - head_len
    return f"{path[:head_len]}...{path[-tail_len:]}"


def format_responsive_title(title: str, max_width: int) -> str:
    """Adaptively truncate a title or message using word-aware soft truncation (WO-019 AC-3)."""
    if not title or max_width <= 0:
        return ""
    if len(title) <= max_width:
        return title
    if max_width <= 3:
        return "…"[:max_width]

    limit = max_width - 1
    candidate = title[:limit]
    last_space = candidate.rfind(" ")
    if last_space > limit // 2:
        return candidate[:last_space].rstrip(",:;.- ") + "…"
    return candidate.rstrip() + "…"


# ── Runtime State Badge Bar (WO-019 AC-1) ────────────────────────────────────

def render_runtime_badge_bar(
    width: int,
    *,
    agents: list[dict[str, Any]] | list[Any] | None = None,
    work_orders: list[dict[str, Any]] | list[Any] | None = None,
    current_operation: dict[str, Any] | Any | None = None,
    state: Any | None = None,
) -> Text:
    """Render a 1-line top badge bar summarizing runtime state for Narrow mode (WO-019 AC-1).

    Format: AGENTS (status) │ WOs (active/total) │ OP (role/name)
    Guaranteed single line, never wrapping.
    """
    if state is not None:
        if agents is None and hasattr(state, "roles"):
            agents = list(state.roles.values())
        if work_orders is None and hasattr(state, "work_orders"):
            work_orders = list(state.work_orders)
        if current_operation is None:
            if hasattr(state, "get_current_operation"):
                current_operation = state.get_current_operation()
            elif hasattr(state, "operations"):
                running = [op for op in state.operations.values() if getattr(op, "status", "").upper() in {"RUNNING", "ACTIVE"}]
                if running:
                    current_operation = running[-1]

    # 1. Agents summary
    if agents:
        running_count = 0
        for a in agents:
            st = (a.get("state") or a.get("status") if isinstance(a, Mapping) else getattr(a, "state", getattr(a, "status", ""))) or ""
            if str(st).lower() in {"running", "active", "orchestrating", "implementing"}:
                running_count += 1
        agent_str = f"AGENTS: {running_count}/{len(agents)} active" if running_count > 0 else f"AGENTS: {len(agents)} idle"
    else:
        agent_str = "AGENTS: idle"

    # 2. Work Orders summary
    if work_orders:
        active_wo = 0
        for w in work_orders:
            st = (w.get("status") if isinstance(w, Mapping) else getattr(w, "status", "")) or ""
            if str(st).upper() in {"ACTIVE", "IN_PROGRESS", "RUNNING"}:
                active_wo += 1
        wo_str = f"WOs: {active_wo}/{len(work_orders)} active" if active_wo > 0 else f"WOs: {len(work_orders)}"
    else:
        wo_str = "WOs: 0"

    # 3. Current Operation summary
    if current_operation:
        if isinstance(current_operation, Mapping):
            role = current_operation.get("role")
            backend = current_operation.get("backend")
            name = current_operation.get("name") or "active"
        else:
            role = getattr(current_operation, "role", None)
            backend = getattr(current_operation, "backend", None)
            name = getattr(current_operation, "name", "active")
        detail = f"{role} · {backend}" if (role and backend) else (role or name)
        # WO-019: word-aware soft truncation keeps unbounded operation names from
        # overflowing the badge bar (consumes format_responsive_title in app code).
        detail = format_responsive_title(detail, 24)
        op_str = f"OP: {detail}"
    else:
        op_str = "OP: idle"

    res = Text(no_wrap=True, overflow="crop")
    res.append(" ", style="dim #475569")
    res.append("● ", style="bold #22c55e" if "active" in agent_str else "dim #64748b")
    res.append(agent_str, style="bold #38bdf8")
    res.append("  │  ", style="dim #334155")
    res.append("◆ ", style="bold #60a5fa" if "active" in wo_str else "dim #64748b")
    res.append(wo_str, style="bold #60a5fa")
    res.append("  │  ", style="dim #334155")
    res.append("⚙ ", style="dim #a855f7")
    res.append(op_str, style="dim white")

    plain_len = len(res.plain)
    if plain_len < width:
        res.append(" " * (width - plain_len))
    return res


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
    """Compose the responsive workspace layout across tiers (WO-019).

    - If show_runtime is False and show_runtime_badge is True (NARROW mode),
      returns Group(badge_bar, conversation_column).
    - If show_runtime is False (VERY_NARROW mode), returns conversation_column.
    - If show_runtime is True (NORMAL, WIDE, VERY_WIDE modes), returns a Table grid
      with [conversation] [divider] [runtime] — all flush at column 0 (WO-022).
    """
    layout = compute_layout(width)

    conv_column = render_conversation_column(
        conversation_renderable,
        scroll=conversation_scroll,
        state=state,
    )

    if not layout.show_runtime:
        has_runtime_data = (
            (state is not None)
            or bool(agents)
            or bool(work_orders)
            or bool(current_operation)
        )
        if layout.show_runtime_badge and has_runtime_data:
            badge = render_runtime_badge_bar(
                width=width,
                agents=agents,
                work_orders=work_orders,
                current_operation=current_operation,
                state=state,
            )
            return Group(badge, conv_column)
        return conv_column

    # Build a grid: [conversation] [divider] [runtime] — flush at column 0 (WO-022 AC-2)
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
    layout = compute_layout(width)
    has_runtime_data = (
        (state is not None)
        or bool(agents)
        or bool(work_orders)
        or bool(current_operation)
    )
    if height is not None:
        # WO-019: reserve 1 line for the NARROW-tier top badge bar when runtime data exists.
        effective_vp_height = (
            max(1, height - 1) if (layout.show_runtime_badge and has_runtime_data) else height
        )
        # 1806659: reserve 2 lines for the scroll activity badge when active (§31).
        active_scroll = conversation_scroll
        if active_scroll is None and state is not None and hasattr(state, "conversation_scroll"):
            active_scroll = getattr(state, "conversation_scroll")
        if active_scroll is not None and getattr(active_scroll, "has_new_activity", False):
            effective_vp_height = max(1, effective_vp_height - 2)
        conversation_text = slice_conversation_viewport(
            conversation_text,
            viewport_height=effective_vp_height,
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
    """Render a complete, anchored full-screen terminal frame across responsive tiers (WO-019).

    Layout geometry:
    - Rows 0 to (height - 5): Workspace layout with viewport slicing
      (Left: conversation viewport; Right: persistent StackMind Runtime panel, or top badge bar in NARROW)
    - Rows (height - 4) to (height - 2): Pinned bottom composer box (3 lines)
      (omitted when include_composer is False to leave rows open for prompt_composer_input).
    - Row (height - 1): Bottom status bar (1 line) aligned with conversation column.
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

    # 4. Workspace layout with sliced conversation and persistent runtime panel
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
    "LayoutTier",
    "NARROW_BREAKPOINT",
    "RuntimePanelScroll",
    "VERY_NARROW_BREAKPOINT",
    "VERY_WIDE_BREAKPOINT",
    "VERY_WIDE_RUNTIME_WIDTH",
    "WIDE_BREAKPOINT",
    "compute_layout",
    "compute_viewport_height",
    "create_live_workspace",
    "format_responsive_path",
    "format_responsive_title",
    "get_status_symbol",
    "render_conversation_column",
    "render_full_screen_workspace",
    "render_runtime_badge_bar",
    "render_runtime_panel",
    "render_runtime_panel_str",
    "render_workspace_layout",
    "render_workspace_layout_str",
    "slice_conversation_viewport",
]
