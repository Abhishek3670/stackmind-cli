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


def test_operation_token_tracking_and_session_accumulation() -> None:
    state = AutonomousDeliveryState(context_window_tokens=8_192)

    # Record tokens for op-001 (Architecture)
    state.record_operation_tokens("op-001", prompt_tokens=4_000, completion_tokens=1_000)
    assert state.prompt_tokens == 4_000
    assert state.completion_tokens == 1_000
    assert state.cumulative_turn_tokens == 5_000

    # Retry or intermediate update of op-001 does not double-count
    state.record_operation_tokens("op-001", prompt_tokens=4_200, completion_tokens=1_100)
    assert state.prompt_tokens == 4_200
    assert state.completion_tokens == 1_100
    assert state.cumulative_turn_tokens == 5_300

    # Record tokens for op-002 (Backend)
    state.record_operation_tokens("op-002", prompt_tokens=5_000, completion_tokens=2_000)
    assert state.prompt_tokens == 9_200
    assert state.completion_tokens == 3_100
    assert state.cumulative_turn_tokens == 12_300


def test_context_tokens_with_actions_diffs_and_telemetry() -> None:
    from cli.tui.state import ActionsGroup, TurnAction

    state = AutonomousDeliveryState(context_window_tokens=8_192)

    # 1. Action with large code and diff
    actions = ActionsGroup(active=False)
    actions.actions.append(
        TurnAction(
            action_id="act-1",
            description="Write index.html",
            tool_name="write_file",
            target="src/index.html",
            diff="+" + ("div { display: flex; }\n" * 100),  # ~2400 chars = ~600 tokens
            status="COMPLETED",
        )
    )
    state.add_message("assistant", "Modified index.html", actions=actions)

    # Transcript estimate should reflect action diff + content
    assert state.context_tokens > 500

    # 2. When real model prompt telemetry is reported, context_tokens reflects model context load
    state.record_operation_tokens("op-qa", prompt_tokens=6_400, completion_tokens=200)
    assert state.last_prompt_tokens == 6_400
    assert state.context_tokens >= 6_400
    assert "Context: 6.6K / 8.2K (81%)" == state.context_meter_text


def test_update_from_session_recovers_journal_telemetry() -> None:
    state = AutonomousDeliveryState(context_window_tokens=8_192)

    session_data = {
        "session_id": "sess-test",
        "journal": [
            {
                "operation_id": "op-001",
                "result": {
                    "telemetry": {
                        "prompt_tokens": 3_500,
                        "completion_tokens": 500,
                    }
                },
            },
            {
                "operation_id": "op-002",
                "result": {
                    "telemetry": {
                        "prompt_tokens": 4_000,
                        "completion_tokens": 1_200,
                    }
                },
            },
        ],
    }

    state.update_from_session(session_data)
    assert state.prompt_tokens == 7_500
    assert state.completion_tokens == 1_700
    assert state.cumulative_turn_tokens == 9_200


def test_process_event_telemetry_and_token_usage() -> None:
    state = AutonomousDeliveryState(context_window_tokens=8_192)

    # Live token.usage event during turn
    state.process_event({
        "name": "token.usage",
        "sequence": 1,
        "payload": {
            "operation_id": "op-10",
            "prompt_tokens": 2_000,
            "completion_tokens": 300,
            "total_tokens": 2_300,
        },
    })
    assert state.last_prompt_tokens == 2_000
    assert state.cumulative_turn_tokens == 2_300

    # cycle.telemetry event at end of turn
    state.process_event({
        "name": "cycle.telemetry",
        "sequence": 2,
        "payload": {
            "operation_id": "op-10",
            "telemetry": {
                "prompt_tokens": 2_500,
                "completion_tokens": 400,
            },
        },
    })
    assert state.last_prompt_tokens == 2_500
    assert state.cumulative_turn_tokens == 2_900


def test_compaction_does_not_resurrect_outbox_reports(tmp_path) -> None:
    """Verify compaction preserves seen_message_keys and does not re-add old outbox reports."""
    from cli.tui.events import recover_transcript_from_events

    outbox_dir = tmp_path / ".sync" / "outbox" / "claude"
    outbox_dir.mkdir(parents=True)
    report_file = outbox_dir / "harness-turn1.md"
    report_file.write_text(
        "# Harness Report: 123\n\n## Report\nOriginal Greeting: I am your Lead Architect.",
        encoding="utf-8",
    )

    state = AutonomousDeliveryState(session_id="test-s1")
    # Initial recovery loads report
    recovered = recover_transcript_from_events([], state, workspace=tmp_path)
    assert len(recovered) == 1
    assert "Original Greeting" in recovered[0].content
    assert len(state.messages) == 1

    # Add more turns
    for i in range(10):
        state.add_message("user", f"user message {i}")
        state.add_message("assistant", f"assistant message {i}")

    # Compact transcript
    state.compact_transcript(keep_recent=4)
    assert len(state.messages) == 5

    # Subsequent poll must NOT re-add the compacted Greeting report
    recovered2 = recover_transcript_from_events([], state, workspace=tmp_path)
    assert len(recovered2) == 0
    assert len(state.messages) == 5
    assert not any("Original Greeting" in m.content for m in state.messages[1:])


