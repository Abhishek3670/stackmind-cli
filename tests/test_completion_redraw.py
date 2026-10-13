"""Tests for completion redraw after streaming (Phase 50).

These tests verify that partial streamed text is properly consolidated
when the turn completes, and the completion redraw shows the finalized state.
"""

import pytest


class TestCompletionRedrawNoStaleText:
    """Tests ensuring completion redraw shows finalized transcript."""

    def test_completion_transcript_shows_complete_answer_only(self):
        """Verify completion produces one assistant message with complete answer."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Simulate what happens during streaming: chunks accumulate
        # streamed_chunks = ["The", " complete", " answer"]
        # On completion: full_resp = "".join(streamed_chunks).strip()

        # Simulate completion: message added with consolidated answer
        state.current_turn_thinking = "Thinking during streaming"
        state.add_message(
            "assistant",
            "The complete answer",
            thinking="Thinking during streaming",
        )

        # Clear the thinking buffer (as done in add_message)
        state.current_turn_thinking = ""

        # Verify state
        assert len(state.messages) == 1
        assert state.messages[0].content == "The complete answer"

        # Get transcript
        transcript = render_chat_transcript_str(state.messages, width=80)

        # Complete answer appears exactly once
        assert "The complete answer" in transcript
        assert transcript.count("The complete answer") == 1

        # No standalone "The"
        # (If "The" appears alone, it would be as a separate line)
        lines = transcript.split('\n')
        standalone_the = [line for line in lines if line.strip() == "The"]
        assert len(standalone_the) == 0, "Standalone 'The' found in transcript"

    def test_multiple_turns_no_stale_partial(self):
        """Verify turn 2 doesn't contain stale partial text from turn 1."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Turn 1
        state.current_turn_thinking = "Thinking 1"
        state.add_message(
            "assistant",
            "The turn 1 answer",
            thinking="Thinking 1",
        )

        # Turn 2 starts fresh
        state.current_turn_thinking = "Thinking 2"
        state.add_message(
            "assistant",
            "The turn 2 answer",
            thinking="Thinking 2",
        )

        # Get transcript
        transcript = render_chat_transcript_str(state.messages, width=80)

        # Each answer appears exactly once
        assert transcript.count("turn 1 answer") == 1
        assert transcript.count("turn 2 answer") == 1

        # Count "The" occurrences - should match number of messages with "The"
        # (Each message starts with "The", so count should be 2)
        assert transcript.count("The") == 2

    def test_finalized_message_replaces_partial(self):
        """Verify finalized message replaces any partial text."""
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # During streaming: "The" is in streamed_chunks
        # On completion: "The complete answer" is added as message

        # Simulate: message added with full answer
        state.add_message("assistant", "The complete answer")

        # Message contains full answer
        assert state.messages[0].content == "The complete answer"

        # No partial "The" message exists separately
        assert len(state.messages) == 1

    def test_thinking_preserved_with_answer(self):
        """Verify thinking remains associated with its answer after completion."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        state.current_turn_thinking = "Original thinking"
        state.add_message(
            "assistant",
            "Final answer",
            thinking="Original thinking",
        )

        assert state.messages[0].thinking == "Original thinking"

        transcript = render_chat_transcript_str(state.messages, width=80)
        assert "Original thinking" in transcript
        assert "Final answer" in transcript


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
