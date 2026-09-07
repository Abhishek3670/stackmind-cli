"""CLI commands for analysis providers."""

from __future__ import annotations

from pathlib import Path

import click

from validators.knowledge.analysis.flow import run_flow_analysis
from validators.knowledge.analysis.runtime import run_runtime_analysis


@click.group()
def analyze():
    """Run optional analysis providers and merge evidence."""
    pass


@analyze.command("runtime", context_settings={"ignore_unknown_options": True})
@click.option(
    "--project",
    "-p",
    "project_path",
    type=click.Path(exists=True),
    default=".",
    help="Project path",
)
@click.option("--include", multiple=True, help="fnmatch pattern for modules, paths, or qualnames")
@click.option("--exclude", multiple=True, help="fnmatch pattern to ignore")
@click.option("--event-cap", default=100_000, show_default=True, type=int)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def runtime_command(
    project_path: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    event_cap: int,
    command: tuple[str, ...],
):
    """Trace runtime calls, for example: stackmind analyze runtime -- pytest tests/."""
    from rich.console import Console

    console = Console()
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise click.UsageError("Provide a command after `--`.")
    result, normalized = run_runtime_analysis(
        Path(project_path),
        command,
        include=include,
        exclude=exclude,
        event_cap=event_cap,
    )
    console.print("[bold green][PASS] Runtime analysis complete[/bold green]")
    console.print(f"run_id: {result.run_id}")
    console.print(f"events: {result.event_count}")
    console.print(f"observations: {len(result.observations)}")
    console.print(f"normalized_edges: {normalized}")
    console.print(f"partial: {result.partial}")


@analyze.command("flows")
@click.option(
    "--project",
    "-p",
    "project_path",
    type=click.Path(exists=True),
    default=".",
    help="Project path",
)
@click.option("--source", "sources", multiple=True, help="Source expression, e.g. request.args")
@click.option("--sink", "sinks", multiple=True, help="Sink call, e.g. db.execute")
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
def flows_command(
    project_path: str,
    sources: tuple[str, ...],
    sinks: tuple[str, ...],
    paths: tuple[str, ...],
):
    """Run bounded static flow analysis and merge FLOWS_TO evidence."""
    from rich.console import Console

    console = Console()
    if not paths:
        raise click.UsageError("Provide at least one Python file path.")
    observations, normalized = run_flow_analysis(
        Path(project_path),
        paths,
        sources=sources or ("request.args", "request.form", "request.json", "os.environ"),
        sinks=sinks or ("db.execute", "cursor.execute", "subprocess.run", "eval", "exec"),
    )
    console.print("[bold green][PASS] Flow analysis complete[/bold green]")
    console.print(f"observations: {len(observations)}")
    console.print(f"normalized_edges: {normalized}")


__all__ = ["analyze"]
