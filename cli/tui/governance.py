"""Contextual Governance Panels & HITL Plan Surfaces for StackMind TUI (WO-032 / Phase 4).

Renders on-demand governance surfaces without permanent dashboard clutter:
- 6D Verification Matrix (:matrix)
- Contract Boundary HUD (:contract / :status)
- HITL Plan Approval Surface (:plan, :approve, :reject)
"""

from __future__ import annotations

import io
from pathlib import Path
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
    grid.add_column(ratio=2, no_wrap=True, overflow="ellipsis")
    grid.add_column(ratio=3, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", ratio=1, no_wrap=True)

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
    scope = contract.get("scope", {}) if isinstance(contract.get("scope"), Mapping) else {}
    write_mode = contract.get("write_mode") or scope.get("write") or "governed"
    allow = contract.get("allow") or scope.get("allow", [])
    deny = contract.get("deny") or scope.get("deny", [])
    governance = contract.get("governance") or scope.get("governance", [])
    budget = contract.get("budget") or scope.get("budget", {})

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
                path = entry.get("path") or entry.get("module") or entry.get("target") or ""
                ops = ", ".join(entry.get("ops", []))
                op_str = f" [{ops}]" if ops else ""
                lines.append(Text(f"  • {path}", style="white").append(op_str, style="dim"))
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
                path = entry.get("path") or entry.get("module") or entry.get("target") or ""
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


def render_skills_panel(
    project_path: Path | str | None = None,
    skill_filter: str | None = None,
    include_global: bool = True,
) -> Panel:
    """Render procedural skills overview or specific skill runbook."""
    from validators.skill.store import SkillStore
    from validators.skill.models import SkillStatus

    path = Path(project_path).resolve() if project_path else Path(".").resolve()
    store = SkillStore(path)
    skills = store.list_skills(include_global=include_global)

    if skill_filter:
        filt = skill_filter.strip().lower()
        matched = [s for s in skills if filt in s.name.lower() or filt in s.skill_id.lower()]
        if len(matched) == 1:
            skill = matched[0]
            lines: list[RenderableType] = []
            slug = skill.name.strip().lower()
            is_local = (store.active_dir / f"{slug}.json").is_file()
            scope_badge = "[local]" if is_local else "[global]"
            lines.append(Text(f"Name: {skill.name} (v{skill.version})  {scope_badge}", style="bold white"))
            lines.append(
                Text(
                    f"ID: {skill.skill_id}  |  Status: {skill.status.value.upper()}  |  Risk: {skill.risk_tier.value.upper()}  |  Confidence: {skill.metrics.confidence_score:.2f}",
                    style="dim",
                )
            )
            lines.append(Text(f"Description: {skill.description}\n", style="white"))

            if skill.applicability.preconditions:
                lines.append(Text("Preconditions:", style="bold cyan"))
                for pre in skill.applicability.preconditions:
                    lines.append(Text(f"  • {pre}", style="dim"))
                lines.append(Text(""))

            lines.append(Text("Execution Procedure:", style="bold cyan"))
            for step in skill.steps:
                cmd_str = f" [{step.command_template}]" if step.command_template else ""
                lines.append(
                    Text(f"  {step.step_index}. [{step.tool}] ", style="bold green")
                    .append(step.action, style="white")
                    .append(cmd_str, style="dim")
                )

            if skill.fallback_procedure:
                lines.append(Text(f"\nFallback: {skill.fallback_procedure}", style="yellow dim"))

            return Panel(
                Group(*lines),
                title=Text(f" SKILL: {skill.name} ", style="bold cyan"),
                title_align="left",
                box=box.ROUNDED,
                border_style="cyan",
                padding=(0, 1),
            )
        skills = matched

    title = Text(" AVAILABLE PROCEDURAL SKILLS (:skills) ", style="bold cyan")

    if not skills:
        msg = Text()
        msg.append("\nNo procedural skills found in .sync/skills/ or global library (~/.stackmindcli/skills/global/).\n", style="yellow")
        msg.append("StackMind learns procedural skills from verified execution trajectories.\n", style="dim")
        msg.append("To discover and distill skills, run:\n", style="dim")
        msg.append("  stackmind learn mine --min-support 2\n", style="bold white")
        msg.append("To consume external procedural skills, run:\n", style="dim")
        msg.append("  :consume-skills <path_or_url> [--local]\n", style="bold white")
        return Panel(
            msg,
            title=title,
            title_align="left",
            box=box.ROUNDED,
            border_style="cyan",
            padding=(0, 1),
        )

    table = Table(box=box.SIMPLE, expand=True, padding=(0, 1))
    table.add_column("Skill ID", style="bold cyan", no_wrap=True)
    table.add_column("Name", style="bold white")
    table.add_column("Ver", justify="right", style="dim")
    table.add_column("Status", style="bold")
    table.add_column("Risk", style="bold")
    table.add_column("Conf", justify="right")
    table.add_column("Modules", style="dim")

    n_local = 0
    for s in skills:
        status_style = (
            "green"
            if s.status == SkillStatus.ACTIVE
            else ("yellow" if s.status == SkillStatus.CANDIDATE else "red")
        )
        risk_style = (
            "red"
            if s.risk_tier.value in ("high", "critical")
            else ("yellow" if s.risk_tier.value == "medium" else "green")
        )
        mods = ", ".join(s.applicability.target_modules[:2])
        if len(s.applicability.target_modules) > 2:
            mods += "..."
        slug = s.name.strip().lower()
        if (store.active_dir / f"{slug}.json").is_file():
            n_local += 1
        disp_id = s.skill_id if len(s.skill_id) <= 16 else f"{s.skill_id[:13]}…"
        table.add_row(
            disp_id,
            s.name,
            f"v{s.version}",
            Text(s.status.value.upper(), style=status_style),
            Text(s.risk_tier.value.upper(), style=risk_style),
            f"{s.metrics.confidence_score:.2f}",
            mods or "*",
        )

    n_global = len(skills) - n_local
    scope_info = f" ({n_local} local, {n_global} global)" if n_global > 0 else ""
    summary = Text(f"\nTotal: {len(skills)} skill(s){scope_info}. Type ':skill <name>' for detailed steps.", style="dim")
    return Panel(
        Group(table, summary),
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
    )


def render_skills_panel_str(
    project_path: Path | str | None = None,
    skill_filter: str | None = None,
    width: int = 80,
    include_global: bool = True,
) -> str:
    """Render procedural skills panel as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_skills_panel(project_path, skill_filter, include_global=include_global))
    return console.export_text().rstrip()


def render_consume_skills_help() -> Panel:
    """Render informative usage guide for :consume-skills command."""
    title = Text(" CONSUME PROCEDURAL SKILLS (:consume-skills) ", style="bold cyan")
    msg = Text()
    msg.append("Ingest and register any skill into StackMind's verified procedural library.\n\n", style="white")
    msg.append("Usage:\n", style="bold yellow")
    msg.append("  :consume-skills <path> [--local]   Install skills from file or directory\n", style="white")
    msg.append("  :consume-skill  <path> [--local]   Alias for :consume-skills\n\n", style="white")
    msg.append("Supported Formats:\n", style="bold yellow")
    msg.append("  [+] Markdown (*.md, SKILL.md) with YAML frontmatter or structured steps\n", style="green")
    msg.append("  [+] JSON manifests (*.json) in native or simplified schemas\n", style="green")
    msg.append("  [+] YAML manifests (*.yaml, *.yml)\n", style="green")
    msg.append("  [+] Directories containing SKILL.md or multiple skill packages\n\n", style="green")
    msg.append("Installation Targets:\n", style="bold yellow")
    msg.append("  [+] Global (default): ~/.stackmindcli/skills/global/ (shared across projects)\n", style="cyan")
    msg.append("  [+] Local (--local):   .sync/skills/active/ (current workspace only)\n\n", style="dim")
    msg.append("Examples:\n", style="bold yellow")
    msg.append("  :consume-skills ./skills/my-skill/SKILL.md\n", style="white dim")
    msg.append("  :consume-skills ~/.gemini/config/plugins/ponytail/skills/ponytail/SKILL.md\n", style="white dim")
    msg.append("  :consume-skills ./custom_skills/ --local\n", style="white dim")

    return Panel(
        msg,
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style="cyan",
        padding=(0, 1),
    )


def render_consume_skills_help_str(width: int = 80) -> str:
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_consume_skills_help())
    return console.export_text().rstrip()


def render_consumed_skills_summary(
    records: Sequence[Any],
    is_global: bool,
    dest_path: str,
    width: int | None = None,
    as_panel: bool = True,
) -> RenderableType:
    """Render summary table of consumed procedural skills."""
    table = Table(box=box.SIMPLE, expand=True, padding=(0, 1))
    table.add_column("Skill ID", style="bold cyan", no_wrap=True)
    table.add_column("Name", style="bold white", no_wrap=True)
    if width is None or width >= 105:
        table.add_column("Ver", justify="right", style="dim")
    table.add_column("Status", style="bold green", no_wrap=True)
    if width is None or width >= 105:
        table.add_column("Risk", style="bold", no_wrap=True)
        table.add_column("Steps", justify="right", style="cyan", no_wrap=True)
        table.add_column("Scope", justify="center", style="yellow", no_wrap=True)
    table.add_column("Description", style="dim", overflow="ellipsis")

    for s in records:
        risk_style = (
            "red"
            if s.risk_tier.value in ("high", "critical")
            else ("yellow" if s.risk_tier.value == "medium" else "green")
        )
        row: list[Any] = [
            s.skill_id,
            s.name,
        ]
        if width is None or width >= 105:
            row.append(f"v{s.version}")
        row.append(s.status.value.upper())
        if width is None or width >= 105:
            row.append(Text(s.risk_tier.value.upper(), style=risk_style))
            row.append(str(len(s.steps)))
            row.append("GLOBAL" if is_global else "LOCAL")
        desc_len = 50 if (width is not None and width < 105) else 75
        desc_text = s.description[:desc_len] + ("..." if len(s.description) > desc_len else "")
        row.append(desc_text)
        table.add_row(*row)

    summary = Text()
    names_str = ", ".join(f"'{s.name}'" for s in records)
    summary.append(f"\n[+] Successfully installed {len(records)} skill(s) ({names_str}) into ", style="bold green")
    summary.append(f"{dest_path}\n", style="bold white")
    summary.append("Run ':skills' to view all active skills, or ':skill <name>' for detailed steps.", style="dim")

    if not as_panel:
        return Group(table, summary)

    title = Text(" CONSUMED PROCEDURAL SKILLS (:consume-skills) ", style="bold green")
    return Panel(
        Group(table, summary),
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style="green",
        padding=(0, 1),
    )


def render_consumed_skills_summary_str(
    records: Sequence[Any],
    is_global: bool,
    dest_path: str,
    width: int = 120,
    as_panel: bool = False,
) -> str:
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_consumed_skills_summary(records, is_global, dest_path, width=width, as_panel=as_panel))
    return console.export_text().rstrip()


def render_projects_panel() -> Panel:
    """Render registered projects panel (:projects)."""
    from validators.kernel.home import get_stackmind_home, list_registered_projects

    projects = list_registered_projects()
    home = get_stackmind_home()
    title = Text(" REGISTERED STACKMIND PROJECTS (:projects) ", style="bold cyan")

    if not projects:
        msg = Text()
        msg.append("\nNo projects registered yet in ~/.stackmindcli/projects.json.\n", style="yellow")
        msg.append(f"StackMind Central Home: {home}\n", style="dim")
        msg.append("Run 'stackmind init <path>' or 'stackmind projects register <path>' to register.", style="dim")
        return Panel(msg, title=title, title_align="left", box=box.ROUNDED, border_style="cyan", padding=(0, 1))

    table = Table(box=box.SIMPLE, expand=True, padding=(0, 1))
    table.add_column("Name", style="bold cyan", width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("Path", style="white", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("Sync Runtime", style="green", width=12, no_wrap=True)
    table.add_column("Last Accessed", style="dim", width=18, no_wrap=True)

    for p in projects:
        sync_badge = "[+] .sync" if p.get("has_sync") else "no .sync"
        if not p.get("exists", True):
            sync_badge = "missing"
        last_acc = str(p.get("last_accessed") or "")
        if "T" in last_acc:
            last_acc = last_acc.split("T")[0] + " " + last_acc.split("T")[1][:5]
        table.add_row(
            str(p.get("name", "unnamed")),
            str(p.get("path", "")),
            sync_badge,
            last_acc,
        )

    summary = Text(f"\nCentral Root: {home} | Total Registered: {len(projects)}", style="dim")
    return Panel(Group(table, summary), title=title, title_align="left", box=box.ROUNDED, border_style="cyan", padding=(0, 1))


def render_projects_panel_str(width: int = 80) -> str:
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_projects_panel())
    return console.export_text().rstrip()


__all__ = [
    "VERIFICATION_DIMENSIONS",
    "render_consume_skills_help",
    "render_consume_skills_help_str",
    "render_consumed_skills_summary",
    "render_consumed_skills_summary_str",
    "render_contract_hud",
    "render_contract_hud_str",
    "render_plan_panel",
    "render_plan_panel_str",
    "render_projects_panel",
    "render_projects_panel_str",
    "render_skills_panel",
    "render_skills_panel_str",
    "render_verification_matrix",
    "render_verification_matrix_str",
]

