"""Unit tests for TUI terminal resize handling, Rich.Live localized layout, and agent badges (WO-003).

Covers:
1. Agent role model & quantization badges (format_model_badge, format_agent_tree, render_runtime_panel_str).
2. Terminal size adaptation & safe resize handling (install_resize_handler, check_terminal_resize, SIGWINCH).
3. Rich.Live localized workspace rendering (LiveWorkspaceManager, create_live_workspace, non-TTY fallback).
4. Full-screen redraw integration with LiveWorkspaceManager.
5. Dynamic responsive layout transitions across narrow (<100 cols) and wide terminal boundaries.
"""

from __future__ import annotations

import io
import os
import signal
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from cli.tui.app import (
    check_terminal_resize,
    dispatch_delivery_command,
    install_resize_handler,
    redraw_full_screen,
    render_roles_panel,
)
from cli.tui.layout import (
    NARROW_THRESHOLD,
    LiveWorkspaceManager,
    compute_layout,
    create_live_workspace,
    render_full_screen_workspace,
    render_workspace_layout_str,
)
from cli.tui.runtime_panel import (
    format_agent_tree,
    format_model_badge,
    render_runtime_panel_str,
)
from cli.tui.state import AutonomousDeliveryState, RoleStatus


# ─── 1. AGENT ROLE MODEL & QUANTIZATION BADGES ───────────────────────────────


def test_format_model_badge_combinations():
    """Verify format_model_badge correctly formats various backend, model, and quantization combinations."""
    # Backend + model + quantization
    assert (
        format_model_badge("ollama", "qwen2.5-coder:7b", "q4_k_m")
        == "ollama/qwen2.5-coder:7b (q4_k_m)"
    )

    # Model already includes backend prefix
    assert (
        format_model_badge("ollama", "ollama/qwen2.5-coder:7b", "q4_k_m")
        == "ollama/qwen2.5-coder:7b (q4_k_m)"
    )

    # Model without quantization
    assert format_model_badge("openai", "gpt-4o") == "openai/gpt-4o"

    # Backend only
    assert format_model_badge("Codex") == "Codex"
    assert format_model_badge("Codex", None, "fp16") == "Codex (fp16)"

    # Quantization already embedded in model name
    assert (
        format_model_badge("ollama", "qwen2.5-coder:7b-q4_k_m", "q4_k_m")
        == "ollama/qwen2.5-coder:7b-q4_k_m"
    )

    # Empty inputs
    assert format_model_badge() == ""
    assert format_model_badge(None, None, None) == ""


def test_runtime_panel_renders_model_badges_for_active_roles():
    """Verify runtime panel tree hierarchy and flat views display model badges for active roles."""
    state = AutonomousDeliveryState(
        session_id="test-badges",
        roles={
            "Architecture": RoleStatus(
                "Architecture",
                backend="Claude",
                model="claude-3-5-sonnet",
                state="RUNNING",
            ),
            "Backend": RoleStatus(
                "Backend",
                backend="ollama",
                model="qwen2.5-coder:7b",
                state="RUNNING",
            ),
            "Frontend": RoleStatus("Frontend", backend="AGY", state="WAITING"),
        },
    )

    # Render panel string with standard width
    out = render_runtime_panel_str(width=38, state=state)

    # Standard tree labels
    assert "◉ Architecture" in out
    assert "orchestrating" in out
    assert "├─ ● Backend" in out

    # Model badges rendered for active roles
    assert "Claude/claude-3-5-sonnet" in out
    assert "ollama/qwen2.5-coder:7b" in out


def test_render_roles_panel_includes_model_badges():
    """Verify cli.tui.app.render_roles_panel displays badges in detailed and compact modes."""
    state = AutonomousDeliveryState(
        session_id="test-roles-detailed",
        roles={
            "Backend": RoleStatus(
                "Backend",
                backend="ollama",
                model="qwen2.5-coder:7b",
                state="RUNNING",
            ),
            "Frontend": RoleStatus("Frontend", backend="AGY", state="WAITING"),
        },
    )

    # Detailed view (:roles)
    detailed = render_roles_panel(state, detailed=True)
    assert "=== AGENT ROLES & EXECUTION BACKENDS (:roles) ===" in detailed
    assert "Backend Agent" in detailed
    assert "Model: qwen2.5-coder:7b" in detailed
    assert "Badge: [ollama/qwen2.5-coder:7b]" in detailed

    # Compact dashboard view
    compact = render_roles_panel(state, detailed=False)
    assert "● Backend" in compact
    assert "[ollama/qwen2.5-coder:7b]" in compact


# ─── 2. TERMINAL RESIZE HANDLING & SAFE SIGNAL HANDLING ─────────────────────


def test_install_resize_handler_is_safe_across_platforms():
    """Verify install_resize_handler runs safely without throwing exceptions on any platform."""
    # Should not raise exception regardless of whether SIGWINCH is available
    handler = install_resize_handler()
    # On Windows hasattr(signal, 'SIGWINCH') is False, handler is None; on POSIX it returns previous handler
    if not hasattr(signal, "SIGWINCH"):
        assert handler is None


def test_check_terminal_resize_detection():
    """Verify check_terminal_resize detects dimension changes and SIGWINCH signals."""
    # 1. No change when dimensions match current
    with patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
        resized, cols, lines = check_terminal_resize(80, 24)
        assert not resized
        assert cols == 80
        assert lines == 24

    # 2. Detected when dimensions change
    with patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 36))):
        resized, cols, lines = check_terminal_resize(80, 24)
        assert resized
        assert cols == 120
        assert lines == 36

    # 3. Detected when SIGWINCH handler sets flag
    import cli.tui.app as app_mod

    app_mod._terminal_resized = True
    with patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
        resized, cols, lines = check_terminal_resize(80, 24)
        assert resized
        # Flag should be reset
        assert not app_mod._terminal_resized


# ─── 3. RICH.LIVE LOCALIZED WORKSPACE RENDERING ──────────────────────────────


def test_live_workspace_manager_non_tty_fallback():
    """Verify LiveWorkspaceManager safely no-ops without error in non-TTY / CI environments."""
    buf = io.StringIO()
    # Non-terminal console (like CI or piped stdout)
    non_tty_console = Console(file=buf, force_terminal=False)

    manager = create_live_workspace(
        session={"session_id": "test-sess", "agent": "gemini"},
        state=AutonomousDeliveryState("demo"),
        console=non_tty_console,
        width=120,
        height=24,
    )

    assert not manager.is_terminal
    assert manager.live is None
    assert not manager.is_active

    # Starting in non-TTY mode returns self and does not crash
    with manager:
        assert not manager.is_active
        manager.update("Updated conversation text")
        manager.handle_resize(100, 30)

    # Stream should remain unaffected by live cursor escapes
    assert buf.getvalue() == ""


def test_live_workspace_manager_active_terminal():
    """Verify LiveWorkspaceManager lifecycle, live updates, and resize adaptation in terminal mode."""
    buf = io.StringIO()
    # Simulated terminal console
    term_console = Console(file=buf, force_terminal=True, width=120, height=24)

    state = AutonomousDeliveryState("demo-live", session_id="live-1")
    manager = LiveWorkspaceManager(
        session={"session_id": "live-1", "agent": "gemini"},
        state=state,
        console=term_console,
        width=120,
        height=24,
    )

    assert manager.is_terminal

    with manager:
        assert manager.is_active
        assert manager.live is not None

        # Localized update without full-screen clearing
        manager.update(conversation_content="Live streaming chunk...")
        assert manager.is_active

        # Resize adaptation
        manager.handle_resize(100, 28)
        assert manager.width == 100
        assert manager.height == 28

    # After exit, manager is inactive
    assert not manager.is_active
    assert manager.live is None


# ─── 4. REDRAW INTEGRATION WITH LIVE MANAGER ────────────────────────────────


def test_redraw_full_screen_delegates_to_live_manager():
    """Verify redraw_full_screen routes updates to LiveWorkspaceManager when active."""
    mock_live = MagicMock()
    mock_live.is_active = True

    buf = io.StringIO()
    state = AutonomousDeliveryState("demo", session_id="test-live-redraw")
    session = {"session_id": "test-live-redraw", "agent": "gemini"}

    frame = redraw_full_screen(
        session=session,
        state=state,
        width=120,
        height=24,
        stream=buf,
        live_manager=mock_live,
    )

    # Live manager update was invoked with appropriate parameters
    mock_live.update.assert_called_once()
    # Stream should NOT have written escape codes because live_manager handled it
    assert buf.getvalue() == ""
    # Returned frame remains intact
    assert "StackMind Runtime" in frame


# ─── 5. RESPONSIVE LAYOUT BOUNDARY ADAPTATION ────────────────────────────────


def test_responsive_layout_transitions():
    """Verify layout adapts cleanly when switching between narrow and wide terminals."""
    # Under narrow threshold (<100 cols): single column, runtime hidden
    narrow_layout = compute_layout(80)
    assert not narrow_layout.show_runtime
    assert narrow_layout.conversation_width == 80
    assert narrow_layout.runtime_width == 0

    narrow_frame = render_workspace_layout_str("Narrow conversation", width=80)
    assert "Narrow conversation" in narrow_frame
    assert "StackMind Runtime" not in narrow_frame

    # Above narrow threshold (>=100 cols): two columns, runtime visible
    wide_layout = compute_layout(120)
    assert wide_layout.show_runtime
    assert wide_layout.conversation_width + wide_layout.runtime_width + 1 == 120

    wide_frame = render_workspace_layout_str("Wide conversation", width=120)
    assert "Wide conversation" in wide_frame
    assert "StackMind Runtime" in wide_frame


# ─── 6. STEP 3 & STEP 4 LIFECYCLE & PAUSE/RESUME VERIFICATION ─────────────────


def test_dispatch_delivery_command_pauses_and_resumes_live_manager():
    """Verify dispatch_delivery_command pauses LiveWorkspaceManager during turn streaming and restarts it."""
    mock_live = MagicMock()
    mock_live.is_active = True
    mock_live.stop.side_effect = lambda: setattr(mock_live, "is_active", False)

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = {"operation_id": "op-test-1"}
    mock_adapter.stream.return_value = iter([
        {"name": "turn.completed", "payload": {"operation_id": "op-test-1", "response": "done"}},
    ])

    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"status": "COMPLETED", "result": {"summary": "done"}}

    state = AutonomousDeliveryState("demo", session_id="test-live-pause")
    session = {"session_id": "test-live-pause", "agent": "gemini"}

    _, should_exit = dispatch_delivery_command(
        mock_adapter,
        mock_client,
        session,
        "test turn message",
        state,
        client_timeout=5.0,
        live_manager=mock_live,
    )

    assert should_exit is False
    # live_manager.stop() should have been called before streaming starts
    mock_live.stop.assert_called()
    # live_manager.start(include_composer=False) should have been called in finally
    mock_live.start.assert_called_with(include_composer=False)


def test_dispatch_delivery_command_stops_live_manager_on_exit():
    """Verify dispatch_delivery_command stops LiveWorkspaceManager when exit command is received."""
    mock_live = MagicMock()
    mock_live.is_active = True

    state = AutonomousDeliveryState("demo", session_id="test-live-exit")
    session = {"session_id": "test-live-exit", "agent": "gemini"}

    for exit_cmd in [":exit", ":quit", "q"]:
        mock_live.reset_mock()
        mock_live.is_active = True
        _, should_exit = dispatch_delivery_command(
            None,
            None,
            session,
            exit_cmd,
            state,
            live_manager=mock_live,
        )
        assert should_exit is True
        mock_live.stop.assert_called_once()

