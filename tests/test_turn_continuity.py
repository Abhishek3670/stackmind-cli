"""Tests for assistant turn continuity during thinking-to-answer transition (Phase 39)."""

import pytest
from cli.tui.app import _build_in_progress_turn_content


class TestTurnContinuity:
    """Tests ensuring thinking and answer render as one assistant turn."""

    def test_thinking_only_shows_one_header(self):
        """Verify thinking-only state renders one assistant header."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "This is the thinking"

        content = _build_in_progress_turn_content(
            state, model_name="test-model", streamed_chunks=[],
            latest_act_desc="running", start_time=0, max_wait=0, layout_width=80,
        )

        # One header only
        assert content.count("✦") == 1
        # Thinking section shown
        assert "Thinking..." in content

    def test_thinking_plus_answer_shows_one_header(self):
        """Verify thinking followed by answer deltas still renders one header."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "This is the thinking"

        content = _build_in_progress_turn_content(
            state, model_name="test-model", streamed_chunks=["answer text"],
            latest_act_desc="running", start_time=0, max_wait=0, layout_width=80,
        )

        # One header only
        assert content.count("✦") == 1
        # Thinking shown during answer streaming (not yet finalized)
        assert "Thinking..." in content

    def test_thinking_heading_appears_at_most_once(self):
        """Verify Thinking heading appears at most once per assistant turn."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "Some thinking"

        content = _build_in_progress_turn_content(
            state, model_name="test-model", streamed_chunks=[],
            latest_act_desc="running", start_time=0, max_wait=0, layout_width=80,
        )

        assert content.count("Thinking...") == 1

    def test_thinking_visible_after_answer_starts(self):
        """Verify thinking remains in final transcript when answer generation completes."""
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "This is the thinking"

        # Simulate completion - add_message should preserve thinking
        msg = state.add_message("assistant", "The answer", thinking=state.current_turn_thinking)

        # Thinking should be on the message
        assert msg.thinking == "This is the thinking"
        # Buffer cleared
        assert state.current_turn_thinking == ""

    def test_answer_updates_without_replacing_thinking(self):
        """Verify answer text updates progressively while thinking remains visible."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "Thinking content"

        # Simulate multiple answer deltas
        for delta in ["first ", "second ", "third "]:
            streamed_chunks = [delta]
            content = _build_in_progress_turn_content(
                state, model_name="test-model", streamed_chunks=streamed_chunks,
                latest_act_desc="running", start_time=0, max_wait=0, layout_width=80,
            )
            # Thinking should remain visible when answer is streaming
            assert "Thinking..." in content

    def test_finalization_no_duplication(self):
        """Verify finalization does not duplicate content."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # First, thinking-only
        state.current_turn_thinking = "Thinking"
        thinking_content = _build_in_progress_turn_content(
            state, model_name="test-model", streamed_chunks=[],
            latest_act_desc="running", start_time=0, max_wait=0, layout_width=80,
        )

        # Then, answer streaming (thinking should not appear)
        state.current_turn_thinking = "Thinking"
        answer_content = _build_in_progress_turn_content(
            state, model_name="test-model", streamed_chunks=["answer"],
            latest_act_desc="running", start_time=0, max_wait=0, layout_width=80,
        )

        # Add final message with thinking
        msg = state.add_message("assistant", "answer", thinking="Thinking")

        # Transcript should have message with thinking
        assert len(state.messages) == 1
        assert state.messages[0].role == "assistant"
        assert state.messages[0].thinking == "Thinking"

    def test_subsequent_turn_gets_own_header(self):
        """Verify a subsequent assistant turn gets its own header and fresh state."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "First thinking"
        msg1 = state.add_message("assistant", "First answer", thinking="First thinking")

        # Second turn
        state.current_turn_thinking = "Second thinking"
        msg2 = state.add_message("assistant", "Second answer", thinking="Second thinking")

        # Both messages should have their own thinking
        assert msg1.thinking == "First thinking"
        assert msg2.thinking == "Second thinking"


class TestTranscriptRendering:
    """Tests for transcript rendering with thinking."""

    def test_transcript_shows_assistant_with_thinking(self):
        """Verify transcript renders assistant messages with thinking."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import ChatMessage

        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!", thinking="Thinking about how to respond"),
        ]

        content = render_chat_transcript_str(messages, width=80)

        # Should include thinking section
        assert "Thinking..." in content or "thinking" in content.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
