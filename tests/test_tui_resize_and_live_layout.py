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
    LayoutTier,
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


# ─── 7. WO-019: MULTI-TIER DYNAMIC RESIZE TRANSITIONS & STABILITY ───────────


def test_dynamic_resize_across_all_breakpoint_transitions():
    """Verify LiveWorkspaceManager cleanly handles repeated multi-tier resize transitions (WO-019 AC-4)."""
    state = AutonomousDeliveryState("demo", session_id="test-resize-multitier")
    state.add_message("user", "Testing dynamic resizing across breakpoints")
    state.add_message("assistant", "Layout adapting cleanly.")
    session = {"session_id": "test-resize-multitier", "agent": "gemini"}

    # Start manager at standard 120x30 (NORMAL tier)
    manager = LiveWorkspaceManager(
        session=session,
        state=state,
        width=120,
        height=30,
    )

    # Dynamic transition sequence: 120 -> 90 -> 160 -> 70 -> 200 -> 120
    resize_sequence = [
        (90, 25, LayoutTier.NARROW, False, True),
        (160, 40, LayoutTier.WIDE, True, False),
        (70, 20, LayoutTier.VERY_NARROW, False, False),
        (200, 50, LayoutTier.VERY_WIDE, True, False),
        (120, 30, LayoutTier.NORMAL, True, False),
    ]

    for new_w, new_h, expected_tier, exp_runtime, exp_badge in resize_sequence:
        manager.handle_resize(new_w, new_h)
        assert manager.width == new_w
        assert manager.height == new_h

        layout = compute_layout(new_w)
        assert layout.tier == expected_tier
        assert layout.show_runtime is exp_runtime
        assert layout.show_runtime_badge is exp_badge

        renderable = manager.build_renderable(include_composer=True)
        frame_text = renderable.plain
        lines = frame_text.splitlines()

        # Strict height constraint preserved across every resize
        assert len(lines) == new_h, (
            f"Resize to {new_w}x{new_h} ({expected_tier.value}) produced {len(lines)} lines, expected {new_h}"
        )

        # No border clipping or malformed boxes; flush at column 0 (WO-022 AC-4)
        comp_bottom = lines[-2]
        assert not comp_bottom.startswith(" ")
        assert comp_bottom[0] in ("╰", "└")
        assert comp_bottom[-1] in ("╯", "┘")


def test_resize_preserves_conversation_scroll_without_geometry_drift():
    """Verify dynamic resizing recalculates scroll bounds without overflow or negative offsets (WO-019 AC-4)."""
    state = AutonomousDeliveryState("demo", session_id="test-resize-scroll")
    for i in range(50):
        state.add_message("user" if i % 2 == 0 else "assistant", f"Message line {i}")

    # Start with scroll offset
    state.conversation_scroll.viewport_height = 20
    state.scroll_conversation_up(10)
    assert state.conversation_scroll.scroll_offset == 10

    manager = LiveWorkspaceManager(
        session={"session_id": "test-resize-scroll"},
        state=state,
        width=120,
        height=30,
    )

    # Resize from 30 -> 15 (shrinking height)
    manager.handle_resize(120, 15)
    renderable_small = manager.build_renderable(include_composer=True)
    lines_small = renderable_small.plain.splitlines()
    assert len(lines_small) == 15
    assert state.conversation_scroll.scroll_offset >= 0

    # Resize from 120x15 -> 80x45 (expanding height, narrowing width)
    manager.handle_resize(80, 45)
    renderable_tall = manager.build_renderable(include_composer=True)
    lines_tall = renderable_tall.plain.splitlines()
    assert len(lines_tall) == 45
    assert state.conversation_scroll.scroll_offset >= 0


def test_dynamic_resize_120_70_140_cache_invalidation_and_panel_visibility():
    """Verify resizing across 120x40 -> 70x20 -> 140x50 adapts panels, cache, and line budgets."""
    from cli.tui.chat import _TRANSCRIPT_CACHE, clear_transcript_cache

    state = AutonomousDeliveryState("demo", session_id="test-resize-120-70-140")
    state.add_message("user", "Can you refactor the backend database layer?")
    state.add_message("assistant", "Sure, starting architectural decomposition.")
    session = {"session_id": "test-resize-120-70-140", "agent": "architect", "provider": "daemon"}

    manager = LiveWorkspaceManager(session=session, state=state, width=120, height=40)

    # 1. At 120x40 (width >= 100, show_runtime is True)
    renderable_120 = manager.build_renderable(include_composer=True)
    lines_120 = renderable_120.plain.splitlines()
    assert len(lines_120) == 40
    layout_120 = compute_layout(120)
    assert layout_120.show_runtime is True
    assert "StackMind Runtime" in renderable_120.plain
    # Cache was populated
    assert len(_TRANSCRIPT_CACHE) > 0

    # 2. Resize to 70x20 (width < 95 -> narrow, show_runtime is False, side panel collapsed)
    with patch("cli.tui.chat.clear_transcript_cache", wraps=clear_transcript_cache) as mock_clear:
        manager.handle_resize(70, 20)
        mock_clear.assert_called()

    renderable_70 = manager.build_renderable(include_composer=True)
    lines_70 = renderable_70.plain.splitlines()
    assert len(lines_70) == 20
    layout_70 = compute_layout(70)
    assert layout_70.show_runtime is False
    assert "StackMind Runtime" not in renderable_70.plain

    # 3. Resize to 140x50 (wide breakpoint, runtime panel re-appears)
    with patch("cli.tui.chat.clear_transcript_cache", wraps=clear_transcript_cache) as mock_clear_2:
        manager.handle_resize(140, 50)
        mock_clear_2.assert_called()

    renderable_140 = manager.build_renderable(include_composer=True)
    lines_140 = renderable_140.plain.splitlines()
    assert len(lines_140) == 50
    layout_140 = compute_layout(140)
    assert layout_140.show_runtime is True
    assert "StackMind Runtime" in renderable_140.plain


def test_long_multiline_composer_and_tall_code_blocks_visual_line_sliced():
    """Verify long multiline pastes and tall assistant code blocks do not overflow viewport."""
    state = AutonomousDeliveryState("demo", session_id="test-tall-blocks")
    tall_code = "```python\n" + "\n".join(f"def func_{i}():\n    return {i}" for i in range(40)) + "\n```"
    state.add_message("assistant", f"Here is the implementation:\n{tall_code}")
    session = {"session_id": "test-tall-blocks", "agent": "architect", "provider": "daemon"}

    # Large multiline composer paste
    large_paste = "\n".join(f"paste line {i} with lots of additional text content" for i in range(25))

    frame = render_full_screen_workspace(
        session=session,
        state=state,
        width=100,
        height=24,
        composer_content=large_paste,
        include_composer=True,
    )
    lines = frame.split("\n")
    # Total lines strictly equal to height
    assert len(lines) == 24
    # Status bar preserved at the bottom
    assert "test-tal" in lines[-1]
    assert "online" in lines[-1]
    # Composer box bottom border at lines[-2]
    assert lines[-2][0] in ("╰", "└")


def test_resize_during_idle_prompt_input_triggers_atomic_repaint(monkeypatch):
    """Verify that resizing while idle at prompt triggers on_resize and atomic screen repaint."""
    import os
    import shutil
    from cli.tui.app import prompt_composer_input
    from cli.tui.keyboard import Key

    state = AutonomousDeliveryState("demo-resize-idle", session_id="test-sess-resize")
    session = {"session_id": "test-sess-resize", "agent": "architect", "provider": "daemon"}

    # Mock terminal sizes: start at 80x24, then resize to 100x30
    sizes = [os.terminal_size((80, 24)), os.terminal_size((100, 30))]
    def mock_get_terminal_size(fallback=(80, 24)):
        if sizes:
            return sizes[0]
        return os.terminal_size((100, 30))

    monkeypatch.setattr(shutil, "get_terminal_size", mock_get_terminal_size)

    # Key stream: first None (idle tick where resize occurs), then ENTER to complete
    called_resize = []
    def mock_on_resize(cols, lines, ed=None):
        called_resize.append((cols, lines))

    # Trigger resize after first check
    key_steps = [None, Key.ENTER]
    def mock_read_next_key(key_stream=None, timeout=None):
        if sizes and len(sizes) > 1:
            # Pop size to simulate resize event during poll tick
            sizes.pop(0)
        if key_steps:
            return key_steps.pop(0)
        return Key.ENTER

    monkeypatch.setattr("cli.tui.keyboard.read_next_key", mock_read_next_key)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    stdout_writes = []
    monkeypatch.setattr("sys.stdout.write", lambda s: stdout_writes.append(s))
    monkeypatch.setattr("sys.stdout.flush", lambda: None)

    val = prompt_composer_input(
        width=80,
        state=state,
        session=session,
        on_resize=mock_on_resize,
        full_screen=True,
    )

    # on_resize was called with the new dimensions
    assert len(called_resize) >= 1
    assert called_resize[0] == (100, 30)


def test_empty_conversation_landing_state_frame_strictly_fits_height():
    """Verify landing block + composer + status bar strictly fits within height lines without trailing newline."""
    state = AutonomousDeliveryState("demo-empty", session_id="test-landing-height")
    session = {"session_id": "test-landing-height", "agent": "codex", "provider": "daemon"}

    for h in (16, 20, 24, 30):
        frame = render_full_screen_workspace(
            session=session,
            state=state,
            width=100,
            height=h,
            include_composer=True,
        )
        # Must have NO trailing newline
        assert not frame.endswith("\n")
        lines = frame.split("\n")
        # Exactly h lines (h - 1 newlines)
        assert len(lines) == h, f"Expected {h} lines for height={h}, got {len(lines)}"
        # Last line must be status bar
        assert "test-lan" in lines[-1]
        assert "online" in lines[-1]
        # Composer bottom border must be line h - 2
        assert lines[-2][0] in ("╰", "└")



