"""Tests for TUI Reconnection, Event Replay, Responsive Polish & Regression (WO-033 / Phase 5)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from click.testing import CliRunner

from cli.main import cli
from cli.tui.app import (
    dispatch_delivery_command,
    format_session_header,
    preserve_composer_buffer,
    prompt_composer_input,
    reconnect_and_sync,
    render_composer_box_str,
    restore_composer_focus,
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
from cli.tui.layout import (
    NARROW_THRESHOLD,
    compute_layout,
    render_conversation_column,
    render_workspace_layout_str,
)
from cli.tui.runtime_panel import RuntimePanelScroll
from cli.tui.state import (
    AutonomousDeliveryState,
    ChatMessage,
    ConversationScroll,
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


# ── Phase 8: Composer, Scroll & Focus Behavior Tests (§28–§31, §41) ──────────

def test_composer_multiline_vertical_expansion():
    """Verify composer styling, border, and dynamic vertical expansion for multiline input (§28, §29, §30)."""
    # 1. Single-line default composer
    comp_single = render_composer_box_str(width=80)
    assert "> " in comp_single
    assert "Type a message..." in comp_single
    assert "Ctrl+K commands | Ctrl+L clear" in comp_single
    single_lines = [line for line in comp_single.splitlines() if line.strip()]
    # Top border, single content line, bottom border -> 3 lines
    assert len(single_lines) == 3

    # 2. Multiline expansion with string content
    multiline_text = "def calculate_total():\n    return sum(items)\ncalculate_total()"
    comp_multi = render_composer_box_str(width=80, content=multiline_text)
    assert "> def calculate_total():" in comp_multi
    assert "return sum(items)" in comp_multi
    assert "calculate_total()" in comp_multi
    assert "Ctrl+K commands | Ctrl+L clear" in comp_multi
    multi_lines = [line for line in comp_multi.splitlines() if line.strip()]
    # Top border + 3 content lines + bottom border -> 5 lines (expanded vertically!)
    assert len(multi_lines) == 5
    assert len(multi_lines) > len(single_lines)

    # 3. Multiline expansion with list of lines
    comp_list = render_composer_box_str(width=80, content=["line alpha", "line beta"])
    assert "> line alpha" in comp_list
    assert "line beta" in comp_list
    list_lines = [line for line in comp_list.splitlines() if line.strip()]
    assert len(list_lines) == 4


def test_composer_partial_input_preservation_and_state_tracking(monkeypatch):
    """Verify composer retains partially typed text during live updates without freezing input (§29, §30)."""
    state = AutonomousDeliveryState(session_id="session-pres-1")
    assert state.composer_buffer == ""
    assert state.is_typing is False

    # Simulate user partially typing
    preserve_composer_buffer(state, "git commit -m 'in progress")
    assert state.composer_buffer == "git commit -m 'in progress"

    # Simulate prompt input completion with preserved buffer
    monkeypatch.setattr("builtins.input", lambda prompt: "'")
    result = prompt_composer_input(width=80, state=state)
    assert result == "git commit -m 'in progress'"
    # Buffer consumed and typing flag reset
    assert state.composer_buffer == ""
    assert state.is_typing is False


def test_focus_restoration_without_hijacking():
    """Verify focus restoration restores focus when idle but never steals from active typing (§30)."""
    state = AutonomousDeliveryState(session_id="session-focus-1")

    # When not typing, focus restoration succeeds and sets focus target
    state.is_typing = False
    state.focus_target = "terminal"
    restored = restore_composer_focus(state)
    assert restored is True
    assert state.focus_target == "composer"

    # When user is actively typing, focus restoration must NOT steal focus or keystrokes
    state.is_typing = True
    state.focus_target = "active_input_buffer"
    restored_while_typing = restore_composer_focus(state)
    assert restored_while_typing is False
    assert state.focus_target == "active_input_buffer"  # Preserved!


def test_conversation_auto_scroll_and_activity_badge():
    """Verify conversation auto-scroll follows at bottom and shows '↓ New activity' badge when scrolled up (§31)."""
    state = AutonomousDeliveryState(session_id="session-scroll-1")
    scroll = state.conversation_scroll
    assert scroll.scroll_offset == 0
    assert scroll.follow_bottom is True
    assert scroll.has_new_activity is False

    # 1. At bottom: new incoming messages maintain live-follow without badge
    state.add_message("assistant", "Initial welcome message")
    assert scroll.follow_bottom is True
    assert scroll.has_new_activity is False

    # 2. Scrolled upward: detaches from live-follow
    state.scroll_conversation_up(lines=3)
    assert scroll.scroll_offset == 3
    assert scroll.follow_bottom is False

    # 3. New message arrives while scrolled up: triggers '↓ New activity' badge
    state.add_message("assistant", "Background operation finished.")
    assert scroll.has_new_activity is True

    # 4. Conversation column rendering includes the badge
    rendered_col = render_conversation_column("Chat transcript body", state=state)
    buf = io.StringIO()
    from rich.console import Console
    c = Console(file=buf, color_system=None, force_terminal=False)
    c.print(rendered_col)
    output = buf.getvalue()
    assert "↓ New activity" in output
    assert "Chat transcript body" in output

    # 5. Returning to bottom resumes live-follow and clears the badge
    state.scroll_conversation_to_bottom()
    assert scroll.scroll_offset == 0
    assert scroll.follow_bottom is True
    assert scroll.has_new_activity is False

    # Verify badge is removed after returning to bottom
    buf_bottom = io.StringIO()
    c_bottom = Console(file=buf_bottom, color_system=None, force_terminal=False)
    c_bottom.print(render_conversation_column("Chat transcript body", state=state))
    assert "↓ New activity" not in buf_bottom.getvalue()


def test_independent_scrolling_conversation_and_runtime():
    """Verify independent vertical scrolling between conversation column and runtime panel (§10, §31)."""
    state = AutonomousDeliveryState(session_id="session-indep-1")

    # Scroll conversation column up, keep runtime panel at bottom
    state.scroll_conversation_up(2)
    assert state.conversation_scroll.follow_bottom is False
    assert state.scroll.follow_bottom is True

    # Trigger conversation activity
    state.add_message("assistant", "New conversation turn")
    assert state.conversation_scroll.has_new_activity is True
    assert state.scroll.has_new_activity is False

    # Render workspace layout: left has badge, right does not
    ws_text_1 = render_workspace_layout_str("Conversation area", width=120, state=state)
    assert "↓ New activity" in ws_text_1
    assert "↓ New runtime activity" not in ws_text_1

    # Scroll runtime panel up and trigger runtime activity
    state.scroll.scroll_up(1)
    state.scroll.notify_activity()
    assert state.scroll.has_new_activity is True

    # Now both have their respective badges independently
    ws_text_2 = render_workspace_layout_str("Conversation area", width=120, state=state)
    assert "↓ New activity" in ws_text_2
    assert "↓ New runtime activity" in ws_text_2

    # Clear conversation scroll -> conversation badge clears, runtime badge remains
    state.scroll_conversation_to_bottom()
    ws_text_3 = render_workspace_layout_str("Conversation area", width=120, state=state)
    assert "↓ New activity" not in ws_text_3
    assert "↓ New runtime activity" in ws_text_3

    # Clear runtime scroll -> runtime badge clears
    state.scroll.scroll_to_bottom()
    ws_text_4 = render_workspace_layout_str("Conversation area", width=120, state=state)
    assert "↓ New activity" not in ws_text_4
    assert "↓ New runtime activity" not in ws_text_4


def test_responsive_workspace_layout_dimensions():
    """Verify responsive two-column workspace layout calculation and rendering across terminal dimensions (§41)."""
    # 80 cols: Narrow threshold (<100) -> runtime hidden, full width to conversation
    layout_80 = compute_layout(80)
    assert layout_80.show_runtime is False
    assert layout_80.conversation_width == 80
    assert layout_80.runtime_width == 0
    assert layout_80.divider_width == 0

    rendered_80 = render_workspace_layout_str("Narrow terminal conversation", width=80)
    assert "Narrow terminal conversation" in rendered_80
    assert "│" not in rendered_80  # No divider when narrow
    assert "StackMind Runtime" not in rendered_80

    # 100 cols: Threshold met -> runtime visible (~28%)
    layout_100 = compute_layout(100)
    assert layout_100.show_runtime is True
    assert layout_100.runtime_width == 28
    assert layout_100.conversation_width == 100 - 28 - 1  # 71
    assert layout_100.divider_width == 1

    # 120 cols: Two-column layout with divider and runtime panel
    layout_120 = compute_layout(120)
    assert layout_120.show_runtime is True
    assert layout_120.conversation_width + layout_120.runtime_width + 1 == 120
    rendered_120 = render_workspace_layout_str("Wide terminal conversation", width=120)
    assert "Wide terminal conversation" in rendered_120
    assert "│" in rendered_120
    assert "StackMind Runtime" in rendered_120

    # 160 cols: Wide layout respects RUNTIME_MAX_WIDTH (42)
    layout_160 = compute_layout(160)
    assert layout_160.show_runtime is True
    assert layout_160.runtime_width <= 42
    assert layout_160.conversation_width + layout_160.runtime_width + 1 == 160

