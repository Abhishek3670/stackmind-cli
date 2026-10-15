"""Unit tests for interactive chat UX, tool steps with new design, live code writing, and thinking."""

import pytest
from cli.tui.app import _build_in_progress_turn_content
from cli.tui.chat import format_contract_payload, render_actions_group_str
from cli.tui.events import (
    extract_assistant_response,
    extract_reasoning_delta,
    format_action_description,
)
from cli.tui.state import ActionsGroup, AutonomousDeliveryState, TurnAction


def test_format_action_description_new_design():
    """Verify tool descriptions format as Action (<target>) per funky naming and meaningful agent tools."""
    assert format_action_description("run_tests", "tests/test_auth.py") == "Test (tests/test_auth.py)"
    assert format_action_description("run_tests", "(tests/test_auth.py)") == "Test (tests/test_auth.py)"
    assert format_action_description("run_tests", "") == "Test"
    assert format_action_description("write_file", "backend/server.py") == "Write (backend/server.py)"
    assert format_action_description("read_file", "package.json") == "Read (package.json)"
    assert format_action_description("edit_file", "src/index.ts") == "Edit (src/index.ts)"
    assert format_action_description("run_harness", "long_running_task") == "Harnessing"
    assert format_action_description("query_graph", "") == "Graphing"
    assert format_action_description("exec_command", "npm run build") == "Execute (npm run build)"


def test_actions_group_rendered_with_new_design():
    """Verify render_actions_group renders raw tool steps with clean (<target>) styling."""
    group = ActionsGroup(
        actions=[
            TurnAction(action_id="1", description="Write (src/main.py)", status="COMPLETED"),
            TurnAction(action_id="2", description="Run tests (tests/test_auth.py)", status="COMPLETED"),
        ],
        expanded=True,
    )
    rendered = render_actions_group_str(group, width=80)
    assert "✓ Run tests (tests/test_auth.py)" in rendered
    assert "✓ Write (src/main.py)" in rendered


def test_in_progress_turn_shows_tool_steps_new_design():
    """Verify in-progress turn renders live tool steps with new design and no chat timer clutter."""
    state = AutonomousDeliveryState()
    state.record_turn_action("call-1", "Write (src/main.py)", status="COMPLETED")
    state.record_turn_action("call-2", "Run tests (tests/test_api.py)", status="RUNNING")

    content = _build_in_progress_turn_content(
        state,
        model_name="gemma4:31b-cloud",
        streamed_chunks=[],
        latest_act_desc="running",
        start_time=0,
        max_wait=0,
        layout_width=80,
    )

    # Tool steps are visible with new design
    assert "Write (src/main.py)" in content
    assert "Run tests (tests/test_api.py)" in content
    # Working... progress timer must NOT be in chat body (it sits at composer text box)
    assert "Working..." not in content


def test_in_progress_live_code_writing_preview():
    """Verify active code writing shows live preview snippet."""
    state = AutonomousDeliveryState()
    state.active_tool_name = "write_file"
    state.active_tool_target = "backend/server.py"
    state.active_tool_code = "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/')\ndef root():\n    return {'status': 'ok'}\n"

    content = _build_in_progress_turn_content(
        state,
        model_name="gemma4:31b-cloud",
        streamed_chunks=[],
        latest_act_desc="writing",
        start_time=0,
        max_wait=0,
        layout_width=80,
    )

    assert "✍ Writing `backend/server.py`..." in content
    assert "│ from fastapi import FastAPI" in content
    assert "│ def root():" in content


def test_in_progress_live_test_running_preview():
    """Verify active test execution shows clean indicator."""
    state = AutonomousDeliveryState()
    state.active_tool_name = "run_tests"
    state.active_tool_target = "tests/test_api.py"

    content = _build_in_progress_turn_content(
        state,
        model_name="gemma4:31b-cloud",
        streamed_chunks=[],
        latest_act_desc="testing",
        start_time=0,
        max_wait=0,
        layout_width=80,
    )

    assert "⚡ Running tests: `tests/test_api.py`" in content


def test_in_progress_thinking_and_answer_coexistence():
    """Verify LLM thinking is displayed and coexists with answer streaming."""
    state = AutonomousDeliveryState()
    state.current_turn_thinking = "Let's first inspect the requirements and design the handler."

    content = _build_in_progress_turn_content(
        state,
        model_name="gemma4:31b-cloud",
        streamed_chunks=["Here is the solution."],
        latest_act_desc="streaming",
        start_time=0,
        max_wait=0,
        layout_width=80,
    )

    assert "Thinking..." in content
    assert "Let's first inspect the requirements" in content
    assert "Here is the solution." in content


def test_filter_sync_files_from_contract_payload():
    """Verify .sync/ internal runtime files are omitted from user-facing turn summaries."""
    payload = {
        "status": "completed",
        "modified_files": [".sync/run.log", ".sync/metadata.json", "frontend/App.tsx"],
    }
    import json
    text = f"```json\n{json.dumps(payload)}\n```"
    formatted = format_contract_payload(text)
    assert "`frontend/App.tsx`" in formatted
    assert ".sync" not in formatted


def test_extract_reasoning_delta_provider_formats():
    """Verify extract_reasoning_delta extracts reasoning from diverse provider event payloads."""
    ev1 = {"name": "reasoning.delta", "payload": {"thinking": "analyzing query"}}
    assert extract_reasoning_delta(ev1) == "analyzing query"

    ev2 = {"name": "agent.thinking", "payload": {"text": "planning step 2"}}
    assert extract_reasoning_delta(ev2) == "planning step 2"

    ev3 = {"name": "turn.thinking", "payload": {"reasoning": "verifying edge cases"}}
    assert extract_reasoning_delta(ev3) == "verifying edge cases"


def test_tool_preview_persists_after_tool_result():
    """Verify preview does not disappear when toolResult arrives; it transitions to ✓ Wrote."""
    state = AutonomousDeliveryState()
    # 1. Tool call starts
    state.process_event({
        "name": "event.toolCall",
        "payload": {
            "tool_name": "write_file",
            "arguments": {"path": ".sync/contracts/WO-004.yaml", "content": "schema_version: 1\nagent_id: gemma\n"},
            "call_id": "call-1",
        },
    })
    assert state.active_tool_name == "write_file"
    assert state.active_tool_target == ".sync/contracts/WO-004.yaml"

    # 2. Tool result arrives (disk write completed)
    state.process_event({
        "name": "event.toolResult",
        "payload": {
            "tool_name": "write_file",
            "call_id": "call-1",
            "status": "COMPLETED",
        },
    })
    # Active is reset, but last_tool_* preserves the context!
    assert state.active_tool_name is None
    assert state.last_tool_name == "write_file"
    assert state.last_tool_target == ".sync/contracts/WO-004.yaml"
    assert "schema_version: 1" in state.last_tool_code

    # In-progress rendering still shows the snippet preview as ✓ Wrote, DOES NOT disappear!
    content = _build_in_progress_turn_content(
        state,
        model_name="gemma4:31b-cloud",
        streamed_chunks=[],
        latest_act_desc="waiting",
        start_time=0,
        max_wait=0,
        layout_width=80,
    )
    assert "✓ Wrote `.sync/contracts/WO-004.yaml`" in content
    assert "│ schema_version: 1" in content


def test_completed_turn_actions_remain_expanded_with_code_preview():
    """Verify that completing a turn keeps latest assistant actions expanded with code preview in transcript."""
    state = AutonomousDeliveryState()
    # Turn 1
    state.add_message("user", "Create contract")
    state.process_event({
        "name": "event.toolCall",
        "payload": {
            "tool_name": "write_file",
            "arguments": {"path": ".sync/contracts/WO-004.yaml", "content": "schema_version: 1\nagent_id: gemma\n"},
            "call_id": "call-1",
        },
    })
    state.process_event({
        "name": "event.toolResult",
        "payload": {"tool_name": "write_file", "call_id": "call-1", "status": "COMPLETED"},
    })
    state.add_message("assistant", "Completed task via tools")

    # Turn 1 assistant message has preview_code saved
    asst_msg1 = state.messages[-1]
    assert asst_msg1.actions is not None
    assert asst_msg1.actions.preview_code is not None
    assert "schema_version: 1" in asst_msg1.actions.preview_code

    # Expand actions to view in transcript
    asst_msg1.actions.expand()
    from cli.tui.chat import render_assistant_message_str
    rendered = render_assistant_message_str(asst_msg1.content, actions=asst_msg1.actions, width=80)
    assert "▾ Actions · 1 completed" in rendered
    assert "✓ Write (.sync/contracts/WO-004.yaml)" in rendered
    assert "✓ Wrote `.sync/contracts/WO-004.yaml`" in rendered
    assert "│ schema_version: 1" in rendered

    # Collapse
    asst_msg1.actions.collapse()
    assert asst_msg1.actions.expanded is False


def test_edit_action_renders_diff_preview():
    """Verify edit action extracts and renders unified diff with + and - lines."""
    from cli.tui.chat import render_action_preview_str
    from cli.tui.events import extract_tool_diff

    # 1. Diff extraction from edit arguments
    args = {
        "path": "cli/tui/chat.py",
        "target_content": "def old_calc():\n    return 1",
        "replacement_content": "def new_calc():\n    return 2",
    }
    diff = extract_tool_diff({}, args=args)
    assert diff is not None
    assert "- def old_calc():" in diff
    assert "+ def new_calc():" in diff

    # 2. Rendering in TurnAction preview
    action = TurnAction(
        action_id="act-edit",
        description="Edit (cli/tui/chat.py)",
        status="COMPLETED",
        tool_name="edit_file",
        target="cli/tui/chat.py",
        diff=diff,
    )
    rendered = render_action_preview_str(action, width=80)
    assert "✓ Edited `cli/tui/chat.py`" in rendered
    assert "- def old_calc():" in rendered
    assert "+ def new_calc():" in rendered


def test_bash_command_action_renders_terminal_output():
    """Verify bash/command action renders terminal output on completion."""
    from cli.tui.chat import render_action_preview_str

    # 1. Running state
    running_act = TurnAction(
        action_id="act-bash",
        description="Run command (npm test)",
        status="RUNNING",
        tool_name="exec_command",
        target="npm test",
    )
    running_render = render_action_preview_str(running_act, width=80)
    assert "⚡ Running command: `npm test`..." in running_render

    # 2. Completed state with stdout
    completed_act = TurnAction(
        action_id="act-bash",
        description="Run command (npm test)",
        status="COMPLETED",
        tool_name="exec_command",
        target="npm test",
        output="Test Suites: 5 passed, 5 total\nTests: 24 passed, 24 total\nSnapshots: 0 total\nTime: 1.234 s",
    )
    completed_render = render_action_preview_str(completed_act, width=80)
    assert "✓ Ran `npm test`" in completed_render
    assert "│ Test Suites: 5 passed" in completed_render
    assert "│ Tests: 24 passed" in completed_render

    # 3. Generation action (harness / no command target)
    harness_act = TurnAction(
        action_id="act-gen",
        description="Run harness",
        status="COMPLETED",
        tool_name="harness.run_once",
        output="Hello! I'm Claude, ready to assist.",
    )
    harness_render = render_action_preview_str(harness_act, width=80)
    assert "✓ Generating" in harness_render
    assert "│ Hello! I'm Claude" in harness_render

    harness_running = TurnAction(
        action_id="act-gen-run",
        description="Run harness",
        status="RUNNING",
        tool_name="harness.run_once",
    )
    harness_running_render = render_action_preview_str(harness_running, width=80)
    assert "⚡ Generating..." in harness_running_render


def test_actions_group_latest_preview_action_precedence():
    """Verify latest_preview_action prioritizes RUNNING over completed, and completed with diff/output over blank."""
    act_read = TurnAction(action_id="1", description="Read (README.md)", status="COMPLETED", tool_name="read_file", target="README.md")
    act_edit = TurnAction(action_id="2", description="Edit (src/app.py)", status="COMPLETED", tool_name="edit_file", target="src/app.py", diff="+ added")
    act_bash = TurnAction(action_id="3", description="Run command (pytest)", status="COMPLETED", tool_name="exec_command", target="pytest", output="5 passed")

    group = ActionsGroup(actions=[act_read, act_edit, act_bash])
    # Most recent completed with output/diff is act_bash
    assert group.latest_preview_action == act_bash

    # If act_edit was after act_bash, it would pick act_edit
    group2 = ActionsGroup(actions=[act_read, act_bash, act_edit])
    assert group2.latest_preview_action == act_edit

    # If a new action starts running, RUNNING takes immediate precedence!
    act_live = TurnAction(action_id="4", description="Writing (src/new.py)", status="RUNNING", tool_name="write_file", target="src/new.py")
    group.actions.append(act_live)
    assert group.latest_preview_action == act_live


def test_preview_persists_even_with_streamed_chunks():
    """Verify actions disclosure and completed preview remain visible during answer streaming."""
    state = AutonomousDeliveryState()
    state.record_turn_action(
        "act-1",
        "Edit (cli/tui/chat.py)",
        status="COMPLETED",
        tool_name="edit_file",
        target="cli/tui/chat.py",
        diff="--- a/cli/tui/chat.py\n+++ b/cli/tui/chat.py\n@@ -1,1 +1,1 @@\n- old\n+ new",
    )
    assert not state.current_turn_actions.is_empty
    assert state.current_turn_actions.latest_preview_action is not None

    # When chunks are actively streaming, the action and its diff preview remain visible!
    content = _build_in_progress_turn_content(
        state,
        model_name="gemma4:31b-cloud",
        streamed_chunks=["Here are the updates I made to `cli/tui/chat.py`."],
        latest_act_desc="streaming",
        start_time=0,
        max_wait=0,
        layout_width=80,
    )

    assert "Edit (cli/tui/chat.py)" in content
    assert "✓ Edited `cli/tui/chat.py`" in content
    assert "+ new" in content
    assert "Here are the updates I made" in content


def test_actions_group_verbosity_tree_flat_compact():
    """Verify ActionsGroup.get_visible_actions produces tree, flat, and compact structures."""
    parent = TurnAction(action_id="act-harness", description="Run harness", tool_name="run_harness", status="COMPLETED")
    c1 = TurnAction(action_id="c1", description="Read (src/lib.py)", tool_name="read_file", parent_id="act-harness", status="COMPLETED")
    c2 = TurnAction(action_id="c2", description="Edit (src/lib.py)", tool_name="edit_file", parent_id="act-harness", status="COMPLETED")
    standalone = TurnAction(action_id="s1", description="Run tests (tests/t.py)", tool_name="run_tests", status="COMPLETED")
    parent.sub_actions = [c1, c2]

    group = ActionsGroup(actions=[parent, standalone], verbosity="tree")

    # Tree mode: parent at indent 0, sub_actions at indent 1, standalone at indent 0
    tree_items = group.get_visible_actions("tree")
    assert tree_items == [(parent, 0), (c1, 1), (c2, 1), (standalone, 0)]
    assert group.count == 4

    # Flat mode: parent suppressed, sub_actions flattened to indent 0, standalone at indent 0
    flat_items = group.get_visible_actions("flat")
    assert flat_items == [(c1, 0), (c2, 0), (standalone, 0)]
    group.verbosity = "flat"
    assert group.count == 3

    # Compact mode: parent at indent 0, sub_actions hidden, standalone at indent 0
    compact_items = group.get_visible_actions("compact")
    assert compact_items == [(parent, 0), (standalone, 0)]
    group.verbosity = "compact"
    assert group.count == 2


def test_render_actions_group_tree_indentation_and_modes():
    """Verify render_actions_group_str renders proper indentation in tree mode and handles flat/compact."""
    parent = TurnAction(action_id="harness-1", description="Run harness", tool_name="run_harness", status="COMPLETED")
    c1 = TurnAction(action_id="c1", description="Read (config.py)", tool_name="read_file", parent_id="harness-1", status="COMPLETED")
    c2 = TurnAction(action_id="c2", description="Edit (config.py)", tool_name="edit_file", parent_id="harness-1", status="COMPLETED")
    parent.sub_actions = [c1, c2]

    group = ActionsGroup(actions=[parent], expanded=True, verbosity="tree")

    # 1. Tree mode rendering
    rendered_tree = render_actions_group_str(group, width=80)
    assert "✓ Run harness" in rendered_tree
    # Sub-actions should be indented with 4 spaces
    assert "    ✓ Read (config.py)" in rendered_tree
    assert "    ✓ Edit (config.py)" in rendered_tree

    # 2. Flat mode rendering
    group.verbosity = "flat"
    rendered_flat = render_actions_group_str(group, width=80)
    # Parent harness banner is suppressed in flat mode
    assert "Run harness" not in rendered_flat
    # Individual actions are surfaced directly at indent 2
    assert "  ✓ Read (config.py)" in rendered_flat
    assert "  ✓ Edit (config.py)" in rendered_flat

    # 3. Compact mode rendering
    group.verbosity = "compact"
    rendered_compact = render_actions_group_str(group, width=80)
    # Only parent is shown
    assert "  ✓ Run harness" in rendered_compact
    assert "Read (config.py)" not in rendered_compact
    assert "Edit (config.py)" not in rendered_compact


def test_govern_colon_command_dispatch_and_settings_persistence(tmp_path):
    """Verify :govern command changes runtime verbosity and persists to settings.json."""
    from cli.tui.app import dispatch_delivery_command
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState()
    session = {"workspace": str(tmp_path), "session_id": "test-session-1"}

    # 1. Switch to flat mode
    dispatch_delivery_command(None, None, session, ":govern flat", state)
    assert state.govern_verbosity == "flat"
    saved = load_settings(tmp_path)
    assert saved.govern_verbosity == "flat"
    assert any("Governance verbosity set to 'flat'" in m.content for m in state.messages)

    # 2. Switch to compact mode
    dispatch_delivery_command(None, None, session, ":govern compact", state)
    assert state.govern_verbosity == "compact"
    saved = load_settings(tmp_path)
    assert saved.govern_verbosity == "compact"

    # 3. Switch back to tree mode
    dispatch_delivery_command(None, None, session, ":govern tree", state)
    assert state.govern_verbosity == "tree"
    saved = load_settings(tmp_path)
    assert saved.govern_verbosity == "tree"

    # 4. Bare :govern displays current verbosity and usage
    dispatch_delivery_command(None, None, session, ":govern", state)
    assert any("Current governance verbosity: tree" in m.content for m in state.messages)

    # 5. Invalid verbosity rejected gracefully
    dispatch_delivery_command(None, None, session, ":govern invalid_mode", state)
    assert state.govern_verbosity == "tree"
    assert any("Unknown governance verbosity 'invalid_mode'" in m.content for m in state.messages)


def test_failed_subaction_causes_expansion_and_badge_count():
    """Verify that a failure within sub_actions keeps disclosure expanded and reflects in failure badge."""
    state = AutonomousDeliveryState()
    # Start harness
    state.record_turn_action("op-1", "Run harness", status="RUNNING", tool_name="run_harness")
    # Sub-action under harness
    state.record_turn_action("call-1", "Read (app.py)", status="COMPLETED", tool_name="read_file", parent_id="op-1")
    # Failing sub-action
    state.record_turn_action("call-2", "Edit (app.py)", status="RUNNING", tool_name="edit_file", parent_id="op-1")
    group = state.current_turn_actions
    assert group is not None

    # Complete failing sub-action
    group.complete_action("call-2", status="FAILED", error="File is read-only")
    assert group.expanded is True

    # Check header text reflects failure
    header_str = group.format_header_text()
    assert "1 failed" in header_str

    # Rendered string reflects failed badge and error
    rendered = render_actions_group_str(group, width=80)
    assert "1 failed" in rendered
    assert "× Edit (app.py)" in rendered
    assert "File is read-only" in rendered


def test_settings_panel_supports_govern_verbosity(tmp_path):
    """Verify settings overview and handle_system_settings_command support govern verbosity."""
    from cli.tui.settings_panel import handle_system_settings_command, render_settings_overview_str
    from validators.kernel.settings import load_settings

    settings = load_settings(tmp_path)
    settings.govern_verbosity = "flat"
    overview = render_settings_overview_str(settings, width=80)
    assert "Governance Verbosity: [FLAT]" in overview

    # Direct subcommand via handle_system_settings_command
    res = handle_system_settings_command(tmp_path, initial_subcmd="govern compact")
    assert "[SUCCESS] Governance verbosity set to COMPACT" in res
    saved = load_settings(tmp_path)
    assert saved.govern_verbosity == "compact"

