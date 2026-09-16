"""Chat-first message and transcript renderers for the StackMind TUI (Phases 2, 4 & 5 per §13–§17, §32, §33)."""

from __future__ import annotations

import datetime
import io
import re
from typing import TYPE_CHECKING

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from cli.tui.state import ChatMessage


def _current_time_str() -> str:
    return datetime.datetime.now().strftime("%H:%M")


def strip_internal_reasoning(content: str) -> str:
    """Remove provider-delimited private reasoning without altering final Markdown."""
    clean = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", content, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<think\b[^>]*>.*$", "", clean, flags=re.IGNORECASE | re.DOTALL).strip()


def render_user_message(
    content: str,
    timestamp: str | None = None,
    *,
    accent: bool = True,
    show_timestamp: bool | None = None,
) -> RenderableType:
    """Render a user message per §14 (visually quiet, 'You' header, unboxed, compact)."""
    if accent:
        hdr = Text("│ ", style="bold #38bdf8").append("You", style="bold #38bdf8")
    else:
        hdr = Text("You", style="bold #38bdf8")

    # Per §17: Suppress timestamps from normal chat rows unless explicitly enabled
    show_ts = show_timestamp if show_timestamp is not None else (timestamp is not None and show_timestamp is not False)
    if show_timestamp is False:
        show_ts = False

    if show_ts and timestamp:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(hdr, Text(timestamp, style="dim #64748b"))
        top_elem: RenderableType = grid
    else:
        top_elem = hdr

    lines: list[RenderableType] = [top_elem]
    for line in content.splitlines():
        if accent:
            msg_line = Text("│ ", style="bold #38bdf8")
            msg_line.append(line, style="white")
        else:
            msg_line = Text(line, style="white")
        lines.append(msg_line)
    return Group(*lines)


def render_user_message_str(
    content: str,
    width: int = 80,
    timestamp: str | None = None,
    *,
    accent: bool = True,
    show_timestamp: bool | None = None,
) -> str:
    """Render a user message as plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(
        render_user_message(
            content,
            timestamp=timestamp,
            accent=accent,
            show_timestamp=show_timestamp,
        )
    )
    return console.export_text().rstrip()


def render_assistant_message(
    content: str,
    timestamp: str | None = None,
    *,
    show_timestamp: bool | None = None,
    inline_diffs: bool = True,
) -> RenderableType:
    """Render an assistant message with '✦ StackMind' header (§15), rich Markdown (§32), and breathing room (§16)."""
    hdr = Text("✦ ", style="bold #a855f7").append("StackMind", style="bold white")

    # Per §17: Suppress timestamps from normal chat rows unless explicitly enabled
    show_ts = show_timestamp if show_timestamp is not None else (timestamp is not None and show_timestamp is not False)
    if show_timestamp is False:
        show_ts = False

    if show_ts and timestamp:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(hdr, Text(timestamp, style="dim #64748b"))
        top_elem: RenderableType = grid
    else:
        top_elem = hdr

    cleaned = strip_internal_reasoning(content)

    # Check for raw unified diff to render cleanly inline per §32, §33
    if inline_diffs and (
        cleaned.startswith("diff --git ")
        or (cleaned.startswith("--- ") and "\n+++ " in cleaned)
    ):
        from cli.tui.diff import render_unified_diff
        body: RenderableType = render_unified_diff(cleaned)
    else:
        body = Markdown(cleaned, code_theme="monokai")

    # Generous breathing room: header, blank line, and rich Markdown body (§16)
    return Group(top_elem, Text(""), body)


def render_assistant_message_str(
    content: str,
    width: int = 80,
    timestamp: str | None = None,
    *,
    show_timestamp: bool | None = None,
    inline_diffs: bool = True,
) -> str:
    """Render an assistant message as plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(
        render_assistant_message(
            content,
            timestamp=timestamp,
            show_timestamp=show_timestamp,
            inline_diffs=inline_diffs,
        )
    )
    return console.export_text().rstrip()


def render_chat_transcript(
    messages: list[ChatMessage],
    *,
    width: int | None = None,
    padding: tuple[int, int] = (0, 1),
    show_timestamps: bool = False,
    accent: bool = True,
) -> RenderableType:
    """Render the full conversation transcript with left alignment, small horizontal padding (§13), and inline diffs (§33)."""
    if not messages:
        empty_text = Text("(No messages in conversation yet.)", style="dim italic")
        return Padding(empty_text, padding) if padding else empty_text

    elements: list[RenderableType] = []
    for msg in messages:
        role = msg.role.lower()
        if role == "user":
            elements.append(
                render_user_message(
                    msg.content,
                    timestamp=msg.timestamp if show_timestamps else None,
                    accent=accent,
                    show_timestamp=show_timestamps,
                )
            )
        elif role == "assistant":
            elements.append(
                render_assistant_message(
                    msg.content,
                    timestamp=msg.timestamp if show_timestamps else None,
                    show_timestamp=show_timestamps,
                )
            )
        elif role == "tool":
            from cli.tui.events import ToolActivity, render_tool_activity
            parts = msg.content.split(None, 1)
            t_name = parts[0] if parts else "tool"
            t_target = parts[1] if len(parts) > 1 else ""
            activity = ToolActivity(tool_name=t_name, target=t_target, status="COMPLETED")
            elements.append(render_tool_activity(activity))
        elif role == "diff":
            from cli.tui.diff import render_unified_diff
            elements.append(render_unified_diff(msg.content))
        else:
            header = Text(f"✦ {msg.role.title()}", style="dim cyan")
            elements.append(Group(header, Markdown(msg.content, code_theme="monokai")))
        elements.append(Text(""))

    # Drop trailing blank separator
    if elements and isinstance(elements[-1], Text) and not elements[-1].plain:
        elements.pop()

    body_group = Group(*elements)
    return Padding(body_group, padding) if padding else body_group


def render_chat_transcript_str(
    messages: list[ChatMessage],
    width: int = 80,
    *,
    padding: tuple[int, int] = (0, 1),
    show_timestamps: bool = False,
    accent: bool = True,
) -> str:
    """Render the full conversation transcript as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(
        render_chat_transcript(
            messages,
            width=width,
            padding=padding,
            show_timestamps=show_timestamps,
            accent=accent,
        )
    )
    return console.export_text().rstrip()


__all__ = [
    "render_assistant_message",
    "render_assistant_message_str",
    "render_chat_transcript",
    "render_chat_transcript_str",
    "strip_internal_reasoning",
    "render_user_message",
    "render_user_message_str",
]
