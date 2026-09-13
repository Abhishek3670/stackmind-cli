"""Tests for TUI Reconnection, Event Replay, Responsive Polish & Regression (WO-033 / Phase 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from click.testing import CliRunner

from cli.main import cli
from cli.tui.app import (
    dispatch_delivery_command,
    format_session_header,
    reconnect_and_sync,
)
from cli.tui.chat import (
    render_assistant_message_str,
    render_chat_transcript_str,
    render_user_message_str,
)
from cli.tui.diff import render_unified_diff_str
from cli.tui.events import (
    is_connection_error,
    recover_transcript_from_events,
    render_connection_status,
    render_connection_status_str,
    render_error_box,
    render_error_box_str,
)
from cli.tui.landing import (
    LANDING_DIAMOND,
    render_landing_block,
    render_landing_block_str,
)
from cli.tui.state import (
    AutonomousDeliveryState,
    ChatMessage,
    ProjectPhase,
)
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter


class _MockReconnectionClient:
    """Mock DaemonClient supporting simulated disconnects and sequenced event replay."""

    def __init__(self, session_id: str = "session-recon-1") -> None:
        self.session_id = session_id
        self.url = "http://127.0.0.1:4040"
        self.is_connected = True
        self.events_db: list[dict[str, Any]] = []
        self.session_data: dict[str, Any] = {
            "session_id": session_id,
            "state": "RUNNING",
            "provider": "daemon",
            "agent": "codex",
            "workspace": "/tmp/test-project",
            "contract": {"allow": ["cli/tui/*"], "deny": [".sync/boot/*"], "write_mode": "governed"},
        }
        self.pauses: list[str] = []
        self.resumes: list[str] = []
        self.cancels: list[str] = []
        self.approvals: list[tuple[str, bool, str]] = []
        self.turns: list[tuple[str, str]] = []

    def get_session(self, session_id: str) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        return dict(self.session_data)

    def health_version(self) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        return {"protocol_version": 1, "status": "ok"}

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        return [e for e in self.events_db if e.get("sequence", 0) > after]

    def turn(self, session_id: str, prompt: str, **params: Any) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        self.turns.append((session_id, prompt))
        return {"operation_id": f"op-{len(self.turns)}"}

    def pause(self, session_id: str) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        self.pauses.append(session_id)
        self.session_data["state"] = "PAUSED"
        return dict(self.session_data)

    def resume(self, session_id: str) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        self.resumes.append(session_id)
        self.session_data["state"] = "RUNNING"
        return dict(self.session_data)

    def cancel(self, session_id: str) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        self.cancels.append(session_id)
        self.session_data["state"] = "CANCELLED"
        return dict(self.session_data)

    def approve(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Connection refused by daemon")
        self.approvals.append((session_id, approved, reason))
        return {"session_id": session_id, "approved": approved, "reason": reason}

    def list_roles(self) -> list[dict[str, Any]]:
        return [
            {"role": "Architecture", "backend": "Claude"},
            {"role": "Backend", "backend": "Codex"},
        ]

    def list_agents(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def plan_get(self, session_id: str, plan_id: str | None = None) -> dict[str, Any]:
        return {}


def test_connection_status_indicator_transitions():
    """Verify visual indicators transition between online, reconnecting, and offline."""
    online_txt = render_connection_status("online")
    assert "online" in online_txt.plain
    assert "●" in online_txt.plain

    reconnecting_txt = render_connection_status("reconnecting")
    assert "reconnecting" in reconnecting_txt.plain
    assert "○" in reconnecting_txt.plain

    offline_txt = render_connection_status("offline")
    assert "offline" in offline_txt.plain
    assert "✗" in offline_txt.plain

    assert render_connection_status_str("online") == "● online"
    assert render_connection_status_str("reconnecting") == "○ reconnecting"
    assert render_connection_status_str("offline") == "✗ offline"


def test_is_connection_error_detection():
    """Verify network and connection failure classification."""
    assert is_connection_error(ConnectionError("Connection refused"))
    assert is_connection_error(ConnectionRefusedError("Target machine refused"))
    assert is_connection_error(OSError("RemoteDisconnected"))
    assert is_connection_error(RuntimeError("Failed to establish a new connection"))
    assert not is_connection_error(ValueError("Invalid syntax"))
    assert not is_connection_error(KeyError("missing key"))


def test_reconnect_and_sync_flow():
    """Verify reconnect_and_sync transitions status and replays missed events."""
    client = _MockReconnectionClient("session-test-1")
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    state = AutonomousDeliveryState(session_id="session-test-1")

    # Initial state
    assert state.connection_status == "online"
    assert state.last_sequence == 0

    # Populate daemon events
    client.events_db = [
        {"sequence": 1, "name": "turn.started", "payload": {"prompt": "Implement auth"}},
        {"sequence": 2, "name": "event.toolCall", "payload": {"tool_name": "edit", "arguments": {"path": "auth.py"}}},
        {"sequence": 3, "name": "event.toolResult", "payload": {"tool_name": "edit", "status": "completed", "response": "Auth implemented successfully."}},
    ]

    # Reconnect and sync
    ok, missed = reconnect_and_sync(client, adapter, state, client.session_data)  # type: ignore[arg-type]
    assert ok is True
    assert len(missed) == 3
    assert state.connection_status == "online"
    assert state.last_sequence == 3
    assert len(state.messages) == 2
    assert state.messages[0].role == "user"
    assert state.messages[0].content == "Implement auth"
    assert state.messages[1].role == "assistant"
    assert state.messages[1].content == "Auth implemented successfully."

    # Disconnect daemon and attempt reconnect
    client.is_connected = False
    fail_ok, fail_missed = reconnect_and_sync(client, adapter, state, client.session_data)  # type: ignore[arg-type]
    assert fail_ok is False
    assert state.connection_status == "offline"
    assert fail_missed == []

    # Restore daemon and reconnect with incremental new event
    client.is_connected = True
    client.events_db.append({
        "sequence": 4,
        "name": "event.toolResult",
        "payload": {"tool_name": "test", "status": "completed", "response": "All tests passed."},
    })
    ok2, missed2 = reconnect_and_sync(client, adapter, state, client.session_data)  # type: ignore[arg-type]
    assert ok2 is True
    assert state.connection_status == "online"
    assert len(missed2) == 1
    assert state.last_sequence == 4
    assert len(state.messages) == 3
    assert state.messages[2].content == "All tests passed."


def test_zero_message_or_activity_duplication_on_repeated_replays():
    """Verify that multiple reconnect/replay runs never duplicate visible messages or activity."""
    state = AutonomousDeliveryState(session_id="session-dup-1")
    events = [
        {"sequence": 1, "name": "turn.started", "payload": {"prompt": "First prompt"}},
        {"sequence": 2, "name": "event.toolResult", "payload": {"tool_name": "edit", "response": "First response"}},
    ]

    # Replay 1
    recover_transcript_from_events(events, state)
    assert len(state.messages) == 2
    assert len(state.activity_log) == 2

    # Replay 2 (same events - must not duplicate messages or activity)
    recover_transcript_from_events(events, state)
    assert len(state.messages) == 2
    assert len(state.activity_log) == 2

    # Replay 3 (partial overlap: sequence 2 and 3)
    events_expanded = [
        {"sequence": 2, "name": "event.toolResult", "payload": {"tool_name": "edit", "response": "First response"}},
        {"sequence": 3, "name": "turn.started", "payload": {"prompt": "Second prompt"}},
    ]
    recover_transcript_from_events(events_expanded, state)
    assert len(state.messages) == 3
    assert state.messages[2].content == "Second prompt"
    assert len(state.activity_log) == 3


def test_transcript_recovery_with_outbox_report(tmp_path: Path):
    """Verify recovery extracts assistant reports from outbox files."""
    state = AutonomousDeliveryState(session_id="session-outbox-1")
    outbox = tmp_path / ".sync" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    report_file = outbox / "harness-2026-09-13T20-00-00.md"
    report_file.write_text(
        "# Harness Run\n\n## Report\nDelivery completed with 100% test pass rate.\n\n## Meta\ntokens: 120",
        encoding="utf-8",
    )

    events = [
        {"sequence": 1, "name": "turn.started", "payload": {"prompt": "Run delivery"}},
    ]

    recovered = recover_transcript_from_events(events, state, workspace=tmp_path)
    assert len(recovered) == 2
    assert recovered[0].role == "user"
    assert recovered[0].content == "Run delivery"
    assert recovered[1].role == "assistant"
    assert "Delivery completed with 100% test pass rate." in recovered[1].content


def test_responsive_terminal_sizing_landing():
    """Verify landing block degrades gracefully without breaking diamond or layout."""
    # Narrow display (width = 40)
    narrow_output = render_landing_block_str(width=40)
    assert LANDING_DIAMOND in narrow_output
    assert "StackMind" in narrow_output
    assert "Start chatting" in narrow_output

    # Standard display (width = 80)
    std_output = render_landing_block_str(width=80)
    assert LANDING_DIAMOND in std_output
    assert "PLAN · BUILD · VERIFY · GOVERN" in std_output
    assert "Ctrl+K" in std_output

    # Wide display (width = 120)
    wide_output = render_landing_block_str(width=120)
    assert LANDING_DIAMOND in wide_output


def test_responsive_session_header_formatting():
    """Verify session header truncates path and metadata on narrow viewports."""
    session = {
        "session_id": "session-1234567890abcdef",
        "state": "RUNNING",
        "provider": "daemon",
        "agent": "codex",
        "workspace": "/users/developer/projects/stackmind-cli-workspace",
    }

    # Wide display (>= 80)
    wide_hdr = format_session_header(session, width=100, status="online")
    assert "workspace: /users/developer/projects/stackmind-cli-workspace" in wide_hdr
    assert "● online" in wide_hdr

    # Medium display (60)
    med_hdr = format_session_header(session, width=60, status="reconnecting")
    assert "stackmind-cli-workspace" in med_hdr
    assert "○ reconnecting" in med_hdr

    # Narrow display (40)
    narrow_hdr = format_session_header(session, width=40, status="offline")
    assert "Session" in narrow_hdr
    assert "RUNNING" in narrow_hdr
    assert "✗ offline" in narrow_hdr
    assert "/users/developer" not in narrow_hdr


def test_inline_error_box_strips_traceback():
    """Verify error box suppresses raw python tracebacks and presents clean UI."""
    raw_tb = (
        "Traceback (most recent call last):\n"
        "  File 'daemon.py', line 42, in call\n"
        "    raise ConnectionRefusedError('Connection actively refused by port 4040')\n"
        "ConnectionRefusedError: Connection actively refused by port 4040"
    )
    box_str = render_error_box_str(raw_tb, title="DAEMON FAILURE", hint="Restart local daemon.")
    assert "DAEMON FAILURE" in box_str
    assert "ConnectionRefusedError: Connection actively refused by port 4040" in box_str
    assert "Restart local daemon." in box_str
    assert "Traceback (most recent call last):" not in box_str


def test_dispatch_reconnect_command():
    """Verify :reconnect command via dispatch_delivery_command."""
    client = _MockReconnectionClient("session-cmd-1")
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    state = AutonomousDeliveryState(session_id="session-cmd-1")

    # Command: :reconnect
    session, should_exit = dispatch_delivery_command(adapter, client, client.session_data, ":reconnect", state)  # type: ignore[arg-type]
    assert should_exit is False
    assert state.connection_status == "online"


def test_dispatch_error_handling_with_disconnected_daemon():
    """Verify dispatch_delivery_command displays error box on disconnect without unhandled exception."""
    client = _MockReconnectionClient("session-down-1")
    client.is_connected = False  # Simulate disconnect
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    state = AutonomousDeliveryState(session_id="session-down-1")

    # Trying to send a turn while daemon is down
    session, should_exit = dispatch_delivery_command(adapter, client, client.session_data, "hello daemon", state)  # type: ignore[arg-type]
    assert should_exit is False
    assert state.connection_status == "reconnecting"
    assert any("Could not submit turn" in m.content for m in state.messages)


def test_tui_repl_comprehensive_colon_commands_regression(tmp_path: Path):
    """Verify all colon commands execute cleanly in interactive CliRunner session."""
    user_inputs = "\n".join([
        ":help",
        ":status",
        ":contract",
        ":matrix",
        ":diff",
        ":events",
        ":roles",
        ":wo",
        ":tree",
        ":plan",
        ":approve initial plan",
        ":reject revision feedback",
        ":completion",
        ":pause",
        ":resume",
        ":reconnect",
        ":landing",
        ":chat",
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
    assert "=== AGENT ROLES & EXECUTION BACKENDS (:roles) ===" in result.output
    assert "WORK ORDERS" in result.output
    assert "Architecture" in result.output
    assert "PROJECT COMPLETE" in result.output
    assert "Session PAUSED" in result.output
    assert "Session RUNNING" in result.output
    assert "reconnected" in result.output or "online" in result.output
    assert "✦" in result.output
