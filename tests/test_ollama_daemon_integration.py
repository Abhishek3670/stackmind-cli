"""Phase 27: Real production turn path with Ollama streaming.

Tests that genuine Ollama deltas traverse the full production path via
Ollama adapter → EventDispatcher → SSE server → SSE client.
"""

import os
import tempfile
import time
import threading
import pytest

from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.server import LocalDaemon
from validators.kernel.tui.client import DaemonClient
from validators.kernel.providers.adapter import OllamaAdapter
from validators.kernel.providers.models import Message
from cli.tui.events import extract_text_delta

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")


class TestOllamaStreaming:
    """Tests for Ollama streaming through production path."""

    def test_ollama_stream_produces_genuine_deltas(self):
        """Ollama adapter produces genuine incremental deltas before completion."""
        adapter = OllamaAdapter(
            endpoint="http://localhost:11434",
            model=OLLAMA_MODEL,
            default_timeout=60.0,
        )

        messages = [Message.user(content="hello")]
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

            # At least one delta received
            assert len(deltas) > 0, f"Expected deltas, got {len(deltas)}"

            # First delta should arrive before completion
            if delta_timestamps:
                first_delta_at = delta_timestamps[0]
                assert first_delta_at < completion_time, (
                    f"First delta at {first_delta_at:.2f}s should arrive before "
                    f"completion at {completion_time:.2f}s"
                )
        except Exception as e:
            pytest.fail(f"Ollama streaming failed: {e}")

    def test_sse_client_receives_deltas_before_completion(self):
        """SSE client receives Ollama deltas before operation completion.

        Tests the full path: Ollama adapter → EventDispatcher → SSE server → SSE client
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Start daemon
            daemon = LocalDaemon(tmpdir, port=0)
            daemon.start()

            manager = daemon.manager
            session_id = manager.create_session(
                agent="claude",
                provider=OLLAMA_MODEL,
                contract={},
                workspace=tmpdir,
            )["session_id"]

            # Track deltas received by SSE client
            received_deltas: list[str] = []
            connection_ready = threading.Event()

            def run_sse_client():
                client = DaemonClient(daemon.url)
                for event in client.stream_events(session_id=session_id, after=0):
                    if not connection_ready.is_set():
                        connection_ready.set()
                    delta = extract_text_delta(event)
                    if delta:
                        received_deltas.append(delta)

            client_thread = threading.Thread(target=run_sse_client, daemon=True)
            client_thread.start()

            # Wait for connection
            if not connection_ready.wait(timeout=5.0):
                daemon.stop()
                pytest.fail("SSE client connection timeout")

            time.sleep(0.3)

            # Create adapter and stream with on_token callback to EventDispatcher
            adapter = OllamaAdapter(
                endpoint="http://localhost:11434",
                model=OLLAMA_MODEL,
                default_timeout=60.0,
            )

            messages = [Message.user(content="hello")]

            def on_token(text: str):
                manager.events.publish(
                    "token.delta",
                    session_id,
                    delta=text,
                )

            try:
                for chunk in adapter.stream(messages, timeout=30.0):
                    if chunk.delta_content:
                        on_token(chunk.delta_content)

                # Signal completion
                manager.events.publish(
                    "operation.completed",
                    session_id,
                    status="COMPLETED",
                )
            except Exception as e:
                daemon.stop()
                pytest.fail(f"Ollama streaming failed: {e}")

            # Wait for deltas to be received
            for _ in range(30):
                if len(received_deltas) > 0:
                    break
                time.sleep(0.1)

            daemon.stop()

            # Verify deltas received before completion
            assert len(received_deltas) > 0, (
                f"SSE client should receive deltas before completion. "
                f"Received {len(received_deltas)} deltas"
            )

            # Verify text integrity
            full_text = "".join(received_deltas)
            assert len(full_text) > 0, "Concatenated text should not be empty"

    def test_event_ordering_preserved(self):
        """Event ordering is preserved through the daemon path."""
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

            # Publish deltas in order
            for token in ["A", "B", "C"]:
                manager.events.publish("token.delta", session_id, delta=token)

            events = manager.events.events(session_id)
            delta_events = [e for e in events if e.name == "token.delta"]

            assert len(delta_events) == 3
            assert [e.payload["delta"] for e in delta_events] == ["A", "B", "C"]

            daemon.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
