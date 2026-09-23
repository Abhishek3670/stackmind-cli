from cli.tui.app import dispatch_delivery_command, render_top_header_bar_str
from cli.tui.state import AutonomousDeliveryState


def test_token_estimation_and_turn_usage_tracking() -> None:
    state = AutonomousDeliveryState(context_window_tokens=1_000)

    assert state.estimate_tokens("abcd") == 1
    assert state.estimate_tokens("abcde") == 2

    state.record_token_usage(prompt="abcdefgh", completion="abcdefghijkl")

    assert state.prompt_tokens == 2
    assert state.completion_tokens == 3
    assert state.cumulative_turn_tokens == 5


def test_context_warning_and_header_meter() -> None:
    state = AutonomousDeliveryState(context_window_tokens=1_000)
    state.add_message("user", "x" * 3_000)

    assert state.context_warning_level == "yellow"
    meter = state.context_meter_text
    assert "Context: 0.8K / 1.0K (75%)" == meter

    header = render_top_header_bar_str(
        {"session_id": "session-123", "agent": "codex", "provider": "ollama"},
        width=120,
        context_meter=(meter, state.context_warning_level),
    )
    assert meter in header


def test_automatic_and_manual_transcript_compaction() -> None:
    state = AutonomousDeliveryState(
        context_window_tokens=500,
        compaction_threshold=0.85,
        compaction_keep_recent=3,
    )
    for index in range(5):
        state.add_message("user", f"milestone {index}: " + "x" * 380)

    assert state.compaction_count == 1
    assert state.messages[0].role == "system"
    assert "Transcript compacted" in state.messages[0].content
    assert len(state.messages) == 4

    state.add_message("assistant", "recent answer")
    assert state.compact_transcript(keep_recent=2) is True
    assert len(state.messages) == 3
    assert state.messages[-1].content == "recent answer"


def test_compact_command_reports_when_no_history_to_prune(capsys) -> None:
    state = AutonomousDeliveryState()

    _, should_exit = dispatch_delivery_command(None, None, {"session_id": "s-1"}, ":compact", state)

    assert should_exit is False
    assert "already within" in capsys.readouterr().out
