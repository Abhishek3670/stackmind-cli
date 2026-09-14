"""Unit and integration tests for TUI Phase 2: Branded Landing Block & Chat-First Surface."""

from __future__ import annotations

from pathlib import Path
from click.testing import CliRunner
from rich.console import Console

import cli
from cli.main import cli as main_cli
from cli.tui.chat import (
    render_assistant_message,
    render_assistant_message_str,
    render_chat_transcript,
    render_chat_transcript_str,
    render_user_message,
    render_user_message_str,
)
from cli.tui.landing import (
    LANDING_DIAMOND,
    LANDING_NAME,
    LANDING_PILLARS,
    LANDING_TAGLINE,
    render_landing_block,
    render_landing_block_str,
)
from cli.tui.state import AutonomousDeliveryState, ChatMessage
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter


def _extract_plain(renderable) -> str:
    console = Console(record=True, width=100, force_terminal=False, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_landing_block_dynamic_version_and_pillars():
    """Verify landing block dynamically derives version and displays all brand elements."""
    # Test default dynamic version
    rendered = _extract_plain(render_landing_block())
    assert LANDING_DIAMOND in rendered
    assert LANDING_NAME in rendered
    assert f"v{cli.__version__}" in rendered
    assert LANDING_TAGLINE in rendered
    for pillar in LANDING_PILLARS:
        assert pillar in rendered

    # Test static input hints
    assert "Start chatting" in rendered
    assert "Ctrl+K" in rendered
    assert ":help" in rendered
    assert ":status" in rendered

    # Test version override (proving it's dynamic and never hardcoded)
    custom_rendered = _extract_plain(render_landing_block(version="9.8.7"))
    assert "v9.8.7" in custom_rendered
    assert f"v{cli.__version__}" not in custom_rendered or cli.__version__ == "9.8.7"

    # String helper returns matching text
    str_version = render_landing_block_str()
    assert LANDING_NAME in str_version
    assert f"v{cli.__version__}" in str_version


def test_user_message_accent_rendering():
    """Verify user message renders with '│ You' header and left accent line."""
    user_text = "explain auth.py\nand suggest improvements"
    renderable = render_user_message(user_text)
    plain = _extract_plain(renderable)

    assert "│" in plain
    assert "You" in plain
    assert "explain auth.py" in plain
    assert "and suggest improvements" in plain

    str_out = render_user_message_str(user_text)
    assert "│" in str_out
    assert "You" in str_out
    assert "explain auth.py" in str_out


def test_assistant_message_markdown_rendering():
    """Verify assistant message renders with '✦ StackMind' header and markdown."""
    markdown_content = "Here is the summary:\n\n```python\ndef auth():\n    return True\n```"
    renderable = render_assistant_message(markdown_content)
    plain = _extract_plain(renderable)

    assert "✦ StackMind" in plain
    assert "Here is the summary:" in plain
    assert "def auth():" in plain

    str_out = render_assistant_message_str(markdown_content)
    assert "✦ StackMind" in str_out
    assert "def auth():" in str_out


def test_assistant_message_strips_provider_thinking_tags():
    """Private provider reasoning must not enter the conversational surface."""
    plain = _extract_plain(render_assistant_message("<think>private plan</think>\n\n## Final\nSafe answer."))
    assert "private plan" not in plain
    assert "Final" in plain
    assert "Safe answer." in plain


def test_chat_transcript_rendering():
    """Verify full transcript rendering of conversation messages."""
    messages = [
        ChatMessage(role="user", content="How do I configure role backends?"),
        ChatMessage(role="assistant", content="Use `:rebind <role> <backend>` or check `:roles`."),
    ]
    plain = _extract_plain(render_chat_transcript(messages))
    assert "You" in plain
    assert "How do I configure role backends?" in plain
    assert "✦ StackMind" in plain
    assert ":rebind" in plain

    # Empty transcript
    empty_plain = _extract_plain(render_chat_transcript([]))
    assert "No messages in conversation yet" in empty_plain


def test_state_conversation_transition():
    """Verify state tracks conversation presence for landing-to-chat transition."""
    state = AutonomousDeliveryState(project_name="proj", session_id="sess-1")
    assert not state.has_conversation
    assert len(state.messages) == 0

    msg = state.add_message("user", "first question")
    assert state.has_conversation
    assert len(state.messages) == 1
    assert msg.content == "first question"
    assert msg.role == "user"

    state.clear_conversation()
    assert not state.has_conversation
    assert len(state.messages) == 0


def test_tui_repl_landing_and_chat_transition(tmp_path: Path):
    """Verify interactive REPL shows landing on startup and transitions to chat on user prompt."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        user_inputs = "\n".join([
            "explain this codebase",
            ":chat",
            ":landing",
            ":exit",
        ]) + "\n"

        result = CliRunner().invoke(
            main_cli,
            ["tui", "--daemon-url", daemon.url, "--workspace", str(tmp_path)],
            input=user_inputs,
        )

        assert result.exit_code == 0, result.output
        # 1. Landing block shown on startup
        assert "StackMind" in result.output
        assert f"v{cli.__version__}" in result.output
        assert "PLAN · BUILD · VERIFY · GOVERN" in result.output

        # 2. User message rendered with You header and accent bar
        assert "│ You" in result.output
        assert "explain this codebase" in result.output

        # 3. Assistant response rendered
        assert "✦ StackMind" in result.output
        assert "Thinking" in result.output


def test_tui_treats_slash_prefixed_input_as_prompt(tmp_path: Path):
    """Slash text is a governed prompt, not an unsupported command."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        result = CliRunner().invoke(
            main_cli,
            ["tui", "--daemon-url", daemon.url, "--workspace", str(tmp_path)],
            input="/status\n:exit\n",
        )

    assert result.exit_code == 0, result.output
    assert "/status" in result.output
    assert "Unknown command" not in result.output
