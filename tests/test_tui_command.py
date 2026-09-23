"""Tests for the registered zero-bypass terminal client command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cli.main import cli
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui.adapter import StackMindTuiAdapter


class _RecordingClient:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.pauses: list[str] = []
        self.resumes: list[str] = []
        self.cancels: list[str] = []
        self.approvals: list[tuple[str, bool, str]] = []
        self.sessions: dict[str, dict[str, Any]] = {
            "session-1": {
                "session_id": "session-1",
                "state": "RUNNING",
                "provider": "daemon",
                "agent": "codex",
                "workspace": ".",
                "contract": {"allow": ["src/*"], "deny": ["boot/*"], "write_mode": "governed"},
            }
        }
        self.events_list: list[dict[str, Any]] = [
            {"sequence": 1, "name": "session.started", "payload": {}},
            {"sequence": 2, "name": "tool_call.read", "payload": {"path": "src/main.py"}},
        ]

    def turn(self, session_id: str, prompt: str, **params: object) -> dict[str, object]:
        del params
        self.turns.append((session_id, prompt))
        return {"operation_id": "operation-1"}

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.sessions.get(session_id, {"session_id": session_id, "state": "RUNNING", "provider": "daemon"})

    def pause(self, session_id: str) -> dict[str, Any]:
        self.pauses.append(session_id)
        return {"session_id": session_id, "state": "PAUSED", "provider": "daemon"}

    def resume(self, session_id: str) -> dict[str, Any]:
        self.resumes.append(session_id)
        return {"session_id": session_id, "state": "RUNNING", "provider": "daemon"}

    def cancel(self, session_id: str) -> dict[str, Any]:
        self.cancels.append(session_id)
        return {"session_id": session_id, "state": "CANCELLED", "provider": "daemon"}

    def approve(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        self.approvals.append((session_id, approved, reason))
        return {"session_id": session_id, "approved": approved, "reason": reason}

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        del session_id
        return [e for e in self.events_list if e["sequence"] > after]


def test_tui_command_is_registered_and_has_help():
    result = CliRunner().invoke(cli, ["tui", "--help"])

    assert result.exit_code == 0
    assert "--daemon-url" in result.output
    assert "--agent" in result.output
    assert "-a" in result.output
    assert "--workspace" in result.output
    assert "-w" in result.output
    assert "--demo" in result.output


def test_tui_demo_starts_and_stops_its_managed_daemon(tmp_path: Path):
    result = CliRunner().invoke(cli, ["tui", "--demo", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "StackMind TUI demo" in result.output
    assert "Session" in result.output
    assert "Verification:" in result.output
    assert "Diff:" in result.output


def test_adapter_routes_all_interactive_commands():
    client = _RecordingClient()
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]

    # Turns
    adapter.command("hello runtime", session_id="session-1")
    adapter.command(":prompt approved prompt", session_id="session-1")
    assert client.turns == [
        ("session-1", "hello runtime"),
        ("session-1", "approved prompt"),
    ]

    # Status
    status = adapter.command(":status", session_id="session-1")
    assert status["session_id"] == "session-1"

    # Pause / Resume / Cancel
    paused = adapter.command(":pause", session_id="session-1")
    assert paused["state"] == "PAUSED"
    assert client.pauses == ["session-1"]

    resumed = adapter.command(":resume", session_id="session-1")
    assert resumed["state"] == "RUNNING"
    assert client.resumes == ["session-1"]

    cancelled = adapter.command(":cancel", session_id="session-1")
    assert cancelled["state"] == "CANCELLED"
    assert client.cancels == ["session-1"]

    # Diff & Matrix
    diff = adapter.command(":diff", session_id="session-1")
    assert "No staged daemon diff" in diff

    matrix = adapter.command(":matrix", session_id="session-1")
    assert "Scope: PASS" in matrix

    # Approve & Reject
    adapter.command(":approve verified changes", session_id="session-1")
    assert client.approvals[-1] == ("session-1", True, "verified changes")

    adapter.command(":reject out of scope", session_id="session-1")
    assert client.approvals[-1] == ("session-1", False, "out of scope")

    # Events
    events = adapter.command(":events", session_id="session-1")
    assert len(events) == 2


def test_tui_repl_interactive_session_execution(tmp_path: Path):
    user_inputs = "\n".join([
        ":help",
        ":status",
        ":matrix",
        ":diff",
        ":events",
        ":approve ready to ship",
        ":reject needs work",
        ":pause",
        ":resume",
        "do something safe",
        ":cancel",
        ":exit",
    ]) + "\n"

    result = CliRunner().invoke(
        cli,
        ["tui", "--workspace", str(tmp_path)],
        input=user_inputs,
    )

    assert result.exit_code == 0, result.output
    assert "Available commands:" in result.output
    assert "[CONTRACT BOUNDARY HUD]" in result.output
    assert "Scope: PASS" in result.output
    assert "Approval recorded." in result.output
    assert "Rejection recorded." in result.output
    assert "Session PAUSED" in result.output
    assert "Session RUNNING" in result.output
    assert "Turn submitted to the governed daemon" in result.output


def test_tui_with_explicit_daemon_url(tmp_path: Path):
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        user_inputs = ":status\n:exit\n"
        result = CliRunner().invoke(
            cli,
            ["tui", "--daemon-url", daemon.url, "--workspace", str(tmp_path)],
            input=user_inputs,
        )
        assert result.exit_code == 0, result.output
        assert "Session" in result.output


def test_dispatch_delivery_command_routes_colon_commands_to_conversation_viewport():
    """Verify colon commands append user message and system response to state.messages (WO-055)."""
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState, RoleStatus, WorkOrderItem

    client = _RecordingClient()
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    session = {"session_id": "session-1", "workspace": ".", "contract": {"allow": ["src/*"]}}
    state = AutonomousDeliveryState(session_id="session-1")
    state.roles["Backend"] = RoleStatus("Backend", "Codex", state="ACTIVE")
    state.work_orders.append(WorkOrderItem(id="WO-055", title="TUI Fixes", status="ACTIVE"))

    # 1. Test :help
    dispatch_delivery_command(adapter, client, session, ":help", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":help"
    assert state.messages[-1].role == "system"
    assert "Available commands:" in state.messages[-1].content

    # 2. Test :roles
    dispatch_delivery_command(adapter, client, session, ":roles", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":roles"
    assert state.messages[-1].role == "system"
    assert "AGENT ROLES" in state.messages[-1].content
    assert "Backend" in state.messages[-1].content

    # 3. Test :status
    dispatch_delivery_command(adapter, client, session, ":status", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":status"
    assert state.messages[-1].role == "system"
    assert "session-1" in state.messages[-1].content

    # 4. Test :contract
    dispatch_delivery_command(adapter, client, session, ":contract", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":contract"
    assert state.messages[-1].role == "system"
    assert "CONTRACT BOUNDARY HUD" in state.messages[-1].content

    # 5. Test :wo
    dispatch_delivery_command(adapter, client, session, ":wo", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":wo"
    assert state.messages[-1].role == "system"
    assert "WORK ORDERS" in state.messages[-1].content
    assert "WO-055" in state.messages[-1].content

    # 6. Test :matrix
    dispatch_delivery_command(adapter, client, session, ":matrix", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":matrix"
    assert state.messages[-1].role == "system"
    assert "VERIFICATION MATRIX" in state.messages[-1].content or "Scope:" in state.messages[-1].content

    # 7. Test :diff
    dispatch_delivery_command(adapter, client, session, ":diff", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":diff"
    assert state.messages[-1].role == "system"

    # 8. Test :tree
    dispatch_delivery_command(adapter, client, session, ":tree", state)
    assert state.messages[-2].role == "user"
    assert state.messages[-2].content == ":tree"
    assert state.messages[-1].role == "system"
    assert "Architecture" in state.messages[-1].content


def test_colon_commands_suppress_stdout_echo_in_tty_mode(monkeypatch):
    """Verify that when sys.stdout.isatty() is True, raw click.echo is suppressed to keep layout intact (WO-055)."""
    import sys
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState

    client = _RecordingClient()
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    session = {"session_id": "session-1", "workspace": "."}
    state = AutonomousDeliveryState(session_id="session-1")

    # Simulate TTY environment
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    echoed: list[str] = []
    monkeypatch.setattr("click.echo", lambda *args, **kwargs: echoed.append(str(args)))

    dispatch_delivery_command(adapter, client, session, ":roles", state)
    # Output must be stored in state.messages
    assert len(state.messages) == 2
    assert state.messages[0].content == ":roles"
    assert "AGENT ROLES" in state.messages[1].content
    # But click.echo must NOT have been called in TTY mode
    assert len(echoed) == 0
