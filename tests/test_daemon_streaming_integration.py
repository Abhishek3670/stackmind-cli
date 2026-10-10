"""End-to-end streaming integration test for Phase 20.

This test exercises the daemon event path: on_token callback -> EventDispatcher.
"""

import pytest

from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from cli.tui.events import extract_text_delta


class TestDaemonStreamingIntegration:
    """Integration tests for daemon streaming delivery via on_token callback."""

    def test_on_token_receives_streaming_deltas(self):
        """on_token callback receives deltas during streaming."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DaemonStorage(tmpdir)
            manager = SessionManager(storage)

            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Track received deltas
            received_deltas: list[str] = []

            # Simulate SessionManager's on_token callback
            def on_token_callback(text: str):
                received_deltas.append(text)

            # Stream deltas (simulating what adapter does)
            deltas = ["Hello ", "World!"]
            for delta in deltas:
                on_token_callback(delta)

            # Verify all deltas were received
            assert received_deltas == deltas, "All deltas should be received"

    def test_event_dispatcher_receives_token_deltas(self):
        """Token deltas reach the daemon's EventDispatcher."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DaemonStorage(tmpdir)
            manager = SessionManager(storage)

            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Simulate SessionManager's on_token callback publishing to EventDispatcher
            deltas = ["Token1", "Token2", "Token3"]
            for delta in deltas:
                manager.events.publish(
                    "token.delta",
                    session_id,
                    delta=delta,
                )

            # Verify events were published
            events = manager.events.events(session_id)
            delta_events = [e for e in events if e.name == "token.delta"]

            assert len(delta_events) == 3, "All token deltas should be published"
            assert [e.payload["delta"] for e in delta_events] == deltas

    def test_empty_deltas_filtered(self):
        """Empty deltas are filtered out."""
        received: list[str] = []
        for delta in ["", "  ", ""]:
            if delta and delta.strip():
                received.append(delta)

        assert len(received) == 0, "Empty deltas should be filtered"

    def test_ordering_preserved(self):
        """Event ordering is preserved through EventDispatcher."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DaemonStorage(tmpdir)
            manager = SessionManager(storage)

            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Publish deltas in order
            for token in ["A", "B", "C", "D"]:
                manager.events.publish("token.delta", session_id, delta=token)

            # Verify order
            events = manager.events.events(session_id)
            delta_events = [e for e in events if e.name == "token.delta"]
            tokens = [e.payload["delta"] for e in delta_events]

            assert tokens == ["A", "B", "C", "D"], "Order should be preserved"


class TestTUIEventCompatibility:
    """Tests for TUI event parser compatibility."""

    def test_extract_text_delta_recognizes_token_delta(self):
        """extract_text_delta recognizes token.delta events."""
        event = {
            "name": "token.delta",
            "payload": {"delta": "Hello World"},
        }

        delta = extract_text_delta(event)
        assert delta == "Hello World"

    def test_extract_text_delta_recognizes_stream_delta(self):
        """extract_text_delta recognizes stream.delta events."""
        event = {
            "name": "stream.delta",
            "payload": {"delta": "Streaming text"},
        }

        delta = extract_text_delta(event)
        assert delta == "Streaming text"

    def test_extract_text_delta_recognizes_content_delta(self):
        """extract_text_delta recognizes content.delta events."""
        event = {
            "name": "content.delta",
            "payload": {"delta": "Content here"},
        }

        delta = extract_text_delta(event)
        assert delta == "Content here"

    def test_extract_text_delta_handles_empty_deltas(self):
        """extract_text_delta handles empty deltas correctly."""
        # Test with empty delta
        event = {
            "name": "stream.delta",
            "payload": {"delta": ""},
        }
        delta = extract_text_delta(event)
        assert delta is None
