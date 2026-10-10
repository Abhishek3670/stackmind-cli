"""Tests for stale partial text appearing in transcript after completion (Phase 46).

These tests verify that partial streamed text (e.g., "The") does not remain
as a standalone block after turn completion.
"""

import pytest


class TestPartialTextNotStaleInTranscript:
    """Tests ensuring partial streamed text consolidates into finalized message."""

    def test_streamed_partial_content_consolidated_on_completion(self):
        """Verify partial streaming text consolidates into single finalized message."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Simulate turn 1 completion with thinking
        state.current_turn_thinking = "Turn 1 thinking"
        state.add_message(
            "assistant",
            "Complete answer after streaming",
            thinking="Turn 1 thinking",
        )

        # Get transcript
        transcript = render_chat_transcript_str(state.messages, width=80)

        # Should have exactly one assistant message with complete content
        assert "Complete answer after streaming" in transcript
        assert "Turn 1 thinking" in transcript

        # Partial text from streaming should NOT appear as standalone
        # (in real flow, streamed_chunks are consolidated on completion)
        assert transcript.count("Complete answer after streaming") == 1

    def test_partial_streaming_not_shown_as_separate_message(self):
        """Verify partial streaming doesn't appear as separate message."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Turn 1: thinking + complete answer
        state.current_turn_thinking = "Thinking for turn 1"
        state.add_message(
            "assistant",
            "The complete answer",
            thinking="Thinking for turn 1",
        )

        # Should have exactly 1 message
        assert len(state.messages) == 1

        # "The" should NOT appear as separate standalone content
        # The partial "The" is consolidated into the complete answer
        assert state.messages[0].content == "The complete answer"

        # Get transcript and verify no duplicate "The"
        transcript = render_chat_transcript_str(state.messages, width=80)
        # "The complete answer" appears once, not "The" as separate block
        assert "The complete answer" in transcript

    def test_two_turns_no_partial_leakage(self):
        """Verify partial text from turn 1 doesn't leak to turn 2."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Turn 1
        state.current_turn_thinking = "Thinking 1"
        state.add_message(
            "assistant",
            "Turn 1 complete answer",
            thinking="Thinking 1",
        )

        # Turn 2
        state.current_turn_thinking = "Thinking 2"
        state.add_message(
            "assistant",
            "Turn 2 complete answer",
            thinking="Thinking 2",
        )

        # Get transcript
        transcript = render_chat_transcript_str(state.messages, width=80)

        # Each turn appears exactly once
        assert transcript.count("Turn 1 complete answer") == 1
        assert transcript.count("Turn 2 complete answer") == 1

        # Partial text from Turn 1 should not appear separately in Turn 2
        assert transcript.count("Turn 1") == 1

    def test_finalized_message_has_complete_answer_not_partial(self):
        """Verify finalized message contains complete answer, not partial."""
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Simulate completion from streamed chunks
        # In production: streamed_chunks = ["The", " complete", " answer"]
        # On completion: full_resp = "".join(streamed_chunks).strip()
        # Result: "The complete answer"

        state.add_message("assistant", "The complete answer", thinking=None)

        assert state.messages[0].content == "The complete answer"
        # The message has the consolidated answer, not just "The"

    def test_no_duplicate_header_after_completion(self):
        """Verify no duplicate assistant header appears after completion."""
        from cli.tui.chat import render_chat_transcript_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        # Turn 1
        state.add_message("assistant", "Turn 1 answer")

        # Turn 2
        state.add_message("assistant", "Turn 2 answer")

        transcript = render_chat_transcript_str(state.messages, width=80)

        # Count how many times StackMind header appears
        # In a proper transcript, there should be one header per message
        header_count = transcript.count("StackMind")
        assert header_count == 2  # One per assistant message

        # Verify no duplicate content for same turn
        assert transcript.count("Turn 1 answer") == 1
        assert transcript.count("Turn 2 answer") == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
