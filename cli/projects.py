"""CLI commands for managing registered StackMind projects and centralized runtime."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from validators.kernel.home import (
    get_daemon_home_dir,
    get_global_settings_path,
    get_global_skills_dir,
    get_global_tmp_dir,
    get_projects_registry_path,
    get_sessions_home_dir,
    get_stackmind_home,
    list_registered_projects,
    register_project,
    unregister_project,
)

console = Console()


@click.group("projects")
def projects_group():
    """Inspect and manage the centralized projects registry (~/.stackmindcli)."""
    pass


@projects_group.command("list")
def list_projects():
    """List all registered StackMind projects."""
    projects = list_registered_projects()
    if not projects:
        console.print("[dim]No projects registered yet in ~/.stackmindcli/projects.json.[/dim]")
        console.print("[dim]Run `stackmind init <path>` or `stackmind projects register <path>` to add one.[/dim]")
        return

    table = Table(title=f"StackMind Projects Registry ({len(projects)} registered)")
    table.add_column("Name", style="bold cyan")
    table.add_column("Path", style="white")
    table.add_column("Sync Runtime", style="green")
    table.add_column("Last Accessed", style="dim")

    for p in projects:
        sync_badge = "[green][+] .sync[/green]" if p.get("has_sync") else "[yellow]no .sync[/yellow]"
        if not p.get("exists", True):
            sync_badge = "[red]missing path[/red]"
        last_acc = str(p.get("last_accessed") or "")
        if "T" in last_acc:
            last_acc = last_acc.split("T")[0] + " " + last_acc.split("T")[1][:5]
        table.add_row(
            str(p.get("name", "unnamed")),
            str(p.get("path", "")),
            sync_badge,
            last_acc,
        )

    console.print(table)


@projects_group.command("register")
@click.argument("project_path", type=click.Path(exists=True), default=".")
@click.option("--name", "-n", default=None, help="Custom project display name")
def cmd_register_project(project_path: str, name: str | None):
    """Register a project path into the centralized registry."""
    path = Path(project_path).resolve()
    rec = register_project(path, name=name)
    console.print(f"[bold green][+] Registered project:[/bold green] {rec['name']} ([dim]{rec['path']}[/dim])")


@projects_group.command("unregister")
@click.argument("project_path", type=click.Path())
def cmd_unregister_project(project_path: str):
    """Remove a project from the centralized registry."""
    path = Path(project_path).resolve()
    removed = unregister_project(path)
    if removed:
        console.print(f"[bold green][+] Unregistered project:[/bold green] {path}")
    else:
        console.print(f"[yellow]Project not found in registry:[/yellow] {path}")


@projects_group.command("home")
def cmd_home():
    """Display the centralized ~/.stackmindcli directory structure."""
    home = get_stackmind_home()
    console.print(f"[bold cyan]StackMind Central Root:[/bold cyan] {home}")
    console.print(f"  - Settings:  [dim]{get_global_settings_path()}[/dim]")
    console.print(f"  - Registry:  [dim]{get_projects_registry_path()}[/dim]")
    console.print(f"  - Skills:    [dim]{get_global_skills_dir()}[/dim]")
    console.print(f"  - Sessions:  [dim]{get_sessions_home_dir()}[/dim]")
    console.print(f"  - Daemon:    [dim]{get_daemon_home_dir()}[/dim]")
    console.print(f"  - Scratch:   [dim]{get_global_tmp_dir()}[/dim]")


__all__ = ["projects_group"]
