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
