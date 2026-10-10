"""Tests for turn lifecycle: streaming to completion without content mixing (Phase 41)."""

import pytest


class TestTurnLifecycleStreaming:
    """Tests ensuring turn lifecycle from streaming to completion."""

    def test_streaming_then_completion_with_thinking(self):
        """Verify turn with thinking then answer finalizes correctly."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Phase 1: Thinking only
        state.current_turn_thinking = "First thinking text"
        content1 = _build_in_progress_turn_content(
            state, "model", [], "running", 0, 0, 80,
        )
        assert "Thinking..." in content1
        assert "First thinking text" in content1

        # Phase 2: Thinking + Answer streaming
        state.current_turn_thinking = "First thinking text"
        content2 = _build_in_progress_turn_content(
            state, "model", ["Answer chunk 1", " chunk 2"], "running", 0, 0, 80,
        )
        # Thinking should still be visible during streaming
        assert "Thinking..." in content2
        assert "Answer chunk 1" in content2

        # Phase 3: Finalization - add message with thinking
        thinking = state.current_turn_thinking
        state.add_message("assistant", "Full answer", thinking=thinking)

        # Buffer should be cleared
        assert state.current_turn_thinking == ""

        # Message should have thinking
        assert len(state.messages) == 1
        assert state.messages[0].thinking == "First thinking text"
        assert state.messages[0].content == "Full answer"

    def test_consecutive_turns_no_mixing(self):
        """Verify consecutive turns don't mix content."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "Turn 1 thinking"
        state.add_message("assistant", "Turn 1 answer", thinking="Turn 1 thinking")

        # First turn complete - buffer cleared
        assert state.current_turn_thinking == ""

        # Second turn - fresh state
        state.current_turn_thinking = "Turn 2 thinking"

        # Streaming phase
        content = _build_in_progress_turn_content(
            state, "model", ["Turn 2 answer"], "running", 0, 0, 80,
        )
        # Should have Turn 2 thinking, not Turn 1
        assert "Turn 2 thinking" in content
        # First turn message should still be in transcript
        assert len(state.messages) == 1
        assert "Turn 1" in state.messages[0].content

        # Finalize second turn
        state.add_message("assistant", "Turn 2 answer", thinking="Turn 2 thinking")

        assert len(state.messages) == 2
        assert state.messages[0].content == "Turn 1 answer"
        assert state.messages[1].content == "Turn 2 answer"
        assert state.messages[0].thinking == "Turn 1 thinking"
        assert state.messages[1].thinking == "Turn 2 thinking"

    def test_empty_thinking_finalization(self):
        """Verify turn without thinking still finalizes correctly."""
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = ""

        state.add_message("assistant", "Answer")

        assert len(state.messages) == 1
        assert state.messages[0].content == "Answer"
        assert state.messages[0].thinking is None
        assert state.current_turn_thinking == ""

    def test_thinking_buffer_isolated(self):
        """Verify thinking buffer is properly isolated between turns."""
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "First"
        state.add_message("assistant", "First")

        # Buffer should be empty
        assert state.current_turn_thinking == ""

        # Second turn
        state.current_turn_thinking = "Second"

        # Should not contain first turn content
        assert "First" not in state.current_turn_thinking
        assert state.current_turn_thinking == "Second"

        # Finalize
        state.add_message("assistant", "Second")

        assert len(state.messages) == 2


class TestInTransContentNoDuplication:
    """Tests ensuring in-progress content doesn't duplicate."""

    def test_in_progress_shows_thinking_once(self):
        """Verify thinking appears once in in-progress view."""
        from cli.tui.app import _build_in_progress_turn_content
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "Thinking text"

        content = _build_in_progress_turn_content(
            state, "model", ["answer"], "running", 0, 0, 80,
        )

        # Should have exactly one "Thinking..."
        assert content.count("Thinking...") == 1

    def test_transcript_after_completion_has_no_in_progress(self):
        """Verify transcript after completion has no in-progress artifacts."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "Thinking"
        state.add_message("assistant", "Answer", thinking="Thinking")

        # Get transcript
        transcript = render_chat_transcript_str(state.messages, width=80)

        # Should have message with thinking, no in-progress content
        assert "Thinking" in transcript
        assert "Answer" in transcript


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
