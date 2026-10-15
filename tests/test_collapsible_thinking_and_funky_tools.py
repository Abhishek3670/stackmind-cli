"""Tests for Collapsible Thinking Block (Ctrl+O) and Funky Tool Naming."""

import pytest
from cli.tui.chat import (
    render_thinking_block,
    render_thinking_block_str,
    render_assistant_message_str,
)
from cli.tui.events import format_action_description, format_tool_name
from cli.tui.keyboard import Key, RawLineEditor
from cli.tui.state import ActionsGroup, AutonomousDeliveryState, ChatMessage, TurnAction


def test_render_thinking_block_collapsed():
    """Verify collapsed thinking block renders ▸ Thought for {elapsed}s, {tokens} tokens and 2-line summary."""
    thinking = "Analyzing user intent\nLooking up relevant files\nPlanning response"
    rendered = render_thinking_block_str(thinking, expanded=False, elapsed=0.2, tokens=15, width=80)
    assert "▸ Thought for 0.2s, 15 tokens" in rendered
    assert "  Planning response" in rendered
    # In collapsed mode, previous thinking lines are not printed
    assert "Analyzing user intent" not in rendered


def test_render_thinking_block_expanded():
    """Verify expanded thinking block renders ▾ Thought for ... with indented lines without heavy box borders."""
    thinking = "Analyzing user intent\nLooking up relevant files\nPlanning response"
    rendered = render_thinking_block_str(thinking, expanded=True, elapsed=0.5, tokens=20, width=80)
    assert "▾ Thought for 0.5s, 20 tokens" in rendered
    assert "  Analyzing user intent" in rendered
    assert "  Looking up relevant files" in rendered
    assert "  Planning response" in rendered
    # No heavy box border
    assert "│" not in rendered


def test_render_thinking_block_in_progress():
    """Verify live in-progress thinking block shows ▸ or ▾ Thought for label."""
    thinking = "Evaluating dependencies..."
    rendered = render_thinking_block_str(thinking, expanded=True, in_progress=True, elapsed=0.1, tokens=3, width=80)
    assert "▾ Thought for 0.1s, 3 tokens" in rendered
    assert "  Evaluating dependencies..." in rendered


def test_assistant_message_reflects_thinking_expansion():
    """Verify assistant message renders thinking block according to thinking_expanded."""
    thinking = "Step 1: Check git status\nStep 2: Commit"
    content = "Changes committed successfully."

    # Collapsed by default
    collapsed = render_assistant_message_str(content, thinking=thinking, thinking_expanded=False, width=80)
    assert "▸ Thought for" in collapsed
    assert "  Step 2: Commit" in collapsed
    assert "Step 1: Check git status" not in collapsed

    # Expanded
    expanded = render_assistant_message_str(content, thinking=thinking, thinking_expanded=True, width=80)
    assert "▾ Thought for" in expanded
    assert "  Step 1: Check git status" in expanded
    assert "  Step 2: Commit" in expanded


def test_state_toggle_thinking():
    """Verify AutonomousDeliveryState toggles thinking globally and per turn."""
    state = AutonomousDeliveryState()
    assert state.thinking_expanded is True

    msg1 = state.add_message("assistant", "Hello", thinking="Thinking 1")
    msg2 = state.add_message("assistant", "World", thinking="Thinking 2")
    assert msg1.thinking_expanded is True
    assert msg2.thinking_expanded is True

    # Toggle globally to collapsed
    res = state.toggle_thinking()
    assert res is False
    assert state.thinking_expanded is False
    assert state.live_thinking_expanded is False
    assert msg1.thinking_expanded is False
    assert msg2.thinking_expanded is False

    # Toggle globally back to expanded
    res2 = state.toggle_thinking()
    assert res2 is True
    assert state.thinking_expanded is True
    assert state.live_thinking_expanded is True
    assert msg1.thinking_expanded is True
    assert msg2.thinking_expanded is True

    # Collapse explicitly
    state.collapse_thinking()
    assert state.thinking_expanded is False
    assert msg1.thinking_expanded is False

    # Expand explicitly
    state.expand_thinking()
    assert state.thinking_expanded is True
    assert msg1.thinking_expanded is True


def test_keyboard_ctrl_o_invokes_callback():
    """Verify RawLineEditor dispatches Key.CTRL_O to on_ctrl_o callback."""
    called = []

    def on_ctrl_o():
        called.append(True)

    editor = RawLineEditor(on_ctrl_o=on_ctrl_o)
    ret = editor.handle_key(Key.CTRL_O)
    assert ret is False  # Does not submit line
    assert len(called) == 1


def test_colon_command_thinking_dispatch():
    """Verify :thinking command toggles state and responds with system confirmation."""
    from cli.tui.commands import dispatch_delivery_command

    state = AutonomousDeliveryState()
    assert state.thinking_expanded is True

    session = {"session_id": "test-session", "workspace": "."}
    # 1. :thinking toggle (to collapse)
    session, _ = dispatch_delivery_command(None, None, session, ":thinking", state)
    assert state.thinking_expanded is False
    assert "Thinking blocks: COLLAPSED" in state.messages[-1].content

    # 2. :thinking expand
    session, _ = dispatch_delivery_command(None, None, session, ":thinking expand", state)
    assert state.thinking_expanded is True
    assert "Thinking blocks: EXPANDED" in state.messages[-1].content

    # 3. :thinking collapse
    session, _ = dispatch_delivery_command(None, None, session, ":thinking collapse", state)
    assert state.thinking_expanded is False
    assert "Thinking blocks: COLLAPSED" in state.messages[-1].content


def test_funky_tool_naming_and_agent_tools():
    """Verify funky names (Harnessing, Graphing), Agent (<agent_name>), and concise agent tool names."""
    # 1. Harness & Agent naming
    assert format_tool_name("harness.run_once", role="Frontend") == "Agent (Frontend)"
    assert format_tool_name("harness.run_once", role="backend") == "Agent (Backend)"
    assert format_tool_name("harness.run_once", role="qa") == "Agent (QA)"
    assert format_tool_name("harness.run_once") == "Agent"
    assert format_tool_name("run_harness") == "Harnessing"
    assert format_action_description("harness.run_once", role="Frontend") == "Agent (Frontend)"
    assert format_action_description("harness.run_once", target="Frontend") == "Agent (Frontend)"
    assert format_action_description("harness.run_once", target="backend") == "Agent (Backend)"
    assert format_action_description("harness.run_once", role="qa") == "Agent (QA)"
    assert format_action_description("harness.run_once", "") == "Agent"
    assert format_action_description("run_harness", "long_running_task") == "Harnessing"
    assert format_action_description("run_harness") == "Harnessing"

    # 2. Graph
    assert format_tool_name("query_graph") == "Graphing"
    assert format_tool_name("graph_query") == "Graphing"
    assert format_action_description("query_graph", "architecture") == "Graphing (architecture)"
    assert format_action_description("query_graph") == "Graphing"

    # 3. Agent tools: Write, Read, Execute, Test, Edit
    assert format_action_description("write_file", "src/auth.py") == "Write (src/auth.py)"
    assert format_action_description("read_file", "README.md") == "Read (README.md)"
    assert format_action_description("edit_file", "src/api.py") == "Edit (src/api.py)"
    assert format_tool_name("exec_command") == "Execute"
    assert format_action_description("exec_command", "npm test") == "Execute (npm test)"
    assert format_action_description("run_tests", "pytest tests/") == "Test (pytest tests/)"


def test_state_process_event_formats_agent_with_role():
    """Verify state.process_event registers Agent (<role>) for harness.run_once tool call."""
    state = AutonomousDeliveryState()
    state.process_event({
        "name": "event.toolCall",
        "payload": {
            "tool_name": "harness.run_once",
            "role": "Frontend",
            "arguments": {"prompt": "Build login UI"},
        },
    })
    group = state.current_turn_actions
    assert len(group.actions) == 1
    assert group.actions[0].description == "Agent (Frontend)"
    assert group.actions[0].status == "RUNNING"


def test_sync_action_changes_working_to_infusing_placeholder():
    """Verify actions targeting .sync/ produce 'Infusing... [...] (...)', else 'Working...'."""
    from cli.tui.terminal import render_composer_box

    # 1. Non-sync action
    state = AutonomousDeliveryState()
    state.active_tool_name = "write_file"
    state.active_tool_target = "src/main.py"
    # Helper replicating _get_working_placeholder logic
    def check_placeholder(st, act_desc=""):
        targets = [
            getattr(st, "active_tool_target", ""),
            getattr(st, "last_tool_target", ""),
            getattr(st, "active_tool_name", ""),
            act_desc,
        ]
        is_sync = any(t and (".sync" in t or ".sync/" in t or ".sync\\" in t) for t in targets)
        prefix = "Infusing..." if is_sync else "Working..."
        return f"{prefix} [1.2s] (150 tokens)"

    p1 = check_placeholder(state)
    assert p1.startswith("Working...")

    # 2. Sync internal action (e.g. .sync/work-orders/WO-001.yaml)
    state.active_tool_target = ".sync/work-orders/WO-001.yaml"
    p2 = check_placeholder(state)
    assert p2.startswith("Infusing...")
    assert "[1.2s] (150 tokens)" in p2

    # 3. Test render_composer_box picks up Infusing... as panel title
    box = render_composer_box(placeholder=p2, width=80)
    assert "Infusing... [1.2s] (150 tokens)" in str(box.title)


def test_latest_preview_action_prioritizes_agent_tools_over_harness():
    """Verify latest_preview_action selects agent tool instead of internal harness envelope."""
    harness_act = TurnAction(
        action_id="harness-1",
        description="Agent (Frontend)",
        status="COMPLETED",
        tool_name="harness.run_once",
        output="Assistant response text that should not be treated as tool snippet",
    )
    agent_write = TurnAction(
        action_id="write-1",
        description="Write (src/main.py)",
        status="COMPLETED",
        tool_name="write_file",
        target="src/main.py",
        code="def main(): pass",
    )
    group = ActionsGroup(actions=[harness_act, agent_write])
    assert group.latest_preview_action == agent_write


def test_compute_composer_cursor_row_always_above_footer():
    """Verify compute_composer_cursor_row correctly positions cursor on row term_lines - 2 above persistent footer."""
    from cli.tui.layout import compute_composer_cursor_row, compute_viewport_height

    state = AutonomousDeliveryState()

    # 1. Landing screen: footer is persistent -> composer middle line is at term_lines - 2
    assert compute_composer_cursor_row(24, state=state) == 22
    assert compute_composer_cursor_row(30, state=state) == 28
    assert compute_viewport_height(24, state=state) == 20  # 24 - 4 (composer box + footer) = 20

    # 2. Conversation active: messages present -> composer middle line remains at term_lines - 2
    state.messages.append(ChatMessage(role="user", content="hello"))
    assert state.has_conversation is True
    assert compute_composer_cursor_row(24, state=state) == 22
    assert compute_composer_cursor_row(30, state=state) == 28
    assert compute_viewport_height(24, state=state) == 20

    # 3. Settings panel open pushes composer up by SETTINGS_PANEL_HEIGHT (11 lines)
    state.settings_panel_open = True
    assert compute_composer_cursor_row(24, state=state) == 11  # 24 - 2 - 11 = 11


def test_landing_screen_full_frame_composer_and_footer_geometry():
    """Verify landing screen frame renders composer above persistent bottom footer."""
    from cli.tui.layout import render_full_screen_workspace, compute_composer_cursor_row

    state = AutonomousDeliveryState()
    session = {"session_id": "test_sess", "provider": "daemon"}

    frame = render_full_screen_workspace(
        session=session,
        state=state,
        width=80,
        height=24,
        include_composer=True,
    )
    lines = frame.split("\n")
    # Total lines rendered must equal terminal height (24 lines)
    assert len(lines) == 24

    # Top border of composer at row index 20 (1-based row 21)
    assert ("╭" in lines[20] or "┌" in lines[20]) and ("╮" in lines[20] or "┐" in lines[20])
    # Input line of composer at row index 21 (1-based row 22)
    assert "│" in lines[21]
    # Bottom border of composer at row index 22 (1-based row 23)
    assert ("╰" in lines[22] or "└" in lines[22]) and ("╯" in lines[22] or "┘" in lines[22])
    # Persistent bottom footer status bar at row index 23 (1-based row 24)
    assert "session: " in lines[23] or "online" in lines[23]

    # Verify compute_composer_cursor_row matches row index 21 (1-based row 22)
    c_row = compute_composer_cursor_row(24, state=state)
    assert c_row == 22


def test_sanitize_internal_llm_error_prompts_in_tool_output():
    """Verify that CRITICAL: Do NOT retry LLM instructions are stripped and clean error badge rendered."""
    from cli.tui.events import clean_error_message
    from cli.tui.chat import render_actions_group_str

    raw_err = (
        "PermissionError: Operation on 'package.json' was denied by runtime policy/contract: "
        "target is explicitly denied. CRITICAL: Do NOT retry accessing or writing to this file "
        "or any file outside your work order scope. Doing so will result in immediate termination."
    )
    cleaned = clean_error_message(raw_err)
    assert "CRITICAL" not in cleaned
    assert "immediate termination" not in cleaned
    assert cleaned == "Permission denied (outside assigned scope)"

    act = TurnAction(
        action_id="read-pkg",
        description="Read (package.json)",
        status="FAILED",
        tool_name="read_file",
        target="package.json",
        error=raw_err,
    )
    group = ActionsGroup(actions=[act])
    rendered = render_actions_group_str(group, width=80)
    assert "● Read(package.json) — Permission denied (outside assigned scope)" in rendered
    assert "CRITICAL" not in rendered


def test_consecutive_subagent_tool_calls_no_duplicate_headers_and_deduplication():
    """Verify consecutive sub-agent calls don't duplicate headers and identical tool calls collapse to (×N)."""
    from cli.tui.chat import render_actions_group_str

    acts = [
        TurnAction("h1", "Agent (Backend)", status="COMPLETED", tool_name="harness.run_once"),
        TurnAction("r1", "Read (src/backend/index.js)", status="COMPLETED", tool_name="read_file", target="src/backend/index.js"),
        TurnAction("r2", "Read (src/backend/index.js)", status="COMPLETED", tool_name="read_file", target="src/backend/index.js"),
        TurnAction("r3", "Read (src/backend/index.js)", status="COMPLETED", tool_name="read_file", target="src/backend/index.js"),
        TurnAction("h2", "Agent (Backend)", status="COMPLETED", tool_name="harness.run_once"),
        TurnAction("e1", "Edit (src/backend/index.js)", status="COMPLETED", tool_name="edit_file", target="src/backend/index.js"),
    ]
    group = ActionsGroup(actions=acts)
    rendered = render_actions_group_str(group, width=80)

    # Agent(Backend) header should only be rendered ONCE (not twice!)
    assert rendered.count("Agent(Backend)") == 1
    # Consecutive reads collapsed to (×3)
    assert "● Read(src/backend/index.js) (×3)" in rendered
    # Indented sub-agent tool calls
    assert "  ● Edit(src/backend/index.js)" in rendered


def test_thinking_collapsed_two_line_summary():
    """Verify collapsed thoughts render as strictly 2 lines: ▸ Thought for ... + 1-line summary."""
    from cli.tui.chat import render_thinking_block_str

    thinking = "# Step 1: Analyze codebase\nInspecting layout.py and terminal.py.\n# Step 2: Formulate plan\nRefactoring chat rendering to flat stream."
    rendered = render_thinking_block_str(thinking, expanded=False, elapsed=0.3, tokens=18, width=80)
    lines = rendered.splitlines()
    assert len(lines) == 2
    assert lines[0].strip() == "▸ Thought for 0.3s, 18 tokens"
    # Stripped of '#' markdown header
    assert lines[1] == "  Refactoring chat rendering to flat stream."


def test_chat_transcript_turn_header_deduplication():
    """Verify render_chat_transcript does not repeat ✦ StackMind for micro-steps in the same turn."""
    from cli.tui.chat import render_chat_transcript_str

    act1 = TurnAction("1", "Read (file.txt)", status="COMPLETED", tool_name="read_file", target="file.txt")
    g1 = ActionsGroup(actions=[act1])

    act2 = TurnAction("2", "Edit (file.txt)", status="COMPLETED", tool_name="edit_file", target="file.txt")
    g2 = ActionsGroup(actions=[act2])

    messages = [
        ChatMessage(role="user", content="Fix the file"),
        ChatMessage(role="assistant", content="", actions=g1),
        ChatMessage(role="assistant", content="", actions=g2),
        ChatMessage(role="assistant", content="All edits finished!"),
    ]

    rendered = render_chat_transcript_str(messages, width=80)
    # ✦ StackMind must only appear ONCE in this assistant turn
    assert rendered.count("✦ StackMind") == 1
    assert "● Read(file.txt)" in rendered
    assert "● Edit(file.txt)" in rendered
    assert "All edits finished!" in rendered



