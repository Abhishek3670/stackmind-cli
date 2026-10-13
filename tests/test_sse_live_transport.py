"""Phase 22: Live HTTP SSE transport verification.

Proves that an SSE client receives a text delta while the producer is
deliberately blocked before emitting the completion event.
"""

import tempfile
import threading
import time

import pytest

from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.server import LocalDaemon
from validators.kernel.tui.client import DaemonClient


class TestLiveSSETransport:
    """Tests for live HTTP SSE transport with real client/server."""

    def test_client_receives_delta_before_completion(self):
        """SSE client receives first delta while producer is blocked before completion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Start daemon server
            daemon = LocalDaemon(tmpdir, port=0)
            daemon.start()

            # Create session
            manager = daemon.manager
            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Synchronization: signal when client is ready to receive
            client_ready = threading.Event()
            received_deltas: list[dict] = []
            connection_established = threading.Event()

            def run_client():
                """Run SSE client in background thread."""
                nonlocal connection_established
                client = DaemonClient(daemon.url)

                # Track first event received to signal connection
                first_event_received = False

                try:
                    # Start streaming - this opens HTTP connection
                    for event in client.stream_events(session_id=session_id, after=0):
                        if not first_event_received:
                            first_event_received = True
                            connection_established.set()

                        if event.get("name") in ("token.delta", "stream.delta"):
                            received_deltas.append(event)
                except Exception:
                    # Expected on disconnect
                    pass

            # Start client in background
            client_thread = threading.Thread(target=run_client, daemon=True)
            client_thread.start()

            # Wait for HTTP connection to be established
            if not connection_established.wait(timeout=2.0):
                raise RuntimeError("Client failed to connect to SSE endpoint")

            # Small delay to ensure server has processed connection
            time.sleep(0.1)

            # Publish first delta - this should arrive while we're still "blocked"
            manager.events.publish(
                "token.delta",
                session_id,
                delta="first token",
            )

            # Wait for delta to be received
            wait_start = time.time()
            while not received_deltas and (time.time() - wait_start) < 1.0:
                time.sleep(0.05)

            # ASSERTION 1: First delta received while blocked before completion
            assert len(received_deltas) >= 1, (
                "First delta should arrive before completion is published"
            )
            assert received_deltas[0]["payload"]["delta"] == "first token", (
                "First delta content should match"
            )

            # Publish completion - should NOT arrive yet because we haven't sent it
            manager.events.publish(
                "operation.completed",
                session_id,
                status="COMPLETED",
            )

            # Give time for completion to be processed
            time.sleep(0.2)

            # Second delta (simulating incremental streaming)
            manager.events.publish(
                "token.delta",
                session_id,
                delta="second token",
            )

            # Wait for second delta
            wait_start = time.time()
            while len(received_deltas) < 2 and (time.time() - wait_start) < 1.0:
                time.sleep(0.05)

            # ASSERTION 2: Second delta arrives in order
            assert len(received_deltas) >= 2, "Second delta should be received"
            assert received_deltas[1]["payload"]["delta"] == "second token"

            # Verify TUI extract_text_delta recognizes received payload
            from cli.tui.events import extract_text_delta

            delta1 = extract_text_delta(received_deltas[0])
            assert delta1 == "first token", "extract_text_delta should recognize token.delta"

            # Cleanup
            daemon.stop()

            assert True  # All assertions passed

    def test_session_isolation_via_live_transport(self):
        """Events for one session are not delivered to another session's SSE stream."""
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = LocalDaemon(tmpdir, port=0)
            daemon.start()

            manager = daemon.manager
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

            session1_deltas: list[str] = []
            session2_deltas: list[str] = []

            def client_for_session(session_id, delta_list: list[str]):
                client = DaemonClient(daemon.url)
                for event in client.stream_events(session_id=session_id, after=0):
                    if event.get("name") == "token.delta":
                        delta_list.append(event["payload"]["delta"])

            t1 = threading.Thread(target=client_for_session, args=(session1, session1_deltas))
            t1.daemon = True
            t1.start()

            t2 = threading.Thread(target=client_for_session, args=(session2, session2_deltas))
            t2.daemon = True
            t2.start()

            time.sleep(0.2)

            manager.events.publish("token.delta", session1, delta="session1-only")
            manager.events.publish("token.delta", session2, delta="session2-only")

            time.sleep(0.3)

            assert "session1-only" in session1_deltas
            assert "session2-only" in session2_deltas
            assert "session2-only" not in session1_deltas
            assert "session1-only" not in session2_deltas

            daemon.stop()

    def test_event_ordering_preserved_via_http(self):
        """Events arrive via SSE in the order they were published."""
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = LocalDaemon(tmpdir, port=0)
            daemon.start()

            manager = daemon.manager
            session_id = manager.create_session(
                agent="claude",
                provider="test",
                contract={},
                workspace=tmpdir,
            )["session_id"]

            received_order: list[str] = []

            def stream_events():
                client = DaemonClient(daemon.url)
                for event in client.stream_events(session_id=session_id):
                    if event.get("name") == "token.delta":
                        received_order.append(event["payload"]["delta"])

            t = threading.Thread(target=stream_events)
            t.daemon = True
            t.start()

            time.sleep(0.2)

            for token in ["A", "B", "C"]:
                manager.events.publish("token.delta", session_id, delta=token)

            time.sleep(0.3)

            assert received_order == ["A", "B", "C"], (
                f"Events should arrive in order, got {received_order}"
            )

            daemon.stop()

    def test_empty_delta_filtered(self):
        """Empty deltas are filtered by extract_text_delta."""
        from cli.tui.events import extract_text_delta

        event = {"name": "token.delta", "payload": {"delta": ""}}
        assert extract_text_delta(event) is None
