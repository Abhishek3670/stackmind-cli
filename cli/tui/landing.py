"""Branded landing block for the StackMind TUI (Phase 2 & WO-034 Visual Fidelity)."""

from __future__ import annotations

import io
from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import cli

LANDING_DIAMOND = "✦"
LANDING_NAME = "StackMind"
LANDING_TAGLINE = "Your AI development partner, with control."
LANDING_MOTTO = "Build better. Safer. Together."
LANDING_PILLARS = ("PLAN", "BUILD", "VERIFY", "GOVERN")
LANDING_HINTS = [
    ("/", "Start chatting"),
    ("Ctrl+K", "Open commands"),
    (":help", "Show all commands"),
    (":status", "Show session status"),
]


def render_landing_block(
    version: str | None = None, width: int | None = None
) -> RenderableType:
    """Render the OpenCode-inspired minimal StackMind landing card.

    Features a rounded bordered card, glowing purple star, gradient branding,
    version, tagline, horizontal rule divider, multi-colored pillars,
    bordered keyboard pill button badges, and bottom motto matching image.png.
    """
    pkg_version = version if version is not None else cli.__version__
    ver_str = f"v{pkg_version}" if not str(pkg_version).startswith("v") else str(pkg_version)

    # 1. Glowing purple diamond star
    diamond = Text(LANDING_DIAMOND, style="bold #a855f7", justify="center")

    # 2. Gradient styled brand title: #38bdf8 -> #c084fc
    gradient_palette = [
        "#38bdf8", "#38bdf8", "#60a5fa", "#60a5fa", "#818cf8",
        "#a855f7", "#a855f7", "#c084fc", "#c084fc",
    ]
    title = Text(justify="center")
    for ch, col in zip(LANDING_NAME, gradient_palette):
        title.append(ch, style=f"bold {col}")

    # 3. Version and tagline
    version_text = Text(ver_str, style="dim white", justify="center")
    tagline_text = Text(LANDING_TAGLINE, style="white", justify="center")

    # 4. Subtle horizontal rule divider
    divider = Rule(style="dim #334155")

    # 5. Multi-colored pillars with dots
    pillars_text = Text(justify="center")
    pillars_text.append("PLAN", style="bold #38bdf8")
    pillars_text.append(" · ", style="dim #475569")
    pillars_text.append("BUILD", style="bold #60a5fa")
    pillars_text.append(" · ", style="dim #475569")
    pillars_text.append("VERIFY", style="bold #a855f7")
    pillars_text.append(" · ", style="dim #475569")
    pillars_text.append("GOVERN", style="bold #c084fc")

    # 6. Pill button badges for keyboard shortcuts
    pill_colors = ["#38bdf8", "#60a5fa", "#a855f7", "#c084fc"]
    if width is not None and width < 60:
        hints_table = Table.grid(padding=(0, 2))
        hints_table.add_column(justify="right")
        hints_table.add_column(justify="left")
        for i, (key, desc) in enumerate(LANDING_HINTS):
            color = pill_colors[i % len(pill_colors)]
            badge = Text("[ ", style="dim #475569").append(key, style=f"bold {color}").append(" ]", style="dim #475569")
            hints_table.add_row(badge, Text(desc, style="white"))
    else:
        hints_table = Table.grid(padding=(0, 2))
        hints_table.add_column(justify="right")
        hints_table.add_column(justify="left")
        hints_table.add_column(justify="right")
        hints_table.add_column(justify="left")

        b0 = Text("[ ", style="dim #475569").append(LANDING_HINTS[0][0], style=f"bold {pill_colors[0]}").append(" ]", style="dim #475569")
        b1 = Text("[ ", style="dim #475569").append(LANDING_HINTS[1][0], style=f"bold {pill_colors[1]}").append(" ]", style="dim #475569")
        b2 = Text("[ ", style="dim #475569").append(LANDING_HINTS[2][0], style=f"bold {pill_colors[2]}").append(" ]", style="dim #475569")
        b3 = Text("[ ", style="dim #475569").append(LANDING_HINTS[3][0], style=f"bold {pill_colors[3]}").append(" ]", style="dim #475569")

        hints_table.add_row(b0, Text(LANDING_HINTS[0][1], style="white"), b1, Text(LANDING_HINTS[1][1], style="white"))
        hints_table.add_row(b2, Text(LANDING_HINTS[2][1], style="white"), b3, Text(LANDING_HINTS[3][1], style="white"))

    centered_hints = Align.center(hints_table)
    motto_text = Text(LANDING_MOTTO, style="dim white", justify="center")

    # 7. Card container
    card_group = Group(
        diamond,
        Text(""),
        title,
        version_text,
        Text(""),
        tagline_text,
        divider,
        pillars_text,
        Text(""),
        centered_hints,
        Text(""),
        motto_text,
    )

    return Panel(
        card_group,
        box=box.ROUNDED,
        border_style="#2563eb",
        padding=(1, 2),
    )


def render_landing_block_str(version: str | None = None, width: int = 80) -> str:
    """Render the landing block as plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_landing_block(version=version, width=width))
    return console.export_text().rstrip()


__all__ = [
    "LANDING_DIAMOND",
    "LANDING_HINTS",
    "LANDING_MOTTO",
    "LANDING_NAME",
    "LANDING_PILLARS",
    "LANDING_TAGLINE",
    "render_landing_block",
    "render_landing_block_str",
]
