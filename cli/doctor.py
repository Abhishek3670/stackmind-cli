"""stackmind doctor — Check runtime status and compatibility.

Provides a human-readable health report including:
- Runtime version compatibility check
- Schema version alignment
- Migration status
- Agent session summary
- Pending issues from validate
"""

from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .validate import Severity, _discover_agents, _load_yaml, validate

console = Console()

CLI_VERSION = __version__.replace("-alpha", "").replace("-beta", "")


import re


def _parse_version(version_str: str) -> tuple[int, int, int]:
    digits = [int(n) for n in re.findall(r"\d+", version_str)]
    digits.extend([0] * max(0, 3 - len(digits)))
    return (digits[0], digits[1], digits[2])


def _check_compatibility(runtime_version: str, cli_version: str) -> str:
    try:
        rv = _parse_version(runtime_version)
        cv = _parse_version(cli_version)
    except (ValueError, IndexError):
        return "UNKNOWN"

    # CLI 2.x is fully compatible with Runtime 1.2+ (Governance schema didn't change)
    if cv[0] == 2 and rv[0] == 1 and rv[1] >= 2:
        return "FULL"

    if rv[0] == cv[0]:
        if rv[1] <= cv[1]:
            return "FULL"
        else:
            return "PARTIAL"
    if cv[0] > rv[0]:
        return "READ_ONLY"
    return "INCOMPATIBLE"


def doctor(project_path: Path) -> bool:
    """Run doctor checks and print a health report.

    Args:
        project_path: Root of the project.

    Returns:
        True if runtime is healthy, False otherwise.
    """
    project_path = project_path.resolve()
    sync_path = project_path / ".sync"

    if not sync_path.exists():
        console.print("[bold red][FAIL] No .sync/ directory found[/bold red]")
        console.print("  This does not appear to be a stackmind project.")
        console.print("  Run `stackmind init` to create a new runtime.")
        return False

    console.print("[bold]Runtime Version Check[/bold]")
    console.print("-" * 40)

    # Read RUNTIME_VERSION
    rv_path = sync_path / "RUNTIME_VERSION"
    runtime_version = None
    schema_versions = None

    if rv_path.exists():
        rv_data, err = _load_yaml(rv_path)
        if rv_data:
            runtime_version = rv_data.get("version", "unknown")
            schema_versions = rv_data.get("schema_versions", {})
            console.print(f"  Runtime version: [bold]{runtime_version}[/bold]")
        else:
            console.print(f"  Runtime version: [bold red]ERROR ({err})[/bold red]")
    else:
        console.print("  Runtime version: [dim]not set (pre-stackmind runtime)[/dim]")
        runtime_version = "0.0.0"

    console.print(f"  CLI version: [bold]{__version__}[/bold]")

    compat = _check_compatibility(runtime_version or "0.0.0", CLI_VERSION)
    compat_display = {
        "FULL": "[bold green]COMPATIBLE[/bold green]",
        "PARTIAL": "[bold yellow]PARTIAL (CLI older than runtime)[/bold yellow]",
        "READ_ONLY": "[bold yellow]READ-ONLY (major version mismatch)[/bold yellow]",
        "INCOMPATIBLE": "[bold red]INCOMPATIBLE[/bold red]",
        "UNKNOWN": "[dim]unknown[/dim]",
    }
    console.print(f"  Compatibility: {compat_display.get(compat, compat)}")

    # Schema versions
    if schema_versions:
        console.print(f"\n[bold]Schema Version Check[/bold]")
        console.print("-" * 40)
        for name, version in schema_versions.items():
            console.print(f"  {name}: v{version}")

    # Agent summary
    console.print(f"\n[bold]Agent Summary[/bold]")
    console.print("-" * 40)

    tree_path = sync_path / "runtime" / "TREE.yaml"
    agents = _discover_agents(sync_path)

    if tree_path.exists():
        tree_data, _ = _load_yaml(tree_path)
        if tree_data and "agents" in tree_data:
            table = Table(show_header=True, header_style="bold")
            table.add_column("Agent")
            table.add_column("Status")
            table.add_column("Sessions")
            table.add_column("Last Task")

            for agent_name, agent_data in tree_data["agents"].items():
                status = agent_data.get("status", "unknown")
                sessions = str(agent_data.get("session_count", 0))
                last_task = agent_data.get("last_task", "—")
                if len(last_task) > 40:
                    last_task = last_task[:37] + "..."

                status_style = {
                    "active": "green",
                    "assigned": "cyan",
                    "idle": "dim",
                    "blocked": "red",
                }.get(status, "")

                table.add_row(
                    agent_name,
                    f"[{status_style}]{status}[/{status_style}]" if status_style else status,
                    sessions,
                    last_task,
                )

            console.print(table)
    elif agents:
        for agent in agents:
            console.print(f"  {agent}: [dim]boot snapshot exists[/dim]")

    # Migration status
    console.print(f"\n[bold]Migration Status[/bold]")
    console.print("-" * 40)
    from .migrate import get_applied_migrations, get_pending_migrations, load_migrations
    applied = get_applied_migrations(sync_path)
    pending = [
        m for m in get_pending_migrations(runtime_version or "0.0.0", None, load_migrations())
        if m.get("to_version") not in applied
    ]
    if pending:
        console.print(f"  Pending migrations: [bold yellow]{len(pending)}[/bold yellow]")
    else:
        console.print("  Pending migrations: [dim]None[/dim]")
        console.print("  Runtime is up to date.")

    # Run validate and report summary
    console.print(f"\n[bold]Validation Summary[/bold]")
    console.print("-" * 40)

    result = validate(project_path)
    if result.passed and not result.warnings:
        console.print("  [bold green]All checks pass. Runtime is healthy.[/bold green]")
    else:
        if result.errors:
            console.print(f"  [bold red]{len(result.errors)} error(s)[/bold red]")
        if result.warnings:
            console.print(f"  [bold yellow]{len(result.warnings)} warning(s)[/bold yellow]")
        console.print("  Run `stackmind validate` for details.")

    return result.passed
