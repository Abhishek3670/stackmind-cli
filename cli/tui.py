"""The zero-bypass terminal client for the local StackMind daemon."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import click

from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter
from validators.kernel.tui.views import (
    activity_line,
    contract_panel,
    diff_viewer,
    session_header,
    verification_matrix,
)


def create_tui_adapter(daemon_url: str) -> StackMindTuiAdapter:
    """Create a presentation adapter; all actions remain daemon RPC calls."""
    return StackMindTuiAdapter(DaemonClient(daemon_url))


def _default_contract() -> dict[str, Any]:
    return {"allow": [], "deny": [], "write_mode": "governed"}


def _show_status(session: dict[str, Any]) -> None:
    click.echo(session_header(session))
    click.echo("[CONTRACT BOUNDARY HUD]")
    click.echo(contract_panel(session.get("contract", {})))


def _show_help() -> None:
    click.echo(
        "Available commands:\n"
        "  :status           Display current session header and Contract Boundary HUD\n"
        "  :diff             Display Unified Diff viewer for staged changes\n"
        "  :matrix           Display 6-Dimensional Verification Matrix\n"
        "  :events           Stream incremental sequenced events from daemon\n"
        "  :approve [reason] Submit Human-in-the-Loop (HITL) approval\n"
        "  :reject [reason]  Submit Human-in-the-Loop (HITL) rejection\n"
        "  :pause            Pause active session turn\n"
        "  :resume           Resume active session\n"
        "  :cancel           Cancel in-flight session turn\n"
        "  :help             Show this help menu\n"
        "  :exit, :quit, q   Gracefully stop daemon and exit\n"
        "  <prompt text>     Submit a governed turn to the agent"
    )


def _run_demo(client: DaemonClient, session: dict[str, Any]) -> None:
    """Render a deterministic walkthrough using only daemon-provided session data."""
    click.echo("StackMind TUI demo")
    _show_status(session)
    click.echo("Verification: " + verification_matrix({
        "scope": True, "state": True, "ast": True, "behavioral": True,
        "security": True, "outcome": True,
    }))
    click.echo("Diff: " + diff_viewer("No staged daemon diff has been published."))
    events = client.events(session["session_id"])
    if events:
        for event in events:
            click.echo(activity_line(event))
    else:
        click.echo(activity_line({"name": "session.created", "payload": {"agent": session.get("agent", "codex")}}))


def _dispatch_command(
    adapter: StackMindTuiAdapter, client: DaemonClient, session: dict[str, Any], text: str
) -> tuple[dict[str, Any], bool]:
    """Dispatch one REPL input; returns the possibly refreshed session and exit flag."""
    normalized = text.strip()
    if not normalized:
        return session, False
    if normalized in {":exit", ":quit", "q"}:
        return session, True
    if normalized == ":help":
        _show_help()
        return session, False
    if normalized == ":status":
        session = adapter.command(":status", session_id=session["session_id"])
        _show_status(session)
        return session, False
    if normalized == ":events":
        events = adapter.command(":events", session_id=session["session_id"])
        if not events:
            click.echo("No new events.")
        for event in events:
            click.echo(activity_line(event))
        return session, False
    if normalized == ":diff":
        click.echo(adapter.command(":diff", session_id=session["session_id"]))
        return session, False
    if normalized == ":matrix":
        click.echo(adapter.command(":matrix", session_id=session["session_id"]))
        return session, False
    if normalized.startswith(":approve"):
        _, _, reason = normalized.partition(" ")
        adapter.command(f":approve {reason}".strip(), session_id=session["session_id"])
        click.echo("Approval recorded.")
        return session, False
    if normalized.startswith(":reject"):
        _, _, reason = normalized.partition(" ")
        adapter.command(f":reject {reason}".strip(), session_id=session["session_id"])
        click.echo("Rejection recorded.")
        return session, False
    if normalized == ":pause":
        session = adapter.command(":pause", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'PAUSED')}")
        return session, False
    if normalized == ":resume":
        session = adapter.command(":resume", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'RUNNING')}")
        return session, False
    if normalized == ":cancel":
        session = adapter.command(":cancel", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'CANCELLED')}")
        return session, False
    if normalized.startswith(":") and not normalized.startswith(":prompt "):
        click.echo("Unknown command. Type :help.")
        return session, False
    result = adapter.command(normalized, session_id=session["session_id"])
    op_id = result.get("operation_id", "turn") if isinstance(result, dict) else "turn"
    click.echo(f"Turn submitted to the governed daemon (operation: {op_id}).")
    return session, False


@click.command("tui")
@click.option("--daemon-url", default=None, help="URL of an existing local daemon.")
@click.option("--agent", "-a", "agent", default="codex", show_default=True)
@click.option("--workspace", "-w", "workspace", type=click.Path(path_type=Path), default=Path("."))
@click.option("--demo", is_flag=True, help="Run the automated daemon-backed walkthrough.")
def tui(daemon_url: str | None, agent: str, workspace: Path, demo: bool) -> None:
    """Start the governed Python-native terminal control plane."""
    temporary_state: tempfile.TemporaryDirectory[str] | None = None
    daemon: LocalDaemon | None = None
    if daemon_url is None:
        temporary_state = tempfile.TemporaryDirectory(prefix="stackmind-tui-")
        daemon = LocalDaemon(temporary_state.name, port=0).start()
        daemon_url = daemon.url

    try:
        client = DaemonClient(daemon_url)
        adapter = StackMindTuiAdapter(client)
        session = client.create_session(
            agent=agent,
            provider="daemon",
            contract=_default_contract(),
            workspace=str(workspace.resolve()),
        )
        if demo:
            _run_demo(client, session)
            return

        _show_status(session)
        _show_help()
        while True:
            try:
                sid = (session["session_id"][:8] + "...") if "session_id" in session else "IDLE"
                text = click.prompt(f"stackmind [{sid}]", prompt_suffix="> ")
            except (EOFError, KeyboardInterrupt):
                click.echo()
                break
            session, should_exit = _dispatch_command(adapter, client, session, text)
            if should_exit:
                break
    finally:
        if daemon is not None:
            daemon.stop()
        if temporary_state is not None:
            temporary_state.cleanup()
