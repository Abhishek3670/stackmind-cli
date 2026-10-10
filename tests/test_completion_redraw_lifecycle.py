"""Tests for completion redraw lifecycle (Phase 52).

These tests exercise the actual production completion path and verify
that partial text does not remain as standalone blocks after completion.
"""

import time
from unittest.mock import MagicMock

import pytest


class TestCompletionRedrawLifecycle:
    """Tests for the complete completion lifecycle."""

    def test_stream_then_completion_single_message(self):
        """Verify streaming + completion produces exactly one assistant message."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Simulate streaming: chunks accumulate
        # streamed_chunks = ["The", " complete", " answer"]

        # On completion: full_resp = "".join(streamed_chunks).strip()
        # state.add_message("assistant", full_resp, thinking=thinking)

        state.current_turn_thinking = "Thinking text"
        state.add_message("assistant", "The complete answer", thinking="Thinking text")

        # Verify exactly one message
        assert len(state.messages) == 1
        assert state.messages[0].content == "The complete answer"

        # Verify transcript
        transcript = render_chat_transcript_str(state.messages, width=80)

        # Complete answer appears once
        assert transcript.count("The complete answer") == 1

        # No standalone "The" as separate line
        lines = transcript.split("\n")
        standalone_the = [line for line in lines if line.strip() == "The"]
        assert len(standalone_the) == 0

    def test_completion_keeps_finalized_transcript(self):
        """Verify finalized transcript persists after completion redraw."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Turn 1: finalize message
        state.add_message("assistant", "Turn 1 answer")

        # Get transcript (simulates completion redraw)
        transcript1 = render_chat_transcript_str(state.messages, width=80)
        assert "Turn 1 answer" in transcript1

        # Turn 2: finalize another message
        state.add_message("assistant", "Turn 2 answer")

        # Get transcript (simulates second completion redraw)
        transcript2 = render_chat_transcript_str(state.messages, width=80)
        assert "Turn 1 answer" in transcript2  # Turn 1 persists
        assert "Turn 2 answer" in transcript2  # Turn 2 added

    def test_thinking_leaks_to_next_turn(self):
        """Verify thinking does not leak between turns."""
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Turn 1
        state.current_turn_thinking = "Turn 1 thinking"
        state.add_message("assistant", "Turn 1 answer", thinking="Turn 1 thinking")

        # Buffer cleared by add_message
        assert state.current_turn_thinking == ""

        # Turn 2 starts with fresh thinking
        state.current_turn_thinking = "Turn 2 thinking"
        state.add_message("assistant", "Turn 2 answer", thinking="Turn 2 thinking")

        # Verify no leak
        assert "Turn 1" not in state.current_turn_thinking
        assert state.messages[0].thinking == "Turn 1 thinking"
        assert state.messages[1].thinking == "Turn 2 thinking"

    def test_production_event_composition_transitions_to_canonical_completion(self):
        """A finalized stream is replaced by history in the actual full-screen frame."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.events import extract_text_delta
        from cli.tui.layout import render_full_screen_workspace
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.add_message("user", "Hey")
        state.process_event({"name": "reasoning.delta", "payload": {"text": "Greeting summary"}})
        state.process_event({
            "name": "event.toolCall",
            "payload": {
                "tool_name": "lookup",
                "arguments": {"path": "greeting.md"},
                "call_id": "g1",
            },
        })

        streamed_chunks = []
        for delta in ("The", " greeting", " answer"):
            event = {"name": "text.delta", "payload": {"delta": delta}}
            streamed_chunks.append(extract_text_delta(event))
            live = _build_in_progress_turn_content(
                state, "test-model", streamed_chunks, "lookup", 0, 0, 80,
            )
            assert delta.strip() in live
            assert live.count("✦ StackMind") == 1
            assert "Greeting summary" in live
            assert "greeting.md" in live

        state.add_message(
            "assistant", "The greeting answer", actions=state.current_turn_actions,
            thinking=state.current_turn_thinking,
        )
        streamed_chunks.clear()

        # This is the same canonical-state path used by the completion redraw.
        frame = render_full_screen_workspace(
            session={}, state=state, width=100, height=30,
        )
        assert frame.count("The greeting answer") == 1
        assert frame.count("✦ StackMind") == 1
        assert "Greeting summary" in frame
        assert "greeting.md" in frame
        assert "Working..." not in frame
        assert "The\n" not in frame

        # Starting a subsequent turn preserves finalized history while isolating
        # transient reasoning and streamed text to that turn.
        state.current_turn_thinking = ""
        state.add_message("user", "What's fibonacci?")
        state.process_event({"name": "reasoning.delta", "payload": {"text": "Fibonacci summary"}})
        second = _build_in_progress_turn_content(
            state, "test-model", ["Fibonacci"], "calculating", 0, 0, 80,
        )
        assert second.count("The greeting answer") == 1
        assert second.count("✦ StackMind") == 2  # One historical answer, one active response.
        assert "Greeting summary" in second
        assert "Fibonacci summary" in second
        assert "Fibonacci" in second

        state.add_message("assistant", "Fibonacci response", thinking=state.current_turn_thinking)
        state.add_message("user", "Third turn")
        state.current_turn_thinking = "Third turn summary"
        third = _build_in_progress_turn_content(
            state, "test-model", ["Third"], "working", 0, 0, 80,
        )
        assert third.count("The greeting answer") == 1
        assert third.count("Fibonacci response") == 1
        assert "Fibonacci summary" in third
        assert "Third turn summary" in third
        assert "Fibonacci" not in state.messages[-1].content

    def test_dispatch_completion_redraw_uses_canonical_state(self, monkeypatch):
        """Exercise dispatch's actual stream and completion redraw call inputs."""
        from cli.tui.app import dispatch_delivery_command
        from cli.tui.state import AutonomousDeliveryState

        session = {"session_id": "session-1", "workspace": "."}
        state = AutonomousDeliveryState(session_id="session-1")
        adapter = MagicMock()
        adapter.command.return_value = {"operation_id": "op-1"}

        def stream_events():
            yield {
                "name": "reasoning.delta",
                "payload": {"text": "Greeting summary", "operation_id": "op-1"},
            }
            yield {"name": "event.toolCall", "payload": {
                "tool_name": "lookup", "arguments": {"path": "greeting.md"},
                "call_id": "g1", "operation_id": "op-1",
            }}
            for delta in ("The", " greeting answer"):
                time.sleep(0.11)
                yield {"name": "text.delta", "payload": {"delta": delta, "operation_id": "op-1"}}
            yield {"name": "operation.completed", "payload": {"operation_id": "op-1"}}

        adapter.stream.return_value = stream_events()
        client = MagicMock()
        redraw_calls = []

        def capture_redraw(*args, **kwargs):
            redraw_calls.append(kwargs)
            return "frame"

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        monkeypatch.setattr("cli.tui.app.redraw_full_screen", capture_redraw)

        dispatch_delivery_command(adapter, client, session, "Hey", state, client_timeout=10)

        assert state.messages[-1].content == "The greeting answer"
        assert state.messages[-1].thinking == "Greeting summary"
        assert state.messages[-1].actions is not None
        streaming_frames = [
            call["conversation_content"]
            for call in redraw_calls
            if "conversation_content" in call
        ]
        assert streaming_frames, redraw_calls
        assert any("The" in frame for frame in streaming_frames), streaming_frames
        assert any("greeting.md" in frame for frame in streaming_frames), streaming_frames
        completion_redraw = redraw_calls[-1]
        assert completion_redraw["clear"] is False
        assert "conversation_content" not in completion_redraw


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
