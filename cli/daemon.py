"""CLI commands and shared lifecycle helpers for StackMind LocalDaemon."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import click
from rich.console import Console

DEFAULT_DAEMON_PORT = 8765
console = Console()
_opener = build_opener(ProxyHandler({}))


def daemon_state_dir(workspace: Path) -> Path:
    """Return the workspace-local directory used for daemon runtime state."""
    return workspace.resolve() / ".sync" / "runtime" / "daemon"


def daemon_pid_path(workspace: Path) -> Path:
    return daemon_state_dir(workspace) / "daemon.pid"


def write_daemon_pid(workspace: Path, port: int, pid: int | None = None) -> Path:
    """Persist the owning process ID and listening port for daemon commands."""
    state_dir = daemon_state_dir(workspace)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "daemon.pid"
    path.write_text(f"{pid if pid is not None else os.getpid()}\n{port}\n", encoding="utf-8")
    return path


def read_daemon_pid(workspace: Path) -> tuple[int | None, int]:
    """Read PID state, tolerating legacy one-line PID files."""
    try:
        values = daemon_pid_path(workspace).read_text(encoding="utf-8").splitlines()
        pid = int(values[0])
        port = int(values[1]) if len(values) > 1 else DEFAULT_DAEMON_PORT
        return pid, port
    except (OSError, ValueError, IndexError):
        return None, DEFAULT_DAEMON_PORT


def clear_daemon_pid(workspace: Path, expected_pid: int | None = None) -> None:
    """Remove daemon state, without deleting ownership transferred to another process."""
    path = daemon_pid_path(workspace)
    recorded_pid, _ = read_daemon_pid(workspace)
    if expected_pid is None or recorded_pid == expected_pid:
        path.unlink(missing_ok=True)


def daemon_health(url: str) -> dict[str, Any] | None:
    """Return health payload only for a healthy StackMind daemon."""
    try:
        with _opener.open(f"{url.rstrip('/')}/health", timeout=1) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read())
    except (OSError, URLError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("status") == "ok" else None


def _wait_until_offline(url: str, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon_health(url) is None:
            return True
        time.sleep(0.1)
    return daemon_health(url) is None


@click.group("daemon")
def daemon_group():
    """Manage the local JSON-RPC runtime daemon."""


@daemon_group.command("start")
@click.option("--port", "-p", default=DEFAULT_DAEMON_PORT, show_default=True, help="Port to listen on")
@click.option("--workspace", "-w", type=click.Path(path_type=Path), default=Path("."), help="Workspace directory")
def daemon_start(port: int, workspace: Path):
    """Start the LocalDaemon JSON-RPC server in the foreground."""
    from validators.kernel.daemon import LocalDaemon

    workspace = workspace.resolve()
    console.print(f"[bold green][+] Starting StackMind LocalDaemon on port {port}...[/bold green]")
    srv = LocalDaemon(str(daemon_state_dir(workspace)), port=port).start()
    write_daemon_pid(workspace, srv.address[1])
    console.print(f"[bold green]✓ Daemon listening at {srv.url}[/bold green]")
    console.print("[dim]Press Ctrl+C to stop daemon.[/dim]")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow][!] Stopping daemon...[/yellow]")
    finally:
        srv.stop()
        clear_daemon_pid(workspace, os.getpid())
        console.print("[green]✓ Daemon stopped.[/green]")


@daemon_group.command("stop")
@click.option("--workspace", "-w", type=click.Path(path_type=Path), default=Path("."), help="Workspace directory")
def daemon_stop(workspace: Path):
    """Stop the daemon process recorded in this workspace's PID file."""
    workspace = workspace.resolve()
    pid, port = read_daemon_pid(workspace)
    url = f"http://127.0.0.1:{port}"
    if pid is None:
        console.print("[dim]No StackMind daemon PID file found.[/dim]")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_daemon_pid(workspace, pid)
        console.print(f"[dim]Removed stale daemon PID {pid}.[/dim]")
        return
    except PermissionError:
        raise click.ClickException(f"Permission denied stopping daemon PID {pid}.")
    if not _wait_until_offline(url):
        raise click.ClickException(f"Daemon PID {pid} did not release port {port}.")
    clear_daemon_pid(workspace, pid)
    console.print(f"[green]✓ Daemon PID {pid} stopped and port {port} released.[/green]")


@daemon_group.command("status")
@click.option("--workspace", "-w", type=click.Path(path_type=Path), default=Path("."), help="Workspace directory")
def daemon_status(workspace: Path):
    """Display health and ownership information for the local daemon."""
    pid, port = read_daemon_pid(workspace.resolve())
    url = f"http://127.0.0.1:{port}"
    health = daemon_health(url)
    state = "online" if health is not None else "offline"
    sessions = health.get("sessions", 0) if health is not None else 0
    console.print(f"State: {state}\nPID: {pid or 'unavailable'}\nPort: {port}\nURL: {url}\nActive sessions: {sessions}")


@daemon_group.command("restart")
@click.option("--port", "-p", default=DEFAULT_DAEMON_PORT, show_default=True, help="Port to listen on")
@click.option("--workspace", "-w", type=click.Path(path_type=Path), default=Path("."), help="Workspace directory")
@click.pass_context
def daemon_restart(ctx: click.Context, port: int, workspace: Path):
    """Restart the LocalDaemon server."""
    console.print("[yellow][!] Restarting StackMind daemon...[/yellow]")
    ctx.invoke(daemon_stop, workspace=workspace)
    ctx.invoke(daemon_start, port=port, workspace=workspace)
