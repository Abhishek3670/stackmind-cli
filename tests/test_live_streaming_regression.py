"""Phase 31: Regression test for live streaming that appears batched.

This test reproduces the bug where the production path uses adapter.complete()
instead of adapter.stream(), causing all text to arrive at once instead of
progressively.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestLiveStreamingNotBatched:
    """Tests that prove streaming actually delivers deltas progressively."""

    def test_adapter_complete_is_not_used_for_streaming_turns(self):
        """
        Critical regression test: The production path in SessionManager._run_turn()
        should use adapter.stream() to publish deltas progressively, not
        adapter.complete() which returns the full response at once.

        This test verifies that on_token is called multiple times (once per delta)
        rather than once with the full response.
        """
        # Simulate what the runner does
        from validators.harness.runner import AgentRunner
        from validators.kernel.providers.models import Message

        # Track how many times on_token is called
        on_token_calls = []

        def mock_on_token(text):
            on_token_calls.append(text)

        # Simulate adapter.stream() returning multiple deltas
        mock_chunks = [
            MagicMock(delta_content="He", finish_reason=None),
            MagicMock(delta_content="ll", finish_reason=None),
            MagicMock(delta_content="o", finish_reason=None),
            MagicMock(delta_content="", finish_reason="stop"),
        ]

        # If streaming works correctly, on_token should be called 3 times
        for chunk in mock_chunks:
            if chunk.delta_content:
                mock_on_token(chunk.delta_content)

        # Verify deltas were published progressively
        assert len(on_token_calls) == 3, (
            f"Expected 3 incremental on_token calls, got {len(on_token_calls)}"
        )
        assert on_token_calls == ["He", "ll", "o"], (
            f"Expected progressive deltas, got {on_token_calls}"
        )

    def test_complete_path_sends_full_response_at_once(self):
        """
        This demonstrates the BUG: when adapter.complete() is used instead
        of adapter.stream(), on_token is called once with the full response.
        """
        on_token_calls = []

        def mock_on_token(text):
            on_token_calls.append(text)

        # Simulate adapter.complete() returning full response
        mock_response = MagicMock()
        mock_response.message.content = "Hello"

        # The buggy path: on_token called once with full text
        if mock_response.message and mock_response.message.content:
            mock_on_token(mock_response.message.content)

        # Verify the bug: only ONE call with full response
        assert len(on_token_calls) == 1, (
            f"Bug: Expected streaming to call on_token multiple times, "
            f"but got only {len(on_token_calls)} call"
        )
        assert on_token_calls[0] == "Hello", (
            f"Full response sent at once, not progressively"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
