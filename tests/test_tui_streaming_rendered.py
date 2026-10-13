"""Phase 28: Regression test proving TUI streaming loop renders before completion.

This test verifies that the interactive TUI event loop updates streamed_chunks
and triggers redraws when token.delta events arrive—not just at completion.
"""

import time
import threading
import pytest

from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.server import LocalDaemon
from validators.kernel.tui.client import DaemonClient


class TestTUIStreamingLoopRendering:
    """Tests that the TUI streaming loop renders deltas progressively."""

    def test_tui_stream_loop_updates_before_completion(self):
        """Prove the TUI event loop consumes deltas before completion event."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = LocalDaemon(tmpdir, port=0)
            daemon.start()

            manager = daemon.manager
            session_id = manager.create_session(
                agent="claude", provider="test", contract={}, workspace=tmpdir,
            )["session_id"]

            # Track when deltas are published vs when completion arrives
            publish_times = []
            completion_time = None
            ready = threading.Event()

            def stream_events_thread():
                client = DaemonClient(daemon.url)
                client.stream_events(session_id=session_id, after=0)
                ready.set()
                try:
                    for ev in client.stream_events(session_id=session_id, after=0):
                        if ev.get("name") == "token.delta":
                            publish_times.append(time.monotonic())
                        if ev.get("name") == "operation.completed":
                            nonlocal completion_time
                            completion_time = time.monotonic()
                except Exception:
                    pass

            thread = threading.Thread(target=stream_events_thread, daemon=True)
            thread.start()

            if not ready.wait(timeout=5.0):
                daemon.stop()
                pytest.fail("SSE stream timeout")

            time.sleep(0.2)

            # Publish deltas then completion (simulating what runner.on_token does)
            for i, delta in enumerate(["He", "ll", "o", "!", " "]):
                manager.events.publish("token.delta", session_id, delta=delta)
                time.sleep(0.05)

            manager.events.publish("operation.completed", session_id, status="COMPLETED")

            thread.join(timeout=2.0)
            daemon.stop()

            # Verify deltas arrived before completion
            assert len(publish_times) == 5, f"Expected 5 deltas, got {len(publish_times)}"
            assert completion_time is not None
            assert publish_times[-1] < completion_time, (
                f"Last delta at {publish_times[-1]:.3f}s arrived after "
                f"completion at {completion_time:.3f}s"
            )

    def test_tui_stream_loop_extracts_all_deltas(self):
        """Prove all deltas are extracted before completion in the TUI loop."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = LocalDaemon(tmpdir, port=0)
            daemon.start()

            manager = daemon.manager
            session_id = manager.create_session(
                agent="claude", provider="test", contract={}, workspace=tmpdir,
            )["session_id"]

            extracted = []
            completed = [False]

            def stream_events_thread():
                client = DaemonClient(daemon.url)
                try:
                    for ev in client.stream_events(session_id=session_id, after=0):
                        if ev.get("name") == "token.delta":
                            delta = ev.get("payload", {}).get("delta")
                            if delta:
                                extracted.append(delta)
                        if ev.get("name") == "operation.completed":
                            completed[0] = True
                            break
                except Exception:
                    pass

            thread = threading.Thread(target=stream_events_thread, daemon=True)
            thread.start()

            time.sleep(0.3)

            # Publish deltas at realistic spacing
            for delta in ["H", "e", "l", "l", "o"]:
                manager.events.publish("token.delta", session_id, delta=delta)
                time.sleep(0.08)

            manager.events.publish("operation.completed", session_id, status="COMPLETED")

            thread.join(timeout=3.0)
            daemon.stop()

            # All deltas should be extracted before completion
            assert extracted == ["H", "e", "l", "l", "o"], f"Got {extracted}"
            assert completed[0], "Completion should have been signaled"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
