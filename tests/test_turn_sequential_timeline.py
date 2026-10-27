"""Tests for chronological turn timeline, compact tool previews, and intermediate chatter folding."""

from cli.tui.chat import format_expanded_tool_output, render_actions_group_str
from cli.tui.commands import _build_in_progress_turn_content
from cli.tui.state import ActionsGroup, AutonomousDeliveryState, TurnAction


def test_read_tool_output_is_compact_when_expanded():
    """Verify Read tool output preview is capped to 6 lines max with line count summary."""
    full_html = "\n".join([f"<div>Line {i}</div>" for i in range(1, 101)])
    action = TurnAction(
        action_id="read-1",
        description="Read (index.html)",
        tool_name="read_file",
        target="index.html",
        output=full_html,
        status="COMPLETED",
    )

    from rich.console import Console
    rendered = format_expanded_tool_output(action)
    assert rendered is not None
    console = Console()
    with console.capture() as cap:
        console.print(rendered)
    console_text = cap.get()
    # Must not contain all 100 lines
    assert "Line 1" in console_text
    assert "Line 6" in console_text
    assert "Line 50" not in console_text
    # Must show total line count hint
    assert "total lines read" in console_text
    assert "100" in console_text


def test_turn_timeline_stream_order_text_above_tool_calls():
    """Verify live in-progress turn renders text before tool calls chronologically above each tool."""
    state = AutonomousDeliveryState()

    # Step 1: Agent thinks
    state.record_turn_thinking("Let's analyze the contact information.")

    # Step 2: Agent outputs commentary before tool 1
    state.record_turn_text("Let me look at the current contact details in the landing page.")

    # Step 3: Tool 1 called and completed
    state.record_turn_action("call-1", "Bash(['grep', 'landing.html'])", tool_name="run_command", status="FAILED", error="Restricted tool")
    state.complete_turn_action("call-1", status="FAILED", error="Restricted tool")

    # Step 4: Agent outputs commentary before tool 2
    state.record_turn_text("Let me re-read the file to locate contact strings before editing.")

    # Step 5: Tool 2 called
    state.record_turn_action("call-2", "Read(landing.html)", tool_name="read_file", target="landing.html", status="RUNNING")

    # Step 6: Active streaming of final answer
    streamed_chunks = ["Done — the landing page is updated."]
    state.record_turn_text("Done — the landing page is updated.")

    content = _build_in_progress_turn_content(
        state,
        model_name="claude-3-5-sonnet",
        streamed_chunks=streamed_chunks,
        latest_act_desc="reading",
        start_time=0,
        max_wait=0,
        layout_width=100,
    )

    # 1. First commentary text appears before Bash tool
    pos_text1 = content.find("Let me look at the current contact details")
    pos_tool1 = content.find("Bash(['grep', 'landing.html'])")
    assert pos_text1 != -1 and pos_tool1 != -1
    assert pos_text1 < pos_tool1, "Text 1 must appear above Tool 1"

    # 2. Second commentary text appears after Tool 1 and before Tool 2
    pos_text2 = content.find("Let me re-read the file to locate contact strings")
    pos_tool2 = content.find("Read(landing.html)")
    assert pos_text2 != -1 and pos_tool2 != -1
    assert pos_tool1 < pos_text2, "Tool 1 must appear before Text 2"
    assert pos_text2 < pos_tool2, "Text 2 must appear above Tool 2"

    # 3. Final response appears at bottom
    pos_final = content.find("Done — the landing page is updated.")
    assert pos_final != -1
    assert pos_tool2 < pos_final, "Final text must appear below Tool 2"


def test_intermediate_chatter_folded_into_thinking_on_completion():
    """Verify intermediate commentary is folded into thinking and removed from final response on turn completion."""
    state = AutonomousDeliveryState()

    state.record_turn_thinking("Initial architectural analysis.")
    state.record_turn_text("Let me inspect index.html first.\n\n")
    state.record_turn_action("call-1", "Read(index.html)", tool_name="read_file", status="COMPLETED")
    state.complete_turn_action("call-1", status="COMPLETED")

    state.record_turn_text("Let me update the styles now.\n\n")
    state.record_turn_action("call-2", "Edit(styles.css)", tool_name="edit_file", status="COMPLETED")
    state.complete_turn_action("call-2", status="COMPLETED")

    # The raw provider completion content containing all intermediate utterances and final response
    full_raw_response = (
        "Let me inspect index.html first.\n\n"
        "Let me update the styles now.\n\n"
        "Done — the styling and branding have been successfully refreshed."
    )

    # Finalize message
    msg = state.add_message("assistant", full_raw_response)

    # Final message content must ONLY contain the clean final response
    assert "Done — the styling and branding have been successfully refreshed." in msg.content
    assert "Let me inspect index.html first." not in msg.content
    assert "Let me update the styles now." not in msg.content

    # The intermediate chatter must be folded into the thinking block
    assert msg.thinking is not None
    assert "Initial architectural analysis." in msg.thinking
    assert "> Let me inspect index.html first." in msg.thinking
    assert "> Let me update the styles now." in msg.thinking


def test_harness_container_does_not_duplicate_subactions():
    """Verify harness.run_once container does not duplicate child tools before and after commentary text."""
    state = AutonomousDeliveryState()

    # Daemon emits harness.run_once container at start of operation
    state.record_turn_action("op-1", "Agent (Architecture)", tool_name="harness.run_once")

    # Agent emits commentary text before invoking tools
    state.record_turn_text("I'll take a look at the workspace to understand what's in the repo.")

    # Agent runs tools under parent op-1
    for i, path in enumerate(["README.md", "PLAN.md", "VERSION", "CHANGELOG.md"], start=1):
        state.record_turn_action(f"read-{i}", f"Read({path})", tool_name="read_file", target=path, parent_id="op-1")
        state.complete_turn_action(f"read-{i}", status="COMPLETED")

    content = _build_in_progress_turn_content(
        state,
        model_name="stackmind-echo-v1",
        streamed_chunks=[],
        latest_act_desc="reading",
        start_time=0,
        max_wait=0,
        layout_width=100,
    )

    # Commentary text must appear before Read(README.md)
    pos_text = content.find("I'll take a look at the workspace")
    pos_read = content.find("Read(README.md)")
    assert pos_text != -1 and pos_read != -1
    assert pos_text < pos_read, "Commentary text must appear above tool calls"

    # Crucial: Each tool action must appear EXACTLY ONCE (count == 1)
    assert content.count("Read(README.md)") == 1
    assert content.count("Read(PLAN.md)") == 1
    assert content.count("Read(VERSION)") == 1
    assert content.count("Read(CHANGELOG.md)") == 1


def test_new_turn_actions_do_not_attach_to_previous_assistant_message():
    """Verify new turn actions are not retroactively attached to the previous completed assistant message."""
    state = AutonomousDeliveryState()

    # Turn 1 completes
    state.add_message("assistant", "Done - I've dispatched the Frontend specialist.")
    prev_msg = state.messages[-1]
    assert prev_msg.actions is None

    # Turn 2 starts with new tool actions
    state.record_turn_action("call-fe-1", "Read(brand-kit.html)", tool_name="read_file", target="brand-kit.html")

    # The previous completed message must still have actions=None (not mutated)
    assert prev_msg.actions is None
    # Current turn actions has the new tool
    assert state.current_turn_actions.count == 1


def test_idle_turn_content_does_not_render_orphaned_stream_header():
    """Verify _build_in_progress_turn_content does not append an orphaned header when idle."""
    state = AutonomousDeliveryState()
    state.add_message("assistant", "Previous completed response.")

    content = _build_in_progress_turn_content(
        state,
        model_name="Agent · daemon",
        streamed_chunks=[],
        latest_act_desc="",
        start_time=0,
        max_wait=0,
        layout_width=100,
    )

    # Must contain only the transcript, not an appended orphaned header
    assert "Previous completed response." in content
    assert "✦ StackMind (Agent · daemon)" not in content
