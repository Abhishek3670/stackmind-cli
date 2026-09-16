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

    # Phase 2: Centered motto present
    assert "Build better. Safer. Together." in rendered

    # Phase 2: Exclude shortcut/help rows and command hints
    assert "Start chatting" not in rendered
    assert "Ctrl+K" not in rendered

    # Phase 2: Metadata section strictly ordered
    assert "Status:" in rendered
    assert "Session:" in rendered
    assert "Project:" in rendered
    assert rendered.index("Status:") < rendered.index("Session:") < rendered.index("Project:")

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


def test_conversation_layout_left_aligned_with_horizontal_padding():
    """Verify conversation uses full available width with small horizontal padding and left alignment (§13)."""
    messages = [
        ChatMessage(role="user", content="Deploy authentication service"),
        ChatMessage(role="assistant", content="Starting authentication deployment."),
    ]
    # Default transcript with padding
    transcript = render_chat_transcript_str(messages, width=80)
    lines = [ln for ln in transcript.splitlines() if ln.strip()]

    # First char should be a padding space (small horizontal padding, does not touch terminal edge)
    for line in lines:
        assert line.startswith(" "), f"Expected line to have horizontal padding: {line!r}"

    # Left alignment: content must not be centered (leading spaces should be minimal padding, e.g. 1-2 chars)
    assert "You" in lines[0]
    assert lines[0].startswith(" │ You") or lines[0].startswith(" You")
    assert not lines[0].startswith("                    ")  # not centered

    # Empty transcript also preserves small horizontal padding
    empty_out = render_chat_transcript_str([], width=80)
    assert empty_out.startswith(" ")
    assert "No messages in conversation yet" in empty_out


def test_user_message_visually_quiet_unboxed_styling():
    """Verify user message styling is visually quiet, unboxed, with no avatar icons (§14)."""
    # 1. Default quiet styling with accent line
    user_out = render_user_message_str("Refactor token management", width=80)
    assert "You" in user_out
    assert "Refactor token management" in user_out
    assert "👤" not in user_out
    assert "┌" not in user_out  # No heavy box card
    assert "└" not in user_out

    # 2. Quiet styling without accent bar
    quiet_out = render_user_message_str("Refactor token management", width=80, accent=False)
    assert "You" in quiet_out
    assert "│" not in quiet_out
    assert "Refactor token management" in quiet_out


def test_assistant_message_identity_and_breathing_room():
    """Verify exact '✦ StackMind' assistant identity (§15) and generous breathing room (§16)."""
    content = "Here is the architectural analysis.\n\nAll security invariants are preserved."
    rendered = render_assistant_message_str(content, width=80)

    # Exact identity label
    assert "✦ StackMind" in rendered

    # Breathing room: blank line between header and markdown body
    lines = rendered.splitlines()
    assert lines[0].strip() == "✦ StackMind"
    assert lines[1].strip() == ""  # blank line breathing room
    assert "Here is the architectural analysis." in lines[2]


def test_suppress_per_message_timestamps_in_chat_transcript():
    """Verify regular chat transcript suppresses per-message timestamps (§17)."""
    messages = [
        ChatMessage(role="user", content="Check health status", timestamp="10:24"),
        ChatMessage(role="assistant", content="All services operational.", timestamp="10:25"),
    ]

    # Normal transcript suppresses per-message timestamps
    transcript = render_chat_transcript_str(messages, width=80)
    assert "10:24" not in transcript
    assert "10:25" not in transcript
    assert "You" in transcript
    assert "✦ StackMind" in transcript

    # Explicit diagnostic request can display timestamps
    diag_transcript = render_chat_transcript_str(messages, width=80, show_timestamps=True)
    assert "10:24" in diag_transcript
    assert "10:25" in diag_transcript


def test_rich_markdown_rendering_features():
    """Verify headings, lists, inline code, and fenced code blocks render rich Markdown (§32)."""
    md_content = """# System Architecture

Here is the plan:
1. Initialize SQLite storage
2. Bind FastAPI router

Key benefits:
- Zero overhead
- Strict type checking

Use `get_db()` helper:
```python
def get_db():
    yield db
```
"""
    rendered = render_assistant_message_str(md_content, width=80)

    # Heading rendered
    assert "System Architecture" in rendered
    # Numbered and bullet list items rendered
    assert "Initialize SQLite storage" in rendered
    assert "Bind FastAPI router" in rendered
    assert "Zero overhead" in rendered
    # Inline code and fenced code rendered
    assert "get_db()" in rendered
    assert "def get_db():" in rendered
    assert "yield db" in rendered


def test_inline_diff_rendering_in_conversation():
    """Verify diffs render inline within the conversation using unified diff viewer (§33)."""
    diff_payload = """--- a/src/auth.py
+++ b/src/auth.py
@@ -1,3 +1,4 @@
 import os
+import secrets
 def generate_token():
"""
    messages = [
        ChatMessage(role="user", content="Show the token diff"),
        ChatMessage(role="diff", content=diff_payload),
        ChatMessage(role="assistant", content="Applied secure secrets token generator."),
    ]

    transcript = render_chat_transcript_str(messages, width=80)
    assert "You" in transcript
    assert "Show the token diff" in transcript
    # Inline diff rendered with unified diff headers and chunks
    assert "src/auth.py" in transcript
    assert "unified diff" in transcript
    assert "+import secrets" in transcript
    # Following assistant response remains inline
    assert "✦ StackMind" in transcript
    assert "Applied secure secrets token generator." in transcript

