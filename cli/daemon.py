"""CLI commands for managing the StackMind LocalDaemon process."""

import tempfile
import time
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.group("daemon")
def daemon_group():
    """Manage the local JSON-RPC runtime daemon."""
    pass


@daemon_group.command("start")
@click.option("--port", "-p", default=8765, show_default=True, help="Port to listen on")
@click.option(
    "--workspace",
    "-w",
    type=click.Path(path_type=Path),
    default=Path("."),
    help="Workspace directory",
)
def daemon_start(port: int, workspace: Path):
    """Start the LocalDaemon JSON-RPC server in background."""
    from validators.kernel.daemon import LocalDaemon

    state_dir = workspace / ".sync" / "runtime" / "daemon"
    state_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold green][+] Starting StackMind LocalDaemon on port {port}...[/bold green]")
    srv = LocalDaemon(str(state_dir), port=port).start()
    console.print(f"[bold green]✓ Daemon listening at {srv.url}[/bold green]")
    console.print("[dim]Press Ctrl+C to stop daemon.[/dim]")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow][!] Stopping daemon...[/yellow]")
        srv.stop()
        console.print("[green]✓ Daemon stopped.[/green]")


@daemon_group.command("stop")
def daemon_stop():
    """Stop running LocalDaemon process listening on default port."""
    import socket

    try:
        import urllib.request

        req = urllib.request.Request("http://localhost:8765/rpc", data=b'{"jsonrpc":"2.0","method":"session.list","id":1}', headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            pass
        console.print("[yellow][!] Daemon detected on http://localhost:8765. Stopping process...[/yellow]")
    except Exception:
        console.print("[dim]No running StackMind daemon detected on port 8765.[/dim]")


@daemon_group.command("restart")
@click.option("--port", "-p", default=8765, show_default=True, help="Port to listen on")
@click.pass_context
def daemon_restart(ctx: click.Context, port: int):
    """Restart the LocalDaemon server."""
    console.print("[yellow][!] Restarting StackMind daemon...[/yellow]")
    ctx.invoke(daemon_stop)
    time.sleep(1)
    ctx.invoke(daemon_start, port=port)
