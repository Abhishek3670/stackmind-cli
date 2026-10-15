"""Inline Unified & Side-by-Side (Split) Diff Viewer for StackMind TUI.

Provides modern Side-by-Side (Split) diff rendering with full-row background highlights:
- Left column (Old/Removed): Line numbers in gutter, '-' sign in soft red, full-width #3b1d26 maroon background
- Right column (New/Added): Line numbers in gutter, '+' sign in soft green/teal, full-width #16332e teal background
- Context lines: Line numbers in dim gray (#6b7280), soft off-white code (#d1d5db) with syntax highlighting
- Automatic responsive fallback to single-column unified diff when avail_width < 90
- Preserves backward compatibility with legacy unified diff panel renderers
"""

from __future__ import annotations

import io
import itertools
import re
from typing import Any, NamedTuple

from rich import box
from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


class DiffCell(NamedTuple):
    """A single cell in a side-by-side diff row."""

    line_no: int | None
    sign: str
    text: str
    kind: str = "ctx"  # "ctx", "del", "add", "empty", "gap"


class SplitRow(NamedTuple):
    """A paired horizontal row in a side-by-side diff."""

    left: DiffCell
    right: DiffCell


class SplitDiffText(str):
    """String subclass that renders with rich ANSI color highlighting in Rich consoles."""

    def __new__(cls, plain: str, rich_renderable: RenderableType | None = None):
        obj = super().__new__(cls, plain)
        obj._rich_renderable = rich_renderable
        return obj

    def __rich__(self) -> RenderableType:
        if self._rich_renderable is not None:
            return self._rich_renderable
        return Text(str(self))


def truncate_cell(text: str, max_w: int) -> str:
    """Truncate text to fit within max_w terminal cells using an ellipsis."""
    if max_w <= 0:
        return ""
    if cell_len(text) <= max_w:
        return text
    target_w = max_w - 1
    if target_w <= 0:
        return "…"[:max_w]
    truncated = ""
    curr_w = 0
    for ch in text:
        ch_w = cell_len(ch)
        if curr_w + ch_w > target_w:
            break
        truncated += ch
        curr_w += ch_w
    return truncated + "…"


def highlight_code_line(text: str, base_style: str = "#d1d5db", bg_style: str = "") -> Text:
    """Apply lightweight syntax highlighting to code tokens while preserving background tint."""
    full_style = f"{base_style} {bg_style}".strip()
    t = Text(text, style=full_style)

    # Keywords
    kw_style = f"#c678dd {bg_style}".strip()
    t.highlight_regex(
        r"\b(import|export|from|def|class|return|const|let|var|if|else|elif|for|while|try|except|finally|async|await|function|interface|type)\b",
        kw_style,
    )
    # JSX / HTML tags
    tag_style = f"#e06c75 {bg_style}".strip()
    t.highlight_regex(r"(</?[a-zA-Z0-9_\-]+|/?>)", tag_style)
    # Numbers and properties / attributes
    num_style = f"#d19a66 {bg_style}".strip()
    t.highlight_regex(r"\b\d+\b", num_style)
    t.highlight_regex(r"\b[a-zA-Z_][a-zA-Z0-9_]*=", num_style)
    # Strings
    str_style = f"#98c379 {bg_style}".strip()
    t.highlight_regex(r"('[^']*'|\"[^\"]*\"|`[^`]*`)", str_style)

    return t


def parse_unified_diff_to_split_rows(diff_text: str) -> list[SplitRow]:
    """Parse standard unified diff hunks into aligned (left_row, right_row) pairs.

    - Context lines (' '): emit (DiffCell(old_no, ' ', text), DiffCell(new_no, ' ', text)).
    - Contiguous '-' and '+' runs: zipped together with itertools.zip_longest so modified
      lines appear on the exact same horizontal row. Unpaired lines receive empty filler cells.
    - Hunk gaps: jump line numbers cleanly without printing raw @@ noise.
    """
    if not diff_text or not diff_text.strip():
        return []

    lines = diff_text.splitlines()
    rows: list[SplitRow] = []
    removed_block: list[DiffCell] = []
    added_block: list[DiffCell] = []
    old_no: int | None = None
    new_no: int | None = None

    def flush_blocks():
        nonlocal removed_block, added_block
        if not removed_block and not added_block:
            return
        for rem, add in itertools.zip_longest(removed_block, added_block, fillvalue=None):
            left_cell = rem if rem is not None else DiffCell(line_no=None, sign=" ", text="", kind="empty")
            right_cell = add if add is not None else DiffCell(line_no=None, sign=" ", text="", kind="empty")
            rows.append(SplitRow(left=left_cell, right=right_cell))
        removed_block = []
        added_block = []

    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for raw_line in lines:
        if raw_line.startswith("diff --git ") or raw_line.startswith("index "):
            flush_blocks()
            continue
        if raw_line.startswith("--- ") or raw_line.startswith("+++ "):
            flush_blocks()
            continue
        m = hunk_re.match(raw_line)
        if m:
            flush_blocks()
            old_no = int(m.group(1))
            new_no = int(m.group(2))
            continue
        if raw_line.startswith(r"\ No newline"):
            continue

        if raw_line.startswith("-"):
            if old_no is None:
                old_no = 1
                new_no = 1
            # Strip synthetic single space from extract_tool_diff if present (avoid stripping code indent)
            if raw_line.startswith("- ") and not raw_line.startswith("-  "):
                code = raw_line[2:]
            else:
                code = raw_line[1:]
            removed_block.append(DiffCell(line_no=old_no, sign="-", text=code, kind="del"))
            old_no += 1
        elif raw_line.startswith("+"):
            if new_no is None:
                old_no = 1
                new_no = 1
            if raw_line.startswith("+ ") and not raw_line.startswith("+  "):
                code = raw_line[2:]
            else:
                code = raw_line[1:]
            added_block.append(DiffCell(line_no=new_no, sign="+", text=code, kind="add"))
            new_no += 1
        else:
            flush_blocks()
            code = raw_line[1:] if raw_line.startswith(" ") else raw_line
            if old_no is None and new_no is None:
                old_no = 1
                new_no = 1
            rows.append(
                SplitRow(
                    left=DiffCell(line_no=old_no, sign=" ", text=code, kind="ctx"),
                    right=DiffCell(line_no=new_no, sign=" ", text=code, kind="ctx"),
                )
            )
            if old_no is not None:
                old_no += 1
            if new_no is not None:
                new_no += 1

    flush_blocks()
    return rows


def render_split_diff(
    diff_text: str,
    avail_width: int = 80,
    max_lines: int = 18,
    file_path: str = "",
    indent: str = "",
) -> SplitDiffText:
    """Render a side-by-side or single-column diff with full-width background highlights.

    - avail_width >= 90: Side-by-Side mode with two equal columns and full-column background tints.
    - avail_width < 90: Responsive Single-Column fallback with line numbers and full-row tints.
    """
    clean_diff = diff_text.strip()
    if not clean_diff or clean_diff.startswith("No staged daemon diff"):
        msg = f"{indent}(No diff available)" if not clean_diff else f"{indent}{clean_diff}"
        return SplitDiffText(msg, Text(msg, style="dim italic"))

    split_rows = parse_unified_diff_to_split_rows(diff_text)
    if not split_rows:
        msg = f"{indent}(No diff available)"
        return SplitDiffText(msg, Text(msg, style="dim italic"))

    indent_len = cell_len(indent)
    usable_w = max(20, avail_width - indent_len)
    rich_lines: list[Text] = []

    if avail_width >= 90 and usable_w >= 80:
        # Side-by-Side Mode (avail_width >= 90)
        col_w = (usable_w - 2) // 2
        code_w = max(6, col_w - 6)

        for row in split_rows:
            # Left column (Deletions / Old context / Filler)
            left = row.left
            l_num_str = f"{left.line_no:>3}" if left.line_no is not None else "   "
            l_gutter = f"{l_num_str} {left.sign} "
            l_trunc = truncate_cell(left.text, code_w)
            l_pad = max(0, col_w - cell_len(l_gutter) - cell_len(l_trunc))
            l_code = l_trunc + (" " * l_pad)

            l_text = Text()
            if left.kind == "del":
                l_text.append(f"{l_num_str} ", style="#9ca3af on #3b1d26")
                l_text.append("- ", style="bold #f87171 on #3b1d26")
                l_text.append_text(highlight_code_line(l_code, base_style="#fca5a5", bg_style="on #3b1d26"))
            elif left.kind == "ctx":
                l_text.append(f"{l_num_str}   ", style="#6b7280")
                l_text.append_text(highlight_code_line(l_code, base_style="#d1d5db"))
            else:  # empty filler
                l_text.append(" " * col_w)

            # Right column (Additions / New context / Filler)
            right = row.right
            r_num_str = f"{right.line_no:>3}" if right.line_no is not None else "   "
            r_gutter = f"{r_num_str} {right.sign} "
            r_trunc = truncate_cell(right.text, code_w)
            r_pad = max(0, col_w - cell_len(r_gutter) - cell_len(r_trunc))
            r_code = r_trunc + (" " * r_pad)

            r_text = Text()
            if right.kind == "add":
                r_text.append(f"{r_num_str} ", style="#9ca3af on #16332e")
                r_text.append("+ ", style="bold #34d399 on #16332e")
                r_text.append_text(highlight_code_line(r_code, base_style="#a7f3d0", bg_style="on #16332e"))
            elif right.kind == "ctx":
                r_text.append(f"{r_num_str}   ", style="#6b7280")
                r_text.append_text(highlight_code_line(r_code, base_style="#d1d5db"))
            else:  # empty filler
                r_text.append(" " * col_w)

            row_line = Text(indent) if indent else Text()
            row_line.append_text(l_text)
            row_line.append("  ")  # 2-space column divider
            row_line.append_text(r_text)
            rich_lines.append(row_line)

    else:
        # Responsive Single-Column Fallback (< 90)
        gutter_w = 10
        code_w = max(10, usable_w - gutter_w)

        for row in split_rows:
            left, right = row.left, row.right
            if left.kind == "ctx":
                old_str = f"{left.line_no:>3}" if left.line_no is not None else "   "
                new_str = f"{right.line_no:>3}" if right.line_no is not None else "   "
                gutter = f"{old_str} {new_str}   "
                trunc = truncate_cell(left.text, code_w)
                pad = max(0, usable_w - gutter_w - cell_len(trunc))
                code = trunc + (" " * pad)
                lt = Text(indent) if indent else Text()
                lt.append(gutter, style="#6b7280")
                lt.append_text(highlight_code_line(code, base_style="#d1d5db"))
                rich_lines.append(lt)

            elif left.kind == "del" and right.kind != "add":
                old_str = f"{left.line_no:>3}" if left.line_no is not None else "   "
                gutter_num = f"{old_str}     "
                trunc = truncate_cell(left.text, code_w)
                pad = max(0, usable_w - gutter_w - cell_len(trunc))
                code = trunc + (" " * pad)
                lt = Text(indent) if indent else Text()
                lt.append(gutter_num, style="#9ca3af on #3b1d26")
                lt.append("- ", style="bold #f87171 on #3b1d26")
                lt.append_text(highlight_code_line(code, base_style="#fca5a5", bg_style="on #3b1d26"))
                rich_lines.append(lt)

            elif right.kind == "add" and left.kind != "del":
                new_str = f"{right.line_no:>3}" if right.line_no is not None else "   "
                gutter_num = f"    {new_str} "
                trunc = truncate_cell(right.text, code_w)
                pad = max(0, usable_w - gutter_w - cell_len(trunc))
                code = trunc + (" " * pad)
                lt = Text(indent) if indent else Text()
                lt.append(gutter_num, style="#9ca3af on #16332e")
                lt.append("+ ", style="bold #34d399 on #16332e")
                lt.append_text(highlight_code_line(code, base_style="#a7f3d0", bg_style="on #16332e"))
                rich_lines.append(lt)

            elif left.kind == "del" and right.kind == "add":
                # Paired modification: Deletion line first, then Addition line
                old_str = f"{left.line_no:>3}" if left.line_no is not None else "   "
                gutter_del = f"{old_str}     "
                trunc_del = truncate_cell(left.text, code_w)
                pad_del = max(0, usable_w - gutter_w - cell_len(trunc_del))
                code_del = trunc_del + (" " * pad_del)
                ldel = Text(indent) if indent else Text()
                ldel.append(gutter_del, style="#9ca3af on #3b1d26")
                ldel.append("- ", style="bold #f87171 on #3b1d26")
                ldel.append_text(highlight_code_line(code_del, base_style="#fca5a5", bg_style="on #3b1d26"))
                rich_lines.append(ldel)

                new_str = f"{right.line_no:>3}" if right.line_no is not None else "   "
                gutter_add = f"    {new_str} "
                trunc_add = truncate_cell(right.text, code_w)
                pad_add = max(0, usable_w - gutter_w - cell_len(trunc_add))
                code_add = trunc_add + (" " * pad_add)
                ladd = Text(indent) if indent else Text()
                ladd.append(gutter_add, style="#9ca3af on #16332e")
                ladd.append("+ ", style="bold #34d399 on #16332e")
                ladd.append_text(highlight_code_line(code_add, base_style="#a7f3d0", bg_style="on #16332e"))
                rich_lines.append(ladd)

    # Clamping & expansion hint
    total_lines = len(rich_lines)
    if max_lines and max_lines > 0 and total_lines > max_lines:
        displayed = rich_lines[:max_lines]
        more_count = total_lines - max_lines
        hint_prefix = f"{indent}  " if indent else "  "
        hint = Text(f"{hint_prefix}... (+{more_count} more diff lines, ctrl+o to expand)", style="dim italic #9ca3af")
        displayed.append(hint)
        rich_lines = displayed

    plain_str = "\n".join(rl.plain for rl in rich_lines)
    return SplitDiffText(plain_str, Group(*rich_lines))


def render_split_diff_str(
    diff_text: str,
    avail_width: int = 80,
    max_lines: int = 18,
    file_path: str = "",
    indent: str = "",
) -> str:
    """Render side-by-side or single-column diff as a string."""
    res = render_split_diff(
        diff_text,
        avail_width=avail_width,
        max_lines=max_lines,
        file_path=file_path,
        indent=indent,
    )
    return str(res)


# ---------------------------------------------------------------------------
# Legacy Unified Diff Functions (Preserved for Backward Compatibility)
# ---------------------------------------------------------------------------


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
    "DiffCell",
    "SplitRow",
    "SplitDiffText",
    "highlight_code_line",
    "parse_unified_diff",
    "parse_unified_diff_to_split_rows",
    "render_file_diff",
    "render_split_diff",
    "render_split_diff_str",
    "render_unified_diff",
    "render_unified_diff_str",
]
