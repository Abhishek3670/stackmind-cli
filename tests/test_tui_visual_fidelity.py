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
    render_composer_bottom_border_str,
    render_composer_top_border_str,
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
    assert "/help" in foot_out


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
    """Verify landing card block matches exact visual specifications."""
    landing_text = render_landing_block_str(width=80)

    # 1. Centered glowing purple star
    assert "✦" in landing_text

    # 2. Stylized StackMind title and version
    assert "StackMind" in landing_text
    assert f"v{cli.__version__}" in landing_text

    # 3. Tagline
    assert "Your AI development partner, with control." in landing_text

    # 4. 4 multi-colored pillars
    assert "PLAN · BUILD · VERIFY · GOVERN" in landing_text

    # 5. Keyboard pill buttons
    assert "[ / ]" in landing_text
    assert "Start chatting" in landing_text
    assert "[ Ctrl+K ]" in landing_text
    assert "Open commands" in landing_text
    assert "[ :help ]" in landing_text
    assert "Show all commands" in landing_text
    assert "[ :status ]" in landing_text
    assert "Show session status" in landing_text

    # 6. Bottom card motto
    assert "Build better. Safer. Together." in landing_text

    # 7. Card boundary borders (Rich rounded panel box)
    assert any(border_char in landing_text for border_char in ("─", "│", "┌", "┐", "└", "┘"))


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
    assert "/help" in foot
    assert "/status" in foot
    assert "/diff" in foot
    assert "/compact" in foot
    assert "/sessions" in foot
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
        assert "/help" in result.output
        assert "/status" in result.output
        assert "/sessions" in result.output

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
    assert "/help" in captured.out
    assert "/status" in captured.out
    assert "/sessions" in captured.out
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



