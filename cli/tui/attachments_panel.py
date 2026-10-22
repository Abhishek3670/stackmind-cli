"""TUI rendering panels for Multi-Modal Attachments & Ingestion (Pillar 4)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from validators.ingest.models import AttachmentRecord, AttachmentType


def render_attachment_ingested(record: AttachmentRecord) -> Panel:
    """Render confirmation panel for a newly ingested attachment."""
    lines = [
        Text(f"Name: {record.original_filename} (ID: {record.attachment_id})", style="bold white"),
        Text(f"Type: {record.attachment_type.value.upper()} | Size: {record.size_bytes / 1024:.1f} KB", style="cyan"),
        Text(f"Summary: {record.summary}", style="white"),
        Text(f"Raw File: {record.raw_relpath}", style="dim"),
    ]
    if record.extracted_relpath:
        lines.append(Text(f"Extracted: {record.extracted_relpath}", style="dim green"))

    lines.append(Text(""))
    lines.append(Text("✓ Verified and indexed into workspace grounding context for multi-agent reasoning.", style="bold green"))

    title = Text(" ATTACHMENT INGESTED ", style="bold green")
    return Panel(
        Group(*lines),
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style="green",
        padding=(0, 1),
    )


def render_attachment_ingested_str(record: AttachmentRecord, width: int = 80) -> str:
    """Render ingested attachment confirmation as string."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_attachment_ingested(record))
    return console.export_text().rstrip()


def render_attachments_list(records: list[AttachmentRecord]) -> Panel:
    """Render table of all tracked attachments."""
    title = Text(" WORKSPACE ATTACHMENTS & GROUNDING ASSETS (:attachments) ", style="bold cyan")

    if not records:
        msg = Text()
        msg.append("\nNo attachments registered in `.sync/attachments/`.\n", style="yellow")
        msg.append("Attach documents, images, or archives for multi-agent grounding using:\n", style="dim")
        msg.append("  :attach <file_or_dir_path>\n", style="bold white")
        msg.append("Supported formats: PDF, DOCX, MD, TXT, CSV, PNG, JPG, WEBP, ZIP, TAR.GZ\n", style="dim")
        return Panel(
            msg,
            title=title,
            title_align="left",
            box=box.ROUNDED,
            border_style="cyan",
            padding=(0, 1),
        )

    table = Table(box=box.SIMPLE, expand=True, padding=(0, 1))
    table.add_column("ID", style="bold cyan", no_wrap=True)
    table.add_column("Filename", style="bold white")
    table.add_column("Type", style="bold")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Summary", style="white")

    for r in records:
        type_style = (
            "green"
            if r.attachment_type == AttachmentType.DOCUMENT
            else ("yellow" if r.attachment_type == AttachmentType.IMAGE else "magenta")
        )
        size_str = f"{r.size_bytes / 1024:.1f} KB" if r.size_bytes < 1024 * 1024 else f"{r.size_bytes / (1024*1024):.1f} MB"
        table.add_row(
            r.attachment_id,
            r.original_filename,
            Text(r.attachment_type.value.upper(), style=type_style),
            size_str,
            r.summary or "-",
        )

    summary_text = Text(
        f"\nTotal: {len(records)} attachment(s). Grounding automatically available to Architecture & Worker agents.",
        style="dim",
    )
    return Panel(
        Group(table, summary_text),
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
    )


def render_attachments_list_str(records: list[AttachmentRecord], width: int = 80) -> str:
    """Render attachments table as string."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_attachments_list(records))
    return console.export_text().rstrip()


def render_attachment_detail_str(record: AttachmentRecord, width: int = 80) -> str:
    """Render detailed view of an attachment."""
    lines = [
        Text(f"Attachment: {record.original_filename} ({record.attachment_id})", style="bold white"),
        Text(f"Type: {record.attachment_type.value.upper()} | SHA-256: {record.sha256[:16]}...", style="dim"),
        Text(f"Summary: {record.summary}\n", style="cyan"),
    ]
    if record.text_content:
        lines.append(Text("Extracted Content Preview:", style="bold white"))
        snippet = record.text_content.strip()
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "\n...(preview truncated)..."
        lines.append(Text(snippet, style="dim"))

    panel = Panel(
        Group(*lines),
        title=Text(f" ATTACHMENT: {record.original_filename} ", style="bold cyan"),
        title_align="left",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
    )
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(panel)
    return console.export_text().rstrip()
