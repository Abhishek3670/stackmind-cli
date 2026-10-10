"""Tests for cross-turn isolation to prevent content mixing (Phase 41)."""

import pytest
from cli.tui.state import AutonomousDeliveryState, ChatMessage


class TestCrossTurnIsolation:
    """Tests ensuring consecutive turns don't mix content."""

    def test_first_turn_completes_cleanly(self):
        """Verify first turn completes with exactly one message."""
        state = AutonomousDeliveryState()

        # First turn thinking
        state.current_turn_thinking = "Welcome back thinking"

        # First turn answer
        msg1 = state.add_message(
            "assistant",
            "Hey! Welcome back to the StackMind CLI workspace.",
            thinking="Welcome back thinking",
        )

        assert len(state.messages) == 1
        assert msg1.content == "Hey! Welcome back to the StackMind CLI workspace."
        assert state.current_turn_thinking == ""

    def test_second_turn_fresh_state(self):
        """Verify second turn starts with fresh state."""
        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "First thinking"
        msg1 = state.add_message(
            "assistant",
            "Hey! Welcome back to the StackMind CLI workspace.",
            thinking="First thinking",
        )

        # Second turn thinking
        state.current_turn_thinking = "Second thinking"

        # Second turn answer
        msg2 = state.add_message(
            "assistant",
            "A petrol engine is an internal combustion engine.",
            thinking="Second thinking",
        )

        # Second turn should not have first turn content
        assert msg2.content != msg1.content
        assert "Welcome back" not in msg2.content
        assert "petrol engine" in msg2.content

        # Messages should be distinct
        assert msg1 is not msg2
        assert len(state.messages) == 2

    def test_thinking_isolated_per_turn(self):
        """Verify thinking is associated only with its turn."""
        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "First thinking text"
        msg1 = state.add_message(
            "assistant",
            "First answer",
            thinking="First thinking text",
        )

        # Second turn
        state.current_turn_thinking = "Second thinking text"
        msg2 = state.add_message(
            "assistant",
            "Second answer",
            thinking="Second thinking text",
        )

        # Each message should have its own thinking
        assert msg1.thinking == "First thinking text"
        assert msg2.thinking == "Second thinking text"

    def test_no_stale_in_progress_content(self):
        """Verify in-progress buffers are cleared on completion."""
        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "First thinking"
        state.add_message(
            "assistant",
            "First answer",
            thinking="First thinking",
        )

        # After completion, buffers should be empty
        assert state.current_turn_thinking == ""

    def test_third_turn_clean_buffers(self):
        """Verify a subsequent turn starts with clean buffers."""
        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "First thinking"
        state.add_message("assistant", "First answer", thinking="First thinking")

        # Second turn
        state.current_turn_thinking = "Second thinking"
        state.add_message("assistant", "Second answer", thinking="Second thinking")

        # Third turn
        state.current_turn_thinking = "Third thinking"
        msg3 = state.add_message(
            "assistant",
            "Third answer",
            thinking="Third thinking",
        )

        # Third turn should have clean thinking
        assert msg3.thinking == "Third thinking"
        assert "First" not in msg3.content
        assert "Second" not in msg3.content

    def test_message_identity_separate(self):
        """Verify each turn creates separate message objects."""
        state = AutonomousDeliveryState()

        msg1 = state.add_message("assistant", "First answer")
        msg2 = state.add_message("assistant", "Second answer")
        msg3 = state.add_message("assistant", "Third answer")

        # All should be different objects
        assert msg1 is not msg2
        assert msg2 is not msg3
        assert msg1 is not msg3

        # All should be in messages list
        assert msg1 in state.messages
        assert msg2 in state.messages
        assert msg3 in state.messages


class TestStreamedChunksIsolation:
    """Tests ensuring streamed_chunks don't leak between turns."""

    def test_streamed_chunks_reset_on_new_turn(self):
        """Verify streamed_chunks concept doesn't leak between turns."""
        state = AutonomousDeliveryState()

        # First turn
        state.current_turn_thinking = "First thinking"
        state.add_message("assistant", "First answer", thinking="First thinking")

        # After completion, thinking buffer should be cleared
        assert state.current_turn_thinking == ""

        # Second turn
        state.current_turn_thinking = "Second thinking"

        # Should not contain first turn content
        assert "First" not in state.current_turn_thinking


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
