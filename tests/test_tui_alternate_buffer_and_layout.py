"""Tests for WO-053: Full-Screen Alternate Buffer & Persistent Layout.

Verifies:
1. Alternate screen buffer lifecycle (enter_alternate_screen, exit_alternate_screen, restore_terminal_state).
2. Viewport slicing and scroll offsets (slice_conversation_viewport).
3. Height-bounded runtime panel rendering (render_runtime_panel, render_runtime_panel_str).
4. Full-screen workspace composition (render_full_screen_workspace).
5. Full-screen redraw engine (redraw_full_screen).
6. Reactive runtime panel persistence across state mutations.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cli.main import cli as main_cli
from cli.tui.app import (
    enter_alternate_screen,
    exit_alternate_screen,
    redraw_full_screen,
    restore_terminal_state,
)
from cli.tui.layout import (
    compute_layout,
    render_full_screen_workspace,
    render_runtime_panel,
    render_runtime_panel_str,
    render_workspace_layout_str,
    slice_conversation_viewport,
)
from cli.tui.state import (
    AutonomousDeliveryState,
    ChatMessage,
    OperationNode,
    RoleStatus,
    WorkOrderItem,
)


# ─── 1. ALTERNATE SCREEN BUFFER LIFECYCLE ────────────────────────────────────


def test_enter_alternate_screen_emits_expected_sequences():
    """Verify enter_alternate_screen writes alternate buffer switch + clear + home cursor."""
    buf = io.StringIO()
    enter_alternate_screen(stream=buf)
    out = buf.getvalue()
    # \x1b[?1049h: enter alternate screen
    assert "\x1b[?1049h" in out
    # \x1b[2J: clear screen
    assert "\x1b[2J" in out
    # \x1b[H: cursor home
    assert "\x1b[H" in out


def test_exit_alternate_screen_emits_expected_sequence():
    """Verify exit_alternate_screen writes primary buffer restore sequence."""
    buf = io.StringIO()
    exit_alternate_screen(stream=buf)
    out = buf.getvalue()
    # \x1b[?1049l: restore primary screen buffer
    assert "\x1b[?1049l" in out


def test_restore_terminal_state_emits_alternate_exit_and_cursor_reset():
    """Verify restore_terminal_state exits alternate screen, shows cursor, and resets attributes."""
    buf = io.StringIO()
    restore_terminal_state(stream=buf)
    out = buf.getvalue()
    assert "\x1b[?1049l" in out
    assert "\x1b[?25h" in out
    assert "\x1b[0m" in out


def test_alternate_screen_functions_handle_broken_or_none_stream(monkeypatch):
    """Verify lifecycle functions never crash even if stdout is None or broken."""
    monkeypatch.setattr("sys.stdout", None)
    # Must not raise
    enter_alternate_screen()
    exit_alternate_screen()
    restore_terminal_state()


# ─── 2. VIEWPORT SLICING & SCROLL OFFSETS ───────────────────────────────────


def test_slice_conversation_viewport_empty():
    """Empty content produces blank lines when padded or empty string when unpadded."""
    padded = slice_conversation_viewport("", viewport_height=10, pad=True)
    lines = padded.split("\n")
    assert len(lines) == 10
    assert all(line == "" for line in lines)

    unpadded = slice_conversation_viewport("", viewport_height=10, pad=False)
    assert unpadded == ""


def test_slice_conversation_viewport_shorter_than_height():
    """Content with fewer lines than viewport_height is padded to full viewport height."""
    content = "Line 1\nLine 2\nLine 3"
    padded = slice_conversation_viewport(content, viewport_height=6, pad=True)
    lines = padded.split("\n")
    assert len(lines) == 6
    assert lines[0] == "Line 1"
    assert lines[1] == "Line 2"
    assert lines[2] == "Line 3"
    assert lines[3] == ""
    assert lines[4] == ""
    assert lines[5] == ""


def test_slice_conversation_viewport_longer_than_height_follow_bottom():
    """Content with more lines than viewport_height shows the bottom tail by default."""
    lines = [f"Line {i}" for i in range(1, 21)]
    content = "\n".join(lines)
    viewport = slice_conversation_viewport(content, viewport_height=5, scroll=0)
    v_lines = viewport.split("\n")
    assert len(v_lines) == 5
    assert v_lines == ["Line 16", "Line 17", "Line 18", "Line 19", "Line 20"]


def test_slice_conversation_viewport_scroll_up():
    """Positive scroll offset scrolls upward toward earlier lines."""
    lines = [f"Line {i}" for i in range(1, 21)]
    content = "\n".join(lines)
    # Scrolling up by 5 lines should show lines 11 through 15
    viewport = slice_conversation_viewport(content, viewport_height=5, scroll=5)
    v_lines = viewport.split("\n")
    assert len(v_lines) == 5
    assert v_lines == ["Line 11", "Line 12", "Line 13", "Line 14", "Line 15"]


def test_slice_conversation_viewport_scroll_clamp():
    """Excessive or negative scroll offsets are clamped gracefully."""
    lines = [f"Line {i}" for i in range(1, 11)]
    content = "\n".join(lines)

    # Scroll beyond top clamps to the very top lines
    viewport_top = slice_conversation_viewport(content, viewport_height=4, scroll=100)
    assert viewport_top.split("\n") == ["Line 1", "Line 2", "Line 3", "Line 4"]

    # Negative scroll clamps to bottom
    viewport_bottom = slice_conversation_viewport(content, viewport_height=4, scroll=-10)
    assert viewport_bottom.split("\n") == ["Line 7", "Line 8", "Line 9", "Line 10"]


# ─── 3. HEIGHT-BOUNDED RUNTIME PANEL ─────────────────────────────────────────


def test_render_runtime_panel_respects_height():
    """Verify render_runtime_panel accepts height parameter and includes core sections."""
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    state.roles["Backend"] = RoleStatus("Backend", backend="Codex", state="RUNNING")
    state.work_orders.append(WorkOrderItem("WO-053", "TUI Layout", status="IN_PROGRESS"))
    state.operations["op-1"] = OperationNode("op-1", "Compile", role="Backend", status="RUNNING")

    rendered = render_runtime_panel_str(state=state, width=32, height=30)
    assert "StackMind Runtime" in rendered
    assert "AGENTS" in rendered
    assert "WORK ORDERS" in rendered
    assert "CURRENT OPERATION" in rendered
    assert "Backend" in rendered
    assert "WO-053" in rendered


def test_render_workspace_layout_str_with_height():
    """Verify render_workspace_layout_str accepts height and aligns columns."""
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    conv_content = "Hello StackMind\nReady to assist."
    rendered = render_workspace_layout_str(
        conv_content,
        width=120,
        height=15,
        state=state,
    )
    assert "│" in rendered
    assert "Hello StackMind" in rendered
    assert "StackMind Runtime" in rendered


# ─── 4. FULL-SCREEN WORKSPACE COMPOSITION ───────────────────────────────────


def test_render_full_screen_workspace_composition():
    """Verify render_full_screen_workspace generates complete full-screen frame."""
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    session = {"session_id": "sess-001", "agent": "codex", "provider": "daemon"}

    frame = render_full_screen_workspace(
        session=session,
        state=state,
        width=120,
        height=24,
        shortcuts="Ctrl+K commands | Ctrl+L clear",
    )

    # Frame must contain top header
    assert "stackmind" in frame
    assert "codex" in frame
    # Frame must contain runtime panel
    assert "StackMind Runtime" in frame
    # Frame must contain composer box borders and shortcuts
    assert "Ctrl+K commands" in frame
    assert "Type a message..." in frame


def test_render_full_screen_workspace_with_messages():
    """Verify full-screen workspace renders conversation messages within left column."""
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    state.add_message("user", "What is the status of Phase 2?")
    state.add_message("assistant", "Phase 2 WO-053 is currently in progress.")

    frame = render_full_screen_workspace(
        session={"session_id": "sess-001"},
        state=state,
        width=120,
        height=26,
    )

    assert "What is the status of Phase 2?" in frame
    assert "Phase 2 WO-053 is currently in progress." in frame
    assert "StackMind Runtime" in frame


# ─── 5. FULL-SCREEN REDRAW ENGINE ────────────────────────────────────────────


def test_redraw_full_screen_writes_to_stream_and_returns_frame():
    """Verify redraw_full_screen outputs ANSI positioning escape and returns frame."""
    buf = io.StringIO()
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    session = {"session_id": "sess-001", "agent": "codex"}

    frame = redraw_full_screen(
        session=session,
        state=state,
        width=120,
        height=22,
        clear=False,
        stream=buf,
    )

    written = buf.getvalue()
    # By default, redraw homes cursor without clear
    assert written.startswith("\x1b[H")
    assert "stackmind" in written
    assert "StackMind Runtime" in written
    assert frame in written


def test_redraw_full_screen_with_clear():
    """Verify redraw_full_screen with clear=True emits full ANSI screen clear."""
    buf = io.StringIO()
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    session = {"session_id": "sess-001", "agent": "codex"}

    redraw_full_screen(
        session=session,
        state=state,
        width=120,
        height=22,
        clear=True,
        stream=buf,
    )

    written = buf.getvalue()
    # When clear=True, prefix is \x1b[2J\x1b[H
    assert written.startswith("\x1b[2J\x1b[H")


# ─── 6. REACTIVE RUNTIME PANEL PERSISTENCE ──────────────────────────────────


def test_runtime_panel_reflects_state_mutations_reactively():
    """Verify runtime panel dynamically updates when agent or work order state changes."""
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")

    # Initial state: default workers are waiting
    initial_render = render_runtime_panel_str(state=state, width=32)
    assert "waiting" in initial_render
    assert "Architecture" in initial_render

    # Simulate daemon SSE event updating role to RUNNING and adding an operation
    state.roles["Backend"].state = "RUNNING"
    state.operations["op-42"] = OperationNode(
        "op-42",
        "Refactor layout",
        role="Backend",
        status="RUNNING",
    )

    updated_render = render_runtime_panel_str(state=state, width=32)
    assert "implementing" in updated_render
    assert "op-42" in updated_render or "Refactor" in updated_render


# ─── 7. COMPOSER DEDUPLICATION & GEOMETRY (WO-055) ──────────────────────────


def test_render_full_screen_workspace_without_composer():
    """Verify include_composer=False omits the bottom composer box (WO-055)."""
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    session = {"session_id": "sess-001", "agent": "codex"}

    frame_with = render_full_screen_workspace(
        session=session,
        state=state,
        width=120,
        height=24,
        include_composer=True,
    )
    frame_without = render_full_screen_workspace(
        session=session,
        state=state,
        width=120,
        height=24,
        include_composer=False,
    )

    # Frame with composer contains the composer prompt and border
    assert "Type a message..." in frame_with
    assert "Ctrl+K commands" in frame_with

    # Frame without composer must NOT contain the static composer box
    assert "Type a message..." not in frame_without
    assert "Ctrl+K commands" not in frame_without

    # Height without composer must be exactly 3 lines shorter than frame with composer
    lines_with = len(frame_with.splitlines())
    lines_without = len(frame_without.splitlines())
    assert lines_without == lines_with - 3


def test_redraw_full_screen_without_composer_emits_newline_for_prompt_positioning():
    """Verify redraw_full_screen with include_composer=False appends newline to place cursor at composer row (WO-055)."""
    buf = io.StringIO()
    state = AutonomousDeliveryState(project_name="demo-proj", session_id="sess-001")
    session = {"session_id": "sess-001", "agent": "codex"}

    redraw_full_screen(
        session=session,
        state=state,
        width=120,
        height=24,
        include_composer=False,
        stream=buf,
    )

    out = buf.getvalue()
    # Must position cursor with newline after frame to ensure prompt_composer_input begins on row (height - 3)
    assert out.endswith("\n")
    assert "Type a message..." not in out
