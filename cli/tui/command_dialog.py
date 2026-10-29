"""System Command Dialog — Anchored Output Panel Above Composer.

Displays output of system commands (like :status, :resume, :wo, :tree, :help)
in a dedicated dialog just above the input text area, preventing command
outputs from cluttering the conversation transcript area.
"""

from __future__ import annotations

import io
from typing import Any

from rich import box
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text

from cli.tui.chat import _make_capture_console, should_render_ansi


COMMAND_NAMES: dict[str, str] = {
    ":status": "Status",
    ":delivery-status": "Product Delivery Status",
    ":delivery": "Product Delivery Status",
    ":deliverystatus": "Product Delivery Status",
    ":resume": "Resume",
    ":plan": "Plan",
    ":pause": "Pause",
    ":wo": "Work Orders",
    ":work-orders": "Work Orders",
    ":tasks": "Work Orders",
    ":goal": "Product Goal",
    ":approve": "Approve",
    ":reject": "Reject",
    ":roles": "Agent Roles",
    ":rebind": "Rebind Role",
    ":config": "Configuration",
    ":provider": "LLM Providers",
    ":providers": "LLM Providers",
    ":tree": "Operation Tree",
    ":agents": "Agent Tree",
    ":completion": "Project Completion",
    ":cancel": "Cancel",
    ":contract": "Contract Boundary",
    ":diff": "Unified Diff",
    ":matrix": "Verification Matrix",
    ":events": "Event Stream",
    ":reconnect": "Reconnect",
    ":chat": "Conversation",
    ":actions": "Actions History",
    ":thinking": "Thinking Process",
    ":govern": "Governance",
    ":compact": "Compact Context",
    ":timeout": "Turn Timeout",
    ":landing": "Project Landing",
    ":skills": "Procedural Skills",
    ":skill": "Procedural Skill",
    ":consume-skills": "Import Skills",
    ":consume-skill": "Import Skill",
    ":copy": "Clipboard Copy",
    ":yank": "Clipboard Copy",
    ":clipboard": "Clipboard Copy",
    ":projects": "Projects",
    ":help": "Help",
    ":debugkeys": "Debug Keys",
    ":exit": "Exit",
    ":quit": "Exit",
}


def get_command_friendly_name(cmd: str | None) -> str:
    """Derive human-friendly display name from command string, without gear icon or raw colon."""
    if not cmd:
        return "Command Output"
    raw = cmd.strip()
    if not raw:
        return "Command Output"
    parts = raw.split(maxsplit=1)
    base_cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    friendly_base = COMMAND_NAMES.get(
        base_cmd,
        base_cmd.lstrip(":").replace("-", " ").replace("_", " ").title() or "Command Output",
    )
    if args:
        return f"{friendly_base} ({args})"
    return friendly_base


def compute_command_dialog_height(state: Any | None, height: int | None = None) -> int:
    """Calculate the height (in terminal lines) of the command dialog box.

    Returns 0 if no command dialog is active or open.
    Otherwise, returns lines needed (visible content rows + 2 border lines).
    Clamped to a maximum proportion of terminal height so the conversation area remains visible.
    """
    if state is None or not getattr(state, "command_dialog_open", False):
        return 0
    output = getattr(state, "command_dialog_output", "")
    if not output:
        return 0

    lines = output.split("\n")
    content_lines = len(lines)

    term_height = height or 24
    max_content_height = max(2, min(8, max(3, (term_height - 10) // 2)))
    visible_content = min(content_lines, max_content_height)
    return visible_content + 2


def render_command_dialog(
    state: Any,
    width: int = 80,
    height: int | None = None,
) -> Panel:
    """Render the rounded command dialog panel showing command output."""
    cmd = getattr(state, "command_dialog_command", ":status")
    output = getattr(state, "command_dialog_output", "")
    scroll = getattr(state, "command_dialog_scroll", 0)

    lines = output.split("\n")
    total_lines = len(lines)

    term_height = height or 24
    max_content = max(2, min(8, max(3, (term_height - 10) // 2)))

    if total_lines > max_content:
        max_scroll = total_lines - max_content
        scroll = max(0, min(scroll, max_scroll))
        if hasattr(state, "command_dialog_scroll"):
            state.command_dialog_scroll = scroll
        visible_lines = lines[scroll : scroll + max_content]
        scroll_indicator = f"[{scroll + 1}-{scroll + len(visible_lines)}/{total_lines} | PgUp/PgDn | Esc close]"
    else:
        visible_lines = lines
        scroll_indicator = "[Esc close]"

    custom_title = getattr(state, "command_dialog_title", None)
    if (
        custom_title
        and not custom_title.startswith("Command: :")
        and not custom_title.startswith("Command: ")
    ):
        title_str = custom_title
    else:
        title_str = get_command_friendly_name(cmd)

    title_text = Text()
    title_text.append(f" {title_str} ", style="bold #f8fafc")

    subtitle_text = Text(f" {scroll_indicator} ", style="dim #94a3b8")

    inner_content = Text("\n".join(visible_lines), no_wrap=False)

    return Panel(
        inner_content,
        title=title_text,
        title_align="left",
        subtitle=subtitle_text,
        subtitle_align="right",
        border_style="#38bdf8",
        box=box.ROUNDED,
        width=width,
    )


def render_command_dialog_str(
    state: Any,
    width: int = 80,
    height: int | None = None,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render command dialog as plain or ANSI-styled string using in-memory capture."""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    panel = render_command_dialog(state, width=width, height=height)
    console.print(panel)
    return console.export_text(styles=use_ansi).rstrip("\n")


__all__ = [
    "compute_command_dialog_height",
    "render_command_dialog",
    "render_command_dialog_str",
]
