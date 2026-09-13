"""Chat-first message and transcript renderers for the StackMind TUI (Phase 2 & WO-034)."""

from __future__ import annotations

import datetime
import io
from typing import TYPE_CHECKING

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from cli.tui.state import ChatMessage


def _current_time_str() -> str:
    return datetime.datetime.now().strftime("%H:%M")


def render_user_message(content: str, timestamp: str | None = None) -> RenderableType:
    """Render a user message with bright blue left accent bar, 'You' header, and right-aligned timestamp."""
    ts_str = timestamp or _current_time_str()
    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="right")

    left = Text("│ ", style="bold #38bdf8").append("You", style="bold #38bdf8")
    right = Text(ts_str, style="dim #64748b")
    grid.add_row(left, right)

    lines: list[RenderableType] = [grid]
    for line in content.splitlines():
        msg_line = Text("│ ", style="bold #38bdf8")
        msg_line.append(line, style="white")
        lines.append(msg_line)
    return Group(*lines)


def render_user_message_str(content: str, width: int = 80, timestamp: str | None = None) -> str:
    """Render a user message as plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_user_message(content, timestamp=timestamp))
    return console.export_text().rstrip()


def render_assistant_message(content: str, timestamp: str | None = None) -> RenderableType:
    """Render an assistant message with '✦ StackMind' header, right-aligned timestamp, and rich Markdown body."""
    ts_str = timestamp or _current_time_str()
    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="right")

    left = Text("✦ ", style="bold #a855f7").append("StackMind", style="bold white")
    right = Text(ts_str, style="dim #64748b")
    grid.add_row(left, right)

    body = Markdown(content)
    return Group(grid, Text(""), body)


def render_assistant_message_str(content: str, width: int = 80, timestamp: str | None = None) -> str:
    """Render an assistant message as plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_assistant_message(content, timestamp=timestamp))
    return console.export_text().rstrip()


def render_chat_transcript(messages: list[ChatMessage]) -> RenderableType:
    """Render the full conversation transcript including tool activities, diffs, and timestamps."""
    elements: list[RenderableType] = []
    for msg in messages:
        role = msg.role.lower()
        if role == "user":
            elements.append(render_user_message(msg.content, timestamp=msg.timestamp))
        elif role == "assistant":
            elements.append(render_assistant_message(msg.content, timestamp=msg.timestamp))
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
            elements.append(Group(header, Markdown(msg.content)))
        elements.append(Text(""))
    return Group(*elements) if elements else Text("(No messages in conversation yet.)", style="dim italic")


def render_chat_transcript_str(messages: list[ChatMessage], width: int = 80) -> str:
    """Render the full conversation transcript as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_chat_transcript(messages))
    return console.export_text().rstrip()


__all__ = [
    "render_assistant_message",
    "render_assistant_message_str",
    "render_chat_transcript",
    "render_chat_transcript_str",
    "render_user_message",
    "render_user_message_str",
]
