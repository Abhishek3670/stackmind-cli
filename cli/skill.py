"""CLI commands for managing versioned, verified Procedural Skills.

Implements Phase 3 (Skill Storage & Versioning) CLI tooling.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax

from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)
from validators.skill.store import SkillStore


@click.group("skill")
def skill_group():
    """Inspect and manage versioned procedural skills."""
    pass


@skill_group.command("list")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
@click.option(
    "--status",
    "-s",
    type=click.Choice([s.value for s in SkillStatus]),
    default=None,
    help="Filter by lifecycle status",
)
@click.option(
    "--active-only",
    is_flag=True,
    help="Show only active promoted skills",
)
@click.option(
    "--all-versions",
    "-a",
    is_flag=True,
    help="Include all historical versions of each skill",
)
def list_command(project_path: str, status: str | None, active_only: bool, all_versions: bool):
    """List procedural skills and their lifecycle states."""
    console = Console()
    store = SkillStore(project_path)
    status_enum = SkillStatus(status) if status else None
    skills = store.list_skills(
        status=status_enum,
        active_only=active_only,
        include_all_versions=all_versions,
    )

    if not skills:
        console.print("[dim]No skills found matching criteria.[/dim]")
        return

    table = Table(title=f"Procedural Skill Registry ({len(skills)} shown)")
    table.add_column("Skill ID", style="bold cyan", no_wrap=True)
    table.add_column("Name", style="bold green")
    table.add_column("Ver", justify="center", style="magenta")
    table.add_column("Status", justify="center")
    table.add_column("Risk", justify="center")
    table.add_column("Confidence", justify="right", style="cyan")
    table.add_column("Description", style="italic")

    status_styles = {
        SkillStatus.ACTIVE: "[bold green]ACTIVE[/bold green]",
        SkillStatus.EXPERIMENTAL: "[bold yellow]EXPERIMENTAL[/bold yellow]",
        SkillStatus.CANDIDATE: "[dim cyan]CANDIDATE[/dim cyan]",
        SkillStatus.STALE: "[bold red]STALE[/bold red]",
        SkillStatus.DEPRECATED: "[dim red]DEPRECATED[/dim red]",
        SkillStatus.ARCHIVED: "[dim]ARCHIVED[/dim]",
    }

    for s in skills:
        stat_styled = status_styles.get(s.status, s.status.value)
        risk_styled = {
            RiskTier.LOW: "[green]LOW[/green]",
            RiskTier.MEDIUM: "[yellow]MED[/yellow]",
            RiskTier.HIGH: "[bold red]HIGH[/bold red]",
            RiskTier.CRITICAL: "[bold magenta]CRIT[/bold magenta]",
        }.get(s.risk_tier, s.risk_tier.value)

        table.add_row(
            s.skill_id,
            s.name,
            f"v{s.version}",
            stat_styled,
            risk_styled,
            f"{s.metrics.confidence_score:.2f}",
            s.description[:45] + ("..." if len(s.description) > 45 else ""),
        )

    console.print(table)


@skill_group.command("show")
@click.argument("name")
@click.option(
    "--version",
    "-v",
    type=int,
    default=None,
    help="Specific version to display (defaults to active or latest)",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
@click.option(
    "--json-output",
    "-j",
    is_flag=True,
    help="Output complete skill manifest as raw JSON",
)
def show_command(name: str, version: int | None, project_path: str, json_output: bool):
    """Display detailed specification, steps, and provenance for a skill."""
    console = Console()
    store = SkillStore(project_path)
    skill = store.get_skill(name, version=version)

    if not skill:
        console.print(f"[bold red]Error:[/bold red] Skill '{name}' (version {version or 'latest'}) not found.")
        return

    if json_output:
        syntax = Syntax(json.dumps(skill.to_dict(), indent=2, sort_keys=True), "json", theme="monokai")
        console.print(syntax)
        return

    # Header Overview
    console.print(
        Panel(
            f"[bold]Skill ID:[/bold] {skill.skill_id}\n"
            f"[bold]Name:[/bold] {skill.name} (v{skill.version})\n"
            f"[bold]Status:[/bold] {skill.status.value.upper()}\n"
            f"[bold]Risk Tier:[/bold] {skill.risk_tier.value.upper()}\n"
            f"[bold]Confidence:[/bold] {skill.metrics.confidence_score:.2f} (Success rate: {skill.metrics.success_rate * 100:.1f}% over {skill.metrics.success_count + skill.metrics.failure_count} runs)\n"
            f"[bold]Description:[/bold] {skill.description}\n"
            f"[bold]Created:[/bold] {skill.provenance.created_at[:19].replace('T', ' ')} by {skill.provenance.author_agent}",
            title=f"Skill Specification: {skill.name} (v{skill.version})",
        )
    )

    # Applicability & Preconditions
    app = skill.applicability
    app_lines = []
    if app.preconditions:
        app_lines.append("[bold cyan]Preconditions:[/bold cyan]")
        for pre in app.preconditions:
            app_lines.append(f"  • {pre}")
    if app.target_modules:
        app_lines.append("[bold cyan]Target Modules:[/bold cyan] " + ", ".join(app.target_modules))
    if app.known_exclusions:
        app_lines.append("[bold red]Known Exclusions:[/bold red] " + ", ".join(app.known_exclusions))
    if app_lines:
        console.print(Panel("\n".join(app_lines), title="Applicability Boundary"))

    # Procedure Steps
    step_table = Table(title="Procedural Execution Steps")
    step_table.add_column("#", justify="right", style="magenta")
    step_table.add_column("Action", style="bold")
    step_table.add_column("Tool", style="yellow")
    step_table.add_column("Command Template", style="cyan")
    step_table.add_column("Expected Outcome", style="green")

    for s in skill.steps:
        step_table.add_row(
            str(s.step_index),
            s.action,
            s.tool,
            s.command_template or "-",
            s.expected_outcome or "-",
        )
    console.print(step_table)

    # Provenance
    prov = skill.provenance
    prov_lines = [
        f"[bold]Author Agent:[/bold] {prov.author_agent}",
        f"[bold]Source Experiences:[/bold] {', '.join(prov.source_experience_ids) or 'None'}",
    ]
    if prov.promotion_reason:
        prov_lines.append(f"[bold]Promotion Reason:[/bold] {prov.promotion_reason}")
    if prov.previous_version_id:
        prov_lines.append(f"[bold]Previous Version ID:[/bold] {prov.previous_version_id}")
    console.print(Panel("\n".join(prov_lines), title="Provenance & Lineage"))


@skill_group.command("create")
@click.argument("name")
@click.option(
    "--description",
    "-d",
    required=True,
    help="Summary of the skill's procedure",
)
@click.option(
    "--risk-tier",
    "-r",
    type=click.Choice([r.value for r in RiskTier]),
    default="low",
    help="Consequence tier for this skill",
)
@click.option(
    "--action",
    "-a",
    "actions",
    multiple=True,
    help="Step specification in format 'action:tool:command_template'",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def create_command(name: str, description: str, risk_tier: str, actions: Sequence[str], project_path: str):
    """Create a new candidate procedural skill (v1)."""
    console = Console()
    store = SkillStore(project_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    slug = name.strip().lower()

    if store.get_skill(slug):
        console.print(f"[bold red]Error:[/bold red] Skill '{slug}' already exists. Use versioning to update.")
        return

    steps: list[SkillStep] = []
    if actions:
        for idx, act_str in enumerate(actions, start=1):
            parts = act_str.split(":", 2)
            action = parts[0]
            tool = parts[1] if len(parts) > 1 else "bash"
            cmd = parts[2] if len(parts) > 2 else None
            steps.append(SkillStep(step_index=idx, action=action, tool=tool, command_template=cmd))
    else:
        steps.append(SkillStep(step_index=1, action="Execute primary task procedure", tool="bash"))

    rec = SkillRecord(
        skill_id=SkillRecord.mint_id(slug, 1),
        name=slug,
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier(risk_tier),
        description=description,
        applicability=SkillApplicability(preconditions=("Runtime environment initialized",), target_modules=("*",)),
        steps=tuple(steps),
        provenance=SkillProvenance(source_experience_ids=(), created_at=now_iso, author_agent="claude"),
        metrics=SkillMetrics(),
    )

    store.save_version(rec)
    console.print(f"[bold green][SUCCESS][/bold green] Created candidate skill '{rec.name}' v1 ({rec.skill_id}).")


@skill_group.command("test")
@click.argument("name")
@click.option(
    "--version",
    "-v",
    type=int,
    default=None,
    help="Version number to test (defaults to latest)",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def test_command(name: str, version: int | None, project_path: str):
    """Run the 3-Stage Verification Pipeline (Structural, Replay, Canary) on a skill."""
    console = Console()
    store = SkillStore(project_path)
    slug = name.strip().lower()
    skill = store.get_skill(slug, version=version)

    if not skill:
        console.print(f"[bold red]Error:[/bold red] Skill '{slug}' (version {version or 'latest'}) not found.")
        return

    from validators.verification.pipeline import VerificationPipeline
    pipeline_result = VerificationPipeline.verify_skill(skill, project_path)

    table = Table(title=f"3-Stage Verification Pipeline: {skill.name} v{skill.version}")
    table.add_column("Verification Stage", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Score", justify="right", style="cyan")
    table.add_column("Diagnostic Evidence", style="italic")

    for s in pipeline_result.stage_results:
        status_styled = "[bold green]PASS[/bold green]" if s.passed else "[bold red]FAIL[/bold red]"
        msg_summary = "; ".join(s.messages) if s.messages else "-"
        table.add_row(
            s.stage_name.capitalize(),
            status_styled,
            f"{s.score:.2f}",
            msg_summary,
        )

    console.print(table)
    overall_styled = "[bold green]PASSED[/bold green]" if pipeline_result.passed else "[bold red]FAILED[/bold red]"
    console.print(
        f"\n[bold]Overall Pipeline Outcome:[/bold] {overall_styled} "
        f"(Composite Score: {pipeline_result.overall_score:.2f})\n"
        f"[dim]Verification Receipt:[/dim] [cyan]{pipeline_result.receipt_id}[/cyan]"
    )


@skill_group.command("promote")
@click.argument("name")
@click.option(
    "--version",
    "-v",
    type=int,
    default=None,
    help="Version number to promote (defaults to latest)",
)
@click.option(
    "--reason",
    "-r",
    default="Passed verification and canary checks",
    help="Audit rationale for promoting this skill",
)
@click.option(
    "--actor",
    "-a",
    default="claude",
    help="Agent identity requesting promotion (e.g. claude, codex, ceo)",
)
@click.option(
    "--human",
    is_flag=True,
    help="Explicit human operator authorization flag",
)
@click.option(
    "--allow-medium",
    is_flag=True,
    help="Allow autonomous promotion for MEDIUM risk tier",
)
@click.option(
    "--skip-pipeline",
    is_flag=True,
    help="Bypass verification pipeline gate (requires explicit justification)",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def promote_command(
    name: str,
    version: int | None,
    reason: str,
    actor: str,
    human: bool,
    allow_medium: bool,
    skip_pipeline: bool,
    project_path: str,
):
    """Promote a candidate/experimental skill version to ACTIVE status after verification."""
    console = Console()
    store = SkillStore(project_path)
    slug = name.strip().lower()

    if version is None:
        version = store.get_latest_version_number(slug)
        if version == 0:
            console.print(f"[bold red]Error:[/bold red] No versions found for skill '{slug}'.")
            return

    try:
        promoted = store.promote_version(
            slug,
            version,
            reason=reason,
            author_agent=actor,
            is_human=human,
            allow_medium_auto=allow_medium,
            skip_pipeline=skip_pipeline,
        )
        console.print(
            f"[bold green][SUCCESS][/bold green] Promoted skill '{promoted.name}' v{promoted.version} "
            f"({promoted.skill_id}, risk={promoted.risk_tier.value.upper()}) to ACTIVE."
        )
    except Exception as exc:
        console.print(f"[bold red]Promotion failed:[/bold red] {exc}")


@skill_group.command("approve")
@click.argument("name")
@click.option(
    "--version",
    "-v",
    type=int,
    default=None,
    help="Version number to approve (defaults to latest)",
)
@click.option(
    "--approver",
    "-a",
    default="human",
    help="Identity granting review approval (e.g. human, ceo, lead-architect)",
)
@click.option(
    "--reason",
    "-r",
    required=True,
    help="Detailed review rationale for approving high/critical risk skill",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def approve_command(name: str, version: int | None, approver: str, reason: str, project_path: str):
    """Record a review approval receipt for a high-risk or critical procedural skill."""
    console = Console()
    store = SkillStore(project_path)
    slug = name.strip().lower()
    skill = store.get_skill(slug, version=version)

    if not skill:
        console.print(f"[bold red]Error:[/bold red] Skill '{slug}' (v{version or 'latest'}) not found.")
        return

    from validators.skill.governor import PromotionGovernor
    governor = PromotionGovernor(project_path)
    receipt = governor.grant_approval(skill, approver=approver, rationale=reason)

    console.print(
        f"[bold green][SUCCESS][/bold green] Granted review approval for '{skill.name}' v{skill.version} "
        f"(Risk: {skill.risk_tier.value.upper()})\n"
        f"[bold]Approval ID:[/bold] [cyan]{receipt.approval_id}[/cyan]\n"
        f"[bold]Approver:[/bold] {receipt.approver}\n"
        f"[bold]Rationale:[/bold] {receipt.rationale}"
    )


@skill_group.command("auto-promote")
@click.option(
    "--max-risk-tier",
    "-m",
    type=click.Choice(["low", "medium"]),
    default="low",
    help="Maximum risk tier eligible for autonomous promotion (default: low)",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def auto_promote_command(max_risk_tier: str, project_path: str):
    """Autonomously verify and promote eligible unpromoted candidates."""
    console = Console()
    store = SkillStore(project_path)
    candidates = store.list_skills(status=SkillStatus.CANDIDATE)

    if not candidates:
        console.print("[dim]No candidate skills found awaiting promotion.[/dim]")
        return

    allowed_tiers = {RiskTier.LOW} if max_risk_tier == "low" else {RiskTier.LOW, RiskTier.MEDIUM}
    promoted_count = 0

    for cand in candidates:
        if cand.risk_tier not in allowed_tiers:
            console.print(f"[yellow]Skipping '{cand.name}' v{cand.version}: risk tier {cand.risk_tier.value.upper()} exceeds max {max_risk_tier.upper()}[/yellow]")
            continue

        try:
            promoted = store.promote_version(
                cand.name,
                cand.version,
                reason=f"Auto-promoted via policy (max risk: {max_risk_tier})",
                allow_medium_auto=(max_risk_tier == "medium"),
            )
            console.print(f"[bold green][PROMOTED][/bold green] '{promoted.name}' v{promoted.version} ({promoted.risk_tier.value.upper()})")
            promoted_count += 1
        except Exception as exc:
            console.print(f"[bold red]Failed '{cand.name}' v{cand.version}:[/bold red] {exc}")

    console.print(f"\n[bold]Auto-promotion complete:[/bold] {promoted_count} skill(s) promoted.")


@skill_group.command("rollback")
@click.argument("name")
@click.option(
    "--to-version",
    "-t",
    type=int,
    required=True,
    help="Historical target version number to restore",
)
@click.option(
    "--reason",
    "-r",
    default="Rollback to previous stable version",
    help="Audit rationale for the rollback",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def rollback_command(name: str, to_version: int, reason: str, project_path: str):
    """Roll back a skill to a previous stable version specification."""
    console = Console()
    store = SkillStore(project_path)
    slug = name.strip().lower()

    try:
        new_ver = store.rollback_version(slug, to_version, reason=reason)
        console.print(
            f"[bold green][SUCCESS][/bold green] Rolled back '{slug}' to v{to_version}. "
            f"Created new active version v{new_ver.version} ({new_ver.skill_id})."
        )
    except Exception as exc:
        console.print(f"[bold red]Rollback failed:[/bold red] {exc}")


@skill_group.command("deprecate")
@click.argument("name")
@click.option(
    "--version",
    "-v",
    type=int,
    default=None,
    help="Specific version to deprecate (defaults to active)",
)
@click.option(
    "--reason",
    "-r",
    default="Deprecated due to staleness or alternative procedure",
    help="Deprecation rationale",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def deprecate_command(name: str, version: int | None, reason: str, project_path: str):
    """Mark a skill as DEPRECATED and remove active pointer."""
    console = Console()
    store = SkillStore(project_path)
    slug = name.strip().lower()

    try:
        dep = store.deprecate_skill(slug, reason=reason, version=version)
        console.print(f"[bold yellow][DEPRECATED][/bold yellow] Skill '{dep.name}' v{dep.version} is now DEPRECATED.")
    except Exception as exc:
        console.print(f"[bold red]Deprecation failed:[/bold red] {exc}")


@skill_group.command("stats")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def stats_command(project_path: str):
    """Display aggregate skill subsystem metrics."""
    console = Console()
    store = SkillStore(project_path)
    stats = store.stats()

    table = Table(title="Procedural Skill Subsystem Statistics")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right", style="cyan")

    table.add_row("Distinct Skills", str(stats["distinct_skills"]))
    table.add_row("Active Skills (Promoted)", f"[green]{stats['active_skills']}[/green]")
    table.add_row("Stale / Revalidating", f"[red]{stats['stale']}[/red]")
    table.add_row("Deprecated Skills", f"[dim red]{stats['deprecated']}[/dim red]")
    table.add_row("Total Version Manifests", str(stats["total_versions"]))

    console.print(table)


@skill_group.command("retrieve")
@click.argument("query")
@click.option(
    "--contract",
    "-c",
    default=None,
    help="Contract file path or Work Order ID to enforce scope boundary",
)
@click.option(
    "--limit",
    "-l",
    default=3,
    type=int,
    help="Maximum skills to retrieve (default: 3)",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def retrieve_command(query: str, contract: str | None, limit: int, project_path: str):
    """Retrieve and format active procedural skills matching a query and contract."""
    console = Console()
    from validators.skill.retriever import SkillRetriever

    retriever = SkillRetriever(project_path)
    results = retriever.retrieve_skills(query, contract=contract, limit=limit)

    if not results:
        console.print("[dim]No active skills matched the query and contract boundary.[/dim]")
        return

    table = Table(title=f"Retrieved Procedural Skills for '{query}' ({len(results)} match(es))")
    table.add_column("Skill ID", style="bold cyan", no_wrap=True)
    table.add_column("Name", style="bold green")
    table.add_column("Risk", justify="center")
    table.add_column("Confidence", justify="right", style="cyan")
    table.add_column("Relevance", justify="right", style="magenta")
    table.add_column("Matched Terms", style="italic")

    for r in results:
        table.add_row(
            r.skill.skill_id,
            f"{r.skill.name} v{r.skill.version}",
            r.skill.risk_tier.value.upper(),
            f"{r.skill.metrics.confidence_score:.2f}",
            f"{r.relevance_score:.2f}",
            ", ".join(r.matched_terms) or "-",
        )

    console.print(table)
    console.print("\n" + retriever.format_prompt_section(results))


@skill_group.command("audit")
@click.option(
    "--name",
    "-n",
    default=None,
    help="Audit specific skill name (defaults to all active skills)",
)
@click.option(
    "--auto-downgrade/--no-downgrade",
    default=True,
    help="Automatically transition stale/decayed skills from ACTIVE to STALE",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def audit_command(name: str | None, auto_downgrade: bool, project_path: str):
    """Audit active skills for code drift, missing modules, or confidence decay."""
    console = Console()
    from validators.skill.decay import SkillDecayManager

    decay_mgr = SkillDecayManager(project_path)
    reports = decay_mgr.audit_staleness(name, auto_downgrade=auto_downgrade)

    if not reports:
        console.print("[bold green][PASS][/bold green] All active procedural skills are fresh and aligned with project state.")
        return

    table = Table(title=f"Procedural Skill Staleness & Drift Audit ({len(reports)} issue(s) detected)")
    table.add_column("Skill ID", style="bold cyan", no_wrap=True)
    table.add_column("Name", style="bold green")
    table.add_column("Confidence", justify="right", style="cyan")
    table.add_column("Staleness Reasons", style="bold red")
    table.add_column("Action Taken", style="bold yellow")
    table.add_column("Details", style="italic")

    for rep in reports:
        action_str = f"Downgraded to STALE" if auto_downgrade else rep.recommended_action
        table.add_row(
            rep.skill_id,
            f"{rep.skill_name} v{rep.version}",
            f"{rep.confidence_score:.2f}",
            ", ".join(rep.reasons),
            action_str,
            "; ".join(rep.details),
        )

    console.print(table)


@skill_group.command("revalidate")
@click.argument("name")
@click.option(
    "--version",
    "-v",
    type=int,
    default=None,
    help="Version number to revalidate (defaults to latest)",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def revalidate_command(name: str, version: int | None, project_path: str):
    """Re-run 3-stage verification pipeline on a STALE skill to restore ACTIVE status."""
    console = Console()
    from validators.skill.decay import SkillDecayManager

    decay_mgr = SkillDecayManager(project_path)
    slug = name.strip().lower()

    try:
        success, record, message = decay_mgr.revalidate_skill(slug, version=version)
        if success:
            console.print(f"[bold green][SUCCESS][/bold green] {message} ('{record.name}' v{record.version} is now ACTIVE)")
        else:
            console.print(f"[bold red][FAILED][/bold red] {message}")
    except Exception as exc:
        console.print(f"[bold red]Revalidation error:[/bold red] {exc}")


@skill_group.command("feedback")
@click.argument("name")
@click.option(
    "--success/--failure",
    "is_success",
    required=True,
    help="Execution outcome of using this skill",
)
@click.option(
    "--error",
    "-e",
    default=None,
    help="Error diagnostic message if failure",
)
@click.option(
    "--version",
    "-v",
    type=int,
    default=None,
    help="Version number",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def feedback_command(name: str, is_success: bool, error: str | None, version: int | None, project_path: str):
    """Record execution feedback for dynamic skill confidence decay or reinforcement."""
    console = Console()
    from validators.skill.decay import SkillDecayManager

    decay_mgr = SkillDecayManager(project_path)
    slug = name.strip().lower()

    try:
        updated = decay_mgr.record_feedback(slug, success=is_success, error_message=error, version=version)
        status_styled = f"[green]{updated.status.value.upper()}[/green]" if updated.status == SkillStatus.ACTIVE else f"[red]{updated.status.value.upper()}[/red]"
        console.print(
            f"[bold]Updated metrics for '{updated.name}' v{updated.version}:[/bold] "
            f"Confidence={updated.metrics.confidence_score:.2f}, "
            f"SuccessRate={updated.metrics.success_rate:.1%}, "
            f"Status={status_styled}"
        )
    except Exception as exc:
        console.print(f"[bold red]Feedback recording failed:[/bold red] {exc}")
