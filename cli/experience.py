"""CLI commands for inspecting captured Experience Records.

Implements Phase 1 (Experience Capture) CLI tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax

from validators.experience.index import ExperienceIndex
from validators.experience.store import ExperienceStore
from validators.harness.snapshot import TrustLevel


@click.group("experience")
def experience_group():
    """Inspect and manage captured Experience Records."""
    pass


@experience_group.command("list")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
@click.option(
    "--eligible-only",
    "-e",
    is_flag=True,
    help="Filter to only learning-eligible experience records",
)
@click.option(
    "--agent",
    "-a",
    "agent_id",
    default=None,
    help="Filter by agent identifier",
)
@click.option(
    "--limit",
    "-n",
    default=20,
    type=int,
    help="Maximum records to display",
)
def list_command(project_path: str, eligible_only: bool, agent_id: str | None, limit: int):
    """List captured execution experience records."""
    console = Console()
    store = ExperienceStore(project_path)
    records = store.list_records(only_learning_eligible=eligible_only, agent_id=agent_id, limit=limit)

    if not records:
        console.print("[dim]No experience records found matching criteria.[/dim]")
        return

    table = Table(title=f"Captured Experience Records ({len(records)} shown)")
    table.add_column("Experience ID", style="bold cyan", no_wrap=True)
    table.add_column("Task ID", style="magenta")
    table.add_column("Agent", style="blue")
    table.add_column("Trust Level", justify="center")
    table.add_column("Eligible", justify="center")
    table.add_column("Outcome", justify="center")
    table.add_column("Recorded At", style="dim", no_wrap=True)

    for r in records:
        trust_style = {
            TrustLevel.LEARNING_ELIGIBLE: "[bold green]LEARNING_ELIGIBLE[/bold green]",
            TrustLevel.VERIFIED: "[bold yellow]VERIFIED[/bold yellow]",
            TrustLevel.OBSERVABLE: "[dim red]OBSERVABLE[/dim red]",
        }.get(r.trust_level, str(r.trust_level))

        eligible_str = "[green]YES[/green]" if r.learning_eligible else "[dim red]NO[/dim red]"
        outcome_style = "[green]completed[/green]" if r.outcome == "completed" else f"[red]{r.outcome}[/red]"

        table.add_row(
            r.experience_id,
            r.task_id,
            r.agent_id,
            trust_style,
            eligible_str,
            outcome_style,
            r.recorded_at[:19].replace("T", " "),
        )

    console.print(table)


@experience_group.command("show")
@click.argument("experience_id")
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
    help="Output full record as JSON",
)
def show_command(experience_id: str, project_path: str, json_output: bool):
    """Show details of a specific experience record."""
    console = Console()
    store = ExperienceStore(project_path)
    rec = store.load_record(experience_id)

    if not rec:
        console.print(f"[bold red][ERROR][/bold red] Experience record '{experience_id}' not found.")
        raise click.Abort()

    if json_output:
        console.print(json.dumps(rec.to_dict(), indent=2, sort_keys=True))
        return

    trust_color = "green" if rec.learning_eligible else "yellow" if rec.trust_level == TrustLevel.VERIFIED else "red"
    console.print(
        Panel.fit(
            f"[bold cyan]{rec.experience_id}[/bold cyan]\n"
            f"Task: [magenta]{rec.task_id}[/magenta] | Agent: [blue]{rec.agent_id}[/blue] | "
            f"Trust: [{trust_color}]{rec.trust_level.value}[/{trust_color}] | "
            f"Outcome: [bold]{rec.outcome}[/bold]",
            title="Experience Artifact",
        )
    )

    # Verification Dimensions
    v_table = Table(title="Verification Evidence")
    v_table.add_column("Dimension", style="bold")
    v_table.add_column("Status", justify="center")

    dims = rec.verification.dimensions
    for name, val in [
        ("Scope Boundary Verified", dims.scope_verified),
        ("Staged State Validated", dims.state_verified),
        ("Code Validity Verified", dims.code_verified),
        ("Behavioral Constraint Met", dims.behavioral_verified),
        ("Security Safeguards (D025)", dims.security_verified),
        ("Outcome Succeeded", dims.outcome_verified),
        ("Declaration Matches Disk", rec.verification.declaration_matches),
    ]:
        v_table.add_row(name, "[green]PASS[/green]" if val else "[red]FAIL[/red]")

    console.print(v_table)

    # Actions & Observations
    if rec.actions:
        a_table = Table(title="Actions Executed")
        a_table.add_column("Tool", style="bold yellow")
        a_table.add_column("Command / Action", style="cyan")
        a_table.add_column("Timestamp", style="dim")
        for a in rec.actions:
            a_table.add_row(a.tool, a.command_or_symbol, a.timestamp[:19].replace("T", " "))
        console.print(a_table)

    # Observed Files Diff
    diff = rec.verification.observed_diff
    if not diff.is_empty:
        diff_lines = []
        for f in diff.added:
            diff_lines.append(f"[green]+ {f}[/green]")
        for f in diff.modified:
            diff_lines.append(f"[yellow]~ {f}[/yellow]")
        for f in diff.deleted:
            diff_lines.append(f"[red]- {f}[/red]")
        console.print(Panel("\n".join(diff_lines), title="Authoritative Filesystem Diff"))


@experience_group.command("stats")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def stats_command(project_path: str):
    """Show summary statistics of captured experience records."""
    console = Console()
    store = ExperienceStore(project_path)
    stats = store.stats()
    index = ExperienceIndex(project_path)
    idx_stats = index.stats()

    table = Table(title="Experience Subsystem Statistics")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right", style="cyan")

    table.add_row("Total Captured Records", str(stats["total"]))
    table.add_row("Learning Eligible (Top Tier)", f"[green]{stats['learning_eligible']}[/green]")
    table.add_row("Verified (Middle Tier)", f"[yellow]{stats['verified']}[/yellow]")
    table.add_row("Observable Only (Base Tier)", f"[dim red]{stats['observable']}[/dim red]")
    table.add_row("FTS Indexed Records", str(idx_stats["indexed"]))
    table.add_row("Index Database Size", f"{idx_stats['db_size_bytes']} bytes")

    console.print(table)


@experience_group.command("compile")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
@click.option(
    "--clean",
    "-c",
    is_flag=True,
    help="Perform a clean rebuild from scratch, wiping existing SQLite cache",
)
def compile_command(project_path: str, clean: bool):
    """Compile Tier 1 raw experience records into the Tier 2 SQLite/FTS5 index."""
    console = Console()
    index = ExperienceIndex(project_path)

    if clean:
        console.print("[dim]Performing clean rebuild of experience compilation index...[/dim]")
        count = index.build_index(clean=True)
        console.print(f"[bold green][SUCCESS][/bold green] Rebuilt experience index with {count} record(s).")
    else:
        upserted, deleted = index.update_index()
        console.print(f"[bold green][SUCCESS][/bold green] Synchronized experience index: {upserted} updated/inserted, {deleted} removed.")


@experience_group.command("search")
@click.argument("query")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
@click.option(
    "--eligible-only",
    "-e",
    is_flag=True,
    help="Filter to only learning-eligible experiences",
)
@click.option(
    "--agent",
    "-a",
    "agent_id",
    default=None,
    help="Filter by agent identifier",
)
@click.option(
    "--limit",
    "-n",
    default=10,
    type=int,
    help="Maximum results to return",
)
def search_command(query: str, project_path: str, eligible_only: bool, agent_id: str | None, limit: int):
    """Search compiled experience history using full-text BM25 ranking."""
    console = Console()
    index = ExperienceIndex(project_path)
    results = index.search(query, learning_eligible_only=eligible_only, agent_id=agent_id, limit=limit)

    if not results:
        console.print(f"[dim]No experience records matched query: '{query}'.[/dim]")
        return

    table = Table(title=f"Experience Search Results for '{query}' ({len(results)} matches)")
    table.add_column("Experience ID", style="bold cyan", no_wrap=True)
    table.add_column("Task Signature", style="magenta")
    table.add_column("Agent", style="blue")
    table.add_column("Trust Level", justify="center")
    table.add_column("Eligible", justify="center")
    table.add_column("Snippet / Match", style="italic")

    for r in results:
        trust_style = {
            TrustLevel.LEARNING_ELIGIBLE: "[bold green]LEARNING_ELIGIBLE[/bold green]",
            TrustLevel.VERIFIED: "[bold yellow]VERIFIED[/bold yellow]",
            TrustLevel.OBSERVABLE: "[dim red]OBSERVABLE[/dim red]",
        }.get(r.trust_level, str(r.trust_level))

        eligible_str = "[green]YES[/green]" if r.learning_eligible else "[dim red]NO[/dim red]"
        snippet_text = r.matched_snippet.replace("[match]", "[bold yellow]").replace("[/match]", "[/bold yellow]") if r.matched_snippet else "-"

        table.add_row(
            r.experience_id,
            r.task_signature,
            r.agent_id,
            trust_style,
            eligible_str,
            snippet_text,
        )

    console.print(table)


@experience_group.command("capture")
@click.option(
    "--work-order",
    "-w",
    default=None,
    help="Work Order ID (e.g. WO-058) to capture as an experience record",
)
@click.option(
    "--agent",
    "-a",
    default=None,
    help="Agent identifier (e.g. codex, gemini)",
)
@click.option(
    "--backfill",
    is_flag=True,
    default=False,
    help="Backfill experience records for all completed work orders in the project",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def capture_command(work_order: str | None, agent: str | None, backfill: bool, project_path: str):
    """Capture execution experience records from work orders or backfill completed history."""
    console = Console()
    from validators.experience.recorder import ExperienceRecorder
    p = Path(project_path).resolve()

    if backfill:
        wo_dir = p / ".sync" / "work-orders"
        if not wo_dir.exists():
            console.print(f"[bold red]Error:[/bold red] No .sync/work-orders directory found at {p}")
            return

        wo_files = list(wo_dir.glob("*.yaml")) + list((wo_dir / "COMPLETED").glob("*.yaml"))
        captured = 0
        for wf in wo_files:
            if wf.name in {"INDEX.yaml"}:
                continue
            rec = ExperienceRecorder.capture_from_work_order(p, wf, agent=agent, save=True)
            if rec:
                captured += 1
                console.print(f"[green][+] Captured {wf.stem}: {rec.experience_id}[/green]")

        console.print(f"\n[bold green]Backfilled {captured} experience record(s).[/bold green]")
        return

    if work_order:
        rec = ExperienceRecorder.capture_from_work_order(p, work_order, agent=agent, save=True)
        if rec:
            console.print(f"[bold green][SUCCESS][/bold green] Captured experience {rec.experience_id} for {work_order}")
        else:
            console.print(f"[bold red]Error:[/bold red] Could not find or parse work order '{work_order}'")
        return

    console.print("[yellow]Please specify --work-order <WO-ID> or --backfill to capture experiences.[/yellow]")
