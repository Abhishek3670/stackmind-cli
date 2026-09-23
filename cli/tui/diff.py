"""Inline Unified Diff Viewer for StackMind TUI (WO-031 / Phase 3).

Renders file diffs inline directly in the chat and activity streams with Rich:
- + additions in soft green
- - deletions in soft red
- @@ chunk headers in cyan/dim
- Clean file path headers with rounded panels
"""

from __future__ import annotations

import io
import re
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


def _clean_path(raw_path: str) -> str:
    path = raw_path.strip()
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def parse_unified_diff(diff_text: str) -> list[dict[str, Any]]:
    """Parse unified diff text into distinct per-file records."""
    cleaned = diff_text.strip()
    if not cleaned or cleaned.startswith("No staged daemon diff"):
        return []

    lines = diff_text.splitlines()
    files: list[dict[str, Any]] = []
    current_file: dict[str, Any] | None = None

    file_header_re = re.compile(r"^(?:diff --git a/(\S+) b/(\S+)|--- (?:a/)?(\S+)|--- (\S+))")
    plus_header_re = re.compile(r"^\+\+\+ (?:b/)?(\S+)")

    for line in lines:
        if line.startswith("diff --git "):
            parts = line.split()
            path = _clean_path(parts[2]) if len(parts) >= 3 else "unknown"
            if current_file:
                files.append(current_file)
            current_file = {"path": path, "lines": []}
            continue

        if line.startswith("--- "):
            m = re.match(r"^--- (?:a/)?(\S+)", line)
            path = m.group(1) if m else "diff"
            path = _clean_path(path)
            if not current_file:
                current_file = {"path": path, "lines": []}
            elif current_file["path"] in {"diff", "unknown", "/dev/null"}:
                current_file["path"] = path
            current_file["lines"].append(("header", line))
            continue

        if line.startswith("+++ "):
            m = plus_header_re.match(line)
            path = m.group(1) if m else None
            if path and path != "/dev/null":
                path = _clean_path(path)
                if current_file:
                    current_file["path"] = path
                else:
                    current_file = {"path": path, "lines": []}
            if current_file:
                current_file["lines"].append(("header", line))
            continue

        if not current_file:
            current_file = {"path": "staged modifications", "lines": []}

        if line.startswith("@@"):
            current_file["lines"].append(("chunk", line))
        elif line.startswith("+"):
            current_file["lines"].append(("add", line))
        elif line.startswith("-"):
            current_file["lines"].append(("del", line))
        else:
            current_file["lines"].append(("ctx", line))

    if current_file:
        files.append(current_file)

    return files


def render_file_diff(file_path: str, lines: list[tuple[str, str]] | list[str] | str) -> Panel:
    """Render a single file's diff in an inline styled Rich panel."""
    content_text = Text()

    normalized_lines: list[tuple[str, str]] = []
    if isinstance(lines, str):
        for raw_line in lines.splitlines():
            if raw_line.startswith("@@"):
                normalized_lines.append(("chunk", raw_line))
            elif raw_line.startswith("+") and not raw_line.startswith("+++"):
                normalized_lines.append(("add", raw_line))
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                normalized_lines.append(("del", raw_line))
            elif raw_line.startswith("---") or raw_line.startswith("+++"):
                normalized_lines.append(("header", raw_line))
            else:
                normalized_lines.append(("ctx", raw_line))
    elif lines and isinstance(lines[0], str):
        for raw_line in lines:  # type: ignore[union-attr]
            if raw_line.startswith("@@"):
                normalized_lines.append(("chunk", raw_line))
            elif raw_line.startswith("+") and not raw_line.startswith("+++"):
                normalized_lines.append(("add", raw_line))
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                normalized_lines.append(("del", raw_line))
            elif raw_line.startswith("---") or raw_line.startswith("+++"):
                normalized_lines.append(("header", raw_line))
            else:
                normalized_lines.append(("ctx", raw_line))
    else:
        normalized_lines = lines  # type: ignore[assignment]

    for kind, text_val in normalized_lines:
        if kind == "chunk":
            content_text.append(text_val + "\n", style="cyan dim")
        elif kind == "add":
            content_text.append(text_val + "\n", style="green")
        elif kind == "del":
            content_text.append(text_val + "\n", style="red")
        elif kind == "header":
            content_text.append(text_val + "\n", style="dim white")
        else:
            content_text.append(text_val + "\n", style="white")

    # Remove trailing newline from Text renderable
    if content_text.plain.endswith("\n"):
        content_text.plain = content_text.plain[:-1]

    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=3)
    grid.add_column(justify="right", ratio=1)

    left_hdr = Text(file_path, style="bold cyan")
    right_hdr = Text("unified diff", style="dim")
    grid.add_row(left_hdr, right_hdr)

    rule = Rule(style="dim #334155")
    body = Group(grid, rule, content_text)

    return Panel(
        body,
        box=box.ROUNDED,
        border_style="#334155",
        padding=(0, 1),
    )


def render_unified_diff(diff_text: str, title: str | None = None) -> RenderableType:
    """Render unified diff content as inline Rich panels."""
    parsed_files = parse_unified_diff(diff_text)
    if not parsed_files:
        msg = diff_text.strip() if diff_text.strip() else "(No staged modifications or diff available)"
        return Panel(
            Text(msg, style="dim italic"),
            title=" Diff " if not title else f" {title} ",
            title_align="left",
            box=box.ROUNDED,
            border_style="#334155",
            padding=(0, 1),
        )

    panels: list[RenderableType] = []
    for f in parsed_files:
        path = title or f["path"]
        panels.append(render_file_diff(path, f["lines"]))
        panels.append(Text(""))

    # Drop trailing spacing
    if panels and isinstance(panels[-1], Text) and not panels[-1].plain:
        panels.pop()

    return Group(*panels) if len(panels) > 1 else panels[0]


def render_unified_diff_str(diff_text: str, width: int = 80, title: str | None = None) -> str:
    """Render unified diff as a plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_unified_diff(diff_text, title=title))
    return console.export_text().rstrip()


__all__ = [
    "parse_unified_diff",
    "render_file_diff",
    "render_unified_diff",
    "render_unified_diff_str",
]
