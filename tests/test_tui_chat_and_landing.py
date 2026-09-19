"""Unit and integration tests for TUI Phase 2: Branded Landing Block & Chat-First Surface."""

from __future__ import annotations

from pathlib import Path
from click.testing import CliRunner
from rich.console import Console

import cli
from cli.main import cli as main_cli
from cli.tui.app import dispatch_delivery_command
from cli.tui.chat import (
    extract_internal_reasoning,
    is_stray_command_bar,
    render_actions_group,
    render_actions_group_str,
    render_assistant_message,
    render_assistant_message_str,
    render_chat_transcript,
    render_chat_transcript_str,
    render_system_message,
    render_system_message_str,
    render_user_message,
    render_user_message_str,
)
from cli.tui.events import extract_assistant_thinking
from cli.tui.landing import (
    LANDING_DIAMOND,
    LANDING_NAME,
    LANDING_PILLARS,
    LANDING_TAGLINE,
    render_landing_block,
    render_landing_block_str,
)
from cli.tui.state import (
    ActionsGroup,
    AutonomousDeliveryState,
    ChatMessage,
    TurnAction,
)
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


def test_actions_group_collapsed_default():
    """Verify actions disclosure group is collapsed by default per §20 and §22."""
    actions = [
        TurnAction("1", "Read src/auth.py", status="COMPLETED"),
        TurnAction("2", "Read tests/test_auth.py", status="COMPLETED"),
        TurnAction("3", "Run harness", status="COMPLETED"),
        TurnAction("4", "Verify changes", status="COMPLETED"),
    ]
    group = ActionsGroup(actions=actions)
    assert not group.expanded  # Collapsed by default

    plain = _extract_plain(render_actions_group(group))
    assert "▸ Actions · 4 completed" in plain
    assert "Read src/auth.py" not in plain  # Details hidden while collapsed

    # Render within assistant message
    asst_rendered = _extract_plain(render_assistant_message("Task finished.", actions=group))
    assert "✦ StackMind" in asst_rendered
    assert "▸ Actions · 4 completed" in asst_rendered
    assert "Task finished." in asst_rendered
    assert "Read src/auth.py" not in asst_rendered


def test_actions_group_expand_collapse_keyboard():
    """Verify actions disclosure group expands/collapses via keyboard command per §20, §22."""
    actions = [
        TurnAction("1", "Read src/auth.py", status="COMPLETED"),
        TurnAction("2", "Read tests/test_auth.py", status="COMPLETED"),
        TurnAction("3", "Run harness", status="COMPLETED"),
        TurnAction("4", "Verify changes", status="COMPLETED"),
    ]
    group = ActionsGroup(actions=actions)
    group.expand()
    assert group.expanded

    plain_expanded = _extract_plain(render_actions_group(group))
    assert "▾ Actions · 4 completed" in plain_expanded
    assert "✓ Read src/auth.py" in plain_expanded
    assert "✓ Read tests/test_auth.py" in plain_expanded
    assert "✓ Run harness" in plain_expanded
    assert "✓ Verify changes" in plain_expanded

    group.collapse()
    assert not group.expanded
    plain_collapsed = _extract_plain(render_actions_group(group))
    assert "▸ Actions · 4 completed" in plain_collapsed
    assert "✓ Read src/auth.py" not in plain_collapsed

    # Keyboard command via state
    state = AutonomousDeliveryState()
    state.add_message("assistant", "Result", actions=group)
    assert not group.expanded

    state.toggle_actions()
    assert group.expanded

    state.toggle_actions()
    assert not group.expanded


def test_actions_group_expand_collapse_mouse():
    """Verify actions disclosure group expands/collapses via mouse click (approved exception per §23)."""
    group = ActionsGroup(actions=[TurnAction("1", "Read src/auth.py", status="COMPLETED")])
    assert not group.expanded

    # Click to expand
    group.handle_click()
    assert group.expanded
    plain_exp = _extract_plain(render_actions_group(group))
    assert "▾ Actions · 1 completed" in plain_exp
    assert "✓ Read src/auth.py" in plain_exp

    # Click to collapse
    group.handle_click()
    assert not group.expanded
    plain_col = _extract_plain(render_actions_group(group))
    assert "▸ Actions · 1 completed" in plain_col

    # Mouse click handling on state level
    state = AutonomousDeliveryState()
    state.add_message("assistant", "Done", actions=group)
    res = state.handle_mouse_click(target="actions")
    assert res is True
    assert group.expanded
    res = state.handle_mouse_click(target="actions")
    assert res is False
    assert not group.expanded


def test_actions_group_one_invocation_one_entry_and_inplace_update():
    """Verify running->completed updates in-place using event IDs without duplicate cards (§21)."""
    group = ActionsGroup(active=True)

    # 1. Start invocation
    group.add_or_update("call-1", "Run harness", status="RUNNING")
    assert len(group.actions) == 1
    assert group.actions[0].status == "RUNNING"
    assert group.actions[0].symbol == "●"

    # Active header shows count without completed
    plain_running = _extract_plain(render_actions_group(group))
    assert "▸ Actions · 1" in plain_running
    assert "completed" not in plain_running

    group.expand()
    plain_running_exp = _extract_plain(render_actions_group(group))
    assert "● Run harness" in plain_running_exp

    # 2. Complete the same invocation
    group.add_or_update("call-1", status="COMPLETED")
    assert len(group.actions) == 1  # No duplicate card!
    assert group.actions[0].status == "COMPLETED"
    assert group.actions[0].symbol == "✓"

    group.active = False
    plain_comp_exp = _extract_plain(render_actions_group(group))
    assert "▾ Actions · 1 completed" in plain_comp_exp
    assert "✓ Run harness" in plain_comp_exp
    assert "● Run harness" not in plain_comp_exp

    # 3. Add distinct invocation
    group.add_or_update("call-2", "Verify changes", status="COMPLETED")
    assert len(group.actions) == 2
    assert group.actions[1].description == "Verify changes"


def test_actions_group_failed_and_cancelled_states():
    """Verify actions symbols across completed (✓), running (●), failed (×), and cancelled (⊘)."""
    actions = [
        TurnAction("1", "Read src/auth.py", status="COMPLETED"),
        TurnAction("2", "Run harness", status="RUNNING"),
        TurnAction("3", "Run tests", status="FAILED", error="1 test failed"),
        TurnAction("4", "Deploy changes", status="CANCELLED"),
    ]
    group = ActionsGroup(actions=actions, expanded=True)
    plain = _extract_plain(render_actions_group(group))

    assert "✓ Read src/auth.py" in plain
    assert "● Run harness" in plain
    assert "× Run tests (1 test failed)" in plain
    assert "⊘ Deploy changes" in plain


def test_actions_group_turn_expansion_preservation():
    """Verify current-turn state preserved and completed turns remain collapsed unless expanded (§22)."""
    turn1_actions = ActionsGroup(actions=[TurnAction("1", "Read file", status="COMPLETED")])
    turn2_actions = ActionsGroup(actions=[TurnAction("2", "Edit file", status="COMPLETED")])

    # Turn 1 is historical and remains collapsed
    assert not turn1_actions.expanded

    # Turn 2 (current turn) is expanded by user
    turn2_actions.expand()
    assert turn2_actions.expanded
    assert not turn1_actions.expanded  # Turn 1 stays collapsed

    messages = [
        ChatMessage(role="user", content="Turn 1"),
        ChatMessage(role="assistant", content="Turn 1 done", actions=turn1_actions),
        ChatMessage(role="user", content="Turn 2"),
        ChatMessage(role="assistant", content="Turn 2 done", actions=turn2_actions),
    ]

    transcript = render_chat_transcript_str(messages, width=80)
    # Turn 1 rendered collapsed
    assert "▸ Actions · 1 completed" in transcript
    # Turn 2 rendered expanded
    assert "▾ Actions · 1 completed" in transcript
    assert "✓ Edit file" in transcript


def test_tui_repl_actions_command_and_mouse_click():
    """Verify interactive REPL handles :actions keyboard command and mouse click exception (§22, §23)."""
    state = AutonomousDeliveryState()
    actions = [
        TurnAction("1", "Read src/auth.py", status="COMPLETED"),
        TurnAction("2", "Run harness", status="COMPLETED"),
    ]
    group = ActionsGroup(actions=actions)
    state.add_message("assistant", "Changes verified.", actions=group)
    assert not group.expanded

    # Keyboard command: toggle/expand
    session = {"session_id": "s1"}
    dispatch_delivery_command(None, None, session, ":actions expand", state)
    assert group.expanded

    # Keyboard command: collapse
    dispatch_delivery_command(None, None, session, ":actions collapse", state)
    assert not group.expanded

    # Keyboard command: bare :actions toggles
    dispatch_delivery_command(None, None, session, ":actions", state)
    assert group.expanded

    # Mouse click exception: :click actions
    dispatch_delivery_command(None, None, session, ":click actions", state)
    assert not group.expanded

    # Mouse escape sequence: \x1b[<0;20;10M
    dispatch_delivery_command(None, None, session, "\x1b[<0;20;10M", state)
    assert group.expanded


def test_assistant_message_renders_genuine_thinking_when_present():
    """If upstream provider exposes genuine thinking, render 'Thinking...' followed by reasoning text (§24)."""
    reasoning = "Inspecting the existing authentication middleware...\nVerifying token expiration handling..."
    msg = "The authentication flow has been updated."
    rendered = _extract_plain(render_assistant_message(msg, thinking=reasoning))

    # Header
    assert "✦ StackMind" in rendered
    # Region 2: Thinking
    assert "Thinking..." in rendered
    assert "Inspecting the existing authentication middleware..." in rendered
    assert "Verifying token expiration handling..." in rendered
    # Region 3: Response
    assert "The authentication flow has been updated." in rendered
    # Order: Header -> Thinking -> Content
    think_idx = rendered.index("Thinking...")
    content_idx = rendered.index("The authentication flow has been updated.")
    assert think_idx < content_idx


def test_assistant_message_clean_3_region_turn_structure():
    """Assistant message cleanly separates Region 1 (Actions), Region 2 (Thinking), Region 3 (Content) per §18, §26, §27."""
    actions = ActionsGroup(actions=[
        TurnAction("1", "Read middleware.py", status="COMPLETED"),
        TurnAction("2", "Edit middleware.py", status="COMPLETED"),
    ])
    reasoning = "Found missing bearer prefix verification in middleware."
    content = "Patched token parsing in `middleware.py`."

    rendered = _extract_plain(render_assistant_message(content, actions=actions, thinking=reasoning))

    assert "▸ Actions · 2 completed" in rendered
    assert "Thinking..." in rendered
    assert "Found missing bearer prefix verification" in rendered
    assert "Patched token parsing in middleware.py." in rendered

    actions_idx = rendered.index("Actions · 2 completed")
    think_idx = rendered.index("Thinking...")
    content_idx = rendered.index("Patched token parsing")
    assert actions_idx < think_idx < content_idx


def test_assistant_message_does_not_fabricate_thinking_when_absent():
    """If no upstream thinking capability exists, do NOT fabricate 'Thinking...' text (§24)."""
    msg = "Pure assistant response with no upstream thinking."
    rendered = _extract_plain(render_assistant_message(msg, thinking=None))

    assert "✦ StackMind" in rendered
    assert "Pure assistant response with no upstream thinking." in rendered
    assert "Thinking..." not in rendered
    assert "Thinking" not in rendered


def test_assistant_message_completed_thinking_indicator():
    """Completed thinking indicator 'Thinking... ✓' renders when thinking is '✓' (§27)."""
    rendered = _extract_plain(render_assistant_message("Task complete.", thinking="✓"))
    assert "Thinking... ✓" in rendered
    assert "Task complete." in rendered


def test_extract_internal_reasoning():
    """extract_internal_reasoning isolates text inside <think> tags."""
    content = "<think>line 1\nline 2</think>\n\nFinal answer."
    extracted = extract_internal_reasoning(content)
    assert extracted == "line 1\nline 2"

    assert extract_internal_reasoning("No thinking tags here.") is None
    assert extract_internal_reasoning("") is None


def test_extract_assistant_thinking_from_structured_payloads(tmp_path: Path):
    """extract_assistant_thinking retrieves genuine thinking from events, payloads, and reports."""
    # 1. Direct thinking keys in payload
    assert extract_assistant_thinking({"thinking": "thought A"}) == "thought A"
    assert extract_assistant_thinking({"thought": "thought B"}) == "thought B"
    assert extract_assistant_thinking({"reasoning": "thought C"}) == "thought C"
    assert extract_assistant_thinking({"reasoning_content": "thought D"}) == "thought D"

    # 2. Inside result dict
    assert extract_assistant_thinking({"result": {"thinking": "res thought"}}) == "res thought"
    assert extract_assistant_thinking({"result": {"thought": "res thought 2"}}) == "res thought 2"
    assert extract_assistant_thinking({"result": {"reasoning_content": "res thought 3"}}) == "res thought 3"

    # 3. Inside report file with ## Thinking
    report_file = tmp_path / "harness-report.md"
    report_file.write_text(
        "# Harness Report\n\n"
        "## Thinking\n"
        "Identified edge case in session recovery.\n\n"
        "## Summary\n"
        "Session recovery fixed.\n\n"
        "## Report\n"
        "Session recovery fixed successfully.\n",
        encoding="utf-8",
    )
    payload_with_report = {"result": {"report_path": str(report_file)}}
    assert extract_assistant_thinking(payload_with_report, workspace=tmp_path) == "Identified edge case in session recovery."

    # 4. From delimited tags inside response text
    payload_with_tags = {"response": "<think>Delimited reason</think>Clean text"}
    assert extract_assistant_thinking(payload_with_tags) == "Delimited reason"

    # 5. Payload without thinking returns None (never fabricates fake reasoning)
    assert extract_assistant_thinking({"response": "Clean text only"}) is None
    assert extract_assistant_thinking({}) is None


def test_assistant_message_left_aligned_model_attribution():
    """Verify assistant message header left-aligns model attribution inline (WO-054)."""
    # 1. Direct model parameter
    out_direct = render_assistant_message_str("Response text.", model="qwen2.5-coder:7b", width=80)
    lines_direct = out_direct.splitlines()
    assert "✦ StackMind (qwen2.5-coder:7b)" in lines_direct[0]
    assert lines_direct[0].startswith("✦ StackMind (qwen2.5-coder:7b)")

    # 2. Auto-extraction from heading / leading text
    content_with_hdr = "# Response from qwen2.5-coder:7b\n\nDirect response body without centering."
    out_extracted = render_assistant_message_str(content_with_hdr, width=80)
    lines_extracted = out_extracted.splitlines()
    # Left-aligned header contains model
    assert "✦ StackMind (qwen2.5-coder:7b)" in lines_extracted[0]
    # Centered markdown heading removed from body
    assert "# Response from" not in out_extracted
    assert "Direct response body without centering." in out_extracted


def test_operational_telemetry_dimming_in_chat():
    """Verify operational telemetry lines are styled with dim secondary coloring (WO-054)."""
    content = "Final response content.\n\nKnowledge revision: 4"
    rendered = _extract_plain(render_assistant_message(content))
    assert "Final response content." in rendered
    assert "Knowledge revision: 4" in rendered

    # System message rendering
    sys_out = render_system_message_str("● Thinking... Turn submitted\nKnowledge revision: 12\nBackground sync done.")
    assert "✦ System" in sys_out
    assert "● Thinking... Turn submitted" in sys_out
    assert "Knowledge revision: 12" in sys_out


def test_stray_command_bar_filtering_in_chat_transcript():
    """Verify stray command footer / shortcut remnants are filtered out of conversational stream (WO-054)."""
    # Detection helper
    assert is_stray_command_bar(":help   :status   :diff   :events   :roles   :landing   StackMind v3.3.0")
    assert is_stray_command_bar("Ctrl+K commands | Ctrl+L clear")
    assert not is_stray_command_bar("Can you explain the diff in auth.py?")

    # Filtering in transcript
    messages = [
        ChatMessage(role="user", content="Show me the changes"),
        ChatMessage(role="assistant", content=":help   :status   :diff   :events   :roles   :landing   StackMind v3.3.0"),
        ChatMessage(role="assistant", content="Here are the actual changes."),
    ]
    transcript = render_chat_transcript_str(messages, width=80)
    assert "Show me the changes" in transcript
    assert "Here are the actual changes." in transcript
    assert ":help   :status   :diff" not in transcript


def test_landing_block_metadata_deduplication_and_tight_padding():
    """Verify landing block de-duplicates full UUIDs to short IDs and tightens vertical dead space (WO-054)."""
    session = {
        "session_id": "b49209fd-1234-5678-abcd-9876543210ef",
        "project": "~/projects/stackmind",
        "status": "online",
    }
    rendered = render_landing_block_str(session=session, width=80)
    # Full UUID should be abbreviated to short 8-char id
    assert "b49209fd" in rendered
    assert "b49209fd-1234-5678-abcd-9876543210ef" not in rendered

    # Verify no dead space (tight vertical layout)
    lines = [line.strip() for line in rendered.splitlines() if line.strip()]
    assert any("✦" in line for line in lines)
    assert any("PLAN · BUILD · VERIFY · GOVERN" in line for line in lines)
    assert any("Status:" in line for line in lines)



