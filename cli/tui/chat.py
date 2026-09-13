"""Chat-first message and transcript renderers for the StackMind TUI (Phase 2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

if TYPE_CHECKING:
    from cli.tui.state import ChatMessage


def render_user_message(content: str) -> RenderableType:
    """Render a user message with subtle left accent line and 'You' header."""
    header = Text("│ ", style="bold cyan")
    header.append("You", style="bold white")
    lines: list[RenderableType] = [header]
    for line in content.splitlines():
        msg_line = Text("│ ", style="cyan")
        msg_line.append(line, style="white")
        lines.append(msg_line)
    return Group(*lines)


def render_user_message_str(content: str, width: int = 80) -> str:
    """Render a user message as plain formatted string."""
    console = Console(record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_user_message(content))
    return console.export_text().rstrip()


def render_assistant_message(content: str) -> RenderableType:
    """Render an assistant message with '✦ StackMind' header and rich Markdown body."""
    header = Text("✦ StackMind", style="bold cyan")
    body = Markdown(content)
    return Group(header, Text(""), body)


def render_assistant_message_str(content: str, width: int = 80) -> str:
    """Render an assistant message as plain formatted string."""
    console = Console(record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_assistant_message(content))
    return console.export_text().rstrip()


def render_chat_transcript(messages: list[ChatMessage]) -> RenderableType:
    """Render the full conversation transcript."""
    elements: list[RenderableType] = []
    for msg in messages:
        if msg.role.lower() == "user":
            elements.append(render_user_message(msg.content))
        elif msg.role.lower() == "assistant":
            elements.append(render_assistant_message(msg.content))
        else:
            header = Text(f"✦ {msg.role.title()}", style="dim cyan")
            elements.append(Group(header, Markdown(msg.content)))
        elements.append(Text(""))
    return Group(*elements) if elements else Text("(No messages in conversation yet.)", style="dim italic")


def render_chat_transcript_str(messages: list[ChatMessage], width: int = 80) -> str:
    """Render the full conversation transcript as plain string."""
    console = Console(record=True, width=width, force_terminal=False, color_system=None)
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
