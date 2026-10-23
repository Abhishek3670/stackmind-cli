"""P0 regression: a provider turn must always reach a terminal state.

Reproduces the live-turn stall where the Architecture operation stayed
``RUNNING`` (and the runtime panel showed ``Architecture (running)``) after the
provider emitted a single token and then stopped producing.

Every test here is deterministic: controlled fake adapters, explicit
``threading.Event`` synchronisation, and monkeypatched timeouts instead of
sleep-based races.
"""

from __future__ import annotations

import threading
import time

import validators.kernel.providers.gateway as gateway_module
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.supervisor import Phase
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.providers.errors import (
    OperationCancelledError,
    ProviderError,
    ProviderStreamInterruptedError,
)
from validators.kernel.providers.gateway import ProviderGateway
from validators.kernel.providers.models import (
    Message,
    ProviderResponse,
    StreamChunk,
    TokenUsage,
)


class FakeStreamAdapter:
    """Controllable adapter whose stream mode is selected per test."""

    provider_name = "fake"
    model_name = "fake-1"

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.release = threading.Event()
        self.entered = threading.Event()
        self.complete_calls = 0
        self.abort_calls = 0
        self.aborted = threading.Event()
        self.abort_finished = threading.Event()
        self.abort_blocks_for = 0.0

    def abort(self) -> None:
        self.abort_calls += 1
        self.aborted.set()
        # Simulates http.client close() blocking on a read that is stuck in
        # another thread (the Windows behaviour observed against a silent
        # transport).
        if self.abort_blocks_for:
            threading.Event().wait(self.abort_blocks_for)
        # Release a fake producer that is blocked mid-stream.
        self.release.set()
        self.abort_finished.set()

    def stream(self, messages, *, tools=None, timeout=None, cancellation_token=None, **kwargs):
        self.entered.set()
        if self.mode == "silent":
            self.release.wait()
            return
        yield StreamChunk(delta_content="The")
        if self.mode == "hang":
            self.release.wait()
            return
        if self.mode == "premature":
            return
        if self.mode == "raise":
            raise ProviderError("connection reset by peer")
        if self.mode == "cancelled_error":
            # Mirrors OpenAICompatibleAdapter.stream: its own cancellation check
            # raises inside the iteration and is re-wrapped as a stream timeout.
            raise ProviderError("Connection or timeout error during stream: Operation was cancelled")
        if self.mode == "complete":
            yield StreamChunk(delta_content=" ball", finish_reason="stop")

    def complete(self, messages, *, tools=None, timeout=None, cancellation_token=None, **kwargs):
        self.complete_calls += 1
        return ProviderResponse(
            message=Message.assistant(content="full retry"),
            usage=TokenUsage(),
            finish_reason="stop",
            model="fake-1",
        )


def _gateway(adapter: FakeStreamAdapter) -> ProviderGateway:
    gateway = ProviderGateway(adapter, None)
    gateway.active_tools_list = []
    gateway.tools = ()
    return gateway


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """Poll ``predicate`` without assuming a fixed sleep ordering."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _run_in_thread(fn, timeout: float = 5.0):
    """Run ``fn`` in a worker thread and fail the test if it never returns."""
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - asserted by the caller
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"turn never reached a terminal state within {timeout}s"
    return box


def test_stream_ending_after_one_token_reports_partial_output():
    adapter = FakeStreamAdapter("premature")
    gateway = _gateway(adapter)
    tokens: list[str] = []
    gateway.on_token = tokens.append

    box = _run_in_thread(lambda: gateway.run_loop([Message.user("Build me a ball")], max_turns=1))

    exc = box.get("error")
    assert isinstance(exc, ProviderStreamInterruptedError), box
    assert exc.reason == "premature_end"
    assert exc.partial_text == "The"
    assert exc.chunks_received == 1
    # The single token was delivered progressively, not swallowed.
    assert tokens == ["The"]
    # No silent re-request: the truncated stream is reported, not retried.
    assert adapter.complete_calls == 0


def test_stream_that_never_terminates_times_out_and_aborts(monkeypatch):
    monkeypatch.setattr(gateway_module, "DEFAULT_STREAM_IDLE_TIMEOUT", 0.2)
    adapter = FakeStreamAdapter("hang")
    gateway = _gateway(adapter)
    tokens: list[str] = []
    gateway.on_token = tokens.append

    started = time.monotonic()
    box = _run_in_thread(lambda: gateway.run_loop([Message.user("Build me a ball")], max_turns=1))
    elapsed = time.monotonic() - started

    exc = box.get("error")
    assert isinstance(exc, ProviderStreamInterruptedError), box
    assert exc.reason == "idle_timeout"
    assert exc.partial_text == "The"
    assert exc.chunks_received == 1
    # The watchdog aborted the transport instead of leaving it blocked.
    assert adapter.abort_calls == 1
    assert adapter.aborted.is_set()
    # Bounded by the idle timeout (+ poll granularity), not by the 6+ minute stall.
    assert elapsed < 3.0


def test_blocking_abort_hook_cannot_delay_terminal_state(monkeypatch):
    """A close() that blocks on a stuck read must not hold the operation open."""
    monkeypatch.setattr(gateway_module, "DEFAULT_STREAM_IDLE_TIMEOUT", 0.2)
    adapter = FakeStreamAdapter("hang")
    adapter.abort_blocks_for = 2.0
    gateway = _gateway(adapter)

    started = time.monotonic()
    box = _run_in_thread(lambda: gateway.run_loop([Message.user("Build me a ball")], max_turns=1))
    elapsed = time.monotonic() - started

    exc = box.get("error")
    assert isinstance(exc, ProviderStreamInterruptedError), box
    assert exc.reason == "idle_timeout"
    assert elapsed < 1.5, f"turn waited for the blocking abort hook ({elapsed:.2f}s)"
    # The release still happens, just off the turn's critical path.
    assert _wait_until(adapter.abort_finished.is_set, timeout=3.0)


def test_stream_with_no_output_hits_first_chunk_timeout(monkeypatch):
    monkeypatch.setattr(gateway_module, "DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT", 0.2)
    monkeypatch.setattr(gateway_module, "DEFAULT_STREAM_IDLE_TIMEOUT", 30.0)
    adapter = FakeStreamAdapter("silent")
    gateway = _gateway(adapter)

    box = _run_in_thread(lambda: gateway.run_loop([Message.user("Build me a ball")], max_turns=1))

    exc = box.get("error")
    assert isinstance(exc, ProviderStreamInterruptedError), box
    assert exc.reason == "first_chunk_timeout"
    assert exc.partial_text == ""
    assert adapter.abort_calls == 1
    assert adapter.complete_calls == 0


def test_stream_exception_after_partial_output_preserves_text():
    adapter = FakeStreamAdapter("raise")
    gateway = _gateway(adapter)

    box = _run_in_thread(lambda: gateway.run_loop([Message.user("Build me a ball")], max_turns=1))

    exc = box.get("error")
    assert isinstance(exc, ProviderStreamInterruptedError), box
    assert exc.reason == "stream_error"
    assert exc.partial_text == "The"
    assert isinstance(exc.__cause__, ProviderError)
    assert "connection reset by peer" in str(exc)
    # A failure after output must not be masked by a silent second request.
    assert adapter.complete_calls == 0


def test_turn_deadline_terminates_stream_and_cleans_up(monkeypatch):
    monkeypatch.setattr(gateway_module, "DEFAULT_STREAM_IDLE_TIMEOUT", 30.0)
    adapter = FakeStreamAdapter("hang")
    gateway = _gateway(adapter)

    box = _run_in_thread(
        lambda: gateway.run_loop([Message.user("Build me a ball")], max_turns=1, timeout=0.2)
    )

    exc = box.get("error")
    assert isinstance(exc, ProviderStreamInterruptedError), box
    assert exc.reason == "deadline"
    assert adapter.abort_calls == 1
    # The producer thread was released, so nothing is left consuming the stream.
    producer_threads = [t for t in threading.enumerate() if t.name == "provider-stream"]
    assert _wait_until(lambda: all(not t.is_alive() for t in producer_threads))
    assert adapter.complete_calls == 0


def test_cancellation_during_a_silent_stream_aborts_transport(monkeypatch):
    monkeypatch.setattr(gateway_module, "DEFAULT_STREAM_IDLE_TIMEOUT", 30.0)
    adapter = FakeStreamAdapter("hang")
    gateway = _gateway(adapter)
    cancel_event = threading.Event()

    def _cancel_after_first_token(_text: str) -> None:
        cancel_event.set()

    gateway.on_token = _cancel_after_first_token

    box = _run_in_thread(
        lambda: gateway.run_loop(
            [Message.user("Build me a ball")], max_turns=1, cancellation_token=cancel_event
        )
    )

    assert isinstance(box.get("error"), OperationCancelledError), box
    assert adapter.abort_calls == 1
    assert adapter.aborted.is_set()


def test_cancellation_wrapped_as_provider_error_is_reported_as_cancelled(monkeypatch):
    """OpenAICompatibleAdapter wraps its own cancellation as a stream timeout.

    Cancelling must terminate the turn as cancelled, not as a provider stream
    failure (observed while validating the OpenRouter-compatible path).
    """
    monkeypatch.setattr(gateway_module, "DEFAULT_STREAM_IDLE_TIMEOUT", 30.0)
    adapter = FakeStreamAdapter("cancelled_error")
    gateway = _gateway(adapter)
    cancel_event = threading.Event()

    def _cancel_after_first_token(_text: str) -> None:
        cancel_event.set()

    gateway.on_token = _cancel_after_first_token

    box = _run_in_thread(
        lambda: gateway.run_loop(
            [Message.user("Build me a ball")], max_turns=1, cancellation_token=cancel_event
        )
    )

    assert isinstance(box.get("error"), OperationCancelledError), box
    assert adapter.abort_calls == 1


def test_valid_stream_with_terminal_chunk_still_completes():
    adapter = FakeStreamAdapter("complete")
    gateway = _gateway(adapter)
    tokens: list[str] = []
    gateway.on_token = tokens.append

    history = gateway.run_loop([Message.user("Build me a ball")], max_turns=1)

    assert tokens == ["The", " ball"]
    assert history[-1].content == "The ball"
    assert adapter.complete_calls == 0


def test_runner_result_preserves_partial_output_for_interrupted_stream():
    from validators.harness.runner import HarnessTask, provider_stream_interruption_result

    task = HarnessTask(
        kind="adhoc",
        identifier="op-123",
        path=None,
        title="Build me a ball",
        body="Build me a ball",
        query="Build me a ball",
    )
    exc = ProviderStreamInterruptedError(
        "Provider stream stalled: no output for 120s after 1 chunk(s)",
        partial_text="The",
        chunks_received=1,
        reason="idle_timeout",
    )

    result = provider_stream_interruption_result(task, exc, operation_id="op-123")

    assert result.status == "blocked"
    assert "Provider stream terminated before completion" in result.reason
    assert "Partial output preserved" in result.reason
    assert result.meta["partial_output"] == "The"
    assert result.meta["failure"]["failure_code"] == "PROVIDER_STREAM_INTERRUPTED"
    assert result.meta["blockers"] == [result.reason]


def _manager(tmp_path, monkeypatch) -> tuple[SessionManager, str]:
    monkeypatch.setenv("STALE_OPERATION_GRACE_SECONDS", "0")
    manager = SessionManager(DaemonStorage(tmp_path / "daemon"))
    session_id = manager.create_session(
        agent="claude",
        provider="test",
        contract={},
        workspace=str(tmp_path),
    )["session_id"]
    return manager, session_id


def test_stopped_operation_is_failed_with_reason_and_not_duplicated(tmp_path, monkeypatch):
    manager, session_id = _manager(tmp_path, monkeypatch)
    _cancel, op_id = manager.begin_operation(
        session_id, "turn", {"prompt": "Build me a ball"}, role="architecture"
    )
    # The operation is RUNNING but no worker thread is executing it.
    assert manager.get_operation(op_id)["status"] == "RUNNING"

    assert manager._reconcile_stopped_operations(session_id) is True

    record = manager.get_operation(op_id)
    assert record["status"] == "FAILED"
    assert "without a terminal state" in (record.get("error") or "")
    # Reconciliation terminalises the existing operation; it never dispatches another.
    assert [op["operation_id"] for op in manager.list_operations(session_id)] == [op_id]
    # Idempotent: a terminal operation is left alone.
    assert manager._reconcile_stopped_operations(session_id) is False


def test_supervisor_stops_at_failed_planning_without_new_work_orders(tmp_path, monkeypatch):
    """A failed planning turn is terminal: no retry storm, no duplicate work order."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState

    monkeypatch.setenv("STALE_OPERATION_GRACE_SECONDS", "0")
    manager = SessionManager(DaemonStorage(tmp_path / "daemon"))
    session_id = manager.create_session(
        agent="claude", provider="test", contract={}, workspace=str(tmp_path)
    )["session_id"]
    _cancel, op_id = manager.begin_operation(
        session_id, "turn", {"prompt": "Build me a ball"}, role="architecture"
    )
    dispatches: list[str] = []
    original_start_turn = manager.start_turn

    def _recording_start_turn(*args, **kwargs):
        dispatches.append(args[1] if len(args) > 1 else kwargs.get("prompt", ""))
        return original_start_turn(*args, **kwargs)

    monkeypatch.setattr(manager, "start_turn", _recording_start_turn)

    supervisor = LifecycleSupervisor(manager)
    state = supervisor.start_run(
        "run-p0", "Build me a ball", tmp_path, session_id, bootstrap_mode=True
    )
    state.phase = Phase.PLANNING
    state.planning_operation_id = op_id
    state.planning_wo_id = "WO-000"

    assert manager._reconcile_stopped_operations(session_id) is True
    result = supervisor.advance(state)
    assert state.phase == Phase.FAILED
    assert "Planning turn failed" in (state.error or "")
    # No replacement planning turn was dispatched.
    assert dispatches == []


def test_tui_reports_running_then_the_real_failure():
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState("demo-project")
    state.process_event(
        {
            "name": "turn.started",
            "payload": {
                "operation_id": "op-arch-1",
                "operation": "turn",
                "role": "architecture",
                "agent_id": "claude",
            },
        }
    )
    assert state.operations["op-arch-1"].status == "RUNNING"
    assert state.roles["Architecture"].display_state == "orchestrating"

    reason = "Provider stream stalled: no output for 120s after 1 chunk(s)"
    state.process_event(
        {
            "name": "operation.failed",
            "payload": {
                "operation_id": "op-arch-1",
                "operation": "turn",
                "role": "architecture",
                "status": "FAILED",
                "error": reason,
            },
        }
    )

    assert state.operations["op-arch-1"].status == "FAILED"
    assert state.roles["Architecture"].state == "FAILED"
    assert state.roles["Architecture"].display_state != "orchestrating"
    assert any("Provider stream stalled" in str(entry) for entry in state.activity_log)

    # The supervisor's terminal phase event carries the same blocker to the
    # runtime panel's error line.
    state.process_event(
        {
            "name": "run.phase",
            "payload": {"phase": "FAILED", "error": reason, "run_id": "run-1"},
        }
    )
    assert reason in (state.error or "")
