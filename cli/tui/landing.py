"""Branded landing block for the StackMind TUI (Phase 2)."""

from __future__ import annotations

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

import cli

LANDING_DIAMOND = "✦"
LANDING_NAME = "StackMind"
LANDING_TAGLINE = "Your AI development partner, with control."
LANDING_PILLARS = ("PLAN", "BUILD", "VERIFY", "GOVERN")
LANDING_HINTS = [
    ("/", "Start chatting"),
    ("Ctrl+K", "Commands"),
    (":help", "Show available commands"),
    (":status", "Show session status"),
]


def render_landing_block(
    version: str | None = None, width: int | None = None
) -> RenderableType:
    """Render the OpenCode-inspired minimal StackMind landing block.

    Supports responsive degradation: adapts hints table to 2 columns on
    narrow displays (width < 60) without breaking diamond or header layout.
    """
    pkg_version = version if version is not None else cli.__version__
    ver_str = f"v{pkg_version}" if not str(pkg_version).startswith("v") else str(pkg_version)

    # Branding header
    diamond = Text(LANDING_DIAMOND, style="bold cyan", justify="center")
    title = Text(LANDING_NAME, style="bold white", justify="center")
    version_text = Text(ver_str, style="dim cyan", justify="center")
    tagline_text = Text(LANDING_TAGLINE, style="italic white", justify="center")
    pillars_text = Text(" · ".join(LANDING_PILLARS), style="bold cyan", justify="center")

    # Responsive static input hints table
    if width is not None and width < 60:
        hints_table = Table.grid(padding=(0, 2))
        hints_table.add_column(justify="left", style="bold cyan")
        hints_table.add_column(justify="left", style="dim white")
        for key, desc in LANDING_HINTS:
            hints_table.add_row(key, desc)
    else:
        hints_table = Table.grid(padding=(0, 3))
        hints_table.add_column(justify="left", style="bold cyan")
        hints_table.add_column(justify="left", style="dim white")
        hints_table.add_column(justify="left", style="bold cyan")
        hints_table.add_column(justify="left", style="dim white")
        hints_table.add_row(
            LANDING_HINTS[0][0], LANDING_HINTS[0][1],
            LANDING_HINTS[1][0], LANDING_HINTS[1][1],
        )
        hints_table.add_row(
            LANDING_HINTS[2][0], LANDING_HINTS[2][1],
            LANDING_HINTS[3][0], LANDING_HINTS[3][1],
        )

    centered_hints = Align.center(hints_table)

    return Group(
        Text(""),
        diamond,
        Text(""),
        title,
        version_text,
        Text(""),
        tagline_text,
        Text(""),
        pillars_text,
        Text(""),
        centered_hints,
        Text(""),
    )


def render_landing_block_str(version: str | None = None, width: int = 80) -> str:
    """Render the landing block as plain formatted string."""
    console = Console(record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_landing_block(version=version, width=width))
    return console.export_text().rstrip()


__all__ = [
    "LANDING_DIAMOND",
    "LANDING_HINTS",
    "LANDING_NAME",
    "LANDING_PILLARS",
    "LANDING_TAGLINE",
    "render_landing_block",
    "render_landing_block_str",
]
