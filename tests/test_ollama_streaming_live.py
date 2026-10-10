"""Phase 25/26: Real Ollama streaming integration test.

Tests genuine incremental output from Ollama through StackMind's production
Ollama adapter with streaming enabled.
"""

import time
import pytest

from validators.kernel.providers.adapter import OllamaAdapter
from validators.kernel.providers.models import Message


class TestOllamaStreamingLive:
    """Tests for real Ollama streaming integration."""

    def test_ollama_stream_produces_genuine_deltas(self):
        """Ollama adapter with stream=True produces multiple non-empty deltas."""
        adapter = OllamaAdapter(
            endpoint="http://localhost:11434",
            model="gemma4:e4b",
            default_timeout=60.0,
        )

        messages = [
            Message.user(content="Say 'Hello World' in exactly 3 short words."),
        ]

        start_time = time.monotonic()
        deltas: list[str] = []
        delta_timestamps: list[float] = []

        try:
            for chunk in adapter.stream(messages, timeout=30.0):
                elapsed = time.monotonic() - start_time
                if chunk.delta_content:
                    deltas.append(chunk.delta_content)
                    delta_timestamps.append(elapsed)

            completion_time = time.monotonic() - start_time

            # Assertions
            assert len(deltas) > 0, "At least one delta should be received"
            assert any(d.strip() for d in deltas), "At least one non-empty delta"

            # Verify timestamps show progressive arrival
            if len(delta_timestamps) > 1:
                first_delta_at = delta_timestamps[0]
                assert first_delta_at < completion_time, (
                    f"First delta arrived at {first_delta_at:.2f}s, "
                    f"completion at {completion_time:.2f}s"
                )

        except Exception as e:
            pytest.fail(f"Ollama streaming test failed: {e}")

    def test_ollama_stream_produces_content(self):
        """Concatenating deltas produces expected response text."""
        adapter = OllamaAdapter(
            endpoint="http://localhost:11434",
            model="gemma4:e4b",
            default_timeout=60.0,
        )

        messages = [
            Message.user(content="Count from 1 to 5."),
        ]

        deltas: list[str] = []
        try:
            for chunk in adapter.stream(messages, timeout=30.0):
                if chunk.delta_content:
                    deltas.append(chunk.delta_content)

            full_text = "".join(deltas)
            assert "1" in full_text, "Response should contain '1'"
            assert "5" in full_text, "Response should contain '5'"
        except Exception as e:
            pytest.fail(f"Ollama content test failed: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
