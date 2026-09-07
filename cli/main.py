"""stackmind CLI entry point."""

from pathlib import Path

import click

from . import __version__
from .analyze import analyze
from .graph import graph
from .harness import harness


@click.group()
@click.version_option(version=__version__, prog_name="stackmind")
def cli():
    """stackmind — Multi-Agent Engineering Runtime Platform."""
    pass


@cli.command()
@click.argument("project_path", type=click.Path())
@click.option("--name", "-n", default=None, help="Project name (defaults to directory name)")
@click.option(
    "--agents",
    "-a",
    default=None,
    help="Comma-separated agent list (default: claude,codex,gemini,gemma,local-llm)",
)
@click.option("--no-git", is_flag=True, help="Skip git initialization")
def init(project_path: str, name: str | None, agents: str | None, no_git: bool):
    """Initialize a new stackmind runtime.

    Creates a fresh multi-agent runtime at PROJECT_PATH with all required
    templates, schemas, and directory structure.

    Examples:

        stackmind init ./my-project

        stackmind init ./my-project --name "My Project"

        stackmind init ./my-project --agents claude,codex,gemini

        stackmind init ./my-project --no-git
    """
    from .init import init as run_init

    agent_list = None
    if agents:
        agent_list = [a.strip() for a in agents.split(",")]

    try:
        success = run_init(
            project_path=Path(project_path),
            name=name,
            agents=agent_list,
            no_git=no_git,
        )
        if not success:
            raise SystemExit(1)
    except RuntimeError as e:
        raise SystemExit(str(e))
    except FileNotFoundError as e:
        raise SystemExit(str(e))


@cli.command()
@click.argument("project_path", type=click.Path(exists=True), default=".")
@click.option("--fix", is_flag=True, help="Auto-fix minor issues")
def validate(project_path: str, fix: bool):
    """Validate runtime health.

    Checks schema compliance, directory structure, protocol integrity,
    and boot snapshot consistency across all four validation layers.

    Examples:

        stackmind validate

        stackmind validate ./my-project

        stackmind validate --fix
    """
    from rich.console import Console

    from .validate import Severity
    from .validate import validate as run_validate

    console = Console()
    result = run_validate(Path(project_path), fix=fix)

    if result.passed and not result.warnings:
        console.print("[bold green][PASS] Schema validation[/bold green]")
        console.print("[bold green][PASS] Structure validation[/bold green]")
        console.print("[bold green][PASS] Protocol compliance[/bold green]")
        console.print("[bold green][PASS] Boot integrity[/bold green]")
        console.print("[bold green][PASS] Knowledge validation[/bold green]")
        console.print("\n[bold green]Runtime is healthy.[/bold green]")
    else:
        for issue in result.issues:
            if issue.severity == Severity.ERROR:
                console.print(f"[bold red][FAIL] {issue.layer}: {issue.message}[/bold red]")
            else:
                console.print(f"[bold yellow][WARN] {issue.layer}: {issue.message}[/bold yellow]")

        if result.errors:
            console.print(
                f"\n[bold red]{len(result.errors)} error(s), "
                f"{len(result.warnings)} warning(s)[/bold red]"
            )
            if not fix:
                fixable = [i for i in result.issues if i.auto_fixable]
                if fixable:
                    console.print(
                        f"\n[dim]Run `stackmind validate --fix` to auto-fix "
                        f"{len(fixable)} issue(s).[/dim]"
                    )
            raise SystemExit(1)
        else:
            console.print(
                f"\n[bold green][PASS] No errors[/bold green] "
                f"[dim]({len(result.warnings)} warning(s))[/dim]"
            )


@cli.command()
@click.argument("project_path", type=click.Path(exists=True), default=".")
def doctor(project_path: str):
    """Check runtime status and compatibility.

    Displays a comprehensive health report including version compatibility,
    schema alignment, agent session summary, and validation results.

    Examples:

        stackmind doctor

        stackmind doctor ./my-project
    """
    from .doctor import doctor as run_doctor

    success = run_doctor(Path(project_path))
    if not success:
        raise SystemExit(1)


@cli.command()
@click.argument("project_path", type=click.Path(exists=True), default=".")
@click.option("--to", "target_version", default=None, help="Target version")
@click.option("--check", is_flag=True, help="Check pending migrations only")
@click.option("--rollback", is_flag=True, help="Rollback last migration")
def migrate(project_path: str, target_version: str | None, check: bool, rollback: bool):
    """Migrate runtime to new version.

    Applies pending migrations to bring the runtime up to date.
    Migrations are defined as YAML manifests in the migrations/ directory.

    Examples:

        stackmind migrate

        stackmind migrate ./my-project

        stackmind migrate --check

        stackmind migrate --rollback
    """
    from .migrate import migrate as run_migrate

    success = run_migrate(
        project_path=Path(project_path),
        target_version=target_version,
        check=check,
        rollback=rollback,
    )
    if not success:
        raise SystemExit(1)


@cli.command()
@click.argument("agent", type=str)
@click.option(
    "--project",
    "-p",
    "project_path",
    type=click.Path(exists=True),
    default=".",
    help="Project path",
)
@click.option("--force", is_flag=True, help="Skip handoff validation (not recommended)")
@click.option(
    "--defer",
    is_flag=True,
    help="Defer unprocessed inbox items instead of blocking shutdown",
)
def shutdown(agent: str, project_path: str, force: bool, defer: bool):
    """Shutdown an agent session with handoff validation.

    Validates that the agent has written a handoff report before
    allowing shutdown. Updates TREE.yaml status and archives the session.

    Examples:

        stackmind shutdown claude

        stackmind shutdown codex --project ./my-project

        stackmind shutdown gemini --force
    """
    from .shutdown import shutdown as run_shutdown

    success = run_shutdown(
        project_path=Path(project_path),
        agent=agent,
        force=force,
        defer=defer,
    )
    if not success:
        raise SystemExit(1)


@cli.command()
@click.argument("agent", type=str)
@click.option(
    "--project",
    "-p",
    "project_path",
    type=click.Path(exists=True),
    default=".",
    help="Project path",
)
def promote(agent: str, project_path: str):
    """Promote a worker's draft snapshot to canonical (CLAUDE-01).

    Enforces the validate -> promote -> validate gate: the draft at
    runtime/drafts/<agent>.boot.draft.yaml is validated before promotion and
    the canonical runtime/boot/<agent>.boot.yaml is validated after. On any
    failure the promotion is aborted/rolled back and a blocker is written to
    Claude's inbox.

    Examples:

        stackmind promote codex

        stackmind promote gemma --project ./my-project
    """
    from .promote import promote as run_promote

    success = run_promote(project_path=Path(project_path), agent=agent)
    if not success:
        raise SystemExit(1)


@cli.group()
def lock():
    """Manage the runtime write lock (PLAT-03).

    The write lock serializes canonical writes (runtime/boot/, TREE.yaml)
    across agent sessions. An agent acquires the lock when it begins a
    session and releases it at shutdown.
    """
    pass


@lock.command("acquire")
@click.argument("agent", type=str)
@click.option(
    "--project",
    "-p",
    "project_path",
    type=click.Path(exists=True),
    default=".",
    help="Project path",
)
@click.option(
    "--session-id",
    "session_id",
    default=None,
    help="Session identifier to record in the lock",
)
@click.option("--force", is_flag=True, help="Steal the lock even if another agent holds it")
def lock_acquire(agent: str, project_path: str, session_id: str | None, force: bool):
    """Acquire the write lock for AGENT.

    Examples:

        stackmind lock acquire claude --session-id 31

        stackmind lock acquire codex --force
    """
    from rich.console import Console

    from .lock import acquire_lock

    console = Console()
    sync_path = Path(project_path) / ".sync"
    if not sync_path.exists():
        console.print("[bold red][x] No .sync/ directory found[/bold red]")
        raise SystemExit(1)

    ok, message = acquire_lock(sync_path, agent, session_id=session_id, force=force)
    if ok:
        console.print(f"[green][+] {message}[/green]")
    else:
        console.print(f"[bold red][x] {message}[/bold red]")
        raise SystemExit(1)


@lock.command("release")
@click.argument("agent", type=str)
@click.option(
    "--project",
    "-p",
    "project_path",
    type=click.Path(exists=True),
    default=".",
    help="Project path",
)
@click.option("--force", is_flag=True, help="Release even if another agent holds the lock")
def lock_release(agent: str, project_path: str, force: bool):
    """Release the write lock held by AGENT.

    Examples:

        stackmind lock release claude

        stackmind lock release codex --force
    """
    from rich.console import Console

    from .lock import release_lock

    console = Console()
    sync_path = Path(project_path) / ".sync"
    if not sync_path.exists():
        console.print("[bold red][x] No .sync/ directory found[/bold red]")
        raise SystemExit(1)

    ok, message = release_lock(sync_path, agent, force=force)
    if ok:
        console.print(f"[green][+] {message}[/green]")
    else:
        console.print(f"[bold red][x] {message}[/bold red]")
        raise SystemExit(1)


@lock.command("status")
@click.option(
    "--project",
    "-p",
    "project_path",
    type=click.Path(exists=True),
    default=".",
    help="Project path",
)
def lock_status(project_path: str):
    """Show the current write-lock status.

    Examples:

        stackmind lock status
    """
    from rich.console import Console

    from .lock import lock_is_malformed, read_lock

    console = Console()
    sync_path = Path(project_path) / ".sync"
    if not sync_path.exists():
        console.print("[bold red][x] No .sync/ directory found[/bold red]")
        raise SystemExit(1)

    if lock_is_malformed(sync_path):
        console.print("[bold red][x] LOCK file present but malformed[/bold red]")
        raise SystemExit(1)

    current = read_lock(sync_path)
    if current is None:
        console.print("[dim]No lock held. Runtime is free for canonical writes.[/dim]")
        return

    console.print(
        f"[bold]LOCK held by:[/bold] {current.get('held_by')}\n"
        f"[bold]Session:[/bold] {current.get('session_id')}\n"
        f"[bold]Acquired at:[/bold] {current.get('acquired_at')}"
    )


from .experience import experience_group
from .skill import skill_group
from .learn import learn_group

cli.add_command(graph)
cli.add_command(harness)
cli.add_command(analyze)
cli.add_command(experience_group)
cli.add_command(skill_group)
cli.add_command(learn_group)

if __name__ == "__main__":
    cli()


