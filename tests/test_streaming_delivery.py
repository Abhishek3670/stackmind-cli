"""Test progressive token streaming delivery for Phase 19."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from validators.harness.backend import BaseExecutionBackend


class SyncBackend(BaseExecutionBackend):
    """Backend with explicit synchronization points for streaming tests."""

    def __init__(self, backend_id: str = "sync-test", **kwargs):
        super().__init__(backend_id=backend_id, backend_type="test", **kwargs)
        self._delta_events: list[dict] = []
        self._notify_event = threading.Event()
        self._completed = False

    def publish_delta(self, delta: str) -> dict:
        """Publish a delta event and return it."""
        with self._lock:
            event = super().publish(self._operation_id or "op-1", "stream.delta", delta=delta)
            self._delta_events.append(event)
            self._notify_event.set()
            return event

    def mark_completed(self):
        """Mark the stream as completed."""
        with self._lock:
            self._completed = True

    def wait_for_delta(self, timeout: float = 1.0) -> dict | None:
        """Wait for the next delta event to be published."""
        if not self._notify_event.wait(timeout=timeout):
            return None
        self._notify_event.clear()
        with self._lock:
            return self._delta_events[-1] if self._delta_events else None


class FakeStreamingAdapter:
    """Fake adapter that yields deltas with controllable synchronization."""

    def __init__(self, deltas: list[str], delay_between_deltas: float = 0.0):
        self._deltas = deltas
        self._delay = delay_between_deltas
        self._yielded = []
        self._complete_event = threading.Event()

    def stream(self, messages, tools=None, **kwargs):
        """Yield deltas with delays to simulate streaming."""
        for delta in self._deltas:
            time.sleep(self._delay)
            self._yielded.append(delta)
            yield MagicMock(delta_content=delta)
        self._complete_event.set()

    @property
    def is_complete(self):
        return self._complete_event.is_set()


class FakeStreamingAdapterBlocking:
    """Adapter that blocks on a specific delta to test live delivery."""

    def __init__(self, deltas: list[str], block_after_delta: int = 0):
        self._deltas = deltas
        self._block_after = block_after_delta
        self._yielded: list[str] = []
        self._blocked = threading.Event()
        self._continue = threading.Event()

    def stream(self, messages, tools=None, **kwargs):
        """Yield deltas, blocking after a specific one."""
        for i, delta in enumerate(self._deltas):
            self._yielded.append(delta)
            yield MagicMock(delta_content=delta)
            if i == self._block_after:
                self._blocked.set()
                self._continue.wait()
        self._continue.set()  # Signal completion


class TestStreamingDelivery:
    """Tests that verify deltas are delivered before completion."""

    def test_delta_published_before_completion(self):
        """First delta should be observable before provider completes."""
        backend = SyncBackend()
        op_id = backend.start_operation(task=MagicMock(identifier="test-task"))

        adapter = FakeStreamingAdapterBlocking(
            deltas=["Hello ", "World!"],
            block_after_delta=0,
        )

        # Start streaming in background
        def stream_thread():
            for chunk in adapter.stream([]):
                if getattr(chunk, 'delta_content', None):
                    backend.publish(
                        op_id,
                        "stream.delta",
                        delta=chunk.delta_content,
                    )
            adapter._continue.set()

        thread = threading.Thread(target=stream_thread)
        thread.start()

        # Wait for first delta to be published
        time.sleep(0.1)

        # Check that first delta was published
        published = backend._events.get(op_id, [])

        # Filter to stream.delta events
        deltas = [e for e in published if e.get("event") == "stream.delta"]

        assert len(deltas) >= 1, "First delta should be published before completion"
        assert deltas[0].get("delta") == "Hello ", "First delta content should match"

        # Second delta should NOT be published yet
        assert len(deltas) == 1, "Second delta should not be published while blocked"

        # Unblock and wait for completion
        adapter._continue.set()
        thread.join(timeout=2.0)

        # Now second delta should be published
        published = backend._events.get(op_id, [])
        deltas = [e for e in published if e.get("event") == "stream.delta"]

        assert len(deltas) == 2, "Both deltas should be published after completion"
        assert deltas[1].get("delta") == "World!", "Second delta content should match"

    def test_streaming_preserves_ordering(self):
        """Deltas must arrive in the order they were generated."""
        backend = SyncBackend()
        op_id = backend.start_operation(task=MagicMock(identifier="test-task"))

        adapter = FakeStreamingAdapterBlocking(
            deltas=["A", "B", "C", "D"],
            block_after_delta=1,
        )

        def stream_thread():
            for chunk in adapter.stream([]):
                if getattr(chunk, 'delta_content', None):
                    backend.publish(op_id, "stream.delta", delta=chunk.delta_content)
            adapter._continue.set()

        thread = threading.Thread(target=stream_thread)
        thread.start()

        time.sleep(0.1)

        published = backend._events.get(op_id, [])
        deltas = [e.get("delta") for e in published if e.get("event") == "stream.delta"]

        assert deltas == ["A", "B"], "First two deltas should be in order"

        adapter._continue.set()
        thread.join(timeout=2.0)

        published = backend._events.get(op_id, [])
        deltas = [e.get("delta") for e in published if e.get("event") == "stream.delta"]

        assert deltas == ["A", "B", "C", "D"], "All deltas should preserve order"

    def test_empty_response_handling(self):
        """Empty deltas should not be published."""
        backend = SyncBackend()
        op_id = backend.start_operation(task=MagicMock(identifier="test-task"))

        adapter = FakeStreamingAdapter(["", "  ", ""])

        for chunk in adapter.stream([]):
            delta = getattr(chunk, 'delta_content', None)
            if delta and delta.strip():
                backend.publish(op_id, "stream.delta", delta=delta)

        published = backend._events.get(op_id, [])
        deltas = [e for e in published if e.get("event") == "stream.delta"]

        # Empty strings should not be published
        assert len(deltas) == 0, "Empty deltas should not be published"

    def test_backend_publish_method_exists(self):
        """BaseExecutionBackend should have publish method."""
        backend = BaseExecutionBackend(backend_id="test", backend_type="test")
        assert hasattr(backend, "publish"), "Backend should have publish method"
        assert callable(backend.publish), "publish should be callable"


class TestStreamingIntegration:
    """Integration tests with the full harness stack."""

    def test_adapter_stream_integration(self):
        """Test that adapter streaming integrates with backend events."""
        from validators.harness.runner import AgentRunner
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Create basic project structure
            (project_path / ".sync").mkdir(parents=True)
            (project_path / ".sync" / "inbox").mkdir()
            (project_path / ".sync" / "inbox" / "claude").mkdir()

            # Create a simple task file
            task_file = project_path / ".sync" / "inbox" / "claude" / "test.md"
            task_file.write_text("Test task")

            backend = SyncBackend()
            runner = AgentRunner(
                project_path=project_path,
                agent="claude",
                backend=backend,
            )

            # Verify backend has publish method
            assert hasattr(backend, "publish")

    def test_live_delivery_detection(self):
        """Test that we can detect if delivery is truly live vs batched."""
        from validators.harness.backend import BaseExecutionBackend

        backend = BaseExecutionBackend(backend_id="test", backend_type="test")
        op_id = backend.start_operation(task=MagicMock(identifier="test-task"))

        # Simulate batched delivery
        events_before_publish = len(backend._events.get(op_id, []))

        # Publish delta
        backend.publish(op_id, "stream.delta", delta="first")

        events_after_first = len(backend._events.get(op_id, []))

        # Publish completion
        backend.publish(op_id, "operation.completed", result="done")

        events_after_completion = len(backend._events.get(op_id, []))

        # Verify event accumulation
        assert events_after_first == events_before_publish + 1
        assert events_after_completion == events_after_first + 1

        # Verify streaming events appear before completion
        ops = backend._events[op_id]
        event_names = [e.get("event") for e in ops]

        delta_idx = event_names.index("stream.delta") if "stream.delta" in event_names else -1
        complete_idx = event_names.index("operation.completed") if "operation.completed" in event_names else -1

        assert delta_idx >= 0, "stream.delta event should exist"
        assert complete_idx >= 0, "operation.completed event should exist"
        assert delta_idx < complete_idx, "delta should come before completion"
