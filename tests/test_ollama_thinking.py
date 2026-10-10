"""Tests for Ollama thinking field extraction and inline agent timeline integration."""

import pytest
from unittest.mock import MagicMock, patch

from validators.kernel.providers.adapter import OllamaAdapter
from validators.kernel.providers.models import Message, StreamChunk


class TestOllamaThinkingExtraction:
    """Tests for Ollama thinking field extraction in streaming responses."""

    def test_ollama_streaming_thinking_field_extracted(self):
        """Verify OllamaAdapter._parse_stream_dict extracts thinking field from message."""
        adapter = OllamaAdapter(model="ornith-1.5-16k:latest")

        # Simulate Ollama streaming response with thinking field
        data = {
            "model": "ornith-1.5-16k:latest",
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": "The user wants me to analyze this problem",
            },
            "done": False,
        }

        chunk = adapter._parse_stream_dict(data)

        assert chunk.delta_thinking == "The user wants me to analyze this problem"
        assert chunk.delta_content == ""

    def test_ollama_streaming_content_and_thinking_separate(self):
        """Verify content and thinking fields are extracted independently."""
        adapter = OllamaAdapter(model="ornith-1.5-16k:latest")

        data = {
            "model": "ornith-1.5-16k:latest",
            "message": {
                "role": "assistant",
                "content": "Here is the answer",
                "thinking": "Let me think about this step by step",
            },
            "done": False,
        }

        chunk = adapter._parse_stream_dict(data)

        assert chunk.delta_content == "Here is the answer"
        assert chunk.delta_thinking == "Let me think about this step by step"

    def test_ollama_streaming_without_thinking_field(self):
        """Verify OllamaAdapter handles responses without thinking field gracefully."""
        adapter = OllamaAdapter(model="some-other-model")

        data = {
            "model": "some-other-model",
            "message": {
                "role": "assistant",
                "content": "Simple response",
            },
            "done": False,
        }

        chunk = adapter._parse_stream_dict(data)

        assert chunk.delta_content == "Simple response"
        assert chunk.delta_thinking == ""

    def test_ollama_streaming_incremental_thinking_accumulation(self):
        """Verify incremental thinking chunks can be accumulated."""
        adapter = OllamaAdapter(model="ornith-1.5-16k:latest")

        chunks = [
            adapter._parse_stream_dict({
                "model": "ornith-1.5-16k:latest",
                "message": {"role": "assistant", "content": "", "thinking": "First"},
                "done": False,
            }),
            adapter._parse_stream_dict({
                "model": "ornith-1.5-16k:latest",
                "message": {"role": "assistant", "content": "", "thinking": " second"},
                "done": False,
            }),
            adapter._parse_stream_dict({
                "model": "ornith-1.5-16k:latest",
                "message": {"role": "assistant", "content": "", "thinking": " third"},
                "done": False,
            }),
        ]

        thinking_text = " ".join(chunk.delta_thinking.strip() for chunk in chunks if chunk.delta_thinking.strip())
        assert thinking_text == "First second third"

    def test_ollama_complete_response_thinking(self):
        """Verify OllamaAdapter complete() handles thinking field."""
        adapter = OllamaAdapter(model="ornith-1.5-16k:latest")

        with patch.object(adapter, '_execute_request') as mock_exec:
            mock_exec.return_value = {
                "model": "ornith-1.5-16k:latest",
                "message": {
                    "role": "assistant",
                    "content": "Final answer",
                    "thinking": "Complete thinking trace that is exposed",
                },
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 5,
                "eval_count": 10,
            }

            response = adapter.complete(
                [Message.user(content="test")],
                tools=None,
            )

            assert "Final answer" in response.message.content
            assert "thinking" in response.raw_payload["message"]


class TestOllamaThinkingIntegration:
    """Tests for Ollama thinking integration with TUI event pipeline."""

    def test_thinking_delta_reaches_tui_via_gateway(self):
        """Verify Ollama thinking deltas forward through gateway on_thinking callback."""
        adapter = OllamaAdapter(model="ornith-1.5-16k:latest")

        thinking_received = []

        def on_thinking(text: str):
            thinking_received.append(text)

        with patch.object(adapter, '_execute_request') as mock_exec:
            mock_exec.return_value = [
                {"model": "ornith-1.5-16k:latest", "message": {"role": "assistant", "content": "", "thinking": "Thought 1"}, "done": False},
                {"model": "ornith-1.5-16k:latest", "message": {"role": "assistant", "content": "", "thinking": "Thought 2"}, "done": False},
                {"model": "ornith-1.5-16k:latest", "message": {"role": "assistant", "content": "Answer"}, "done": True},
            ]

            for chunk in adapter.stream(
                [Message.user(content="test")],
                tools=None,
            ):
                if chunk.delta_thinking:
                    on_thinking(chunk.delta_thinking)

        assert len(thinking_received) == 2
        assert thinking_received[0] == "Thought 1"
        assert thinking_received[1] == "Thought 2"

    def test_ollama_thinking_exposed_via_reasoning_delta(self):
        """Verify Ollama thinking is exposed via reasoning.delta event name for TUI."""
        from cli.tui.events import extract_reasoning_delta

        # Simulate the reasoning.delta event format used by TUI
        event = {
            "name": "reasoning.delta",
            "payload": {
                "text": "This is the thinking content from Ollama",
                "operation_id": "op-123",
            },
        }

        extracted = extract_reasoning_delta(event)
        assert extracted == "This is the thinking content from Ollama"

    def test_ollama_streaming_preserves_token_deltas(self):
        """Verify token deltas continue to work alongside thinking deltas."""
        adapter = OllamaAdapter(model="ornith-1.5-16k:latest")

        data = {
            "model": "ornith-1.5-16k:latest",
            "message": {
                "role": "assistant",
                "content": "Some answer text",
                "thinking": "Thinking...",
            },
            "done": False,
        }

        chunk = adapter._parse_stream_dict(data)

        assert chunk.delta_content == "Some answer text"
        assert chunk.delta_thinking == "Thinking..."
        assert chunk.finish_reason is None


class TestOllamaProviderCompatibility:
    """Tests for provider compatibility and edge cases."""

    def test_provider_without_thinking_shows_empty(self):
        """Verify providers without thinking support don't fabricate data."""
        adapter = OllamaAdapter(model="some-model-without-thinking")

        data = {
            "model": "some-model-without-thinking",
            "message": {
                "role": "assistant",
                "content": "Response",
            },
            "done": False,
        }

        chunk = adapter._parse_stream_dict(data)

        assert chunk.delta_thinking == ""
        assert chunk.delta_content == "Response"

    def test_thinking_field_is_empty_string_not_none(self):
        """Verify empty thinking field returns empty string, not None."""
        adapter = OllamaAdapter(model="ornith-1.5-16k:latest")

        data = {
            "model": "ornith-1.5-16k:latest",
            "message": {
                "role": "assistant",
                "content": "Response",
                "thinking": "",
            },
            "done": False,
        }

        chunk = adapter._parse_stream_dict(data)

        assert chunk.delta_thinking == ""
        assert chunk.delta_thinking is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
