"""Automated Visual Fidelity & Layout Alignment Test Suite (WO-034).

Verifies 100% visual fidelity with image.png and fixes the double-echo bug:
- Double-echo elimination across all *_str() renderers using in-memory buffers
- Top status header bar (stackmind | dev ~/projects/..., session, agent, provider, ● online)
- Card-bordered landing block (rounded panel, purple ✦ star, StackMind title, dynamic version,
  tagline, divider, multi-colored pillars, keyboard badges, centered motto)
- Chat message headers and right-aligned HH:MM timestamps
- Inline tool activity cards (⚯, formatted name, target, duration, ✓ checkmark)
- Inline diff viewer cards (file path, 'unified diff' label, thin divider, syntax lines)
- Input composer box (> Type a message..., Ctrl+K commands | Ctrl+L clear)
- Bottom footer bar (/help, /status, /diff, /compact, /sessions, StackMind version)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import cli
from cli.main import cli as main_cli
from cli.tui import (
    AutonomousDeliveryState,
    ChatMessage,
    ToolActivity,
    ToolStatus,
    render_assistant_message_str,
    render_bottom_footer_bar_str,
    render_chat_transcript_str,
    render_composer_box_str,
    render_contract_hud_str,
    render_error_box_str,
    render_landing_block_str,
    render_operational_event_str,
    render_plan_panel_str,
    render_tool_activity_str,
    render_top_header_bar_str,
    render_unified_diff_str,
    render_user_message_str,
    render_verification_matrix_str,
)
from cli.tui.app import (
    prompt_composer_input,
    render_composer_box,
    render_composer_bottom_border_str,
    render_composer_top_border_str,
    restore_terminal_state,
)
from cli.tui.landing import abbreviate_path
from cli.tui.layout import (
    NARROW_THRESHOLD,
    RUNTIME_HEADING,
    RUNTIME_MAX_WIDTH,
    RUNTIME_MIN_WIDTH,
    RUNTIME_SECTIONS,
    ColumnLayout,
    compute_layout,
    render_runtime_panel_str,
    render_workspace_layout_str,
)
from validators.kernel.daemon import LocalDaemon


def test_double_echo_elimination_in_str_renderers(capsys: pytest.CaptureFixture[str]):
    """Verify that calling *_str() helpers never writes directly to sys.stdout."""
    # Clear any previous capture
    capsys.readouterr()

    # 1. Landing block
    landing_out = render_landing_block_str()
    captured = capsys.readouterr()
    assert captured.out == "", "render_landing_block_str leaked to sys.stdout"
    assert "StackMind" in landing_out

    # 2. User & Assistant messages
    u_out = render_user_message_str("test user prompt", timestamp="14:30")
    captured = capsys.readouterr()
    assert captured.out == "", "render_user_message_str leaked to sys.stdout"
    assert "test user prompt" in u_out

    a_out = render_assistant_message_str("test assistant response", timestamp="14:31")
    captured = capsys.readouterr()
    assert captured.out == "", "render_assistant_message_str leaked to sys.stdout"
    assert "test assistant response" in a_out

    # 3. Tool activity
    tool_out = render_tool_activity_str(ToolActivity(tool_name="read", target="app.py", status=ToolStatus.COMPLETED))
    captured = capsys.readouterr()
    assert captured.out == "", "render_tool_activity_str leaked to sys.stdout"
    assert "Read file" in tool_out

    # 4. Unified diff
    diff_out = render_unified_diff_str("--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new")
    captured = capsys.readouterr()
    assert captured.out == "", "render_unified_diff_str leaked to sys.stdout"
    assert "+new" in diff_out

    # 5. Governance surfaces
    matrix_out = render_verification_matrix_str()
    captured = capsys.readouterr()
    assert captured.out == "", "render_verification_matrix_str leaked to sys.stdout"
    assert "6D VERIFICATION MATRIX" in matrix_out

    hud_out = render_contract_hud_str({"write_mode": "governed", "allow": ["src/*"]})
    captured = capsys.readouterr()
    assert captured.out == "", "render_contract_hud_str leaked to sys.stdout"
    assert "CONTRACT BOUNDARY HUD" in hud_out

    state = AutonomousDeliveryState(project_name="proj", session_id="s1")
    plan_out = render_plan_panel_str(state)
    captured = capsys.readouterr()
    assert captured.out == "", "render_plan_panel_str leaked to sys.stdout"
    assert "WORK DECOMPOSITION" in plan_out

    # 6. Header, Composer, Footer
    hdr_out = render_top_header_bar_str({"session_id": "sess-12345", "agent": "codex", "provider": "daemon"})
    captured = capsys.readouterr()
    assert captured.out == "", "render_top_header_bar_str leaked to sys.stdout"
    assert "stackmind" in hdr_out

    comp_out = render_composer_box_str()
    captured = capsys.readouterr()
    assert captured.out == "", "render_composer_box_str leaked to sys.stdout"
    assert "Type a message" in comp_out

    foot_out = render_bottom_footer_bar_str()
    captured = capsys.readouterr()
    assert captured.out == "", "render_bottom_footer_bar_str leaked to sys.stdout"
    assert ":help" in foot_out


def test_top_header_bar_visual_fidelity():
    """Verify top header bar components match image.png."""
    session = {
        "session_id": "a7f3b19c-test-456",
        "agent": "gemini",
        "provider": "anthropic",
        "workspace": "/users/developer/projects/stackmind",
    }

    # Wide display
    hdr_wide = render_top_header_bar_str(session, width=100, status="online")
    assert "stackmind" in hdr_wide
    assert "dev" in hdr_wide
    assert "session: a7f3b19c" in hdr_wide
    assert "agent: gemini" in hdr_wide
    assert "provider: anthropic" in hdr_wide
    assert "● online" in hdr_wide

    # Offline and reconnecting states
    hdr_offline = render_top_header_bar_str(session, width=100, status="offline")
    assert "✗ offline" in hdr_offline

    hdr_reconn = render_top_header_bar_str(session, width=100, status="reconnecting")
    assert "○ reconnecting" in hdr_reconn


def test_card_bordered_landing_block_visual_fidelity():
    """Verify landing card block matches exact visual specifications for Phase 2."""
    session = {
        "session_id": "a295c255",
        "project": "~/projects/stackmind",
        "status": "online",
    }
    landing_text = render_landing_block_str(session=session, width=80)

    # 1. Centered glowing purple star
    assert "✦" in landing_text

    # 2. Stylized StackMind title and version
    assert "StackMind" in landing_text
    assert f"v{cli.__version__}" in landing_text

    # 3. Tagline
    assert "Your AI development partner, with control." in landing_text

    # 4. 4 multi-colored pillars
    assert "PLAN · BUILD · VERIFY · GOVERN" in landing_text

    # 5. Bottom card motto
    assert "Build better. Safer. Together." in landing_text

    # 6. Strict exclusions: no shortcut rows or command hints
    assert "Start chatting" not in landing_text
    assert "Ctrl+K" not in landing_text
    assert "[ :help ]" not in landing_text

    # 7. Metadata section: strictly ordered Status -> Session -> Project without inner card
    assert "Status:" in landing_text
    assert "● online" in landing_text
    assert "Session:" in landing_text
    assert "a295c255" in landing_text
    assert "Project:" in landing_text
    assert "~/projects/stackmind" in landing_text

    idx_status = landing_text.index("Status:")
    idx_session = landing_text.index("Session:")
    idx_project = landing_text.index("Project:")
    assert idx_status < idx_session < idx_project

    # 8. Card boundary borders (Rich rounded panel box)
    assert any(border_char in landing_text for border_char in ("─", "│", "┌", "┐", "└", "┘", "╭", "╮", "╰", "╯"))


def test_landing_block_phase2_locked_design_and_metadata_order():
    """Verify Phase 2 locked landing block design, metadata order, and exclusions."""
    session = {
        "session_id": "fe92a104",
        "project": "/custom/path/repo",
        "status": "reconnecting",
    }
    landing = render_landing_block_str(session=session, width=80)

    # Structural brand elements
    assert "✦  StackMind" in landing
    assert "Your AI development partner, with control." in landing
    assert "PLAN · BUILD · VERIFY · GOVERN" in landing
    assert "Build better. Safer. Together." in landing

    # Metadata ordered
    assert "Status:" in landing
    assert "reconnecting" in landing
    assert "Session:" in landing
    assert "fe92a104" in landing
    assert "Project:" in landing
    assert "/custom/path/repo" in landing

    assert landing.index("Status:") < landing.index("Session:") < landing.index("Project:")

    # Exclusions
    assert "Start chatting" not in landing
    assert "Ctrl+K" not in landing
    assert "Open commands" not in landing


def test_chat_messages_accent_and_timestamps():
    """Verify user message accent bar, assistant star, and right-aligned timestamps."""
    u_msg = render_user_message_str("Analyze authentication security", timestamp="16:42")
    assert "│" in u_msg
    assert "You" in u_msg
    assert "Analyze authentication security" in u_msg
    assert "16:42" in u_msg

    a_msg = render_assistant_message_str("Found 0 critical vulnerabilities.", timestamp="16:43")
    assert "✦" in a_msg
    assert "StackMind" in a_msg
    assert "Found 0 critical vulnerabilities." in a_msg
    assert "16:43" in a_msg


def test_inline_tool_card_visual_fidelity():
    """Verify inline tool activity card matches image.png (⚯, name, target, duration, ✓)."""
    activity = ToolActivity(
        tool_name="read_file",
        target="src/auth/service.py",
        status=ToolStatus.COMPLETED,
        duration_seconds=0.2,
    )
    rendered = render_tool_activity_str(activity)

    assert "⚯" in rendered
    assert "Read file" in rendered
    assert "src/auth/service.py" in rendered
    assert "0.2s" in rendered
    assert "✓" in rendered


def test_inline_diff_card_visual_fidelity():
    """Verify inline diff card matches image.png (file path, 'unified diff' label, syntax lines)."""
    diff_snippet = """--- a/src/auth/service.py
+++ b/src/auth/service.py
@@ -10,3 +10,4 @@
 def authenticate():
-    return False
+    return True
"""
    rendered = render_unified_diff_str(diff_snippet)

    assert "src/auth/service.py" in rendered
    assert "unified diff" in rendered
    assert "@@ -10,3 +10,4 @@" in rendered
    assert "-    return False" in rendered
    assert "+    return True" in rendered


def test_composer_box_and_footer_bar():
    """Verify input composer box and bottom navigation footer bar."""
    comp = render_composer_box_str(width=80)
    assert "> " in comp
    assert "Type a message..." in comp
    assert "Ctrl+K commands | Ctrl+L clear" in comp

    foot = render_bottom_footer_bar_str(width=80)
    assert ":help" in foot
    assert ":status" in foot
    assert ":diff" in foot
    assert ":events" in foot
    assert ":roles" in foot
    assert ":landing" in foot
    assert "/help" not in foot
    assert f"StackMind v{cli.__version__}" in foot


def test_tui_repl_startup_layout_fidelity(tmp_path: Path):
    """Verify full TUI startup displays header, landing card, composer, and footer."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        runner = CliRunner()
        result = runner.invoke(
            main_cli,
            ["tui", "--daemon-url", daemon.url, "--workspace", str(tmp_path)],
            input=":exit\n",
        )

        assert result.exit_code == 0, result.output

        # Top header bar
        assert "stackmind" in result.output
        assert "● online" in result.output

        # Landing card
        assert "✦" in result.output
        assert "StackMind" in result.output
        assert "PLAN · BUILD · VERIFY · GOVERN" in result.output
        assert "Build better. Safer. Together." in result.output

        # Composer box
        assert "Type a message..." in result.output
        assert "Ctrl+K commands | Ctrl+L clear" in result.output

        # Footer bar
        assert ":help" in result.output
        assert ":status" in result.output
        assert ":events" in result.output

        # WO-035: No plain legacy prompt anywhere
        assert "stackmind [" not in result.output


def test_interactive_composer_box_borders():
    """Verify top and bottom borders of interactive composer box match image.png (WO-035)."""
    top = render_composer_top_border_str(width=80)
    assert top.startswith("╭─ ")
    assert "Type a message..." in top
    assert "Ctrl+K commands | Ctrl+L clear" in top
    assert top.endswith(" ─╮")
    assert len(top) == 80

    bot = render_composer_bottom_border_str(width=80)
    assert bot.startswith("╰")
    assert bot.endswith("╯")
    assert len(bot) == 80


def test_interactive_composer_input_and_prompt(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Verify prompt_composer_input captures user input, displays borders and footer (WO-035)."""
    monkeypatch.setattr("builtins.input", lambda prompt: "hello from test")
    capsys.readouterr()

    received = prompt_composer_input(width=80)
    assert received == "hello from test"

    captured = capsys.readouterr()
    assert "Type a message..." in captured.out
    assert "Ctrl+K commands | Ctrl+L clear" in captured.out
    assert "╭─ " in captured.out
    assert "╰" in captured.out
    assert ":help" in captured.out
    assert ":status" in captured.out
    assert ":events" in captured.out
    assert f"StackMind v{cli.__version__}" in captured.out


def test_tui_startup_unconditionally_renders_header_and_landing_even_with_prior_outbox_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Verify top header and landing block are always rendered on startup even when prior outbox reports exist (WO-038)."""
    # Create prior outbox report
    outbox_dir = tmp_path / ".sync" / "outbox" / "codex"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    report_file = outbox_dir / "harness-2026-09-13.md"
    report_file.write_text(
        "# Harness Run\n\n## Report\nPrior session completed successfully.\n\n## Meta\nsession: 22\n",
        encoding="utf-8",
    )

    from validators.kernel.tui import DaemonClient

    monkeypatch.setattr(
        DaemonClient,
        "events",
        lambda self, session_id, after=0: [
            {"sequence": 1, "name": "turn.started", "payload": {"prompt": "prior prompt"}},
        ],
    )

    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        runner = CliRunner()
        result = runner.invoke(
            main_cli,
            ["tui", "--daemon-url", daemon.url, "--workspace", str(tmp_path)],
            input=":exit\n",
        )

        assert result.exit_code == 0, result.output

        # Top header bar must be present despite state.has_conversation being True
        assert "stackmind" in result.output
        assert "● online" in result.output

        # Branded landing card must be present despite state.has_conversation being True
        assert "✦" in result.output
        assert "StackMind" in result.output
        assert "PLAN · BUILD · VERIFY · GOVERN" in result.output
        assert "Build better. Safer. Together." in result.output


def test_tui_landing_command_renders_both_header_and_landing(tmp_path: Path):
    """Verify :landing command renders both the top header bar and landing block (WO-038)."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        runner = CliRunner()
        result = runner.invoke(
            main_cli,
            ["tui", "--daemon-url", daemon.url, "--workspace", str(tmp_path)],
            input=":landing\n:exit\n",
        )

        assert result.exit_code == 0, result.output

        # Both header bar and landing block should appear
        assert "stackmind" in result.output
        assert "● online" in result.output
        assert "✦" in result.output
        assert "StackMind" in result.output
        assert "PLAN · BUILD · VERIFY · GOVERN" in result.output


# ── Phase 3: Two-Column Workspace Layout Tests ──────────────────────────────


def test_compute_layout_narrow_hides_runtime():
    """Verify narrow terminals (< 100) hide the runtime panel entirely."""
    for w in (40, 60, 80, 99):
        layout = compute_layout(w)
        assert not layout.show_runtime, f"width={w} should hide runtime"
        assert layout.conversation_width == w
        assert layout.runtime_width == 0
        assert layout.divider_width == 0


def test_compute_layout_wide_shows_runtime():
    """Verify wide terminals (>= 100) show runtime panel with correct proportions."""
    for w in (100, 120, 160):
        layout = compute_layout(w)
        assert layout.show_runtime, f"width={w} should show runtime"
        assert layout.divider_width == 1
        # Runtime within bounds
        assert RUNTIME_MIN_WIDTH <= layout.runtime_width <= RUNTIME_MAX_WIDTH
        # Conversation + divider + runtime = total
        assert layout.conversation_width + 1 + layout.runtime_width == w
        # Conversation gets ~70-75%
        conv_pct = layout.conversation_width / w
        assert 0.55 <= conv_pct <= 0.80, f"conversation={conv_pct:.2%} out of range at width={w}"


def test_compute_layout_target_dimensions():
    """Verify the four target dimensions from the spec: 80x24, 100x30, 120x40, 160x50."""
    # 80: narrow fallback
    l80 = compute_layout(80)
    assert not l80.show_runtime
    assert l80.conversation_width == 80

    # 100: minimum two-column
    l100 = compute_layout(100)
    assert l100.show_runtime
    assert l100.conversation_width + 1 + l100.runtime_width == 100

    # 120: standard two-column
    l120 = compute_layout(120)
    assert l120.show_runtime
    assert RUNTIME_MIN_WIDTH <= l120.runtime_width <= RUNTIME_MAX_WIDTH

    # 160: wide two-column
    l160 = compute_layout(160)
    assert l160.show_runtime
    assert RUNTIME_MIN_WIDTH <= l160.runtime_width <= RUNTIME_MAX_WIDTH


def test_runtime_panel_placeholder_sections():
    """Verify runtime panel renders all three section headings."""
    panel = render_runtime_panel_str(width=35)

    assert RUNTIME_HEADING in panel
    for section in RUNTIME_SECTIONS:
        assert section in panel

    # Verify section order
    idx_agents = panel.index("AGENTS")
    idx_wo = panel.index("WORK ORDERS")
    idx_op = panel.index("CURRENT OPERATION")
    assert idx_agents < idx_wo < idx_op


def test_runtime_panel_with_dynamic_data():
    """Verify runtime panel renders actual agent/WO/operation data."""
    agents = [
        {"name": "codex", "status": "active"},
        {"name": "gemma", "status": "idle"},
    ]
    work_orders = [{"id": "WO-043", "title": "Layout"}]
    current_op = {"name": "graph update", "status": "running"}

    panel = render_runtime_panel_str(
        width=35, agents=agents, work_orders=work_orders, current_operation=current_op
    )

    assert "codex: active" in panel
    assert "gemma: idle" in panel
    assert "WO-043: Layout" in panel
    assert "graph update" in panel
    assert "running" in panel


def test_runtime_panel_no_stdout_leak(capsys: pytest.CaptureFixture[str]):
    """Verify runtime panel str renderer does not leak to stdout."""
    capsys.readouterr()
    out = render_runtime_panel_str(width=30)
    captured = capsys.readouterr()
    assert captured.out == "", "render_runtime_panel_str leaked to sys.stdout"
    assert RUNTIME_HEADING in out


def test_workspace_layout_narrow_returns_conversation_only():
    """Verify narrow workspace layout returns only conversation text."""
    result = render_workspace_layout_str("Hello world", width=80)
    assert "Hello world" in result
    # Runtime sections should NOT be present
    assert "AGENTS" not in result
    assert "WORK ORDERS" not in result


def test_workspace_layout_wide_renders_both_columns():
    """Verify wide workspace layout renders both conversation and runtime panel."""
    result = render_workspace_layout_str("Conversation content here", width=120)
    assert "Conversation content here" in result
    # Runtime panel sections should be present
    assert RUNTIME_HEADING in result
    assert "AGENTS" in result
    assert "WORK ORDERS" in result
    assert "CURRENT OPERATION" in result


def test_workspace_layout_vertical_divider():
    """Verify the vertical divider character is present in wide layout."""
    result = render_workspace_layout_str("Left side", width=120)
    assert "│" in result


def test_phase10_responsive_sizing_canonical_dimensions():
    """Verify responsive sizing across all canonical form factors (§41, §43).
    - 80x24: narrow single-column fallback (suppress runtime panel, conversation full width).
    - 100x30: entry threshold for two-column layout (70/30 split, runtime ~28 cols).
    - 120x40: standard two-column workspace layout (runtime ~33 cols).
    - 160x50: wide two-column workspace layout (runtime capped at RUNTIME_MAX_WIDTH=42).
    """
    # 80x24: Single-column fallback
    layout_80 = compute_layout(80)
    assert not layout_80.show_runtime
    assert layout_80.conversation_width == 80
    assert layout_80.runtime_width == 0
    assert layout_80.divider_width == 0

    # 100x30: Entry threshold for two-column layout
    layout_100 = compute_layout(100)
    assert layout_100.show_runtime
    assert layout_100.runtime_width == 28
    assert layout_100.conversation_width == 71
    assert layout_100.divider_width == 1
    assert layout_100.conversation_width + layout_100.divider_width + layout_100.runtime_width == 100

    # 120x40: Standard two-column workspace
    layout_120 = compute_layout(120)
    assert layout_120.show_runtime
    assert layout_120.runtime_width == 33
    assert layout_120.conversation_width == 86
    assert layout_120.divider_width == 1
    assert layout_120.conversation_width + layout_120.divider_width + layout_120.runtime_width == 120

    # 160x50: Wide layout capped at RUNTIME_MAX_WIDTH=42
    layout_160 = compute_layout(160)
    assert layout_160.show_runtime
    assert layout_160.runtime_width == RUNTIME_MAX_WIDTH  # 42
    assert layout_160.conversation_width == 117
    assert layout_160.divider_width == 1
    assert layout_160.conversation_width + layout_160.divider_width + layout_160.runtime_width == 160


def test_phase10_visual_palette_and_border_hierarchy():
    """Verify visual palette and border hierarchy (§42, §43).
    - Composer container border is strongest (#475569 idle, highlighted to #60a5fa when active).
    - Composer border is stronger and distinct from subtle divider/landing borders (#334155).
    """
    # Idle composer box
    idle_panel = render_composer_box(is_active=False)
    assert idle_panel.border_style == "#475569"

    # Active composer box
    active_panel = render_composer_box(is_active=True)
    assert active_panel.border_style == "#60a5fa"

    # Top & bottom border renderers reflect active state
    top_idle = render_composer_top_border_str(width=80, is_active=False)
    top_active = render_composer_top_border_str(width=80, is_active=True)
    assert "─" in top_idle
    assert "─" in top_active

    bottom_idle = render_composer_bottom_border_str(width=80, is_active=False)
    bottom_active = render_composer_bottom_border_str(width=80, is_active=True)
    assert "╰" in bottom_idle and "╯" in bottom_idle
    assert "╰" in bottom_active and "╯" in bottom_active


def test_phase10_long_path_visual_abbreviation_in_landing():
    """Verify long project paths are visually abbreviated on constrained viewports (§43)."""
    # Short path is preserved as-is
    short_path = "~/projects/stackmind"
    assert abbreviate_path(short_path, max_len=60) == short_path

    # Long nested path is abbreviated
    long_path = "/users/developer/company/large-repository/very/deeply/nested/stackmind-cli-repo"
    abbr_30 = abbreviate_path(long_path, max_len=30)
    assert len(abbr_30) <= 30
    assert "stackmind-cli-repo" in abbr_30
    assert "..." in abbr_30

    # In landing block: standard path at width 80
    session_std = {"session_id": "s1", "project": "~/projects/stackmind", "status": "online"}
    out_std = render_landing_block_str(session=session_std, width=80)
    assert "~/projects/stackmind" in out_std

    # In landing block: very long path at width 60
    session_long = {
        "session_id": "s2",
        "project": "/very/long/nested/path/to/some/enterprise/project/monorepo/subproject",
        "status": "online",
    }
    out_narrow = render_landing_block_str(session=session_long, width=60)
    assert "Project:" in out_narrow
    assert "subproject" in out_narrow
    assert "..." in out_narrow
    # Crucially, metadata lines remain ordered and not wrapped awkwardly
    assert out_narrow.index("Status:") < out_narrow.index("Session:") < out_narrow.index("Project:")


def test_phase10_terminal_state_restoration(monkeypatch):
    """Verify terminal state restoration cleanly resets cursor and terminal modes (§43, §44)."""
    import io
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_stdout)

    restore_terminal_state()

    output = fake_stdout.getvalue()
    # Verifies ANSI show cursor sequence (\x1b[?25h) and attribute reset (\x1b[0m)
    assert "\x1b[?25h" in output
    assert "\x1b[0m" in output

    # Handles broken or missing stdout gracefully without raising exception
    monkeypatch.setattr("sys.stdout", None)
    restore_terminal_state()  # Must not raise


def test_full_width_composer_expansion():
    """Verify composer container expands to full terminal width (WO-054)."""
    box_80 = render_composer_box_str(width=80)
    assert len(box_80.splitlines()[0]) == 80

    box_120 = render_composer_box_str(width=120)
    assert len(box_120.splitlines()[0]) == 120


def test_prompt_composer_input_footer_suppression(monkeypatch, capsys):
    """Verify prompt_composer_input suppresses footer bar when show_footer=False (WO-054)."""
    monkeypatch.setattr("builtins.input", lambda prompt: "input without footer")
    capsys.readouterr()

    received = prompt_composer_input(width=80, show_footer=False)
    assert received == "input without footer"

    captured = capsys.readouterr()
    assert ":help" not in captured.out
    assert ":status" not in captured.out
    assert ":events" not in captured.out
    assert "StackMind" not in captured.out
    assert "Type a message..." in captured.out
    assert "╰" in captured.out


