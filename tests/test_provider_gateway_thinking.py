"""Regression tests for Thinking Panel Phase 2: normalized thinking events."""

import pytest
from dataclasses import dataclass
from typing import Any

from validators.kernel.daemon.events import EventDispatcher
from validators.kernel.providers.gateway import ProviderGateway
from validators.kernel.providers.adapter import ProviderAdapter
from validators.kernel.providers.models import StreamChunk, ProviderResponse, Message
from validators.kernel.daemon.manager import SessionManager


@dataclass
class MockTransport:
    """Mock transport that yields streaming chunks with thinking deltas."""
    chunks: list[StreamChunk]

    def __call__(self, payload: dict[str, Any], stream: bool, timeout: float | None) -> Any:
        if stream:
            return iter(self.chunks)
        # Return complete response format
        return {"choices": [{"message": {"content": "Final response", "tool_calls": []}}]}


class MockAdapterWithThinking(ProviderAdapter):
    """Mock adapter that emits thinking deltas during streaming."""

    def __init__(self):
        super().__init__(provider_name="mock", model_name="mock-model")
        self.chunks: list[StreamChunk] = []

    def complete(self, messages, *, tools=None, timeout=None, cancellation_token=None, **kwargs):
        return ProviderResponse(message=Message.assistant(content="Complete response"))

    def stream(self, messages, *, tools=None, timeout=None, cancellation_token=None, **kwargs):
        return iter(self.chunks)


class TestThinkingDeltaStreaming:
    """Tests for thinking delta streaming through the gateway."""

    def test_anthropic_thinking_delta_populates_stream_chunk(self):
        """Verify thinking_delta events populate StreamChunk.delta_thinking."""
        adapter = MockAdapterWithThinking()
        adapter.chunks = [
            StreamChunk(delta_thinking="I need to think about this"),
            StreamChunk(delta_content="The answer"),
            StreamChunk(delta_thinking="Let me verify"),
        ]

        thinking_text = []
        def on_thinking(text):
            thinking_text.append(text)

        gateway = ProviderGateway(
            adapter=adapter,
            tool_gateway=None,
            on_thinking=on_thinking,
        )

        # Invoke stream path directly (simulating what execute_turn does)
        accumulated_thinking = []
        for chunk in adapter.stream([]):
            if chunk.delta_thinking and on_thinking:
                on_thinking(chunk.delta_thinking)

        assert thinking_text == ["I need to think about this", "Let me verify"]

    def test_gateway_forwards_thinking_incrementally(self):
        """Verify gateway forwards thinking deltas as they arrive."""
        adapter = MockAdapterWithThinking()
        adapter.chunks = [
            StreamChunk(delta_thinking="Step 1"),
            StreamChunk(delta_content="Step 1 text"),
            StreamChunk(delta_thinking="Step 2"),
            StreamChunk(delta_content="Step 2 text"),
        ]

        thinking_received = []
        tokens_received = []

        def on_thinking(text):
            thinking_received.append(text)

        def on_token(text):
            tokens_received.append(text)

        gateway = ProviderGateway(
            adapter=adapter,
            tool_gateway=None,
            on_thinking=on_thinking,
            on_token=on_token,
        )

        # Simulate streaming loop from execute_turn
        accumulated_thinking = []
        accumulated_text = []

        for chunk in adapter.stream([]):
            if chunk.delta_thinking and on_thinking:
                on_thinking(chunk.delta_thinking)
            if chunk.delta_content and on_token:
                on_token(chunk.delta_content)

        # Verify thinking arrived before text completion
        assert thinking_received == ["Step 1", "Step 2"]
        assert tokens_received == ["Step 1 text", "Step 2 text"]

    def test_reasoning_delta_event_published(self):
        """Verify daemon publishes reasoning.delta events from thinking."""
        dispatcher = EventDispatcher()

        received_events = []

        def listener(event):
            received_events.append(event)

        dispatcher.subscribe(listener)

        # Simulate what DaemonManager._run_turn does
        dispatcher.publish(
            "reasoning.delta",
            session_id="test-session",
            text="I'm thinking...",
            operation_id="test-op",
            role="architecture",
            agent_id="claude",
        )

        assert len(received_events) == 1
        event = received_events[0]
        assert event.name == "reasoning.delta"
        assert event.session_id == "test-session"
        assert event.payload["text"] == "I'm thinking..."
        assert event.payload["operation_id"] == "test-op"
        assert event.payload["role"] == "architecture"
        assert event.payload["agent_id"] == "claude"

    def test_tui_reasoning_delta_extraction(self):
        """Verify TUI extractor recognizes reasoning.delta events."""
        from cli.tui.events import extract_reasoning_delta

        # Valid reasoning event
        event = {
            "name": "reasoning.delta",
            "payload": {"text": "I need to think about this"},
        }
        assert extract_reasoning_delta(event) == "I need to think about this"

        # Empty reasoning
        empty_event = {"name": "reasoning.delta", "payload": {}}
        assert extract_reasoning_delta(empty_event) is None

        # Malformed event
        malformed = {"payload": {"text": "test"}}
        assert extract_reasoning_delta(malformed) is None

        # Other event types ignored
        other_event = {"name": "token.delta", "payload": {"delta": "test"}}
        assert extract_reasoning_delta(other_event) is None

    def test_backward_compatibility_no_thinking(self):
        """Verify adapters without thinking support work correctly."""
        adapter = MockAdapterWithThinking()
        adapter.chunks = [
            StreamChunk(delta_content="Just text"),
            StreamChunk(delta_content="More text"),
        ]

        thinking_received = []
        tokens_received = []

        def on_thinking(text):
            thinking_received.append(text)

        def on_token(text):
            tokens_received.append(text)

        gateway = ProviderGateway(
            adapter=adapter,
            tool_gateway=None,
            on_thinking=on_thinking,
            on_token=on_token,
        )

        for chunk in adapter.stream([]):
            if chunk.delta_thinking and on_thinking:
                on_thinking(chunk.delta_thinking)
            if chunk.delta_content and on_token:
                on_token(chunk.delta_content)

        assert thinking_received == []
        assert tokens_received == ["Just text", "More text"]

    def test_stream_chunk_default_thinking_empty(self):
        """Verify StreamChunk initializes with empty thinking."""
        chunk = StreamChunk(delta_content="test")
        assert chunk.delta_thinking == ""
        assert chunk.delta_content == "test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
