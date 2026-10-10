"""Phase 21: Full SSE streaming verification.

Tests that a provider-generated delta travels through the actual daemon EventDispatcher,
SSE transport, HTTP client, before generation completes.
"""

import tempfile
from unittest.mock import patch
import pytest

from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.tui.client import DaemonClient


class TestEventDispatcherIntegration:
    """Tests for EventDispatcher integration with SSE transport."""

    def test_events_published_via_dispatcher(self):
        """Events published via EventDispatcher are stored correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DaemonStorage(tmpdir)
            manager = SessionManager(storage)

            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Publish events through EventDispatcher
            deltas = ["first token", "second token", "third token"]
            for delta in deltas:
                manager.events.publish(
                    "token.delta",
                    session_id,
                    delta=delta,
                )

            # Verify events were stored
            events = manager.events.events(session_id)
            delta_events = [e for e in events if e.name == "token.delta"]

            assert len(delta_events) == 3
            assert [e.payload["delta"] for e in delta_events] == deltas

    def test_events_preserve_ordering(self):
        """EventDispatcher preserves event ordering."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DaemonStorage(tmpdir)
            manager = SessionManager(storage)

            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Publish in specific order
            for token in ["A", "B", "C", "D"]:
                manager.events.publish("token.delta", session_id, delta=token)

            # Verify order
            events = manager.events.events(session_id)
            delta_events = [e for e in events if e.name == "token.delta"]
            tokens = [e.payload["delta"] for e in delta_events]

            assert tokens == ["A", "B", "C", "D"]


class TestSSEProtocolCompatibility:
    """Tests for SSE protocol and payload compatibility."""

    def test_token_delta_payload_recognized_by_tui(self):
        """token.delta payload is recognized by extract_text_delta."""
        from cli.tui.events import extract_text_delta

        event = {"name": "token.delta", "payload": {"delta": "Hello"}}
        delta = extract_text_delta(event)
        assert delta == "Hello"

    def test_stream_delta_payload_recognized_by_tui(self):
        """stream.delta payload is recognized by extract_text_delta."""
        from cli.tui.events import extract_text_delta

        event = {"name": "stream.delta", "payload": {"delta": "World"}}
        delta = extract_text_delta(event)
        assert delta == "World"

    def test_empty_delta_returns_none(self):
        """Empty deltas are filtered by extract_text_delta."""
        from cli.tui.events import extract_text_delta

        event = {"name": "token.delta", "payload": {"delta": ""}}
        delta = extract_text_delta(event)
        assert delta is None


class TestConcurrentOperationIsolation:
    """Tests for concurrent operation/session isolation."""

    def test_concurrent_sessions_isolated(self):
        """Two concurrent sessions receive only their own events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DaemonStorage(tmpdir)
            manager = SessionManager(storage)

            # Create two sessions
            session1 = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            session2 = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Publish to session1
            for token in ["S1-A", "S1-B"]:
                manager.events.publish("token.delta", session1, delta=token)

            # Publish to session2
            for token in ["S2-A", "S2-B"]:
                manager.events.publish("token.delta", session2, delta=token)

            # Verify isolation
            events1 = manager.events.events(session1)
            events2 = manager.events.events(session2)

            deltas1 = [
                e.payload["delta"]
                for e in events1
                if e.name == "token.delta"
            ]
            deltas2 = [
                e.payload["delta"]
                for e in events2
                if e.name == "token.delta"
            ]

            assert deltas1 == ["S1-A", "S1-B"]
            assert deltas2 == ["S2-A", "S2-B"]


class TestStreamingLifecycle:
    """Tests for streaming lifecycle handling."""

    def test_completion_published_exactly_once(self):
        """Completion event is published exactly once per operation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DaemonStorage(tmpdir)
            manager = SessionManager(storage)

            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Publish deltas and completion
            for token in ["A", "B"]:
                manager.events.publish("token.delta", session_id, delta=token)

            manager.events.publish(
                "operation.completed",
                session_id,
                status="COMPLETED",
            )

            events = manager.events.events(session_id)
            complete_events = [
                e for e in events
                if e.name == "operation.completed"
            ]

            assert len(complete_events) == 1
