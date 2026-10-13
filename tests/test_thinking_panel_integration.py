"""Integration tests for Thinking Panel Phase 3: TUI integration."""

import pytest
from cli.tui.events import extract_reasoning_delta
from cli.tui.state import AutonomousDeliveryState


class TestThinkingPanelTUIIntegration:
    """Tests for reasoning.delta event flow through the TUI."""

    def test_reasoning_delta_updates_state_buffer(self):
        """Verify reasoning.delta events populate state.current_turn_thinking."""
        state = AutonomousDeliveryState()

        # Simulate daemon.publish("reasoning.delta", ...) flow
        event = {
            "name": "reasoning.delta",
            "payload": {
                "text": "I need to think about this problem",
                "operation_id": "op-123",
            },
        }

        state.process_event(event)

        assert state.current_turn_thinking == "I need to think about this problem"

    def test_reasoning_delta_accumulates_incrementally(self):
        """Verify multiple reasoning deltas accumulate in order."""
        state = AutonomousDeliveryState()

        events = [
            {"name": "reasoning.delta", "payload": {"text": "First thought"}},
            {"name": "reasoning.delta", "payload": {"text": "Second thought"}},
            {"name": "reasoning.delta", "payload": {"text": "Final thought"}},
        ]

        for ev in events:
            state.process_event(ev)

        # Deltas accumulate with space separation (preserves word spacing)
        expected = "First thought Second thought Final thought"
        assert state.current_turn_thinking == expected

    def test_token_delta_independent_of_thinking(self):
        """Verify token.delta does not contaminate thinking buffer."""
        state = AutonomousDeliveryState()

        # Token delta event
        token_event = {
            "name": "token.delta",
            "payload": {"delta": "The answer is 42"},
        }

        state.process_event(token_event)

        # Thinking should remain empty
        assert state.current_turn_thinking == ""

    def test_tool_events_dont_contaminate_thinking(self):
        """Verify tool events don't affect thinking buffer."""
        state = AutonomousDeliveryState()

        tool_event = {
            "name": "event.toolCall",
            "payload": {
                "tool_name": "read_file",
                "arguments": {"path": "test.txt"},
            },
        }

        state.process_event(tool_event)

        assert state.current_turn_thinking == ""

    def test_unsupported_provider_no_thinking(self):
        """Verify unsupported providers don't show thinking."""
        state = AutonomousDeliveryState()

        # Simulate provider that doesn't emit reasoning events
        # Thinking buffer should remain empty
        assert state.current_turn_thinking == ""

    def test_add_message_clears_thinking_on_completion(self):
        """Verify thinking buffer is cleared when assistant message is added."""
        state = AutonomousDeliveryState()

        # Set thinking during turn
        state.current_turn_thinking = "Some thinking content"

        # Add assistant message (this should clear the thinking buffer)
        state.add_message("assistant", "The answer is 42")

        # Thinking should be cleared after message finalization
        assert state.current_turn_thinking == ""

    def test_extraction_function_recognizes_reasoning_delta(self):
        """Verify extract_reasoning_delta only matches reasoning.delta events."""
        # Valid reasoning event
        reasoning_event = {
            "name": "reasoning.delta",
            "payload": {"text": "I think..."},
        }
        assert extract_reasoning_delta(reasoning_event) == "I think..."

        # Other event types ignored
        token_event = {"name": "token.delta", "payload": {"delta": "test"}}
        assert extract_reasoning_delta(token_event) is None

        tool_event = {"name": "event.toolCall", "payload": {}}
        assert extract_reasoning_delta(tool_event) is None

    def test_extraction_handles_malformed_events(self):
        """Verify extract_reasoning_delta handles malformed events safely."""
        # Empty payload
        assert extract_reasoning_delta({"name": "reasoning.delta"}) is None
        assert extract_reasoning_delta({"name": "reasoning.delta", "payload": {}}) is None

        # Non-string text
        assert extract_reasoning_delta({
            "name": "reasoning.delta",
            "payload": {"text": 123},
        }) is None

    def test_extraction_supports_alternative_keys(self):
        """Verify extract_reasoning_delta recognizes alternative field names."""
        # Using 'reasoning' key
        event = {
            "name": "reasoning.delta",
            "payload": {"reasoning": "Alternative format"},
        }
        assert extract_reasoning_delta(event) == "Alternative format"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
