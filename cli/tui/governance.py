"""Contextual Governance Panels & HITL Plan Surfaces for StackMind TUI (WO-032 / Phase 4).

Renders on-demand governance surfaces without permanent dashboard clutter:
- 6D Verification Matrix (:matrix)
- Contract Boundary HUD (:contract / :status)
- HITL Plan Approval Surface (:plan, :approve, :reject)
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, Mapping

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from cli.tui.state import AutonomousDeliveryState


VERIFICATION_DIMENSIONS = (
    ("Scope", "scope", "Graph & file access within contract boundary"),
    ("State", "state", "Workspace git & repository clean state"),
    ("AST", "ast", "Syntactic & AST invariant verification"),
    ("Behavioral", "behavioral", "Automated test pass & behavioral guarantees"),
    ("Security", "security", "Security policies, secrets & sandbox enforcement"),
    ("Outcome", "outcome", "Work order deliverables and exit criteria met"),
)


def render_verification_matrix(
    dimensions: Mapping[str, bool] | None = None,
    title: str = "6D VERIFICATION MATRIX",
) -> Panel:
    """Render the 6-dimensional verification matrix in a clean Rich panel."""
    dims = dict(dimensions) if dimensions else {
        "scope": True,
        "state": True,
        "ast": True,
        "behavioral": True,
        "security": True,
        "outcome": True,
    }

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=2)
    grid.add_column(ratio=3)
    grid.add_column(justify="right", ratio=1)

    all_pass = True
    for label, key, desc in VERIFICATION_DIMENSIONS:
        passed = bool(dims.get(key, dims.get(label.lower(), False)))
        if not passed:
            all_pass = False

        status_text = "PASS" if passed else "FAIL"
        status_style = "bold green" if passed else "bold red"
        glyph = "✓" if passed else "✗"
        glyph_style = "bold green" if passed else "bold red"

        # Col 1: Label and Status guaranteeing exact substring "Label: PASS"
        col1 = Text()
        col1.append(f"{label}: ", style="bold white")
        col1.append(status_text, style=status_style)

        # Col 2: Description
        col2 = Text(f"({desc})", style="dim")

        # Col 3: Icon
        col3 = Text(glyph, style=glyph_style)

        grid.add_row(col1, col2, col3)

    border_color = "green" if all_pass else "red"
    header_title = Text(" ", style="dim")
    header_title.append(title, style=f"bold {border_color}")
    header_title.append(" ", style="dim")

    summary_text = Text()
    if all_pass:
        summary_text.append("\n✓ All 6 verification dimensions passed. Governed write verified.", style="green dim")
    else:
        summary_text.append("\n✗ One or more verification gates failed. Check logs before commit.", style="red dim")

    body = Group(grid, summary_text)

    return Panel(
        body,
        title=header_title,
        title_align="left",
        box=box.ROUNDED,
        border_style=border_color,
        padding=(0, 1),
    )


def render_verification_matrix_str(
    dimensions: Mapping[str, bool] | None = None, width: int = 80
) -> str:
    """Render 6D verification matrix as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_verification_matrix(dimensions))
    return console.export_text().rstrip()


def render_contract_hud(
    contract: Mapping[str, Any], title: str = "CONTRACT BOUNDARY HUD"
) -> Panel:
    """Render contract scope, write mode, and deny boundaries in a clean panel."""
    write_mode = contract.get("write_mode", "governed")
    allow = contract.get("allow", [])
    deny = contract.get("deny", [])
    governance = contract.get("governance", [])
    budget = contract.get("budget", {})

    lines: list[RenderableType] = []

    # Write mode
    mode_text = Text("Write Mode: ", style="bold white")
    mode_text.append(str(write_mode).upper(), style="bold green" if write_mode == "governed" else "yellow")
    lines.append(mode_text)

    # Allowed paths
    lines.append(Text(""))
    lines.append(Text("Allowed Scope:", style="bold cyan"))
    if allow:
        for entry in allow:
            if isinstance(entry, dict):
                path = entry.get("path", "")
                ops = ", ".join(entry.get("ops", []))
                lines.append(Text(f"  • {path} ", style="white").append(f"[{ops}]", style="dim"))
            else:
                lines.append(Text(f"  • {entry}", style="white"))
    else:
        lines.append(Text("  (None specified)", style="dim italic"))

    # Denied paths
    lines.append(Text(""))
    lines.append(Text("Denied Boundaries (Fail-Closed):", style="bold red"))
    if deny:
        for entry in deny:
            if isinstance(entry, dict):
                path = entry.get("path", "")
                reason = entry.get("reason", "")
                reason_str = f" — {reason}" if reason else ""
                lines.append(Text(f"  ✗ {path}", style="red").append(reason_str, style="dim"))
            else:
                lines.append(Text(f"  ✗ {entry}", style="red"))
    else:
        lines.append(Text("  (Standard root-level invariants apply)", style="dim italic"))

    # Budget & Governance
    extras: list[str] = []
    if budget:
        tok = budget.get("max_tokens")
        files = budget.get("max_files_touched")
        tim = budget.get("max_time_minutes")
        b_parts = []
        if tok:
            b_parts.append(f"{tok} tokens")
        if files:
            b_parts.append(f"max {files} files")
        if tim:
            b_parts.append(f"{tim}m limit")
        if b_parts:
            extras.append(f"Budget: {', '.join(b_parts)}")

    if governance:
        extras.append(f"Rules: {', '.join(governance)}")

    if extras:
        lines.append(Text(""))
        lines.append(Text(" | ".join(extras), style="dim"))

    header_title = Text(" [CONTRACT BOUNDARY HUD] ", style="bold cyan")

    return Panel(
        Group(*lines),
        title=header_title,
        title_align="left",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
    )


def render_contract_hud_str(contract: Mapping[str, Any], width: int = 80) -> str:
    """Render contract HUD as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_contract_hud(contract))
    return console.export_text().rstrip()


def render_plan_panel(state: AutonomousDeliveryState) -> Panel:
    """Render the HITL Plan Approval surface (:plan command) in a clean panel."""
    plan = state.plan
    plan_id = plan.get("plan_id") or plan.get("id") or "PLAN.md"
    title = plan.get("title", "Autonomous Delivery Plan")
    status = plan.get("state") or ("AWAITING_APPROVAL" if state.phase.value == "AWAITING_APPROVAL" else "DRAFT")
    metadata = plan.get("metadata", {})

    status_color = "yellow" if status == "AWAITING_APPROVAL" else ("green" if status == "APPROVED" else "white")

    lines: list[RenderableType] = []

    # Header info
    header = Text("Plan: ", style="bold white")
    header.append(f"{plan_id} — {title}", style="bold cyan")
    lines.append(header)

    stat_line = Text("Status: ", style="bold white")
    stat_line.append(status, style=f"bold {status_color}")
    lines.append(stat_line)

    # Work Decomposition
    lines.append(Text(""))
    lines.append(Text("WORK DECOMPOSITION:", style="bold white"))
    wos = metadata.get("work_orders") or [
        {"id": wo.id, "title": wo.title, "role": wo.role} for wo in state.work_orders
    ]
    for w in wos:
        if isinstance(w, dict):
            wid = w.get("id", "WO")
            wtitle = w.get("title", "")
            wrole = w.get("role", "Worker")
            wline = Text(f"  • {wid}: ", style="bold cyan")
            wline.append(wtitle, style="white")
            wline.append(f" (Role: {wrole})", style="dim")
            lines.append(wline)
        else:
            lines.append(Text(f"  • {w}", style="white"))

    # Revision History
    if state.plan_revisions:
        lines.append(Text(""))
        lines.append(Text("REVISION HISTORY:", style="bold white"))
        for rev in state.plan_revisions:
            fb = f" — Feedback: '{rev.feedback}'" if rev.feedback else ""
            lines.append(Text(f"  [Rev {rev.revision}] {rev.state}{fb} ({rev.timestamp})", style="dim"))

    # Controls
    lines.append(Text(""))
    if status == "AWAITING_APPROVAL" or state.phase.value == "AWAITING_APPROVAL":
        ctrl = Text("HITL CONTROLS: ", style="bold yellow")
        ctrl.append("[Approve: :approve [reason]]  ", style="bold green")
        ctrl.append("[Reject: :reject [feedback]]  ", style="bold red")
        ctrl.append("[Inspect: :plan]", style="dim cyan")
        lines.append(ctrl)
    elif status == "APPROVED":
        lines.append(Text("STATUS: Approved by operator. Multi-agent execution authorized.", style="bold green"))
    elif status == "REJECTED":
        lines.append(Text("STATUS: Rejected by operator. Awaiting revised plan from Architecture agent.", style="bold red"))

    panel_title = Text(" PLAN READY ", style="bold yellow" if status == "AWAITING_APPROVAL" else "bold cyan")

    return Panel(
        Group(*lines),
        title=panel_title,
        title_align="left",
        box=box.ROUNDED,
        border_style="yellow" if status == "AWAITING_APPROVAL" else "cyan",
        padding=(0, 1),
    )


def render_plan_panel_str(state: AutonomousDeliveryState, width: int = 80) -> str:
    """Render HITL plan panel as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_plan_panel(state))
    return console.export_text().rstrip()


__all__ = [
    "VERIFICATION_DIMENSIONS",
    "render_contract_hud",
    "render_contract_hud_str",
    "render_plan_panel",
    "render_plan_panel_str",
    "render_verification_matrix",
    "render_verification_matrix_str",
]
